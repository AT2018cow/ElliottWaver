"""Feature extraction shared by evaluation and forecast inference."""

from __future__ import annotations

import math

from eliotbot.schemas.outlook import OutlookCase
from eliotbot.schemas.wave import WaveScenario

FEATURE_VERSION = "2"
SUPPORTED_FEATURE_VERSIONS = {"2", "3", "4"}

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
TARGET_FEATURES = [
    "target_distance_atr",
    "inside_target_zone",
    "missing_target_geometry",
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
OUTLOOK_PRICE_FEATURES = [
    "outlook_advance_distance",
    "outlook_stop_distance",
    "outlook_stage_exit_distance",
    "outlook_target_low_distance",
    "outlook_target_high_distance",
    "outlook_target_mode_active",
    "outlook_target_mode_after_advance",
]
OUTLOOK_WAVE_FEATURES = [
    "outlook_current_leg_up",
    "outlook_current_leg_down",
    "outlook_advance_up",
    "outlook_advance_down",
    "outlook_double_three",
    "outlook_subwave_verified",
    "outlook_subwave_depth",
    "outlook_relation_verified",
    "outlook_window_same_count",
]
OUTLOOK_FEATURES = OUTLOOK_PRICE_FEATURES + OUTLOOK_WAVE_FEATURES
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


def target_geometry(
    scenario: WaveScenario, current: float, atr: float
) -> tuple[float, float, float]:
    """Decision-time distance to the first target zone in ATR units."""
    if not scenario.target_zones or not math.isfinite(atr) or atr <= 0 or current <= 0:
        return 0.0, 0.0, 1.0
    target = scenario.target_zones[0]
    if target.low <= 0 or target.high <= 0 or target.low > target.high:
        return 0.0, 0.0, 1.0
    distance = max(target.low - current, current - target.high, 0.0) / atr
    inside = 1.0 if target.low <= current <= target.high else 0.0
    return distance, inside, 0.0


def candidate_features(
    scenario: WaveScenario, current: float, atr: float, *, rank: int = 1
) -> dict:
    confirmations = [c.price for c in scenario.conditions if c.kind == "CONFIRMATION"]
    invalidations = [c.price for c in scenario.conditions if c.kind == "INVALIDATION"]
    target = target_geometry(scenario, current, atr)
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
        "target_distance_atr": target[0],
        "inside_target_zone": target[1],
        "missing_target_geometry": target[2],
    }


def outlook_features(
    case: OutlookCase,
    scenario: WaveScenario,
    current: float,
    atr: float,
    *,
    wave_relations=(),
    window_state: str | None = None,
) -> dict[str, float]:
    """Features that describe the frozen outlook geometry and wave evidence."""

    def distance(prices: list[float]) -> float:
        if not prices or not math.isfinite(atr) or atr <= 0 or current <= 0:
            return 0.0
        return min(abs(price - current) for price in prices) / atr

    def role(role_name: str) -> list[float]:
        return [item.condition.price for item in case.conditions if item.role == role_name]

    target_low = case.target.low if case.target is not None else 0.0
    target_high = case.target.high if case.target is not None else 0.0
    relation_verified = any(
        (item.get("daily_scenario_id") if isinstance(item, dict) else item.daily_scenario_id)
        == scenario.scenario_id
        and (item.get("status") if isinstance(item, dict) else item.status) == "VERIFIED"
        for item in wave_relations
    )
    return {
        "outlook_advance_distance": distance(role("ADVANCE")),
        "outlook_stop_distance": distance(role("INVALIDATION") + role("STAGE_EXIT")),
        "outlook_stage_exit_distance": distance(role("STAGE_EXIT")),
        "outlook_target_low_distance": distance([target_low]) if target_low else 0.0,
        "outlook_target_high_distance": distance([target_high]) if target_high else 0.0,
        "outlook_target_mode_active": float(case.target_mode == "ACTIVE"),
        "outlook_target_mode_after_advance": float(case.target_mode == "AFTER_ADVANCE"),
        "outlook_current_leg_up": float(case.current_leg_direction == "UP"),
        "outlook_current_leg_down": float(case.current_leg_direction == "DOWN"),
        "outlook_advance_up": float(case.advance_direction == "UP"),
        "outlook_advance_down": float(case.advance_direction == "DOWN"),
        "outlook_double_three": float(scenario.variant == "DOUBLE_THREE"),
        "outlook_subwave_verified": float(scenario.subwave_status == "VERIFIED"),
        "outlook_subwave_depth": (
            scenario.structure.verified_depth / 2 if scenario.structure is not None else 0.0
        ),
        "outlook_relation_verified": float(relation_verified),
        "outlook_window_same_count": float(window_state == "SAME_COUNT"),
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
        elif name in OUTLOOK_FEATURES and candidate is not None:
            value = candidate.get(name, 0.0)
            values.append(float(value) if value is not None else 0.0)
        elif name in TARGET_FEATURES:
            if candidate is None or name not in candidate:
                raise ValueError(f"Missing target geometry feature: {name}")
            values.append(float(candidate[name]))
        elif candidate is not None and name in CANDIDATE_FEATURES:
            values.append(float(candidate.get(name, 0.0)))
        elif candidate is not None and name in PATTERNS:
            values.append(float(candidate.get("pattern") == name))
        elif candidate is not None and name in STAGES:
            values.append(float(candidate.get("stage") == name))
        else:
            values.append(0.0)
    return values


def event_feature_order(event_id: str, feature_version: str = FEATURE_VERSION) -> list[str]:
    if feature_version not in SUPPORTED_FEATURE_VERSIONS:
        raise ValueError(f"Unsupported feature_version: {feature_version}")
    order = list(DECISION_FEATURES)
    if event_id.startswith(
        ("CONFIRM_FIRST_", "TARGET_FIRST_", "ADVANCE_FIRST_", "OUTLOOK_TARGET_FIRST_")
    ):
        order += CANDIDATE_FEATURES + PATTERNS + STAGES
        if feature_version in {"3", "4"} and event_id.startswith(
            ("TARGET_FIRST_", "OUTLOOK_TARGET_FIRST_")
        ):
            order += TARGET_FEATURES
        if feature_version == "4":
            order += OUTLOOK_FEATURES
    return order
