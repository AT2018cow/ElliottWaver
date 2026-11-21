import hashlib
import json
import sys
from datetime import UTC, date, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / "scripts"))

import audit_index_target
import compare_index_target
import train_forecasts
from compare_index_target import _label_available, _rolling_states
from train_forecasts import _label

from eliotbot.evaluation.calibration import rolling_logit_offset
from eliotbot.evaluation.features import (
    TARGET_FEATURES,
    event_feature_order,
    feature_vector,
    target_geometry,
)
from eliotbot.schemas.wave import PriceZone


def _scenario(low=100.0, high=104.0):
    return SimpleNamespace(
        target_zones=(
            PriceZone(low=low, high=high, label="T1", methods=("fib",), independent_method_count=1),
        )
    )


def test_target_geometry_distance_inside_and_missing():
    scenario = _scenario()
    assert target_geometry(scenario, 96, 2) == (2.0, 0.0, 0.0)
    assert target_geometry(scenario, 102, 2) == (0.0, 1.0, 0.0)
    assert target_geometry(scenario, 108, 2) == (2.0, 0.0, 0.0)
    assert target_geometry(scenario, 100, 2) == (0.0, 1.0, 0.0)
    assert target_geometry(scenario, 104, 2) == (0.0, 1.0, 0.0)
    assert target_geometry(SimpleNamespace(target_zones=()), 102, 2) == (0.0, 0.0, 1.0)
    assert target_geometry(scenario, 102, 0) == (0.0, 0.0, 1.0)
    invalid = SimpleNamespace(target_zones=(SimpleNamespace(low=104, high=100),))
    assert target_geometry(invalid, 102, 2) == (0.0, 0.0, 1.0)


def test_target_geometry_uses_first_zone_and_mirrors_direction():
    zones = SimpleNamespace(
        target_zones=(
            PriceZone(
                low=100.0,
                high=104.0,
                label="T1",
                methods=("fib",),
                independent_method_count=1,
            ),
            PriceZone(
                low=90.0,
                high=95.0,
                label="T2",
                methods=("fib",),
                independent_method_count=1,
            ),
        )
    )
    assert target_geometry(zones, 98, 2) == (1.0, 0.0, 0.0)
    down = SimpleNamespace(
        target_zones=(
            PriceZone(
                low=96.0,
                high=100.0,
                label="T1",
                methods=("fib",),
                independent_method_count=1,
            ),
        )
    )
    assert target_geometry(down, 108, 2) == (4.0, 0.0, 0.0)
    assert target_geometry(down, 98, 2) == (0.0, 1.0, 0.0)


def test_feature_vector_rejects_missing_v3_target_geometry():
    order = event_feature_order("TARGET_FIRST_20D_V1", "3")
    with pytest.raises(ValueError, match="Missing target geometry feature"):
        feature_vector(order, {}, {})
    candidate = {
        "target_distance_atr": 1.5,
        "inside_target_zone": 0.0,
        "missing_target_geometry": 0.0,
    }
    vector = feature_vector(order, {}, candidate)
    assert vector[-3:] == [1.5, 0.0, 0.0]


def test_feature_version_three_adds_target_geometry_only_to_target_events():
    v2 = event_feature_order("TARGET_FIRST_20D_V1", "2")
    v3 = event_feature_order("TARGET_FIRST_20D_V1", "3")
    assert not set(TARGET_FEATURES) & set(v2)
    assert v3[-len(TARGET_FEATURES) :] == TARGET_FEATURES
    assert not set(TARGET_FEATURES) & set(event_feature_order("CONFIRM_FIRST_20D_V1", "3"))
    with pytest.raises(ValueError, match="Unsupported feature_version"):
        event_feature_order("TARGET_FIRST_20D_V1", "9")


