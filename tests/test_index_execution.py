import json
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from hashlib import sha256
from pathlib import Path
from subprocess import run
from sys import executable

import pytest

from elliottwaver.brief import build_brief
from elliottwaver.config import Settings
from elliottwaver.data.calendar import load_calendar
from elliottwaver.data.demo import demo_bundle
from elliottwaver.data.snapshots import SnapshotStore
from elliottwaver.errors import InputError
from elliottwaver.evaluation.index_execution import matched_entries, replay_execution
from elliottwaver.evaluation.index_research import evaluate_index_trading, seal_trading_protocol
from elliottwaver.schemas.index_execution import (
    ExecutionPolicy,
    ExecutionSignal,
    FeeSchedule,
    IssuanceSlot,
    ToolAction,
    ToolArchive,
    ToolMapping,
    ToolQuote,
    TradingProtocol,
)
from elliottwaver.schemas.market import AnalysisRequest
from elliottwaver.visualization.brief_formatter import publish_brief

D = Decimal
MAPPING = ToolMapping(
    index_symbol="SPX",
    tool_symbol="SPY",
    version="fixture-1",
    venue="XNYS",
    timezone="America/New_York",
    currency="USD",
    feed="fixture",
)
RULE = FeeSchedule(
    version="fixture-1",
    effective_from=date(2024, 1, 1),
    tick="0.01",
    lot=1,
    sell_lag_sessions=1,
    minimum_commission=2,
    sell_tax_bps="4.7619",
)
POLICY = ExecutionPolicy(
    capital=10000,
    quantity=100,
    risk_budget=10000,
    max_entry_price=150,
    stop_pct="0.08",
    target_pct="0.04",
    max_open_participation=1,
)


def quote(day, op, high, low, close, **overrides):
    start = datetime(2024, 9, 9, 13, 30, tzinfo=UTC) + timedelta(days=day)
    fields = dict(
        quote_id=f"q{day}",
        session=start.date(),
        start_at=start,
        end_at=start + timedelta(hours=6, minutes=30),
        observed_at=start + timedelta(hours=6, minutes=30),
        opening_observed_at=start,
        open=op,
        high=high,
        low=low,
        close=close,
        bid=op,
        ask=op,
        opening_volume=10000,
    )
    return ToolQuote(**{**fields, **overrides})


def archive(quotes, *actions):
    return ToolArchive(
        mapping=MAPPING,
        evidence="SYNTHETIC",
        cutoff=quotes[-1].observed_at,
        quotes=quotes,
        actions=actions,
    )


def signal(first, last, **overrides):
    args = dict(
        key="p1",
        origin_digest="original",
        evidence_id="r1",
        horizon="SHORT",
        available_at=first.start_at - timedelta(seconds=1),
        due_at=last.end_at,
    )
    return ExecutionSignal(**{**args, **overrides})


def trades(book):
    return [row for row in book["fills_and_actions"] if row["side"] in {"BUY", "SELL"}]


def test_cash_fees_partial_and_idempotent_signal():
    first, second = quote(0, 20, 21, 20, 21), quote(1, 21, 21, 21, 21)
    entry = signal(first, second)
    result = replay_execution(archive((first, second)), (RULE,), POLICY, (entry, entry))
    buy, sell = trades(result)
    assert (buy["quantity"], buy["price"], sum(buy["fees"].values())) == (100, 20, 2)
    assert (sell["quantity"], sell["price"], sum(sell["fees"].values())) == (100, 21, 3)
    assert result["final"]["cash"] == 10095
    assert result["final"]["quantity"] == 0
    assert result["trade_paths"][0]["net_pnl"] == 95
    assert all(
        p["cash"] + p["market_value"] + p["receivables"] == p["net_value"] for p in result["nav"]
    )
    partial = first.model_copy(update={"opening_volume": 43})
    competing = entry.model_copy(update={"key": "p2", "horizon": "SWING"})
    result = replay_execution(archive((partial, second)), (RULE,), POLICY, (competing, entry))
    assert trades(result)[0]["signal_key"] == "p1" and trades(result)[0]["quantity"] == 43
    assert len(trades(result)) == 2


