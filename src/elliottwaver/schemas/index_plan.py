"""Frozen single-index guidance and append-only observation contracts."""

from datetime import date
from decimal import Decimal
from typing import Literal

from pydantic import AwareDatetime, Field, model_validator

from elliottwaver.schemas.market import Contract
from elliottwaver.schemas.outlook import OutlookPeriod
from elliottwaver.schemas.wave import LevelCondition, PriceZone

Horizon = Literal["SHORT", "SWING"]
PlanState = Literal[
    "WAITING",
    "NEAR",
    "TRIGGERED",
    "TARGET_REACHED",
    "INVALIDATED",
    "STAGE_EXIT",
    "EXPIRED",
    "CANCELLED",
    "AMBIGUOUS",
]


class IndexSizingInput(Contract):
    instrument: str = Field(min_length=1)
    as_of: AwareDatetime
    valid_until: AwareDatetime
    risk_budget: Decimal = Field(ge=0)
    risk_per_unit: Decimal = Field(gt=0)
    cost_per_unit: Decimal = Field(gt=0)
    cash: Decimal = Field(ge=0)
    liquidity_limit: int = Field(ge=0)
    lot_size: int = Field(gt=0)
    sellable_quantity: int = Field(ge=0)

    @model_validator(mode="after")
    def valid_window(self):
        if self.as_of > self.valid_until:
            raise ValueError("sizing snapshot expires before its timestamp")
        return self


class IndexSizing(Contract):
    inputs: IndexSizingInput
    buy_limit: int = Field(ge=0)
    sell_limit: int = Field(ge=0)


class TargetDistance(Contract):
    zone: PriceZone
    boundary: float
    points: float
    atr: float | None
    location: Literal["AHEAD", "IN_ZONE", "PASSED"]


class ConfirmationEvidence(Contract):
    pivot_id: str
    extreme_at: AwareDatetime
    extreme_price: float
    confirmed_at: AwareDatetime
    engine_confirmed_at: AwareDatetime
    confirmation_bar_id: str
    confirmation_price: float
    confirmation_atr: float | None
    extreme_to_confirmation_seconds: float = Field(ge=0)


class NextIndexPrice(Contract):
    bar_id: str
    available_at: AwareDatetime
    price: float
    confirmation_to_price_seconds: float = Field(ge=0)
    moved_points: float
    moved_atr: float | None
    remaining: TargetDistance


class DevelopingEvidence(Contract):
    pivot_id: str
    kind: Literal["HIGH", "LOW"]
    extreme_at: AwareDatetime
    extreme_price: float
    first_seen_at: AwareDatetime
    reference_price: float
    target: PriceZone | None = None
    early_eligible: bool = False
    comparable_entry_ceiling: float | None = None
    hard_boundary: LevelCondition | None = None

    @model_validator(mode="after")
    def require_comparable_early_inputs(self):
        if self.early_eligible and (
            self.target is None
            or self.hard_boundary is None
            or self.comparable_entry_ceiling is None
        ):
            raise ValueError("eligible early observation requires its frozen inputs")
        return self


class IntradayPlanPolicy(Contract):
    method: Literal["TOUCH", "CLOSE", "RETEST"]
    retest_tolerance_points: float = Field(ge=0)
    max_age_minutes: int = Field(ge=15)
    background_max_age_minutes: int = Field(ge=60)


