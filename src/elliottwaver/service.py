"""Shared deterministic analysis facade used by CLI and replay."""

from dataclasses import dataclass, field
from datetime import UTC, datetime
from uuid import uuid4

from elliottwaver import ENGINE_VERSION
from elliottwaver.config import Settings
from elliottwaver.data.adjustment import adjust_to_basis
from elliottwaver.data.periods import as_weekly
from elliottwaver.data.quality import missing_intervals
from elliottwaver.data.resample import weekly_bars
from elliottwaver.data.router import route_symbol
from elliottwaver.engine.degree import wave_degree
from elliottwaver.engine.hierarchical import build_structure, is_confirmed
from elliottwaver.engine.indicators import calculate
from elliottwaver.engine.matcher import Candidate, SearchBudget, deduplicate_candidates, search
from elliottwaver.engine.parents import attach_parent_context
from elliottwaver.engine.projector import Projection, project, project_with_gates
from elliottwaver.engine.scorer import score, weight_scenarios
from elliottwaver.engine.span_selector import require_history, select_anchor_details
from elliottwaver.engine.subwaves import SubwaveCertification, certify_subwaves
from elliottwaver.engine.templates import variant_allowed_in_parent
from elliottwaver.engine.zigzag import ZigZagResult, extract
from elliottwaver.errors import DataError, InputError
from elliottwaver.schemas.brief import BriefAnchor
from elliottwaver.schemas.market import AnalysisRequest, MarketDataBundle
from elliottwaver.schemas.wave import (
    AnalysisResult,
    SearchDiagnostics,
    SubwaveLegEvidence,
    WaveLabel,
    WaveScenario,
)
from elliottwaver.util import digest

TEMPLATE_LABELS = {
    "IMPULSE": "推动",
    "ZIGZAG_ABC": "锯齿ABC",
    "TRIANGLE": "三角形",
    "FLAT": "平台",
    "DIAGONAL": "楔形",
    "WXY": "双锯齿WXY",
}


def engine_rules_version(settings: Settings) -> str:
    if settings.analysis.engine_profile == "hierarchical_v1":
        return f"{ENGINE_VERSION}/hierarchical-rules-v3"
    return ENGINE_VERSION


