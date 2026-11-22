import json
from dataclasses import replace

import pyarrow.parquet as pq
import pytest

from elliottwaver.brief import _select_cards, attach_previous, build_brief
from elliottwaver.config import Settings
from elliottwaver.data.demo import demo_bundle
from elliottwaver.data.snapshots import SnapshotStore
from elliottwaver.engine.indicators import calculate
from elliottwaver.errors import InputError
from elliottwaver.evaluation.decisions import (
    _confirmed_pivots,
    _pivot_rewrite_counts,
    _structure_stats,
    evaluate_protocol,
    merge_evaluations,
)
from elliottwaver.forecast import ModelRegistry
from elliottwaver.schemas.market import AnalysisRequest
from elliottwaver.schemas.wave import LevelCondition
from elliottwaver.service import prepare_bundle


def test_evaluate_protocol_writes_decisions_events_and_summary(bundle, tmp_path):
    request = AnalysisRequest(symbol="AAPL", as_of=bundle.as_of)
    prepared = prepare_bundle(request, bundle)
    snapshot_id = SnapshotStore(tmp_path / ".cache/elliottwaver").save(prepared)
    protocol = {
        "protocol_version": "1",
        "cache_root": str(tmp_path / ".cache/elliottwaver"),
        "samples": [
            {
                "symbol": "AAPL",
                "asset_type": "EQUITY",
                "source_timeframe": "1d",
                "snapshot_id": snapshot_id,
            }
        ],
        "grid": {
            "start": str(bundle.bars[-30].session_date),
            "end": str(bundle.bars[-10].session_date),
            "cadence": "EVERY_SESSION",
        },
        "events": [
            "CLOSE_BAND_5D_V1",
            "CLOSE_BAND_20D_V1",
            "CONFIRM_FIRST_20D_V1",
            "TARGET_FIRST_20D_V1",
        ],
    }
    protocol_path = tmp_path / "protocol.json"
    protocol_path.write_text(json.dumps(protocol))

    summary = evaluate_protocol(protocol_path, tmp_path / "eval")

    assert summary["decisions"] > 0
    assert summary["analyzable"] > 0
    assert "CLOSE_BAND_5D_V1" in summary["events"]
    close_stats = summary["events"]["CLOSE_BAND_5D_V1"]
    assert close_stats["coverage"] == 1.0
    assert close_stats["status_rates"]
    assert (tmp_path / "eval" / "decisions.jsonl").exists()
    assert (tmp_path / "eval" / "events.parquet").exists()
    assert (tmp_path / "eval" / "summary.json").exists()
    assert (tmp_path / "eval" / "manifest.json").exists()
    rows = pq.read_table(tmp_path / "eval" / "events.parquet").to_pylist()
    assert rows
    assert {row["event_id"] for row in rows} >= {
        "CLOSE_BAND_5D_V1",
        "CLOSE_BAND_20D_V1",
    }
    assert "by_symbol" in summary
    assert "by_asset_type" in summary
    assert "by_pattern" in summary
    assert "by_horizon" in summary
    assert summary["by_timeframe"]["1d"]["analyzable"] == summary["analyzable"]
    structure = summary["structure"]
    assert structure["confirmed_pivots"] > 0
    assert structure["pivot_rewrite_pairs"] > 0
    assert structure["confirmation_delay"]["count"] > 0
    assert structure["prediction_coverage"] is not None
    decision_rows = [
        json.loads(line)
        for line in (tmp_path / "eval" / "decisions.jsonl").read_text().splitlines()
    ]
    assert decision_rows[0]["new_pivots"]
    assert decision_rows[0]["confirmed_pivot_count"] > 0
    assert "pivot_rewrite_count" in decision_rows[0]
    assert "primary_switch" in decision_rows[0]
    assert "search_truncated" in decision_rows[0]

    merged = merge_evaluations(
        protocol_path,
        [tmp_path / "eval", tmp_path / "eval"],
        tmp_path / "merged",
    )
    assert merged["decisions"] == summary["decisions"]
    assert merged["analyzable"] == summary["analyzable"]
    manifest = json.loads((tmp_path / "merged" / "manifest.json").read_text())
    assert len(manifest["merged_from"]) == 2