class IndexPlan(Contract):
    plan_id: str
    revision: int = Field(ge=1)
    parent_plan_id: str | None = None
    symbol: str
    mode: Literal["LIVE", "FROZEN_DATA_REPLAY"]
    horizon: Horizon
    holding_sessions: int = Field(ge=1)
    structure_key: str
    scenario_id: str
    policy_hash: str
    engine_version: str
    analysis_config_hash: str
    decision_at: AwareDatetime
    issued_at: AwareDatetime
    decision_session: date
    source_snapshot_id: str
    input_snapshot_id: str
    signal_bar_id: str
    feed: str
    adjustment: str
    index_coordinate_id: str
    calendar_version: str
    periods: tuple[OutlookPeriod, ...] = Field(min_length=1)
    activation_sessions: int = Field(ge=1)
    activation_deadline: AwareDatetime
    trigger: LevelCondition
    invalidations: tuple[LevelCondition, ...] = Field(min_length=1)
    stage_exits: tuple[LevelCondition, ...] = ()
    targets: tuple[PriceZone, ...] = Field(min_length=1)
    reference_price: float = Field(gt=0)
    atr: float = Field(gt=0)
    atr_window: Literal[14] = 14
    max_chase_atr: float = Field(ge=0)
    near_atr: float = Field(ge=0)
    max_entry_index_price: float = Field(gt=0)
    confirmation: ConfirmationEvidence
    intraday_policy: IntradayPlanPolicy | None = Field(None, exclude_if=lambda value: value is None)

    @model_validator(mode="after")
    def verify_policy(self):
        if (
            len(self.periods) < self.activation_sessions + self.holding_sessions - 1
            or self.activation_deadline != self.periods[self.activation_sessions - 1].end_at
            or self.issued_at > self.periods[0].start_at
            or self.decision_at > self.issued_at
            or self.trigger.side != "CLOSE"
            or self.trigger.op not in {">", ">="}
            or not self.trigger.price <= self.max_entry_index_price < self.targets[0].low
            or any(
                condition.coordinate_id != self.index_coordinate_id
                for condition in (self.trigger, *self.invalidations, *self.stage_exits)
            )
        ):
            raise ValueError("invalid frozen single-index policy")
        if any(
            len(period.sessions) != 1
            or period.sessions[0] != period.end_session
            or period.end_session <= self.decision_session
            or period.start_at < self.issued_at
            for period in self.periods
        ) or any(
            a.end_at >= b.start_at for a, b in zip(self.periods, self.periods[1:], strict=False)
        ):
            raise ValueError("index plan requires ordered full trading sessions")
        return self


class PlanObservation(Contract):
    state: PlanState = "WAITING"
    through_session: date | None = None
    through_observed_at: AwareDatetime | None = Field(None, exclude_if=lambda value: value is None)
    through_bar_id: str | None = Field(None, exclude_if=lambda value: value is None)
    quality: Literal["OK", "DATA_PENDING", "DATA_REVISED", "NOT_COMPARABLE"] = "OK"
    reason: str = "WAIT_FOR_CLOSE"
    activation_session: date | None = None
    activation_at: AwareDatetime | None = None
    activation_bar_id: str | None = None
    activation_price: float | None = None
    exit_session: date | None = None
    due_session: date | None = None
    due_at: AwareDatetime | None = None
    activation_target_distance: TargetDistance | None = None
    next_index_price: NextIndexPrice | None = None


class PlanTransition(Contract):
    transition_id: str
    plan_id: str
    revision: int
    horizon: Horizon
    from_state: PlanState | None
    to_state: PlanState
    reason: str
    session: date
    effective_at: AwareDatetime
    observable_at: AwareDatetime
    source_bar_id: str | None
    coordinate_id: str


class IndexTrack(Contract):
    horizon: Horizon
    holding_sessions: int
    plan: IndexPlan | None = None
    observation: PlanObservation = Field(default_factory=PlanObservation)
    trigger_distance_points: float | None = None
    trigger_distance_atr: float | None = None
    current_targets: tuple[TargetDistance, ...] = ()
    candidate_wait_reason: str | None = None
    pending_structure_key: str | None = None
    pending_since: AwareDatetime | None = None
    transitions: tuple[PlanTransition, ...] = ()


class IndexGuidance(Contract):
    version: Literal["1.0"] = "1.0"
    policy_hash: str
    direction: Literal["UP", "DOWN", "UNKNOWN"]
    structure_key: str | None = None
    last_price: float
    last_price_age_seconds: float = Field(ge=0)
    target_distances: tuple[TargetDistance, ...] = ()
    developing: DevelopingEvidence | None = None
    confirmation: ConfirmationEvidence | None = None
    comparison: str | None = None
    sizing: IndexSizing | None = None
    tool_status: Literal["NOT_CONFIGURED", "SIZING_READY", "SIZING_INPUT_OUTSIDE_WINDOW"] = (
        "NOT_CONFIGURED"
    )
    tracks: tuple[IndexTrack, IndexTrack]
