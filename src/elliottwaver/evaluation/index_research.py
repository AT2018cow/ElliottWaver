"""Sealed index-to-ETF experiments with an explicit issuance denominator."""

import fcntl
from collections import Counter, defaultdict
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from hashlib import sha256
from importlib.metadata import version
from pathlib import Path
from random import Random
from zoneinfo import ZoneInfo

from elliottwaver.data.calendar import load_calendar
from elliottwaver.data.periods import available_pairs, bar_available
from elliottwaver.data.router import route_symbol
from elliottwaver.data.snapshots import SnapshotStore
from elliottwaver.errors import InputError
from elliottwaver.evaluation.forward import live_reports
from elliottwaver.evaluation.index_execution import (
    EXIT_POLICIES,
    matched_entries,
    replay_execution,
)
from elliottwaver.evaluation.index_plans import settle_index_track
from elliottwaver.index_intraday import ParentIssuance, verify_price_basis
from elliottwaver.schemas.index_execution import (
    ExecutionSignal,
    Split,
    ToolArchive,
    TradingProtocol,
)
from elliottwaver.schemas.index_plan import PlanObservation
from elliottwaver.util import atomic_write, canonical_json, digest, package_source_digest
from elliottwaver.visualization.brief_formatter import read_brief_report


def _fingerprint() -> dict:
    return {
        "source": package_source_digest(),
        "dependencies": {
            name: version(name)
            for name in ("pydantic", "pandas", "numpy", "pyarrow", "exchange-calendars")
        },
    }


def _protocol(path: Path) -> TradingProtocol:
    protocol = TradingProtocol.model_validate_json(path.read_bytes())
    instrument = route_symbol(protocol.mapping.index_symbol)
    if (
        instrument.asset_type != "INDEX"
        or instrument.canonical_symbol != protocol.mapping.index_symbol
    ):
        raise InputError("Research protocol requires one canonical primary index")
    expected_market = "US" if protocol.mapping.venue == "XNYS" else "CN"
    if instrument.market != expected_market:
        raise InputError("Index and ETF must use the same market and currency in this protocol")
    return protocol


def seal_trading_protocol(path: Path, output_dir: Path, *, now: datetime | None = None) -> dict:
    protocol = _protocol(path)
    sealed_at = now or datetime.now(UTC)
    if protocol.mode != "FORWARD" or sealed_at >= protocol.grid[0].cutoff:
        raise InputError("A forward execution protocol must be sealed before its first decision")
    seal = {
        "protocol_hash": digest(protocol),
        "environment": _fingerprint(),
        "sealed_at": sealed_at,
    }
    output_dir.mkdir(parents=True, exist_ok=True)
    with (output_dir / "seal.json").open("x") as stream:
        stream.write(canonical_json({**seal, "seal_id": digest(seal)}))
    return {**seal, "sealed_at": sealed_at.isoformat(), "seal_id": digest(seal)}


def _validate_seal(protocol, path, now):
    from json import loads

    if path is None:
        raise InputError("Forward execution requires the original seal")
    receipt = loads(path.read_text())
    identity = receipt.pop("seal_id", None)
    if (
        identity != digest(receipt)
        or receipt.get("protocol_hash") != digest(protocol)
        or receipt.get("environment") != _fingerprint()
    ):
        raise InputError("Forward execution seal does not match the protocol and environment")
    issued = datetime.fromisoformat(receipt["sealed_at"])
    if issued > now or issued >= protocol.grid[0].cutoff:
        raise InputError("Forward execution seal postdates its decision grid")
    return identity


def _archives(directory: Path, protocol: TradingProtocol, *, forward: bool):
    if forward:
        return {
            (report.brief.timeframe, report.brief.market_asof, report.brief.brief_digest): (
                report.brief,
                report.readable_at,
            )
            for report in live_reports(directory, allow_empty=True).values()
            if report.brief.symbol == protocol.mapping.index_symbol
        }
    archives = {}
    for body in sorted(directory.rglob("brief.json")):
        if any(p.startswith(".") for p in body.relative_to(directory).parts[:-1]):
            continue
        brief, _ = read_brief_report(body.parent)
        if (
            brief.symbol != protocol.mapping.index_symbol
            or brief.mode != "FROZEN_DATA_REPLAY"
            or brief.retrospective
        ):
            continue
        at = brief.outlook.issued_at if brief.outlook else brief.as_of
        archives[(brief.timeframe, brief.market_asof, brief.brief_digest)] = (brief, at)
    return archives


