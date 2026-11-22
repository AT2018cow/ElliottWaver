import hashlib
import json
import sys
from datetime import date, timedelta
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / "scripts"))
from train_forecasts import train

from elliottwaver import ENGINE_VERSION
from elliottwaver.config import Settings
from elliottwaver.evaluation.features import FEATURE_VERSION
from elliottwaver.forecast import ModelRegistry, load_model
from elliottwaver.util import digest


def _write_model(path, approved_model, **options):
    model = approved_model(name=path.name, **options)
    path.write_text(json.dumps(model))
    return model["model_id"]


def test_model_registry_register_withdraw_and_fallback(tmp_path, approved_model):
    registry = ModelRegistry(tmp_path / "registry")
    first = tmp_path / "first.json"
    second = tmp_path / "second.json"
    first_id = _write_model(first, approved_model)
    second_id = _write_model(second, approved_model)

    registry.register(first)
    assert registry.load()["model_id"] == first_id
    registry.register(second)
    assert registry.load()["model_id"] == second_id
    registry.withdraw(second_id)
    assert registry.load()["model_id"] == first_id
    registry.withdraw(first_id)
    assert registry.load() is None
    registry.register(first)
    assert registry.load()["model_id"] == first_id


def test_model_registry_falls_back_when_current_file_is_missing(tmp_path, approved_model):
    registry = ModelRegistry(tmp_path / "registry")
    first = tmp_path / "first.json"
    second = tmp_path / "second.json"
    first_id = _write_model(first, approved_model)
    _write_model(second, approved_model)
    registry.register(first)
    registry.register(second)
    second.unlink()
    assert registry.load()["model_id"] == first_id


def test_model_registry_keeps_approval_timeline_and_loads_at_decision_time(
    tmp_path, approved_model
):
    registry = ModelRegistry(tmp_path / "registry")
    first = tmp_path / "first.json"
    second = tmp_path / "second.json"
    first_id = _write_model(first, approved_model)
    second_id = _write_model(second, approved_model)
    registry.register(first, approved_at="2025-01-01T00:00:00+00:00")
    registry.register(second, approved_at="2025-02-01T00:00:00+00:00")
    registry.withdraw(second_id, withdrawn_at="2025-03-01T00:00:00+00:00")

    assert registry.load_at("2024-12-31T00:00:00+00:00") is None
    assert registry.load_at("2025-01-15T00:00:00+00:00")["model_id"] == first_id
    assert registry.load_at("2025-02-15T00:00:00+00:00")["model_id"] == second_id
    assert registry.load_at("2025-03-15T00:00:00+00:00")["model_id"] == first_id
    actions = [(event["model_id"], event["action"]) for event in registry.timeline()]
    assert (first_id, "APPROVED") in actions
    assert (second_id, "APPROVED") in actions
    assert (second_id, "WITHDRAWN") in actions


def test_model_registry_active_at_returns_approval_time(tmp_path, approved_model):
    registry = ModelRegistry(tmp_path / "registry")
    model = tmp_path / "model.json"
    model_id = _write_model(model, approved_model)
    registry.register(model, approved_at="2025-01-01T00:00:00+00:00")
    active = registry.active_at("2025-01-02T00:00:00+00:00")
    assert active["model_id"] == model_id
    assert active["approved_at"] == "2025-01-01T00:00:00+00:00"


def test_model_registry_skips_incompatible_engine_and_uses_previous(tmp_path, approved_model):
    registry = ModelRegistry(tmp_path / "registry")
    first = tmp_path / "first.json"
    second = tmp_path / "second.json"
    first_id = _write_model(first, approved_model, engine="new-engine")
    _write_model(second, approved_model)
    model = json.loads(second.read_text())
    model["engine_version"] = "old-engine"
    model["model_id"] = digest({key: value for key, value in model.items() if key != "model_id"})
    second.write_text(json.dumps(model))
    registry.register(first)
    registry.register(second)

    assert registry.load(engine_version="new-engine")["model_id"] == first_id
    assert registry.load(engine_version="old-engine")["model_id"] == model["model_id"]


