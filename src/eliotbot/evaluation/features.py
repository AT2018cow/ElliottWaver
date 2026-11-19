"""Feature extraction shared by evaluation and forecast inference."""

from __future__ import annotations

import math

from eliotbot.schemas.wave import WaveScenario

FEATURE_VERSION = "2"

DECISION_FEATURES = [
    "log_return_20",
    "missing_return",
    "atr_ratio",
    "candidate_count",
    "up_weight",
    "down_weight",
    "unknown_weight",
    "top_quality",
    "top_coverage",
    "top2_quality_gap",
    "top_bias",
    "weekly_bias",
    "missing_weekly",
    "missing_second",
]
CANDIDATE_FEATURES = [
    "quality",
    "coverage",
    "bias",
    "confirm_distance",
    "invalidation_distance",
    "missing_confirm",
    "missing_invalidation",
    "candidate_rank",
]
PATTERNS = ["IMPULSE", "ZIGZAG_ABC", "TRIANGLE", "FLAT", "DIAGONAL", "WXY"]
STAGES = [
    "COMPLETE",
    "W2_DEVELOPING",
    "W3_DEVELOPING",
    "W4_DEVELOPING",
    "W5_DEVELOPING",
    "B_DEVELOPING",
    "C_DEVELOPING",
    "D_DEVELOPING",
    "E_DEVELOPING",
    "X_DEVELOPING",
    "Y_DEVELOPING",
]


def decision_features(
    result, prepared, indicators, *, qualified=None, weekly=None
) -> dict[str, float]:
    bars = prepared.bars
    close = bars[-1].close
    atr = float(indicators.atr[-1])
    if len(bars) >= 21 and bars[-21].close > 0:
        log_return = math.log(close / bars[-21].close)
        missing_return = False
    else:
        log_return = 0.0
        missing_return = True
    scenarios = result.scenarios if qualified is None else qualified
    weekly_bias = (
        {"UP": 1.0, "DOWN": -1.0}.get(weekly.scenarios[0].next_leg_bias)
        if weekly is not None and weekly.status == "OK" and weekly.scenarios
        else None
    )
    up = sum(s.relative_weight for s in scenarios if s.next_leg_bias == "UP")
    down = sum(s.relative_weight for s in scenarios if s.next_leg_bias == "DOWN")
    unknown = sum(s.relative_weight for s in scenarios if s.next_leg_bias == "UNKNOWN")
    top = scenarios[0] if scenarios else None
    second = scenarios[1] if len(scenarios) > 1 else None
    bias = {"UP": 1.0, "DOWN": -1.0}.get(top.next_leg_bias, 0.0) if top else 0.0
    return {
        "log_return_20": log_return,
        "missing_return": float(missing_return),
        "atr_ratio": atr / close if close > 0 else 0.0,
        "candidate_count": len(scenarios),
        "up_weight": up,
        "down_weight": down,
        "unknown_weight": unknown,
        "top_quality": top.score.quality_score / 100 if top else 0.0,
        "top_coverage": top.score.coverage if top else 0.0,
        "top2_quality_gap": (
            (top.score.quality_score - second.score.quality_score) / 100 if top and second else 0.0
        ),
        "top_bias": bias,
        "weekly_bias": weekly_bias or 0.0,
        "missing_weekly": float(weekly_bias is None),
        "missing_second": float(second is None),
    }


def candidate_features(
    scenario: WaveScenario, current: float, atr: float, *, rank: int = 1
) -> dict:
    confirmations = [c.price for c in scenario.conditions if c.kind == "CONFIRMATION"]
    invalidations = [c.price for c in scenario.conditions if c.kind == "INVALIDATION"]
    return {
        "quality": scenario.score.quality_score / 100,
        "coverage": scenario.score.coverage,
        "bias": {"UP": 1.0, "DOWN": -1.0}.get(scenario.next_leg_bias, 0.0),
        "pattern": scenario.pattern_type,
        "stage": scenario.stage,
        "candidate_rank": rank,
        "missing_confirm": float(not confirmations),
        "missing_invalidation": float(not invalidations),
        "confirm_distance": (
            min(abs(price - current) for price in confirmations) / atr
            if confirmations and atr > 0
            else 0.0
        ),
        "invalidation_distance": (
            min(abs(price - current) for price in invalidations) / atr
            if invalidations and atr > 0
            else 0.0
        ),
    }


def feature_vector(
    feature_order: list[str],
    decision: dict[str, float],
    candidate: dict | None,
) -> list[float]:
    values = []
    for name in feature_order:
        if name in decision:
            values.append(float(decision[name]))
        elif candidate is not None and name in CANDIDATE_FEATURES:
            values.append(float(candidate.get(name, 0.0)))
        elif candidate is not None and name in PATTERNS:
            values.append(float(candidate.get("pattern") == name))
        elif candidate is not None and name in STAGES:
            values.append(float(candidate.get("stage") == name))
        else:
            values.append(0.0)
    return values


def event_feature_order(event_id: str) -> list[str]:
    order = list(DECISION_FEATURES)
    if event_id.startswith(("CONFIRM_FIRST_", "TARGET_FIRST_")):
        order += CANDIDATE_FEATURES + PATTERNS + STAGES
    return order