def prepare_bundle(request: AnalysisRequest, bundle: MarketDataBundle) -> MarketDataBundle:
    if request.timeframe != bundle.timeframe:
        if request.timeframe == "1w" and bundle.timeframe == "1d":
            daily = prepare_bundle(request.model_copy(update={"timeframe": "1d"}), bundle)
            return prepare_bundle(request, as_weekly(daily))
        raise InputError(
            f"Snapshot timeframe {bundle.timeframe} does not match {request.timeframe}"
        )
    instrument = route_symbol(request.symbol)
    if (
        instrument.canonical_symbol != bundle.instrument.canonical_symbol
        or instrument.market != bundle.instrument.market
    ):
        raise InputError("Snapshot security identity does not match the requested symbol")
    if request.feed and request.feed != bundle.provenance.feed:
        raise InputError("Requested feed does not match the frozen snapshot feed")
    if request.mode == "POINT_IN_TIME_REPLAY":
        raise InputError("PIT replay is not certified; use explicitly labeled FROZEN_DATA_REPLAY")
    beyond_coverage = request.as_of > bundle.as_of
    if beyond_coverage and not request.allow_stale:
        raise DataError(
            "STALE_DATA: requested as-of exceeds the frozen snapshot coverage; use --allow-stale explicitly"
        )
    indices = [
        i
        for i, bar in enumerate(bundle.bars)
        if bar.usable_at <= request.as_of
        and (request.end is None or bar.session_date <= request.end)
    ]
    if not indices:
        raise DataError("No completed available bars at the requested time")
    last = indices[-1]
    raw = bundle.raw_bars[: last + 1]
    bars = bundle.bars[: last + 1]
    factors = tuple(p for p in bundle.factors if p.effective_date <= raw[-1].session_date)
    provenance = bundle.provenance
    raw_context = tuple(
        b
        for b in bundle.raw_daily_context
        if b.usable_at <= request.as_of and b.end_at <= raw[-1].end_at
    )
    context = tuple(
        b
        for b in bundle.daily_context
        if b.usable_at <= request.as_of and b.end_at <= raw[-1].end_at
    )
    if beyond_coverage and "STALE_SNAPSHOT" not in provenance.warnings:
        provenance = provenance.model_copy(
            update={"warnings": provenance.warnings + ("STALE_SNAPSHOT",)}
        )
    if provenance.adjustment == "basis_date_qfq":
        if bundle.timeframe == "1w":
            if not raw_context:
                raise DataError(
                    "Weekly QFQ requires original daily bars; cannot adjust aggregated raw OHLC"
                )
            context = adjust_to_basis(raw_context, factors, basis_date=raw[-1].session_date)
            bars = weekly_bars(context, bundle.sessions)
            raw = weekly_bars(raw_context, bundle.sessions)
        else:
            bars = adjust_to_basis(raw, factors)
            if raw_context:
                context = adjust_to_basis(raw_context, factors, basis_date=raw[-1].session_date)
        provenance = provenance.model_copy(
            update={
                "basis_date": raw[-1].session_date,
                "factor_hash": digest(factors),
                "price_coordinate_id": digest(["sina", raw[-1].session_date, digest(factors)]),
            }
        )
    elif provenance.basis_date != bars[-1].session_date:
        provenance = provenance.model_copy(update={"basis_date": bars[-1].session_date})
    return MarketDataBundle(
        instrument=bundle.instrument,
        timeframe=bundle.timeframe,
        as_of=request.as_of,
        bars=bars,
        raw_bars=raw,
        factors=factors,
        provenance=provenance,
        sessions=bundle.sessions,
        auxiliary=tuple(b for b in bundle.auxiliary if b.usable_at <= request.as_of),
        daily_context=context,
        raw_daily_context=raw_context,
        expected_bar_starts=tuple(t for t in bundle.expected_bar_starts if t <= bars[-1].start_at),
    )


@dataclass(frozen=True)
class AnalysisContext:
    result: AnalysisResult
    qualified: tuple[WaveScenario, ...]
    anchors: tuple[BriefAnchor, ...]
    zigzags: tuple[ZigZagResult, ...] = ()
    projections: dict[str, Projection] = field(default_factory=dict)