def test_mixed_event_order_preserves_outlook_columns_through_merge(bundle, tmp_path):
    store = SnapshotStore(tmp_path / "cache")
    day = str(bundle.bars[-20].session_date)
    protocol = {
        "protocol_version": "1",
        "protocol_id": "mixed-event-order",
        "cache_root": str(store.root),
        "samples": [
            {
                "symbol": "AAPL",
                "asset_type": "EQUITY",
                "source_timeframe": "1d",
                "snapshot_id": store.save(bundle),
            }
        ],
        "grid": {"start": day, "end": day, "cadence": "EVERY_SESSION"},
        "events": ["CLOSE_BAND_5D_V1", "OUTLOOK_TARGET_FIRST_5D_V1"],
    }
    path = tmp_path / "protocol.json"
    rows_by_order = []
    for index in range(2):
        path.write_text(json.dumps(protocol))
        shard = tmp_path / f"shard-{index}"
        merged = tmp_path / f"merged-{index}"
        evaluate_protocol(path, shard)
        merge_evaluations(path, [shard], merged)
        table = pq.read_table(merged / "events.parquet")
        assert {"slot", "outlook_version", "structure_key"} <= set(table.column_names)
        rows = [row for row in table.to_pylist() if row["event_id"].startswith("OUTLOOK_TARGET")]
        assert rows and rows[0]["outlook_version"] == "1.0"
        assert "PRIMARY" in {row["slot"] for row in rows}
        rows_by_order.append(rows)
        protocol["events"].reverse()
    assert rows_by_order[0] == rows_by_order[1]


@pytest.mark.parametrize("profile", ["classic_v4", "hierarchical_v1"])
def test_replay_uses_published_comparison_mode_and_primary_key(
    bundle, tmp_path, monkeypatch, profile
):
    previous_bar, next_bar = bundle.bars[-22:-20]
    store = SnapshotStore(tmp_path / "cache")
    snapshot = store.save(bundle)
    protocol_path = tmp_path / "protocol.json"
    protocol_path.write_text(
        json.dumps(
            {
                "protocol_version": "1",
                "cache_root": str(tmp_path / "cache"),
                "samples": [
                    {
                        "symbol": "AAPL",
                        "asset_type": "EQUITY",
                        "source_timeframe": "1d",
                        "snapshot_id": snapshot,
                    }
                ],
                "grid": {
                    "start": str(previous_bar.session_date),
                    "end": str(next_bar.session_date),
                    "cadence": "EVERY_SESSION",
                },
                "settings": {"analysis": {"engine_profile": profile}},
                "events": [],
            }
        )
    )
    built = []

    def control_count(request, source, settings, **kwargs):
        run = build_brief(request, source, settings, include_weekly=False, check_windows=False)
        if not built:
            condition = LevelCondition(
                kind="INVALIDATION",
                side="LOW",
                op="<=",
                price=next_bar.low,
                active_from=previous_bar.usable_at,
                reason="fixture",
                coordinate_id=run.brief.price_coordinate_id,
            )
            scenario = run.result.scenarios[0].model_copy(
                update={"variant": None, "conditions": (condition,)}
            )
        else:
            scenario = built[0].result.scenarios[0]
            if profile == "hierarchical_v1":
                scenario = scenario.model_copy(update={"variant": "TRUNCATED_FIFTH"})
        result = run.result.model_copy(update={"scenarios": (scenario,)})
        brief = run.brief.model_copy(update={"cards": _select_cards(result, run.prepared)})
        built.append(replace(run, brief=brief, result=result))
        return built[-1]

    monkeypatch.setattr("elliottwaver.evaluation.decisions.build_brief", control_count)
    output = tmp_path / "evaluation"
    evaluate_protocol(protocol_path, output)
    first, second = [
        json.loads(line) for line in (output / "decisions.jsonl").read_text().splitlines()
    ]
    previous, current = built
    published = attach_previous(
        current,
        previous.brief,
        previous_result=previous.result,
        previous_source=previous.source,
        current_source=current.source,
    ).brief.previous
    assert second["previous_change"] == published.change
    assert [first["first_structure_key"], second["first_structure_key"]] == [
        run.brief.cards[0].structure_key for run in built
    ]
    if profile == "classic_v4":
        assert published.change == "SAME_COUNT" and published.triggered_conditions
        assert second["primary_switch"] is False
    else:
        assert published.structure_change == "COUNT_CHANGED"
        assert second["primary_switch"] is True