def test_load_model_rejects_dimension_mismatch(tmp_path):
    model = {
        "models": {
            "CLOSE_BAND_5D_V1": {
                "status": "APPROVED",
                "feature_order": ["log_return_20"],
                "classes": ["UP", "RANGE", "DOWN"],
                "mean": [0.0],
                "scale": [1.0],
                "weights": [[0.0, 0.0]],
                "intercept": [0.0, 0.0, 0.0],
                "temperature": 1.0,
            }
        }
    }
    model["model_id"] = digest(model)
    path = tmp_path / "bad.json"
    path.write_text(json.dumps(model))
    with pytest.raises(ValueError, match="dimension"):
        load_model(path)


def _gated_model(ci_lower=0.01):
    support = {name: [{"low": 0.2, "high": 0.4}] for name in ("UP", "RANGE", "DOWN")}
    model = {
        "schema_version": "1.1",
        "release": {"min_supported_coverage": 0.5},
        "models": {
            "CLOSE_BAND_5D_V1": {
                "status": "APPROVED",
                "event_version": "V1",
                "reasons": [],
                "feature_order": ["log_return_20"],
                "classes": ["UP", "RANGE", "DOWN"],
                "mean": [0.0],
                "scale": [1.0],
                "weights": [[0.0, 0.0, 0.0]],
                "intercept": [0.0, 0.0, 0.0],
                "temperature": 1.0,
                "support_bins": support,
                "supported_coverage": 0.8,
                "improvement": {
                    name: {
                        "brier_ci": [ci_lower, 0.1],
                        "log_loss_ci": [ci_lower, 0.1],
                    }
                    for name in ("frequency", "momentum", "price")
                },
            }
        },
    }
    model["model_id"] = digest(model)
    return model


def test_load_model_checks_release_gate(tmp_path):
    model = _gated_model()
    path = tmp_path / "gated.json"
    path.write_text(json.dumps(model))
    assert load_model(path)["model_id"] == model["model_id"]

    model["models"]["CLOSE_BAND_5D_V1"]["improvement"]["frequency"]["brier_ci"] = [
        -0.01,
        0.1,
    ]
    model["model_id"] = digest({key: value for key, value in model.items() if key != "model_id"})
    path.write_text(json.dumps(model))
    with pytest.raises(ValueError, match="does not clear zero"):
        load_model(path)