def test_mfe_mae_capture_and_early_high_excluded():
    previous = quote(0, 200, 205, 198, 200)
    buy = quote(1, 100, 108, 97, 102)
    sell = quote(2, 104, 500, 1, 120)
    active = signal(buy, sell)
    book = replay_execution(archive((previous, buy, sell)), (RULE,), POLICY, (active,))
    row = book["trade_paths"][0]
    assert (row["mfe"], row["mae"], row["gross_return"], row["capture_ratio"]) == (
        D("0.08"),
        D("0.03"),
        D("0.04"),
        D("0.5"),
    )
    assert row["mfe_observable_at"] == buy.observed_at
    assert row["exit_at"] == sell.start_at


def test_limits_delayed_receipts_and_one_bar_delay():
    first = quote(0, 20, 22, 18, 21)
    halted = quote(1, 20, 20, 20, 20, tradable=False)
    last = quote(2, 21, 22, 20, 21)
    source = archive((first, halted, last))
    entry = signal(first, last)
    rule = RULE.model_copy(update={"lot": 10})
    limits = POLICY.model_copy(update={"max_entry_price": D(20), "slippage_bps": D(1)})
    no_fill = replay_execution(source, (rule,), limits, (entry,))
    assert trades(no_fill) == [] and no_fill["orders"][0]["status"] == "ETF_CHASE_LIMIT"
    delayed = replay_execution(source, (rule,), POLICY, (entry,), execution_delay=1)
    assert trades(delayed) == []
    late = first.model_copy(update={"opening_observed_at": first.start_at + timedelta(seconds=3)})
    replay = replay_execution(
        archive((late, halted, last)), (rule,), POLICY, (entry,), enforce_receipts=True
    )
    assert not trades(replay) and replay["orders"][0]["status"] == "NO_OPENING_EVIDENCE"
    approximate = first.model_copy(update={"bid": None, "ask": None, "opening_volume": None})
    raw = archive((approximate, halted, last))
    assert not trades(replay_execution(raw, (rule,), POLICY, (entry,)))
    optimistic = replay_execution(
        raw, (rule,), POLICY, (entry,), optimistic=True, target_first=True
    )
    assert optimistic["same_bar_conflicts"][0]["quote_id"] == approximate.quote_id
    assert trades(optimistic)[1]["reason"] == "AMBIGUOUS_TARGET_FIRST"


def test_sell_lag_split_dividend_and_nav_account_for_all_cash():
    first = quote(0, 20, 21, 20, 21)
    second = quote(1, 10, 10, 10, 10)
    third = quote(2, "10.50", 11, 10, 11)
    action = ToolAction(
        action_id="split",
        kind="SPLIT",
        known_at=first.start_at,
        effective_at=second.start_at,
        amount=2,
    )
    dividend = ToolAction(
        action_id="income",
        kind="DIVIDEND",
        known_at=first.start_at,
        effective_at=third.start_at,
        amount="0.50",
        pay_at=third.end_at,
    )
    hold = RULE.model_copy(update={"sell_lag_sessions": 2})
    result = replay_execution(
        archive((first, second, third), action, dividend), (hold,), POLICY, (signal(first, third),)
    )
    buy, sell = trades(result)
    assert (buy["quantity"], sell["quantity"], sell["session"]) == (100, 200, third.session)
    assert sum(sell["fees"].values()) == 3 and result["final"]["cash"] == 10195
    assert result["final"]["receivables"] == 0
    assert any(o["status"] == "DEFERRED" for o in result["orders"])
    assert result["nav"][1]["quantity"] == 200


def test_fixed_and_partial_exits_have_same_entry():
    a = quote(0, 100, 105, 99, 104)
    b = quote(1, 104, 107, 102, 106)
    c = quote(2, 106, 110, 100, 109)
    d = quote(3, 109, 110, 101, 107)
    source = archive((a, b, c, d))
    signals = (signal(a, c),)
    policy = POLICY.model_copy(update={"capital": D(12000)})
    fixed = replay_execution(source, (RULE,), policy, signals)
    partial = replay_execution(
        source,
        (RULE,),
        policy,
        signals,
        alternative="PARTIAL",
        fixed_entries=matched_entries(fixed),
    )
    assert matched_entries(fixed) == matched_entries(partial)
    assert [(r["price"], r["quantity"]) for r in trades(partial)] == [
        (100, 100),
        (104, 50),
        (109, 50),
    ]
    assert partial["trade_paths"][0]["net_pnl"] > fixed["trade_paths"][0]["net_pnl"]
    first_sale, final_sale = partial["trade_paths"][0]["matched_sales"]
    assert first_sale["mfe"] == D("0.05") and final_sale["mfe"] == D("0.10")
    assert (
        sum(s["net_pnl"] for s in (first_sale, final_sale)) == partial["trade_paths"][0]["net_pnl"]
    )


