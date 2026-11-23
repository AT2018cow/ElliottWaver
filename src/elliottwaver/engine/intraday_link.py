"""Intraday parent/child leg mapping on exact session intervals."""

from __future__ import annotations

from datetime import datetime

from elliottwaver.engine.hierarchical import match_interval
from elliottwaver.engine.indicators import calculate
from elliottwaver.engine.matcher import SearchBudget
from elliottwaver.engine.templates import allowed_child_templates_advanced
from elliottwaver.engine.zigzag import extract
from elliottwaver.schemas.market import MarketDataBundle
from elliottwaver.schemas.wave import PivotPoint, WaveRelation, WaveStructure


def _root(structure: WaveStructure | None):
    if structure is None:
        return None
    return next((node for node in structure.nodes if node.node_id == structure.root_id), None)


def _point(node, point_id: str):
    return next((point for point in node.points if point.point_id == point_id), None)


def _child_index(bars, pivot: PivotPoint) -> int | None:
    for index, bar in enumerate(bars):
        if bar.start_at <= pivot.extreme_at < bar.end_at:
            return index
    return None


def _child_pivot(pivot: PivotPoint, bars, index: int) -> PivotPoint:
    bar = bars[index]
    price = bar.high if pivot.kind == "HIGH" else bar.low
    return pivot.model_copy(
        update={
            "index": index,
            "extreme_bar_id": bar.bar_id,
            "price": price,
            "state": "CONFIRMED",
            "confirmed_at": max(pivot.confirmed_at or bar.usable_at, bar.usable_at),
        }
    )


def _verified(structure: WaveStructure | None, depth: int) -> bool:
    return (
        depth > 0
        and structure is not None
        and structure.status == "VERIFIED"
        and structure.verified_depth >= depth
    )


def link_intraday_timeframes(
    parent_context,
    parent_prepared: MarketDataBundle,
    child_context,
    child_prepared: MarketDataBundle,
    *,
    as_of: datetime,
    depth: int,
    budget: SearchBudget,
    scales: tuple[float, ...],
) -> tuple[WaveRelation, ...]:
    """Map parent structure legs to exact child-bar intervals."""
    a, b = parent_prepared.provenance, child_prepared.provenance
    if (
        parent_prepared.instrument.canonical_symbol != child_prepared.instrument.canonical_symbol
        or a.feed != b.feed
        or a.adjustment != b.adjustment
    ):
        return ()
    parent_scenarios = tuple(parent_context.qualified) or tuple(parent_context.result.scenarios)
    child_scenarios = tuple(child_context.qualified) or tuple(child_context.result.scenarios)
    if not parent_scenarios or not child_scenarios:
        return ()
    child_scenario = child_scenarios[0]
    indicators = calculate(child_prepared.bars)
    zigzags = tuple(
        extract(child_prepared.bars, indicators.atr, scale, child_prepared.instrument.tick_size)
        for scale in scales
    )
    interval_cache: dict = {}
    relations: list[WaveRelation] = []
    for parent in parent_scenarios:
        root = _root(parent.structure)
        if root is None:
            continue
        legs = root.child_links
        if legs:
            leg_items = [
                (
                    leg.wave_label,
                    _point(root, leg.left_id),
                    _point(root, leg.right_id),
                    index,
                )
                for index, leg in enumerate(legs)
            ]
        else:
            points = root.points
            leg_items = [
                (
                    str(index),
                    points[index],
                    points[index + 1],
                    index,
                )
                for index in range(len(points) - 1)
            ]
        for label, left_point, right_point, index in leg_items:
            if left_point is None or right_point is None:
                continue
            left = left_point.pivot
            right = right_point.pivot
            left_index = _child_index(child_prepared.bars, left)
            right_index = _child_index(child_prepared.bars, right)
            if left_index is None or right_index is None or left_index >= right_index:
                relations.append(
                    WaveRelation(
                        weekly_scenario_id=parent.scenario_id,
                        daily_scenario_id=child_scenario.scenario_id,
                        parent_wave_label=label,
                        relation="UNDETERMINED",
                        status="UNDETERMINED",
                        reason="父腿端点未落在子周期 K 线区间内",
                    )
                )
                continue
            allowed = allowed_child_templates_advanced(root.pattern_type, index, root.variant)
            if not allowed:
                continue
            child_left = _child_pivot(left, child_prepared.bars, left_index)
            child_right = _child_pivot(right, child_prepared.bars, right_index)
            remaining = budget.limit - budget.expansions
            allowance = min(remaining, max(1, remaining // max(1, len(leg_items) - index)))
            local = SearchBudget(allowance)
            match = match_interval(
                child_left,
                child_right,
                allowed,
                child_prepared.bars,
                zigzags,
                as_of=as_of,
                depth=depth + 1,
                budget=local,
                tick_size=child_prepared.instrument.tick_size,
                coordinate_id=child_prepared.provenance.price_coordinate_id,
                timeframe=child_prepared.timeframe,
                raw_bars=child_prepared.raw_bars,
                interval_cache=interval_cache,
            )
            budget.expansions += local.expansions
            budget.pruned += local.pruned
            budget.cache_hits += local.cache_hits
            limited = local.truncated or allowance == 0
            if limited:
                budget.mark_limit("INTRADAY_LINK_LIMIT")
            if match.template is not None:
                verified = _verified(match.structure, depth)
                reason = (
                    f"盘中父腿匹配 {match.template}，内部证据已验证"
                    if verified
                    else f"盘中父腿匹配 {match.template}，内部证据待补"
                )
                relations.append(
                    WaveRelation(
                        weekly_scenario_id=parent.scenario_id,
                        daily_scenario_id=child_scenario.scenario_id,
                        parent_wave_label=label,
                        relation="EXACT_LEG",
                        status="VERIFIED" if verified else "PROVISIONAL",
                        weekly_point_ids=(left_point.point_id, right_point.point_id),
                        daily_point_ids=tuple(point.extreme_bar_id for point in match.points),
                        structure=match.structure,
                        reason=reason,
                    )
                )
            else:
                relations.append(
                    WaveRelation(
                        weekly_scenario_id=parent.scenario_id,
                        daily_scenario_id=child_scenario.scenario_id,
                        parent_wave_label=label,
                        relation="UNDETERMINED",
                        status="UNDETERMINED",
                        weekly_point_ids=(left_point.point_id, right_point.point_id),
                        reason=(
                            "SEARCH_LIMITED: 盘中父腿搜索预算耗尽"
                            if limited
                            else f"暂未找到可关联的子周期结构（{match.reason_code or 'NO_MATCH'}）"
                        ),
                    )
                )
    order = {"VERIFIED": 0, "PROVISIONAL": 1, "INCOMPATIBLE": 2, "UNDETERMINED": 3}
    return tuple(sorted(relations, key=lambda item: order[item.status]))
