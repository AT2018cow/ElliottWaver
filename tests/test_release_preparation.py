import hashlib
import json
import subprocess
import sys
from datetime import UTC, datetime, time, timedelta
from pathlib import Path

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from eliotbot import ENGINE_VERSION
from eliotbot.config import Settings
from eliotbot.evaluation.calibration import binary_logit_affine
from eliotbot.evaluation.features import FEATURE_VERSION, event_feature_order
from eliotbot.forecast import ModelRegistry, load_model
from eliotbot.util import canonical_json, digest
from scripts.review_wave_cases import _pattern_checks
from scripts.train_forecasts import DEFAULT_RELEASE

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "prepare_index_target.py"
EVENT = "TARGET_FIRST_20D_V1"


def test_affine_calibration_fits_base_rate_with_constant_log_odds():
    temperature, bias = binary_logit_affine(np.zeros(4), np.array([1, 0, 1, 1]))
    assert temperature == 1
    assert bias == pytest.approx(np.log(3.0))


@pytest.mark.parametrize("direction", [1, -1])
@pytest.mark.parametrize("overlap", [False, True])
def test_independent_impulse_overlap_rule(direction, overlap):
    prices = [100, 110, 106, 121, 109 if overlap else 113, 126]
    prices = prices if direction > 0 else [230 - price for price in prices]
    signed = [direction * price for price in prices]
    lows = [min(a, b) for a, b in zip(signed[:-1], signed[1:], strict=True)]
    rules = _pattern_checks(signed, [0, 2, 4, 6, 8, 10], lows)["IMPULSE"]
    overlap_rule = next(rule for rule in rules if rule["rule"] == "W4_NO_OVERLAP")
    assert overlap_rule["status"] == ("FAIL" if overlap else "PASS")


