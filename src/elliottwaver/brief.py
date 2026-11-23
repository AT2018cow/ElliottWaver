"""Build the one-page close brief from an already prepared analysis."""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo

from elliottwaver.data.adjustment import adjust_from_issuance, provider_price_scale
from elliottwaver.data.periods import background_bundle
from elliottwaver.data.quality import missing_intervals
from elliottwaver.engine.indicators import calculate
from elliottwaver.engine.intraday_link import link_intraday_timeframes
from elliottwaver.engine.matcher import SearchBudget
from elliottwaver.engine.timeframe_link import link_timeframes
from elliottwaver.errors import DataError
from elliottwaver.evaluation.events import condition_hit
from elliottwaver.evaluation.outcomes import (
    outcome_window,
    period_dates,
    scheduled_dates,
    target_hit,
)
from elliottwaver.forecast import forecasts_for_brief
from elliottwaver.outlook import build_outlook
from elliottwaver.schemas.brief import (
    Brief,
    BriefAnchor,
    BriefCard,
    BriefCrossTimeframe,
    BriefIssue,
    BriefPeriod,
    BriefPrevious,
    BriefWindow,
    BriefWindowComparison,
    WindowParticipant,
    forecast_message,
)
from elliottwaver.schemas.market import AnalysisRequest, MarketDataBundle
from elliottwaver.schemas.wave import AnalysisResult, LevelCondition, WaveScenario
from elliottwaver.service import AnalysisService, prepare_bundle
from elliottwaver.util import digest

_CODE = re.compile(r"^[A-Z][A-Z0-9_]+$")


@dataclass(frozen=True)
class BriefRun:
    brief: Brief
    result: AnalysisResult
    prepared: MarketDataBundle
    source: MarketDataBundle
    weekly_result: AnalysisResult | None = None
    weekly_prepared: MarketDataBundle | None = None
    alternate_result: AnalysisResult | None = None
    alternate_prepared: MarketDataBundle | None = None
    qualified: tuple[WaveScenario, ...] = ()


def _structure_key(
    scenario: WaveScenario, prepared: MarketDataBundle, symbol: str, timeframe: str
) -> str:
    endpoints = []
    for wave in scenario.completed_waves:
        index = wave.pivot.index
        raw_id = (
            prepared.raw_bars[index].bar_id
            if 0 <= index < len(prepared.raw_bars)
            else wave.pivot.extreme_bar_id
        )
        endpoints.append((wave.label, wave.pivot.kind, raw_id))
    identity = [
        symbol,
        timeframe,
        scenario.pattern_type,
        scenario.stage,
        scenario.pattern_direction,
        endpoints,
    ]
    if scenario.variant is not None:
        identity.append(scenario.variant)
    return digest(identity)[:24]


def _difference(primary: WaveScenario, alternate: WaveScenario) -> str:
    if primary.pattern_direction != alternate.pattern_direction:
        return "方向不同"
    if (primary.pattern_type, primary.stage) != (
        alternate.pattern_type,
        alternate.stage,
    ):
        return (
            f"模板/阶段不同：{primary.pattern_type}/{primary.stage} "
            f"vs {alternate.pattern_type}/{alternate.stage}"
        )
    for left, right in zip(primary.completed_waves, alternate.completed_waves, strict=False):
        if (left.label, left.pivot.kind, left.pivot.extreme_bar_id) != (
            right.label,
            right.pivot.kind,
            right.pivot.extreme_bar_id,
        ):
            return (
                f"首个不同端点在 {left.pivot.extreme_at.date()} vs {right.pivot.extreme_at.date()}"
            )
    if len(primary.completed_waves) != len(alternate.completed_waves):
        return "已完成端点数不同"
    return "结构相同"


def _card(
    scenario: WaveScenario,
    *,
    rank: int,
    role: str,
    difference: str,
    structure_key: str,
) -> BriefCard:
    return BriefCard(
        scenario_id=scenario.scenario_id,
        original_rank=rank,
        structure_key=structure_key,
        role=role,
        difference=difference,
        name=scenario.name,
        pattern_type=scenario.pattern_type,
        variant=scenario.variant,
        pattern_direction=scenario.pattern_direction,
        next_leg_bias=scenario.next_leg_bias,
        stage=scenario.stage,
        degree=scenario.degree,
        parent_context=scenario.parent_context,
        parent_pattern=scenario.parent_pattern,
        parent_degree=scenario.parent_degree,
        parent_wave_label=scenario.parent_wave_label,
        subwave_status=scenario.subwave_status,
        quality_score=scenario.score.quality_score,
        coverage=scenario.score.coverage,
        relative_weight=scenario.relative_weight,
        conditions=scenario.conditions,
        target_zones=scenario.target_zones,
        warnings=scenario.warnings,
        structure=scenario.structure,
    )


def _select_cards(result: AnalysisResult, prepared: MarketDataBundle) -> tuple[BriefCard, ...]:
    if not result.scenarios:
        return ()
    symbol = result.instrument.canonical_symbol
    timeframe = result.timeframe
    primary = result.scenarios[0]
    primary_key = _structure_key(primary, prepared, symbol, timeframe)
    cards = [
        _card(
            primary,
            rank=1,
            role="PRIMARY",
            difference="主选",
            structure_key=primary_key,
        )
    ]
    seen = {primary_key}
    for rank, scenario in enumerate(result.scenarios[1:], 2):
        if len(cards) >= 3:
            break
        if scenario.degree != primary.degree:
            continue
        key = _structure_key(scenario, prepared, symbol, timeframe)
        if key in seen:
            continue
        seen.add(key)
        cards.append(
            _card(
                scenario,
                rank=rank,
                role="ALTERNATE",
                difference=_difference(primary, scenario),
                structure_key=key,
            )
        )
    return tuple(cards)


