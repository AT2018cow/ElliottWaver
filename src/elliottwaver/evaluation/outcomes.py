"""Fixed-horizon outcomes on the exchange calendar and the issuance price basis."""

import math
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from elliottwaver.data.adjustment import adjust_to_basis
from elliottwaver.data.resample import weekly_bars
from elliottwaver.errors import DataError
from elliottwaver.evaluation.events import EventSpec, condition_hit
from elliottwaver.schemas.market import Bar, FactorPoint, MarketDataBundle
from elliottwaver.schemas.outlook import OutlookCase, OutlookPeriod
from elliottwaver.schemas.wave import PriceZone, WaveScenario


@dataclass(frozen=True)
class Outcome:
    status: str
    session: date | None = None
    value: float | None = None
    evidence: tuple[str, ...] = ()


@dataclass(frozen=True)
class OutcomeWindow:
    dates: tuple[date, ...]
    bars: tuple[Bar | None, ...]
    observed_through: date
    closing_bars: tuple[Bar | None, ...] = ()
    period_sessions: tuple[tuple[date, ...], ...] = ()
    calendar_mismatches: frozenset[date] = frozenset()
    price_basis_unverified: bool = False

    def close_bar(self, horizon: int) -> Bar | None:
        if 0 < horizon <= len(self.dates):
            return (self.closing_bars or self.bars)[horizon - 1]
        return None

    def missing(self, day: date) -> Outcome:
        if self.price_basis_unverified:
            return Outcome("PRICE_BASIS_UNVERIFIED", day)
        if day in self.calendar_mismatches:
            return Outcome("SETTLEMENT_CALENDAR_MISMATCH", day)
        return Outcome("INCOMPLETE_DATA" if day <= self.observed_through else "RIGHT_CENSORED", day)


def period_dates(sessions: tuple[date, ...], *, weekly: bool) -> tuple[date, ...]:
    if not weekly:
        return sessions
    return tuple({day.isocalendar()[:2]: day for day in sessions}.values())


def scheduled_dates(bundle: MarketDataBundle, decision: date, horizon: int) -> tuple[date, ...]:
    weekly = bundle.timeframe == "1w"
    sessions = bundle.sessions
    dates = tuple(day for day in period_dates(sessions, weekly=weekly) if day > decision)
    if len(dates) < horizon and bundle.provenance.provider != "synthetic_demo":
        from elliottwaver.data.calendar import load_calendar

        try:
            calendar = load_calendar(
                bundle.instrument,
                decision,
                decision + timedelta(days=horizon * (9 if weekly else 3)),
            )
            sessions = tuple(sorted(set(sessions) | set(calendar.dates)))
            dates = tuple(day for day in period_dates(sessions, weekly=weekly) if day > decision)
        except DataError:
            pass
    return dates[:horizon]


