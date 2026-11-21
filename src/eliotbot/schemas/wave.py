from datetime import date
from typing import Literal

from pydantic import AwareDatetime, Field, model_validator

from eliotbot.schemas.market import Contract, Instrument
from eliotbot.timeframes import Timeframe

Pattern = Literal["IMPULSE", "ZIGZAG_ABC", "TRIANGLE", "FLAT", "DIAGONAL", "WXY"]
Degree = Literal["MINOR", "INTERMEDIATE", "PRIMARY", "CYCLE", "SUPERCYCLE", "UNKNOWN"]
ParentContext = Literal["UNKNOWN", "VERIFIED", "PROVISIONAL"]
ParentRelation = Literal["UNKNOWN", "EXACT_LEG", "CONTAINED"]
SubwaveStatus = Literal["UNAVAILABLE", "UNVERIFIED", "VERIFIED"]


class PivotPoint(Contract):
    pivot_id: str
    index: int = Field(ge=0)
    extreme_bar_id: str
    extreme_at: AwareDatetime
    confirmed_at: AwareDatetime | None = None
    price: float = Field(gt=0)
    kind: Literal["HIGH", "LOW"]
    state: Literal["SEED", "CONFIRMED", "DEVELOPING"]
    scale: float = Field(gt=0)
    threshold_at_extreme: float = Field(gt=0)


class RuleResult(Contract):
    rule_id: str
    status: Literal["PASS", "FAIL", "PENDING"]
    reason: str
    observed: float | None = None
    boundary: float | None = None
    evidence_bar_ids: tuple[str, ...] = ()


class WaveLabel(Contract):
    label: str
    pivot: PivotPoint


class LevelCondition(Contract):
    kind: Literal["INVALIDATION", "CONFIRMATION", "STAGE_EXIT"]
    side: Literal["HIGH", "LOW", "CLOSE"]
    op: Literal["<", "<=", ">", ">="]
    price: float = Field(gt=0)
    active_from: AwareDatetime
    reason: str
    coordinate_id: str


class PriceZone(Contract):
    low: float = Field(gt=0)
    high: float = Field(gt=0)
    label: str
    methods: tuple[str, ...]
    independent_method_count: int = Field(ge=1)
    conditional: bool = True

    @model_validator(mode="after")
    def ordered(self):
        if self.low > self.high:
            raise ValueError("zone bounds reversed")
        return self


class ScoreEvidence(Contract):
    feature: str
    group: str
    weight: float = Field(ge=0)
    status: Literal["AVAILABLE", "UNAVAILABLE", "NOT_APPLICABLE"]
    value: float | None = None
    fit: float = Field(0, ge=0, le=1)
    explanation: str


class ScoreBreakdown(Contract):
    quality_score: float = Field(ge=0, le=100)
    fit_score: float = Field(ge=0, le=100)
    coverage: float = Field(ge=0, le=1)
    evidence: tuple[ScoreEvidence, ...]


class ProjectedPoint(Contract):
    label: str
    target_price: float = Field(gt=0)
    min_bars: int = Field(ge=1)
    reference_bars: int = Field(ge=1)
    max_bars: int = Field(ge=1)
    timeframe: Timeframe = "1d"
    conditional: bool = True


class SubwaveLegEvidence(Contract):
    leg: str
    status: SubwaveStatus
    pattern: Pattern | None = None
    point_count: int = Field(0, ge=0)
    scale: float | None = Field(None, gt=0)
    depth: int = Field(0, ge=0, le=2)
    reason: str


StructureStatus = Literal["VERIFIED", "PENDING", "SEARCH_LIMITED", "REJECTED", "UNRESOLVED"]


class StructurePoint(Contract):
    point_id: str
    pivot: PivotPoint
    bar_id: str
    price_coordinate_id: str


class StructureLeg(Contract):
    wave_label: str
    left_id: str
    right_id: str
    allowed_templates: tuple[Pattern, ...] = ()
    child_node_id: str | None = None
    status: StructureStatus = "UNRESOLVED"
    reason_code: str | None = None
    failed_rule_ids: tuple[str, ...] = ()


