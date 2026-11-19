"""Shared deterministic analysis facade used by CLI and replay."""

from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import uuid4

from eliotbot import ENGINE_VERSION
from eliotbot.config import Settings
from eliotbot.data.adjustment import adjust_to_basis
from eliotbot.data.periods import as_weekly
from eliotbot.data.quality import missing_intervals
from eliotbot.data.resample import weekly_bars
from eliotbot.data.router import route_symbol
from eliotbot.engine.degree import wave_degree
from eliotbot.engine.indicators import calculate
from eliotbot.engine.matcher import Candidate, search
from eliotbot.engine.parents import attach_parent_context
from eliotbot.engine.projector import project
from eliotbot.engine.scorer import score, weight_scenarios
from eliotbot.engine.span_selector import require_history, select_anchor_details
from eliotbot.engine.subwaves import SubwaveCertification, certify_subwaves
from eliotbot.engine.zigzag import extract
from eliotbot.errors import DataError, InputError
from eliotbot.schemas.brief import BriefAnchor
from eliotbot.schemas.market import AnalysisRequest, MarketDataBundle
from eliotbot.schemas.wave import AnalysisResult, SearchDiagnostics, WaveLabel, WaveScenario
from eliotbot.util import digest

TEMPLATE_LABELS = {
    "IMPULSE": "推动",
    "ZIGZAG_ABC": "锯齿ABC",
    "TRIANGLE": "三角形",
    "FLAT": "平台",
    "DIAGONAL": "楔形",
    "WXY": "双锯齿WXY",
}


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


class AnalysisService:
    def __init__(self, settings: Settings | None = None):
        self.settings = settings or Settings()

    def analyze(self, request: AnalysisRequest) -> AnalysisResult:
        from eliotbot.data.workflow import fetch_bundle

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
        self, request: AnalysisRequest, bundle: MarketDataBundle
    ) -> AnalysisContext:
        bars = bundle.bars
        config = self.settings
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
            search(bars, zigzags, anchors, config.search, exact_anchor=request.anchor is not None)
            if not gaps or request.allow_gaps
            else ([], None)
        )
        if request.anchor and not candidates:
            warnings.append("EXPLICIT_ANCHOR_UNMATCHED: 指定锚点没有匹配合法的当前计数")
        parent_links = attach_parent_context(candidates, request.timeframe)
        scenarios = []
        excluded: list[tuple[float, float, str, Candidate]] = []
        for candidate in candidates:
            certification = (
                certify_subwaves(
                    candidate,
                    zigzags,
                    config.analysis.scales,
                    config.search.max_subwave_depth,
                    bars,
                )
                if config.search.max_subwave_depth > 0
                else SubwaveCertification("UNAVAILABLE", None, (), 0)
            )
            subwave = certification.fit
            parent = parent_links[candidate.key]
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
            bias = -candidate.direction if candidate.stage == "COMPLETE" else candidate.direction
            if config.search.max_subwave_depth == 0:
                note = ("未启用子浪认证；当前为端点与价格规则候选",)
            elif certification.status == "VERIFIED":
                note = ("子浪规则认证通过",)
            elif certification.status == "UNVERIFIED":
                note = ("子浪规则认证未完全通过，查看 subwave_evidence",)
            else:
                note = ("子浪证据不足，当前为端点与价格规则候选",)
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
                    subwave_status=certification.status,
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
            candidates_found=len(candidates),
            candidates_qualified=len(scenarios),
            anchors=anchors,
            pivot_counts={
                str(scale): len(zz.confirmed)
                for scale, zz in zip(config.analysis.scales, zigzags, strict=True)
            },
        )
        result = AnalysisResult(
            run_id=uuid4().hex,
            result_digest="",
            engine_version=f"{ENGINE_VERSION}/{config.scoring.profile}",
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
            result.model_copy(update={"result_digest": identity}), qualified, anchor_details
        )