def _rolling_inputs():
    dates = [f"2025-01-{day:02d}" for day in range(1, 13)]
    available = [datetime(2025, 1, day, 10, tzinfo=UTC) for day in range(1, 13)]
    logits = [0.0] * 120
    labels = [1] * 20 + [0] * 100
    # Each date has ten rows; labels are spread across dates.
    decision_dates = [dates[index // 10] for index in range(120)]
    label_available = [available[index // 10] for index in range(120)]
    return logits, labels, decision_dates, label_available


def test_rolling_logit_offset_uses_only_available_labels():
    logits, labels, dates, available = _rolling_inputs()
    before = rolling_logit_offset(
        logits,
        labels,
        dates,
        available,
        datetime(2025, 1, 1, 9, tzinfo=UTC),
    )
    assert before["delta"] == 0.0
    assert before["reason"]
    after = rolling_logit_offset(
        logits,
        labels,
        dates,
        available,
        datetime(2025, 1, 12, 11, tzinfo=UTC),
    )
    assert after["reason"] is None
    assert after["dates"] == 12
    assert after["rows"] == 120
    assert after["delta"] != 0.0
    assert after["last_label_available_at"] == available[-1].isoformat()


def test_rolling_logit_offset_excludes_unavailable_and_missing_rows():
    logits = [0.0] * 12
    labels = [1] * 6 + [0] * 6
    dates = [f"2025-01-{day:02d}" for day in range(1, 13)]
    available = [datetime(2025, 1, day, 10, tzinfo=UTC) for day in range(1, 13)]
    available[5] = None
    result = rolling_logit_offset(
        logits,
        labels,
        dates,
        available,
        datetime(2025, 1, 12, 11, tzinfo=UTC),
        min_dates=1,
        min_rows=1,
        min_positive=1,
        min_negative=1,
    )
    assert result["rows"] == 11
    assert result["dates"] == 11


def test_rolling_offset_waits_for_due_bar_after_early_touch():
    logits = [0.0] * 20
    labels = [1] * 10 + [0] * 10
    dates = ["2025-01-01"] * 10 + ["2025-01-02"] * 10
    # The event touched its target early, but the due bar only becomes usable
    # on 2025-01-05/06; the early touch must not enter the rolling window.
    available = [datetime(2025, 1, 5, 10, tzinfo=UTC)] * 10 + [
        datetime(2025, 1, 6, 10, tzinfo=UTC)
    ] * 10
    before = rolling_logit_offset(
        logits,
        labels,
        dates,
        available,
        datetime(2025, 1, 5, 9, tzinfo=UTC),
        min_dates=1,
        min_rows=1,
        min_positive=1,
        min_negative=1,
    )
    assert before["delta"] == 0.0
    assert before["reason"]
    after = rolling_logit_offset(
        logits,
        labels,
        dates,
        available,
        datetime(2025, 1, 6, 11, tzinfo=UTC),
        min_dates=1,
        min_rows=1,
        min_positive=1,
        min_negative=1,
    )
    assert after["dates"] == 2
    assert after["rows"] == 20


def test_label_timestamp_is_verified_against_the_frozen_due_bar():
    due = date(2025, 1, 31)
    usable_at = datetime(2025, 1, 31, 7, 20, tzinfo=UTC)
    snapshot = SimpleNamespace(bars=[SimpleNamespace(session_date=due, usable_at=usable_at)])
    store = SimpleNamespace(load=lambda snapshot_id: snapshot)
    decision = {"source_snapshot_id": "frozen", "as_of": "2025-01-03T07:20:00+00:00"}
    due_times = {}
    assert _label_available(decision, {}, due, due_times, store) == usable_at.isoformat()
    matching = {"label_available_at": "2025-01-31T15:20:00+08:00"}
    assert _label_available(decision, matching, due, due_times, store) == usable_at.isoformat()
    premature = {"label_available_at": "2025-01-10T07:20:00+00:00"}
    with pytest.raises(ValueError, match="does not match the frozen due bar"):
        _label_available(decision, premature, due, due_times, store)
    assert _label_available(decision, {}, date(2025, 1, 30), due_times, store) is None
    with pytest.raises(ValueError, match="does not match the frozen due bar"):
        _label_available(decision, matching, date(2025, 1, 30), due_times, store)


@pytest.mark.parametrize(
    ("versions", "field", "message"),
    [
        (("3", "3"), None, "requires feature version 2"),
        (("3", "2"), None, "requires feature version 2"),
        (("2", "3"), "engine_version", "engine_version mismatch"),
        (("2", "3"), "config_hash", "config_hash mismatch"),
    ],
)
def test_compare_rejects_mismatched_versions_or_provenance(
    monkeypatch, tmp_path, versions, field, message
):
    left = {"feature_version": versions[0], "engine_version": "engine", "config_hash": "config"}
    right = {**left, "feature_version": versions[1]}
    if field:
        right[field] = "different"
    manifests = {Path("v2"): left, Path("v3"): right}
    monkeypatch.setattr(compare_index_target, "read_evaluation_manifest", manifests.__getitem__)
    with pytest.raises(ValueError, match=message):
        compare_index_target.compare(Path("v2"), Path("v3"), tmp_path / "comparison", "")
    assert not (tmp_path / "comparison").exists()


def test_main_training_pipeline_preserves_candidate_geometry(monkeypatch, tmp_path):
    from eliotbot import ENGINE_VERSION
    from eliotbot.config import Settings

    event_id = "TARGET_FIRST_20D_V1"
    snapshot = "a" * 64
    protocol = {
        "protocol_version": "1",
        "feature_version": "3",
        "samples": [
            {
                "symbol": "000300.SH",
                "asset_type": "INDEX",
                "source_timeframe": "1d",
                "snapshot_id": snapshot,
            },
        ],
        "grid": {"start": "2023-01-01", "end": "2025-12-31", "cadence": "LAST_SESSION_OF_WEEK"},
        "splits": {
            name: {"start": f"{year}-01-01", "end": f"{year}-12-31"}
            for name, year in (("train", 2023), ("calibration", 2024), ("test", 2025))
        },
        "events": [event_id],
        "release": train_forecasts.DEFAULT_RELEASE,
    }
    path = tmp_path / "protocol.json"
    path.write_text(json.dumps(protocol))
    manifest = {
        "protocol": protocol,
        "protocol_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        "engine_version": ENGINE_VERSION,
        "config_hash": Settings().computation_hash,
    }
    decision = {
        "symbol": "000300.SH",
        "market": "CN",
        "asset_type": "INDEX",
        "timeframe": "1d",
        "feed": "sina-index",
        "adjustment": "none",
        "source_snapshot_id": snapshot,
        "decision_date": "2023-01-06",
        "features": {"atr_ratio": 0.02},
    }
    event = {
        "symbol": decision["symbol"],
        "decision_id": "decision",
        "candidate_key": "candidate",
        "event_id": event_id,
        "status": "TARGET_FIRST",
        "due_date": "2023-02-10",
        "features": {"target_distance_atr": 2.5, "invalidation_distance": 1.25},
    }

    def capture(_protocol, _windows, _manifest, _cohort, rows, _output, **_kwargs):
        order = ["target_distance_atr", "invalidation_distance"]
        return {"vector": train_forecasts._features(rows[0], decision, order)}

    monkeypatch.setattr(train_forecasts, "read_evaluation_manifest", lambda _: manifest)
    monkeypatch.setattr(train_forecasts, "_load_rows", lambda _: ({"decision": decision}, [event]))
    monkeypatch.setattr(train_forecasts, "_train_cohort", capture)
    assert train_forecasts.train(path, tmp_path, tmp_path) == {"vector": [2.5, 1.25]}


def test_audit_replays_exact_wave_five_and_counts_only_decided_outcomes(monkeypatch, tmp_path):
    from eliotbot.evaluation import decisions as evaluation

    decision = {
        "symbol": "000300.SH",
        "asset_type": "INDEX",
        "timeframe": "1d",
        "status": "OK",
        "analyzable": True,
        "decision_date": "2025-01-03",
        "as_of": "2025-01-03T07:20:00+00:00",
        "source_snapshot_id": "snapshot",
    }
    events = []
    for key, status in [
        ("expected", "UNDEFINED_EVENT"),
        ("long-third", "UNDEFINED_EVENT"),
        ("unknown", "UNDEFINED_EVENT"),
        ("settled", "TARGET_FIRST"),
        ("incomplete", "INCOMPLETE_DATA"),
        ("censored", "RIGHT_CENSORED"),
        ("ambiguous", "AMBIGUOUS_SAME_BAR"),
    ]:
        events.append(
            {
                "decision_id": "decision",
                "symbol": decision["symbol"],
                "candidate_key": key,
                "event_id": audit_index_target.EVENT,
                "horizon": 20,
                "due_date": "2025-02-07",
                "status": status,
                "features": {
                    "stage": "W5_DEVELOPING",
                    "pattern": "IMPULSE",
                    "missing_invalidation": float(status == "UNDEFINED_EVENT"),
                },
            }
        )
    protocol = {
        "protocol_version": "1",
        "events": [audit_index_target.EVENT],
        "samples": [{"symbol": decision["symbol"], "snapshot_id": "snapshot"}],
        "splits": {"test": {"start": "2025-01-01"}},
        "grid": {"end": "2025-02-20"},
        "cache_root": str(tmp_path / "cache"),
    }
    evaluation_path = tmp_path / "evaluation"
    evaluation._write_artifacts(
        protocol,
        "protocol",
        [{"decision_id": "decision", **decision}],
        events,
        evaluation_path,
        evaluation.Settings(),
        source_sha256="generated",
    )

    def scenario(key, prices):
        return SimpleNamespace(
            scenario_id=key,
            stage="W5_DEVELOPING",
            pattern_type="IMPULSE",
            pattern_direction="UP",
            conditions=(),
            target_zones=(),
            completed_waves=[
                SimpleNamespace(label=str(i), pivot=SimpleNamespace(price=price))
                for i, price in enumerate(prices)
            ],
        )

    scenarios = [
        scenario("expected", [90, 110, 105, 115, 112]),
        scenario("long-third", [90, 110, 105, 130, 120]),
    ]
    context = SimpleNamespace(qualified=scenarios, result=SimpleNamespace(scenarios=[]))
    calls = []
    monkeypatch.setattr(audit_index_target.SnapshotStore, "load", lambda *_: object())
    monkeypatch.setattr(audit_index_target, "prepare_bundle", lambda *_: SimpleNamespace(bars=[]))

    def analyze(_, request, prepared):
        calls.append(request.as_of)
        return context

    monkeypatch.setattr(audit_index_target.AnalysisService, "analyze_with_context", analyze)
    result = audit_index_target.audit(evaluation_path, tmp_path / "audit")
    checks = {row["candidate_key"]: row for row in result["conditional_checks"]}
    assert checks["expected"]["category"] == audit_index_target.CATEGORY_SUSPECTED_MISSING
    assert checks["expected"]["cap_price"] == 122
    assert checks["long-third"]["category"] == audit_index_target.CATEGORY_CONDITIONAL_NOT_TRIGGERED
    assert checks["unknown"]["category"] == audit_index_target.CATEGORY_INSUFFICIENT
    assert checks["unknown"]["scenario_source"] is None
    assert result["settled"] == 1
    assert result["both_conditions"] == 4
    assert result["unresolved"] == {
        "INCOMPLETE_DATA": 1,
        "RIGHT_CENSORED": 1,
        "AMBIGUOUS_SAME_BAR": 1,
    }
    assert all(result["reconciliation"].values())
    assert len(calls) == 2  # One pass for the three candidates and one for the representative.


def test_rolling_offset_future_labels_do_not_change_earlier_state():
    dates = [f"2025-01-{day:02d}" for day in range(1, 21)]
    available = [datetime(2025, 1, day, 10, tzinfo=UTC) for day in range(1, 21)]
    labels = [1] * 60 + [0] * 60 + [1] * 40 + [0] * 40
    logits = [0.0] * 200
    decision_dates = [dates[index // 10] for index in range(200)]
    label_available = [available[index // 10] for index in range(200)]
    cutoff = datetime(2025, 1, 12, 11, tzinfo=UTC)
    before = rolling_logit_offset(logits, labels, decision_dates, label_available, cutoff)
    changed = list(labels)
    for index in range(120, 200):
        changed[index] = 1 - changed[index]
    after = rolling_logit_offset(logits, changed, decision_dates, label_available, cutoff)
    assert before == after
    assert before["window_end"] == "2025-01-12"
    assert before["delta"] != 0.0


def test_ambiguous_same_bar_and_censored_rows_are_not_labels():
    assert _label("TARGET_FIRST_20D_V1", "AMBIGUOUS_SAME_BAR") is None
    assert _label("TARGET_FIRST_20D_V1", "RIGHT_CENSORED") is None
    assert _label("TARGET_FIRST_20D_V1", "TARGET_FIRST") == 1
    assert _label("TARGET_FIRST_20D_V1", "EXPIRED") == 0


def test_rolling_states_are_shared_by_all_candidates_on_one_date():
    release = {
        "rolling_window_dates": 12,
        "rolling_min_dates": 8,
        "rolling_min_rows": 100,
        "rolling_min_positive": 10,
        "rolling_min_negative": 10,
        "rolling_l2": 0.1,
    }
    rows = [
        {
            "decision_date": date(2025, 1, 3),
            "as_of": "2025-01-03T07:20:00+00:00",
            "label_available_at": "2025-01-03T07:20:00+00:00",
            "y": 1,
        },
        {
            "decision_date": date(2025, 1, 3),
            "as_of": "2025-01-03T07:20:00+00:00",
            "label_available_at": "2025-01-03T07:20:00+00:00",
            "y": 0,
        },
    ]
    states = _rolling_states([], rows, [], [0.0, 0.0], release)
    assert list(states) == [date(2025, 1, 3)]


def test_rolling_states_ignore_future_label_changes():
    release = {
        "rolling_window_dates": 12,
        "rolling_min_dates": 8,
        "rolling_min_rows": 100,
        "rolling_min_positive": 10,
        "rolling_min_negative": 10,
        "rolling_l2": 0.1,
    }

    def rows():
        values = []
        for day in range(1, 21):
            for candidate in range(2):
                values.append(
                    {
                        "decision_date": date(2025, 1, day),
                        "as_of": f"2025-01-{day:02d}T07:20:00+00:00",
                        "label_available_at": f"2025-01-{day:02d}T07:20:00+00:00",
                        "symbol": f"S{candidate}",
                        "key": (f"S{candidate}", date(2025, 1, day), f"C{candidate}"),
                        "y": 1 if day <= 6 else 0,
                    }
                )
        return values

    first = rows()
    second = rows()
    for row in second:
        if row["decision_date"] > date(2025, 1, 12):
            row["y"] = 1 - row["y"]
    first_states = _rolling_states([], first, [], [0.0] * len(first), release)
    second_states = _rolling_states([], second, [], [0.0] * len(second), release)
    for day in sorted(first_states):
        if day <= date(2025, 1, 12):
            assert first_states[day] == second_states[day]


def test_rolling_logit_offset_is_deterministic_and_falls_back():
    logits, labels, dates, available = _rolling_inputs()
    first = rolling_logit_offset(
        logits,
        labels,
        dates,
        available,
        datetime(2025, 1, 12, 11, tzinfo=UTC),
    )
    second = rolling_logit_offset(
        list(logits),
        list(labels),
        list(dates),
        list(available),
        datetime(2025, 1, 12, 11, tzinfo=UTC),
    )
    assert first == second
    fallback = rolling_logit_offset(
        logits[:5],
        labels[:5],
        dates[:5],
        available[:5],
        datetime(2025, 1, 12, 11, tzinfo=UTC),
    )
    assert fallback["delta"] == 0.0
    assert fallback["reason"]


def test_rolling_offset_is_case_date_level_not_row_level():
    logits = [0.0] * 20
    labels = [1] * 10 + [0] * 10
    dates = ["2025-01-01"] * 10 + ["2025-01-02"] * 10
    available = [datetime(2025, 1, 1, 10, tzinfo=UTC)] * 10 + [
        datetime(2025, 1, 2, 10, tzinfo=UTC)
    ] * 10
    result = rolling_logit_offset(
        logits,
        labels,
        dates,
        available,
        datetime(2025, 1, 2, 11, tzinfo=UTC),
        min_dates=1,
        min_rows=1,
        min_positive=1,
        min_negative=1,
    )
    assert result["dates"] == 2
    assert result["rows"] == 20


def test_evaluation_manifest_records_feature_version_and_target_columns(tmp_path):
    from eliotbot.data.demo import demo_bundle
    from eliotbot.data.snapshots import SnapshotStore
    from eliotbot.errors import InputError
    from eliotbot.evaluation.decisions import evaluate_protocol, read_evaluation_manifest
    from eliotbot.evaluation.features import event_feature_order

    bundle = demo_bundle()
    snapshot = SnapshotStore(tmp_path / "cache").save(bundle)
    protocol = {
        "protocol_version": "1",
        "protocol_id": "v3-feature-smoke",
        "feature_version": "3",
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
            "start": str(bundle.bars[-20].session_date),
            "end": str(bundle.bars[-10].session_date),
            "cadence": "EVERY_SESSION",
        },
        "events": ["TARGET_FIRST_20D_V1"],
    }
    path = tmp_path / "protocol.json"
    path.write_text(json.dumps(protocol))
    evaluate_protocol(path, tmp_path / "evaluation")
    manifest = read_evaluation_manifest(tmp_path / "evaluation")
    assert manifest["feature_version"] == "3"
    import pyarrow.parquet as pq

    rows = pq.read_table(tmp_path / "evaluation" / "events.parquet").to_pylist()
    assert rows
    assert "target_distance_atr" in rows[0]["features"]
    assert "inside_target_zone" in rows[0]["features"]
    assert "missing_target_geometry" in rows[0]["features"]
    assert event_feature_order("TARGET_FIRST_20D_V1", "3")[-3:] == [
        "target_distance_atr",
        "inside_target_zone",
        "missing_target_geometry",
    ]
    manifest["feature_version"] = "2"
    (tmp_path / "evaluation" / "manifest.json").write_text(json.dumps(manifest))
    with pytest.raises(InputError, match="Unsupported evaluation manifest"):
        read_evaluation_manifest(tmp_path / "evaluation")
