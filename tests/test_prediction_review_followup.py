"""Regression evidence for the 2026-11-21 independent outlook review (PO-01..PO-09)."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from conftest import make_bar, make_pivots

from elliottwaver.brief import build_brief
from elliottwaver.cli.console_render import render_outlook
from elliottwaver.config import Settings
from elliottwaver.data.demo import demo_bundle
from elliottwaver.data.periods import as_weekly
from elliottwaver.engine.matcher import Candidate
from elliottwaver.engine.projector import _filter_zones, project, project_with_gates
from elliottwaver.engine.templates import FLAT, IMPULSE, TRIANGLE, WXY, ZIGZAG
from elliottwaver.evaluation.events import EVENT_SPECS, condition_hit
from elliottwaver.evaluation.features import event_feature_order
from elliottwaver.evaluation.outcomes import (
    OutcomeWindow,
    close_band,
    evaluate_outlook_event,
    outlook_window,
)
from elliottwaver.forecast import ModelRegistry, validate_model
from elliottwaver.outlook import _case, _periods, build_outlook
from elliottwaver.schemas.market import AnalysisRequest
from elliottwaver.schemas.outlook import OutlookCase, OutlookCondition, OutlookPeriod
from elliottwaver.schemas.wave import LevelCondition, PriceZone
from elliottwaver.service import engine_rules_version
from elliottwaver.settlement import event_specs
from elliottwaver.util import digest
from elliottwaver.visualization.brief_text import condition_text, price_text
from scripts import train_forecasts

_AS_OF = datetime(2026, 1, 1, tzinfo=UTC)


def _candidate(template, prices, completed, stage, direction=1):
    bars, points = make_pivots(prices)
    return bars, Candidate(template, points[:completed], points[-1], direction, stage, (), (1.4,))


def _card(run, scenario_id, variant=None):
    card = run.brief.cards[0]
    return card.model_copy(
        update={
            "scenario_id": scenario_id,
            "variant": variant,
            "target_zones": (),
            "conditions": (),
        }
    )


def _condition(kind, side, op, price, reason):
    return LevelCondition(
        kind=kind,
        side=side,
        op=op,
        price=price,
        active_from=_AS_OF,
        reason=reason,
        coordinate_id="fixture",
    )


# PO-01


def test_wxy_x_retracement_and_break_of_w_have_separate_roles():
    bars, candidate = _candidate(WXY, [100, 120, 114], 2, "X_DEVELOPING")
    bars[-1] = bars[-1].model_copy(update={"high": 119, "low": 109, "close": 114})
    projection = project_with_gates(
        candidate, bars, atr=1.0, coordinate_id="fixture", as_of=_AS_OF, tick=0.01
    )
    advance = [item.condition for item in projection.conditions if item.role == "ADVANCE"]
    assert [(condition.side, condition.op, condition.price) for condition in advance] == [
        ("HIGH", ">", 120)
    ]
    assert any(
        item.role == "CURRENT_EVIDENCE"
        and item.condition.side == "LOW"
        and item.condition.price == 110
        for item in projection.conditions
    )
    run = build_brief(
        AnalysisRequest(symbol="AAPL", as_of=demo_bundle("AAPL").as_of),
        demo_bundle("AAPL"),
        Settings(),
        include_weekly=False,
        check_windows=False,
    )
    case = _case(_card(run, candidate.key), "PRIMARY", projection, 114, "SESSIONS")
    assert case.target_mode == "AFTER_ADVANCE"
    target = next(item for item in case.events if item.event_id.startswith("OUTLOOK_TARGET"))
    assert target.state == "DEFINED"
    tested_bar = make_bar(5, 114, high=119, low=109)
    window = OutcomeWindow((tested_bar.session_date,), (tested_bar,), tested_bar.session_date)
    advance = evaluate_outlook_event(EVENT_SPECS["ADVANCE_FIRST_5D_V1"], case, window)
    assert advance.status == "RIGHT_CENSORED"


def test_wxy_x_retracement_and_break_of_w_down_mirror():
    bars, candidate = _candidate(WXY, [200, 180, 186], 2, "X_DEVELOPING", direction=-1)
    bars[-1] = bars[-1].model_copy(update={"high": 191, "low": 181, "close": 186})
    projection = project_with_gates(
        candidate, bars, atr=1.0, coordinate_id="fixture", as_of=_AS_OF, tick=0.01
    )
    advance = [item.condition for item in projection.conditions if item.role == "ADVANCE"]
    assert [(condition.side, condition.op, condition.price) for condition in advance] == [
        ("LOW", "<", 180)
    ]
    assert any(
        item.role == "CURRENT_EVIDENCE"
        and item.condition.side == "HIGH"
        and item.condition.price == 190
        for item in projection.conditions
    )


# PO-02


def test_flat_b_beyond_origin_suppresses_c_confirmation_and_target():
    bars, candidate = _candidate(FLAT, [100, 140, 97, 110], 3, "C_DEVELOPING")
    projection = project_with_gates(
        candidate, bars, atr=1.0, coordinate_id="fixture", as_of=_AS_OF, tick=0.01
    )
    assert not any("平台C进入A端点附近" in item.condition.reason for item in projection.conditions)
    assert projection.zones == ()
    run = build_brief(
        AnalysisRequest(symbol="AAPL", as_of=demo_bundle("AAPL").as_of),
        demo_bundle("AAPL"),
        Settings(),
        include_weekly=False,
        check_windows=False,
    )
    case = _case(_card(run, candidate.key), "PRIMARY", projection, 110, "SESSIONS")
    assert case.target is None
    assert case.target_mode == "NONE"
    target = next(item for item in case.events if item.event_id.startswith("OUTLOOK_TARGET"))
    assert target.state == "UNDEFINED"


def test_unfinished_triangle_has_stage_exit_and_no_target():
    bars, candidate = _candidate(TRIANGLE, [100, 145, 115, 135, 119, 125], 5, "E_DEVELOPING")
    projection = project_with_gates(
        candidate, bars, atr=1.0, coordinate_id="fixture", as_of=_AS_OF, tick=0.01
    )
    assert projection.zones == ()
    assert any(
        item.role == "STAGE_EXIT"
        and item.condition.side == "HIGH"
        and item.condition.price == 125
        and "三角形尚未完成" in item.condition.reason
        for item in projection.conditions
    )
    assert not any("越过三角形C点" in item.condition.reason for item in projection.conditions)


def test_wave3_has_a_strict_stage_exit_below_wave2_end():
    bars, candidate = _candidate(IMPULSE, [100, 120, 110, 122], 3, "W3_DEVELOPING")
    projection = project_with_gates(
        candidate, bars, atr=1.0, coordinate_id="fixture", as_of=_AS_OF, tick=0.01
    )
    exit_item = next(
        item for item in projection.conditions if "浪3严格穿越" in item.condition.reason
    )
    assert exit_item.role == "STAGE_EXIT"
    assert (exit_item.condition.side, exit_item.condition.op, exit_item.condition.price) == (
        "LOW",
        "<",
        110,
    )
    bars, candidate = _candidate(IMPULSE, [200, 180, 190, 178], 3, "W3_DEVELOPING", direction=-1)
    projection = project_with_gates(
        candidate, bars, atr=1.0, coordinate_id="fixture", as_of=_AS_OF, tick=0.01
    )
    exit_item = next(
        item for item in projection.conditions if "浪3严格穿越" in item.condition.reason
    )
    assert (exit_item.condition.side, exit_item.condition.op, exit_item.condition.price) == (
        "HIGH",
        ">",
        190,
    )


@pytest.mark.parametrize(
    ("template", "prices", "stage", "progress_level"),
    [
        (IMPULSE, [100, 120, 110, 115], "W3_DEVELOPING", 120),
        (IMPULSE, [100, 120, 110, 145, 130, 136], "W5_DEVELOPING", 145),
        (ZIGZAG, [100, 120, 110, 115], "C_DEVELOPING", 120),
        (FLAT, [100, 140, 120, 125], "C_DEVELOPING", 130),
        (WXY, [100, 120, 110, 114], "Y_DEVELOPING", 120),
    ],
)
def test_developing_wave_progress_keeps_its_target_active(template, prices, stage, progress_level):
    bars, candidate = _candidate(template, prices, len(prices) - 1, stage)
    projection = project_with_gates(
        candidate, bars, atr=1.0, coordinate_id="fixture", as_of=_AS_OF, tick=0.01
    )
    assert not any(item.role == "ADVANCE" for item in projection.conditions)
    assert any(
        item.role == "CURRENT_EVIDENCE" and item.condition.price == progress_level
        for item in projection.conditions
    )
    bundle = demo_bundle("AAPL")
    run = build_brief(
        AnalysisRequest(symbol="AAPL", as_of=bundle.as_of),
        bundle,
        Settings(),
        include_weekly=False,
        check_windows=False,
    )
    case = _case(_card(run, candidate.key), "PRIMARY", projection, prices[-1], "SESSIONS")
    assert case.target_mode == "ACTIVE"
    assert case.target is not None
    hit_price = max(progress_level + 0.01, case.target.low)
    bar = make_bar(6, hit_price)
    window = OutcomeWindow((bar.session_date,), (bar,), bar.session_date)
    assert (
        evaluate_outlook_event(EVENT_SPECS["OUTLOOK_TARGET_FIRST_5D_V1"], case, window).status
        == "TARGET_FIRST"
    )


def test_target_filter_rejects_equal_stop_and_target_before_advance_gate():
    candidate = Candidate(IMPULSE, (), None, 1, "W3_DEVELOPING", (), (1.4,))
    zones = (PriceZone(low=130, high=135, label="z", methods=("x",), independent_method_count=1),)
    gate = OutlookCondition(
        role="ADVANCE",
        condition=_condition("CONFIRMATION", "HIGH", ">", 140, "gate"),
    )
    assert _filter_zones(candidate, zones, (gate,), 100, 0.01) == ()
    stop = OutlookCondition(
        role="INVALIDATION",
        condition=_condition("INVALIDATION", "LOW", "<=", 130, "stop"),
    )
    assert _filter_zones(candidate, zones, (stop,), 100, 0.01) == ()
    stop = OutlookCondition(
        role="INVALIDATION",
        condition=_condition("INVALIDATION", "LOW", "<=", 90, "stop"),
    )
    assert _filter_zones(candidate, zones, (stop,), 100, 0.01) == zones


def test_build_outlook_refuses_search_limited_result():
    bundle = demo_bundle("AAPL")
    run = build_brief(
        AnalysisRequest(symbol="AAPL", as_of=bundle.as_of),
        bundle,
        Settings(),
        include_weekly=False,
        check_windows=False,
    )
    limited = run.result.model_copy(update={"status": "SEARCH_LIMITED"})
    outlook = build_outlook(run.brief.cards, limited, run.prepared, {})
    assert outlook.status == "UNAVAILABLE"
    assert "SEARCH_LIMITED" in (outlook.reason or "")


# PO-03


def test_weekly_outlook_starts_after_the_issue_time():
    bundle = demo_bundle("AAPL", timeframe="1w").model_copy(
        update={"as_of": datetime(2026, 11, 18, 22, tzinfo=UTC)}
    )
    periods, horizon, unit = _periods(bundle, date(2026, 11, 13), "1w")
    assert (horizon, unit) == (2, "WEEKS")
    assert periods[0].sessions[0] == date(2026, 11, 23)
    assert periods[1].end_session == date(2026, 12, 4)


def test_delayed_weekly_data_does_not_count_an_already_started_week():
    bundle = demo_bundle("AAPL", timeframe="1w").model_copy(
        update={"as_of": datetime(2026, 11, 20, 22, tzinfo=UTC)}
    )
    periods, _horizon, _unit = _periods(bundle, date(2026, 11, 13), "1w")
    assert periods[0].sessions[0] == date(2026, 11, 23)


def test_daily_outlook_skips_a_session_already_open_at_issue():
    bundle = demo_bundle("AAPL").model_copy(
        update={"as_of": datetime(2026, 11, 18, 18, tzinfo=UTC)}
    )
    periods, _horizon, _unit = _periods(bundle, date(2026, 11, 17), "1d")
    assert periods[0].sessions[0] == date(2026, 11, 19)


def test_settlement_and_replay_share_the_frozen_periods():
    bundle = demo_bundle("AAPL", timeframe="1w")
    run = build_brief(
        AnalysisRequest(symbol="AAPL", as_of=bundle.as_of, timeframe="1w"),
        bundle,
        Settings(),
        include_weekly=False,
        check_windows=False,
    )
    outlook = run.brief.outlook
    assert outlook is not None
    window = outlook_window(bundle, run.result.market_asof, outlook.periods, 2)
    assert window.dates == tuple(period.end_session for period in outlook.periods[:2])
    assert window.period_sessions == tuple(tuple(period.sessions) for period in outlook.periods[:2])
    old = run.brief.model_copy(update={"outlook": None})
    old_ids = {event_id for event_id, _scope, _horizon, _unit in event_specs(old)}
    new_ids = {event_id for event_id, _scope, _horizon, _unit in event_specs(run.brief)}
    assert "CLOSE_BAND_2W_V1" not in old_ids
    assert not any(event.startswith(("ADVANCE_FIRST", "OUTLOOK_TARGET")) for event in old_ids)
    assert "CLOSE_BAND_2W_V1" in new_ids
    unavailable = run.brief.model_copy(
        update={
            "outlook": run.brief.outlook.model_copy(
                update={"status": "UNAVAILABLE", "cases": (), "reason": "fixture"}
            )
        }
    )
    unavailable_ids = {event_id for event_id, _scope, _horizon, _unit in event_specs(unavailable)}
    assert "CLOSE_BAND_2W_V1" not in unavailable_ids
    assert not any(
        event.startswith(("ADVANCE_FIRST", "OUTLOOK_TARGET")) for event in unavailable_ids
    )


def test_frozen_week_distinguishes_calendar_changes_from_a_missing_bar():
    daily = demo_bundle("AAPL")
    weekly = as_weekly(daily)
    decision, first, due = weekly.bars[20:23]
    periods = tuple(
        OutlookPeriod(
            start_at=bar.start_at,
            end_at=bar.end_at,
            end_session=bar.session_date,
            sessions=tuple(
                day
                for day in daily.sessions
                if day.isocalendar()[:2] == bar.session_date.isocalendar()[:2]
            ),
        )
        for bar in (first, due)
    )
    missing = periods[1].sessions[0]
    shortened = daily.model_copy(
        update={
            "bars": tuple(bar for bar in daily.bars if bar.session_date != missing),
            "raw_bars": tuple(bar for bar in daily.raw_bars if bar.session_date != missing),
        }
    )
    w = outlook_window(as_weekly(shortened), decision.session_date, periods, 2)
    assert w.bars[1] is None
    assert w.missing(due.session_date).status == "INCOMPLETE_DATA"
    assert close_band(w, decision.close, 1.0, 2).value == due.close

    shortened_calendar = shortened.model_copy(
        update={
            "sessions": tuple(day for day in daily.sessions if day != missing),
        }
    )
    w = outlook_window(as_weekly(shortened_calendar), decision.session_date, periods, 2)
    assert w.bars[1] is None
    assert close_band(w, decision.close, 1.0, 2).status == "SETTLEMENT_CALENDAR_MISMATCH"


@pytest.mark.parametrize("weekly", [False, True])
def test_outlook_waits_for_the_closing_bars_actual_availability(weekly):
    daily = demo_bundle("AAPL")
    source = as_weekly(daily) if weekly else daily
    decision, closing = source.bars[20:22]
    sessions = tuple(
        day
        for day in daily.sessions
        if (
            day.isocalendar()[:2] == closing.session_date.isocalendar()[:2]
            if weekly
            else day == closing.session_date
        )
    )
    period = OutlookPeriod(
        start_at=closing.start_at,
        end_at=closing.end_at,
        end_session=closing.session_date,
        sessions=sessions,
    )

    def delay(bars):
        return tuple(
            bar.model_copy(update={"available_at": bar.end_at + timedelta(days=3)})
            if bar.session_date == closing.session_date
            else bar
            for bar in bars
        )

    delayed = daily.model_copy(
        update={"bars": delay(daily.bars), "raw_bars": delay(daily.raw_bars)}
    )
    source = as_weekly(delayed) if weekly else delayed
    w = outlook_window(
        source, decision.session_date, (period,), 1, outcome_as_of=closing.session_date
    )
    assert close_band(w, decision.close, 1.0, 1).status == "RIGHT_CENSORED"
    w = outlook_window(
        source,
        decision.session_date,
        (period,),
        1,
        outcome_as_of=closing.end_at + timedelta(days=3),
    )
    assert close_band(w, decision.close, 1.0, 1).value == closing.close


# PO-04


def _write_evaluation(directory: Path, protocol: dict, decisions: list[dict], events: list[dict]):
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / "decisions.jsonl").open("w") as stream:
        for row in decisions:
            stream.write(json.dumps(row) + "\n")
    pq.write_table(pa.Table.from_pylist(events), directory / "events.parquet")
    (directory / "summary.json").write_text("{}")
    files = {
        name: hashlib.sha256((directory / name).read_bytes()).hexdigest()
        for name in ("decisions.jsonl", "events.parquet", "summary.json")
    }
    manifest = {
        "schema_version": "1.1",
        "feature_version": "4",
        "protocol": protocol,
        "protocol_sha256": "protocol",
        "engine_version": "engine",
        "config_hash": "config",
        "source_sha256": "a" * 64,
        "files": files,
    }
    (directory / "manifest.json").write_text(json.dumps(manifest))
    return manifest


def _decision(symbol, asset_type="EQUITY", market="US", feed="sip", adjustment="provider_all"):
    return {
        "decision_id": f"d-{symbol}",
        "symbol": symbol,
        "asset_type": asset_type,
        "market": market,
        "feed": feed,
        "adjustment": adjustment,
        "timeframe": "1d",
        "decision_date": "2026-04-01",
        "as_of": "2026-04-01T21:00:00+00:00",
        "source_snapshot_id": "b" * 64,
        "status": "OK",
        "analyzable": True,
        "features": {
            "log_return_20": 0.1,
            "missing_return": 0.0,
            "atr_ratio": 0.02,
            "candidate_count": 1,
            "up_weight": 1.0,
            "down_weight": 0.0,
            "unknown_weight": 0.0,
            "top_quality": 0.6,
            "top_coverage": 0.8,
            "top2_quality_gap": 0.1,
            "top_bias": 1.0,
            "weekly_bias": 0.0,
            "missing_weekly": 0.0,
            "missing_second": 0.0,
        },
    }


def _event(decision, slot="PRIMARY", symbol=None):
    return {
        "decision_id": decision["decision_id"],
        "symbol": symbol or decision["symbol"],
        "as_of": decision["as_of"],
        "event_id": "ADVANCE_FIRST_5D_V1",
        "horizon": 5,
        "due_date": "2026-04-10",
        "label_available_at": "2026-04-10T21:00:00+00:00",
        "candidate_key": f"c-{decision['symbol']}-{slot}",
        "slot": slot,
        "status": "CONFIRMED_FIRST",
        "session": "2026-04-08",
        "value": None,
        "features": {
            "quality": 0.6,
            "coverage": 0.8,
            "bias": 1.0,
            "pattern": "IMPULSE",
            "stage": "W3_DEVELOPING",
            "candidate_rank": 1,
            "confirm_distance": 1.0,
            "invalidation_distance": 2.0,
            "target_distance_atr": 2.0,
            "inside_target_zone": 0.0,
            "missing_target_geometry": 0.0,
            "outlook_advance_distance": 1.0,
            "outlook_stop_distance": 2.0,
            "outlook_stage_exit_distance": 3.0,
            "outlook_target_low_distance": 2.0,
            "outlook_target_high_distance": 3.0,
            "outlook_target_mode_active": 1.0,
            "outlook_target_mode_after_advance": 0.0,
            "outlook_current_leg_up": 1.0,
            "outlook_current_leg_down": 0.0,
            "outlook_advance_up": 1.0,
            "outlook_advance_down": 0.0,
            "outlook_double_three": 0.0,
            "outlook_subwave_verified": 1.0,
            "outlook_subwave_depth": 0.5,
            "outlook_relation_verified": 1.0,
            "outlook_window_same_count": 1.0,
        },
    }


def test_train_requires_and_filters_the_outlook_slot(monkeypatch, tmp_path):
    protocol = {
        "protocol_version": "1",
        "protocol_id": "slot-filter",
        "samples": [
            {
                "symbol": "AAPL",
                "asset_type": "EQUITY",
                "source_timeframe": "1d",
                "snapshot_id": "b" * 64,
            }
        ],
        "grid": {"start": "2026-04-01", "end": "2026-04-30", "cadence": "EVERY_SESSION"},
        "splits": {
            "train": {"start": "2025-01-01", "end": "2025-06-30"},
            "calibration": {"start": "2025-07-01", "end": "2025-12-31"},
            "test": {"start": "2026-04-01", "end": "2026-04-30"},
        },
        "events": ["ADVANCE_FIRST_5D_V1"],
        "release": {"min_test_rows": 1},
        "feature_version": "4",
    }
    path = tmp_path / "protocol.json"
    path.write_text(json.dumps(protocol))
    decision = _decision("AAPL")
    events = [_event(decision, "PRIMARY"), _event(decision, "ALTERNATE_1")]
    combined = {
        **protocol,
        "protocol_id": "combined",
        "events": protocol["events"] + ["CLOSE_BAND_5D_V1"],
    }
    manifest = {
        "protocol": combined,
        "protocol_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        "engine_version": engine_rules_version(Settings()),
        "config_hash": Settings().computation_hash,
        "files": {"decisions.jsonl": "x", "events.parquet": "y"},
    }
    monkeypatch.setattr(train_forecasts, "read_evaluation_manifest", lambda _: manifest)
    monkeypatch.setattr(
        train_forecasts, "_load_rows", lambda _: ({decision["decision_id"]: decision}, events)
    )
    captured = {}

    def capture(_protocol, _windows, _manifest, _cohort, rows, _output, **kwargs):
        captured["rows"] = rows
        captured.update(kwargs)
        return {"model_id": "fixture"}

    monkeypatch.setattr(train_forecasts, "_train_cohort", capture)
    with pytest.raises(ValueError, match="explicit --outlook-slot"):
        train_forecasts.train(path, tmp_path, tmp_path / "out")
    train_forecasts.train(path, tmp_path, tmp_path / "out", outlook_slot="PRIMARY")
    assert len(captured["rows"]) == 1
    assert captured["rows"][0]["slot"] == "PRIMARY"
    assert captured["symbols"] == ["AAPL"]


def _frozen_candidate():
    event = {
        "feature_order": ["atr_ratio"],
        "classes": ["NO", "YES"],
        "mean": [0.0],
        "scale": [1.0],
        "weights": [[0.0, 0.0]],
        "intercept": [0.0, 0.0],
        "temperature": 1.0,
        "status": "INSUFFICIENT_EVIDENCE",
        "baselines": {
            "frequency": {"probabilities": [0.5, 0.5]},
            "momentum": {
                "feature_order": ["atr_ratio"],
                "mean": [0.0],
                "scale": [1.0],
                "weights": [[0.0, 0.0]],
                "intercept": [0.0, 0.0],
                "temperature": 1.0,
            },
            "price": {
                "feature_order": ["atr_ratio"],
                "mean": [0.0],
                "scale": [1.0],
                "weights": [[0.0, 0.0]],
                "intercept": [0.0, 0.0],
                "temperature": 1.0,
            },
        },
    }
    candidate = {
        "schema_version": "1.0",
        "stage": "FROZEN_CANDIDATE",
        "frozen_at": "2026-03-31T23:59:59+00:00",
        "development_end": "2026-03-31",
        "development_label_available_at": "2026-03-31T21:00:00+00:00",
        "feature_version": "4",
        "engine_version": "engine",
        "config_hash": "config",
        "scope": {
            "markets": ["US"],
            "asset_types": ["EQUITY"],
            "timeframes": ["1d"],
            "feed": ["sip"],
            "adjustment": ["provider_all"],
            "symbols": ["AAPL"],
            "outlook_version": "1.0",
            "outlook_slot": "PRIMARY",
        },
        "symbols": ["AAPL"],
        "splits": {
            "train": {"start": "2025-01-01", "end": "2025-06-30"},
            "calibration": {"start": "2025-07-01", "end": "2025-12-31"},
            "test": {"start": "2026-01-01", "end": "2026-03-31"},
        },
        "release": {"min_test_rows": 1},
        "decisions_sha256": "c" * 64,
        "events_sha256": "d" * 64,
        "models": {"ADVANCE_FIRST_5D_V1": event},
    }
    candidate["candidate_id"] = digest(candidate)
    return candidate


def test_score_filters_rows_to_the_frozen_symbol_and_slot(monkeypatch, tmp_path):
    candidate = _frozen_candidate()
    candidate_path = tmp_path / "candidate.json"
    candidate_path.write_text(json.dumps(candidate))
    protocol = {
        "protocol_version": "1",
        "protocol_id": "scope-filter",
        "samples": [
            {
                "symbol": symbol,
                "asset_type": asset_type,
                "source_timeframe": "1d",
                "snapshot_id": "e" * 64,
            }
            for symbol, asset_type in (
                ("AAPL", "EQUITY"),
                ("MSFT", "EQUITY"),
                ("SPY", "INDEX"),
            )
        ],
        "grid": {
            "start": "2026-04-01",
            "end": "2026-04-30",
            "cadence": "EVERY_SESSION",
            "outcome_as_of": "2026-05-10",
        },
        "splits": {
            "train": {"start": "2026-04-01", "end": "2026-04-10"},
            "calibration": {"start": "2026-04-11", "end": "2026-04-20"},
            "test": {"start": "2026-04-21", "end": "2026-04-30"},
        },
        "events": ["ADVANCE_FIRST_5D_V1"],
        "feature_version": "4",
        "data_status": "HISTORICAL_HOLDOUT",
        "release": {"min_test_rows": 1},
    }
    decisions = [
        _decision("AAPL"),
        _decision("MSFT"),
        _decision("SPY", asset_type="INDEX"),
    ]
    events = [
        _event(decisions[0], "PRIMARY"),
        _event(decisions[0], "ALTERNATE_1"),
        _event(decisions[1], "PRIMARY"),
        _event(decisions[2], "PRIMARY"),
    ]
    holdout = tmp_path / "holdout"
    _write_evaluation(holdout, protocol, decisions, events)
    captured = {}

    def fake_evidence(event_id, rows, classes, probabilities, baselines, release, cadence):
        captured["rows"] = rows
        return {
            "status": "INSUFFICIENT_EVIDENCE",
            "reasons": ["fixture"],
            "baseline_metrics": {name: {} for name in baselines},
        }

    monkeypatch.setattr(train_forecasts, "forecast_evidence", fake_evidence)
    summary = train_forecasts.score(candidate_path, holdout, tmp_path / "output")
    assert [row["symbol"] for row in captured["rows"]] == ["AAPL"]
    assert [row["slot"] for row in captured["rows"]] == ["PRIMARY"]
    model = json.loads((tmp_path / "output" / "model.json").read_text())
    assert model["study_mode"] == "RETROSPECTIVE"
    assert model["operational_allowed"] is False
    assert summary["models"]["ADVANCE_FIRST_5D_V1"]["status"] == "INSUFFICIENT_EVIDENCE"


def test_score_keeps_an_untrained_frozen_event_insufficient(monkeypatch, tmp_path):
    candidate = _frozen_candidate()
    for key in ("feature_order", "weights", "mean", "scale", "intercept"):
        candidate["models"]["ADVANCE_FIRST_5D_V1"].pop(key, None)
    candidate["candidate_id"] = digest(
        {key: value for key, value in candidate.items() if key != "candidate_id"}
    )
    path = tmp_path / "candidate.json"
    path.write_text(json.dumps(candidate))
    protocol = {
        "protocol_version": "1",
        "samples": [
            {
                "symbol": "AAPL",
                "asset_type": "EQUITY",
                "source_timeframe": "1d",
                "snapshot_id": "e" * 64,
            }
        ],
        "grid": {
            "start": "2026-04-01",
            "end": "2026-04-30",
            "cadence": "EVERY_SESSION",
            "outcome_as_of": "2026-05-10",
        },
        "splits": {},
        "events": ["ADVANCE_FIRST_5D_V1"],
        "feature_version": "4",
        "data_status": "HISTORICAL_HOLDOUT",
        "release": {"min_test_rows": 1},
    }
    holdout = tmp_path / "holdout"
    decision = _decision("AAPL")
    _write_evaluation(holdout, protocol, [decision], [_event(decision)])
    summary = train_forecasts.score(path, holdout, tmp_path / "output")
    entry = summary["models"]["ADVANCE_FIRST_5D_V1"]
    assert entry["status"] == "INSUFFICIENT_EVIDENCE"
    assert entry["reasons"] == ["frozen candidate has no fitted parameters"]


def test_freeze_rejects_a_backdated_frozen_at(monkeypatch, tmp_path):
    protocol = {
        "protocol_version": "1",
        "protocol_id": "backdate",
        "samples": [
            {
                "symbol": "AAPL",
                "asset_type": "EQUITY",
                "source_timeframe": "1d",
                "snapshot_id": "b" * 64,
            }
        ],
        "grid": {"start": "2024-01-01", "end": "2026-03-31", "cadence": "EVERY_SESSION"},
        "splits": {
            "train": {"start": "2024-01-01", "end": "2024-12-31"},
            "calibration": {"start": "2025-01-01", "end": "2025-12-31"},
            "test": {"start": "2026-01-01", "end": "2026-03-31"},
        },
        "events": ["ADVANCE_FIRST_5D_V1"],
        "release": {"min_test_rows": 1},
    }
    path = tmp_path / "protocol.json"
    path.write_text(json.dumps(protocol))
    decisions_dir = tmp_path / "decisions"
    decisions_dir.mkdir()
    decision = _decision("AAPL")
    decision.update({"decision_date": "2026-03-31", "as_of": "2026-03-31T21:00:00+00:00"})
    events = [_event(decision)]
    _write_evaluation(decisions_dir, protocol, [decision], events)
    monkeypatch.setattr(train_forecasts, "train", lambda *args, **kwargs: {})
    with pytest.raises(ValueError, match="precedes the development window"):
        train_forecasts.freeze(
            path,
            decisions_dir,
            tmp_path / "candidate.json",
            outlook_slot="PRIMARY",
            frozen_at=datetime(2023, 12, 31, 23, 59, tzinfo=UTC),
        )


def test_score_rejects_a_reused_development_artifact(tmp_path):
    candidate = _frozen_candidate()
    holdout = tmp_path / "holdout"
    protocol = {
        "protocol_version": "1",
        "samples": [
            {
                "symbol": "AAPL",
                "asset_type": "EQUITY",
                "source_timeframe": "1d",
                "snapshot_id": "e" * 64,
            }
        ],
        "grid": {
            "start": "2026-04-01",
            "end": "2026-04-30",
            "cadence": "EVERY_SESSION",
            "outcome_as_of": "2026-05-10",
        },
        "splits": {},
        "events": ["ADVANCE_FIRST_5D_V1"],
        "feature_version": "4",
        "data_status": "HISTORICAL_HOLDOUT",
        "release": {"min_test_rows": 1},
    }
    manifest = _write_evaluation(holdout, protocol, [], [])
    candidate["decisions_sha256"] = manifest["files"]["decisions.jsonl"]
    candidate["candidate_id"] = digest(
        {key: value for key, value in candidate.items() if key != "candidate_id"}
    )
    path = tmp_path / "candidate.json"
    path.write_text(json.dumps(candidate))
    with pytest.raises(ValueError, match="reuses the development"):
        train_forecasts.score(path, holdout, tmp_path / "output")


def test_registry_rejects_new_outlook_approval_without_holdout_provenance():
    order = event_feature_order("ADVANCE_FIRST_5D_V1", "4")
    model = {
        "schema_version": "1.2",
        "feature_version": "4",
        "engine_version": "engine",
        "config_hash": "config",
        "scope": {
            "markets": ["US"],
            "asset_types": ["EQUITY"],
            "timeframes": ["1d"],
            "feed": ["sip"],
            "adjustment": ["provider_all"],
            "symbols": ["AAPL"],
            "outlook_version": "1.0",
            "outlook_slot": "PRIMARY",
        },
        "splits": {
            "train": {"start": "2025-01-01", "end": "2025-06-30"},
            "calibration": {"start": "2025-07-01", "end": "2025-12-31"},
            "test": {"start": "2026-01-01", "end": "2026-03-31"},
        },
        "release": {
            "min_test_rows": 1,
            "min_test_dates": 1,
            "min_test_symbols": 1,
            "min_bin_count": 1,
            "min_bin_dates": 1,
            "min_supported_coverage": 0.5,
            "reliability_tolerance": 0.1,
        },
        "protocol_sha256": "1" * 64,
        "decisions_sha256": "2" * 64,
        "events_sha256": "3" * 64,
        "models": {
            "ADVANCE_FIRST_5D_V1": {
                "status": "APPROVED",
                "feature_order": order,
                "classes": ["NO", "YES"],
                "mean": [0.0] * len(order),
                "scale": [1.0] * len(order),
                "weights": [[0.0, 0.0] for _ in order],
                "intercept": [0.0, 0.0],
                "reasons": [],
                "improvement": {
                    name: {"brier_ci": [0.01, 0.03], "log_loss_ci": [0.01, 0.03]}
                    for name in ("frequency", "momentum", "price")
                },
                "supported_coverage": 1.0,
                "test_rows": 50,
                "test_dates": 10,
                "test_symbols": 5,
                "support_bins": {
                    "NO": [
                        {
                            "low": 0.0,
                            "high": 1.0,
                            "count": 50,
                            "dates": 10,
                            "occupied_blocks": 2,
                            "calibration_gap_ci": [-0.02, 0.02],
                        }
                    ],
                    "YES": [
                        {
                            "low": 0.0,
                            "high": 1.0,
                            "count": 50,
                            "dates": 10,
                            "occupied_blocks": 2,
                            "calibration_gap_ci": [-0.02, 0.02],
                        }
                    ],
                },
            }
        },
    }
    model["model_id"] = digest(model)
    with pytest.raises(ValueError, match="frozen holdout provenance"):
        validate_model(model, operational=True)


# PO-06


def test_new_outlook_labels_map_stage_exit_and_ambiguity_to_no():
    assert train_forecasts._label("ADVANCE_FIRST_5D_V1", "STAGE_EXIT_FIRST") == 0
    assert train_forecasts._label("ADVANCE_FIRST_5D_V1", "AMBIGUOUS_SAME_BAR") == 0
    assert train_forecasts._label("ADVANCE_FIRST_5D_V1", "INCOMPLETE_DATA") is None
    assert train_forecasts._label("CONFIRM_FIRST_20D_V1", "STAGE_EXIT_FIRST") is None


def test_same_bar_double_stop_is_hard_invalidation_with_both_reasons():
    case = OutlookCase(
        scenario_id="scenario",
        structure_key="structure",
        slot="PRIMARY",
        conditions=(
            OutlookCondition(
                role="STAGE_EXIT",
                condition=_condition("STAGE_EXIT", "LOW", "<", 150, "stage exit"),
            ),
            OutlookCondition(
                role="INVALIDATION",
                condition=_condition("INVALIDATION", "LOW", "<=", 150, "hard stop"),
            ),
            OutlookCondition(
                role="ADVANCE",
                condition=_condition("CONFIRMATION", "HIGH", ">", 160, "advance"),
            ),
        ),
    )
    bar = make_bar(0, 149, high=155, low=149)
    window = OutcomeWindow((bar.session_date,), (bar,), bar.session_date)
    outcome = evaluate_outlook_event(EVENT_SPECS["ADVANCE_FIRST_5D_V1"], case, window)
    assert outcome.status == "INVALIDATED_FIRST"
    assert outcome.evidence == ("stage exit", "hard stop")


# PO-07


def _new_model(event_id, slot, *, symbol="AAPL"):
    order = event_feature_order(event_id, "4")
    model = {
        "schema_version": "1.2",
        "feature_version": "4",
        "engine_version": "engine",
        "config_hash": "config",
        "frozen_candidate_id": f"candidate-{event_id}",
        "holdout_source_sha256": "4" * 64,
        "data_status": "SEALED_FORWARD_HOLDOUT",
        "study_mode": "DECISION_TIME",
        "operational_allowed": True,
        "frozen_at": "2023-12-28T00:00:00+00:00",
        "forward_evidence": {
            "seal_id": "5" * 64,
            "receipt_sha256": "6" * 64,
            "receipt_count": 5,
            "sealed_at": "2023-12-29T00:00:00+00:00",
        },
        "scope": {
            "markets": ["US"],
            "asset_types": ["EQUITY"],
            "timeframes": ["1d"],
            "feed": ["synthetic"],
            "adjustment": ["synthetic_no_actions"],
            "symbols": [symbol],
            "outlook_version": "1.0",
            "outlook_slot": slot,
        },
        "splits": {
            "train": {"start": "2023-01-01", "end": "2023-06-30"},
            "calibration": {"start": "2023-07-01", "end": "2023-12-31"},
            "test": {"start": "2024-01-01", "end": "2024-03-01"},
        },
        "release": {
            "min_test_rows": 1,
            "min_test_dates": 1,
            "min_test_symbols": 1,
            "min_bin_count": 1,
            "min_bin_dates": 1,
            "min_supported_coverage": 0.5,
            "reliability_tolerance": 0.1,
        },
        "protocol_sha256": "1" * 64,
        "decisions_sha256": "2" * 64,
        "events_sha256": "3" * 64,
        "models": {
            event_id: {
                "status": "APPROVED",
                "feature_order": order,
                "classes": ["NO", "YES"],
                "mean": [0.0] * len(order),
                "scale": [1.0] * len(order),
                "weights": [[0.0, 0.0] for _ in order],
                "intercept": [0.0, 0.0],
                "temperature": 1.0,
                "reasons": [],
                "test_rows": 50,
                "test_dates": 10,
                "test_symbols": 5,
                "supported_coverage": 1.0,
                "support_bins": {
                    name: [
                        {
                            "low": 0.0,
                            "high": 1.0,
                            "count": 50,
                            "dates": 10,
                            "occupied_blocks": 2,
                            "calibration_gap_ci": [-0.02, 0.02],
                        }
                    ]
                    for name in ("NO", "YES")
                },
                "improvement": {
                    name: {"brier_ci": [0.01, 0.03], "log_loss_ci": [0.01, 0.03]}
                    for name in ("frequency", "momentum", "price")
                },
            }
        },
    }
    model["model_id"] = digest(model)
    return model


def test_brief_uses_models_for_two_events_and_two_slots(tmp_path):
    registry = ModelRegistry(tmp_path / "registry")
    advance = _new_model("ADVANCE_FIRST_5D_V1", "PRIMARY")
    target = _new_model("OUTLOOK_TARGET_FIRST_5D_V1", "ALTERNATE_1")
    advance_path = tmp_path / "advance.json"
    target_path = tmp_path / "target.json"
    advance_path.write_text(json.dumps(advance))
    target_path.write_text(json.dumps(target))
    registry.register(advance_path, approved_at="2025-01-01T00:00:00+00:00")
    registry.register(target_path, approved_at="2025-02-01T00:00:00+00:00")
    bundle = demo_bundle("AAPL")
    run = build_brief(
        AnalysisRequest(symbol="AAPL", as_of=bundle.as_of),
        bundle,
        Settings(),
        models=(advance, target),
        include_weekly=False,
        check_windows=False,
    )
    by_event = {item.event_id: item for item in run.brief.forecasts}
    assert by_event["ADVANCE_FIRST_5D_V1"].model_id == advance["model_id"]
    assert by_event["ADVANCE_FIRST_5D_V1"].outlook_slot == "PRIMARY"
    assert by_event["OUTLOOK_TARGET_FIRST_5D_V1"].model_id == target["model_id"]
    assert by_event["OUTLOOK_TARGET_FIRST_5D_V1"].outlook_slot in {"ALTERNATE_1", None}


def test_brief_keeps_an_old_model_alongside_new_outlook_models(approved_model):
    bundle = demo_bundle("AAPL")
    old = approved_model()
    new = _new_model("ADVANCE_FIRST_5D_V1", "PRIMARY")
    run = build_brief(
        AnalysisRequest(symbol="AAPL", as_of=bundle.as_of),
        bundle,
        Settings(),
        models=(old, new),
        include_weekly=False,
        check_windows=False,
    )
    by_event = {item.event_id: item for item in run.brief.forecasts}
    assert by_event["CLOSE_BAND_5D_V1"].model_id == old["model_id"]
    assert by_event["ADVANCE_FIRST_5D_V1"].model_id == new["model_id"]


def test_replay_event_features_read_the_frozen_window_state():
    from elliottwaver.engine.indicators import calculate
    from elliottwaver.evaluation.decisions import _event_rows

    bundle = demo_bundle("AAPL")
    run = build_brief(
        AnalysisRequest(symbol="AAPL", as_of=bundle.as_of),
        bundle,
        Settings(),
        include_weekly=False,
        check_windows=False,
    )
    decision = {
        "decision_id": "d",
        "symbol": "AAPL",
        "as_of": run.result.as_of.isoformat(),
        "outlook": run.brief.outlook.model_dump(mode="json"),
        "wave_relations": (),
        "window_comparison": {"state": "SAME_COUNT"},
    }
    protocol = {
        "events": ["ADVANCE_FIRST_5D_V1", "OUTLOOK_TARGET_FIRST_5D_V1"],
        "grid": {},
    }
    rows = _event_rows(
        protocol,
        decision,
        bundle,
        run.prepared,
        run.qualified,
        float(calculate(run.prepared.bars).atr[-1]),
    )
    assert rows
    assert all(row["period_sessions"] for row in rows)
    assert all(
        row["features"]["outlook_window_same_count"] == 1.0 for row in rows if row.get("features")
    )


def test_build_brief_resolves_window_state_before_forecasting(monkeypatch):
    captured = {}

    def capture(*args, **kwargs):
        captured.update(kwargs)
        return ()

    monkeypatch.setattr("elliottwaver.brief.forecasts_for_brief", capture)
    bundle = demo_bundle("AAPL")
    run = build_brief(
        AnalysisRequest(symbol="AAPL", as_of=bundle.as_of),
        bundle,
        Settings(),
        include_weekly=False,
        check_windows=False,
    )
    assert captured["window_state"] == run.brief.window_comparison.state


# PO-08


def test_console_condition_precision_matches_executable_boundary():
    item = OutlookCondition(
        role="ADVANCE",
        condition=_condition("CONFIRMATION", "HIGH", ">", 22.266, "越过前高"),
    )
    line = condition_text(item.condition, item.role)
    assert "最高价高于" in line
    assert "22.266" in line
    assert "22.27" not in line
    assert price_text(22.27) == "22.27"
    bar = make_bar(0, 22.27, high=22.27, low=22.2)
    assert condition_hit(item.condition, bar)


def test_console_outlook_explains_slot_and_target_mode(capsys):
    bundle = demo_bundle("AAPL")
    run = build_brief(
        AnalysisRequest(symbol="AAPL", as_of=bundle.as_of),
        bundle,
        Settings(),
        include_weekly=False,
        check_windows=False,
    )
    render_outlook(run.brief)
    output = capsys.readouterr().out
    assert "主选" in output
    assert ("目标立即观察" in output) or ("目标等待推进后观察" in output) or ("暂无目标" in output)


# PO-09


def test_project_keeps_legacy_analysis_conditions_while_outlook_filters():
    bars, candidate = _candidate(FLAT, [100, 140, 97, 110], 3, "C_DEVELOPING")
    legacy_conditions, legacy_zones, _legacy_path = project(
        candidate, bars, 1.0, "fixture", _AS_OF, 0.01
    )
    assert any("平台C进入A端点附近" in item.reason for item in legacy_conditions)
    assert legacy_zones
    outlook = project_with_gates(
        candidate, bars, atr=1.0, coordinate_id="fixture", as_of=_AS_OF, tick=0.01
    )
    assert not any("平台C进入A端点附近" in item.condition.reason for item in outlook.conditions)
    assert outlook.zones == ()

    bars, candidate = _candidate(TRIANGLE, [100, 145, 115, 135, 119, 125], 5, "E_DEVELOPING")
    legacy_conditions, legacy_zones, _ = project(candidate, bars, 1.0, "fixture", _AS_OF, 0.01)
    assert any("越过三角形C点" in item.reason for item in legacy_conditions)
    assert legacy_zones
    outlook = project_with_gates(
        candidate, bars, atr=1.0, coordinate_id="fixture", as_of=_AS_OF, tick=0.01
    )
    assert not any("越过三角形C点" in item.condition.reason for item in outlook.conditions)
    assert outlook.zones == ()
