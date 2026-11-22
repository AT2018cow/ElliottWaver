"""Freeze and score the CN daily target-first candidate using separate evidence."""

import argparse
import json
import tempfile
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
from train_forecasts import (
    MOMENTUM_FEATURES,
    _features,
    _fit_model,
    _frequency_baseline,
    _label,
    _load_rows,
    _price_order,
    _release,
    _serialize_parameters,
    _split_rows,
    forecast_evidence,
)

from elliottwaver.evaluation.calibration import brier, log_loss, predict
from elliottwaver.evaluation.decisions import read_evaluation_manifest, split_windows
from elliottwaver.evaluation.features import (
    FEATURE_VERSION,
    PATTERNS,
    STAGES,
    event_feature_order,
)
from elliottwaver.forecast import validate_model
from elliottwaver.util import atomic_write, canonical_json, digest

EVENT = "TARGET_FIRST_20D_V1"
CLASSES = ["NO", "YES"]
CADENCE = "LAST_SESSION_OF_WEEK"
SCOPE = {
    "markets": ["CN"],
    "asset_types": ["INDEX"],
    "timeframes": ["1d"],
    "feed": ["sina-index"],
    "adjustment": ["none"],
}
METHODS = ("temperature", "binary_logit_affine")
DEVELOPMENT_END = date(2025, 12, 31)
FOLDS = (
    ("2023-12-31", "2024-06-30", "2024-12-31"),
    ("2024-06-30", "2024-12-31", "2025-06-30"),
    ("2024-12-31", "2025-06-30", "2025-12-31"),
)


def _session_day(timestamp: datetime) -> date:
    return timestamp.astimezone(ZoneInfo("Asia/Shanghai")).date()


def _matrix(rows: list[dict], order: list[str]) -> np.ndarray:
    return np.asarray([_features(row, row["decision"], order) for row in rows], dtype=float)


def _outcomes(rows: list[dict]) -> np.ndarray:
    return np.asarray([row["y"] for row in rows], dtype=int)


