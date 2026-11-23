from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

import pyarrow.parquet as pq
import pytest
from typer.testing import CliRunner

from elliottwaver.brief import build_brief
from elliottwaver.cli.main import _choose_brief_timeframe, app
from elliottwaver.config import Settings
from elliottwaver.data.demo import demo_bundle
from elliottwaver.data.periods import resample_bundle
from elliottwaver.data.snapshots import SnapshotStore
from elliottwaver.evaluation.decisions import evaluate_protocol
from elliottwaver.evaluation.events import EVENT_SPECS
from elliottwaver.evaluation.outcomes import OutcomeWindow, evaluate_outlook_event
from elliottwaver.evaluation.replay import replay
from elliottwaver.history import HistoryStore
from elliottwaver.schemas.market import AnalysisRequest, Bar
from elliottwaver.schemas.outlook import OutlookCase, OutlookCondition
from elliottwaver.schemas.wave import LevelCondition, PriceZone
from elliottwaver.service import AnalysisService
from elliottwaver.settlement import SettlementStore, event_specs
from elliottwaver.visualization.brief_formatter import publish_brief


def _run(symbol: str, timeframe: str):
    bundle = demo_bundle(symbol, timeframe=timeframe)
    request = AnalysisRequest(
        symbol=symbol,
        as_of=bundle.as_of,
        timeframe=timeframe,
        mode="FROZEN_DATA_REPLAY",
    )
    return bundle, build_brief(request, bundle, Settings(), include_weekly=False)


def test_cn_60m_brief_uses_five_future_bars_not_weeks():
    bundle, run = _run("600519.SH", "60m")
    outlook = run.brief.outlook
    assert outlook is not None
    assert outlook.version == "1.1"
    assert outlook.horizon_unit == "BARS"
    assert outlook.horizon == 5
    assert len(outlook.periods) == 5
    assert all(period.start_at >= bundle.as_of for period in outlook.periods)
    assert [period.start_at for period in outlook.periods] == sorted(
        period.start_at for period in outlook.periods
    )
    event_ids = {event.event_id for case in outlook.cases for event in case.events}
    assert "ADVANCE_FIRST_60M_5B_V1" in event_ids
    assert "OUTLOOK_TARGET_FIRST_60M_5B_V1" in event_ids
    assert not any("2W" in event_id for event_id in event_ids)


def test_us_5m_brief_uses_five_future_bars_on_the_same_session():
    bundle, run = _run("AAPL", "5m")
    outlook = run.brief.outlook
    assert outlook is not None
    assert outlook.horizon_unit == "BARS"
    assert len(outlook.periods) == 5
    assert len({period.end_session for period in outlook.periods}) == 1
    starts = [period.start_at for period in outlook.periods]
    assert starts == [bundle.as_of + timedelta(minutes=5 * index) for index in range(5)]
    assert run.brief.last_bar_end_at == bundle.bars[-1].end_at
    assert run.brief.last_bar_id == bundle.bars[-1].bar_id


def test_cn_60m_outlook_crosses_lunch_break_in_scheduled_order():
    _bundle, run = _run("600519.SH", "60m")
    periods = run.brief.outlook.periods
    first = periods[0]
    second = periods[1]
    assert first.start_at.hour == 6  # 14:00 CST
    assert second.start_at.hour == 1  # next session 09:30 CST
    assert second.start_at.minute == 30


def test_intraday_timestamp_anchor_selects_the_exact_bar():
    bundle = demo_bundle("AAPL", timeframe="5m")
    bar = bundle.bars[-20]
    run = build_brief(
        AnalysisRequest(
            symbol="AAPL",
            as_of=bundle.as_of,
            timeframe="5m",
            anchor=bar.session_date,
            anchor_at=bar.start_at,
            mode="FROZEN_DATA_REPLAY",
        ),
        bundle,
        Settings(),
        include_weekly=False,
        check_windows=False,
    )
    detail = run.brief.window.anchor_details[0]
    assert detail.at == bar.start_at
    assert run.result.diagnostics.anchor_times == (bar.start_at,)
    assert run.brief.window.start_at == bar.start_at
    assert run.brief.window.end_at == bundle.bars[-1].end_at


