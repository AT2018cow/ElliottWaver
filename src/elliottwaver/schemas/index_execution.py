"""Frozen inputs for single-index, cash-only ETF execution research."""

from collections import defaultdict
from datetime import date
from decimal import Decimal
from typing import Annotated, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import AwareDatetime, Field, model_validator

from elliottwaver.schemas.index_plan import IntradayPlanPolicy
from elliottwaver.schemas.market import Contract

Price = Annotated[Decimal, Field(gt=0, max_digits=16, decimal_places=8)]
Money = Annotated[Decimal, Field(ge=0, max_digits=20, decimal_places=8)]
BasisPoints = Annotated[Decimal, Field(ge=0, lt=10000, max_digits=10, decimal_places=5)]
Sha256 = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
Horizon = Literal["SHORT", "SWING"]
ExitPolicy = Literal["FIXED", "STAGNATION", "PARTIAL", "TRAILING"]
Split = Literal["DEVELOPMENT", "CALIBRATION", "HOLDOUT"]


class ToolMapping(Contract):
    index_symbol: str = Field(min_length=1)
    tool_symbol: str = Field(min_length=1)
    version: str = Field(min_length=1)
    venue: Literal["XNYS", "XSHG", "XSHE"]
    timezone: str
    currency: Literal["USD", "CNY"]
    feed: str = Field(min_length=1)

    @model_validator(mode="after")
    def validate_mapping(self):
        if self.index_symbol == self.tool_symbol:
            raise ValueError("ETF and index must have different symbols")
        expected = ("USD", "America/New_York") if self.venue == "XNYS" else ("CNY", "Asia/Shanghai")
        try:
            ZoneInfo(self.timezone)
        except ZoneInfoNotFoundError as exc:
            raise ValueError("unknown execution timezone") from exc
        if (self.currency, self.timezone) != expected:
            raise ValueError("execution venue, currency and timezone disagree")
        return self


class FeeSchedule(Contract):
    version: str = Field(min_length=1)
    effective_from: date
    tick: Price
    lot: int = Field(gt=0)
    sell_lag_sessions: int = Field(ge=0, le=10)
    commission_bps: BasisPoints = Decimal(0)
    minimum_commission: Money = Decimal(0)
    exchange_bps: BasisPoints = Decimal(0)
    buy_tax_bps: BasisPoints = Decimal(0)
    sell_tax_bps: BasisPoints = Decimal(0)
    cash_tick: Price = Decimal("0.01")


class ToolQuote(Contract):
    quote_id: str = Field(min_length=1)
    session: date
    start_at: AwareDatetime
    end_at: AwareDatetime
    observed_at: AwareDatetime
    opening_observed_at: AwareDatetime | None = None
    open: Price
    high: Price
    low: Price
    close: Price
    bid: Price | None = None
    ask: Price | None = None
    opening_volume: int | None = Field(None, ge=0)
    lower_limit: Price | None = None
    upper_limit: Price | None = None
    tradable: bool = True

    @model_validator(mode="after")
    def validate_quote(self):
        if not self.start_at < self.end_at <= self.observed_at:
            raise ValueError("quote and bar availability are out of order")
        if self.opening_observed_at and not (
            self.start_at <= self.opening_observed_at <= self.observed_at
        ):
            raise ValueError("opening receipt falls outside the observation interval")
        if not self.low <= min(self.open, self.close) <= max(self.open, self.close) <= self.high:
            raise ValueError("invalid tool OHLC")
        if (self.bid is None) != (self.ask is None) or (
            self.bid is not None and self.bid > self.ask
        ):
            raise ValueError("tool quotes require an ordered bid/ask pair")
        if (self.lower_limit and self.low < self.lower_limit) or (
            self.upper_limit and self.high > self.upper_limit
        ):
            raise ValueError("tool OHLC exceeds the provided price limits")
        return self


class ToolAction(Contract):
    action_id: str = Field(min_length=1)
    kind: Literal["SPLIT", "DIVIDEND"]
    known_at: AwareDatetime
    effective_at: AwareDatetime
    amount: Price
    pay_at: AwareDatetime | None = None

    @model_validator(mode="after")
    def validate_action(self):
        if self.known_at > self.effective_at:
            raise ValueError("corporate action requires pre-effective evidence")
        if self.kind == "DIVIDEND":
            if self.pay_at is None or self.pay_at < self.effective_at:
                raise ValueError("dividend payment must follow entitlement")
        elif self.pay_at is not None:
            raise ValueError("split cannot declare a cash payment")
        return self


class ToolArchive(Contract):
    version: Literal["1.0"] = "1.0"
    mapping: ToolMapping
    evidence: Literal["SYNTHETIC", "FROZEN", "RECEIVED"]
    cutoff: AwareDatetime
    price_basis: Literal["RAW"] = "RAW"
    quotes: tuple[ToolQuote, ...] = Field(min_length=1)
    actions: tuple[ToolAction, ...] = ()

    @model_validator(mode="after")
    def validate_archive(self):
        zone = ZoneInfo(self.mapping.timezone)
        if any(
            q.observed_at > self.cutoff or q.session != q.start_at.astimezone(zone).date()
            for q in self.quotes
        ):
            raise ValueError("quote cutoff or tool-local session mismatch")
        if any(
            a.end_at > b.start_at or a.session >= b.session
            for a, b in zip(self.quotes, self.quotes[1:], strict=False)
        ):
            raise ValueError("expected one ordered tool bar per trading session")
        for ids in ([q.quote_id for q in self.quotes], [a.action_id for a in self.actions]):
            if len(ids) != len(set(ids)):
                raise ValueError("duplicate tool or action evidence")
        actions_at = defaultdict(list)
        for action in self.actions:
            actions_at[action.effective_at].append(action.kind)
        if any("SPLIT" in kinds and len(kinds) > 1 for kinds in actions_at.values()):
            raise ValueError("A split requires a distinct effective timestamp from other actions")
        if any(a.known_at > self.cutoff for a in self.actions):
            raise ValueError("action could not have been known by the archive cutoff")
        return self