def test_structure_stats_counts_switches_rewrites_and_delays():
    rows = [
        {
            "analyzable": True,
            "first_structure_key": "a",
            "primary_switch": None,
            "pivot_rewrite_count": None,
            "pivot_new_count": None,
            "confirmed_pivots": [{"pivot_id": "p1", "scale": "1.4", "bars_to_confirm": 2}],
            "search_truncated": False,
        },
        {
            "analyzable": True,
            "first_structure_key": "b",
            "primary_switch": True,
            "pivot_rewrite_count": 1,
            "pivot_new_count": 2,
            "confirmed_pivots": [
                {"pivot_id": "p2", "scale": "1.4", "bars_to_confirm": 4},
                {"pivot_id": "p3", "scale": "2.4", "bars_to_confirm": 6},
            ],
            "search_truncated": True,
        },
        {
            "analyzable": True,
            "first_structure_key": "b",
            "primary_switch": False,
            "pivot_rewrite_count": 0,
            "pivot_new_count": 0,
            "confirmed_pivots": [{"pivot_id": "p4", "scale": "1.4", "bars_to_confirm": 2}],
            "search_truncated": False,
        },
        {
            "analyzable": False,
            "first_structure_key": None,
            "primary_switch": None,
            "pivot_rewrite_count": None,
            "pivot_new_count": None,
            "confirmed_pivots": [],
            "search_truncated": False,
        },
    ]
    stats = _structure_stats(rows)
    assert stats["prediction_coverage"] == 1.0
    assert stats["signals"] == 3
    assert stats["no_signal"] == 0
    assert stats["primary_switches"] == 1
    assert stats["comparable_primary_pairs"] == 2
    assert stats["primary_switch_rate"] == 0.5
    assert stats["pivot_rewrite_pairs"] == 2
    assert stats["pivot_rewrites"] == 1
    assert stats["pivot_rewrites_per_pair"] == 0.5
    assert stats["pivot_rewrite_rate"] == 0.5
    assert stats["new_pivots"] == 2
    assert stats["confirmed_pivots"] == 4
    assert stats["confirmed_pivot_observations"] == 4
    assert stats["confirmation_delay"]["count"] == 4
    assert stats["confirmation_delay"]["max"] == 6
    assert stats["confirmation_delay_by_scale"]["2.4"]["count"] == 1
    assert stats["search_truncated"] == 1


def test_pivot_rewrite_counts_detect_removal_and_price_revision():
    previous = [
        {"pivot_key": "p1", "raw_price": 10.0, "extreme_date": "2025-01-02"},
        {"pivot_key": "p2", "raw_price": 12.0, "extreme_date": "2025-01-03"},
        {"pivot_key": "p3", "raw_price": 9.0, "extreme_date": "2025-01-06"},
    ]
    current = [
        {"pivot_key": "p1", "raw_price": 10.0, "extreme_date": "2025-01-02"},
        {"pivot_key": "p2", "raw_price": 12.5, "extreme_date": "2025-01-03"},
        {"pivot_key": "p4", "raw_price": 11.0, "extreme_date": "2025-01-07"},
    ]
    rewrites, new = _pivot_rewrite_counts(previous, current)
    assert rewrites == 2  # p3 removed, p2 price revised
    assert new == 1  # p4 newly confirmed


def test_confirmed_pivots_record_extreme_to_confirmation_delay(bundle):
    request = AnalysisRequest(symbol="AAPL", as_of=bundle.as_of)
    prepared = prepare_bundle(request, bundle)
    indicators = calculate(prepared.bars)
    pivots = _confirmed_pivots(
        prepared.bars,
        prepared.raw_bars,
        indicators.atr,
        Settings().analysis.scales,
        prepared.instrument.tick_size,
    )
    assert pivots
    for pivot in pivots:
        assert pivot["bars_to_confirm"] is None or pivot["bars_to_confirm"] >= 0
        if pivot["bars_to_confirm"] is not None:
            confirm_index = next(
                index
                for index, bar in enumerate(prepared.bars)
                if bar.usable_at.isoformat() >= pivot["confirmed_at"]
            )
            assert pivot["bars_to_confirm"] == confirm_index - pivot["index"]