def _index_excursion(plan, price, triggered_at, limit, market_bars, evidence_cutoff, source):
    if not price:
        return None
    eligible = [
        (bar, raw, received)
        for bar, raw, received in market_bars.values()
        if triggered_at <= bar.start_at
        and bar.end_at <= min(limit, plan.periods[-1].end_at)
        and received <= evidence_cutoff
    ]
    if not eligible:
        return None
    high, raw_high, high_received = max(eligible, key=lambda row: row[0].high)
    low, raw_low, low_received = min(eligible, key=lambda row: row[0].low)
    return {
        "anchor_index_points": price,
        "favorable_points": max(0, high.high - price),
        "adverse_points": max(0, price - low.low),
        "high_bar_id": raw_high.bar_id,
        "low_bar_id": raw_low.bar_id,
        "high_received_at": high_received,
        "low_received_at": low_received,
        "observed_bars": len(eligible),
        "through_snapshot": source.snapshot_id if source else None,
    }


def _next_split_cutoffs(grid):
    starts = {}
    for slot in grid:
        starts.setdefault(slot.split, slot.cutoff)
    return dict(zip(starts, (*tuple(starts.values())[1:], None), strict=True))


def _check_intraday_witness(plan, parent, issued, scan, received, track, store, *, live):
    context = scan.index_context.intraday
    witness = next((item for item in context.inputs if item.timeframe == "15m"), None)
    if witness is None or scan.input_snapshot_id != witness.snapshot_id:
        raise InputError("Intraday receipt does not identify its preserved 15m snapshot")
    child = store.load(witness.snapshot_id)
    if (
        child.timeframe != "15m"
        or child.instrument.canonical_symbol != scan.symbol
        or child.provenance.feed != scan.feed
        or child.provenance.adjustment != scan.adjustment
        or child.bars[-1].bar_id != scan.last_bar_id
        or child.raw_bars[-1].bar_id != witness.last_bar_id
        or child.bars[-1].end_at != witness.last_bar_end_at
        or bar_available(child.bars[-1], child.raw_bars[-1], live) != witness.last_bar_usable_at
        or witness.last_bar_usable_at > received
    ):
        raise InputError("Intraday receipt does not match its preserved 15m evidence")
    daily = store.load(plan.input_snapshot_id)
    price_basis = verify_price_basis(
        plan,
        ParentIssuance(parent.brief_digest, issued, daily),
        child,
        observed_cutoff=received,
    )
    if price_basis != (track.price_basis, track.basis_sessions, track.basis_observable_at):
        raise InputError("Intraday receipt does not match its recorded cross-feed proof")
    pairs = {raw.bar_id: (bar, raw) for bar, raw in zip(child.bars, child.raw_bars, strict=True)}
    for item in track.variants:
        if item.status != "TRIGGERED":
            continue
        pair = pairs.get(item.raw_bar_id)
        if (
            pair is None
            or pair[1].end_at != item.bar_end_at
            or item.observable_at is None
            or not bar_available(*pair, live) <= item.observable_at <= received
        ):
            raise InputError("Intraday trigger has no matching contemporaneously preserved bar")
    return True


def _signal(
    plan, identity, origin, observed_at, observation, report_time, cancellation, label_limit
):
    if observation.activation_at is None or observation.due_at is None:
        return None
    ready = max(plan.issued_at, report_time, observed_at, observation.activation_at)
    if ready > plan.activation_deadline or ready >= observation.due_at:
        return None
    if cancellation is not None and cancellation <= ready:
        return None
    return ExecutionSignal(
        key=identity,
        evidence_id=observation.activation_bar_id or identity,
        origin_digest=origin,
        horizon=plan.horizon,
        available_at=ready,
        due_at=observation.due_at,
        cancel_at=cancellation,
        holding_sessions=plan.holding_sessions,
        label_by=label_limit,
    )