def _freeze_json(destination: Path, value: dict) -> None:
    """Link a complete file into place while preserving any existing freeze."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=destination.parent) as temporary:
        staged = Path(temporary) / "artifact.json"
        atomic_write(staged, canonical_json(value) + "\n")
        destination.hardlink_to(staged)


def _rows(directory: Path, manifest: dict, symbols: set[str], start: date, end: date) -> list[dict]:
    decisions, events = _load_rows(directory)
    scoped = {
        key: row
        for key, row in decisions.items()
        if row["symbol"] in symbols
        and row["asset_type"] == "INDEX"
        and date.fromisoformat(row["decision_date"]) >= start
        and date.fromisoformat(row["decision_date"]) <= end
    }
    if {row["symbol"] for row in scoped.values()} != symbols:
        raise ValueError("Evaluation does not cover every frozen index")
    snapshots = {
        row["symbol"]: row["snapshot_id"] for row in manifest["samples"] if row["symbol"] in symbols
    }
    rows = []
    seen = set()
    for event in events:
        decision = scoped.get(event["decision_id"])
        if decision is None or event["event_id"] != EVENT or not event["due_date"]:
            continue
        day = date.fromisoformat(decision["decision_date"])
        due = date.fromisoformat(event["due_date"])
        if not day < due <= end:
            continue
        if (
            decision["market"] != "CN"
            or decision["timeframe"] != "1d"
            or decision["feed"] != "sina-index"
            or decision["adjustment"] != "none"
            or event["symbol"] != decision["symbol"]
            or event["as_of"] != decision["as_of"]
            or decision["source_snapshot_id"] != snapshots[decision["symbol"]]
            or not event["candidate_key"]
            or not decision.get("features")
            or not event.get("features")
        ):
            raise ValueError("Invalid index target-first decision or candidate identity")
        identity = event["decision_id"], event["candidate_key"]
        if identity in seen:
            raise ValueError("Duplicate index target-first event")
        seen.add(identity)
        value = _label(EVENT, event["status"])
        if value is not None:
            rows.append(
                {
                    "decision_date": day,
                    "due_date": due,
                    "symbol": event["symbol"],
                    "decision": decision,
                    "features": event["features"],
                    "y": value,
                }
            )
    return rows


def _fit(
    train: list[dict],
    calibration: list[dict],
    evaluation: list[dict],
    order: list[str],
    method: str,
):
    train_y, calibration_y = _outcomes(train), _outcomes(calibration)
    if set(train_y) != {0, 1} or set(calibration_y) != {0, 1} or not evaluation:
        raise ValueError("Calibration comparison requires both outcomes and non-empty splits")
    probabilities, params = _fit_model(
        _matrix(train, order),
        train_y,
        _matrix(calibration, order),
        calibration_y,
        _matrix(evaluation, order),
        2,
        calibration_method=method,
    )
    return probabilities, _serialize_parameters(params)


def freeze(directory: Path, destination: Path) -> dict:
    manifest = read_evaluation_manifest(directory)
    protocol = manifest["protocol"]
    feature_version = str(protocol.get("feature_version", FEATURE_VERSION))
    if (
        protocol["grid"]["cadence"] != CADENCE
        or EVENT not in protocol["events"]
        or manifest["feature_version"] != feature_version
    ):
        raise ValueError("Incompatible development evaluation")
    windows = split_windows(protocol)
    start, _ = windows["train"]
    if windows["calibration"][1] != DEVELOPMENT_END or start != date(2023, 1, 1):
        raise ValueError("Expected the frozen 2023–2025 development window")
    symbols = {row["symbol"] for row in protocol["samples"] if row["asset_type"] == "INDEX"}
    rows = _rows(directory, manifest, symbols, start, DEVELOPMENT_END)
    order = event_feature_order(EVENT, feature_version)
    results = {method: [] for method in METHODS}
    for ends in FOLDS:
        a, b, c = (date.fromisoformat(day) for day in ends)
        split = _split_rows(
            rows,
            {
                "train": (start, a),
                "calibration": (a + timedelta(days=1), b),
                "test": (b + timedelta(days=1), c),
            },
        )
        y = _outcomes(split["test"])
        for method in METHODS:
            probabilities, _ = _fit(
                split["train"], split["calibration"], split["test"], order, method
            )
            results[method].append(
                {
                    "test_end": c.isoformat(),
                    "rows": len(y),
                    "log_loss": log_loss(probabilities, y),
                    "brier": brier(probabilities, y),
                    "mean_prediction": float(probabilities[:, 1].mean()),
                    "actual_rate": float(y.mean()),
                }
            )

    def pooled_loss(method):
        folds = results[method]
        return sum(row["rows"] * row["log_loss"] for row in folds) / sum(
            row["rows"] for row in folds
        )

    selected = min(METHODS, key=pooled_loss)
    splits = {name: windows[name] for name in ("train", "calibration")}
    parts = _split_rows(rows, splits)
    train, calibration = parts["train"], parts["calibration"]
    _, parameters = _fit(train, calibration, calibration, order, selected)
    baselines = {"frequency": {"probabilities": _frequency_baseline(_outcomes(train), 2).tolist()}}
    for name, columns in (
        ("momentum", MOMENTUM_FEATURES),
        ("price", _price_order(EVENT, feature_version)),
    ):
        _, params = _fit(train, calibration, calibration, columns, "temperature")
        baselines[name] = {**params, "feature_order": columns}
    record = {
        "schema_version": "1.0",
        "stage": "FROZEN_CANDIDATE",
        "event_id": EVENT,
        "frozen_at": datetime.now(UTC).isoformat(),
        "selected_method": selected,
        "development_validation": results,
        "development_protocol_sha256": manifest["protocol_sha256"],
        "development_files": manifest["files"],
        "development_source_sha256": manifest["source_sha256"],
        "engine_version": manifest["engine_version"],
        "feature_version": feature_version,
        "config_hash": manifest["config_hash"],
        "scope": SCOPE,
        "symbols": sorted(symbols),
        "development_splits": {name: protocol["splits"][name] for name in splits},
        "development_counts": {name: len(values) for name, values in parts.items()},
        "release": _release(protocol),
        "feature_order": order,
        "allow_gaps": protocol.get("allow_gaps", False),
        "parameters": parameters,
        "baselines": baselines,
    }
    record["candidate_id"] = digest(record)
    if destination.exists():
        raise FileExistsError(f"Frozen candidate already exists: {destination}")
    _freeze_json(destination, record)
    return record


def _predict_frozen(rows: list[dict], parameters: dict, order: list[str]) -> np.ndarray:
    x = (_matrix(rows, order) - np.asarray(parameters["mean"])) / np.asarray(parameters["scale"])
    return predict(
        x,
        np.asarray(parameters["weights"]),
        np.asarray(parameters["intercept"]),
        parameters["temperature"],
    )


def score(candidate_file: Path, directory: Path, output_dir: Path) -> dict:
    candidate = json.loads(candidate_file.read_text())
    if not isinstance(candidate, dict):
        raise ValueError("Frozen candidate must be a JSON object")
    candidate_id = candidate.pop("candidate_id", None)
    feature_version = str(candidate.get("feature_version", FEATURE_VERSION))
    if (
        candidate_id != digest(candidate)
        or candidate["stage"] != "FROZEN_CANDIDATE"
        or candidate["schema_version"] != "1.0"
        or candidate["event_id"] != EVENT
        or candidate["scope"] != SCOPE
        or candidate["feature_order"] != event_feature_order(EVENT, feature_version)
    ):
        raise ValueError("Invalid frozen candidate")
    manifest = read_evaluation_manifest(directory)
    protocol = manifest["protocol"]
    grid = protocol["grid"]
    frozen = datetime.fromisoformat(candidate["frozen_at"])
    start, stop = (date.fromisoformat(grid[key]) for key in ("start", "end"))
    cutoff = date.fromisoformat(grid["outcome_as_of"])
    if (
        not _session_day(frozen) < start <= stop <= cutoff <= _session_day(datetime.now(UTC))
        or protocol["events"] != [EVENT]
        or grid["cadence"] != CADENCE
        or protocol.get("allow_gaps", False) != candidate["allow_gaps"]
        or _release(protocol) != candidate["release"]
        or any(
            manifest[key] != candidate[key]
            for key in ("engine_version", "feature_version", "config_hash")
        )
        or sorted(row["symbol"] for row in protocol["samples"]) != candidate["symbols"]
        or any(
            row["asset_type"] != "INDEX" or row["source_timeframe"] != "1d"
            for row in protocol["samples"]
        )
    ):
        raise ValueError("Holdout must start after freezing and match the frozen evaluation scope")
    rows = [
        row
        for row in _rows(directory, manifest, set(candidate["symbols"]), start, cutoff)
        if row["decision_date"] <= stop
    ]
    if not rows:
        raise ValueError("No mature prospective holdout events")
    base = candidate["baselines"]
    comparison = {"frequency": np.tile(base["frequency"]["probabilities"], (len(rows), 1))}
    for name in ("momentum", "price"):
        comparison[name] = _predict_frozen(rows, base[name], base[name]["feature_order"])
    evidence = forecast_evidence(
        EVENT,
        rows,
        CLASSES,
        _predict_frozen(rows, candidate["parameters"], candidate["feature_order"]),
        comparison,
        candidate["release"],
        CADENCE,
    )
    baseline_metrics = evidence.pop("baseline_metrics")
    model = {
        "schema_version": "1.2",
        "feature_version": feature_version,
        "event_versions": {EVENT: "V1"},
        "scope": {**SCOPE, "symbols": candidate["symbols"]},
        "engine_version": candidate["engine_version"],
        "config_hash": candidate["config_hash"],
        "splits": {
            **candidate["development_splits"],
            "test": {"start": str(start), "end": str(cutoff)},
        },
        "release": candidate["release"],
        "protocol_sha256": manifest["protocol_sha256"],
        "decisions_sha256": manifest["files"]["decisions.jsonl"],
        "events_sha256": manifest["files"]["events.parquet"],
        "frozen_candidate_id": candidate_id,
        "holdout_source_sha256": manifest["source_sha256"],
        "models": {
            EVENT: {
                "event_id": EVENT,
                "event_version": "V1",
                "classes": CLASSES,
                "feature_order": candidate["feature_order"],
                "encoding": {"patterns": PATTERNS, "stages": STAGES},
                **candidate["parameters"],
                **evidence,
                "sample_counts": {**candidate["development_counts"], "test": len(rows)},
                "baselines": {
                    name: {**parameters, "metrics": baseline_metrics[name]}
                    for name, parameters in base.items()
                },
            }
        },
    }
    validate_model(model, operational=True)
    model["model_id"] = digest(model)
    destination = output_dir / "model.json"
    try:
        _freeze_json(destination, model)
    except FileExistsError:
        if json.loads(destination.read_text()) != model:
            raise
    return model


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    trainer = commands.add_parser("freeze")
    trainer.add_argument("--development", type=Path, required=True)
    trainer.add_argument("--output", type=Path, required=True)
    validator = commands.add_parser("score")
    validator.add_argument("--candidate", type=Path, required=True)
    validator.add_argument("--holdout", type=Path, required=True)
    validator.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    if args.command == "freeze":
        record = freeze(args.development, args.output)
        print(
            canonical_json(
                {
                    key: record[key]
                    for key in (
                        "candidate_id",
                        "selected_method",
                        "frozen_at",
                        "development_validation",
                    )
                }
            )
        )
    else:
        model = score(args.candidate, args.holdout, args.output_dir)
        print(
            canonical_json(
                {"model_id": model["model_id"], "status": model["models"][EVENT]["status"]}
            )
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
