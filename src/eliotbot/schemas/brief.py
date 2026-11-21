"""Strict contract for the one-page close brief."""

from datetime import date
from typing import Literal

from pydantic import AwareDatetime, Field

from eliotbot.schemas.market import Contract
from eliotbot.schemas.wave import (
    Degree,
    LevelCondition,
    ParentContext,
    Pattern,
    PriceZone,
    SubwaveStatus,
    WaveRelation,
    WaveStructure,
)
from eliotbot.timeframes import Timeframe


class BriefAnchor(Contract):
    date: date
    origin: Literal["EXPLICIT", "MACRO_HIGH", "MACRO_LOW", "FALLBACK"]
    background_timeframe: Literal["1d", "1w", "1mo"] | None = None
    confirmed_at: AwareDatetime | None = None


class BriefWindow(Contract):
    start: date
    end: date
    bar_count: int = Field(ge=1)
    anchors: tuple[date, ...]
    anchor_details: tuple[BriefAnchor, ...] = ()
    fallback: bool = False
    primary_pattern_start: date | None = None


class BriefCard(Contract):
    scenario_id: str
    original_rank: int = Field(ge=1)
    structure_key: str
    role: Literal["PRIMARY", "ALTERNATE"]
    difference: str
    name: str
    pattern_type: Pattern
    variant: str | None = Field(None, exclude_if=lambda value: value is None)
    pattern_direction: Literal["UP", "DOWN"]
    next_leg_bias: Literal["UP", "DOWN", "UNKNOWN"]
    stage: str
    degree: Degree
    parent_context: ParentContext
    parent_pattern: Pattern | None = None
    parent_degree: Degree | None = None
    parent_wave_label: str | None = None
    subwave_status: SubwaveStatus
    quality_score: float = Field(ge=0, le=100)
    coverage: float = Field(ge=0, le=1)
    relative_weight: float = Field(ge=0, le=1)
    conditions: tuple[LevelCondition, ...] = ()
    target_zones: tuple[PriceZone, ...] = ()
    warnings: tuple[str, ...] = ()
    structure: WaveStructure | None = Field(None, exclude_if=lambda value: value is None)


class BriefIssue(Contract):
    code: str
    scope: str
    message: str


class BriefPeriod(Contract):
    timeframe: Timeframe
    status: Literal["OK", "UNCLEAR", "INSUFFICIENT_DATA", "STALE_DATA", "SEARCH_LIMITED"]
    market_asof: date
    input_snapshot_id: str
    result_digest: str
    price_coordinate_id: str
    window: BriefWindow
    cards: tuple[BriefCard, ...] = ()
    omitted_weight: float = Field(0, ge=0, le=1)
    omitted_top_k_count: int | None = Field(None, ge=0)
    omitted_top_k_weight: float | None = Field(None, ge=0, le=1)
    issues: tuple[BriefIssue, ...] = ()


class BriefCrossTimeframe(Contract):
    state: Literal["ALIGNED", "CONFLICT", "UNDETERMINED", "NOT_AVAILABLE"]
    primary_bias: Literal["UP", "DOWN", "UNKNOWN"]
    weekly_bias: Literal["UP", "DOWN", "UNKNOWN"] | None = None
    primary_market_asof: date
    weekly_market_asof: date | None = None
    reason: str


class BriefForecast(Contract):
    event_id: str
    status: Literal[
        "APPROVED",
        "UNCALIBRATED",
        "INSUFFICIENT_EVIDENCE",
        "OUT_OF_SCOPE",
        "UNDEFINED_EVENT",
        "UNSUPPORTED_BIN",
        "MODEL_INVALID",
    ]
    horizon_sessions: int = Field(ge=1)
    horizon_unit: Literal["SESSIONS", "WEEKS"] = "SESSIONS"
    probabilities: dict[str, float] | None = None
    model_id: str | None = None
    reason: str | None = None
    scenario_id: str | None = None
    sample_count: int | None = None
    calibration_end: date | None = None
    scope: dict | None = None