def outcome_window(
    bundle: MarketDataBundle,
    decision: date,
    horizon: int,
    *,
    issuance_factors: tuple[FactorPoint, ...] | None = None,
    outcome_as_of: date | None = None,
    issuance_dates: tuple[date, ...] = (),
    price_scale: float = 1.0,
) -> OutcomeWindow:
    weekly = bundle.timeframe == "1w"
    proposed = scheduled_dates(bundle, decision, horizon) if len(issuance_dates) < horizon else ()
    dates = (issuance_dates or proposed)[:horizon]
    if issuance_dates and len(issuance_dates) < horizon:
        if proposed[: len(issuance_dates)] != issuance_dates:
            raise DataError("SETTLEMENT_CALENDAR_MISMATCH")
        dates = proposed
    observed = (
        bundle.raw_daily_context[-1].session_date
        if weekly and bundle.raw_daily_context
        else bundle.bars[-1].session_date
    )
    if outcome_as_of is not None:
        observed = min(observed, outcome_as_of)
    if not dates:
        return OutcomeWindow((), (), observed)

    daily = ()
    if bundle.provenance.adjustment == "basis_date_qfq":
        raw = bundle.raw_daily_context if weekly else bundle.raw_bars
        future = tuple(
            bar for bar in raw if decision < bar.session_date <= min(dates[-1], observed)
        )
        factors = bundle.factors
        if issuance_factors is not None:
            factors = (*issuance_factors, *(p for p in factors if p.effective_date > decision))
        try:
            adjusted = adjust_to_basis(future, factors, basis_date=decision) if future else ()
        except DataError:
            adjusted = ()
        bars = weekly_bars(adjusted, bundle.sessions) if weekly else adjusted
        daily = adjusted if weekly else ()
    else:
        bars = bundle.bars
        if weekly:
            daily = bundle.daily_context

    def relevant(series):
        by_date = {}
        wanted = set(dates)
        for bar in series:
            if bar.session_date not in wanted or bar.session_date > observed:
                continue
            if price_scale != 1.0:
                bar = Bar.model_validate(
                    {
                        **bar.model_dump(),
                        **{
                            field: getattr(bar, field) * price_scale
                            for field in ("open", "high", "low", "close")
                        },
                    }
                )
            by_date[bar.session_date] = bar
        return by_date

    by_date = relevant(bars)
    closing = relevant(daily)
    return OutcomeWindow(
        dates,
        tuple(by_date.get(day) for day in dates),
        observed,
        tuple(closing.get(day) or by_date.get(day) for day in dates) if weekly else (),
    )


def outlook_window(
    bundle: MarketDataBundle,
    decision: date,
    periods: tuple[OutlookPeriod, ...],
    horizon: int,
    *,
    issuance_factors: tuple[FactorPoint, ...] | None = None,
    outcome_as_of: date | datetime | None = None,
    price_scale: float = 1.0,
) -> OutcomeWindow:
    """Observe only available bars in the sessions frozen by the issuing brief."""
    selected = tuple(periods[:horizon])
    dates = tuple(period.end_session for period in selected)
    period_sessions = tuple(period.sessions for period in selected)
    cutoff = bundle.as_of
    if isinstance(outcome_as_of, datetime):
        cutoff = min(cutoff, outcome_as_of)
    elif outcome_as_of is not None:
        next_day = datetime.combine(
            outcome_as_of + timedelta(days=1), time.min, ZoneInfo(bundle.instrument.timezone)
        )
        cutoff = min(cutoff, next_day - timedelta(microseconds=1))
    weekly = bundle.timeframe == "1w"
    qfq = bundle.provenance.adjustment == "basis_date_qfq"
    if weekly:
        daily = bundle.raw_daily_context if qfq else bundle.daily_context
    else:
        daily = bundle.raw_bars if qfq else bundle.bars
    observed = max(
        (
            bar.session_date
            for bar in daily or bundle.bars
            if bar.is_final and bar.usable_at <= cutoff
        ),
        default=decision,
    )
    expected = {day for period in selected for day in period.sessions}
    eligible = tuple(
        bar
        for bar in daily
        if bar.session_date in expected and bar.is_final and bar.usable_at <= cutoff
    )
    if qfq and eligible:
        factors = bundle.factors
        if issuance_factors is not None:
            factors = (
                *issuance_factors,
                *(point for point in factors if point.effective_date > decision),
            )
        try:
            eligible = adjust_to_basis(eligible, factors, basis_date=decision)
        except DataError:
            missing_bars = (None,) * len(dates)
            return OutcomeWindow(
                dates,
                missing_bars,
                observed,
                missing_bars,
                period_sessions,
                price_basis_unverified=True,
            )
    if price_scale != 1.0:
        eligible = tuple(
            bar.model_copy(
                update={
                    field: getattr(bar, field) * price_scale
                    for field in ("open", "high", "low", "close")
                }
            )
            for bar in eligible
        )
    by_date = {bar.session_date: bar for bar in eligible}
    declared_weeks: dict[tuple[int, int], list[date]] = {}
    for day in bundle.sessions:
        declared_weeks.setdefault(day.isocalendar()[:2], []).append(day)
    bars: list[Bar | None] = []
    closing: list[Bar | None] = []
    mismatches = set()
    for period in selected:
        day = period.end_session
        declared = tuple(declared_weeks.get(day.isocalendar()[:2], ()))
        if not weekly:
            declared = (day,) if day in declared else ()
        if day <= observed and declared != period.sessions:
            mismatches.add(day)
            bars.append(None)
            closing.append(None)
            continue
        close_bar = by_date.get(day)
        closing.append(close_bar)
        if weekly:
            members = tuple(by_date[day] for day in period.sessions if day in by_date)
            aggregates = weekly_bars(members, period.sessions) if members else ()
            bars.append(aggregates[0] if aggregates else None)
        else:
            bars.append(close_bar)
    return OutcomeWindow(
        dates,
        tuple(bars),
        observed,
        tuple(closing),
        period_sessions,
        frozenset(mismatches),
    )


