"""Connect daily counts to verified full-week endpoints on a common price basis."""

from bisect import bisect_left, bisect_right
from collections import defaultdict
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from datetime import datetime
from itertools import pairwise
from math import isclose

from elliottwaver.data.adjustment import adjust_to_basis, provider_price_scale
from elliottwaver.engine.hierarchical import is_confirmed, match_interval
from elliottwaver.engine.indicators import calculate
from elliottwaver.engine.matcher import SearchBudget
from elliottwaver.engine.templates import (
    TEMPLATES,
    allowed_child_templates_advanced,
    variant_allowed_in_parent,
)
from elliottwaver.engine.zigzag import ZigZagResult, extract
from elliottwaver.errors import DataError
from elliottwaver.schemas.market import MarketDataBundle
from elliottwaver.schemas.wave import (
    Pattern,
    PivotPoint,
    WaveLabel,
    WaveRelation,
    WaveScenario,
    WaveStructure,
)

_MAX_SCENARIOS = 8
_LABELS = {template.name: template.labels for template in TEMPLATES}


@dataclass(frozen=True)
class _HistoricalLeg:
    relation: WaveRelation
    parent_pattern: Pattern
    left: PivotPoint
    right: PivotPoint
    allowed: tuple[Pattern, ...]
    displayed: bool


def _options(context) -> tuple[WaveScenario, ...]:
    ranked = {item.scenario_id: item for item in context.qualified[:_MAX_SCENARIOS]}
    ranked.update({item.scenario_id: item for item in context.result.scenarios})
    return tuple(ranked.values())


def _waves(scenario: WaveScenario) -> tuple[WaveLabel, ...]:
    waves = scenario.completed_waves
    labels = _LABELS[scenario.pattern_type]
    if scenario.stage != "COMPLETE" and len(waves) < len(labels):
        waves += (WaveLabel(label=labels[len(waves)], pivot=scenario.developing_tip),)
    return waves


def _verified(structure: WaveStructure | None, depth: int) -> bool:
    return (
        depth > 0
        and structure is not None
        and structure.status == "VERIFIED"
        and structure.verified_depth >= depth
    )


def _weekly_on_daily_basis(daily: MarketDataBundle, weekly: MarketDataBundle):
    a, b = daily.provenance, weekly.provenance
    if (daily.instrument.market, daily.instrument.canonical_symbol, a.feed, a.adjustment) != (
        weekly.instrument.market,
        weekly.instrument.canonical_symbol,
        b.feed,
        b.adjustment,
    ):
        return (), None
    if a.adjustment == "basis_date_qfq":
        try:
            return adjust_to_basis(daily.raw_bars, daily.factors, basis_date=b.basis_date), 1.0
        except DataError:
            return (), None
    if a.adjustment == "provider_all" and a.price_coordinate_id != b.price_coordinate_id:
        return daily.bars, provider_price_scale(weekly, daily, b.basis_date)
    return daily.bars, 1.0


def _point_indices(
    pivot: PivotPoint,
    weekly_bars,
    aligned_daily,
    weekly_raw,
    daily_raw,
    scale: float,
    by_week,
    schedule,
    as_of: datetime,
) -> tuple[int, ...]:
    if not 0 <= pivot.index < len(weekly_bars):
        return ()
    bar = weekly_bars[pivot.index]
    if bar.usable_at > as_of:
        return ()
    key = bar.session_date.isocalendar()[:2]
    indices = by_week.get(key, ())
    if not indices or tuple(aligned_daily[i].session_date for i in indices) != tuple(schedule[key]):
        return ()
    rows = [aligned_daily[i] for i in indices]
    raw = [daily_raw[i] for i in indices]
    ref = weekly_raw[pivot.index]
    if (raw[0].open, max(b.high for b in raw), min(b.low for b in raw), raw[-1].close) != (
        ref.open,
        ref.high,
        ref.low,
        ref.close,
    ):
        return ()
    checks = (
        (rows[0].open, bar.open),
        (rows[-1].close, bar.close),
        (max(x.high for x in rows), bar.high),
        (min(x.low for x in rows), bar.low),
    )
    tolerance = dict(rel_tol=1e-9, abs_tol=1e-10)
    side = pivot.kind.lower()
    if not all(isclose(value * scale, target, **tolerance) for value, target in checks):
        return ()
    if not isclose(pivot.price, getattr(bar, side), **tolerance):
        return ()
    return tuple(
        i
        for i in indices
        if isclose(getattr(aligned_daily[i], side) * scale, pivot.price, **tolerance)
    )