class BriefPrevious(Contract):
    change: Literal[
        "FIRST_RECORD",
        "SAME_COUNT",
        "ALTERNATE_PROMOTED",
        "STAGE_ADVANCED",
        "COUNT_CHANGED",
        "TIP_MOVED",
        "HARD_INVALIDATED",
        "EVIDENCE_CHANGED",
        "SEARCH_LIMITED",
        "CONFIG_CHANGED",
        "DATA_REVISED",
        "NOT_COMPARABLE",
    ]
    reason: str
    previous_digest: str | None = None
    previous_market_asof: date | None = None
    previous_as_of: AwareDatetime | None = None
    previous_path: str | None = None
    structure_change: str | None = None
    data_revision: Literal["NONE", "REVISED", "NOT_COMPARABLE"] = "NONE"
    revision_count: int = Field(0, ge=0)
    factor_revision_count: int = Field(0, ge=0)
    triggered_conditions: tuple[LevelCondition, ...] = ()
    events: tuple[dict, ...] = Field((), exclude_if=lambda value: not value)
    input_change: str | None = Field(None, exclude_if=lambda value: value is None)


class WindowParticipant(Contract):
    start: date
    scenario_id: str | None = None
    structure_key: str | None = None
    next_leg_bias: Literal["UP", "DOWN", "UNKNOWN"] = "UNKNOWN"
    status: str
    reason: str


class BriefWindowComparison(Contract):
    state: Literal[
        "SAME_COUNT",
        "CHANGED_COUNT_SAME_BIAS",
        "CHANGED_BIAS",
        "INCONCLUSIVE",
        "NOT_APPLICABLE",
    ]
    primary_scenario_id: str | None = None
    alternate_scenario_id: str | None = None
    alternate_start: date | None = None
    alternate_bar_count: int | None = Field(None, ge=1)
    participants: tuple[WindowParticipant, ...] = Field((), exclude_if=lambda value: not value)
    total_expansions: int | None = Field(None, ge=0, exclude_if=lambda value: value is None)
    search_limits: tuple[str, ...] = Field((), exclude_if=lambda value: not value)
    reason: str


class Brief(Contract):
    schema_version: Literal["1.0", "1.1", "1.2"] = "1.1"
    brief_digest: str
    source_snapshot_id: str
    symbol: str
    as_of: AwareDatetime
    mode: str
    engine_version: str
    config_hash: str
    generated_at: AwareDatetime
    timeframe: Timeframe
    timeframe_reason: str
    status: Literal["OK", "UNCLEAR", "INSUFFICIENT_DATA", "STALE_DATA", "SEARCH_LIMITED"]
    market_asof: date
    input_snapshot_id: str
    result_digest: str
    price_coordinate_id: str
    feed: str
    adjustment: str
    latest_price: float = Field(gt=0)
    window: BriefWindow
    cards: tuple[BriefCard, ...] = ()
    omitted_weight: float = Field(0, ge=0, le=1)
    omitted_top_k_count: int | None = Field(None, ge=0)
    omitted_top_k_weight: float | None = Field(None, ge=0, le=1)
    issues: tuple[BriefIssue, ...] = ()
    weekly_state: Literal["NOT_REQUESTED", "AVAILABLE", "INSUFFICIENT_DATA"] = "NOT_REQUESTED"
    weekly: BriefPeriod | None = None
    cross_timeframe: BriefCrossTimeframe | None = None
    window_comparison: BriefWindowComparison | None = None
    previous: BriefPrevious | None = None
    forecasts: tuple[BriefForecast, ...] = ()
    forecast_status: Literal["UNCALIBRATED", "APPROVED", "INSUFFICIENT_EVIDENCE"] = "UNCALIBRATED"
    model_approved_at: AwareDatetime | None = None
    retrospective: bool = False
    decision_atr: float | None = Field(None, ge=0)
    outcome_dates: tuple[date, ...] = ()
    wave_relations: tuple[WaveRelation, ...] = Field((), exclude_if=lambda value: not value)
    forecast_note: str = "价格概率尚未校准；当前只提供条件情景与候选权重。"


def forecast_message(status: str) -> str:
    return {
        "APPROVED": "下表显示通过发布验证的事件概率及各事件状态。",
        "INSUFFICIENT_EVIDENCE": "当前事件缺少可发布的概率证据。",
        "UNCALIBRATED": "价格概率尚未校准。当前提供条件情景和候选权重。",
    }[status]