def _window(
    result: AnalysisResult, prepared: MarketDataBundle, details: tuple[BriefAnchor, ...]
) -> BriefWindow:
    anchors = result.diagnostics.anchors
    intraday = result.timeframe.endswith("m")
    anchor_times = tuple(detail.at for detail in details if detail.at)
    if intraday:
        start_at = min(anchor_times) if anchor_times else prepared.bars[0].start_at
        end_at = prepared.bars[-1].end_at
        zone = ZoneInfo(prepared.instrument.timezone)
        start = start_at.astimezone(zone).date()
        end = prepared.bars[-1].session_date
        bar_count = sum(1 for bar in prepared.bars if start_at <= bar.start_at <= end_at)
    else:
        start_at = None
        end_at = None
        start = min(anchors) if anchors else prepared.bars[0].session_date
        end = result.market_asof
        bar_count = sum(1 for bar in prepared.bars if start <= bar.session_date <= end)
    primary_start = None
    if result.scenarios and result.scenarios[0].completed_waves:
        primary_start = prepared.bars[
            result.scenarios[0].completed_waves[0].pivot.index
        ].session_date
    return BriefWindow(
        start=start,
        end=end,
        bar_count=max(1, bar_count),
        anchors=tuple(anchors),
        anchor_details=details,
        anchor_times=anchor_times if intraday else (),
        start_at=start_at,
        end_at=end_at,
        fallback=any(warning.startswith("ANCHOR_FALLBACK") for warning in result.warnings),
        primary_pattern_start=primary_start,
    )


def _issues(result: AnalysisResult) -> tuple[BriefIssue, ...]:
    issues: list[BriefIssue] = []
    seen: set[tuple[str, str]] = set()

    def add(code: str, message: str, scope: str = "analysis") -> None:
        key = (code, message)
        if key in seen:
            return
        seen.add(key)
        issues.append(BriefIssue(code=code, scope=scope, message=message))

    for warning in result.warnings:
        code = warning.split(":", 1)[0].strip()
        if _CODE.match(code):
            message = (
                warning[len(code) + 1 :].strip() if warning.startswith(f"{code}:") else warning
            )
            add(code, message)
        else:
            add("ENGINE_WARNING", warning)
    if result.diagnostics.search_truncated:
        add("SEARCH_LIMITED", "搜索达到预算限制，候选枚举不完整", "search")
    if result.status == "UNCLEAR" and not result.scenarios:
        add("NO_QUALIFIED_CANDIDATE", "当前没有达到质量与证据门槛的候选", "analysis")
    if result.status == "INSUFFICIENT_DATA":
        add("INSUFFICIENT_DATA", "输入数据不足或存在未知缺口", "data")
    if result.status == "STALE_DATA":
        add("STALE_DATA", "输入快照未覆盖请求截止时刻", "data")
    return tuple(issues)


def _omitted_top_k(result: AnalysisResult, cards: tuple[BriefCard, ...]) -> dict:
    shown = {card.scenario_id for card in cards}
    omitted = [scenario for scenario in result.scenarios if scenario.scenario_id not in shown]
    return {
        "omitted_top_k_count": len(omitted),
        "omitted_top_k_weight": min(1.0, sum(scenario.relative_weight for scenario in omitted)),
    }


def _period(result: AnalysisResult, prepared: MarketDataBundle, details) -> BriefPeriod:
    cards = _select_cards(result, prepared)
    return BriefPeriod(
        timeframe=result.timeframe,
        status=result.status,
        market_asof=result.market_asof,
        input_snapshot_id=result.input_snapshot_id,
        result_digest=result.result_digest,
        price_coordinate_id=result.price_coordinate_id,
        window=_window(result, prepared, details),
        cards=cards,
        omitted_weight=result.omitted_weight,
        **_omitted_top_k(result, cards),
        issues=_issues(result),
    )


def _bias(result: AnalysisResult) -> str:
    if not result.scenarios:
        return "UNKNOWN"
    return result.scenarios[0].next_leg_bias


def _timeframe_label(value: str) -> str:
    return {
        "1m": "1分钟线",
        "5m": "5分钟线",
        "15m": "15分钟线",
        "30m": "30分钟线",
        "60m": "60分钟线",
        "1d": "日线",
        "1w": "周线",
    }.get(value, value)


def _cross_timeframe(
    primary: AnalysisResult,
    background: AnalysisResult | None,
    background_timeframe: str | None = None,
) -> BriefCrossTimeframe:
    primary_bias = _bias(primary)
    if background is None:
        reason = (
            "盘中周期暂未取得固定配对背景" if primary.timeframe.endswith("m") else "周线背景不可用"
        )
        return BriefCrossTimeframe(
            state="NOT_AVAILABLE",
            primary_bias=primary_bias,
            primary_market_asof=primary.market_asof,
            reason=reason,
        )
    background_bias = _bias(background)
    label = _timeframe_label(background_timeframe or background.timeframe)
    if primary.status != "OK" or background.status != "OK":
        state = "UNDETERMINED"
        reason = f"主周期状态 {primary.status}，{label}状态 {background.status}"
    elif primary_bias == "UNKNOWN" or background_bias == "UNKNOWN":
        state = "UNDETERMINED"
        reason = f"主周期或{label}没有明确的下一段方向"
    elif primary_bias == background_bias:
        state = "ALIGNED"
        reason = f"主周期与{label}下一段方向一致"
    else:
        state = "CONFLICT"
        reason = f"主周期与{label}下一段方向相反"
    if background_timeframe == "1w" or (
        background_timeframe is None and background.timeframe == "1w"
    ):
        return BriefCrossTimeframe(
            state=state,
            primary_bias=primary_bias,
            weekly_bias=background_bias,
            primary_market_asof=primary.market_asof,
            weekly_market_asof=background.market_asof,
            background_timeframe="1w",
            background_bias=background_bias,
            background_market_asof=background.market_asof,
            reason=reason,
        )
    return BriefCrossTimeframe(
        state=state,
        primary_bias=primary_bias,
        primary_market_asof=primary.market_asof,
        background_timeframe=background_timeframe or background.timeframe,
        background_bias=background_bias,
        background_market_asof=background.market_asof,
        reason=reason,
    )