def test_protocol_rejects_invalid_snapshot_id(tmp_path):
    protocol = {
        "protocol_version": "1",
        "samples": [
            {
                "symbol": "AAPL",
                "asset_type": "EQUITY",
                "source_timeframe": "1d",
                "snapshot_id": "not-a-sha256",
            }
        ],
        "grid": {"start": "2025-01-01", "end": "2025-02-01", "cadence": "EVERY_SESSION"},
        "events": ["CLOSE_BAND_5D_V1"],
    }
    protocol_path = tmp_path / "bad-protocol.json"
    protocol_path.write_text(json.dumps(protocol))
    with pytest.raises(InputError, match="Invalid snapshot_id"):
        evaluate_protocol(protocol_path, tmp_path / "out")


def test_protocol_id_stabilizes_decision_ids_across_subsets(bundle, tmp_path):
    request = AnalysisRequest(symbol="AAPL", as_of=bundle.as_of)
    prepared = prepare_bundle(request, bundle)
    snapshot_id = SnapshotStore(tmp_path / ".cache/elliottwaver").save(prepared)
    base = {
        "protocol_version": "1",
        "protocol_id": "stable-protocol",
        "cache_root": str(tmp_path / ".cache/elliottwaver"),
        "samples": [
            {
                "symbol": "AAPL",
                "asset_type": "EQUITY",
                "source_timeframe": "1d",
                "snapshot_id": snapshot_id,
            }
        ],
        "grid": {
            "start": str(bundle.bars[-20].session_date),
            "end": str(bundle.bars[-10].session_date),
            "cadence": "EVERY_SESSION",
        },
        "events": ["CLOSE_BAND_5D_V1"],
    }
    first = dict(base, note="first")
    second = dict(base, note="second")
    first_path = tmp_path / "first.json"
    second_path = tmp_path / "second.json"
    first_path.write_text(json.dumps(first))
    second_path.write_text(json.dumps(second))
    evaluate_protocol(first_path, tmp_path / "first-out")
    evaluate_protocol(second_path, tmp_path / "second-out")
    first_ids = {
        row["decision_id"]
        for row in map(
            json.loads, (tmp_path / "first-out" / "decisions.jsonl").read_text().splitlines()
        )
    }
    second_ids = {
        row["decision_id"]
        for row in map(
            json.loads, (tmp_path / "second-out" / "decisions.jsonl").read_text().splitlines()
        )
    }
    assert first_ids == second_ids


def _write_close_model(path, approved_model):
    model = approved_model()
    path.write_text(json.dumps(model))
    return model


def _model_protocol(bundle, tmp_path):
    snapshot_id = SnapshotStore(tmp_path / ".cache/elliottwaver").save(bundle)
    protocol = {
        "protocol_version": "1",
        "cache_root": str(tmp_path / ".cache/elliottwaver"),
        "samples": [
            {
                "symbol": "AAPL",
                "asset_type": "EQUITY",
                "source_timeframe": "1d",
                "snapshot_id": snapshot_id,
            }
        ],
        "grid": {
            "start": str(bundle.bars[-40].session_date),
            "end": str(bundle.bars[-10].session_date),
            "cadence": "EVERY_SESSION",
        },
        "events": ["CLOSE_BAND_5D_V1"],
    }
    path = tmp_path / "model-protocol.json"
    path.write_text(json.dumps(protocol))
    return path


def test_evaluation_uses_model_approved_at_decision_time(bundle, tmp_path, approved_model):
    protocol_path = _model_protocol(bundle, tmp_path)
    model_path = tmp_path / "model.json"
    model = _write_close_model(model_path, approved_model)
    approved_at = bundle.bars[-25].usable_at.isoformat()
    registry = ModelRegistry(tmp_path / "registry")
    registry.register(model_path, approved_at=approved_at)

    summary = evaluate_protocol(
        protocol_path, tmp_path / "eval", model_registry=tmp_path / "registry"
    )

    rows = [
        json.loads(line)
        for line in (tmp_path / "eval" / "decisions.jsonl").read_text().splitlines()
    ]
    before = [row for row in rows if row["as_of"] < approved_at]
    after = [row for row in rows if row["as_of"] >= approved_at]
    assert before and after
    assert all(row["model_id"] is None for row in before)
    assert all(row["retrospective"] is False for row in before)
    assert all(row["model_id"] == model["model_id"] for row in after)
    assert all(row["retrospective"] is False for row in after)
    assert all(
        any(
            forecast["event_id"] == "CLOSE_BAND_5D_V1" and forecast["status"] == "APPROVED"
            for forecast in row["forecasts"]
        )
        for row in after
    )
    usage = summary["model_usage"]
    assert usage["study_mode"] == "DECISION_TIME"
    assert usage["decisions_with_model"] == len(after)
    assert usage["retrospective_decisions"] == 0