@pytest.fixture
def protocol_fixture(tmp_path):
    index = demo_bundle("SPX")
    last = index.bars[186]
    request = AnalysisRequest(
        symbol="SPX", as_of=last.usable_at, end=last.session_date, mode="FROZEN_DATA_REPLAY"
    )
    replay = build_brief(request, index, Settings(), include_weekly=False, check_windows=False)
    assert replay.brief.index_guidance.tracks[0].plan
    reports = tmp_path / "reports"
    publish_brief(replay, reports, formats=())
    cache = tmp_path / "cache"
    store = SnapshotStore(cache)
    identity = store.save(index)
    store.save(replay.prepared)
    calendar = load_calendar(index.instrument, last.session_date, index.sessions[-1])
    days = [row for row in calendar.sessions if row.date > last.session_date][:24]
    quote_rows = tuple(
        ToolQuote(
            quote_id=f"f{i}",
            session=row.date,
            start_at=row.open_at,
            end_at=row.close_at,
            observed_at=row.close_at,
            opening_observed_at=row.open_at,
            open=20,
            high=21,
            low=20,
            close=21,
            bid=20,
            ask=20,
            opening_volume=10000,
        )
        for i, row in enumerate(days)
    )
    data = archive(quote_rows)
    path = tmp_path / "tool.json"
    path.write_text(data.model_dump_json())
    first = IssuanceSlot(
        session=last.session_date,
        cutoff=replay.brief.as_of,
        issue_by=replay.brief.as_of + timedelta(minutes=5),
        label_by=days[22].close_at,
        split="HOLDOUT",
    )
    other = first.model_copy(
        update={
            "session": days[0].date,
            "cutoff": days[0].close_at,
            "issue_by": days[0].close_at,
            "label_by": days[-1].close_at,
        }
    )
    protocol = TradingProtocol(
        mode="FROZEN",
        mapping=MAPPING,
        index_feed=index.provenance.feed,
        index_adjustment=index.provenance.adjustment,
        analysis_config_hash=replay.brief.config_hash,
        guidance_policy_hash=replay.brief.index_guidance.policy_hash,
        settings=POLICY,
        rules=(RULE,),
        grid=(first, other),
        experiment_attempts=("main-v1",),
        index_snapshot_id=identity,
        tool_archive_sha256=sha256(path.read_bytes()).hexdigest(),
    )
    descriptor = tmp_path / "protocol.json"
    descriptor.write_text(protocol.model_dump_json())
    return protocol, descriptor, reports, cache, path