def _window_comparison(
    request: AnalysisRequest,
    primary: AnalysisResult,
    prepared: MarketDataBundle,
    alternate: AnalysisResult | None,
    alternate_prepared: MarketDataBundle | None,
    reason: str,
) -> BriefWindowComparison:
    if alternate is None or alternate_prepared is None:
        return BriefWindowComparison(state="INCONCLUSIVE", reason=reason)
    if request.anchor is not None or request.anchor_at is not None:
        return BriefWindowComparison(state="NOT_APPLICABLE", reason="手动锚点不参与自动换窗")
    anchors = tuple(dict.fromkeys(primary.diagnostics.anchors))
    if len(anchors) < 2:
        return BriefWindowComparison(state="NOT_APPLICABLE", reason="只有一个自动锚点")
    alternate_start = min(alternate.diagnostics.anchors)
    if alternate_start == min(anchors):
        return BriefWindowComparison(state="NOT_APPLICABLE", reason="实际搜索范围相同")
    if primary.diagnostics.search_truncated or alternate.diagnostics.search_truncated:
        return BriefWindowComparison(
            state="INCONCLUSIVE",
            alternate_start=alternate_start,
            alternate_bar_count=sum(
                1 for bar in alternate_prepared.bars if bar.session_date >= alternate_start
            ),
            reason="至少一次搜索被截断",
        )
    if not primary.scenarios or not alternate.scenarios:
        return BriefWindowComparison(
            state="INCONCLUSIVE",
            alternate_start=alternate_start,
            alternate_bar_count=sum(
                1 for bar in alternate_prepared.bars if bar.session_date >= alternate_start
            ),
            reason="至少一次运行没有可比较的首选",
        )
    primary_scenario = primary.scenarios[0]
    alternate_scenario = alternate.scenarios[0]
    primary_key = _structure_key(
        primary_scenario, prepared, primary.instrument.canonical_symbol, primary.timeframe
    )
    alternate_key = _structure_key(
        alternate_scenario,
        alternate_prepared,
        alternate.instrument.canonical_symbol,
        alternate.timeframe,
    )
    if primary_key == alternate_key:
        state = "SAME_COUNT"
        reason = "换窗后首选结构键相同"
    elif primary_scenario.next_leg_bias == alternate_scenario.next_leg_bias:
        state = "CHANGED_COUNT_SAME_BIAS"
        reason = "换窗后结构改变，但下一段方向相同"
    else:
        state = "CHANGED_BIAS"
        reason = "换窗后下一段方向改变"
    return BriefWindowComparison(
        state=state,
        primary_scenario_id=primary_scenario.scenario_id,
        alternate_scenario_id=alternate_scenario.scenario_id,
        alternate_start=alternate_start,
        alternate_bar_count=sum(
            1 for bar in alternate_prepared.bars if bar.session_date >= alternate_start
        ),
        reason=reason,
    )


def _advanced_windows(request, prepared, context, service, budget):
    result = context.result
    main_start = min(result.diagnostics.anchors)
    primary = result.scenarios[0] if result.scenarios else None
    main_pivot = primary.completed_waves[0].pivot if primary and primary.completed_waves else None
    opposite = (
        {"LOW": "MACRO_HIGH", "HIGH": "MACRO_LOW"}.get(main_pivot.kind) if main_pivot else None
    )
    eligible = [anchor for anchor in context.anchors if anchor.date > main_start]
    reverse = [anchor.date for anchor in eligible if anchor.origin == opposite]
    starts = [max(reverse)] if reverse else []
    remaining = [anchor.date for anchor in eligible if anchor.date not in starts]
    if remaining:
        starts.append(min(remaining))
    primary_key = (
        _structure_key(primary, prepared, result.instrument.canonical_symbol, result.timeframe)
        if primary is not None
        else None
    )
    participants = [
        WindowParticipant(
            start=main_start,
            scenario_id=primary.scenario_id if primary else None,
            structure_key=primary_key,
            next_leg_bias=primary.next_leg_bias if primary else "UNKNOWN",
            status=result.status,
            reason="主窗口",
        )
    ]
    comparisons = []
    seen_starts = {main_start}
    for start in starts:
        alt_req = request.model_copy(update={"start": start})
        try:
            other = service.analyze_with_context(alt_req, prepared, shared_budget=budget).result
        except DataError as exc:
            participants.append(
                WindowParticipant(start=start, status="INSUFFICIENT_DATA", reason=str(exc))
            )
            comparisons.append(
                (BriefWindowComparison(state="INCONCLUSIVE", reason=str(exc)), None, None)
            )
            continue
        actual_start = min(other.diagnostics.anchors)
        if actual_start in seen_starts:
            continue
        seen_starts.add(actual_start)
        candidate = other.scenarios[0] if other.scenarios else None
        key = (
            _structure_key(
                candidate, prepared, result.instrument.canonical_symbol, result.timeframe
            )
            if candidate is not None
            else None
        )
        if candidate is None or primary is None or result.status != "OK" or other.status != "OK":
            state, reason = "INCONCLUSIVE", "至少一套窗口的结构或搜索证据不足"
        elif primary.next_leg_bias != candidate.next_leg_bias:
            state, reason = "CHANGED_BIAS", "备选窗口的下一段方向与主窗口相反"
        elif key != primary_key:
            state, reason = "CHANGED_COUNT_SAME_BIAS", "备选窗口改变了首选结构"
        else:
            state, reason = "SAME_COUNT", "两个窗口得到相同结构"
        participants.append(
            WindowParticipant(
                start=actual_start,
                scenario_id=candidate.scenario_id if candidate else None,
                structure_key=key,
                next_leg_bias=candidate.next_leg_bias if candidate else "UNKNOWN",
                status=other.status,
                reason=reason,
            )
        )
        comparisons.append(
            (
                BriefWindowComparison(
                    state=state,
                    reason=reason,
                    primary_scenario_id=primary.scenario_id if primary else None,
                    alternate_scenario_id=candidate.scenario_id if candidate else None,
                    alternate_start=actual_start,
                    alternate_bar_count=sum(
                        bar.session_date >= actual_start for bar in prepared.bars
                    ),
                ),
                other,
                prepared,
            )
        )
    if not comparisons:
        return (
            BriefWindowComparison(state="NOT_APPLICABLE", reason="没有其他可比较的确认锚点"),
            None,
            None,
        )
    priority = {"CHANGED_BIAS": 3, "INCONCLUSIVE": 2, "CHANGED_COUNT_SAME_BIAS": 1, "SAME_COUNT": 0}
    selected, alternate_result, alternate = max(
        comparisons, key=lambda item: priority[item[0].state]
    )
    return (
        selected.model_copy(
            update={
                "participants": tuple(participants),
                "total_expansions": budget.expansions,
                "search_limits": tuple(sorted(budget.cutoffs)),
            }
        ),
        alternate_result,
        alternate,
    )


