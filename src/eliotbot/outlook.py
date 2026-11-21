"""Build the short-term outlook from selected brief cards and gated projections."""

from __future__ import annotations

from datetime import date, timedelta

from eliotbot.data.calendar import load_calendar
from eliotbot.engine.projector import Projection
from eliotbot.schemas.brief import BriefCard
from eliotbot.schemas.market import MarketDataBundle
from eliotbot.schemas.outlook import (
    BriefOutlook,
    OutlookCase,
    OutlookCondition,
    OutlookEvent,
    OutlookPeriod,
)
from eliotbot.schemas.wave import AnalysisResult, LevelCondition

_SLOTS = ("PRIMARY", "ALTERNATE_1", "ALTERNATE_2")
_DOUBLE_THREE_MARKERS = ("双锯齿", "W终点", "X不可触及", "X端点")


def _periods(
    prepared: MarketDataBundle, market_asof: date, timeframe: str
) -> tuple[tuple[OutlookPeriod, ...], int, str]:
    horizon = 5 if timeframe == "1d" else 2
    unit = "SESSIONS" if timeframe == "1d" else "WEEKS"
    last_error: Exception | None = None
    for days in (70, 35, 20, 15, 10):
        try:
            calendar = load_calendar(
                prepared.instrument,
                market_asof,
                market_asof + timedelta(days=days),
            )
        except Exception as exc:  # calendar coverage can end before the requested window
            last_error = exc
            continue
        future = [
            session
            for session in calendar.sessions
            if session.date > market_asof and session.open_at >= prepared.as_of
        ]
        if timeframe == "1d":
            selected = future[:horizon]
            periods = tuple(
                OutlookPeriod(
                    start_at=session.open_at,
                    end_session=session.date,
                    end_at=session.close_at,
                    sessions=(session.date,),
                )
                for session in selected
            )
        else:
            current_week = market_asof.isocalendar()[:2]
            groups: list[tuple[tuple[int, int], list]] = []
            for session in calendar.sessions:
                if session.date <= market_asof:
                    continue
                week = session.date.isocalendar()[:2]
                if week == current_week:
                    continue
                if groups and groups[-1][0] == week:
                    groups[-1][1].append(session)
                else:
                    groups.append((week, [session]))
            groups = [
                (week, sessions)
                for week, sessions in groups
                if sessions[0].open_at >= prepared.as_of
            ]
            selected = groups[:horizon]
            periods = tuple(
                OutlookPeriod(
                    start_at=sessions[0].open_at,
                    end_session=sessions[-1].date,
                    end_at=sessions[-1].close_at,
                    sessions=tuple(session.date for session in sessions),
                )
                for _, sessions in selected
            )
        if len(periods) >= horizon:
            return periods, horizon, unit
        last_error = last_error or ValueError("insufficient future sessions")
    raise ValueError(f"CALENDAR_UNAVAILABLE: {last_error}")


def _fallback_role(condition: LevelCondition) -> str:
    if condition.kind == "INVALIDATION":
        return "INVALIDATION"
    if condition.kind == "STAGE_EXIT":
        return "STAGE_EXIT"
    return "CURRENT_EVIDENCE"


def _conditions(card: BriefCard, projection: Projection | None) -> tuple[OutlookCondition, ...]:
    if projection is not None:
        gated = projection.conditions
    else:
        gated = tuple(
            OutlookCondition(
                role=_fallback_role(condition),
                condition=condition,
                source_index=index,
            )
            for index, condition in enumerate(card.conditions)
        )
    if card.variant == "DOUBLE_THREE":
        gated = tuple(
            item
            for item in gated
            if not any(marker in item.condition.reason for marker in _DOUBLE_THREE_MARKERS)
        )
    return gated


def _target(
    card: BriefCard, projection: Projection | None, current: float
) -> tuple[object | None, str]:
    zones = projection.zones if projection is not None else card.target_zones
    if card.variant == "DOUBLE_THREE" or not zones:
        return None, "UNKNOWN"
    zone = zones[0]
    if zone.low > current:
        return zone, "UP"
    if zone.high < current:
        return zone, "DOWN"
    return None, "UNKNOWN"


def _event(event_id: str, available: bool, reason: str) -> OutlookEvent:
    return OutlookEvent(
        event_id=event_id,
        state="DEFINED" if available else "UNDEFINED",
        reason=None if available else reason,
    )


