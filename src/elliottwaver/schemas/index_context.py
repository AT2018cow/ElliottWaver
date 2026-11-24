"""Point-in-time inputs and output for single-index market context."""

from datetime import date, datetime
from typing import Literal

from pydantic import AwareDatetime, Field, model_validator

from elliottwaver.schemas.market import Contract


class ConstituentVersion(Contract):
    symbol: str = Field(min_length=1)
    revision: int = Field(ge=1)
    announced_at: AwareDatetime
    received_at: AwareDatetime
    effective_from: date
    effective_through: date | None = None
    weight: float = Field(ge=0, le=1)
    withdrawn: bool = False

    @model_validator(mode="after")
    def ordered(self):
        if self.effective_through and self.effective_through < self.effective_from:
            raise ValueError("constituent coverage dates reversed")
        return self


class ConstituentClose(Contract):
    symbol: str = Field(min_length=1)
    session: date
    usable_at: AwareDatetime
    close: float = Field(gt=0)
    basis: str = Field(min_length=1)


class EventVersion(Contract):
    event_id: str = Field(min_length=1)
    revision: int = Field(ge=1)
    symbol: str = Field(min_length=1)
    kind: Literal["REBALANCE", "EARNINGS"]
    announcement_at: AwareDatetime
    received_at: AwareDatetime
    expected_at: AwareDatetime
    effective_at: AwareDatetime | None = None
    source: str = Field(min_length=1)
    withdrawn: bool = False

    def window_timestamp(self, cutoff: datetime) -> datetime:
        if self.effective_at is not None and self.effective_at <= cutoff:
            return self.effective_at
        return self.expected_at


class EventCoverage(Contract):
    kind: Literal["REBALANCE", "EARNINGS"]
    source: str = Field(min_length=1)
    known_at: AwareDatetime
    start: date
    end: date
    symbols: tuple[str, ...] = ()

    @model_validator(mode="after")
    def ordered(self):
        if self.start > self.end:
            raise ValueError("event coverage dates reversed")
        return self


class IndexContextDataset(Contract):
    version: Literal["1.0"] = "1.0"
    symbol: str = Field(min_length=1)
    membership: tuple[ConstituentVersion, ...] = ()
    closes: tuple[ConstituentClose, ...] = ()
    events: tuple[EventVersion, ...] = ()
    event_coverage: tuple[EventCoverage, ...] = ()

    @model_validator(mode="after")
    def unique_versions(self):
        for keys in (
            [(row.symbol, row.effective_from, row.revision) for row in self.membership],
            [(row.symbol, row.session, row.usable_at) for row in self.closes],
            [(row.event_id, row.revision) for row in self.events],
        ):
            if len(keys) != len(set(keys)):
                raise ValueError("duplicate context revision")
        identities = {}
        for row in self.events:
            if identities.setdefault(row.event_id, (row.symbol, row.kind)) != (
                row.symbol,
                row.kind,
            ):
                raise ValueError("event revisions cannot change subject or kind")
        return self


class IndexTrend(Contract):
    status: str
    start: date | None = None
    end: date | None = None
    sessions: int
    return_pct: float | None = None
    volatility: float | None = None
    volatility_percentile: float | None = None
    latest_overnight_gap_pct: float | None = None
    prior_windows: int = 0


class IndexReference(Contract):
    status: str
    symbol: str | None = None
    feed: str | None = None
    adjustment: str | None = None
    coordinate_id: str | None = None
    snapshot_id: str | None = None
    latest_usable_at: AwareDatetime | None = None
    start: date | None = None
    end: date | None = None
    main_return_pct: float | None = None
    reference_return_pct: float | None = None
    spread_pp: float | None = None


class IndexBreadth(Contract):
    status: str
    session: date | None = None
    members: int = 0
    comparable: int = 0
    rising: int = 0
    member_coverage: float | None = None
    weight_coverage: float | None = None
    rising_fraction: float | None = None


class EventWindow(Contract):
    horizon: Literal["SHORT", "SWING"]
    start: date
    end: date
    calendar_status: str
    next_open_at: AwareDatetime | None = None
    closed_weekdays: tuple[date, ...] = ()
    rebalance_status: str
    earnings_status: str
    earnings_covered_weight: float | None = None
    earnings_event_weight: float | None = None
    events: tuple[EventVersion, ...] = ()


class IntradayInput(Contract):
    timeframe: Literal["1d", "60m", "15m"]
    snapshot_id: str
    feed: str
    adjustment: str
    coordinate_id: str
    last_bar_id: str
    last_bar_usable_at: AwareDatetime
    last_bar_end_at: AwareDatetime
    max_age_minutes: int | None = None


class IntradayVariant(Contract):
    method: Literal["TOUCH", "CLOSE", "RETEST"]
    status: str
    raw_bar_id: str | None = None
    bar_end_at: AwareDatetime | None = None
    observable_at: AwareDatetime | None = None
    price: float | None = None
    transition_id: str | None = None


class IntradayTrack(Contract):
    horizon: Literal["SHORT", "SWING"]
    plan_id: str | None = None
    revision: int | None = None
    parent_brief_digest: str | None = None
    selected_method: Literal["TOUCH", "CLOSE", "RETEST"] | None = Field(
        None, exclude_if=lambda value: value is None
    )
    parent_visible_at: AwareDatetime | None = None
    deadline: AwareDatetime | None = None
    status: str
    threshold: float | None = None
    price_basis: str | None = None
    basis_sessions: tuple[date, ...] = ()
    basis_observable_at: AwareDatetime | None = None
    # Earliest qualifying index crossing under each frozen rule.
    variants: tuple[IntradayVariant, ...] = ()


class IntradayVolume(Contract):
    status: str
    sample_days: int = 0
    value: float | None = None


class IndexIntraday(Contract):
    version: Literal["1.0"] = "1.0"
    selected_method: Literal["TOUCH", "CLOSE", "RETEST"]
    policy_hash: str
    inputs: tuple[IntradayInput, ...] = ()
    background_bias: Literal["UP", "DOWN", "UNKNOWN"] = "UNKNOWN"
    volume: IntradayVolume
    tracks: tuple[IntradayTrack, IntradayTrack]


class IndexContext(Contract):
    version: Literal["1.0"] = "1.0"
    as_of: AwareDatetime
    main_snapshot_id: str
    daily_snapshot_id: str | None = None
    dataset_sha256: str | None = None
    dataset_status: str | None = None
    trend: IndexTrend | None = None
    reference: IndexReference | None = None
    breadth: IndexBreadth | None = None
    events: tuple[EventWindow, ...] = ()
    intraday: IndexIntraday | None = None