def build_brief(
    request: AnalysisRequest,
    source: MarketDataBundle,
    settings,
    *,
    timeframe_reason: str = "",
    include_weekly: bool = True,
    check_windows: bool = True,
    model: dict | None = None,
    models: tuple[dict, ...] | None = None,
    model_approved_at: datetime | None = None,
    retrospective: bool = False,
    model_issue: str | None = None,
    intraday_issued_at: datetime | None = None,
) -> BriefRun:
    service = AnalysisService(settings)
    advanced = settings.analysis.engine_profile == "hierarchical_v1"
    day_budget = SearchBudget(settings.search.max_expansions) if advanced else None
    background_request = None
    background_prepared = None
    background_context = None
    background_result = None
    background = None
    background_state = "NOT_REQUESTED"
    background_state_message = ""
    background_timeframe = None
    analysis_source = source
    if request.timeframe.endswith("m"):
        from elliottwaver.timeframes import BACKGROUND_TIMEFRAMES

        target = BACKGROUND_TIMEFRAMES.get(request.timeframe)
        if target is not None:
            try:
                derived = background_bundle(source, target, as_of=request.as_of)
                background_request = request.model_copy(
                    update={"timeframe": target, "anchor": None, "anchor_at": None}
                )
                background_prepared = prepare_bundle(background_request, derived)
                analysis_source = source.model_copy(update={"auxiliary": background_prepared.bars})
            except DataError as exc:
                background_state = "INSUFFICIENT_DATA"
                background_request = None
                background_prepared = None
                background_state_message = str(exc)
    prepared = prepare_bundle(request, analysis_source)
    context = service.analyze_with_context(request, prepared, shared_budget=day_budget)
    result = context.result
    issues = list(_issues(result))
    if model_issue:
        issues.append(BriefIssue(code="MODEL_INVALID", scope="forecast", message=model_issue))
    if background_state == "INSUFFICIENT_DATA":
        issues.append(
            BriefIssue(
                code="BACKGROUND_UNAVAILABLE",
                scope="background",
                message=background_state_message or "固定配对背景不可用",
            )
        )
    if background_request is not None and background_prepared is not None:
        try:
            background_context = service.analyze_with_context(
                background_request, background_prepared
            )
            background_result = background_context.result
            background_timeframe = background_prepared.timeframe
            background = _period(background_result, background_prepared, background_context.anchors)
            background_state = (
                "AVAILABLE"
                if background_result.status in {"OK", "UNCLEAR", "SEARCH_LIMITED"}
                else "INSUFFICIENT_DATA"
            )
        except DataError as exc:
            background_state = "INSUFFICIENT_DATA"
            issues.append(
                BriefIssue(code="BACKGROUND_UNAVAILABLE", scope="background", message=str(exc))
            )

    weekly_result = None
    weekly_prepared = None
    weekly_context = None
    weekly = None
    weekly_state = "NOT_REQUESTED"
    if include_weekly and request.timeframe == "1d" and source.timeframe == "1d":
        try:
            weekly_context, weekly_prepared = service.weekly_background(request, source)
            weekly_result = weekly_context.result
            weekly = _period(weekly_result, weekly_prepared, weekly_context.anchors)
            weekly_state = (
                "AVAILABLE"
                if weekly_result.status in {"OK", "UNCLEAR", "SEARCH_LIMITED"}
                else "INSUFFICIENT_DATA"
            )
        except DataError as exc:
            weekly_state = "INSUFFICIENT_DATA"
            issues.append(BriefIssue(code="WEEKLY_UNAVAILABLE", scope="weekly", message=str(exc)))

    indicators = calculate(prepared.bars)
    cards = _select_cards(result, prepared)
    wave_relations = ()
    if advanced and background_context is not None and background_prepared is not None:
        wave_relations = link_intraday_timeframes(
            background_context,
            background_prepared,
            context,
            prepared,
            as_of=request.as_of,
            depth=settings.search.max_subwave_depth,
            budget=SearchBudget(settings.search.max_expansions),
            scales=settings.analysis.scales,
        )
    elif advanced and weekly_context is not None and weekly_prepared is not None:
        wave_relations = link_timeframes(
            context,
            prepared,
            weekly_context,
            weekly_prepared,
            as_of=request.as_of,
            depth=settings.search.max_subwave_depth,
            budget=SearchBudget(settings.search.max_expansions),
        )
    forecast_weekly = weekly_result
    active_models = tuple(models) if models is not None else ((model,) if model is not None else ())
    if active_models and not include_weekly and source.timeframe == request.timeframe == "1d":
        try:
            forecast_weekly = service.weekly_background(request, source)[0].result
        except DataError:
            pass
    cross_timeframe = _cross_timeframe(
        result,
        background_result or weekly_result,
        background_timeframe or ("1w" if weekly_result is not None else None),
    )

    alternate_result = None
    alternate_prepared = None
    anchors = sorted(set(result.diagnostics.anchors))
    if (
        not check_windows
        or request.anchor is not None
        or request.anchor_at is not None
        or len(anchors) < 2
    ):
        if not check_windows:
            reason = "未启用换窗复核"
        elif request.anchor is not None or request.anchor_at is not None:
            reason = "手动锚点不参与自动换窗"
        else:
            reason = "只有一个自动锚点"
        window_comparison = BriefWindowComparison(state="NOT_APPLICABLE", reason=reason)
    elif not advanced:
        alternate_start = max(anchors)
        alternate_request = request.model_copy(update={"start": alternate_start, "anchor": None})
        try:
            alternate_prepared = prepare_bundle(alternate_request, source)
            alternate_result = service.analyze_prepared(alternate_request, alternate_prepared)
            window_comparison = _window_comparison(
                request, result, prepared, alternate_result, alternate_prepared, "换窗运行失败"
            )
        except DataError as exc:
            window_comparison = BriefWindowComparison(
                state="INCONCLUSIVE", reason=f"换窗运行失败：{exc}"
            )
    else:
        window_comparison, alternate_result, alternate_prepared = _advanced_windows(
            request, prepared, context, service, day_budget
        )

    live_intraday = request.mode == "LIVE" and result.timeframe.endswith("m")
    issue_now = live_intraday and intraday_issued_at is None
    source_snapshot_id = source.snapshot_id
    issued_at = request.as_of
    if live_intraday:
        issued_at = intraday_issued_at or max(request.as_of, datetime.now(UTC))
    outlook = build_outlook(cards, result, prepared, context.projections, issued_at=issued_at)
    forecast_inputs = dict(
        qualified=context.qualified,
        models=active_models,
        weekly=forecast_weekly,
        candidate_ids={card.scenario_id for card in cards},
        wave_relations=wave_relations,
        window_state=window_comparison.state,
    )
    forecasts = forecasts_for_brief(
        result,
        prepared,
        indicators,
        outlook=outlook,
        **forecast_inputs,
    )
    generated_at = datetime.now(UTC)
    while issue_now:
        issued_at = max(request.as_of, generated_at)
        if not outlook.periods or outlook.periods[0].start_at >= issued_at:
            outlook = outlook.model_copy(update={"issued_at": issued_at})
            break
        previous_cases = outlook.cases
        outlook = build_outlook(cards, result, prepared, context.projections, issued_at=issued_at)
        # Forecast features depend on the cases, while the calendar fixes the event window.
        if outlook.cases != previous_cases:
            forecasts = forecasts_for_brief(
                result, prepared, indicators, outlook=outlook, **forecast_inputs
            )
        generated_at = datetime.now(UTC)
    by_scenario = {}
    for item in forecasts:
        if item.scenario_id is not None:
            by_scenario.setdefault(item.scenario_id, []).append(item)
    cards = tuple(
        card.model_copy(update={"forecasts": tuple(by_scenario.get(card.scenario_id, ()))})
        for card in cards
    )
    if any(item.status == "APPROVED" for item in forecasts):
        forecast_status = "APPROVED"
    elif forecasts:
        forecast_status = "INSUFFICIENT_EVIDENCE"
    else:
        forecast_status = "UNCALIBRATED"

    brief = Brief(
        schema_version="1.2" if advanced else "1.1",
        brief_digest="",
        source_snapshot_id=source_snapshot_id,
        symbol=result.instrument.canonical_symbol,
        as_of=request.as_of,
        mode=request.mode,
        engine_version=result.engine_version,
        config_hash=result.config_hash,
        generated_at=generated_at,
        timeframe=result.timeframe,
        timeframe_reason=timeframe_reason,
        status=result.status,
        market_asof=result.market_asof,
        input_snapshot_id=result.input_snapshot_id,
        result_digest=result.result_digest,
        price_coordinate_id=result.price_coordinate_id,
        feed=result.feed,
        adjustment=result.adjustment,
        latest_price=result.latest_price,
        last_bar_end_at=prepared.bars[-1].end_at,
        last_bar_id=prepared.bars[-1].bar_id,
        last_bar_available_at=prepared.bars[-1].usable_at,
        window=_window(result, prepared, context.anchors),
        cards=cards,
        omitted_weight=result.omitted_weight,
        **_omitted_top_k(result, cards),
        issues=tuple(issues),
        weekly_state=weekly_state,
        weekly=weekly,
        background_state=background_state,
        background=background,
        cross_timeframe=cross_timeframe,
        wave_relations=wave_relations,
        outlook=outlook,
        window_comparison=window_comparison,
        forecasts=forecasts,
        forecast_status=forecast_status,
        forecast_note=forecast_message(forecast_status),
        model_approved_at=model_approved_at,
        retrospective=retrospective,
        decision_atr=float(indicators.atr[-1]) if math.isfinite(indicators.atr[-1]) else None,
        outcome_dates=scheduled_dates(prepared, result.market_asof, 20)
        if result.timeframe in {"1d", "1w"}
        else (),
    )
    brief_digest = digest(brief.model_dump(mode="json", exclude={"brief_digest", "generated_at"}))
    brief = brief.model_copy(update={"brief_digest": brief_digest})
    return BriefRun(
        brief=brief,
        result=result,
        prepared=prepared,
        source=source,
        weekly_result=weekly_result,
        weekly_prepared=weekly_prepared,
        alternate_result=alternate_result,
        alternate_prepared=alternate_prepared,
        qualified=context.qualified,
    )


