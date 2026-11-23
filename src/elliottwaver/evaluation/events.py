"""Fixed-horizon outcome labels, kept separate from analysis features."""

from dataclasses import dataclass
from datetime import date
from typing import Literal

from elliottwaver.schemas.market import Bar, MarketDataBundle
from elliottwaver.schemas.wave import LevelCondition, WaveScenario
from elliottwaver.timeframes import Timeframe


def condition_hit(condition: LevelCondition, bar: Bar) -> bool:
    value = getattr(bar, condition.side.lower())
    boundary = condition.price
    return {
        "<": value < boundary,
        "<=": value <= boundary,
        ">": value > boundary,
        ">=": value >= boundary,
    }[condition.op]


@dataclass(frozen=True)
class EventOutcome:
    status: str
    session: date | None = None


@dataclass(frozen=True)
class EventSpec:
    event_id: str
    timeframe: Timeframe
    horizon: int
    unit: Literal["SESSIONS", "WEEKS", "BARS"]
    scope: Literal["SYMBOL", "CARD"]
    evaluator: Literal["close_band", "advance_first", "target_first"]
    success_label: str
    training_label: str


EVENT_SPECS: dict[str, EventSpec] = {
    "CLOSE_BAND_5D_V1": EventSpec(
        "CLOSE_BAND_5D_V1", "1d", 5, "SESSIONS", "SYMBOL", "close_band", "UP", "UP"
    ),
    "CLOSE_BAND_2W_V1": EventSpec(
        "CLOSE_BAND_2W_V1", "1w", 2, "WEEKS", "SYMBOL", "close_band", "UP", "UP"
    ),
    "CONFIRM_FIRST_20D_V1": EventSpec(
        "CONFIRM_FIRST_20D_V1",
        "1d",
        20,
        "SESSIONS",
        "CARD",
        "advance_first",
        "CONFIRMED_FIRST",
        "YES",
    ),
    "TARGET_FIRST_20D_V1": EventSpec(
        "TARGET_FIRST_20D_V1", "1d", 20, "SESSIONS", "CARD", "target_first", "TARGET_FIRST", "YES"
    ),
    "CONFIRM_FIRST_8W_V1": EventSpec(
        "CONFIRM_FIRST_8W_V1", "1w", 8, "WEEKS", "CARD", "advance_first", "CONFIRMED_FIRST", "YES"
    ),
    "TARGET_FIRST_8W_V1": EventSpec(
        "TARGET_FIRST_8W_V1", "1w", 8, "WEEKS", "CARD", "target_first", "TARGET_FIRST", "YES"
    ),
    "ADVANCE_FIRST_5D_V1": EventSpec(
        "ADVANCE_FIRST_5D_V1",
        "1d",
        5,
        "SESSIONS",
        "CARD",
        "advance_first",
        "CONFIRMED_FIRST",
        "YES",
    ),
    "ADVANCE_FIRST_2W_V1": EventSpec(
        "ADVANCE_FIRST_2W_V1", "1w", 2, "WEEKS", "CARD", "advance_first", "CONFIRMED_FIRST", "YES"
    ),
    "OUTLOOK_TARGET_FIRST_5D_V1": EventSpec(
        "OUTLOOK_TARGET_FIRST_5D_V1",
        "1d",
        5,
        "SESSIONS",
        "CARD",
        "target_first",
        "TARGET_FIRST",
        "YES",
    ),
    "OUTLOOK_TARGET_FIRST_2W_V1": EventSpec(
        "OUTLOOK_TARGET_FIRST_2W_V1",
        "1w",
        2,
        "WEEKS",
        "CARD",
        "target_first",
        "TARGET_FIRST",
        "YES",
    ),
}

INTRADAY_SUFFIX = {"1m": "1M", "5m": "5M", "15m": "15M", "30m": "30M", "60m": "60M"}
INTRADAY_EVENTS: dict[str, tuple[str, str]] = {}
for _timeframe, _suffix in INTRADAY_SUFFIX.items():
    _advance = f"ADVANCE_FIRST_{_suffix}_5B_V1"
    _target = f"OUTLOOK_TARGET_FIRST_{_suffix}_5B_V1"
    EVENT_SPECS[_advance] = EventSpec(
        _advance, _timeframe, 5, "BARS", "CARD", "advance_first", "CONFIRMED_FIRST", "YES"
    )
    EVENT_SPECS[_target] = EventSpec(
        _target, _timeframe, 5, "BARS", "CARD", "target_first", "TARGET_FIRST", "YES"
    )
    INTRADAY_EVENTS[_timeframe] = (_advance, _target)


def evaluate_event(
    scenario: WaveScenario, bundle: MarketDataBundle, decision_date: date, horizon=20
) -> EventOutcome:
    if horizon < 1:
        raise ValueError("horizon must be positive")
    invalidations = [c for c in scenario.conditions if c.kind == "INVALIDATION"]
    if not invalidations or not scenario.target_zones:
        return EventOutcome("UNDEFINED_EVENT")
    future = [day for day in bundle.sessions if day > decision_date][:horizon]
    bars = {b.session_date: b for b in bundle.bars}
    target = scenario.target_zones[0]
    for day in future:
        if day not in bars:
            return EventOutcome(
                "INCOMPLETE_DATA" if day <= bundle.bars[-1].session_date else "RIGHT_CENSORED", day
            )
        bar = bars[day]
        hit_target = bar.high >= target.low and bar.low <= target.high
        # A gap beyond the target also crosses its boundary at the first executable observation.
        hit_target = (
            hit_target
            or (scenario.next_leg_bias == "UP" and bar.low > target.high)
            or (scenario.next_leg_bias == "DOWN" and bar.high < target.low)
        )
        hit_invalid = any(condition_hit(c, bar) for c in invalidations)
        if hit_target and hit_invalid:
            return EventOutcome("AMBIGUOUS_SAME_BAR", day)
        if hit_invalid:
            return EventOutcome("INVALIDATED_FIRST", day)
        if hit_target:
            return EventOutcome("TARGET_FIRST", day)
    if len(future) < horizon or not future or future[-1] > bundle.bars[-1].session_date:
        return EventOutcome("RIGHT_CENSORED")
    return EventOutcome("EXPIRED", future[-1])
