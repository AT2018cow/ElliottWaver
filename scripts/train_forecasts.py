"""Train and calibrate fixed-horizon forecast models from evaluation rows."""

from __future__ import annotations

import argparse
import hashlib
import json
from bisect import bisect_left
from collections import Counter, defaultdict
from datetime import date
from pathlib import Path

import numpy as np
import pyarrow.parquet as pq

from eliotbot import ENGINE_VERSION
from eliotbot.config import Settings
from eliotbot.evaluation.calibration import (
    block_bootstrap_paired_improvement,
    bootstrap_reliability,
    brier,
    fit_softmax,
    log_loss,
    predict,
    reliability,
    standardize,
    temperature_scale,
)
from eliotbot.evaluation.decisions import _load_protocol, read_evaluation_manifest, split_windows
from eliotbot.evaluation.features import (
    FEATURE_VERSION,
    PATTERNS,
    STAGES,
    event_feature_order,
    feature_vector,
)
from eliotbot.util import atomic_write, canonical_json, digest

CLOSE_EVENTS = {
    "CLOSE_BAND_5D_V1",
    "CLOSE_BAND_20D_V1",
    "CLOSE_BAND_5W_V1",
    "CLOSE_BAND_20W_V1",
}
CANDIDATE_EVENTS = {
    "CONFIRM_FIRST_20D_V1",
    "TARGET_FIRST_20D_V1",
    "CONFIRM_FIRST_8W_V1",
    "TARGET_FIRST_8W_V1",
}
PRICE_FEATURES = ["log_return_20", "missing_return", "atr_ratio"]
MOMENTUM_FEATURES = ["log_return_20"]
DEFAULT_RELEASE = {
    "reliability_bins": [0.0, 0.2, 0.4, 0.6, 0.8, 1.0],
    "min_bin_count": 50,
    "min_bin_dates": 10,
    "reliability_tolerance": 0.10,
    "min_test_rows": 50,
    "min_test_dates": 10,
    "min_test_symbols": 5,
    "min_supported_coverage": 0.5,
    "bootstrap_repetitions": 2000,
    "bootstrap_seed": 20261119,
}


def _load_rows(directory: Path) -> tuple[dict[str, dict], list[dict]]:
    decisions = {}
    with (directory / "decisions.jsonl").open() as stream:
        for line in stream:
            row = json.loads(line)
            if row.get("decision_id"):
                if row["decision_id"] in decisions:
                    raise ValueError("Duplicate evaluation decision")
                decisions[row["decision_id"]] = row
    events = pq.read_table(directory / "events.parquet").to_pylist()
    return decisions, events


def _price_order(event_id: str) -> list[str]:
    order = list(PRICE_FEATURES)
    if event_id in CANDIDATE_EVENTS:
        order += ["confirm_distance", "invalidation_distance"]
    return order


def _features(event: dict, decision: dict, feature_order: list[str]) -> list[float]:
    return feature_vector(feature_order, decision["features"], event.get("features"))


def _label(event_id: str, status: str) -> int | None:
    if event_id in CLOSE_EVENTS:
        return {"UP": 0, "RANGE": 1, "DOWN": 2}.get(status)
    if event_id in CANDIDATE_EVENTS:
        if status in {"CONFIRMED_FIRST", "TARGET_FIRST"}:
            return 1
        if status in {"INVALIDATED_FIRST", "EXPIRED"}:
            return 0
    return None


def _event_horizon(event_id: str) -> int:
    if "20D" in event_id or "20W" in event_id:
        return 20
    if "8W" in event_id:
        return 8
    return 5