def link_timeframes(
    daily_result,
    daily_bundle: MarketDataBundle,
    weekly_result,
    weekly_bundle: MarketDataBundle,
    *,
    as_of: datetime,
    depth: int,
    budget: SearchBudget | None = None,
) -> tuple[WaveRelation, ...]:
    budget = budget if budget is not None else SearchBudget(10_000)
    daily_scenarios = _options(daily_result)
    daily_waves = [(scenario, _waves(scenario)) for scenario in daily_scenarios]
    aligned, scale = _weekly_on_daily_basis(daily_bundle, weekly_bundle)
    by_week: dict[tuple, list[int]] = defaultdict(list)
    schedule: dict[tuple, list] = defaultdict(list)
    for index, bar in enumerate(daily_bundle.bars):
        if bar.usable_at <= as_of:
            by_week[bar.session_date.isocalendar()[:2]].append(index)
    for day in weekly_bundle.sessions:
        schedule[day.isocalendar()[:2]].append(day)
    visible_sessions = sorted(
        bar.session_date for bar in daily_bundle.bars if bar.usable_at <= as_of
    )
    visible_set = set(visible_sessions)
    missing = sorted(day for day in daily_bundle.sessions if day not in visible_set)
    mapped: dict[PivotPoint, tuple[int, ...]] = {}
    zigzags = getattr(daily_result, "zigzags", ())
    if not zigzags and scale is not None:
        scales = sorted(
            {scale for scenario in daily_scenarios for scale in scenario.discovered_scales}
        )
        atr = calculate(daily_bundle.bars).atr
        zigzags = tuple(
            extract(daily_bundle.bars, atr, value, daily_bundle.instrument.tick_size)
            for value in (scales or [1.0, 1.4, 2.4, 3.8])
        )
    actual: dict[tuple[int, str], PivotPoint] = {}
    for zigzag in zigzags:
        for point in zigzag.confirmed:
            if is_confirmed(point, as_of):
                key = (point.index, point.kind)
                if key not in actual or point.scale > actual[key].scale:
                    actual[key] = point

    relations = []
    historical: list[_HistoricalLeg] = []
    for weekly in _options(weekly_result):
        waves = _waves(weekly)
        for index, (first, last) in enumerate(pairwise(waves)):
            left, right = first.pivot, last.pivot
            identity = {
                "weekly_scenario_id": weekly.scenario_id,
                "parent_wave_label": last.label,
                "weekly_point_ids": (left.extreme_bar_id, right.extreme_bar_id),
            }
            if not budget.take():
                relations.append(
                    WaveRelation(**identity, reason="SEARCH_LIMITED: 日周关联预算耗尽")
                )
                continue
            if scale is None:
                relations.append(WaveRelation(**identity, reason="日周价格口径不可比"))
                continue
            for point in (left, right):
                if point not in mapped:
                    mapped[point] = _point_indices(
                        point,
                        weekly_bundle.bars,
                        aligned,
                        weekly_bundle.raw_bars,
                        daily_bundle.raw_bars,
                        scale,
                        by_week,
                        schedule,
                        as_of,
                    )
            starts, ends = mapped[left], mapped[right]
            if not starts or not ends:
                relations.append(
                    WaveRelation(**identity, reason="周极值无法在同一价格坐标的完整交易周中核实")
                )
                continue
            start, end = starts[0], ends[-1]
            fixed = is_confirmed(left, as_of) and is_confirmed(right, as_of)
            if right.state == "DEVELOPING":
                end = max(i for indices in by_week.values() for i in indices)
            if end <= start:
                continue
            lo, hi = daily_bundle.bars[start].session_date, daily_bundle.bars[end].session_date
            if bisect_right(missing, hi) > bisect_left(missing, lo):
                relations.append(WaveRelation(**identity, reason="父腿区间存在应有交易日缺口"))
                continue
            unique = len(starts) == len(ends) == 1
            allowed = allowed_child_templates_advanced(weekly.pattern_type, index, weekly.variant)
            direction = right.price > left.price
            found = False
            exact_found = False
            for daily, lower in daily_waves:
                if len(lower) < 2:
                    continue
                a, b = lower[0].pivot, lower[-1].pivot
                if a.index < start or b.index > end:
                    continue
                exact = (
                    a.index in starts
                    and b.index in ends
                    and (a.kind, b.kind) == (left.kind, right.kind)
                )
                fixed_child = all(is_confirmed(wave.pivot, as_of) for wave in lower)
                admissible = daily.pattern_type in allowed and variant_allowed_in_parent(
                    daily.variant, weekly.pattern_type, last.label
                )
                if (
                    exact
                    and unique
                    and fixed
                    and fixed_child
                    and (direction != (b.price > a.price) or (allowed and not admissible))
                ):
                    relation, status, reason = (
                        "INCOMPATIBLE",
                        "INCOMPATIBLE",
                        "子浪违反方向或父腿位置规则",
                    )
                elif (
                    exact
                    and unique
                    and fixed
                    and fixed_child
                    and admissible
                    and _verified(daily.structure, depth)
                ):
                    relation, status, reason = (
                        "EXACT_LEG",
                        "VERIFIED",
                        "完整周、价格及子浪证据均已核对",
                    )
                else:
                    relation = "EXACT_LEG" if exact else "CONTAINED"
                    status, reason = "PROVISIONAL", "已定位区间，端点、父腿或子浪证据仍待确认"
                relations.append(
                    WaveRelation(
                        **identity,
                        daily_scenario_id=daily.scenario_id,
                        relation=relation,
                        status=status,
                        daily_point_ids=tuple(wave.pivot.extreme_bar_id for wave in lower),
                        reason=reason,
                    )
                )
                found = True
                exact_found |= status == "VERIFIED"
            if not exact_found and fixed and unique and allowed:
                a, b = actual.get((start, left.kind)), actual.get((end, right.kind))
                if a is not None and b is not None:
                    if depth == 0:
                        if not found:
                            relations.append(
                                WaveRelation(
                                    **identity,
                                    relation="EXACT_LEG",
                                    status="PROVISIONAL",
                                    daily_point_ids=(a.extreme_bar_id, b.extreme_bar_id),
                                    reason="已核对历史父腿端点，未请求子浪认证",
                                )
                            )
                        continue
                    historical.append(
                        _HistoricalLeg(
                            WaveRelation(**identity), weekly.pattern_type, a, b, allowed, found
                        )
                    )
                    continue
            if not found:
                reason = (
                    "SEARCH_LIMITED: 日周关联预算耗尽"
                    if budget.truncated
                    else "暂未找到可关联的日线结构"
                )
                relations.append(WaveRelation(**identity, reason=reason))
    relations.extend(_historical_relations(historical, daily_bundle, zigzags, as_of, depth, budget))
    return _ordered(relations)