def _source_revision(
    previous: MarketDataBundle | None,
    current: MarketDataBundle | None,
    *,
    decision: date,
    cutoff: datetime,
) -> tuple[str, int, int, str]:
    if previous is None or current is None:
        return "NOT_COMPARABLE", 0, 0, "上一份源快照不可读"
    differences = []
    if previous.instrument.canonical_symbol != current.instrument.canonical_symbol:
        differences.append("symbol")
    if previous.provenance.feed != current.provenance.feed:
        differences.append("feed")
    if previous.provenance.adjustment != current.provenance.adjustment:
        differences.append("adjustment")
    if previous.timeframe != current.timeframe:
        differences.append("timeframe")
    if differences:
        return "NOT_COMPARABLE", 0, 0, "数据口径不同：" + ", ".join(differences)
    previous_bars = {
        bar.start_at: bar
        for bar in previous.raw_bars
        if bar.session_date <= decision and bar.usable_at <= cutoff
    }
    current_bars = {
        bar.start_at: bar
        for bar in current.raw_bars
        if bar.session_date <= decision and bar.usable_at <= cutoff
    }
    common = sorted(set(previous_bars) & set(current_bars))
    if not common:
        return "NOT_COMPARABLE", 0, 0, "两份源快照没有重叠 K 线"
    revisions = 0
    for key in common:
        left, right = previous_bars[key], current_bars[key]
        if (
            left.open,
            left.high,
            left.low,
            left.close,
            left.volume,
            left.amount,
        ) != (
            right.open,
            right.high,
            right.low,
            right.close,
            right.volume,
            right.amount,
        ):
            revisions += 1
    previous_factors = {
        point.effective_date: point.factor
        for point in previous.factors
        if point.effective_date <= decision
    }
    current_factors = {
        point.effective_date: point.factor
        for point in current.factors
        if point.effective_date <= decision
    }
    if bool(previous_factors) != bool(current_factors):
        return "NOT_COMPARABLE", revisions, 0, "一侧缺少复权因子"
    common_factors = sorted(set(previous_factors) & set(current_factors))
    if previous_factors and not common_factors:
        return "NOT_COMPARABLE", revisions, 0, "两份源快照没有重叠因子日期"
    factor_revisions = sum(previous_factors[day] != current_factors[day] for day in common_factors)
    if revisions or factor_revisions:
        return (
            "REVISED",
            revisions,
            factor_revisions,
            f"重叠 K 线修订 {revisions} 根，因子修订 {factor_revisions} 个",
        )
    return "NONE", 0, 0, "重叠 K 线与因子一致"


def _brief_scope_change(previous: Brief, current: Brief) -> tuple[str, str] | None:
    differences = []
    if previous.symbol != current.symbol:
        differences.append("symbol")
    if previous.feed != current.feed:
        differences.append("feed")
    if previous.adjustment != current.adjustment:
        differences.append("adjustment")
    if previous.timeframe != current.timeframe:
        differences.append("timeframe")
    if differences:
        return "NOT_COMPARABLE", "数据口径不同：" + ", ".join(differences)
    return None


