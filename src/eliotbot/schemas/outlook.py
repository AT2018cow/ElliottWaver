"""Strict contracts for the short-term wave outlook."""

from datetime import date
from typing import Literal

from pydantic import AwareDatetime, Field

from eliotbot.schemas.market import Contract
from eliotbot.schemas.wave import LevelCondition, PriceZone

ConditionRole = Literal["ADVANCE", "CURRENT_EVIDENCE", "INVALIDATION", "STAGE_EXIT"]
OutlookStatus = Literal["AVAILABLE", "UNAVAILABLE"]
OutlookEventState = Literal["DEFINED", "UNDEFINED"]
OutlookSlot = Literal["PRIMARY", "ALTERNATE_1", "ALTERNATE_2"]
OutlookTargetMode = Literal["ACTIVE", "AFTER_ADVANCE", "NONE"]
OutlookDirection = Literal["UP", "DOWN", "UNKNOWN"]


class OutlookPeriod(Contract):
    start_at: AwareDatetime
    end_session: date
    end_at: AwareDatetime
    sessions: tuple[date, ...] = Field(min_length=1)


class OutlookCondition(Contract):
    role: ConditionRole
    condition: LevelCondition
    source_index: int | None = Field(None, ge=0)


class OutlookEvent(Contract):
    event_id: str
    state: OutlookEventState
    reason: str | None = None


class OutlookCase(Contract):
    scenario_id: str
    structure_key: str
    slot: OutlookSlot
    active_wave: str | None = None
    current_leg_direction: OutlookDirection = "UNKNOWN"
    advance_direction: OutlookDirection = "UNKNOWN"
    conditions: tuple[OutlookCondition, ...] = ()
    target_mode: OutlookTargetMode = "NONE"
    target_direction: OutlookDirection = "UNKNOWN"
    target: PriceZone | None = None
    events: tuple[OutlookEvent, ...] = ()


class BriefOutlook(Contract):
    version: Literal["1.0"] = "1.0"
    issued_at: AwareDatetime | None = None
    price_coordinate_id: str | None = None
    horizon: int = Field(ge=1)
    horizon_unit: Literal["SESSIONS", "WEEKS"]
    periods: tuple[OutlookPeriod, ...]
    status: OutlookStatus
    reason: str | None = None
    cases: tuple[OutlookCase, ...] = ()