def test_background_pairs_resample_from_the_same_source():
    cn_1m = demo_bundle("600519.SH", timeframe="1m")
    cn_5m = resample_bundle(cn_1m, "5m", as_of=cn_1m.as_of)
    assert cn_5m.timeframe == "5m"
    assert cn_5m.provenance.aggregation == "1m->5m"
    assert cn_5m.bars and cn_5m.raw_bars

    us_5m = demo_bundle("AAPL", timeframe="5m")
    us_60m = resample_bundle(us_5m, "60m", as_of=us_5m.as_of)
    assert us_60m.timeframe == "60m"
    assert us_60m.provenance.aggregation == "5m->60m"
    assert us_60m.bars and us_60m.raw_bars

    cn_60m = demo_bundle("600519.SH", timeframe="60m")
    cn_1d = resample_bundle(cn_60m, "1d", as_of=cn_60m.as_of)
    assert cn_1d.timeframe == "1d"
    assert cn_1d.provenance.aggregation == "60m->1d"
    assert cn_1d.bars and cn_1d.raw_bars


def test_background_resample_refuses_an_incomplete_coarse_window():
    bundle = demo_bundle("AAPL", timeframe="5m")
    bars = list(bundle.bars)
    removed = bars.pop(len(bars) // 2)
    broken = bundle.model_copy(update={"bars": tuple(bars), "raw_bars": tuple(bars)})
    result = resample_bundle(broken, "60m", as_of=broken.as_of)
    assert all(not (bar.start_at <= removed.start_at < bar.end_at) for bar in result.bars)


def test_hierarchical_engine_accepts_intraday_timeframes():
    bundle = demo_bundle("AAPL", timeframe="5m")
    settings = Settings(
        analysis={"engine_profile": "hierarchical_v1"},
        search={"max_subwave_depth": 1},
    )
    result = AnalysisService(settings).analyze_bundle(
        AnalysisRequest(
            symbol="AAPL",
            as_of=bundle.as_of,
            timeframe="5m",
            mode="FROZEN_DATA_REPLAY",
        ),
        bundle,
    )
    assert result.status == "OK"
    assert result.schema_version == "1.1"
    assert result.scenarios
    assert result.scenarios[0].structure is not None


def test_intraday_replay_uses_only_bars_available_at_each_prefix():
    bundle = demo_bundle("AAPL", timeframe="5m")
    results = list(replay(bundle, AnalysisService(Settings()), step=50))
    assert len(results) >= 3
    usable = {bar.usable_at for bar in bundle.bars}
    for result in results:
        assert result.timeframe == "5m"
        assert result.as_of in usable
    assert results[0].as_of < results[-1].as_of


def _condition(kind, side, op, price):
    return LevelCondition(
        kind=kind,
        side=side,
        op=op,
        price=price,
        active_from=datetime(2024, 1, 5, 14, 30, tzinfo=ZoneInfo("America/New_York")),
        reason="fixture",
        coordinate_id="fixture",
    )


def _intraday_case():
    return OutlookCase(
        scenario_id="scenario",
        structure_key="structure",
        slot="PRIMARY",
        conditions=(
            OutlookCondition(
                role="ADVANCE",
                condition=_condition("CONFIRMATION", "HIGH", ">", 105),
            ),
            OutlookCondition(
                role="INVALIDATION",
                condition=_condition("INVALIDATION", "LOW", "<", 98),
            ),
        ),
        target=PriceZone(
            low=110,
            high=112,
            label="目标区1",
            methods=("fixture",),
            independent_method_count=1,
        ),
        target_mode="AFTER_ADVANCE",
        target_direction="UP",
    )


def _intraday_bar(index, close, high, low):
    start = datetime(2024, 1, 5, 19, 30, tzinfo=UTC) + timedelta(minutes=5 * index)
    return Bar(
        start_at=start,
        end_at=start + timedelta(minutes=5),
        session_date=date(2024, 1, 5),
        observed_at=datetime(2026, 1, 1, tzinfo=UTC),
        open=close,
        close=close,
        high=high,
        low=low,
        volume=100,
    )


def test_intraday_event_same_bar_target_and_stop_is_ambiguous():
    first = _intraday_bar(0, 104, 108, 99)
    second = _intraday_bar(1, 109, 111, 97)
    window = OutcomeWindow(
        (first.session_date, second.session_date),
        (first, second),
        second.session_date,
        period_starts=(first.start_at, second.start_at),
        period_ends=(first.end_at, second.end_at),
        observed_at=second.usable_at,
    )
    result = evaluate_outlook_event(
        EVENT_SPECS["OUTLOOK_TARGET_FIRST_5M_5B_V1"], _intraday_case(), window
    )
    assert result.status == "AMBIGUOUS_SAME_BAR"
    assert result.bar_id == second.bar_id
    assert result.start_at == second.start_at


def test_intraday_event_missing_first_bar_is_incomplete():
    first = _intraday_bar(0, 104, 108, 99)
    window = OutcomeWindow(
        (first.session_date,),
        (None,),
        first.session_date,
        period_starts=(first.start_at,),
        period_ends=(first.end_at,),
        observed_at=first.usable_at,
        incomplete_indices=frozenset({0}),
    )
    result = evaluate_outlook_event(EVENT_SPECS["ADVANCE_FIRST_5M_5B_V1"], _intraday_case(), window)
    assert result.status == "INCOMPLETE_DATA"


def test_intraday_protocol_writes_bar_level_decisions_and_5b_events(tmp_path):
    import json

    bundle = demo_bundle("600519.SH", timeframe="60m")
    cache = tmp_path / ".cache/elliottwaver"
    snapshot = SnapshotStore(cache).save(bundle)
    protocol = {
        "protocol_version": "1",
        "protocol_id": "intraday-test",
        "cache_root": str(cache),
        "samples": [
            {
                "symbol": "600519.SH",
                "asset_type": "EQUITY",
                "source_timeframe": "60m",
                "snapshot_id": snapshot,
            }
        ],
        "events": [
            "ADVANCE_FIRST_60M_5B_V1",
            "OUTLOOK_TARGET_FIRST_60M_5B_V1",
        ],
        "grid": {
            "start": "2024-01-10",
            "end": "2024-04-01",
            "cadence": "EVERY_BAR",
            "step": 20,
            "outcome_as_of": "2024-04-09",
        },
    }
    path = tmp_path / "protocol.json"
    path.write_text(json.dumps(protocol))
    summary = evaluate_protocol(path, tmp_path / "evaluation")
    decisions = [
        json.loads(line)
        for line in (tmp_path / "evaluation" / "decisions.jsonl").read_text().splitlines()
    ]
    assert decisions
    assert all(item["timeframe"] == "60m" for item in decisions)
    assert len({item["decision_id"] for item in decisions}) == len(decisions)
    assert all(item["decision_at"] for item in decisions)
    assert summary["decisions"] > 0
    assert summary["events"]
    events = pq.read_table(tmp_path / "evaluation" / "events.parquet").to_pylist()
    assert events
    assert {row["event_id"] for row in events} == set(protocol["events"])
    assert all(row["due_at"] for row in events)
    assert all(row["period_starts"] for row in events)


def test_same_day_intraday_history_orders_by_last_bar_not_insertion(tmp_path):
    bundle = demo_bundle("600519.SH", timeframe="60m")
    runs = []
    for decision in (bundle.bars[-30], bundle.bars[-10]):
        runs.append(
            build_brief(
                AnalysisRequest(
                    symbol="600519.SH",
                    as_of=decision.usable_at,
                    end=decision.session_date,
                    timeframe="60m",
                    mode="FROZEN_DATA_REPLAY",
                ),
                bundle,
                Settings(),
                include_weekly=False,
                check_windows=False,
            )
        )
    store = HistoryStore(tmp_path / ".cache/elliottwaver")
    paths = []
    for index, run in enumerate(runs):
        paths.append(publish_brief(run, tmp_path / f"reports-{index}", formats=("md",)))
    # Write the later decision first, then the earlier decision.
    store.register(runs[1].brief, paths[1])
    store.register(runs[0].brief, paths[0])
    rows = store.query("600519.SH", timeframe="60m")
    assert [row["brief_digest"] for row in rows] == [
        runs[1].brief.brief_digest,
        runs[0].brief.brief_digest,
    ]
    previous = store.previous(runs[1].brief)
    assert previous is not None
    assert previous["brief_digest"] == runs[0].brief.brief_digest
    assert previous["last_bar_end_at"] == runs[0].brief.last_bar_end_at.isoformat()


def test_intraday_snapshot_is_adopted_by_auto_brief_timeframe():
    bundle = demo_bundle("600519.SH", timeframe="60m")
    chosen, reason = _choose_brief_timeframe("auto", bundle, None, bundle.as_of)
    assert chosen == "60m"
    assert "分钟" in reason


def test_intraday_brief_cli_auto_and_json(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    bundle = demo_bundle("600519.SH", timeframe="60m")
    snapshot = SnapshotStore(tmp_path / ".cache/elliottwaver").save(bundle)
    result = CliRunner().invoke(
        app,
        [
            "brief",
            "600519.SH",
            "--offline",
            "--snapshot",
            snapshot,
            "--format",
            "json",
            "--json",
        ],
    )
    assert result.exit_code == 0, result.output
    payload = __import__("json").loads(result.stdout)
    assert payload["timeframe"] == "60m"
    assert payload["outlook"]["horizon_unit"] == "BARS"
    assert payload["outlook"]["horizon"] == 5


def test_intraday_settlement_uses_frozen_exact_window(tmp_path):
    bundle = demo_bundle("600519.SH", timeframe="60m")
    decision = bundle.bars[-10]
    run = build_brief(
        AnalysisRequest(
            symbol="600519.SH",
            as_of=decision.usable_at,
            end=decision.session_date,
            timeframe="60m",
            mode="FROZEN_DATA_REPLAY",
        ),
        bundle,
        Settings(),
        include_weekly=False,
        check_windows=False,
    )
    assert run.brief.outlook is not None
    assert run.brief.outlook.horizon_unit == "BARS"
    path = publish_brief(run, tmp_path / "reports", formats=("md",))
    store = SettlementStore(tmp_path / ".cache/elliottwaver")
    inserted = store.register_brief(run.brief, path)
    assert inserted >= 1
    store.settle_brief(run.brief, path, bundle)
    rows = store.rows(brief_digest=run.brief.brief_digest)
    assert rows
    assert all(row["timeframe"] == "60m" for row in rows)
    assert all(row["horizon_unit"] == "BARS" for row in rows)
    assert any(row["event_id"].startswith("ADVANCE_FIRST_60M_") for row in rows)
    assert any(row["event_id"].startswith("OUTLOOK_TARGET_FIRST_60M_") for row in rows)


def test_intraday_settlement_rejects_old_weekly_window(tmp_path):
    bundle = demo_bundle("600519.SH", timeframe="60m")
    decision = bundle.bars[-10]
    run = build_brief(
        AnalysisRequest(
            symbol="600519.SH",
            as_of=decision.usable_at,
            end=decision.session_date,
            timeframe="60m",
            mode="FROZEN_DATA_REPLAY",
        ),
        bundle,
        Settings(),
        include_weekly=False,
        check_windows=False,
    )
    old = run.brief.outlook.model_copy(
        update={
            "version": "1.0",
            "horizon": 2,
            "horizon_unit": "WEEKS",
            "periods": run.brief.outlook.periods[:1],
        }
    )
    brief = run.brief.model_copy(update={"outlook": old})
    with pytest.raises(Exception, match="旧观察窗与主周期不匹配"):
        event_specs(brief)