def _register_plans(
    grid, archives, source, index_sessions, protocol, *, data_cutoff, snapshot_store
):
    observed = defaultdict(list)
    intraday = defaultdict(list)
    policy_rejected = set()
    for (timeframe, session, _), (brief, received) in archives.items():
        if brief.adjustment != protocol.index_adjustment or received > data_cutoff:
            continue
        if timeframe == "1d" and brief.feed == protocol.index_feed:
            if (
                protocol.analysis_config_hash and brief.config_hash != protocol.analysis_config_hash
            ) or (
                protocol.guidance_policy_hash
                and (
                    brief.index_guidance is None
                    or brief.index_guidance.policy_hash != protocol.guidance_policy_hash
                )
            ):
                policy_rejected.add(session)
                continue
            observed[session].append((brief, received))
        elif (
            timeframe == "15m"
            and brief.index_context
            and brief.index_context.intraday
            and brief.feed == (protocol.intraday_feed or protocol.index_feed)
            and (protocol.mode != "FORWARD" or protocol.intraday_feed is not None)
            and (
                not protocol.analysis_config_hash
                or brief.config_hash == protocol.analysis_config_hash
            )
        ):
            for track in brief.index_context.intraday.tracks:
                if track.parent_brief_digest and track.plan_id:
                    intraday[(track.parent_brief_digest, track.plan_id, track.revision)].append(
                        (brief, received, track)
                    )

    market_bars = {}
    if source is not None:
        market_bars = {
            bar.session_date: (bar, raw, max(bar.usable_at, raw.usable_at))
            for bar, raw in available_pairs(source.bars, source.raw_bars, data_cutoff)
        }
    else:
        for session, items in observed.items():
            for brief, received in sorted(items, key=lambda item: item[1]):
                prepared = snapshot_store.load(brief.input_snapshot_id)
                if (
                    prepared.timeframe != "1d"
                    or prepared.instrument.canonical_symbol != brief.symbol
                    or prepared.provenance.feed != brief.feed
                    or prepared.provenance.adjustment != brief.adjustment
                ):
                    raise InputError("Live receipt does not match its preserved input scope")
                bar, raw = prepared.bars[-1], prepared.raw_bars[-1]
                if (
                    bar.session_date != session
                    or bar.bar_id != brief.last_bar_id
                    or max(bar.observed_at, raw.observed_at, bar.usable_at, raw.usable_at)
                    > received
                ):
                    raise InputError("Live receipt has no contemporaneously preserved index bar")
                market_bars.setdefault(session, (bar, raw, received))
    published_transitions = defaultdict(list)
    for items in observed.values():
        for newer, received in items:
            if newer.index_guidance is None:
                continue
            for current in newer.index_guidance.tracks:
                for transition in current.transitions:
                    if transition.to_state in {
                        "CANCELLED",
                        "INVALIDATED",
                        "STAGE_EXIT",
                        "TARGET_REACHED",
                        "AMBIGUOUS",
                    }:
                        published_transitions[(transition.plan_id, transition.revision)].append(
                            max(received, transition.observable_at)
                        )

    next_split = _next_split_cutoffs(protocol.grid)
    verified_scans = set()
    outcomes, plan_rows = [], []
    by_split = defaultdict(lambda: defaultdict(list))
    for slot in grid:
        timely = [
            (brief, received)
            for brief, received in observed[slot.session]
            if slot.cutoff <= brief.as_of <= received <= slot.issue_by
        ]
        if len(timely) > 1:
            raise InputError(f"Duplicate in-grid decision for {slot.session}")
        status = (
            "AVAILABLE"
            if timely
            else "POLICY_MISMATCH"
            if slot.session in policy_rejected
            else "OUTSIDE_WINDOW"
            if observed[slot.session]
            else "MISSED"
        )
        crosses = next_split[slot.split] is not None and slot.label_by >= next_split[slot.split]
        mature = not crosses and slot.label_by <= data_cutoff
        row = {
            "session": slot.session,
            "split": slot.split,
            "cutoff": slot.cutoff,
            "label_by": slot.label_by,
            "mature": mature,
            "status": status,
            "purged": crosses,
            "brief_digest": timely[0][0].brief_digest if timely else None,
        }
        outcomes.append(row)
        if not timely or crosses:
            continue
        brief, issued = timely[0]
        row["received_at"] = issued
        following = [s for s in index_sessions if s.open_at > issued]
        if len(following) < protocol.baseline_holding_sessions:
            raise InputError("Exchange calendar cannot resolve baseline due period")
        baseline_due = following[protocol.baseline_holding_sessions - 1].close_at
        if baseline_due > slot.label_by:
            row["baseline_status"] = "CROSSES_LABEL_WINDOW"
        else:
            row["baseline_status"] = "AVAILABLE"
            baseline = {
                "origin_digest": brief.brief_digest,
                "evidence_id": brief.last_bar_id or brief.brief_digest,
                "horizon": "SHORT",
                "available_at": issued,
                "due_at": baseline_due,
                "holding_sessions": protocol.baseline_holding_sessions,
                "label_by": slot.label_by,
            }
            if not by_split[slot.split]["BUY_HOLD"]:
                by_split[slot.split]["BUY_HOLD"].append(
                    ExecutionSignal(
                        **{
                            **baseline,
                            "due_at": max(s.label_by for s in grid if s.split == slot.split),
                            "holding_sessions": None,
                        },
                        key=f"BUY_HOLD:{slot.split}",
                    )
                )
            baseline_source = snapshot_store.load(brief.input_snapshot_id)
            if (
                baseline_source.instrument.canonical_symbol != brief.symbol
                or baseline_source.provenance.feed != brief.feed
                or baseline_source.timeframe != "1d"
                or baseline_source.provenance.adjustment != brief.adjustment
            ):
                raise InputError("Issued index snapshot does not match the baseline price scope")
            if baseline_source is not None:
                seen = [
                    bar
                    for bar, raw in available_pairs(
                        baseline_source.bars, baseline_source.raw_bars, brief.as_of
                    )
                    if bar.session_date <= slot.session
                    and (source is not None or max(bar.observed_at, raw.observed_at) <= issued)
                ]
                if (
                    seen
                    and seen[-1].session_date == slot.session
                    and seen[-1].bar_id == brief.last_bar_id
                    and len(seen) > protocol.momentum_sessions
                ):
                    previous = seen[-protocol.momentum_sessions - 1 : -1]
                    for arm, threshold in (
                        ("MOMENTUM", previous[0].close),
                        ("BREAKOUT", max(bar.high for bar in previous)),
                    ):
                        if seen[-1].close > threshold:
                            by_split[slot.split][arm].append(
                                ExecutionSignal(**baseline, key=f"{arm}:{brief.brief_digest}")
                            )
        guidance = brief.index_guidance
        row["issued_plans"] = 0
        if guidance is None:
            continue
        for track in guidance.tracks:
            plan = track.plan
            if (
                not plan
                or plan.decision_session != slot.session
                or not any(
                    t.reason == "PLAN_ISSUED"
                    and (t.plan_id, t.revision) == (plan.plan_id, plan.revision)
                    for t in track.transitions
                )
            ):
                continue
            identity = digest(plan)
            row["issued_plans"] += 1
            plan_row = {
                "plan_key": identity,
                "plan_id": plan.plan_id,
                "revision": plan.revision,
                "brief_digest": brief.brief_digest,
                "session": slot.session,
                "horizon": plan.horizon,
                "split": slot.split,
                "status": "WAITING",
            }
            plan_rows.append(plan_row)
            if (
                (
                    protocol.analysis_config_hash
                    and plan.analysis_config_hash != protocol.analysis_config_hash
                )
                or (
                    protocol.guidance_policy_hash
                    and plan.policy_hash != protocol.guidance_policy_hash
                )
                or (
                    protocol.intraday_policy is not None
                    and plan.intraday_policy != protocol.intraday_policy
                )
            ):
                plan_row["status"] = "POLICY_MISMATCH"
                continue
            related = [
                (received, current)
                for candidates in observed.values()
                for newer, received in candidates
                if issued <= received <= slot.label_by and newer.index_guidance
                for current in newer.index_guidance.tracks
                if current.plan == plan
            ]
            cancelled = [
                at
                for at in published_transitions[(plan.plan_id, plan.revision)]
                if issued <= at <= slot.label_by
            ]
            # Purge the newest possible plan label and its availability at the split boundary.
            if plan.periods[-1].end_at > slot.label_by:
                plan_row["status"] = "PLAN_WINDOW_CROSSES_LABEL"
                continue
            if source is not None:
                scoped = source.model_copy(update={"as_of": min(source.as_of, slot.label_by)})
                settled = settle_index_track(track, scoped)
                observation = PlanObservation.model_validate(settled["observation"])
                observed_at = observation.activation_at
                witnessed_gap = observation.quality == "DATA_PENDING" and any(
                    item["to_state"] == "TRIGGERED"
                    and item["source_bar_id"] == observation.activation_bar_id
                    for item in settled["transitions"]
                )
                cancelled.extend(
                    datetime.fromisoformat(t["observable_at"])
                    for t in settled["transitions"]
                    if t["to_state"]
                    in {"CANCELLED", "INVALIDATED", "STAGE_EXIT", "TARGET_REACHED", "AMBIGUOUS"}
                )
            else:
                witnessed_gap = False
                hits = [
                    (received, item.observation)
                    for received, item in related
                    if item.observation.quality == "OK"
                    and item.observation.activation_at is not None
                ]
                observed_at, observation = (
                    min(hits, key=lambda item: item[0]) if hits else (None, track.observation)
                )
            cancel_at = min(cancelled, default=None)
            if observed_at is not None and (observation.quality == "OK" or witnessed_gap):
                candidate = _signal(
                    plan,
                    identity,
                    brief.brief_digest,
                    observed_at,
                    observation,
                    issued,
                    cancel_at,
                    slot.label_by,
                )
                if candidate:
                    by_split[slot.split]["FULL"].append(candidate)
                    trend = brief.index_context.trend if brief.index_context else None
                    if trend is not None and trend.return_pct is not None:
                        plan_row["trend_filter"] = "PASS" if trend.return_pct >= 0 else "FILTERED"
                        if trend.return_pct >= 0:
                            by_split[slot.split]["FULL_TREND_FILTER"].append(candidate)
                    else:
                        plan_row["trend_filter"] = "UNAVAILABLE"
                    plan_row.update(
                        status="ACTIVATED_FOLLOW_UP_PENDING" if witnessed_gap else "ACTIVATED",
                        observation=observation,
                        index_excursion=_index_excursion(
                            plan,
                            observation.activation_price,
                            candidate.available_at,
                            min(candidate.due_at, cancel_at or candidate.due_at),
                            market_bars,
                            min(slot.label_by, data_cutoff),
                            source,
                        ),
                    )
            plan_row["observation"] = observation
            if plan_row["status"] == "WAITING":
                plan_row["status"] = (
                    observation.quality
                    if observation.quality != "OK"
                    else (
                        "UNOBSERVABLE_ACTIVATION"
                        if observation.activation_at
                        else observation.state
                    )
                )

            # Price-only crossing of the issued geometry, on the same complete prefix.
            prefix = issued
            for i, period in enumerate(plan.periods[: plan.activation_sessions]):
                evidence = market_bars.get(period.end_session)
                if evidence is None:
                    plan_row["geometry_status"] = "MISSING_BAR"
                    break
                bar, raw, availability = evidence
                prefix = max(prefix, availability)
                if prefix > plan.activation_deadline:
                    break
                if plan.trigger.price < bar.close <= plan.max_entry_index_price:
                    by_split[slot.split]["GEOMETRY"].append(
                        ExecutionSignal(
                            key=identity,
                            origin_digest=brief.brief_digest,
                            evidence_id=raw.bar_id,
                            horizon=plan.horizon,
                            available_at=prefix,
                            due_at=plan.periods[i + plan.holding_sessions - 1].end_at,
                            holding_sessions=plan.holding_sessions,
                            label_by=slot.label_by,
                        )
                    )
                    break

            candidates = intraday[(brief.brief_digest, plan.plan_id, plan.revision)]
            for method in ("TOUCH", "CLOSE", "RETEST"):
                choices = [
                    (received, item, scan, t)
                    for scan, received, t in candidates
                    for item in t.variants
                    if item.method == method
                    and t.price_basis
                    == ("SAME_FEED" if scan.feed == plan.feed else "CROSS_FEED_VERIFIED")
                    and item.status == "TRIGGERED"
                    and t.status
                    not in {"PARENT_EXPIRED", "PARENT_INVALIDATED", "AMBIGUOUS", "MISSING_15M_SLOT"}
                    and item.observable_at is not None
                    and item.bar_end_at is not None
                    and max(received, item.observable_at) <= plan.activation_deadline
                    and (
                        source is not None
                        or (
                            plan.intraday_policy is not None
                            and received - item.bar_end_at
                            <= timedelta(minutes=plan.intraday_policy.max_age_minutes)
                        )
                    )
                ]
                if choices:
                    received, item, scan, intraday_track = min(
                        choices, key=lambda x: max(x[0], x[1].observable_at)
                    )
                    witness_key = (identity, scan.brief_digest)
                    if (
                        source is None or scan.feed != plan.feed
                    ) and witness_key not in verified_scans:
                        _check_intraday_witness(
                            plan,
                            brief,
                            issued,
                            scan,
                            received,
                            intraday_track,
                            snapshot_store,
                            live=source is None,
                        )
                        verified_scans.add(witness_key)
                    period = next(
                        (
                            i
                            for i, p in enumerate(plan.periods)
                            if p.start_at < item.bar_end_at <= p.end_at
                        ),
                        None,
                    )
                    ready = max(issued, received, item.observable_at)
                    if period is not None and (cancel_at is None or cancel_at > ready):
                        by_split[slot.split][f"INTRADAY_{method}"].append(
                            ExecutionSignal(
                                key=identity,
                                origin_digest=brief.brief_digest,
                                evidence_id=scan.brief_digest,
                                horizon=plan.horizon,
                                available_at=ready,
                                cancel_at=cancel_at,
                                due_at=plan.periods[period + plan.holding_sessions - 1].end_at,
                                holding_sessions=plan.holding_sessions,
                                label_by=slot.label_by,
                            )
                        )
    return outcomes, plan_rows, by_split