def close_band(window: OutcomeWindow, decision_close: float, atr: float, horizon: int) -> Outcome:
    if horizon < 1 or not math.isfinite(atr) or atr <= 0:
        return Outcome("UNDEFINED_EVENT")
    if len(window.dates) < horizon:
        return Outcome("RIGHT_CENSORED")
    bar = window.close_bar(horizon)
    if bar is None:
        return window.missing(window.dates[horizon - 1])
    upper = decision_close + atr
    lower = decision_close - atr
    status = "UP" if bar.close > upper else "DOWN" if bar.close < lower else "RANGE"
    return Outcome(status, bar.session_date, bar.close)


def target_hit(target: PriceZone, bias: str, bar: Bar) -> bool:
    return (
        (bar.high >= target.low and bar.low <= target.high)
        or (bias == "UP" and bar.low > target.high)
        or (bias == "DOWN" and bar.high < target.low)
    )


def first_touch(
    scenario: WaveScenario, window: OutcomeWindow, horizon: int, *, confirmation: bool
) -> Outcome:
    invalidations = [c for c in scenario.conditions if c.kind == "INVALIDATION"]
    confirmations = [c for c in scenario.conditions if c.kind == "CONFIRMATION"]
    if not invalidations or not (confirmations if confirmation else scenario.target_zones):
        return Outcome("UNDEFINED_EVENT")
    for day, bar in zip(window.dates[:horizon], window.bars[:horizon], strict=True):
        if bar is None:
            return window.missing(day)
        invalidated = any(condition_hit(condition, bar) for condition in invalidations)
        if confirmation:
            hit = any(condition_hit(condition, bar) for condition in confirmations)
        else:
            hit = target_hit(scenario.target_zones[0], scenario.next_leg_bias, bar)
        if hit and invalidated:
            return Outcome("AMBIGUOUS_SAME_BAR", day)
        if hit:
            return Outcome("CONFIRMED_FIRST" if confirmation else "TARGET_FIRST", day)
        if invalidated:
            return Outcome("INVALIDATED_FIRST", day)
    if len(window.dates) < horizon:
        return Outcome("RIGHT_CENSORED")
    return Outcome("EXPIRED", window.dates[horizon - 1])


def _outlook_stop(case: OutlookCase, bar: Bar) -> tuple[str | None, tuple[str, ...]]:
    hits = [
        item
        for item in case.conditions
        if item.role in {"INVALIDATION", "STAGE_EXIT"} and condition_hit(item.condition, bar)
    ]
    if not hits:
        return None, ()
    # A hard invalidation dominates a stage exit when both trigger on the same bar.
    status = (
        "INVALIDATED_FIRST"
        if any(item.role == "INVALIDATION" for item in hits)
        else "STAGE_EXIT_FIRST"
    )
    return status, tuple(item.condition.reason for item in hits)