class ExecutionPolicy(Contract):
    capital: Price
    quantity: int = Field(gt=0)
    allocation_priority: tuple[Horizon, Horizon] = ("SHORT", "SWING")
    risk_budget: Price
    max_entry_price: Price
    stop_pct: Price = Field(lt=1)
    target_pct: Price
    max_open_participation: Price = Field(le=1)
    slippage_bps: BasisPoints = Decimal(0)
    entry_sessions: int = Field(1, ge=1, le=10)
    stagnant_sessions: int = Field(3, ge=1, le=30)
    progress_pct: Price = Decimal("0.01")
    partial_fraction: Price = Field(Decimal("0.5"), lt=1)
    trailing_pct: Price = Field(Decimal("0.03"), lt=1)

    @model_validator(mode="after")
    def validate_policy(self):
        if len(set(self.allocation_priority)) != 2:
            raise ValueError("allocation needs a unique priority for each holding horizon")
        return self


class ExecutionSignal(Contract):
    key: str
    origin_digest: str
    evidence_id: str
    horizon: Horizon
    available_at: AwareDatetime
    due_at: AwareDatetime
    cancel_at: AwareDatetime | None = None
    holding_sessions: int | None = Field(None, ge=1)
    label_by: AwareDatetime | None = None


class IssuanceSlot(Contract):
    session: date
    cutoff: AwareDatetime
    issue_by: AwareDatetime
    label_by: AwareDatetime
    split: Split

    @model_validator(mode="after")
    def validate_slot(self):
        if not self.cutoff <= self.issue_by < self.label_by:
            raise ValueError("issuance and label deadlines are out of order")
        return self


class TradingProtocol(Contract):
    kind: Literal["SINGLE_INDEX_EXECUTION"] = "SINGLE_INDEX_EXECUTION"
    version: Literal["1.0"] = "1.0"
    mode: Literal["FROZEN", "FORWARD"]
    mapping: ToolMapping
    index_feed: str = Field(min_length=1)
    index_adjustment: Literal["none", "synthetic_no_actions"] = "none"
    analysis_config_hash: Sha256 | None = None
    guidance_policy_hash: Sha256 | None = None
    intraday_feed: str | None = Field(None, min_length=1)
    intraday_policy: IntradayPlanPolicy | None = None
    settings: ExecutionPolicy
    rules: tuple[FeeSchedule, ...] = Field(min_length=1)
    grid: tuple[IssuanceSlot, ...] = Field(min_length=1)
    index_snapshot_id: Sha256 | None = None
    tool_archive_sha256: Sha256 | None = None
    experiment_attempts: tuple[str, ...] = Field(min_length=1)
    momentum_sessions: int = Field(3, ge=1, le=60)
    baseline_holding_sessions: int = Field(5, ge=2, le=30)
    block_sessions: int = Field(30, ge=2)
    min_holdout_sessions: int = Field(60, ge=2)
    min_holdout_trades: int = Field(10, ge=1)
    min_timely_fraction: float = Field(0.95, gt=0, le=1)
    min_net_increment: Decimal = Decimal(0)
    max_drawdown: Price = Field(Decimal("0.2"), le=1)
    bootstrap_samples: int = Field(1000, ge=100, le=20000)
    bootstrap_seed: int = 0

    @model_validator(mode="after")
    def validate_protocol(self):
        dates = [r.effective_from for r in self.rules]
        if dates != sorted(set(dates)):
            raise ValueError("historical rules need unique ordered effective dates")
        if self.mode == "FROZEN" and not self.index_snapshot_id:
            raise ValueError("frozen replay needs the index settlement snapshot id")
        if self.mode == "FORWARD" and (self.index_snapshot_id or self.tool_archive_sha256):
            raise ValueError("forward seals must precede future snapshot identities")
        if self.mode == "FORWARD" and not (self.analysis_config_hash and self.guidance_policy_hash):
            raise ValueError(
                "forward research requires the sealed analysis and index guidance hashes"
            )
        if (
            self.intraday_feed is not None
            and self.mode == "FORWARD"
            and self.intraday_policy is None
        ):
            raise ValueError("forward intraday research requires a frozen intraday policy")
        if len(set(self.experiment_attempts)) != len(self.experiment_attempts):
            raise ValueError("duplicate registered parameter attempts")
        split_rank = {"DEVELOPMENT": 0, "CALIBRATION": 1, "HOLDOUT": 2}
        if any(
            a.session >= b.session
            or a.issue_by >= b.cutoff
            or split_rank[a.split] > split_rank[b.split]
            for a, b in zip(self.grid, self.grid[1:], strict=False)
        ):
            raise ValueError("grid must follow a unique, chronological session and split order")
        if self.mode == "FORWARD" and any(row.split != "HOLDOUT" for row in self.grid):
            raise ValueError("forward execution study requires a sealed holdout grid")
        return self
