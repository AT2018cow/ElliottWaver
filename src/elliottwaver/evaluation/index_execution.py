"""ETF fills, a reconciled cash ledger and entry-conditioned exit research."""

from bisect import bisect_right
from collections import Counter, defaultdict
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import ROUND_CEILING, ROUND_FLOOR, ROUND_HALF_UP, Decimal

from elliottwaver.errors import InputError
from elliottwaver.schemas.index_execution import (
    ExecutionPolicy,
    ExecutionSignal,
    ExitPolicy,
    FeeSchedule,
    ToolArchive,
    ToolQuote,
)
from elliottwaver.util import digest

ZERO = Decimal(0)
BPS = Decimal(10000)
EXIT_POLICIES: tuple[ExitPolicy, ...] = ("FIXED", "STAGNATION", "PARTIAL", "TRAILING")


def _round(value: Decimal, step: Decimal, rounding: str) -> Decimal:
    return (value / step).to_integral_value(rounding=rounding) * step


def _lots(quantity: Decimal | int, lot: int) -> int:
    return max(0, int(quantity) // lot * lot)


def _fees(value: Decimal, rule: FeeSchedule, buy: bool, multiplier: int) -> dict[str, Decimal]:
    tax = rule.buy_tax_bps if buy else rule.sell_tax_bps
    return {
        key: _round(amount * multiplier, rule.cash_tick, ROUND_HALF_UP)
        for key, amount in (
            ("commission", max(rule.minimum_commission, value * rule.commission_bps / BPS)),
            ("exchange_fee", value * rule.exchange_bps / BPS),
            ("tax", value * tax / BPS),
        )
    }


def _price(
    quote: ToolQuote, rule: FeeSchedule, policy: ExecutionPolicy, buy: bool, multiplier: int
) -> Decimal:
    reference = (quote.ask if buy else quote.bid) or quote.open
    direction = 1 if buy else -1
    adjusted = reference * (1 + direction * policy.slippage_bps * multiplier / BPS)
    return _round(adjusted, rule.tick, ROUND_CEILING if buy else ROUND_FLOOR)


@dataclass
class Position:
    signal: ExecutionSignal
    quantity: int
    initial_quantity: int
    acquired_at: datetime
    sellable_session: int
    entry_price: Decimal
    entry_cost: Decimal
    risk: Decimal
    high_water: Decimal
    last_holding_session: int | None = None
    prior_highs: list[tuple[datetime, datetime, Decimal]] = field(default_factory=list)
    latest_exit_bar: datetime | None = None
    factor: Decimal = Decimal(1)
    dividends_per_original_share: Decimal = ZERO
    proceeds: Decimal = ZERO
    sell_turnover: Decimal = ZERO
    dividends: Decimal = ZERO
    stagnant: int = 0
    partial_done: bool = False
    last_exit: datetime | None = None
    exit_reason: str | None = None
    pending: tuple[str, int] | None = None
    adjustments: list[tuple[datetime, Decimal, Decimal]] = field(default_factory=list)
    ranges: list[tuple[datetime, datetime, Decimal, Decimal]] = field(default_factory=list)
    matched_sales: list[dict] = field(default_factory=list)

    def observe_range(
        self, high: Decimal, low: Decimal, seen: datetime, *, anchor: datetime, through: datetime
    ):
        factor, dividends = next(
            ((f, d) for effective, f, d in reversed(self.adjustments) if effective <= anchor),
            (Decimal(1), ZERO),
        )
        upper = (high * factor + dividends) / self.entry_price - 1
        lower = 1 - (low * factor + dividends) / self.entry_price
        self.ranges.append((through, seen, upper, lower))

    def excursions(self, progress: Decimal, *, through: datetime | None = None) -> dict:
        peak = adverse = ZERO
        peak_at = adverse_at = progressed_at = None
        for end_at, seen, upper, lower in self.ranges:
            if through is not None and end_at > through:
                continue
            if upper > peak:
                peak, peak_at = upper, seen
            if lower > adverse:
                adverse, adverse_at = lower, seen
            if upper >= progress and (progressed_at is None or seen < progressed_at):
                progressed_at = seen
        return {
            "mfe": peak,
            "mfe_observable_at": peak_at,
            "mae": adverse,
            "mae_observable_at": adverse_at,
            "first_progress_seconds": (progressed_at - self.acquired_at).total_seconds()
            if progressed_at
            else None,
        }

    def result(self, progress: Decimal) -> dict:
        closed = self.quantity == 0
        entry_value = self.entry_price * self.initial_quantity
        gross_return = (self.sell_turnover + self.dividends - entry_value) / entry_value
        excursions = self.excursions(progress)
        matched = []
        for sale in self.matched_sales:
            observed = self.excursions(progress, through=sale["exit_at"])
            ratio = sale["gross_return"] / observed["mfe"] if observed["mfe"] else None
            matched.append({**sale, **observed, "capture_ratio": ratio})
        return {
            "signal_key": self.signal.key,
            "origin_digest": self.signal.origin_digest,
            "evidence_id": self.signal.evidence_id,
            "horizon": self.signal.horizon,
            "entry_at": self.acquired_at,
            "entry_price": self.entry_price,
            "quantity": self.initial_quantity,
            "open_quantity": self.quantity,
            "exit_at": self.last_exit,
            "exit_reason": self.exit_reason,
            "holding_seconds": (self.last_exit - self.acquired_at).total_seconds()
            if closed and self.last_exit
            else None,
            "entry_cost": self.entry_cost,
            "sell_proceeds": self.proceeds,
            "entitled_dividends": self.dividends,
            "turnover": entry_value + self.sell_turnover,
            "net_pnl": self.proceeds + self.dividends - self.entry_cost if closed else None,
            "gross_return": gross_return if closed else None,
            **excursions,
            "matched_sales": matched,
            "capture_ratio": gross_return / excursions["mfe"]
            if closed and excursions["mfe"]
            else None,
            "status": "CLOSED" if closed else "RIGHT_CENSORED",
        }


def _close_instruction(
    position: Position,
    quote: ToolQuote,
    policy: ExecutionPolicy,
    alternative: ExitPolicy,
    optimistic: bool,
    lot: int,
) -> tuple[tuple[str, int] | None, bool]:
    stop = position.entry_price * (1 - policy.stop_pct)
    if alternative == "TRAILING":
        prior_high = max(
            (
                high
                for end, seen, high in position.prior_highs
                if end <= quote.start_at and seen <= quote.start_at
            ),
            default=position.entry_price,
        )
        stop = max(stop, prior_high * (1 - policy.trailing_pct))
    target = position.entry_price * (1 + policy.target_pct)
    high = quote.high * position.factor + position.dividends_per_original_share
    low = quote.low * position.factor + position.dividends_per_original_share
    hit_stop, hit_target = low <= stop, high >= target
    ambiguous = hit_stop and hit_target
    position.prior_highs.append((quote.end_at, quote.observed_at, high))
    advanced = high >= position.high_water * (1 + policy.progress_pct)
    position.stagnant = 0 if advanced else position.stagnant + 1
    position.high_water = max(position.high_water, high)
    if hit_stop and (not ambiguous or not optimistic or position.partial_done):
        return ("AMBIGUOUS_STOP_FIRST" if ambiguous else "STOP", position.quantity), ambiguous
    if hit_target and alternative in ("FIXED", "PARTIAL") and not position.partial_done:
        if alternative == "PARTIAL":
            quantity = _lots(position.quantity * policy.partial_fraction, lot)
            return (("PARTIAL_TARGET", quantity) if quantity else None), ambiguous
        return ("AMBIGUOUS_TARGET_FIRST" if ambiguous else "TARGET", position.quantity), ambiguous
    if hit_stop:
        return ("AMBIGUOUS_STOP_FIRST" if ambiguous else "STOP", position.quantity), ambiguous
    if alternative == "STAGNATION" and position.stagnant >= policy.stagnant_sessions:
        return ("STAGNATION", position.quantity), ambiguous
    return None, ambiguous


def replay_execution(
    archive: ToolArchive,
    rules: Sequence[FeeSchedule],
    policy: ExecutionPolicy,
    signals: Sequence[ExecutionSignal],
    *,
    alternative: ExitPolicy = "FIXED",
    optimistic: bool = False,
    target_first: bool = False,
    cost_multiplier: int = 1,
    execution_delay: int = 0,
    enforce_receipts: bool = False,
    hold_only: bool = False,
    fixed_entries: dict[str, tuple[str, int]] | None = None,
    session_opens: Sequence[tuple[date, datetime]] | None = None,
) -> dict:
    """Process fills at witnessed openings and exit instructions on completed observations."""
    unique: dict[str, ExecutionSignal] = {}
    for signal in signals:
        if unique.setdefault(signal.key, signal) != signal:
            raise InputError(f"Conflicting repeat execution signal {signal.key}")
    priority = {name: i for i, name in enumerate(policy.allocation_priority)}
    signals = sorted(unique.values(), key=lambda s: (s.available_at, priority[s.horizon], s.key))
    delayed_buys, delayed_sells = Counter(), Counter()
    cash = policy.capital
    receivables = ZERO
    positions: dict[str, Position] = {}
    orders, ledger, nav, ambiguities = [], [], [], []
    states = {}
    # Snapshot account quantities at the valuation close, separately from price receipt.
    events = []
    valuation_accounts = {}
    for quote in archive.quotes:
        events.extend(
            [
                (quote.start_at, 2, quote.quote_id, quote),
                (quote.end_at, 3, quote.quote_id, quote),
                (quote.observed_at, 4, quote.quote_id, quote),
            ]
        )
    for action in archive.actions:
        if action.effective_at <= archive.cutoff:
            events.append((action.effective_at, 0, action.action_id, action))
        if action.pay_at and action.pay_at <= archive.cutoff:
            events.append((action.pay_at, 1, action.action_id, action))
    schedule = session_opens or [(q.session, q.start_at) for q in archive.quotes]
    index_by_session = {day: i for i, (day, _) in enumerate(schedule)}
    opening_times = [opened for _, opened in schedule]
    first_slot = {s.key: bisect_right(opening_times, s.available_at) for s in signals}
    active_rules = {}
    for quote in archive.quotes:
        valid = [rule for rule in rules if rule.effective_from <= quote.session]
        if not valid:
            raise InputError(f"No execution rule effective for {quote.session}")
        active_rules[quote.quote_id] = max(valid, key=lambda rule: rule.effective_from)
    if 2 * policy.slippage_bps >= BPS:
        raise InputError("doubled slippage must keep modeled bid prices positive")
    last_price, last_mark = None, None
    unpaid = defaultdict(lambda: ZERO)
    owned = set()

    def transact(now, quote, rule, signal, side, quantity, price, reason):
        nonlocal cash
        fees = _fees(quantity * price, rule, side == "BUY", cost_multiplier)
        change = quantity * price
        cash_delta = (change if side == "SELL" else -change) - sum(fees.values())
        cash += cash_delta
        ledger.append(
            {
                "fill_id": digest([signal.key, quote.quote_id, side]),
                "at": now,
                "session": quote.session,
                "quote_id": quote.quote_id,
                "rule": rule.version,
                "tool": archive.mapping.tool_symbol,
                "signal_key": signal.key,
                "side": side,
                "quantity": quantity,
                "price": price,
                "fees": fees,
                "cash_delta": cash_delta,
                "cash": cash,
                "reason": reason,
                "price_evidence": "OPENING_BID_ASK" if quote.bid else "OPEN_PROXY",
            }
        )
        return cash_delta

    for now, event, identity, record in sorted(events, key=lambda item: item[:3]):
        if event == 0:
            for key in sorted(owned):
                position = positions[key]
                if record.kind == "SPLIT":
                    shares = position.quantity * record.amount
                    if shares != int(shares):
                        raise InputError(f"Split {identity} requires a fractional-share settlement")
                    original = position.quantity
                    position.quantity = int(shares)
                    position.factor *= record.amount
                    if position.pending:
                        reason, qty = position.pending
                        position.pending = reason, min(position.quantity, int(qty * record.amount))
                    ledger.append(
                        {
                            "at": now,
                            "action_id": identity,
                            "signal_key": key,
                            "side": "SPLIT",
                            "quantity_before": original,
                            "quantity": position.quantity,
                            "cash_delta": ZERO,
                            "cash": cash,
                        }
                    )
                else:
                    value = position.quantity * record.amount
                    receivables += value
                    unpaid[identity] += value
                    position.dividends += value
                    position.dividends_per_original_share += record.amount * position.factor
                    ledger.append(
                        {
                            "at": now,
                            "action_id": identity,
                            "signal_key": key,
                            "side": "DIVIDEND_ENTITLEMENT",
                            "receivable": value,
                            "cash_delta": ZERO,
                            "cash": cash,
                        }
                    )
                position.adjustments.append(
                    (now, position.factor, position.dividends_per_original_share)
                )
            if record.kind == "SPLIT" and last_price is not None:
                last_price /= record.amount
            if record.kind == "DIVIDEND" and last_price is not None:
                last_price = max(ZERO, last_price - record.amount)
            continue
        if event == 1:
            value = unpaid.pop(identity, ZERO)
            cash += value
            receivables -= value
            ledger.append(
                {
                    "at": now,
                    "action_id": identity,
                    "side": "DIVIDEND_PAYMENT",
                    "cash_delta": value,
                    "cash": cash,
                }
            )
            continue
        quote = record
        rule = active_rules[identity]
        session_index = index_by_session[quote.session]
        if event == 3:
            valuation_accounts[identity] = (
                cash,
                receivables,
                sum(positions[key].quantity for key in owned),
            )
            continue
        if event == 4:
            for key, position in positions.items():
                if (
                    not quote.tradable
                    or (position.signal.label_by and now > position.signal.label_by)
                    or quote.start_at < position.acquired_at
                    or (key not in owned and position.last_exit < quote.end_at)
                ):
                    continue
                position.observe_range(
                    quote.high, quote.low, now, anchor=quote.start_at, through=quote.end_at
                )
                if (
                    hold_only
                    or key not in owned
                    or (position.adjustments and position.adjustments[-1][0] > quote.start_at)
                ):
                    continue
                if (
                    alternative == "STAGNATION"
                    and position.latest_exit_bar is not None
                    and quote.end_at < position.latest_exit_bar
                ):
                    continue
                position.latest_exit_bar = max(
                    quote.end_at, position.latest_exit_bar or quote.end_at
                )
                instruction, ambiguous = _close_instruction(
                    position, quote, policy, alternative, target_first, rule.lot
                )
                if ambiguous:
                    ambiguities.append(
                        {"signal_key": key, "quote_id": identity, "observed_at": now}
                    )
                if instruction and (
                    position.pending is None
                    or (
                        position.pending[0] == "PARTIAL_TARGET"
                        and instruction[0] != "PARTIAL_TARGET"
                    )
                ):
                    position.pending = instruction
            if last_mark is None or quote.end_at > last_mark:
                last_price, last_mark = quote.close, quote.end_at
                for action in sorted(archive.actions, key=lambda row: row.effective_at):
                    if quote.end_at < action.effective_at <= now:
                        if action.kind == "SPLIT":
                            last_price /= action.amount
                        else:
                            last_price = max(ZERO, last_price - action.amount)
            marked_cash, marked_receivables, marked_quantity = valuation_accounts.pop(identity)
            market_value = marked_quantity * quote.close
            nav.append(
                {
                    "session": quote.session,
                    "at": now,
                    "mark_at": quote.end_at,
                    "cash": marked_cash,
                    "market_value": market_value,
                    "receivables": marked_receivables,
                    "net_value": marked_cash + market_value + marked_receivables,
                    "quantity": marked_quantity,
                }
            )
            continue

        receipt_ok = quote.opening_observed_at == now or (
            quote.opening_observed_at is None and not enforce_receipts
        )
        executable = quote.tradable and receipt_ok and (quote.bid is not None or optimistic)
        capacity = _lots((quote.opening_volume or 0) * policy.max_open_participation, rule.lot)
        if quote.opening_volume is None and optimistic and not enforce_receipts:
            capacity = policy.quantity
        for key in sorted(owned):
            position = positions[key]
            signal = position.signal
            if signal.label_by and now > signal.label_by:
                continue
            forced = (
                "INDEX_EXIT"
                if signal.cancel_at and signal.cancel_at < now
                else "HOLDING_DEADLINE"
                if (
                    session_index > position.last_holding_session
                    if position.last_holding_session is not None
                    else signal.due_at < now
                )
                else None
            )
            instruction = (forced, position.quantity) if forced else position.pending
            if not instruction:
                continue
            reason, requested = instruction
            price = _price(quote, rule, policy, False, cost_multiplier)
            # A complete liquidation can sell the odd lot created by a split.
            quantity = (
                position.quantity
                if requested >= position.quantity and capacity >= position.quantity
                else _lots(min(requested, position.quantity, capacity), rule.lot)
            )
            if (
                not executable
                or price <= 0
                or (quote.lower_limit and price < quote.lower_limit)
                or (quote.upper_limit and price > quote.upper_limit)
                or session_index < position.sellable_session
                or not quantity
                or quantity * price
                < sum(_fees(quantity * price, rule, False, cost_multiplier).values())
            ):
                orders.append(
                    {
                        "signal_key": key,
                        "at": now,
                        "quote_id": identity,
                        "side": "SELL",
                        "status": "DEFERRED",
                        "reason": reason,
                    }
                )
                continue
            delay_key = (key, reason)
            if delayed_sells[delay_key] < execution_delay:
                delayed_sells[delay_key] += 1
                orders.append(
                    {
                        "signal_key": key,
                        "at": now,
                        "quote_id": identity,
                        "side": "SELL",
                        "status": "DELAY_STRESS",
                        "reason": reason,
                    }
                )
                continue
            position.observe_range(price, price, now, anchor=now, through=now)
            net_sale = transact(now, quote, rule, signal, "SELL", quantity, price, reason)
            equivalent = quantity / position.factor
            original_value = equivalent * position.entry_price
            allocated_cost = position.entry_cost * equivalent / position.initial_quantity
            sale_value = quantity * price
            entitled = equivalent * position.dividends_per_original_share
            position.matched_sales.append(
                {
                    "exit_at": now,
                    "sell_fill_id": ledger[-1]["fill_id"],
                    "quantity": quantity,
                    "reason": reason,
                    "entry_cost": allocated_cost,
                    "entitled_dividends": entitled,
                    "net_pnl": net_sale + entitled - allocated_cost,
                    "gross_return": (sale_value + entitled - original_value) / original_value,
                }
            )
            position.proceeds += net_sale
            position.sell_turnover += sale_value
            position.quantity -= quantity
            available_risk = position.risk * position.quantity / (position.quantity + quantity)
            position.risk = available_risk
            position.last_exit, position.exit_reason = now, reason
            capacity -= quantity
            if reason == "PARTIAL_TARGET" and quantity == requested:
                position.partial_done, position.pending = True, None
            else:
                position.pending = (reason, min(position.quantity, requested - quantity))
            if not position.quantity:
                owned.remove(key)

        eligible = sorted(
            (s for s in signals if s.key not in states and s.available_at < now),
            key=lambda s: (priority[s.horizon], s.available_at, s.key),
        )
        for signal in eligible:
            key = signal.key
            entry_boundary = (
                signal.label_by if signal.holding_sessions is not None else signal.due_at
            )
            if (entry_boundary and entry_boundary <= now) or (
                signal.cancel_at and signal.cancel_at <= now
            ):
                states[key] = (
                    "INDEX_EXIT_BEFORE_ENTRY"
                    if signal.cancel_at and signal.cancel_at <= now
                    else "NO_ENTRY_BEFORE_DEADLINE"
                )
                continue
            if session_index - first_slot[key] >= policy.entry_sessions + execution_delay:
                states[key] = "ENTRY_WINDOW_EXPIRED"
                continue
            if fixed_entries is not None and (
                key not in fixed_entries or identity != fixed_entries[key][0]
            ):
                continue
            if not executable:
                status = (
                    "NO_OPENING_EVIDENCE"
                    if not receipt_ok or (quote.bid is None and not optimistic)
                    else "HALTED"
                )
                orders.append(
                    {
                        "signal_key": key,
                        "at": now,
                        "quote_id": identity,
                        "side": "BUY",
                        "status": status,
                    }
                )
                continue
            price = _price(quote, rule, policy, True, cost_multiplier)
            exceeds_chase = price > policy.max_entry_price
            if exceeds_chase:
                states[key] = "ETF_CHASE_LIMIT"
            if (
                exceeds_chase
                or price <= 0
                or (quote.lower_limit and price < quote.lower_limit)
                or (quote.upper_limit and price > quote.upper_limit)
            ):
                orders.append(
                    {
                        "signal_key": key,
                        "at": now,
                        "quote_id": identity,
                        "side": "BUY",
                        "status": "ETF_CHASE_LIMIT" if exceeds_chase else "PRICE_LIMIT",
                    }
                )
                continue
            requested = fixed_entries[key][1] if fixed_entries is not None else policy.quantity
            remaining_risk = policy.risk_budget - sum(positions[k].risk for k in owned)
            high, low = _lots(min(requested, capacity), rule.lot) // rule.lot, 0
            risk_per_share = price * policy.stop_pct
            exit_price = max(rule.tick, price * (1 - policy.stop_pct))
            while low < high:
                lots = (low + high + 1) // 2
                size = lots * rule.lot
                purchase = size * price
                commission = sum(_fees(purchase, rule, True, cost_multiplier).values())
                exit_fees = sum(_fees(size * exit_price, rule, False, cost_multiplier).values())
                if (
                    purchase + commission <= cash
                    and size * risk_per_share + commission + exit_fees <= remaining_risk
                ):
                    low = lots
                else:
                    high = lots - 1
            quantity = low * rule.lot
            if fixed_entries is not None and quantity != requested:
                states[key] = "PAIRED_ENTRY_UNAVAILABLE"
                continue
            if not quantity:
                orders.append(
                    {
                        "signal_key": key,
                        "at": now,
                        "quote_id": identity,
                        "side": "BUY",
                        "status": "NO_VOLUME" if not capacity else "CASH_OR_RISK_LIMIT",
                    }
                )
                continue
            if delayed_buys[key] < execution_delay:
                delayed_buys[key] += 1
                orders.append(
                    {
                        "signal_key": key,
                        "at": now,
                        "quote_id": identity,
                        "side": "BUY",
                        "status": "DELAY_STRESS",
                    }
                )
                continue
            cost = -transact(now, quote, rule, signal, "BUY", quantity, price, "INDEX_ACTIVATION")
            risk = (
                quantity * risk_per_share
                + cost
                - quantity * price
                + sum(_fees(quantity * exit_price, rule, False, cost_multiplier).values())
            )
            positions[key] = Position(
                signal,
                quantity,
                quantity,
                now,
                session_index + rule.sell_lag_sessions,
                price,
                cost,
                risk,
                price,
                last_holding_session=(
                    session_index + signal.holding_sessions - 1
                    if signal.holding_sessions is not None
                    else None
                ),
            )
            owned.add(key)
            states[key] = "FILLED" if requested == quantity else "PARTIALLY_FILLED"
            capacity -= quantity
    # Apply already received payments after the final mark without relabeling their receipt date.
    market_value = sum((positions[key].quantity * last_price for key in owned), ZERO)
    return {
        "policy": alternative,
        "opening_model": "OPEN_PROXY_WHEN_MISSING" if optimistic else "BID_ASK_ONLY",
        "same_bar_model": "TARGET_FIRST" if target_first else "STOP_FIRST",
        "orders": orders,
        "fills_and_actions": ledger,
        "trade_paths": [position.result(policy.progress_pct) for position in positions.values()],
        "signal_status": [
            {
                "signal_key": s.key,
                "origin_digest": s.origin_digest,
                "status": states.get(
                    s.key,
                    "PAIR_NOT_SELECTED"
                    if fixed_entries is not None and s.key not in fixed_entries
                    else "RIGHT_CENSORED"
                    if archive.cutoff < s.due_at
                    else "UNFILLED",
                ),
            }
            for s in signals
        ],
        "same_bar_conflicts": ambiguities,
        "nav": sorted(nav, key=lambda row: row["session"]),
        "final": {
            "cash": cash,
            "market_value": market_value,
            "receivables": receivables,
            "net_value": cash + market_value + receivables,
            "quantity": sum(positions[key].quantity for key in owned),
        },
    }


def matched_entries(book: dict) -> dict[str, tuple[str, int]]:
    return {
        row["signal_key"]: (row["quote_id"], row["quantity"])
        for row in book["fills_and_actions"]
        if row["side"] == "BUY"
    }
