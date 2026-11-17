from datetime import UTC, date, datetime
from typing import Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, field_validator, model_validator

from eliotbot.timeframes import Timeframe, normalize_timeframe
from eliotbot.util import digest

Market = Literal["CN", "US"]


class Contract(BaseModel):
    model_config = ConfigDict(
        extra="forbid", frozen=True, allow_inf_nan=False, revalidate_instances="always"
    )


class Instrument(Contract):
    instrument_id: str
    input_symbol: str
    canonical_symbol: str
    provider_symbol: str
    market: Market
    exchange: str
    currency: str
    timezone: str
    name: str = ""
    tick_size: float = Field(0.01, gt=0)
    identity_verified: bool = False


class Bar(Contract):
    start_at: AwareDatetime
    end_at: AwareDatetime
    session_date: date
    open: float = Field(gt=0)
    high: float = Field(gt=0)
    low: float = Field(gt=0)
    close: float = Field(gt=0)
    volume: float = Field(ge=0)
    amount: float | None = Field(None, ge=0)
    available_at: AwareDatetime | None = None
    observed_at: AwareDatetime
    is_final: bool = True
    source_revision: str = ""

    @field_validator("start_at", "end_at", "available_at", "observed_at")
    @classmethod
    def utc(cls, value: datetime | None):
        return value.astimezone(UTC) if value else None

    @model_validator(mode="after")
    def valid_bar(self):
        if self.end_at <= self.start_at:
            raise ValueError("bar end must follow start")
        if self.available_at and self.available_at < self.end_at:
            raise ValueError("bar availability cannot precede the end of its interval")
        if not self.low <= min(self.open, self.close) <= max(self.open, self.close) <= self.high:
            raise ValueError("inconsistent OHLC ordering")
        return self

    @property
    def bar_id(self) -> str:
        return digest(self.model_dump(mode="json", exclude={"observed_at"}))[:24]

    @property
    def usable_at(self) -> datetime:
        return max(self.end_at, self.available_at or self.end_at)


class FactorPoint(Contract):
    effective_date: date
    factor: float = Field(gt=0)
    known_at: AwareDatetime | None = None


class Provenance(Contract):
    provider: str
    feed: str
    adjustment: str
    price_coordinate_id: str
    basis_date: date
    session_profile: str
    calendar_version: str
    parser_version: str = "1"
    fetched_at: AwareDatetime
    requested_start: date
    requested_end: date
    factor_hash: str = ""
    volume_unit: str = "shares"
    amount_currency: str = ""
    volume_comparable: bool = True
    known_suspensions: tuple[date, ...] = ()
    warnings: tuple[str, ...] = ()


class MarketDataBundle(Contract):
    schema_version: Literal["1.1"] = "1.1"
    timeframe: Timeframe = "1d"
    instrument: Instrument
    as_of: AwareDatetime
    bars: tuple[Bar, ...]
    raw_bars: tuple[Bar, ...]
    provenance: Provenance
    sessions: tuple[date, ...]
    factors: tuple[FactorPoint, ...] = ()
    auxiliary: tuple[Bar, ...] = ()
    daily_context: tuple[Bar, ...] = ()
    raw_daily_context: tuple[Bar, ...] = ()
    expected_bar_starts: tuple[AwareDatetime, ...] = ()

    @field_validator("as_of")
    @classmethod
    def utc_asof(cls, value):
        return value.astimezone(UTC)

    @field_validator("expected_bar_starts")
    @classmethod
    def utc_intervals(cls, values):
        return tuple(v.astimezone(UTC) for v in values)

    @model_validator(mode="after")
    def valid_bundle(self):
        if not self.bars or len(self.bars) != len(self.raw_bars):
            raise ValueError("nonempty raw and analysis bars must have equal length")
        for series in (
            self.bars,
            self.raw_bars,
            self.auxiliary,
            self.daily_context,
            self.raw_daily_context,
        ):
            starts = [b.start_at for b in series]
            if starts != sorted(set(starts)):
                raise ValueError("bars must be strictly ordered and unique by timestamp")
            if any(not b.is_final or b.usable_at > self.as_of for b in series):
                raise ValueError("bundle contains unfinished or unavailable future bars")
        if [(b.start_at, b.end_at) for b in self.bars] != [
            (b.start_at, b.end_at) for b in self.raw_bars
        ]:
            raise ValueError("raw and analysis timestamps do not align")
        if tuple(sorted(set(self.sessions))) != self.sessions:
            raise ValueError("sessions must be strictly ordered and unique")
        if any(b.session_date not in self.sessions for b in self.bars):
            raise ValueError("bar is outside the declared trading calendar")
        dates = [p.effective_date for p in self.factors]
        if dates != sorted(set(dates)):
            raise ValueError("factor dates must be ordered and unique")
        if self.provenance.basis_date != self.bars[-1].session_date:
            raise ValueError("basis_date must identify the last analyzed daily bar")
        if len(self.daily_context) != len(self.raw_daily_context):
            raise ValueError("raw and adjusted daily context must align")
        if [(b.start_at, b.end_at) for b in self.daily_context] != [
            (b.start_at, b.end_at) for b in self.raw_daily_context
        ]:
            raise ValueError("daily context timestamps do not align")
        if tuple(sorted(set(self.expected_bar_starts))) != self.expected_bar_starts:
            raise ValueError("expected bar starts must be ordered and unique")
        if self.timeframe.endswith("m") and not self.expected_bar_starts:
            raise ValueError("intraday data requires explicit expected bar intervals")
        if self.expected_bar_starts and self.expected_bar_starts[-1] != self.bars[-1].start_at:
            raise ValueError("expected final bar is absent")
        return self

    @property
    def snapshot_id(self) -> str:
        return digest(self)


class AnalysisRequest(Contract):
    symbol: str
    as_of: AwareDatetime
    timeframe: Timeframe = "1d"
    mode: Literal["LIVE", "FROZEN_DATA_REPLAY", "POINT_IN_TIME_REPLAY"] = "LIVE"
    start: date | None = None
    end: date | None = None
    anchor: date | None = None
    feed: Literal["sip", "iex"] | None = None
    top_k: int | None = Field(None, ge=1, le=20)
    allow_stale: bool = False

    @field_validator("as_of")
    @classmethod
    def utc_asof(cls, value):
        return value.astimezone(UTC)

    @field_validator("timeframe", mode="before")
    @classmethod
    def normalize_period(cls, value):
        return normalize_timeframe(value)

    @model_validator(mode="after")
    def valid_range(self):
        if self.start and self.end and self.start > self.end:
            raise ValueError("start cannot follow end")
        if self.anchor and self.start and self.anchor < self.start:
            raise ValueError("anchor cannot precede start")
        if self.anchor and self.end and self.anchor > self.end:
            raise ValueError("anchor cannot follow end")
        return self