def _outlook_advance(case: OutlookCase, bar: Bar) -> tuple[bool, tuple[str, ...]]:
    hits = [
        item
        for item in case.conditions
        if item.role == "ADVANCE" and condition_hit(item.condition, bar)
    ]
    return bool(hits), tuple(item.condition.reason for item in hits)


def evaluate_outlook_event(
    spec: EventSpec,
    case: OutlookCase,
    window: OutcomeWindow,
) -> Outcome:
    """Evaluate the new card-level advance and target events."""
    issued = next((event for event in case.events if event.event_id == spec.event_id), None)
    if issued is not None and issued.state == "UNDEFINED":
        return Outcome("UNDEFINED_EVENT", evidence=(issued.reason,) if issued.reason else ())
    if spec.evaluator == "advance_first":
        if not any(item.role == "ADVANCE" for item in case.conditions):
            return Outcome("UNDEFINED_EVENT")
        if not any(item.role in {"INVALIDATION", "STAGE_EXIT"} for item in case.conditions):
            return Outcome("UNDEFINED_EVENT")
        for day, bar in zip(window.dates[: spec.horizon], window.bars[: spec.horizon], strict=True):
            if bar is None:
                return window.missing(day)
            stop, stop_evidence = _outlook_stop(case, bar)
            advance, advance_evidence = _outlook_advance(case, bar)
            if stop and advance:
                return Outcome("AMBIGUOUS_SAME_BAR", day, evidence=stop_evidence + advance_evidence)
            if stop:
                return Outcome(stop, day, evidence=stop_evidence)
            if advance:
                return Outcome("CONFIRMED_FIRST", day, evidence=advance_evidence)
        return _outlook_expiry(window, spec)
    if spec.evaluator == "target_first":
        if case.target is None or not any(
            item.role in {"INVALIDATION", "STAGE_EXIT"} for item in case.conditions
        ):
            return Outcome("UNDEFINED_EVENT")
        if case.target_mode == "AFTER_ADVANCE" and not any(
            item.role == "ADVANCE" for item in case.conditions
        ):
            return Outcome("UNDEFINED_EVENT")
        waiting = case.target_mode == "AFTER_ADVANCE"
        target_evidence = (case.target.label,)
        for day, bar in zip(window.dates[: spec.horizon], window.bars[: spec.horizon], strict=True):
            if bar is None:
                return window.missing(day)
            stop, stop_evidence = _outlook_stop(case, bar)
            advance, advance_evidence = _outlook_advance(case, bar)
            hit = target_hit(
                case.target,
                case.target_direction if case.target_direction != "UNKNOWN" else "UP",
                bar,
            )
            if waiting:
                if stop and advance:
                    return Outcome(
                        "AMBIGUOUS_SAME_BAR", day, evidence=stop_evidence + advance_evidence
                    )
                if stop:
                    return Outcome(stop, day, evidence=stop_evidence)
                if advance and hit:
                    return Outcome(
                        "AMBIGUOUS_SAME_BAR", day, evidence=stop_evidence + target_evidence
                    )
                if advance:
                    waiting = False
                continue
            if stop and hit:
                return Outcome("AMBIGUOUS_SAME_BAR", day, evidence=stop_evidence + target_evidence)
            if stop:
                return Outcome(stop, day, evidence=stop_evidence)
            if hit:
                return Outcome("TARGET_FIRST", day, evidence=target_evidence)
        return _outlook_expiry(window, spec)
    return Outcome("UNDEFINED_EVENT")


def _outlook_expiry(window: OutcomeWindow, spec: EventSpec) -> Outcome:
    if len(window.dates) < spec.horizon or not window.dates:
        return Outcome("RIGHT_CENSORED")
    if any(bar is None for bar in window.bars[: spec.horizon]):
        return window.missing(window.dates[min(len(window.dates), spec.horizon) - 1])
    return Outcome("EXPIRED", window.dates[spec.horizon - 1])