def _config_change(previous: Brief, current: Brief) -> tuple[str, str] | None:
    differences = []
    if previous.config_hash != current.config_hash:
        differences.append("config_hash")
    if previous.engine_version != current.engine_version:
        differences.append("engine_version")
    if differences:
        return "CONFIG_CHANGED", "引擎或配置变化：" + ", ".join(differences)
    return None


@dataclass(frozen=True)
class DecisionComparison:
    change: str
    reason: str
    structure_change: str | None = None
    structure_reason: str | None = None
    input_change: str = "NONE"
    data_revision: str = "NONE"
    revision_count: int = 0
    factor_revision_count: int = 0
    triggered_conditions: tuple[LevelCondition, ...] = ()
    events: tuple[dict, ...] = ()


def _structure_transition(
    previous: Brief,
    current: Brief,
    previous_result: AnalysisResult | None,
    current_result: AnalysisResult | None,
    previous_source: MarketDataBundle | None,
    *,
    legacy_view: bool,
) -> tuple[str, str, tuple[dict, ...]]:
    current_cards = current.cards
    previous_cards = previous.cards
    if not current_cards and not previous_cards:
        return "SAME_COUNT", "两次都没有合格候选", ()
    if not previous_cards:
        return (
            "COUNT_CHANGED",
            "上次没有候选，本次出现候选",
            ({"type": "structure", "previous": None, "current": current_cards[0].structure_key},),
        )
    if not current_cards:
        return (
            "COUNT_CHANGED",
            "上次有候选，本次没有候选",
            ({"type": "structure", "previous": previous_cards[0].structure_key, "current": None},),
        )
    current_primary = current_cards[0]
    previous_primary = previous_cards[0]
    old = previous_result.scenarios[0] if previous_result and previous_result.scenarios else None
    new = current_result.scenarios[0] if current_result and current_result.scenarios else None
    event = {
        "type": "structure",
        "previous": previous_primary.structure_key,
        "current": current_primary.structure_key,
        "previous_stage": previous_primary.stage,
        "current_stage": current_primary.stage,
    }
    if current_primary.structure_key == previous_primary.structure_key:
        tip_moved = (
            old is not None
            and new is not None
            and new.stage != "COMPLETE"
            and (
                old.developing_tip.extreme_at != new.developing_tip.extreme_at
                or (
                    previous.adjustment != "basis_date_qfq"
                    and old.developing_tip.price != new.developing_tip.price
                )
            )
        )
        previous_tree = previous_primary.structure
        current_tree = current_primary.structure
        previous_proof = (
            (previous_tree.status, previous_tree.verified_depth) if previous_tree else (None, None)
        )
        current_proof = (
            (current_tree.status, current_tree.verified_depth) if current_tree else (None, None)
        )
        status_changed = current_primary.subwave_status != previous_primary.subwave_status
        if not legacy_view and (status_changed or current_proof != previous_proof):
            event.update(
                {
                    "first_difference": "subwave_status" if status_changed else "structure_proof",
                    "previous_status": previous_primary.subwave_status,
                    "current_status": current_primary.subwave_status,
                    "previous_structure_status": previous_proof[0],
                    "current_structure_status": current_proof[0],
                    "previous_verified_depth": previous_proof[1],
                    "current_verified_depth": current_proof[1],
                }
            )
            return "EVIDENCE_CHANGED", "主选结构相同但子浪证据状态变化", (event,)
        if tip_moved:
            event.update(
                {
                    "first_difference": "developing_tip",
                    "previous_tip": old.developing_tip.extreme_at.isoformat()
                    if old is not None
                    else None,
                    "current_tip": new.developing_tip.extreme_at.isoformat()
                    if new is not None
                    else None,
                }
            )
            return "TIP_MOVED", "已确认端点相同，暂定端点移动", (event,)
        return "SAME_COUNT", "主选结构键与上次相同", (event,)
    if current_primary.structure_key in {card.structure_key for card in previous_cards[1:]}:
        event.update({"first_difference": "alternate_promoted"})
        return "ALTERNATE_PROMOTED", "上次的备选成为本次主选", (event,)
    if (
        old is not None
        and previous_source is not None
        and new is not None
        and current_primary.pattern_type == previous_primary.pattern_type
        and current_primary.pattern_direction == previous_primary.pattern_direction
        and current_primary.stage != previous_primary.stage
        and len(old.completed_waves) < len(new.completed_waves)
        and all(
            left.label == right.label
            and left.pivot.kind == right.pivot.kind
            and left.pivot.extreme_at == right.pivot.extreme_at
            for left, right in zip(old.completed_waves, new.completed_waves, strict=False)
        )
    ):
        event.update(
            {
                "first_difference": "stage",
                "previous_stage": previous_primary.stage,
                "current_stage": current_primary.stage,
            }
        )
        return "STAGE_ADVANCED", "同一模板的阶段发生变化", (event,)
    event.update({"first_difference": "structure_key"})
    return "COUNT_CHANGED", "主选结构发生变化", (event,)