def test_existing_evaluation_entrypoint_and_missing_grid_slot(tmp_path, protocol_fixture):
    protocol, descriptor, reports, cache, path = protocol_fixture
    output = tmp_path / "artifacts"
    root = Path(__file__).resolve().parents[1]
    command = run(
        [
            executable,
            str(root / "scripts/evaluate_briefs.py"),
            "--protocol",
            str(descriptor),
            "--output-dir",
            str(output),
            "--index-reports",
            str(reports),
            "--snapshot-store",
            str(cache),
            "--tool-archive",
            str(path),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert command.returncode == 0, command.stderr
    summary = json.loads(command.stdout)
    assert summary["mode"] == "FROZEN_DATA_REPLAY"
    assert summary["status"] == "INSUFFICIENT_EVIDENCE"
    assert summary["issuance_counts"] == {"AVAILABLE": 1, "MISSED": 1}
    assert "FULL_DELAYED" in summary["splits"]["HOLDOUT"]["metrics"]
    manifest = json.loads((output / "manifest.json").read_text())
    assert (
        sha256((output / "books.json").read_bytes()).hexdigest() == manifest["files"]["books.json"]
    )
    new = protocol.model_copy(update={"tool_archive_sha256": None})
    descriptor.write_text(new.model_dump_json())
    output = tmp_path / "index-only"
    index_only = evaluate_index_trading(
        descriptor,
        output,
        report_directory=reports,
        snapshot_directory=cache,
    )
    assert index_only["status"] == "INDEX_ONLY"
    assert json.loads((output / "plans.json").read_text())


def test_forward_seal_rejects_retrospective_archives_and_protocol_edit(tmp_path, protocol_fixture):

    protocol, descriptor, reports, cache, path = protocol_fixture
    forward = protocol.model_copy(
        update={
            "mode": "FORWARD",
            "index_snapshot_id": None,
            "tool_archive_sha256": None,
        }
    )
    descriptor.write_text(forward.model_dump_json())
    seal_dir = tmp_path / "sealing"
    first = forward.grid[0].cutoff
    with pytest.raises(InputError, match="before"):
        seal_trading_protocol(descriptor, seal_dir, now=first)
    seal_trading_protocol(descriptor, seal_dir, now=first - timedelta(seconds=1))
    transform = dict(
        report_directory=reports,
        snapshot_directory=cache,
        tool_archive_path=path,
        seal_path=seal_dir / "seal.json",
        now=first,
    )
    with pytest.raises(InputError, match="No verified live decision"):
        evaluate_index_trading(descriptor, tmp_path / "score", **transform)
    descriptor.write_text(forward.model_copy(update={"block_sessions": 31}).model_dump_json())
    with pytest.raises(InputError, match="seal does not match"):
        evaluate_index_trading(descriptor, tmp_path / "score", **transform)


def test_forward_fixture_accepts_verified_receipt_but_keeps_missing_slots(
    tmp_path, protocol_fixture
):
    from dataclasses import replace

    protocol, descriptor, _, cache, path = protocol_fixture
    first = protocol.grid[0]
    issued = first.cutoff + timedelta(seconds=10)
    source = SnapshotStore(cache).load(protocol.index_snapshot_id)
    bars = tuple(b.model_copy(update={"observed_at": b.usable_at}) for b in source.bars)
    source = source.model_copy(update={"bars": bars, "raw_bars": bars})
    request = AnalysisRequest(symbol="SPX", as_of=first.cutoff, end=first.session, mode="LIVE")
    run = build_brief(
        request,
        source,
        Settings(),
        include_weekly=False,
        check_windows=False,
        outlook_issued_at=issued,
    )
    live = replace(run, brief=run.brief.model_copy(update={"generated_at": issued}))
    store = SnapshotStore(cache)
    store.save(live.prepared)
    reports = tmp_path / "live"
    publish_brief(live, reports, formats=(), clock=lambda: issued)
    forward = protocol.model_copy(
        update={"mode": "FORWARD", "index_snapshot_id": None, "tool_archive_sha256": None}
    )
    descriptor.write_text(forward.model_dump_json())
    seal = tmp_path / "seal"
    seal_trading_protocol(descriptor, seal, now=first.cutoff - timedelta(days=1))
    data = ToolArchive.model_validate_json(path.read_text())
    data = data.model_copy(update={"evidence": "RECEIVED"})
    path.write_text(data.model_dump_json())
    summary = evaluate_index_trading(
        descriptor,
        tmp_path / "score",
        report_directory=reports,
        snapshot_directory=cache,
        tool_archive_path=path,
        seal_path=seal / "seal.json",
        now=data.cutoff,
    )
    assert summary["mode"] == "SEALED_FORWARD"
    assert summary["status"] == "INSUFFICIENT_EVIDENCE"
    assert summary["issuance_counts"] == {"AVAILABLE": 1, "MISSED": 1}
    assert summary["signal_basis"] == "VERIFIED_REPORT_RECEIPTS"
    empty = tmp_path / "empty"
    empty.mkdir()
    score = evaluate_index_trading(
        descriptor,
        tmp_path / "empty-score",
        report_directory=empty,
        snapshot_directory=cache,
        tool_archive_path=path,
        seal_path=seal / "seal.json",
        now=data.cutoff,
    )
    assert score["issuance_counts"] == {"MISSED": 2}
    from elliottwaver.config import IndexGuidanceConfig

    changed = build_brief(
        request,
        source,
        Settings(index_guidance=IndexGuidanceConfig(short_sessions=3)),
        include_weekly=False,
        check_windows=False,
        outlook_issued_at=issued,
    )
    different = replace(changed, brief=changed.brief.model_copy(update={"generated_at": issued}))
    store.save(different.prepared)
    mismatched = tmp_path / "different"
    publish_brief(different, mismatched, formats=(), clock=lambda: issued)
    score = evaluate_index_trading(
        descriptor,
        tmp_path / "different-score",
        report_directory=mismatched,
        snapshot_directory=cache,
        tool_archive_path=path,
        seal_path=seal / "seal.json",
        now=data.cutoff,
    )
    assert score["issuance_counts"] == {"POLICY_MISMATCH": 1, "MISSED": 1}


def test_development_omits_labels_crossing_the_split(tmp_path, protocol_fixture):
    protocol, descriptor, reports, cache, path = protocol_fixture
    first, last = protocol.grid
    first = first.model_copy(update={"split": "DEVELOPMENT"})
    descriptor.write_text(protocol.model_copy(update={"grid": (first, last)}).model_dump_json())
    out = tmp_path / "development"
    summary = evaluate_index_trading(
        descriptor,
        out,
        report_directory=reports,
        snapshot_directory=cache,
        tool_archive_path=path,
        only_split="DEVELOPMENT",
    )
    assert summary["scored_split"] == "DEVELOPMENT"
    assert summary["purged_issuances"] == 1 and summary["plan_counts"] == {}
    grid = json.loads((out / "issuance_grid.json").read_text())
    assert [row["session"] for row in grid] == [first.session.isoformat()]
    assert grid[0]["mature"] is False
    all_splits = evaluate_index_trading(
        descriptor,
        tmp_path / "all",
        report_directory=reports,
        snapshot_directory=cache,
        tool_archive_path=path,
    )
    assert all_splits["splits"]["DEVELOPMENT"] == summary["splits"]["DEVELOPMENT"]


def test_exit_uses_next_tradable_open_and_prior_high_for_trailing():
    a = quote(0, 100, 108, 99, 104)
    b = quote(1, 106, 110, 105, 109)
    c = quote(2, 97, 100, 96, 97)
    d = quote(3, 90, 91, 88, 89)
    quotes = archive((a, b, c, d))
    entry = signal(a, d)
    policy = POLICY.model_copy(update={"capital": D(12000), "trailing_pct": D("0.05")})
    result = replay_execution(quotes, (RULE,), policy, (entry,), alternative="TRAILING")
    assert [(item["session"], item["price"]) for item in trades(result)] == [
        (a.session, 100),
        (d.session, 90),
    ]
    assert result["trade_paths"][0]["exit_reason"] == "STOP"
    assert result["trade_paths"][0]["mae"] == D("0.10")


def test_missing_open_cannot_extend_entry_window_or_backdate_nav():
    first = quote(0, 20, 20, 20, 20)
    second = quote(1, 20, 20, 20, 20)
    third = quote(2, 21, 21, 21, 21)
    late = first.model_copy(update={"observed_at": third.end_at + timedelta(seconds=1)})
    cutoff = third.end_at + timedelta(minutes=5)
    source = ToolArchive(mapping=MAPPING, evidence="SYNTHETIC", cutoff=cutoff, quotes=(late, third))
    entry = signal(second, third)
    schedule = [(q.session, q.start_at) for q in (first, second, third)]
    result = replay_execution(source, (RULE,), POLICY, (entry,), session_opens=schedule)
    assert trades(result) == []
    assert result["signal_status"][0]["status"] == "ENTRY_WINDOW_EXPIRED"
    assert [row["session"] for row in result["nav"]] == [first.session, third.session]
    assert all(row["net_value"] == POLICY.capital for row in result["nav"])


def test_fill_starts_holding_clock_and_late_nav_uses_valuation_account():
    a, b, c, d = (quote(i, p, p, p, p) for i, p in enumerate((20, 30, 30, 30)))
    a = a.model_copy(update={"observed_at": b.start_at + timedelta(seconds=1)})
    tool = archive((a, b, c, d))
    candidate = signal(b, b, holding_sessions=2, label_by=d.end_at)
    result = replay_execution(tool, (RULE,), POLICY, (candidate,))
    assert [(row["side"], row["session"]) for row in trades(result)] == [
        ("BUY", b.session),
        ("SELL", d.session),
    ]
    first = result["nav"][0]
    assert (first["session"], first["cash"], first["quantity"], first["net_value"]) == (
        a.session,
        10000,
        0,
        10000,
    )
    assert first["at"] == a.observed_at and first["mark_at"] == a.end_at


def test_chase_is_terminal_but_delay_skips_executable_buy_and_sell_only():
    dates = range(6)
    prices = (21, 19, 19, 20, 20, 20)
    quotes = tuple(
        quote(i, p, p, p, p, opening_volume=0 if i == 0 else 1000)
        for i, p in zip(dates, prices, strict=True)
    )
    a, b, c, d, e, f = quotes
    setup = POLICY.model_copy(update={"entry_sessions": 4, "max_entry_price": D(20)})
    candidate = signal(a, f, holding_sessions=1, label_by=f.end_at)
    chased = replay_execution(archive(quotes), (RULE,), setup, (candidate,))
    assert not trades(chased) and chased["signal_status"][0]["status"] == "ETF_CHASE_LIMIT"
    a = a.model_copy(
        update={
            "open": D(19),
            "high": D(19),
            "low": D(19),
            "close": D(19),
            "bid": D(19),
            "ask": D(19),
        }
    )
    source = archive((a, b, c, d, e, f))
    base = replay_execution(source, (RULE,), setup, (candidate,))
    stress = replay_execution(source, (RULE,), setup, (candidate,), execution_delay=1)
    assert [t["session"] for t in trades(base)] == [b.session, c.session]
    assert [t["session"] for t in trades(stress)] == [c.session, e.session]
    assert [o["side"] for o in stress["orders"] if o["status"] == "DELAY_STRESS"] == ["BUY", "SELL"]


def test_late_prior_bar_does_not_inherit_trailing_stop_from_newer_high():
    a = quote(0, 100, 101, 99, 100)
    b = quote(1, 100, 101, 97, 100)
    c = quote(2, 104, 107, 103, 105)
    d = quote(3, 106, 107, 104, 105)
    setup = POLICY.model_copy(update={"capital": D(12000), "trailing_pct": D("0.05")})
    candidate = signal(a, d)
    for prior in (b, b.model_copy(update={"observed_at": c.observed_at + timedelta(seconds=1)})):
        book = replay_execution(
            archive((a, prior, c, d)), (RULE,), setup, (candidate,), alternative="TRAILING"
        )
        assert [row["side"] for row in trades(book)] == ["BUY"]
        assert book["trade_paths"][0]["mae"] == D("0.03")


def test_ambiguous_simultaneous_actions_and_bootstrap_tail(protocol_fixture):
    from elliottwaver.evaluation.index_research import _block_interval

    protocol, *_ = protocol_fixture
    a = quote(0, 20, 20, 20, 20)
    action = dict(known_at=a.start_at, effective_at=a.start_at)
    split = ToolAction(action_id="split", kind="SPLIT", amount=2, **action)
    dividend = ToolAction(
        action_id="dividend", kind="DIVIDEND", amount="0.5", pay_at=a.end_at, **action
    )
    with pytest.raises(ValueError, match="distinct effective timestamp"):
        archive((a,), split, dividend)
    sessions = [a.session + timedelta(days=i) for i in range(5)]
    values = (101, 102, 103, 104, 90)
    book = {
        "nav": [
            {"session": day, "net_value": D(value)}
            for day, value in zip(sessions, values, strict=True)
        ]
    }
    protocol = protocol.model_copy(
        update={
            "block_sessions": 2,
            "settings": protocol.settings.model_copy(update={"capital": D(100)}),
        }
    )
    lower, upper = _block_interval(book, {"nav": []}, sessions, protocol)
    assert lower < 0 < upper
