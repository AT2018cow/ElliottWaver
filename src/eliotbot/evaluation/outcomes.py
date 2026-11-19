"""Fixed-horizon outcomes on the exchange calendar and the issuance price basis."""

import math
from dataclasses import dataclass
from datetime import date, timedelta

from eliotbot.data.adjustment import adjust_to_basis
from eliotbot.data.resample import weekly_bars
from eliotbot.errors import DataError
from eliotbot.evaluation.events import condition_hit
from eliotbot.schemas.market import Bar, FactorPoint, MarketDataBundle
from eliotbot.schemas.wave import WaveScenario


@dataclass(frozen=True)
class Outcome:
    status: str
    session: date | None = None
    value: float | None = None


@dataclass(frozen=True)
class OutcomeWindow:
    dates: tuple[date, ...]
    bars: tuple[Bar | None, ...]
    observed_through: date
    closing_bars: tuple[Bar | None, ...] = ()

    def close_bar(self, horizon: int) -> Bar | None:
        if 0 < horizon <= len(self.dates):
            return (self.closing_bars or self.bars)[horizon - 1]
        return None

    def missing(self, day: date) -> Outcome:
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
        from eliotbot.data.calendar import load_calendar

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
            target = scenario.target_zones[0]
            hit = (
                (bar.high >= target.low and bar.low <= target.high)
                or (scenario.next_leg_bias == "UP" and bar.low > target.high)
                or (scenario.next_leg_bias == "DOWN" and bar.high < target.low)
            )
        if hit and invalidated:
            return Outcome("AMBIGUOUS_SAME_BAR", day)
        if hit:
            return Outcome("CONFIRMED_FIRST" if confirmation else "TARGET_FIRST", day)
        if invalidated:
            return Outcome("INVALIDATED_FIRST", day)
    if len(window.dates) < horizon:
        return Outcome("RIGHT_CENSORED")
    return Outcome("EXPIRED", window.dates[horizon - 1])