def _fixture(tmp_path, retrospective, strong=False):
    scope = {
        "markets": ["CN"],
        "asset_types": ["INDEX"],
        "timeframes": ["1d"],
        "feed": ["sina-index"],
        "adjustment": ["none"],
    }
    order = event_feature_order(EVENT)
    params = {
        "mean": [0.0] * len(order),
        "scale": [1.0] * len(order),
        "weights": [[0.0, 0.0] for _ in order],
        "intercept": [0.0, 0.0],
        "temperature": 1.0,
    }
    release = {**DEFAULT_RELEASE, "bootstrap_repetitions": 3}
    candidate = {
        "schema_version": "1.0",
        "stage": "FROZEN_CANDIDATE",
        "event_id": EVENT,
        "frozen_at": "2025-02-01T12:00:00+00:00",
        "scope": scope,
        "feature_order": order,
        "release": release,
        "symbols": ["000905.SH"],
        "allow_gaps": False,
        "engine_version": ENGINE_VERSION,
        "feature_version": FEATURE_VERSION,
        "config_hash": Settings().computation_hash,
        "development_counts": {"train": 100, "calibration": 100},
        "development_splits": {
            "train": {"start": "2023-01-01", "end": "2023-12-31"},
            "calibration": {"start": "2024-01-01", "end": "2024-12-31"},
        },
        "parameters": params,
        "baselines": {
            "frequency": {"probabilities": [0.5, 0.5]},
            **{key: {**params, "feature_order": order} for key in ("momentum", "price")},
        },
    }
    if strong:
        candidate["symbols"] = ["000016.SH", "000688.SH", "000852.SH", "000905.SH", "399001.SZ"]
        indicator = order.index("quality")
        weights = [[0.0, 0.0] for _ in order]
        weights[indicator][1] = 18.0
        candidate["parameters"] = {**params, "weights": weights, "intercept": [0.0, -9.0]}
    candidate["candidate_id"] = digest(candidate)
    candidate_path = tmp_path / "candidate.json"
    candidate_path.write_text(canonical_json(candidate))
    eval_path = tmp_path / "evaluation"
    eval_path.mkdir()
    first = datetime(2024 if retrospective else 2025, 3, 7, tzinfo=UTC).date()
    samples = [
        {
            "symbol": symbol,
            "asset_type": "INDEX",
            "source_timeframe": "1d",
            "snapshot_id": f"{index + 1:064x}",
        }
        for index, symbol in enumerate(candidate["symbols"])
    ]
    decisions, events = [], []
    duration = 30 if strong else 2
    for index in range(duration):
        day = first + timedelta(days=7 * index)
        as_of = datetime.combine(day, time(hour=8), tzinfo=UTC).isoformat()
        for position, sample in enumerate(samples):
            positive = (index + position) % 2 == 0
            identity = f"{index}-{position}"
            decisions.append(
                {
                    "symbol": sample["symbol"],
                    "asset_type": "INDEX",
                    "market": "CN",
                    "timeframe": "1d",
                    "feed": "sina-index",
                    "adjustment": "none",
                    "source_snapshot_id": sample["snapshot_id"],
                    "decision_id": identity,
                    "decision_date": day.isoformat(),
                    "as_of": as_of,
                    "features": {"log_return_20": 0.0},
                }
            )
            events.append(
                {
                    "symbol": sample["symbol"],
                    "event_id": EVENT,
                    "status": "TARGET_FIRST" if positive else "EXPIRED",
                    "due_date": (day + timedelta(days=30)).isoformat(),
                    "as_of": as_of,
                    "decision_id": identity,
                    "candidate_key": identity,
                    "features": {"pattern": "IMPULSE", "quality": float(positive)},
                }
            )
    cutoff = (first + timedelta(days=7 * (duration - 1) + 31)).isoformat()
    (eval_path / "decisions.jsonl").write_text(
        "".join(canonical_json(row) + "\n" for row in decisions)
    )
    pq.write_table(pa.Table.from_pylist(events), eval_path / "events.parquet")
    (eval_path / "summary.json").write_text("{}")
    protocol = {
        "samples": samples,
        "events": [EVENT],
        "release": release,
        "grid": {
            "start": first.isoformat(),
            "end": cutoff,
            "outcome_as_of": cutoff,
            "cadence": "LAST_SESSION_OF_WEEK",
        },
    }
    manifest = {
        "schema_version": "1.1",
        "protocol": protocol,
        "samples": samples,
        "feature_version": FEATURE_VERSION,
        "engine_version": ENGINE_VERSION,
        "source_sha256": "c" * 64,
        "config_hash": candidate["config_hash"],
        "protocol_sha256": digest(protocol),
        "files": {
            path.name: hashlib.sha256(path.read_bytes()).hexdigest() for path in eval_path.iterdir()
        },
    }
    (eval_path / "manifest.json").write_text(canonical_json(manifest))
    return candidate_path, eval_path


@pytest.mark.parametrize("retrospective", [True, False])
def test_frozen_candidate_score_refuses_old_rows_and_reports_insufficient_new_evidence(
    tmp_path, retrospective
):
    candidate, evaluation = _fixture(tmp_path, retrospective)
    destination = tmp_path / "scored"
    result = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "score",
            "--candidate",
            str(candidate),
            "--holdout",
            str(evaluation),
            "--output-dir",
            str(destination),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if retrospective:
        assert result.returncode != 0
        assert "Holdout must start after freezing" in result.stderr
        assert not destination.exists()
    else:
        assert result.returncode == 0, result.stderr
        assert json.loads(result.stdout)["status"] == "INSUFFICIENT_EVIDENCE"
        path = destination / "model.json"
        assert load_model(path, operational=True)["models"][EVENT]["test_rows"] == 2
        with pytest.raises(ValueError, match="no APPROVED event"):
            ModelRegistry(tmp_path / "registry").register(path)


def test_frozen_candidate_can_pass_and_is_registered_only_for_validated_indices(tmp_path):
    candidate, evaluation = _fixture(tmp_path, retrospective=False, strong=True)
    output = tmp_path / "scored"
    result = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "score",
            "--candidate",
            str(candidate),
            "--holdout",
            str(evaluation),
            "--output-dir",
            str(output),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["status"] == "APPROVED"
    registry = ModelRegistry(tmp_path / "registry")
    registry.register(output / "model.json", approved_at="2026-01-01T00:00:00+00:00")
    assert registry.active_at("2026-01-02T00:00:00+00:00", symbol="000905.SH")
    assert registry.active_at("2026-01-02T00:00:00+00:00", symbol="000300.SH") is None