def _intraday_triggered_conditions(
    previous: Brief,
    current: Brief,
    previous_source: MarketDataBundle | None,
    current_source: MarketDataBundle | None,
) -> tuple[tuple[LevelCondition, ...], tuple[dict, ...], str | None]:
    if (
        not previous.cards
        or previous_source is None
        or current_source is None
        or previous.last_bar_end_at is None
        or current.last_bar_end_at is None
    ):
        return (), (), None
    if any(
        condition.coordinate_id != previous.price_coordinate_id
        for condition in previous.cards[0].conditions
    ):
        return (), (), "PRICE_BASIS_UNVERIFIED: 条件价格坐标与原摘要不一致"
    scale = 1.0
    if (
        previous.adjustment == "provider_all"
        and previous_source.provenance.price_coordinate_id
        != current_source.provenance.price_coordinate_id
    ):
        scale = provider_price_scale(
            previous_source, current_source, previous.market_asof, issued_as_of=previous.as_of
        )
        if scale is None:
            return (), (), "PRICE_BASIS_UNVERIFIED: 发行时价格坐标无法核实"
    issued_at = previous.outlook.issued_at if previous.outlook is not None else None
    start = max(previous.last_bar_end_at, previous.as_of, issued_at or previous.as_of)
    end = current.last_bar_end_at
    qfq = previous.adjustment == "basis_date_qfq"
    source_bars = tuple(
        bar
        for bar in (current_source.raw_bars if qfq else current_source.bars)
        if bar.is_final
        and bar.usable_at <= current.as_of
        and start <= bar.start_at
        and bar.end_at <= end
    )
    if not source_bars:
        return (), (), None
    bars = source_bars
    if qfq:
        try:
            bars = adjust_from_issuance(
                source_bars, current_source.factors, previous_source.factors, previous.market_asof
            )
        except DataError:
            return (), (), "PRICE_BASIS_UNVERIFIED: 发行时复权因子不足"
    elif scale != 1.0:
        bars = tuple(
            bar.model_copy(
                update={
                    field: getattr(bar, field) * scale for field in ("open", "high", "low", "close")
                }
            )
            for bar in source_bars
        )
    gaps = tuple(gap for gap in missing_intervals(current_source) if start <= gap < end)
    triggered: list[LevelCondition] = []
    events: list[dict] = []
    issue = None
    card = previous.cards[0]
    for condition in card.conditions:
        active = max(start, condition.active_from)
        for source_bar, bar in zip(source_bars, bars, strict=True):
            if bar.start_at < active or not condition_hit(condition, bar):
                continue
            missing = any(active <= gap < bar.start_at for gap in gaps)
            status = "OBSERVED_AFTER_GAP" if missing else "TRIGGERED"
            if not missing:
                triggered.append(condition)
            else:
                issue = "INCOMPLETE_DATA: 缺口之后只记录已观测的触达"
            events.append(
                {
                    "type": "condition",
                    "status": status,
                    "kind": condition.kind,
                    "side": condition.side,
                    "op": condition.op,
                    "price": condition.price,
                    "observed": getattr(bar, condition.side.lower()),
                    "coordinate_id": condition.coordinate_id,
                    "session": bar.session_date.isoformat(),
                    "bar_id": source_bar.bar_id,
                    "start_at": source_bar.start_at.isoformat(),
                    "end_at": source_bar.end_at.isoformat(),
                }
            )
            break
    by_bar: dict[str, dict[str, str]] = {}
    for event in events:
        if event["type"] == "condition" and event["status"] == "TRIGGERED":
            by_bar.setdefault(event["bar_id"], {})[event["kind"]] = event["session"]
    for bar_id, kinds in by_bar.items():
        if {"CONFIRMATION", "INVALIDATION"} <= kinds.keys():
            events.append(
                {
                    "type": "outcome",
                    "status": "AMBIGUOUS_SAME_BAR",
                    "session": kinds["INVALIDATION"],
                    "bar_id": bar_id,
                }
            )
    return tuple(triggered), tuple(events), issue


def _triggered_conditions(
    previous: Brief,
    current: Brief,
    previous_source: MarketDataBundle | None,
    current_source: MarketDataBundle | None,
    *,
    scope_comparable: bool,
    revision_comparable: bool,
) -> tuple[tuple[LevelCondition, ...], tuple[dict, ...], str | None]:
    if current.timeframe.endswith("m"):
        if not scope_comparable or not revision_comparable:
            return (), (), None
        return _intraday_triggered_conditions(previous, current, previous_source, current_source)
    if (
        not previous.cards
        or previous_source is None
        or current_source is None
        or not scope_comparable
        or not revision_comparable
        or current.timeframe not in {"1d", "1w"}
    ):
        return (), (), None
    from elliottwaver.data.periods import as_weekly

    future_source = (
        as_weekly(current_source)
        if current.timeframe == "1w" and current_source.timeframe == "1d"
        else current_source
    )
    scale = 1.0
    if (
        previous.adjustment == "provider_all"
        and previous_source.provenance.price_coordinate_id
        != current_source.provenance.price_coordinate_id
    ):
        scale = provider_price_scale(previous_source, current_source, previous.market_asof)
    dates = period_dates(future_source.sessions, weekly=future_source.timeframe == "1w")
    horizon = sum(previous.market_asof < day <= current.market_asof for day in dates)
    if scale is None:
        return (), (), "PRICE_BASIS_UNVERIFIED: 发行时价格坐标无法核实"
    if not horizon:
        return (), (), None
    window = outcome_window(
        future_source,
        previous.market_asof,
        horizon,
        outcome_as_of=current.market_asof,
        issuance_factors=tuple(
            point
            for point in previous_source.factors
            if point.effective_date <= previous.market_asof
        ),
        price_scale=scale,
    )
    source_bars = {bar.session_date: bar for bar in future_source.bars}
    triggered: list[LevelCondition] = []
    events: list[dict] = []
    card = previous.cards[0]
    zone = ZoneInfo(previous_source.instrument.timezone)
    issue = None
    if any(
        condition.coordinate_id != previous.price_coordinate_id for condition in card.conditions
    ):
        return (), (), "PRICE_BASIS_UNVERIFIED: 条件价格坐标与原摘要不一致"
    for condition in card.conditions:
        active = max(previous.as_of, condition.active_from)
        if active > current.as_of:
            continue
        missing = False
        for day, bar in zip(window.dates, window.bars, strict=True):
            if day < active.astimezone(zone).date():
                continue
            source_bar = source_bars.get(day)
            if bar is None or source_bar is None or bar.usable_at > current.as_of:
                missing = True
                continue
            trigger_at = bar.end_at if condition.side == "CLOSE" else bar.start_at
            if trigger_at < active or not condition_hit(condition, bar):
                continue
            status = "OBSERVED_AFTER_GAP" if missing else "TRIGGERED"
            if not missing:
                triggered.append(condition)
            else:
                issue = "INCOMPLETE_DATA: 缺口之后只记录已观测的触达"
            events.append(
                {
                    "type": "condition",
                    "status": status,
                    "kind": condition.kind,
                    "side": condition.side,
                    "op": condition.op,
                    "price": condition.price,
                    "observed": getattr(bar, condition.side.lower()),
                    "coordinate_id": condition.coordinate_id,
                    "session": day.isoformat(),
                    "bar_id": source_bar.bar_id,
                }
            )
            if not missing and condition.kind == "INVALIDATION" and card.target_zones:
                first_target = card.target_zones[0]
                previous_hit = any(
                    earlier is not None
                    and active <= earlier.start_at
                    and target_hit(first_target, card.next_leg_bias, earlier)
                    for earlier_day, earlier in zip(window.dates, window.bars, strict=True)
                    if earlier_day < day
                )
                if not previous_hit and target_hit(first_target, card.next_leg_bias, bar):
                    events.append(
                        {
                            "type": "outcome",
                            "status": "AMBIGUOUS_SAME_BAR",
                            "session": day.isoformat(),
                            "bar_id": source_bar.bar_id,
                        }
                    )
            break
        if missing:
            issue = "INCOMPLETE_DATA: 条件窗口有未核实的交易会话"
    by_bar = {}
    for event in events:
        if event["type"] == "condition" and event["status"] == "TRIGGERED":
            by_bar.setdefault(event["bar_id"], {})[event["kind"]] = event["session"]
    for bar_id, kinds in by_bar.items():
        if {"CONFIRMATION", "INVALIDATION"} <= kinds.keys() and not any(
            event["type"] == "outcome" and event["bar_id"] == bar_id for event in events
        ):
            events.append(
                {
                    "type": "outcome",
                    "status": "AMBIGUOUS_SAME_BAR",
                    "session": kinds["INVALIDATION"],
                    "bar_id": bar_id,
                }
            )
    return tuple(triggered), tuple(events), issue