def _block_interval(book, comparison, sessions, protocol):
    def returns(ledger):
        marks = {point["session"]: point["net_value"] for point in ledger["nav"]}
        result, previous = [], protocol.settings.capital
        for day in sessions:
            current = marks.get(day, previous)
            result.append(float(current / previous - 1) if previous > 0 else 0.0)
            previous = current
        return result

    paired = [a - b for a, b in zip(returns(book), returns(comparison), strict=True)]
    width = protocol.block_sessions
    if len(paired) < 2 * width:
        return None
    random = Random(protocol.bootstrap_seed)
    means = []
    for _ in range(protocol.bootstrap_samples):
        total = 0.0
        for offset in range(0, len(paired), width):
            start = random.randrange(len(paired))
            total += sum(
                paired[(start + step) % len(paired)]
                for step in range(min(width, len(paired) - offset))
            )
        means.append(total / len(paired))
    means.sort()
    return [means[int((len(means) - 1) * p)] for p in (0.025, 0.975)]


def _book_summary(book, capital):
    peak = capital
    drawdown = Decimal(0)
    funds = [row["net_value"] for row in book["nav"]] + [book["final"]["net_value"]]
    for current in funds:
        peak = max(peak, current)
        drawdown = max(drawdown, 1 - current / peak)
    rows = book["trade_paths"]
    pnl = sorted(row["net_pnl"] for row in rows if row["net_pnl"] is not None)
    return {
        "positions": len(rows),
        "completed": len(pnl),
        "unclosed": len(rows) - len(pnl),
        "net_return": book["final"]["net_value"] / capital - 1,
        "max_drawdown": drawdown,
        "worst_closed_pnl": pnl[0] if pnl else None,
        "turnover": sum((row["turnover"] for row in rows), Decimal(0)),
        "same_bar_conflicts": len(book["same_bar_conflicts"]),
        "same_bar_conflict_rate": (
            len({row["signal_key"] for row in book["same_bar_conflicts"]}) / len(rows)
            if rows
            else None
        ),
        "signal_status": dict(Counter(row["status"] for row in book["signal_status"])),
    }