def _case(
    card: BriefCard,
    slot: str,
    projection: Projection | None,
    current: float,
    horizon_unit: str,
) -> OutlookCase:
    conditions = _conditions(card, projection)
    stop = any(item.role in {"INVALIDATION", "STAGE_EXIT"} for item in conditions)
    advance = any(item.role == "ADVANCE" for item in conditions)
    target, target_direction = _target(card, projection, current)
    current_direction = projection.current_leg_direction if projection else "UNKNOWN"
    advance_direction = projection.advance_direction if projection else "UNKNOWN"
    stop_hit = bool(projection is not None and projection.stop_hit)
    advance_pending = bool(projection is not None and projection.advance_pending)
    if target is None or stop_hit:
        target_mode = "NONE"
    elif advance_pending or (
        current_direction != "UNKNOWN"
        and target_direction != "UNKNOWN"
        and current_direction != target_direction
    ):
        target_mode = "AFTER_ADVANCE"
    else:
        target_mode = "ACTIVE"
    suffix = "5D" if horizon_unit == "SESSIONS" else "2W"
    advance_event = _event(
        f"ADVANCE_FIRST_{suffix}_V1",
        advance and stop and not stop_hit,
        "STOP_ALREADY_HIT" if stop_hit else "NO_ADVANCE_GATE" if not advance else "NO_STOP",
    )
    target_ready = (
        target is not None and stop and not stop_hit and (target_mode != "AFTER_ADVANCE" or advance)
    )
    target_reason = (
        "STOP_ALREADY_HIT"
        if stop_hit
        else "NO_VALID_TARGET"
        if target is None
        else "NO_STOP"
        if not stop
        else "NO_ADVANCE_GATE"
    )
    return OutlookCase(
        scenario_id=card.scenario_id,
        structure_key=card.structure_key,
        slot=slot,
        active_wave=projection.active_wave if projection else None,
        current_leg_direction=current_direction,
        advance_direction=advance_direction,
        conditions=conditions,
        target_mode=target_mode,
        target_direction=target_direction,
        target=target,
        events=(
            advance_event,
            _event(f"OUTLOOK_TARGET_FIRST_{suffix}_V1", target_ready, target_reason),
        ),
    )


def build_outlook(
    cards: tuple[BriefCard, ...],
    result: AnalysisResult,
    prepared: MarketDataBundle,
    projections: dict[str, Projection],
) -> BriefOutlook:
    """Freeze the next observation window and per-card conditions."""
    issued_at = result.as_of
    coordinate_id = result.price_coordinate_id
    try:
        periods, horizon, unit = _periods(prepared, result.market_asof, result.timeframe)
    except Exception as exc:  # calendar coverage is optional evidence, not a hard failure
        return BriefOutlook(
            issued_at=issued_at,
            price_coordinate_id=coordinate_id,
            horizon=5 if result.timeframe == "1d" else 2,
            horizon_unit="SESSIONS" if result.timeframe == "1d" else "WEEKS",
            periods=(),
            status="UNAVAILABLE",
            reason=f"CALENDAR_UNAVAILABLE: {exc}",
        )
    if len(periods) < horizon:
        return BriefOutlook(
            issued_at=issued_at,
            price_coordinate_id=coordinate_id,
            horizon=horizon,
            horizon_unit=unit,
            periods=periods,
            status="UNAVAILABLE",
            reason="INSUFFICIENT_CALENDAR: 没有足够的后续完整交易期",
        )
    if result.status not in {"OK", "UNCLEAR"}:
        return BriefOutlook(
            issued_at=issued_at,
            price_coordinate_id=coordinate_id,
            horizon=horizon,
            horizon_unit=unit,
            periods=periods,
            status="UNAVAILABLE",
            reason=f"RESULT_{result.status}: 当前分析状态不允许发行短期事件",
        )
    if not cards:
        return BriefOutlook(
            issued_at=issued_at,
            price_coordinate_id=coordinate_id,
            horizon=horizon,
            horizon_unit=unit,
            periods=periods,
            status="UNAVAILABLE",
            reason="OUTLOOK_UNAVAILABLE: 没有可发行卡片",
        )
    cases = tuple(
        _case(
            card,
            _SLOTS[index] if index < len(_SLOTS) else "ALTERNATE_2",
            projections.get(card.scenario_id),
            result.latest_price,
            unit,
        )
        for index, card in enumerate(cards[:3])
    )
    return BriefOutlook(
        issued_at=issued_at,
        price_coordinate_id=coordinate_id,
        horizon=horizon,
        horizon_unit=unit,
        periods=periods,
        status="AVAILABLE",
        cases=cases,
    )