class AnalysisService:
    def __init__(self, settings: Settings | None = None):
        self.settings = settings or Settings()

    def analyze(self, request: AnalysisRequest) -> AnalysisResult:
        from elliottwaver.data.workflow import fetch_bundle

        return self.analyze_bundle(request, fetch_bundle(request, self.settings))

    def analyze_bundle(self, request: AnalysisRequest, bundle: MarketDataBundle) -> AnalysisResult:
        return self.analyze_prepared(request, prepare_bundle(request, bundle))

    def analyze_prepared(
        self, request: AnalysisRequest, bundle: MarketDataBundle
    ) -> AnalysisResult:
        return self.analyze_with_context(request, bundle).result

    def weekly_background(self, request: AnalysisRequest, source: MarketDataBundle):
        weekly_request = request.model_copy(update={"timeframe": "1w", "anchor": None})
        prepared = prepare_bundle(weekly_request, source)
        return self.analyze_with_context(weekly_request, prepared), prepared

    def analyze_with_context(
        self,
        request: AnalysisRequest,
        bundle: MarketDataBundle,
        *,
        shared_budget: SearchBudget | None = None,
    ) -> AnalysisContext:
        bars = bundle.bars
        config = self.settings
        advanced = config.analysis.engine_profile == "hierarchical_v1"
        if advanced and request.timeframe not in {"1d", "1w"}:
            raise InputError("hierarchical_v1 supports 1d and 1w")
        require_history(bars)
        anchor_details, warnings = select_anchor_details(bundle, request, config.analysis)
        anchors = tuple(sorted({item.date for item in anchor_details}))
        warnings = list(bundle.provenance.warnings + warnings)
        if len(bars) > config.analysis.max_analysis_bars:
            warnings.append(
                f"ANALYSIS_WINDOW_TRUNCATED: {len(bars)} bars available; "
                f"anchor search uses the latest {config.analysis.max_analysis_bars}"
            )
        if request.start and bars[0].session_date > request.start:
            warnings.append(
                f"DATA_START_CLIPPED: requested {request.start}; first bar {bars[0].session_date}"
            )
        if len(bars) < config.analysis.warmup_bars + config.analysis.min_anchor_bars:
            warnings.append("WARMUP_TRUNCATED: 可用历史不足完整预热与分析跨度")
        if not bundle.instrument.identity_verified:
            warnings.append("IDENTITY_UNVERIFIED: 证券身份来自离线输入，未经在线核实")
        gaps = missing_intervals(bundle, min(anchors))
        if gaps:
            if request.allow_gaps:
                warnings.append(
                    f"ALLOWED_GAPS: {len(gaps)} unknown gaps ignored by explicit --allow-gaps"
                )
            else:
                warnings.append(f"UNKNOWN_SESSION_GAPS: 分析范围有{len(gaps)}个未知行情缺口")
        stale = "STALE_SNAPSHOT" in bundle.provenance.warnings
        if request.end and bundle.timeframe == "1d":
            eligible = [day for day in bundle.sessions if day <= request.end]
            stale = stale or bool(eligible and eligible[-1] != bars[-1].session_date)
        indicators = calculate(bars)
        zigzags = [
            extract(bars, indicators.atr, scale, bundle.instrument.tick_size)
            for scale in config.analysis.scales
        ]
        candidates, budget = (
            search(
                bars,
                zigzags,
                anchors,
                config.search,
                exact_anchor=request.anchor is not None,
                tick_size=bundle.instrument.tick_size,
                advanced=advanced,
                budget=shared_budget,
            )
            if not gaps or request.allow_gaps
            else ([], None)
        )
        if request.anchor and not candidates:
            warnings.append("EXPLICIT_ANCHOR_UNMATCHED: 指定锚点没有匹配合法的当前计数")
        parent_links = attach_parent_context(candidates, request.timeframe)
        scenarios = []
        projections: dict[str, Projection] = {}
        interval_cache = {}
        excluded: list[tuple[float, float, str, Candidate]] = []
        structural_rejections = 0
        depth = config.search.max_subwave_depth
        for candidate in candidates:
            parent = parent_links[candidate.key]
            scenario_variant = candidate.variant if advanced else None
            structure = None
            if advanced:
                if candidate.template.name == "DIAGONAL" and parent.status == "VERIFIED":
                    if parent.wave_label in {"1", "A"}:
                        scenario_variant = "LEADING_DIAGONAL"
                    elif parent.wave_label in {"5", "C"}:
                        scenario_variant = "ENDING_DIAGONAL"
                confirmed_ends = all(is_confirmed(p, request.as_of) for p in candidate.completed)
                if scenario_variant in {"TRUNCATED_FIFTH", "DOUBLE_THREE"} and (
                    candidate.developing or depth == 0 or not confirmed_ends
                ):
                    continue
                if parent.status == "VERIFIED" and not variant_allowed_in_parent(
                    scenario_variant, parent.parent_pattern, parent.wave_label
                ):
                    structural_rejections += 1
                    continue
                structure = build_structure(
                    candidate,
                    bars,
                    zigzags,
                    as_of=request.as_of,
                    coordinate_id=bundle.provenance.price_coordinate_id,
                    depth=depth,
                    budget=budget,
                    timeframe=request.timeframe,
                    variant=scenario_variant,
                    raw_bars=bundle.raw_bars,
                    tick_size=bundle.instrument.tick_size,
                    interval_cache=interval_cache,
                )
                if structure.status == "REJECTED":
                    structural_rejections += 1
                    continue
                root = structure.nodes[0]
                nodes = {node.node_id: node for node in structure.nodes}
                verified_fifth = any(
                    leg.wave_label == "5"
                    and leg.status == "VERIFIED"
                    and leg.child_node_id in nodes
                    and nodes[leg.child_node_id].pattern_type in {"IMPULSE", "DIAGONAL"}
                    for leg in root.child_links
                )
                if scenario_variant == "TRUNCATED_FIFTH" and not verified_fifth:
                    continue
                if scenario_variant == "DOUBLE_THREE" and structure.verified_depth < 1:
                    continue
                if scenario_variant == "RUNNING_FLAT" and structure.verified_depth < 1:
                    continue
                if (
                    scenario_variant not in {"TRUNCATED_FIFTH", "CONTRACTING_DIAGONAL"}
                    and structure.verified_depth < 1
                ):
                    scenario_variant = None
                evidence = tuple(
                    SubwaveLegEvidence(
                        leg=leg.wave_label,
                        status=(
                            "VERIFIED"
                            if leg.status == "VERIFIED"
                            else "UNAVAILABLE"
                            if leg.status == "PENDING"
                            else "UNVERIFIED"
                        ),
                        pattern=nodes[leg.child_node_id].pattern_type
                        if leg.child_node_id in nodes
                        else None,
                        point_count=len(nodes[leg.child_node_id].points)
                        if leg.child_node_id in nodes
                        else 0,
                        depth=depth,
                        reason=leg.reason_code or "子浪端点和硬规则通过",
                    )
                    for leg in root.child_links
                )
                verified_legs = sum(leg.status == "VERIFIED" for leg in evidence)
                fit = (
                    verified_legs / len(evidence)
                    if any(leg.status != "UNAVAILABLE" for leg in evidence)
                    else None
                )
                certification = SubwaveCertification(
                    "VERIFIED"
                    if structure.status == "VERIFIED"
                    else "UNAVAILABLE"
                    if fit is None
                    else "UNVERIFIED",
                    fit,
                    evidence,
                    structure.verified_depth,
                )
            else:
                certification = (
                    certify_subwaves(candidate, zigzags, config.analysis.scales, depth, bars)
                    if depth > 0
                    else SubwaveCertification("UNAVAILABLE", None, (), 0)
                )
            subwave = certification.fit
            breakdown = score(
                candidate,
                bars,
                indicators,
                volume_comparable=bundle.provenance.volume_comparable,
                profile=config.scoring.profile,
                subwave_fit=subwave,
            )
            groups = {e.group for e in breakdown.evidence if e.status == "AVAILABLE"}
            reasons = []
            if breakdown.quality_score < config.scoring.min_quality:
                reasons.append("quality")
            if breakdown.coverage < config.scoring.min_coverage:
                reasons.append("coverage")
            if len(groups) < 2:
                reasons.append("groups")
            if reasons:
                if (
                    not any(rule.status == "FAIL" for rule in candidate.rules)
                    and breakdown.coverage >= config.scoring.min_coverage
                ):
                    excluded.append(
                        (
                            breakdown.quality_score,
                            breakdown.coverage,
                            "+".join(reasons),
                            candidate,
                        )
                    )
                continue
            conditions, zones, path = project(
                candidate,
                bars,
                float(indicators.atr[-1]),
                bundle.provenance.price_coordinate_id,
                request.as_of,
                bundle.instrument.tick_size,
                timeframe=request.timeframe,
            )
            projection = project_with_gates(
                candidate,
                bars,
                float(indicators.atr[-1]),
                bundle.provenance.price_coordinate_id,
                request.as_of,
                bundle.instrument.tick_size,
                timeframe=request.timeframe,
                variant=scenario_variant,
            )
            projections[candidate.key] = projection
            bias = -candidate.direction if candidate.stage == "COMPLETE" else candidate.direction
            structure_status = structure.status if structure is not None else certification.status
            if config.search.max_subwave_depth == 0:
                note = ("未启用子浪认证；当前为端点与价格规则候选",)
            elif structure_status == "VERIFIED" or certification.status == "VERIFIED":
                note = ("子浪规则认证通过",)
            elif structure_status in {"UNVERIFIED", "PENDING", "SEARCH_LIMITED", "REJECTED"}:
                note = ("子浪规则认证未完全通过，查看 subwave_evidence",)
            else:
                note = ("子浪证据不足，当前为端点与价格规则候选",)
            if advanced and structure is not None:
                note += (f"HIERARCHICAL_STRUCTURE: {structure.status}",)
            if parent.status == "PROVISIONAL":
                note += (f"PARENT_CONTEXT_PROVISIONAL: {parent.reason}",)
            if not zones:
                note += ("NO_FEASIBLE_TARGET: 当前没有符合方向及硬边界的未触及目标",)
            scenarios.append(
                WaveScenario(
                    scenario_id=candidate.key,
                    name=(
                        f"{'上涨' if candidate.direction > 0 else '下跌'}"
                        f"{TEMPLATE_LABELS.get(candidate.template.name, candidate.template.name)}"
                        f" · {candidate.stage}"
                    ),
                    pattern_type=candidate.template.name,
                    pattern_direction="UP" if candidate.direction > 0 else "DOWN",
                    next_leg_bias="UP" if bias > 0 else "DOWN",
                    stage=candidate.stage,
                    degree=wave_degree(candidate.points, request.timeframe),
                    parent_context=parent.status,
                    parent_relation=parent.relation,
                    parent_scenario_id=parent.parent_key,
                    parent_pattern=parent.parent_pattern,
                    parent_degree=parent.parent_degree,
                    parent_wave_label=parent.wave_label,
                    subwave_status=(
                        "VERIFIED"
                        if structure is not None and structure.status == "VERIFIED"
                        else certification.status
                    ),
                    subwave_evidence=certification.evidence,
                    completed_waves=tuple(
                        WaveLabel(label=label, pivot=pivot)
                        for label, pivot in zip(
                            candidate.template.labels, candidate.completed, strict=False
                        )
                    ),
                    developing_tip=candidate.tip,
                    rules=candidate.rules,
                    score=breakdown,
                    conditions=conditions,
                    target_zones=zones,
                    projected_path=path,
                    discovered_scales=candidate.scales,
                    warnings=note,
                    structure=structure,
                    variant=scenario_variant,
                )
            )
        seen_excluded = set()
        for quality, coverage, reason, candidate in sorted(excluded, key=lambda item: -item[0])[:3]:
            points = " ".join(
                f"{label}={pivot.price:.2f}"
                for label, pivot in zip(candidate.template.labels, candidate.points, strict=False)
            )
            span = (
                f"{candidate.points[0].extreme_at.date()}→{candidate.points[-1].extreme_at.date()}"
            )
            message = (
                f"EXCLUDED_STRUCTURE: {candidate.template.name}/{candidate.stage} "
                f"q={quality:.1f} cov={coverage:.2f} reason={reason}; {span} {points}"
            )
            if message not in seen_excluded:
                seen_excluded.add(message)
                warnings.append(message)
        if advanced and scenarios:
            ranked = {scenario.scenario_id: scenario for scenario in scenarios}
            retained = deduplicate_candidates(
                [candidate for candidate in candidates if candidate.key in ranked],
                tick_size=bundle.instrument.tick_size,
                ranked=ranked,
            )
            scenarios = [
                ranked[candidate.key].model_copy(update={"discovered_scales": candidate.scales})
                for candidate in retained
            ]
        if structural_rejections:
            warnings.append(
                f"STRUCTURES_REJECTED: {structural_rejections} 个候选违反已核验子浪条件"
            )
        qualified, _ = weight_scenarios(
            scenarios, config.scoring.temperature, max(1, len(scenarios))
        )
        displayed = qualified[: request.top_k or config.analysis.top_k]
        omitted = max(0, 1 - sum(s.relative_weight for s in displayed)) if qualified else 0
        warnings.extend(
            f"PARENT_CONTEXT_PROVISIONAL: {scenario.scenario_id} "
            f"{parent_links[scenario.scenario_id].reason}"
            for scenario in displayed
            if scenario.parent_context == "PROVISIONAL"
        )
        degrees = {scenario.degree for scenario in displayed if scenario.degree != "UNKNOWN"}
        if len(degrees) > 1:
            warnings.append(
                "MIXED_DEGREE_SCENARIOS: "
                + ", ".join(sorted(degrees))
                + "; compare counts within the same degree"
            )
        status = (
            "OK"
            if displayed and displayed[0].score.quality_score >= config.scoring.unclear_below
            else "UNCLEAR"
        )
        if budget and budget.truncated:
            status = "SEARCH_LIMITED"
        if gaps and not request.allow_gaps:
            status = "INSUFFICIENT_DATA"
        if stale:
            status = "STALE_DATA"
        ambiguity = "HIGH"
        if len(displayed) == 1 and displayed[0].score.quality_score >= 70:
            ambiguity = "MEDIUM"
        elif len(displayed) > 1:
            margin = displayed[0].score.quality_score - displayed[1].score.quality_score
            if displayed[0].score.quality_score >= 70 and margin >= 15 and status == "OK":
                ambiguity = "LOW"
        diagnostics = SearchDiagnostics(
            expansions=budget.expansions if budget else 0,
            pruned=budget.pruned if budget else 0,
            search_truncated=budget.truncated if budget else False,
            cache_hits=budget.cache_hits if advanced and budget else 0,
            truncation_reason=(
                ",".join(sorted(budget.cutoffs)) if advanced and budget and budget.cutoffs else None
            ),
            candidates_found=len(candidates),
            candidates_qualified=len(scenarios),
            anchors=anchors,
            pivot_counts={
                str(scale): len(zz.confirmed)
                for scale, zz in zip(config.analysis.scales, zigzags, strict=True)
            },
        )
        engine_profile = (
            "/hierarchical_v1" if config.analysis.engine_profile == "hierarchical_v1" else ""
        )
        result = AnalysisResult(
            run_id=uuid4().hex,
            result_digest="",
            schema_version="1.1" if advanced else "1.0",
            engine_version=f"{engine_rules_version(config)}/{config.scoring.profile}{engine_profile}",
            config_hash=config.computation_hash,
            input_snapshot_id=bundle.snapshot_id,
            instrument=bundle.instrument,
            timeframe=request.timeframe,
            as_of=request.as_of,
            market_asof=bars[-1].session_date,
            analyzed_at=datetime.now(UTC),
            mode=request.mode,
            status=status,
            ambiguity=ambiguity,
            latest_price=bars[-1].close,
            latest_raw_close=bundle.raw_bars[-1].close,
            price_coordinate_id=bundle.provenance.price_coordinate_id,
            feed=bundle.provenance.feed,
            adjustment=bundle.provenance.adjustment,
            scenarios=displayed,
            omitted_weight=omitted,
            diagnostics=diagnostics,
            warnings=tuple(warnings),
        )
        identity = digest(
            result.model_dump(
                mode="json", exclude={"run_id", "result_digest", "analyzed_at", "mode"}
            )
        )
        return AnalysisContext(
            result.model_copy(update={"result_digest": identity}),
            qualified,
            anchor_details,
            tuple(zigzags),
            projections,
        )