def _historical_relations(
    legs: Sequence[_HistoricalLeg],
    bundle: MarketDataBundle,
    zigzags: Sequence[ZigZagResult],
    as_of: datetime,
    depth: int,
    budget: SearchBudget,
) -> Iterator[WaveRelation]:
    interval_cache: dict = {}
    # Match displayed counts first. Each historical interval gets a share of the
    # remaining budget so a long early leg cannot suppress other parent links.
    for index, leg in enumerate(legs):
        remaining = budget.limit - budget.expansions
        allowance = min(remaining, max(1, remaining // (len(legs) - index)))
        local = SearchBudget(allowance)
        match = match_interval(
            leg.left,
            leg.right,
            leg.allowed,
            bundle.bars,
            zigzags,
            as_of=as_of,
            depth=depth + 1,
            budget=local,
            tick_size=bundle.instrument.tick_size,
            coordinate_id=bundle.provenance.price_coordinate_id,
            raw_bars=bundle.raw_bars,
            interval_cache=interval_cache,
        )
        budget.expansions += local.expansions
        budget.pruned += local.pruned
        budget.cache_hits += local.cache_hits
        limited = local.truncated or allowance == 0
        if limited:
            budget.mark_limit("HISTORICAL_RELATION_LIMIT")
        if (
            match.status == "VERIFIED"
            and match.template is not None
            and variant_allowed_in_parent(
                match.variant, leg.parent_pattern, leg.relation.parent_wave_label
            )
        ):
            verified = _verified(match.structure, depth)
            if verified:
                reason = f"历史父腿的 {match.template} 分段已验证"
            elif limited:
                reason = f"SEARCH_LIMITED: 历史父腿匹配 {match.template}，内部证据搜索未完成"
            else:
                reason = f"历史父腿匹配 {match.template} 的端点，待补内部证据"
            yield leg.relation.model_copy(
                update={
                    "relation": "EXACT_LEG",
                    "status": "VERIFIED" if verified else "PROVISIONAL",
                    "daily_point_ids": tuple(point.extreme_bar_id for point in match.points),
                    "structure": match.structure,
                    "reason": reason,
                }
            )
        elif not leg.displayed:
            reason = "SEARCH_LIMITED: 历史父腿预算耗尽" if limited else "暂未找到可关联的日线结构"
            yield leg.relation.model_copy(update={"reason": reason})


def _ordered(relations: list[WaveRelation]) -> tuple[WaveRelation, ...]:
    order = {"VERIFIED": 0, "PROVISIONAL": 1, "INCOMPATIBLE": 2, "UNDETERMINED": 3}
    return tuple(sorted(relations, key=lambda item: order[item.status]))