def _block_size(event_id: str, cadence: str) -> int:
    horizon = _event_horizon(event_id)
    if event_id.endswith("D_V1") and cadence == "LAST_SESSION_OF_WEEK":
        return max(1, (horizon + 4) // 5)
    return horizon


def _split_rows(rows, windows):
    return {
        name: [row for row in rows if start <= row["decision_date"] <= row["due_date"] <= end]
        for name, (start, end) in windows.items()
    }


def _frequency_baseline(train_y: np.ndarray, n_classes: int) -> np.ndarray:
    counts = np.bincount(train_y, minlength=n_classes).astype(float)
    return counts / max(1.0, counts.sum())


def _fit_model(
    x_train: np.ndarray,
    y_train: np.ndarray,
    x_cal: np.ndarray,
    y_cal: np.ndarray,
    x_test: np.ndarray,
    n_classes: int,
) -> tuple[np.ndarray, dict]:
    x_train_s, x_cal_s, stats = standardize(x_train, x_cal)
    x_test_s = (x_test - stats[0]) / stats[1]
    weights, intercept = fit_softmax(x_train_s, y_train, n_classes)
    cal_prob = predict(x_cal_s, weights, intercept)
    temperature = temperature_scale(cal_prob, y_cal)
    test_prob = predict(x_test_s, weights, intercept, temperature)
    return test_prob, {
        "mean": stats[0],
        "scale": stats[1],
        "weights": weights,
        "intercept": intercept,
        "temperature": temperature,
    }


def _metrics(probabilities: np.ndarray, y: np.ndarray) -> dict:
    return {
        "brier": brier(probabilities, y),
        "log_loss": log_loss(probabilities, y),
        "reliability": reliability(probabilities, y),
    }


def _release(protocol: dict) -> dict:
    release = dict(DEFAULT_RELEASE)
    release.update(protocol.get("release", {}))
    release["reliability_bins"] = [float(value) for value in release["reliability_bins"]]
    return release


COHORT_KEYS = ("market", "asset_type", "timeframe", "feed", "adjustment")
SCOPE_KEYS = ("markets", "asset_types", "timeframes", "feed", "adjustment")


def _bin_supported(value: float, supported: list[dict]) -> bool:
    for item in supported:
        if item["low"] <= value < item["high"] or (item["high"] == 1.0 and value == 1.0):
            return True
    return False


def _annotate_reliability(
    rows: list[dict], classes: list[str], release: dict
) -> dict[str, list[dict]]:
    support: dict[str, list[dict]] = {}
    for class_index, class_name in enumerate(classes):
        supported = []
        for row in rows:
            if row["class_index"] != class_index:
                continue
            ci = row["difference_ci"]
            if (
                row["count"] < release["min_bin_count"]
                or row["dates"] < release["min_bin_dates"]
                or row["occupied_blocks"] < 2
            ):
                row["status"] = "INSUFFICIENT_SUPPORT"
            elif (
                ci is None
                or ci[0] < -release["reliability_tolerance"]
                or ci[1] > release["reliability_tolerance"]
            ):
                row["status"] = "MISCALIBRATED"
            else:
                row["status"] = "SUPPORTED"
                supported.append(
                    {
                        "low": row["low"],
                        "high": row["high"],
                        "count": row["count"],
                        "dates": row["dates"],
                        "occupied_blocks": row["occupied_blocks"],
                        "calibration_gap_ci": ci,
                    }
                )
        support[class_name] = supported
    return support


def _supported_coverage(
    probabilities: np.ndarray, support: dict[str, list[dict]], classes: list[str]
) -> float:
    if len(probabilities) == 0:
        return 0.0
    covered = []
    for row in probabilities:
        covered.append(
            all(
                _bin_supported(float(row[index]), support[class_name])
                for index, class_name in enumerate(classes)
            )
        )
    return float(np.mean(covered))


def _approval_reasons(
    *,
    event_id: str,
    test_rows: list[dict],
    classes: list[str],
    support: dict[str, list[dict]],
    coverage: float,
    improvements: dict[str, dict],
    release: dict,
) -> list[str]:
    reasons = []
    test_dates = len({row["decision_date"] for row in test_rows})
    test_symbols = len({row["symbol"] for row in test_rows})
    if len(test_rows) < release["min_test_rows"]:
        reasons.append(f"test rows {len(test_rows)} < {release['min_test_rows']}")
    if test_dates < release["min_test_dates"]:
        reasons.append(f"test dates {test_dates} < {release['min_test_dates']}")
    if test_symbols < release["min_test_symbols"]:
        reasons.append(f"test symbols {test_symbols} < {release['min_test_symbols']}")
    for name in ("frequency", "momentum", "price"):
        improvement = improvements[name]
        if improvement["brier"] <= 0:
            reasons.append(f"{name}: Brier improvement <= 0")
        if improvement["brier_ci"] is None or improvement["brier_ci"][0] <= 0:
            reasons.append(f"{name}: Brier improvement 95% CI lower bound <= 0")
        if improvement["log_loss"] <= 0:
            reasons.append(f"{name}: log-loss improvement <= 0")
        if improvement["log_loss_ci"] is None or improvement["log_loss_ci"][0] <= 0:
            reasons.append(f"{name}: log-loss improvement 95% CI lower bound <= 0")
    for class_name in classes:
        if not support[class_name]:
            reasons.append(f"{class_name}: no reliability bin meets support/calibration gate")
    if coverage < release["min_supported_coverage"]:
        reasons.append(
            f"supported-bin coverage {coverage:.3f} < {release['min_supported_coverage']}"
        )
    if event_id not in CLOSE_EVENTS | CANDIDATE_EVENTS:
        reasons.append("unsupported event version")
    return reasons


def _serialize_parameters(parameters: dict) -> dict:
    return {
        "mean": parameters["mean"].tolist(),
        "scale": parameters["scale"].tolist(),
        "weights": parameters["weights"].tolist(),
        "intercept": parameters["intercept"].tolist(),
        "temperature": parameters["temperature"],
    }


def train(protocol_path: Path, decisions_dir: Path, output_dir: Path) -> dict:
    protocol = _load_protocol(protocol_path)
    windows = split_windows(protocol)
    if "release" not in protocol:
        raise ValueError("Training requires frozen release settings")
    manifest = read_evaluation_manifest(decisions_dir)
    protocol_hash = hashlib.sha256(protocol_path.read_bytes()).hexdigest()
    settings = Settings(**protocol.get("settings", {}))
    if (
        manifest["protocol_sha256"] != protocol_hash
        or manifest["protocol"] != protocol
        or manifest["engine_version"] != ENGINE_VERSION
        or manifest["config_hash"] != settings.computation_hash
    ):
        raise ValueError("Evaluation and training protocol or engine identity mismatch")
    decisions, events = _load_rows(decisions_dir)
    cohorts = defaultdict(list)
    sample_keys = {
        (s["symbol"], s["asset_type"], s["source_timeframe"], s["snapshot_id"])
        for s in protocol["samples"]
    }
    for decision in decisions.values():
        if (
            tuple(
                decision[key] for key in ("symbol", "asset_type", "timeframe", "source_snapshot_id")
            )
            not in sample_keys
        ):
            raise ValueError("Decision is outside the frozen sample")

        cohorts[tuple(decision[key] for key in COHORT_KEYS)]
    seen_events = set()
    for event in events:
        identity = event["decision_id"], event["event_id"], event.get("candidate_key")
        if identity in seen_events:
            raise ValueError("Duplicate evaluation event")
        seen_events.add(identity)
        decision = decisions[event["decision_id"]]
        label = _label(event["event_id"], event["status"])
        if label is None or not decision.get("features") or not event.get("due_date"):
            continue
        if event["event_id"] not in protocol["events"] or event["symbol"] != decision["symbol"]:
            raise ValueError("Event does not match decision or training protocol")
        cohorts[tuple(decision[key] for key in COHORT_KEYS)].append(
            {
                "event_id": event["event_id"],
                "decision_date": date.fromisoformat(decision["decision_date"]),
                "due_date": date.fromisoformat(event["due_date"]),
                "symbol": decision["symbol"],
                "decision": decision,
                "candidate": event.get("features"),
                "y": label,
            }
        )
    if not cohorts:
        raise ValueError("No evaluation decisions to train")
    if len(cohorts) == 1:
        key, rows = next(iter(cohorts.items()))
        return _train_cohort(protocol, windows, manifest, key, rows, output_dir)
    summaries = {}
    for key, rows in sorted(cohorts.items()):
        cohort_id = digest(key)[:16]
        summaries[cohort_id] = _train_cohort(
            protocol, windows, manifest, key, rows, output_dir / cohort_id
        )
    summary = {"schema_version": "1.2", "protocol_sha256": protocol_hash, "cohorts": summaries}
    atomic_write(output_dir / "summary.json", canonical_json(summary) + "\n")
    return summary


def _train_cohort(protocol, windows, manifest, cohort, rows, output_dir):
    release = _release(protocol)
    bins = tuple(release["reliability_bins"])
    cadence = protocol["grid"]["cadence"]
    output_dir.mkdir(parents=True, exist_ok=True)
    models = {}
    timeframe = cohort[2]
    for event_id in sorted(
        event
        for event in protocol["events"]
        if event.endswith("D_V1" if timeframe == "1d" else "W_V1")
    ):
        subset = [row for row in rows if row["event_id"] == event_id]
        split = _split_rows(subset, windows)
        train_rows, cal_rows, test_rows = (split[name] for name in ("train", "calibration", "test"))
        n_classes = 3 if event_id in CLOSE_EVENTS else 2
        classes = ["UP", "RANGE", "DOWN"] if n_classes == 3 else ["NO", "YES"]
        counts = {
            "train": len(train_rows),
            "calibration": len(cal_rows),
            "test": len(test_rows),
        }
        if min(counts.values()) < 5 or len({row["y"] for row in train_rows}) != n_classes:
            models[event_id] = {
                "status": "INSUFFICIENT_EVIDENCE",
                "event_id": event_id,
                "event_version": "V1",
                "reason": "insufficient split rows or a missing training class",
                "reasons": ["insufficient split rows or a missing training class"],
                "sample_counts": counts,
            }
            continue
        feature_order = event_feature_order(event_id)
        x_train = np.asarray(
            [_features(row, row["decision"], feature_order) for row in train_rows],
            dtype=float,
        )
        x_cal = np.asarray(
            [_features(row, row["decision"], feature_order) for row in cal_rows],
            dtype=float,
        )
        x_test = np.asarray(
            [_features(row, row["decision"], feature_order) for row in test_rows],
            dtype=float,
        )
        y_train = np.asarray([row["y"] for row in train_rows], dtype=int)
        y_cal = np.asarray([row["y"] for row in cal_rows], dtype=int)
        y_test = np.asarray([row["y"] for row in test_rows], dtype=int)
        test_dates = [row["decision_date"].isoformat() for row in test_rows]
        dates = sorted({row["decision_date"] for row in test_rows})
        overlap = max(
            bisect_left(dates, row["due_date"]) - bisect_left(dates, row["decision_date"])
            for row in test_rows
        )
        block_size = max(_block_size(event_id, cadence), overlap)

        test_prob, parameters = _fit_model(x_train, y_train, x_cal, y_cal, x_test, n_classes)
        frequency = _frequency_baseline(y_train, n_classes)
        frequency_prob = np.tile(frequency, (len(y_test), 1))
        momentum_prob, momentum_parameters = _fit_model(
            np.asarray(
                [_features(row, row["decision"], MOMENTUM_FEATURES) for row in train_rows],
                dtype=float,
            ),
            y_train,
            np.asarray(
                [_features(row, row["decision"], MOMENTUM_FEATURES) for row in cal_rows],
                dtype=float,
            ),
            y_cal,
            np.asarray(
                [_features(row, row["decision"], MOMENTUM_FEATURES) for row in test_rows],
                dtype=float,
            ),
            n_classes,
        )
        price_order = _price_order(event_id)
        price_prob, price_parameters = _fit_model(
            np.asarray(
                [_features(row, row["decision"], price_order) for row in train_rows],
                dtype=float,
            ),
            y_train,
            np.asarray(
                [_features(row, row["decision"], price_order) for row in cal_rows],
                dtype=float,
            ),
            y_cal,
            np.asarray(
                [_features(row, row["decision"], price_order) for row in test_rows],
                dtype=float,
            ),
            n_classes,
        )
        metrics = _metrics(test_prob, y_test)
        baseline_metrics = {
            "frequency": _metrics(frequency_prob, y_test),
            "momentum": _metrics(momentum_prob, y_test),
            "price": _metrics(price_prob, y_test),
        }
        improvements = {}
        for name, baseline_prob in (
            ("frequency", frequency_prob),
            ("momentum", momentum_prob),
            ("price", price_prob),
        ):
            improvement = {
                "brier": baseline_metrics[name]["brier"] - metrics["brier"],
                "log_loss": baseline_metrics[name]["log_loss"] - metrics["log_loss"],
            }
            bootstrap = block_bootstrap_paired_improvement(
                y_test,
                test_prob,
                baseline_prob,
                test_dates,
                block_size=block_size,
                repetitions=release["bootstrap_repetitions"],
                seed=release["bootstrap_seed"],
            )
            improvement.update(
                bootstrap
                or {
                    "brier_ci": None,
                    "log_loss_ci": None,
                    "repetitions": 0,
                    "block_size": block_size,
                    "blocks": 0,
                }
            )
            improvements[name] = improvement
        class_reliability = bootstrap_reliability(
            test_prob,
            y_test,
            test_dates,
            bins=bins,
            block_size=block_size,
            repetitions=release["bootstrap_repetitions"],
            seed=release["bootstrap_seed"],
        )
        support = _annotate_reliability(class_reliability, classes, release)
        coverage = _supported_coverage(test_prob, support, classes)
        reasons = _approval_reasons(
            event_id=event_id,
            test_rows=test_rows,
            classes=classes,
            support=support,
            coverage=coverage,
            improvements=improvements,
            release=release,
        )
        status = "APPROVED" if not reasons else "INSUFFICIENT_EVIDENCE"
        models[event_id] = {
            "status": status,
            "event_id": event_id,
            "event_version": "V1",
            "reasons": reasons,
            "feature_order": feature_order,
            "encoding": {
                "patterns": PATTERNS if event_id in CANDIDATE_EVENTS else [],
                "stages": STAGES if event_id in CANDIDATE_EVENTS else [],
            },
            "classes": classes,
            **_serialize_parameters(parameters),
            "support_bins": support,
            "reliability": class_reliability,
            "sample_counts": counts,
            "test_rows": len(test_rows),
            "test_dates": len(set(test_dates)),
            "test_symbols": len({row["symbol"] for row in test_rows}),
            "class_counts": dict(Counter(int(value) for value in y_train)),
            "metrics": metrics,
            "baseline": baseline_metrics["frequency"],
            "baselines": {
                "frequency": {
                    "metrics": baseline_metrics["frequency"],
                    "probabilities": frequency.tolist(),
                    "feature_order": [],
                },
                "momentum": {
                    "metrics": baseline_metrics["momentum"],
                    "feature_order": MOMENTUM_FEATURES,
                    **_serialize_parameters(momentum_parameters),
                },
                "price": {
                    "metrics": baseline_metrics["price"],
                    "feature_order": price_order,
                    **_serialize_parameters(price_parameters),
                },
            },
            "improvement": improvements,
            "brier_improvement_ci": improvements["frequency"]["brier_ci"],
            "reliability_gap": max(
                (
                    abs(row["mean_confidence"] - row["accuracy"])
                    for row in metrics["reliability"]
                    if row["count"] >= 5
                ),
                default=0.0,
            ),
            "supported_coverage": coverage,
            "block_size": block_size,
        }
    model = {
        "schema_version": "1.2",
        "feature_version": FEATURE_VERSION,
        "event_versions": {event_id: "V1" for event_id in models},
        "scope": {key: [value] for key, value in zip(SCOPE_KEYS, cohort, strict=True)},
        "splits": protocol.get("splits"),
        "engine_version": ENGINE_VERSION,
        "config_hash": manifest["config_hash"],
        "release": release,
        "protocol_sha256": manifest["protocol_sha256"],
        "decisions_sha256": manifest["files"]["decisions.jsonl"],
        "events_sha256": manifest["files"]["events.parquet"],
        "models": models,
    }
    model["model_id"] = digest(model)
    atomic_write(output_dir / "model.json", canonical_json(model) + "\n")
    summary = {
        "model_path": str(output_dir / "model.json"),
        "model_id": model["model_id"],
        "scope": model["scope"],
        "release": release,
        "models": {
            event_id: {
                "status": value["status"],
                "reasons": value.get("reasons"),
                "sample_counts": value.get("sample_counts"),
                "test_rows": value.get("test_rows"),
                "test_dates": value.get("test_dates"),
                "test_symbols": value.get("test_symbols"),
                "metrics": value.get("metrics"),
                "baselines": {
                    name: baseline["metrics"]
                    for name, baseline in value.get("baselines", {}).items()
                },
                "improvement": value.get("improvement"),
                "support_bins": value.get("support_bins"),
                "supported_coverage": value.get("supported_coverage"),
            }
            for event_id, value in models.items()
        },
    }
    atomic_write(output_dir / "summary.json", canonical_json(summary) + "\n")
    files = ["model.json", "summary.json"]
    manifest = {
        "schema_version": "1.1",
        "model_id": model["model_id"],
        "protocol_sha256": model["protocol_sha256"],
        "engine_version": model["engine_version"],
        "config_hash": model["config_hash"],
        "scope": model["scope"],
        "release": release,
        "files": {
            name: hashlib.sha256((output_dir / name).read_bytes()).hexdigest() for name in files
        },
    }
    atomic_write(output_dir / "manifest.json", canonical_json(manifest) + "\n")
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--protocol", type=Path, required=True)
    parser.add_argument("--decisions", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    summary = train(args.protocol, args.decisions, args.output_dir)
    print(json.dumps(summary, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