class StructureNode(Contract):
    node_id: str
    pattern_type: Pattern
    variant: str | None = None
    timeframe: Timeframe
    direction: Literal["UP", "DOWN"]
    stage: str
    points: tuple[StructurePoint, ...]
    rules: tuple[RuleResult, ...] = ()
    child_links: tuple[StructureLeg, ...] = ()
    status: StructureStatus = "UNRESOLVED"
    requested_depth: int = Field(0, ge=0, le=2)
    verified_depth: int = Field(0, ge=0, le=2)
    reasons: tuple[str, ...] = ()


class WaveStructure(Contract):
    version: Literal["hierarchical_v1"] = "hierarchical_v1"
    root_id: str
    status: StructureStatus = "UNRESOLVED"
    requested_depth: int = Field(0, ge=0, le=2)
    verified_depth: int = Field(0, ge=0, le=2)
    nodes: tuple[StructureNode, ...]
    reasons: tuple[str, ...] = ()


class WaveRelation(Contract):
    weekly_scenario_id: str | None = None
    daily_scenario_id: str | None = None
    parent_wave_label: str | None = None
    relation: Literal["EXACT_LEG", "CONTAINED", "UNDETERMINED", "INCOMPATIBLE"] = "UNDETERMINED"
    status: Literal["VERIFIED", "PROVISIONAL", "UNDETERMINED", "INCOMPATIBLE"] = "UNDETERMINED"
    weekly_point_ids: tuple[str, ...] = ()
    daily_point_ids: tuple[str, ...] = ()
    structure: WaveStructure | None = Field(None, exclude_if=lambda value: value is None)
    reason: str = ""


class WaveScenario(Contract):
    scenario_id: str
    name: str
    pattern_type: Pattern
    variant: str | None = Field(None, exclude_if=lambda value: value is None)
    pattern_direction: Literal["UP", "DOWN"]
    next_leg_bias: Literal["UP", "DOWN", "UNKNOWN"]
    stage: str
    degree: Degree = "UNKNOWN"
    parent_context: ParentContext = "UNKNOWN"
    parent_relation: ParentRelation = "UNKNOWN"
    parent_scenario_id: str | None = None
    parent_pattern: Pattern | None = None
    parent_degree: Degree | None = None
    parent_wave_label: str | None = None
    subwave_status: SubwaveStatus = "UNAVAILABLE"
    subwave_evidence: tuple[SubwaveLegEvidence, ...] = ()
    relative_weight: float = Field(0, ge=0, le=1)
    calibrated_event_probability: None = None
    completed_waves: tuple[WaveLabel, ...]
    developing_tip: PivotPoint
    rules: tuple[RuleResult, ...]
    score: ScoreBreakdown
    conditions: tuple[LevelCondition, ...] = ()
    target_zones: tuple[PriceZone, ...] = ()
    projected_path: tuple[ProjectedPoint, ...] = ()
    discovered_scales: tuple[float, ...] = ()
    warnings: tuple[str, ...] = ()
    structure: WaveStructure | None = Field(None, exclude_if=lambda value: value is None)


class SearchDiagnostics(Contract):
    expansions: int = 0
    pruned: int = 0
    candidates_found: int = 0
    candidates_qualified: int = 0
    search_truncated: bool = False
    cache_hits: int = Field(0, ge=0, exclude_if=lambda value: value == 0)
    truncation_reason: str | None = Field(None, exclude_if=lambda value: value is None)
    anchors: tuple[date, ...] = ()
    pivot_counts: dict[str, int] = Field(default_factory=dict)


class AnalysisResult(Contract):
    schema_version: Literal["1.0", "1.1"] = "1.0"
    run_id: str
    result_digest: str
    engine_version: str
    config_hash: str
    input_snapshot_id: str
    instrument: Instrument
    as_of: AwareDatetime
    market_asof: date
    analyzed_at: AwareDatetime
    timeframe: Timeframe = "1d"
    mode: str
    status: Literal["OK", "UNCLEAR", "INSUFFICIENT_DATA", "STALE_DATA", "SEARCH_LIMITED"]
    ambiguity: Literal["LOW", "MEDIUM", "HIGH"]
    latest_price: float = Field(gt=0)
    latest_raw_close: float = Field(gt=0)
    price_coordinate_id: str
    feed: str
    adjustment: str
    scenarios: tuple[WaveScenario, ...]
    omitted_weight: float = Field(0, ge=0, le=1)
    diagnostics: SearchDiagnostics
    warnings: tuple[str, ...] = ()