def test_train_forecasts_writes_model_and_summary(tmp_path):
    decisions = []
    events = []
    start = date(2025, 1, 1)
    for index in range(60):
        day = start + timedelta(days=index)
        decision_id = f"d{index}"
        signal = (index % 7) - 3
        decisions.append(
            {
                "decision_id": decision_id,
                "symbol": "AAPL",
                "asset_type": "EQUITY",
                "market": "US",
                "feed": "synthetic",
                "adjustment": "synthetic_no_actions",
                "timeframe": "1d",
                "decision_date": day.isoformat(),
                "as_of": f"{day}T21:00:00+00:00",
                "source_snapshot_id": "a" * 64,
                "input_snapshot_id": "b" * 64,
                "status": "OK",
                "analyzable": True,
                "first_structure_key": f"s{index}",
                "candidate_keys": [f"c{index}"],
                "anchors": [],
                "price_coordinate_id": "p",
                "features": {
                    "log_return_20": signal / 10,
                    "missing_return": 0.0,
                    "atr_ratio": 0.02,
                    "candidate_count": 1,
                    "up_weight": 1.0 if signal > 0 else 0.0,
                    "down_weight": 1.0 if signal < 0 else 0.0,
                    "unknown_weight": 0.0,
                    "top_quality": 0.6,
                    "top_coverage": 0.8,
                    "top2_quality_gap": 0.1,
                    "top_bias": 1.0 if signal > 0 else -1.0,
                },
            }
        )
        events.append(
            {
                "decision_id": decision_id,
                "symbol": "AAPL",
                "as_of": f"{day}T21:00:00+00:00",
                "event_id": "CLOSE_BAND_5D_V1",
                "horizon": 5,
                "due_date": str(day + timedelta(days=7)),
                "candidate_key": None,
                "status": "UP" if signal > 0 else "DOWN" if signal < 0 else "RANGE",
                "session": day.isoformat(),
                "value": 100.0,
                "features": None,
            }
        )
        events.append(
            {
                "decision_id": decision_id,
                "symbol": "AAPL",
                "as_of": f"{day}T21:00:00+00:00",
                "event_id": "CONFIRM_FIRST_20D_V1",
                "horizon": 20,
                "due_date": str(day + timedelta(days=10)),
                "candidate_key": f"c{index}",
                "status": "CONFIRMED_FIRST" if signal > 0 else "INVALIDATED_FIRST",
                "session": day.isoformat(),
                "value": None,
                "features": {
                    "quality": 0.6,
                    "coverage": 0.8,
                    "bias": 1.0 if signal > 0 else -1.0,
                    "pattern": "IMPULSE",
                    "stage": "COMPLETE",
                    "confirm_distance": 1.0,
                    "invalidation_distance": 2.0,
                },
            }
        )
    with (tmp_path / "decisions.jsonl").open("w") as stream:
        for row in decisions:
            stream.write(json.dumps(row) + "\n")
    pq.write_table(pa.Table.from_pylist(events), tmp_path / "events.parquet")
    protocol = {
        "protocol_version": "1",
        "samples": [
            {
                "symbol": "AAPL",
                "source_timeframe": "1d",
                "asset_type": "EQUITY",
                "snapshot_id": "a" * 64,
            }
        ],
        "grid": {"start": "2025-01-01", "end": "2025-03-01", "cadence": "EVERY_SESSION"},
        "events": ["CLOSE_BAND_5D_V1", "CONFIRM_FIRST_20D_V1"],
        "release": {},
        "splits": {
            "train": {"start": "2025-01-01", "end": "2025-01-20"},
            "calibration": {"start": "2025-01-21", "end": "2025-02-10"},
            "test": {"start": "2025-02-11", "end": "2025-03-01"},
        },
    }
    protocol_path = tmp_path / "protocol.json"
    protocol_path.write_text(json.dumps(protocol))

    with pytest.raises(FileNotFoundError):
        train(protocol_path, tmp_path, tmp_path / "models")
    (tmp_path / "summary.json").write_text("{}")
    files = {
        name: hashlib.sha256((tmp_path / name).read_bytes()).hexdigest()
        for name in ("summary.json", "decisions.jsonl", "events.parquet")
    }
    (tmp_path / "manifest.json").write_text(
        json.dumps(
            {
                "schema_version": "1.1",
                "feature_version": FEATURE_VERSION,
                "protocol": protocol,
                "protocol_sha256": hashlib.sha256(protocol_path.read_bytes()).hexdigest(),
                "config_hash": Settings().computation_hash,
                "engine_version": ENGINE_VERSION,
                "files": files,
            }
        )
    )

    summary = train(protocol_path, tmp_path, tmp_path / "models")

    assert (tmp_path / "models" / "model.json").exists()
    assert (tmp_path / "models" / "summary.json").exists()
    assert (tmp_path / "models" / "manifest.json").exists()
    assert summary["model_id"]
    assert summary["release"]["min_bin_count"] == 50
    assert summary["release"]["bootstrap_repetitions"] == 2000
    assert "CLOSE_BAND_5D_V1" in summary["models"]
    entry = summary["models"]["CLOSE_BAND_5D_V1"]
    assert entry["status"] == "INSUFFICIENT_EVIDENCE"
    assert entry["reasons"]
    assert set(entry["baselines"]) == {"frequency", "momentum", "price"}
    assert entry["improvement"]["frequency"]["brier_ci"] is not None
    assert entry["improvement"]["momentum"]["brier_ci"] is not None
    model = json.loads((tmp_path / "models" / "model.json").read_text())
    assert model["schema_version"] == "1.2"
    assert entry["sample_counts"] == {"train": 13, "calibration": 14, "test": 12}
    assert model["release"]["reliability_bins"] == [0.0, 0.2, 0.4, 0.6, 0.8, 1.0]
    assert model["engine_version"]
    assert model["config_hash"]
    assert model["scope"]["asset_types"] == ["EQUITY"]