def _paired_exits(base, alternate):
    first = {t["signal_key"]: t for t in base["trade_paths"]}
    second = {t["signal_key"]: t for t in alternate["trade_paths"]}
    pairs = []
    for key in first.keys() & second.keys():
        a, b = first[key], second[key]
        if (a["entry_price"], a["entry_at"], a["quantity"]) != (
            b["entry_price"],
            b["entry_at"],
            b["quantity"],
        ):
            continue
        pairs.append(
            {
                "signal_key": key,
                "base_pnl": a["net_pnl"],
                "alternate_pnl": b["net_pnl"],
                "difference": b["net_pnl"] - a["net_pnl"]
                if a["net_pnl"] is not None and b["net_pnl"] is not None
                else None,
                "base_holding_seconds": a["holding_seconds"],
                "alternate_holding_seconds": b["holding_seconds"],
            }
        )
    return {
        "matched": len(pairs),
        "unmatched": len(first) - len(pairs),
        "pairs": sorted(pairs, key=lambda row: row["signal_key"]),
    }


def evaluate_index_trading(
    protocol_path: Path,
    output_dir: Path,
    *,
    report_directory: Path,
    snapshot_directory: Path | None = None,
    tool_archive_path: Path | None = None,
    seal_path: Path | None = None,
    only_split: Split | None = None,
    now: datetime | None = None,
) -> dict:
    protocol = _protocol(protocol_path)
    now = now or datetime.now(UTC)
    forward = protocol.mode == "FORWARD"
    seal_id = _validate_seal(protocol, seal_path, now) if forward else None
    grid = tuple(slot for slot in protocol.grid if not only_split or slot.split == only_split)
    if not grid:
        raise InputError("The selected execution split has no registered issuance slots")
    boundary = next((slot.cutoff for slot in protocol.grid if slot.cutoff > grid[-1].cutoff), None)
    index = route_symbol(protocol.mapping.index_symbol)
    if not report_directory.is_dir():
        raise InputError("A directory of issued index reports is required")
    received = _archives(report_directory, protocol, forward=forward)
    store = SnapshotStore(snapshot_directory) if snapshot_directory else None
    archive = source = None
    tool_hash = None
    if not forward:
        if store is None:
            raise InputError("Historical execution research requires the frozen snapshot store")
        source = store.load(protocol.index_snapshot_id)
        if (
            source.instrument.canonical_symbol != index.canonical_symbol
            or source.timeframe != "1d"
            or source.provenance.feed != protocol.index_feed
            or source.provenance.adjustment != protocol.index_adjustment
        ):
            raise InputError("Index settlement snapshot does not match the frozen index scope")
    if tool_archive_path:
        content = tool_archive_path.read_bytes()
        tool_hash = sha256(content).hexdigest()
        archive = ToolArchive.model_validate_json(content)
        if archive.mapping != protocol.mapping:
            raise InputError("ETF archive does not match the frozen mapping")
        if forward and (archive.evidence != "RECEIVED" or archive.cutoff > now):
            raise InputError("Forward execution requires contemporaneously received ETF evidence")
        if not forward and tool_hash != protocol.tool_archive_sha256:
            raise InputError("Historical ETF archive is missing its matching frozen digest")

    if not tool_archive_path and protocol.tool_archive_sha256:
        raise InputError("Frozen protocol requires the declared ETF archive")
    data_cutoff = (
        min(now, archive.cutoff) if forward and archive else now if forward else source.as_of
    )
    if boundary is not None:
        data_cutoff = min(data_cutoff, boundary - timedelta(microseconds=1))
        received = {key: item for key, item in received.items() if item[1] <= data_cutoff}
    lower = grid[0].session
    upper = max(slot.label_by for slot in grid).astimezone(ZoneInfo(index.timezone)).date()
    calendar = load_calendar(index, lower, upper)
    sessions = {s.date: s for s in calendar.sessions}
    if any(
        row.session not in sessions or row.cutoff < sessions[row.session].close_at for row in grid
    ):
        raise InputError("Daily grid must identify completed, scheduled index sessions")
    if forward and store is None:
        raise InputError("Forward execution requires the preserved snapshots from issued reports")
    outcomes, plan_rows, candidates = _register_plans(
        grid,
        received,
        source,
        calendar.sessions,
        protocol,
        data_cutoff=data_cutoff,
        snapshot_store=store,
    )
    outputs = {}
    summary = {
        "mode": "SEALED_FORWARD" if forward else "FROZEN_DATA_REPLAY",
        "scored_split": only_split or "ALL",
        "signal_basis": (
            "VERIFIED_REPORT_RECEIPTS" if forward else "FROZEN_DAILY_AND_INTRADAY_ARCHIVES"
        ),
        "index": index.canonical_symbol,
        "tool": protocol.mapping.tool_symbol if archive else None,
        "tool_evidence": archive.evidence if archive else "UNAVAILABLE",
        "status": "INDEX_ONLY" if archive is None else "INSUFFICIENT_EVIDENCE",
        "splits": {},
    }
    if archive:
        tool_sessions = load_calendar(index, archive.quotes[0].session, archive.quotes[-1].session)
        tool_calendar = {row.date: row for row in tool_sessions.sessions}
        if any(
            quote.session not in tool_calendar
            or (quote.start_at, quote.end_at)
            != (
                tool_calendar[quote.session].open_at,
                tool_calendar[quote.session].close_at,
            )
            for quote in archive.quotes
        ):
            raise InputError("ETF bars must match their exchange's full-session endpoints")
        if any(
            any(quote.start_at < action.effective_at < quote.end_at for quote in archive.quotes)
            for action in archive.actions
        ):
            raise InputError(
                "Daily execution cannot represent a corporate action inside a tool bar"
            )
        opening_grid = tuple((row.date, row.open_at) for row in tool_sessions.sessions)
        split_cutoffs = _next_split_cutoffs(protocol.grid)
        for split in ("DEVELOPMENT", "CALIBRATION", "HOLDOUT"):
            slots = [row for row in grid if row.split == split]
            if not slots:
                continue
            start, end = (
                slots[0].cutoff,
                min(archive.cutoff, data_cutoff, max(s.label_by for s in slots)),
            )
            if split_cutoffs[split] is not None:
                end = min(end, split_cutoffs[split] - timedelta(microseconds=1))
            quotes = tuple(
                q for q in archive.quotes if start <= q.start_at and q.observed_at <= end
            )
            active = [
                row.date
                for row in calendar.sessions
                if start <= row.open_at and row.close_at <= end
            ]
            missing = sorted(set(active) - {q.session for q in quotes})
            if not quotes:
                summary["splits"][split] = {"status": "NO_TOOL_WINDOW", "missing_sessions": missing}
                continue
            window = archive.model_copy(update={"quotes": quotes, "cutoff": end})
            arms = candidates[split]
            run_options = {"enforce_receipts": forward, "session_opens": opening_grid}

            def run(arm, window=window, arms=arms, run_options=run_options, **options):
                settings = protocol.settings
                if arm == "BUY_HOLD":
                    settings = settings.model_copy(update={"risk_budget": settings.capital})
                return replay_execution(
                    window, protocol.rules, settings, arms[arm], **run_options, **options
                )

            names = (
                "CASH",
                "BUY_HOLD",
                "MOMENTUM",
                "BREAKOUT",
                "GEOMETRY",
                "FULL",
                "FULL_TREND_FILTER",
                "INTRADAY_TOUCH",
                "INTRADAY_CLOSE",
                "INTRADAY_RETEST",
            )
            books = {name: run(name, hold_only=name == "BUY_HOLD") for name in names}
            base = books["FULL"]
            for horizon in protocol.settings.allocation_priority:
                arms[f"FULL_{horizon}"] = [s for s in arms["FULL"] if s.horizon == horizon]
                books[f"FULL_{horizon}"] = run(f"FULL_{horizon}")
            entries = matched_entries(base)
            books["FULL_DUAL_COST"] = run("FULL", cost_multiplier=2)
            books["FULL_DELAYED"] = run("FULL", execution_delay=1)
            books["FULL_OPEN_PROXY"] = run("FULL", optimistic=True)
            books["FULL_TARGET_FIRST"] = run("FULL", target_first=True)
            books["BUY_HOLD_OPEN_PROXY"] = run("BUY_HOLD", optimistic=True, hold_only=True)
            for alternative in EXIT_POLICIES[1:]:
                books[f"EXIT_{alternative}"] = run(
                    "FULL", alternative=alternative, fixed_entries=entries
                )
            pairs = {
                name: _paired_exits(base, book)
                for name, book in books.items()
                if name.startswith("EXIT_")
            }
            outputs[split] = books
            metrics = {
                name: _book_summary(book, protocol.settings.capital) for name, book in books.items()
            }
            comparisons = {
                name: _block_interval(base, books[name], active, protocol)
                for name in ("CASH", "BUY_HOLD", "MOMENTUM", "BREAKOUT", "GEOMETRY")
            }
            relevant = [row for row in outcomes if row["split"] == split and not row["purged"]]
            holdout_counts = Counter(row["status"] for row in relevant)
            time_coverage = holdout_counts["AVAILABLE"] / len(relevant) if relevant else 0
            coverage = not missing and (
                not forward
                or all(
                    not q.tradable
                    or (
                        q.opening_observed_at == q.start_at
                        and q.bid
                        and q.opening_volume is not None
                    )
                    for q in quotes
                )
            )
            relevant_plan_sessions = {p["session"] for p in plan_rows if p["split"] == split}
            label_lengths = [
                sum(
                    slot.session < day and sessions[day].close_at <= slot.label_by for day in active
                )
                for slot in slots
                if slot.session in relevant_plan_sessions
            ]
            longest = max(
                [
                    protocol.baseline_holding_sessions + protocol.settings.entry_sessions,
                    *label_lengths,
                ]
            )
            reasons = []
            if split != "HOLDOUT":
                reasons.append("DEVELOPMENT_OR_CALIBRATION")
            if archive.evidence == "SYNTHETIC":
                reasons.append("SYNTHETIC_EVIDENCE")
            if (
                len({r["session"] for r in relevant if r["mature"]}) < protocol.min_holdout_sessions
                or metrics["FULL"]["completed"] < protocol.min_holdout_trades
            ):
                reasons.append("INSUFFICIENT_MATURE_SAMPLE")
            if (
                not coverage
                or time_coverage < protocol.min_timely_fraction
                or not all(row["mature"] and row["label_by"] <= archive.cutoff for row in relevant)
            ):
                reasons.append("INCOMPLETE_ISSUANCE_OR_TOOL_EVIDENCE")
            if any(
                row["status"]
                in {
                    "DATA_PENDING",
                    "ACTIVATED_FOLLOW_UP_PENDING",
                    "DATA_REVISED",
                    "NOT_COMPARABLE",
                    "PLAN_WINDOW_CROSSES_LABEL",
                    "POLICY_MISMATCH",
                }
                for row in plan_rows
                if row["split"] == split
            ):
                reasons.append("UNRESOLVED_PLAN_EVIDENCE")
            if protocol.block_sessions < longest or any(
                value is None or value[0] <= float(protocol.min_net_increment)
                for value in comparisons.values()
            ):
                reasons.append("PAIRED_INCREMENT_NOT_ESTABLISHED")
            if (
                metrics["FULL"]["max_drawdown"] > protocol.max_drawdown
                or any(
                    metrics[name]["net_return"] <= 0 for name in ("FULL_DUAL_COST", "FULL_DELAYED")
                )
                or metrics["FULL"]["unclosed"]
            ):
                reasons.append("RISK_OR_STRESS_GATE")
            status = (
                "INSUFFICIENT_EVIDENCE"
                if reasons
                else ("FORWARD_SHADOW_THRESHOLDS_MET" if forward else "HISTORICAL_THRESHOLDS_MET")
            )
            if split == "HOLDOUT":
                summary["status"] = status
            summary["splits"][split] = {
                "status": status,
                "reasons": reasons,
                "issuances": dict(holdout_counts),
                "timely_fraction": time_coverage,
                "missing_sessions": missing,
                "metrics": metrics,
                "paired_exits": pairs,
                "paired_daily_return_ci95": comparisons,
            }
    summary["issuance_counts"] = dict(Counter(row["status"] for row in outcomes))
    summary["purged_issuances"] = sum(row["purged"] for row in outcomes)
    summary["plan_counts"] = dict(Counter(row["status"] for row in plan_rows))
    identity = {
        "protocol_hash": digest(protocol),
        "environment": _fingerprint(),
        "seal_id": seal_id,
        "scored_split": only_split or "ALL",
        "index_snapshot": source.snapshot_id if source else None,
        "tool_archive_sha256": tool_hash,
        "data_cutoff": data_cutoff,
        "reports": sorted(
            (brief.brief_digest, available) for brief, available in received.values()
        ),
        "calendar": calendar.version,
    }
    summary["experiment_id"] = digest(identity)
    artifacts = {
        "issuance_grid": outcomes,
        "plans": plan_rows,
        "books": outputs,
        "summary": summary,
    }
    output_dir.mkdir(parents=True, exist_ok=True)
    with (output_dir / ".execution.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        bodies = {f"{name}.json": canonical_json(value) for name, value in artifacts.items()}
        for name, body in bodies.items():
            atomic_write(output_dir / name, body)
        hashes = {name: sha256(body.encode()).hexdigest() for name, body in bodies.items()}
        atomic_write(output_dir / "manifest.json", canonical_json({**identity, "files": hashes}))
    return summary
