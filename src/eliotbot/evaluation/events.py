"""Fixed-horizon outcome labels, kept separate from analysis features."""

from dataclasses import dataclass
from datetime import date

from eliotbot.schemas.market import Bar, MarketDataBundle
from eliotbot.schemas.wave import LevelCondition, WaveScenario


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