def test_forced_model_recompute_is_marked_retrospective(bundle, tmp_path, approved_model):
    protocol_path = _model_protocol(bundle, tmp_path)
    model_path = tmp_path / "model.json"
    model = _write_close_model(model_path, approved_model)
    approved_at = bundle.bars[-25].usable_at.isoformat()

    summary = evaluate_protocol(
        protocol_path,
        tmp_path / "retrospective",
        model=model_path,
        model_approved_at=approved_at,
    )
    rows = [
        json.loads(line)
        for line in (tmp_path / "retrospective" / "decisions.jsonl").read_text().splitlines()
    ]
    before = [row for row in rows if row["as_of"] < approved_at]
    after = [row for row in rows if row["as_of"] >= approved_at]
    assert all(row["model_id"] == model["model_id"] for row in rows)
    assert all(row["retrospective"] is True for row in before)
    assert all(row["retrospective"] is False for row in after)
    usage = summary["model_usage"]
    assert usage["study_mode"] == "RETROSPECTIVE"
    assert usage["retrospective_decisions"] == len(before)


def test_forced_model_without_approval_time_is_fully_retrospective(
    bundle, tmp_path, approved_model
):
    protocol_path = _model_protocol(bundle, tmp_path)
    model_path = tmp_path / "model.json"
    _write_close_model(model_path, approved_model)
    summary = evaluate_protocol(protocol_path, tmp_path / "forced", model=model_path)
    assert summary["model_usage"]["study_mode"] == "RETROSPECTIVE"
    assert (
        summary["model_usage"]["retrospective_decisions"]
        == summary["model_usage"]["decisions_with_model"]
    )


def test_evaluate_weekly_grid_and_breakdowns(tmp_path):
    bundle = demo_bundle(timeframe="1w")
    snapshot_id = SnapshotStore(tmp_path / ".cache/elliottwaver").save(bundle)
    protocol = {
        "protocol_version": "1",
        "cache_root": str(tmp_path / ".cache/elliottwaver"),
        "samples": [
            {
                "symbol": "AAPL",
                "asset_type": "EQUITY",
                "source_timeframe": "1w",
                "snapshot_id": snapshot_id,
            }
        ],
        "grid": {
            "start": str(bundle.bars[-30].session_date),
            "end": str(bundle.bars[-10].session_date),
            "cadence": "LAST_SESSION_OF_WEEK",
        },
        "events": [
            "CLOSE_BAND_5W_V1",
            "CLOSE_BAND_20W_V1",
            "CONFIRM_FIRST_8W_V1",
            "TARGET_FIRST_8W_V1",
        ],
    }
    protocol_path = tmp_path / "weekly-protocol.json"
    protocol_path.write_text(json.dumps(protocol))

    summary = evaluate_protocol(protocol_path, tmp_path / "weekly-eval")

    assert summary["analyzable"] > 0
    assert summary["by_timeframe"]["1w"]["analyzable"] == summary["analyzable"]
    assert summary["by_asset_type"]["EQUITY"]["analyzable"] == summary["analyzable"]
    assert summary["by_symbol"]["AAPL"]["analyzable"] == summary["analyzable"]
    assert summary["by_horizon"]
    rows = pq.read_table(tmp_path / "weekly-eval" / "events.parquet").to_pylist()
    assert {row["event_id"] for row in rows} >= {
        "CLOSE_BAND_5W_V1",
        "CLOSE_BAND_20W_V1",
        "CONFIRM_FIRST_8W_V1",
        "TARGET_FIRST_8W_V1",
    }