def compare_decisions(
    previous: Brief | None,
    current: Brief,
    *,
    previous_result: AnalysisResult | None = None,
    current_result: AnalysisResult | None = None,
    previous_source: MarketDataBundle | None = None,
    current_source: MarketDataBundle | None = None,
) -> DecisionComparison:
    """Compare two published decisions without reading or rewriting either report."""
    if previous is None:
        return DecisionComparison(change="FIRST_RECORD", reason="没有更早的同类摘要")
    legacy_view = current.schema_version != "1.2" and not current.timeframe.endswith("m")
    structure_change, structure_reason, structure_events = _structure_transition(
        previous, current, previous_result, current_result, previous_source, legacy_view=legacy_view
    )
    revision_state, revisions, factor_revisions, revision_reason = _source_revision(
        previous_source,
        current_source,
        decision=previous.market_asof,
        cutoff=previous.as_of,
    )
    scope_change = _brief_scope_change(previous, current)
    config_change = _config_change(previous, current)
    input_change = "NONE"
    if scope_change is not None:
        change, scope_reason = scope_change
        reason = f"{scope_reason}；结构：{structure_reason}"
        input_change = "SCOPE"
    elif revision_state == "NOT_COMPARABLE":
        change = "NOT_COMPARABLE"
        reason = f"{revision_reason}；结构：{structure_reason}"
        input_change = "NOT_COMPARABLE"
    elif config_change is not None:
        change, config_reason = config_change
        reason = config_reason
        if revision_state == "REVISED":
            reason += f"；{revision_reason}"
        reason += f"；结构：{structure_reason}"
        input_change = "CONFIG_CHANGED"
    elif revision_state == "REVISED":
        change = "DATA_REVISED"
        reason = f"{revision_reason}；结构：{structure_reason}"
        input_change = "DATA_REVISED"
    else:
        change = structure_change
        reason = structure_reason
    triggered, condition_events, trigger_issue = _triggered_conditions(
        previous,
        current,
        previous_source,
        current_source,
        scope_comparable=scope_change is None,
        revision_comparable=revision_state != "NOT_COMPARABLE",
    )
    if trigger_issue:
        reason += f"。{trigger_issue}"
    if (
        not legacy_view
        and triggered
        and any(condition.kind == "INVALIDATION" for condition in triggered)
        and change not in {"NOT_COMPARABLE", "CONFIG_CHANGED", "DATA_REVISED"}
    ):
        change = "HARD_INVALIDATED"
        reason = "上次主选的失效条件已经触发；" + reason
    elif (
        not legacy_view
        and current.status == "SEARCH_LIMITED"
        and previous.status != "SEARCH_LIMITED"
        and change not in {"NOT_COMPARABLE", "CONFIG_CHANGED", "DATA_REVISED"}
    ):
        change = "SEARCH_LIMITED"
        reason = "本次搜索被预算截断；" + reason
    return DecisionComparison(
        change=change,
        reason=reason,
        structure_change=structure_change,
        structure_reason=structure_reason,
        input_change=input_change,
        data_revision=revision_state,
        revision_count=revisions,
        factor_revision_count=factor_revisions,
        triggered_conditions=triggered,
        events=(*structure_events, *condition_events),
    )


def attach_previous(
    run: BriefRun,
    previous: Brief | None,
    previous_path: str | None = None,
    *,
    previous_source: MarketDataBundle | None = None,
    current_source: MarketDataBundle | None = None,
    previous_result: AnalysisResult | None = None,
) -> BriefRun:
    comparison = compare_decisions(
        previous,
        run.brief,
        previous_result=previous_result,
        current_result=run.result,
        previous_source=previous_source,
        current_source=current_source,
    )
    if previous is None:
        previous_info = BriefPrevious(change=comparison.change, reason=comparison.reason)
    else:
        previous_info = BriefPrevious(
            change=comparison.change,
            reason=comparison.reason,
            previous_digest=previous.brief_digest,
            previous_market_asof=previous.market_asof,
            previous_as_of=previous.as_of,
            previous_last_bar_end_at=previous.last_bar_end_at,
            previous_path=previous_path,
            structure_change=comparison.structure_change,
            data_revision=comparison.data_revision,
            revision_count=comparison.revision_count,
            factor_revision_count=comparison.factor_revision_count,
            triggered_conditions=comparison.triggered_conditions,
            events=comparison.events
            if run.brief.schema_version == "1.2" or run.brief.timeframe.endswith("m")
            else (),
            input_change=comparison.input_change
            if run.brief.schema_version == "1.2" or run.brief.timeframe.endswith("m")
            else None,
        )
    brief = run.brief.model_copy(update={"previous": previous_info})
    brief_digest = digest(brief.model_dump(mode="json", exclude={"brief_digest", "generated_at"}))
    brief = brief.model_copy(update={"brief_digest": brief_digest})
    return BriefRun(
        brief=brief,
        result=run.result,
        prepared=run.prepared,
        source=run.source,
        weekly_result=run.weekly_result,
        weekly_prepared=run.weekly_prepared,
        alternate_result=run.alternate_result,
        alternate_prepared=run.alternate_prepared,
        qualified=run.qualified,
    )
