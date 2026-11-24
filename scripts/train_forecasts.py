"""Train and calibrate fixed-horizon forecast models from evaluation rows."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from bisect import bisect_left
from collections import Counter, defaultdict
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
import pyarrow.parquet as pq

from elliottwaver.config import Settings
from elliottwaver.evaluation.calibration import (
    binary_logit_affine,
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
from elliottwaver.evaluation.decisions import (
    _load_protocol,
    read_evaluation_manifest,
    split_windows,
)
from elliottwaver.evaluation.events import EVENT_SPECS
from elliottwaver.evaluation.features import (
    FEATURE_VERSION,
    OUTLOOK_PRICE_FEATURES,
    OUTLOOK_V5_PRICE_FEATURES,
    PATTERNS,
    STAGES,
    TARGET_FEATURES,
    event_feature_order,
    feature_vector,
)
from elliottwaver.evaluation.forward import holdout_plan, live_receipt_key, live_reports
from elliottwaver.service import engine_rules_version
from elliottwaver.util import atomic_write, canonical_json, digest

CLOSE_EVENTS = {key for key, spec in EVENT_SPECS.items() if spec.scope == "SYMBOL"}
CANDIDATE_EVENTS = {key for key, spec in EVENT_SPECS.items() if spec.scope == "CARD"}
NEW_OUTLOOK_EVENTS = {key for key, spec in EVENT_SPECS.items() if spec.outlook}
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


def _price_order(event_id: str, feature_version: str = FEATURE_VERSION) -> list[str]:
    order = list(PRICE_FEATURES)
    spec = EVENT_SPECS[event_id]
    if spec.scope == "CARD":
        order += ["confirm_distance", "invalidation_distance"]
        if feature_version in {"3", "4", "5"} and spec.evaluator == "target_first":
            order += TARGET_FEATURES
        if feature_version in {"4", "5"}:
            order += OUTLOOK_PRICE_FEATURES
        if feature_version == "5" and spec.unit == "BARS":
            order += OUTLOOK_V5_PRICE_FEATURES
    return order


def _features(event: dict, decision: dict, feature_order: list[str]) -> list[float]:
    return feature_vector(feature_order, decision["features"], event.get("features"))


def _label(event_id: str, status: str) -> int | None:
    spec = EVENT_SPECS.get(event_id)
    return spec.label(status) if spec else None


def _block_size(event_id: str, cadence: str) -> int:
    spec = EVENT_SPECS[event_id]
    if spec.unit == "BARS":
        return 1  # All minute decisions in a market session share one bootstrap cluster.
    if spec.unit == "SESSIONS" and cadence == "LAST_SESSION_OF_WEEK":
        return max(1, (spec.horizon + 4) // 5)
    return spec.horizon


def _timestamp(value: str) -> datetime:
    stamp = datetime.fromisoformat(value)
    if stamp.utcoffset() is None:
        raise ValueError("An intraday decision, due time or label requires an explicit timezone")
    return stamp.astimezone(UTC)


def _market_zone(decision: dict) -> ZoneInfo:
    return ZoneInfo("Asia/Shanghai" if decision.get("market") == "CN" else "America/New_York")


def _intraday_times(event: dict, decision: dict) -> dict | None:
    spec = EVENT_SPECS[event["event_id"]]
    if spec.timeframe != decision.get("timeframe"):
        raise ValueError("Event timeframe does not match its decision")
    if spec.unit != "BARS":
        return {}
    if (
        event.get("horizon") != spec.horizon
        or event.get("horizon_unit", spec.unit) != spec.unit
        or event.get("outlook_version") != spec.outlook_version
        or event.get("slot") not in {"PRIMARY", "ALTERNATE_1", "ALTERNATE_2"}
    ):
        raise ValueError("Intraday event does not match its registered horizon or slot")
    if not event.get("due_at") or not event.get("label_available_at"):
        return None
    ready = _timestamp(decision.get("decision_at") or decision["as_of"])
    ends = tuple(_timestamp(end) for end in event.get("period_ends") or ())
    starts = tuple(_timestamp(start) for start in event.get("period_starts") or ())
    due = _timestamp(event["due_at"])
    label = _timestamp(event["label_available_at"])
    bar_end = _timestamp(decision["last_bar_end_at"])
    if (
        len(ends) != spec.horizon
        or len(starts) != spec.horizon
        or bar_end > ready
        or ready > starts[0]
        or any(start >= end for start, end in zip(starts, ends, strict=True))
        or any(a > b for a, b in zip(ends, starts[1:], strict=False))
        or not ready < due == ends[-1] <= label
        or due.astimezone(_market_zone(decision)).date().isoformat() != event["due_date"]
    ):
        raise ValueError("Intraday event timestamps do not match its complete frozen window")
    return {"decision_at": ready, "due_at": due, "label_available_at": label}


def _label_within_cutoff(event: dict, decision: dict, cutoff: str) -> bool:
    received_at = _as_utc(datetime.fromisoformat(event["label_available_at"]))
    if len(cutoff) == 10:
        last_day = date.fromisoformat(cutoff)
        return (
            date.fromisoformat(event["due_date"]) <= last_day
            and received_at.astimezone(_market_zone(decision)).date() <= last_day
        )
    last_instant = _timestamp(cutoff)
    due_at = event.get("due_at")
    return received_at <= last_instant and (due_at is None or _timestamp(due_at) <= last_instant)


def _split_rows(rows, windows):
    output = {}
    for name, (start, end) in windows.items():
        selected = []
        for row in rows:
            if not (start <= row["decision_date"] <= row["due_date"] <= end):
                continue
            available = row.get("label_available_at")
            if row.get("decision_at") is not None:
                zone = _market_zone(row["decision"])
                lower = datetime.combine(start, time.min, zone)
                upper = datetime.combine(end + timedelta(days=1), time.min, zone)
                if not lower <= row["decision_at"] < row["due_at"] <= available < upper:
                    continue
            elif available and date.fromisoformat(str(available)[:10]) > end:
                continue
            selected.append(row)
        output[name] = selected
    return output


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
    *,
    calibration_method: str = "temperature",
) -> tuple[np.ndarray, dict]:
    x_train_s, x_cal_s, stats = standardize(x_train, x_cal)
    x_test_s = (x_test - stats[0]) / stats[1]
    weights, intercept = fit_softmax(x_train_s, y_train, n_classes)
    if calibration_method == "binary_logit_affine":
        if n_classes != 2:
            raise ValueError("Affine logit calibration is only supported for binary events")
        logits = x_cal_s @ (weights[:, 1] - weights[:, 0]) + intercept[1] - intercept[0]
        slope_temperature, offset = binary_logit_affine(logits, y_cal)
        weights = weights / slope_temperature
        intercept = intercept / slope_temperature
        intercept[1] += offset
        temperature = 1.0
    elif calibration_method == "temperature":
        cal_prob = predict(x_cal_s, weights, intercept)
        temperature = temperature_scale(cal_prob, y_cal)
    else:
        raise ValueError(f"Unknown calibration method: {calibration_method}")
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
    if any(EVENT_SPECS[event].unit == "BARS" for event in protocol["events"]):
        for name in ("min_test_rows", "min_test_dates", "min_test_symbols"):
            release[name] = max(release[name], DEFAULT_RELEASE[name])
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


def forecast_evidence(
    event_id: str,
    test_rows: list[dict],
    classes: list[str],
    probabilities: np.ndarray,
    baselines: dict[str, np.ndarray],
    release: dict,
    cadence: str,
) -> dict:
    y = np.asarray([row["y"] for row in test_rows], dtype=int)
    test_dates = [row["decision_date"].isoformat() for row in test_rows]
    dates = sorted({row["decision_date"] for row in test_rows})
    overlap = max(
        bisect_left(dates, row["due_date"]) - bisect_left(dates, row["decision_date"])
        for row in test_rows
    )
    block_size = max(_block_size(event_id, cadence), overlap)
    metrics = _metrics(probabilities, y)
    baseline_metrics = {name: _metrics(values, y) for name, values in baselines.items()}
    improvements = {}
    bootstrap_options = {
        "block_size": block_size,
        "repetitions": release["bootstrap_repetitions"],
        "seed": release["bootstrap_seed"],
    }
    for name, values in baselines.items():
        bootstrap = block_bootstrap_paired_improvement(
            y, probabilities, values, test_dates, **bootstrap_options
        )
        improvements[name] = {
            "brier": baseline_metrics[name]["brier"] - metrics["brier"],
            "log_loss": baseline_metrics[name]["log_loss"] - metrics["log_loss"],
            **(
                bootstrap
                or {
                    "brier_ci": None,
                    "log_loss_ci": None,
                    "repetitions": 0,
                    "block_size": block_size,
                    "blocks": 0,
                }
            ),
        }
    class_reliability = bootstrap_reliability(
        probabilities, y, test_dates, bins=tuple(release["reliability_bins"]), **bootstrap_options
    )
    support = _annotate_reliability(class_reliability, classes, release)
    coverage = _supported_coverage(probabilities, support, classes)
    reasons = _approval_reasons(
        event_id=event_id,
        test_rows=test_rows,
        classes=classes,
        support=support,
        coverage=coverage,
        improvements=improvements,
        release=release,
    )
    return {
        "status": "APPROVED" if not reasons else "INSUFFICIENT_EVIDENCE",
        "reasons": reasons,
        "support_bins": support,
        "reliability": class_reliability,
        "test_rows": len(test_rows),
        "test_dates": len(dates),
        "test_symbols": len({row["symbol"] for row in test_rows}),
        "metrics": metrics,
        "baseline_metrics": baseline_metrics,
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


def train(
    protocol_path: Path,
    decisions_dir: Path,
    output_dir: Path,
    *,
    outlook_slot: str | None = None,
) -> dict:
    protocol = _load_protocol(protocol_path)
    windows = split_windows(protocol)
    if "release" not in protocol:
        raise ValueError("Training requires frozen release settings")
    if any(event in NEW_OUTLOOK_EVENTS for event in protocol["events"]) and outlook_slot not in {
        "PRIMARY",
        "ALTERNATE_1",
        "ALTERNATE_2",
    }:
        raise ValueError("New outlook events require an explicit --outlook-slot")
    manifest = read_evaluation_manifest(decisions_dir)
    settings = Settings(**protocol.get("settings", {}))
    expected_engine = engine_rules_version(settings)
    manifest_protocol = manifest["protocol"]
    identity = {
        key: value for key, value in protocol.items() if key not in {"events", "protocol_id"}
    }
    manifest_identity = {
        key: value
        for key, value in manifest_protocol.items()
        if key not in {"events", "protocol_id"}
    }
    if (
        identity != manifest_identity
        or not set(protocol["events"]) <= set(manifest_protocol["events"])
        or manifest["engine_version"] != expected_engine
        or manifest["config_hash"] != settings.computation_hash
    ):
        raise ValueError("Evaluation and training protocol or engine identity mismatch")
    decisions, events = _load_rows(decisions_dir)
    cohorts = defaultdict(list)
    cohort_symbols: dict[tuple, set[str]] = defaultdict(set)
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
        cohort = tuple(decision[key] for key in COHORT_KEYS)
        cohorts[cohort]
        cohort_symbols[cohort].add(decision["symbol"])
    seen_events = set()
    for event in events:
        identity = (
            event["decision_id"],
            event["event_id"],
            event.get("candidate_key"),
            event.get("symbol"),
            event.get("slot"),
        )
        if identity in seen_events:
            raise ValueError("Duplicate evaluation event")
        seen_events.add(identity)
        decision = decisions[event["decision_id"]]
        if event["event_id"] not in protocol["events"]:
            continue
        if event["symbol"] != decision["symbol"]:
            raise ValueError("Event does not match decision or training protocol")
        label = _label(event["event_id"], event["status"])
        if label is None or not decision.get("features") or not event.get("due_date"):
            continue
        timestamps = _intraday_times(event, decision)
        if timestamps is None:
            continue
        if event["event_id"] in NEW_OUTLOOK_EVENTS and event.get("slot") != outlook_slot:
            continue
        cohorts[tuple(decision[key] for key in COHORT_KEYS)].append(
            {
                "event_id": event["event_id"],
                "decision_date": date.fromisoformat(decision["decision_date"]),
                "due_date": date.fromisoformat(event["due_date"]),
                "symbol": decision["symbol"],
                "decision": decision,
                "features": event.get("features"),
                "slot": event.get("slot"),
                "label_available_at": event.get("label_available_at"),
                **timestamps,
                "y": label,
            }
        )
    if not cohorts:
        raise ValueError("No evaluation decisions to train")
    if len(cohorts) == 1:
        key, rows = next(iter(cohorts.items()))
        return _train_cohort(
            protocol,
            windows,
            manifest,
            key,
            rows,
            output_dir,
            symbols=sorted(cohort_symbols[key]),
            outlook_slot=outlook_slot,
        )
    summaries = {}
    for key, rows in sorted(cohorts.items()):
        cohort_id = digest(key)[:16]
        summaries[cohort_id] = _train_cohort(
            protocol,
            windows,
            manifest,
            key,
            rows,
            output_dir / cohort_id,
            symbols=sorted(cohort_symbols[key]),
            outlook_slot=outlook_slot,
        )
    summary = {
        "schema_version": "1.2",
        "protocol_sha256": manifest["protocol_sha256"],
        "cohorts": summaries,
    }
    atomic_write(output_dir / "summary.json", canonical_json(summary) + "\n")
    return summary


def _train_cohort(protocol, windows, manifest, cohort, rows, output_dir, *, symbols, outlook_slot):
    release = _release(protocol)
    cadence = protocol["grid"]["cadence"]
    output_dir.mkdir(parents=True, exist_ok=True)
    feature_version = str(protocol.get("feature_version", FEATURE_VERSION))
    models = {}
    timeframe = cohort[2]
    for event_id in sorted(
        event for event in protocol["events"] if EVENT_SPECS[event].timeframe == timeframe
    ):
        subset = [row for row in rows if row["event_id"] == event_id]
        split = _split_rows(subset, windows)
        train_rows, cal_rows, test_rows = (split[name] for name in ("train", "calibration", "test"))
        spec = EVENT_SPECS[event_id]
        n_classes = 3 if spec.scope == "SYMBOL" else 2
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
                "event_contract": spec.horizon_contract,
                "reason": "insufficient split rows or a missing training class",
                "reasons": ["insufficient split rows or a missing training class"],
                "sample_counts": counts,
            }
            continue
        feature_order = event_feature_order(event_id, feature_version)
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
        test_prob, parameters = _fit_model(x_train, y_train, x_cal, y_cal, x_test, n_classes)
        frequency = _frequency_baseline(y_train, n_classes)
        frequency_prob = np.tile(frequency, (len(test_rows), 1))
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
        price_order = _price_order(event_id, feature_version)
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
        evidence = forecast_evidence(
            event_id,
            test_rows,
            classes,
            test_prob,
            {"frequency": frequency_prob, "momentum": momentum_prob, "price": price_prob},
            release,
            cadence,
        )
        baseline_metrics = evidence.pop("baseline_metrics")
        models[event_id] = {
            **evidence,
            "event_id": event_id,
            "event_version": "V1",
            "event_contract": spec.horizon_contract,
            "feature_order": feature_order,
            "encoding": {
                "patterns": PATTERNS if event_id in CANDIDATE_EVENTS else [],
                "stages": STAGES if event_id in CANDIDATE_EVENTS else [],
            },
            "classes": classes,
            **_serialize_parameters(parameters),
            "sample_counts": counts,
            "class_counts": dict(Counter(int(value) for value in y_train)),
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
        }
    scope = {key: [value] for key, value in zip(SCOPE_KEYS, cohort, strict=True)}
    scope["symbols"] = list(symbols)
    if any(event_id in NEW_OUTLOOK_EVENTS for event_id in models):
        scope.update(
            {
                "outlook_version": "1.1" if timeframe.endswith("m") else "1.0",
                "outlook_slot": outlook_slot,
            }
        )
    model = {
        "schema_version": "1.2",
        "feature_version": feature_version,
        "event_versions": {event_id: "V1" for event_id in models},
        "scope": scope,
        "splits": protocol.get("splits"),
        "engine_version": manifest["engine_version"],
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


def _predict_frozen(rows: list[dict], parameters: dict, order: list[str]) -> np.ndarray:
    x = np.asarray([_features(row, row["decision"], order) for row in rows], dtype=float)
    x = (x - np.asarray(parameters["mean"])) / np.asarray(parameters["scale"])
    return predict(
        x,
        np.asarray(parameters["weights"]),
        np.asarray(parameters["intercept"]),
        parameters["temperature"],
    )


def _as_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _label_available_bounds(
    directory: Path, protocol: dict
) -> tuple[datetime | None, datetime | None]:
    """Return the last in-window decision cutoff and label-availability time."""
    try:
        decisions, events = _load_rows(directory)
    except (OSError, ValueError, KeyError):
        return None, None
    grid_end = date.fromisoformat(protocol["grid"]["end"])
    cutoff_value = protocol["grid"].get("outcome_as_of") or grid_end.isoformat()
    cutoffs = [
        _as_utc(datetime.fromisoformat(row.get("decision_at") or row["as_of"]))
        for row in decisions.values()
        if row.get("as_of") and date.fromisoformat(row["decision_date"]) <= grid_end
    ]
    labels = [
        _as_utc(datetime.fromisoformat(row["label_available_at"]))
        for row in events
        if row.get("label_available_at")
        and row.get("due_date")
        and _label_within_cutoff(row, decisions[row["decision_id"]], cutoff_value)
    ]
    return (max(cutoffs) if cutoffs else None, max(labels) if labels else None)


def freeze(
    protocol_path: Path,
    decisions_dir: Path,
    output: Path,
    *,
    outlook_slot: str | None = None,
    event_id: str | None = None,
    frozen_at: datetime | None = None,
    market: str | None = None,
    asset_type: str | None = None,
    timeframe: str | None = None,
    feed: str | None = None,
    adjustment: str | None = None,
) -> dict:
    """Train on the development protocol and freeze its parameters."""
    protocol = _load_protocol(protocol_path)
    last_decision, last_label = _label_available_bounds(decisions_dir, protocol)
    requested_freeze = _as_utc(frozen_at) if frozen_at is not None else None
    development_end = max(
        date.fromisoformat(protocol["grid"]["end"]),
        date.fromisoformat((protocol["grid"].get("outcome_as_of") or protocol["grid"]["end"])[:10]),
        date.fromisoformat(protocol["splits"]["test"]["end"]),
        last_decision.date() if last_decision is not None else date.min,
        last_label.date() if last_label is not None else date.min,
    )
    if requested_freeze is not None:
        if requested_freeze.date() < development_end:
            raise ValueError("Frozen time precedes the development window's last available label")
        if last_label is not None and requested_freeze < last_label:
            raise ValueError("Frozen time precedes the development window's last available label")
    training_dir = output.parent / "_freeze_training"
    summary = train(
        protocol_path,
        decisions_dir,
        training_dir,
        outlook_slot=outlook_slot,
    )
    if "cohorts" in summary:
        matches = [
            (cohort_id, cohort)
            for cohort_id, cohort in summary["cohorts"].items()
            if (market is None or cohort["scope"]["markets"] == [market])
            and (asset_type is None or cohort["scope"]["asset_types"] == [asset_type])
            and (timeframe is None or cohort["scope"]["timeframes"] == [timeframe])
            and (feed is None or cohort["scope"]["feed"] == [feed])
            and (adjustment is None or cohort["scope"]["adjustment"] == [adjustment])
        ]
        if len(matches) != 1:
            raise ValueError(
                "Freeze requires explicit market/asset-type/timeframe/feed/adjustment "
                "for a multi-cohort protocol"
            )
        cohort_id = matches[0][0]
        model_path = training_dir / cohort_id / "model.json"
    else:
        model_path = training_dir / "model.json"
    model = json.loads(model_path.read_text())
    models = model["models"]
    if event_id is not None:
        if event_id not in models:
            raise ValueError(f"Frozen event not found: {event_id}")
        models = {event_id: models[event_id]}
    new_events = set(models) & NEW_OUTLOOK_EVENTS
    if new_events and outlook_slot not in {"PRIMARY", "ALTERNATE_1", "ALTERNATE_2"}:
        raise ValueError("New outlook events require --outlook-slot")
    if new_events and len(models) != 1:
        raise ValueError("Freeze an outlook model with one explicit --event")
    if "CLOSE_BAND_2W_V1" in models and outlook_slot != "SYMBOL":
        raise ValueError("CLOSE_BAND_2W_V1 requires --outlook-slot SYMBOL")
    scope = dict(model["scope"])
    if outlook_slot is not None:
        scope.update(
            {"outlook_version": scope.get("outlook_version", "1.0"), "outlook_slot": outlook_slot}
        )
    actual_freeze = datetime.now(UTC)
    if requested_freeze is not None and requested_freeze > actual_freeze:
        raise ValueError("Historical split time cannot be in the future")
    development_manifest = (
        read_evaluation_manifest(decisions_dir)
        if (decisions_dir / "manifest.json").is_file()
        else {}
    )
    candidate = {
        "schema_version": "1.1",
        "stage": "FROZEN_CANDIDATE",
        "frozen_at": actual_freeze.isoformat(),
        "historical_split_at": requested_freeze.isoformat()
        if requested_freeze is not None
        else None,
        "development_source_sha256": development_manifest.get("source_sha256"),
        "development_label_available_at": (
            last_label.isoformat() if last_label is not None else None
        ),
        "development_end": development_end.isoformat(),
        "feature_version": model["feature_version"],
        "scope": scope,
        "splits": model["splits"],
        "engine_version": model["engine_version"],
        "config_hash": model["config_hash"],
        "release": model["release"],
        "protocol_sha256": model["protocol_sha256"],
        "decisions_sha256": model["decisions_sha256"],
        "events_sha256": model["events_sha256"],
        "symbols": sorted(
            scope.get("symbols") or {sample["symbol"] for sample in protocol["samples"]}
        ),
        "allow_gaps": bool(protocol.get("allow_gaps", False)),
        "selected_method": "D",
        "models": models,
    }
    candidate["candidate_id"] = digest(candidate)
    atomic_write(output, canonical_json(candidate) + "\n")
    return candidate


def _scope_matches(decision: dict, candidate: dict) -> bool:
    scope = candidate.get("scope") or {}
    symbols = set(candidate.get("symbols") or scope.get("symbols") or ())
    if symbols and decision["symbol"] not in symbols:
        return False
    for decision_key, scope_key in (
        ("market", "markets"),
        ("asset_type", "asset_types"),
        ("timeframe", "timeframes"),
        ("feed", "feed"),
        ("adjustment", "adjustment"),
    ):
        allowed = scope.get(scope_key)
        if allowed and decision.get(decision_key) not in allowed:
            return False
    return True


def _slot_matches(event: dict, candidate: dict) -> bool:
    event_id = event["event_id"]
    slot = (candidate.get("scope") or {}).get("outlook_slot")
    if event_id in NEW_OUTLOOK_EVENTS:
        return (
            slot is not None
            and event.get("slot") == slot
            and (
                EVENT_SPECS[event_id].unit != "BARS"
                or event.get("outlook_version") == EVENT_SPECS[event_id].outlook_version
            )
        )
    if event_id == "CLOSE_BAND_2W_V1":
        return slot == "SYMBOL"
    return True


def _span(rows: list[dict], key: str) -> dict | None:
    values = sorted(row[key] for row in rows)
    return {"start": str(values[0]), "end": str(values[-1])} if values else None


def _read_candidate(candidate_path: Path) -> tuple[str, dict]:
    candidate = json.loads(candidate_path.read_text())
    candidate_id = candidate.pop("candidate_id", None)
    if (
        candidate_id != digest(candidate)
        or candidate.get("stage") != "FROZEN_CANDIDATE"
        or candidate.get("schema_version") not in {"1.0", "1.1"}
        or not all(
            candidate.get(key)
            for key in ("frozen_at", "splits", "decisions_sha256", "events_sha256")
        )
    ):
        raise ValueError("Invalid frozen candidate")
    return candidate_id, candidate


def seal(candidate_path: Path, protocol_path: Path, output: Path) -> dict:
    """Commit to a future holdout range and cohort before its first decision."""
    candidate_id, candidate = _read_candidate(candidate_path)
    protocol = _load_protocol(protocol_path)
    sealed_at = datetime.now(UTC)
    frozen_at = _as_utc(datetime.fromisoformat(candidate["frozen_at"]))
    start = date.fromisoformat(protocol["grid"]["start"])
    if (
        candidate["schema_version"] != "1.1"
        or candidate.get("historical_split_at")
        or not candidate.get("development_source_sha256")
        or frozen_at > sealed_at
        or sealed_at.date() >= start
        or date.fromisoformat(candidate["development_end"]) >= start
        or protocol.get("data_status")
        not in {"AWAITING_FORWARD_SNAPSHOT", "SEALED_FORWARD_HOLDOUT"}
        or not set(candidate["models"]) <= set(protocol["events"])
        or str(protocol.get("feature_version", FEATURE_VERSION)) != candidate["feature_version"]
    ):
        raise ValueError("Seal requires a newly frozen candidate and a future holdout")
    record = {
        "schema_version": "1.0",
        "candidate_id": candidate_id,
        "sealed_at": sealed_at.isoformat(),
        "plan_sha256": digest(holdout_plan(protocol)),
    }
    record["seal_id"] = digest(record)
    atomic_write(output, canonical_json(record) + "\n")
    return record


def _forward_evidence(
    candidate: dict, candidate_id: str, manifest: dict, rows: dict, seal_path: Path, reports: Path
) -> dict:
    record = json.loads(seal_path.read_text())
    seal_id = record.pop("seal_id", None)
    protocol = manifest["protocol"]
    if (
        record.get("schema_version") != "1.0"
        or seal_id != digest(record)
        or record.get("candidate_id") != candidate_id
        or record.get("plan_sha256") != digest(holdout_plan(protocol))
        or candidate["schema_version"] != "1.1"
        or candidate.get("historical_split_at")
        or not manifest.get("source_sha256")
        or manifest["source_sha256"] != candidate.get("development_source_sha256")
    ):
        raise ValueError("Forward holdout seal does not match the candidate, code or protocol")
    sealed_at = _as_utc(datetime.fromisoformat(record["sealed_at"]))
    if sealed_at < _as_utc(
        datetime.fromisoformat(candidate["frozen_at"])
    ) or sealed_at.date() >= date.fromisoformat(protocol["grid"]["start"]):
        raise ValueError("Holdout must be sealed after freezing and before the first decision")
    receipts = live_reports(reports)
    verified = set()
    for cohort in rows.values():
        for row in cohort:
            decision = row["decision"]
            issued = receipts.get(
                live_receipt_key(
                    decision["symbol"],
                    decision["timeframe"],
                    decision["decision_date"],
                    decision.get("last_bar_end_at"),
                )
            )
            receipt = issued.brief if issued is not None else None
            if (
                receipt is None
                or decision.get("mode") != "LIVE"
                or decision.get("issued_brief_digest") != receipt.brief_digest
                or decision.get("result_digest") != receipt.result_digest
                or decision.get("input_snapshot_id") != receipt.input_snapshot_id
                or digest(decision.get("outlook")) != digest(receipt.outlook)
                or _as_utc(datetime.fromisoformat(decision["as_of"])) != receipt.as_of
                or decision.get("decision_at") != issued.readable_at.isoformat()
                or not sealed_at
                < receipt.as_of
                <= receipt.generated_at
                <= issued.readable_at
                < row["label_available_at"]
                or receipt.outlook is None
                or not receipt.outlook.periods
                or issued.readable_at > receipt.outlook.periods[0].start_at
            ):
                raise ValueError(
                    "Scored decision lacks a matching live report from the sealed holdout"
                )
            event_id = row["event_id"]
            if (
                event_id not in NEW_OUTLOOK_EVENTS
                and event_id != "CLOSE_BAND_2W_V1"
                and receipt.outcome_dates
                and receipt.outcome_dates[0] < receipt.outlook.periods[0].end_session
            ):
                raise ValueError("Legacy event window had already started at publication")
            verified.add((receipt.brief_digest, issued.readable_at.isoformat()))
    if not verified:
        raise ValueError("Forward holdout has no verified mature live decisions")
    return {
        "seal_id": seal_id,
        "sealed_at": sealed_at.isoformat(),
        "receipt_sha256": digest(sorted(verified)),
        "receipt_count": len(verified),
    }


def _index_holdout(event_id, rows, classes, probabilities, baselines, release, cadence):
    by_symbol = defaultdict(list)
    local_release = {**release, "min_test_symbols": 1}
    for i, row in enumerate(rows):
        by_symbol[row["symbol"]].append(i)
    results = {}
    for symbol, indices in sorted(by_symbol.items()):
        selected = [rows[i] for i in indices]
        if (
            len(selected) < release["min_test_rows"]
            or len({row["decision_date"] for row in selected}) < release["min_test_dates"]
        ):
            results[symbol] = {
                "eligible": False,
                "reasons": ["index-specific holdout sample too small"],
            }
            continue
        evidence = forecast_evidence(
            event_id,
            selected,
            classes,
            probabilities[indices],
            {name: values[indices] for name, values in baselines.items()},
            local_release,
            cadence,
        )
        results[symbol] = {
            "eligible": evidence["status"] == "APPROVED",
            **{
                name: evidence[name]
                for name in (
                    "test_rows",
                    "test_dates",
                    "reasons",
                    "improvement",
                    "supported_coverage",
                    "support_bins",
                )
            },
        }
    return results


def score(
    candidate_path: Path,
    holdout_dir: Path,
    output_dir: Path,
    *,
    seal_path: Path | None = None,
    issued_reports: Path | None = None,
) -> dict:
    """Score a frozen candidate on an independent holdout evaluation."""
    candidate_id, candidate = _read_candidate(candidate_path)
    manifest = read_evaluation_manifest(holdout_dir)
    protocol = manifest["protocol"]
    data_status = protocol.get("data_status")
    if data_status not in {"HISTORICAL_HOLDOUT", "SEALED_FORWARD_HOLDOUT"}:
        raise ValueError("Holdout is not an independent sealed holdout")
    forward = data_status == "SEALED_FORWARD_HOLDOUT"
    if forward and (seal_path is None or issued_reports is None):
        raise ValueError("Forward holdout requires a seal and verified live reports")
    study_mode = "DECISION_TIME" if forward else "RETROSPECTIVE"
    frozen_at = _as_utc(datetime.fromisoformat(candidate["frozen_at"]))
    independent_after = frozen_at
    if not forward and candidate["schema_version"] == "1.1":
        historical_cutoff = candidate.get("historical_split_at") or candidate.get(
            "development_label_available_at"
        )
        if historical_cutoff:
            independent_after = _as_utc(datetime.fromisoformat(historical_cutoff))
        else:
            independent_after = datetime.fromisoformat(candidate["development_end"]).replace(
                tzinfo=UTC
            )
    start = date.fromisoformat(protocol["grid"]["start"])
    stop = date.fromisoformat(protocol["grid"]["end"])
    cutoff_value = protocol["grid"]["outcome_as_of"]
    markets = (candidate.get("scope") or {}).get("markets") or ()
    cutoff_zone = _market_zone({"market": markets[0]}) if len(markets) == 1 else UTC
    cutoff = (
        date.fromisoformat(cutoff_value)
        if len(cutoff_value) == 10
        else _timestamp(cutoff_value).astimezone(cutoff_zone).date()
    )
    if independent_after.date() >= start:
        raise ValueError("Holdout must start after the frozen candidate")
    reused_decisions = candidate["decisions_sha256"] in {
        manifest.get("decisions_sha256"),
        manifest["files"].get("decisions.jsonl"),
    }
    reused_events = candidate["events_sha256"] in {
        manifest.get("events_sha256"),
        manifest["files"].get("events.parquet"),
    }
    # Independent outages can produce byte-identical empty event tables.
    if reused_decisions or (
        reused_events and pq.ParquetFile(holdout_dir / "events.parquet").metadata.num_rows
    ):
        raise ValueError("Holdout reuses the development evaluation artifact")
    development_end = date.fromisoformat(
        candidate.get("development_end")
        or candidate.get("development_label_available_at")
        or candidate["splits"]["test"]["end"]
    )
    if start <= development_end:
        raise ValueError("Holdout overlaps the development window")
    for name, window in (candidate.get("splits") or {}).items():
        if date.fromisoformat(window["end"]) >= start:
            raise ValueError(f"Development {name} split overlaps the holdout window")
    holdout_test = (protocol.get("splits") or {}).get("test")
    if holdout_test is not None and date.fromisoformat(holdout_test["start"]) < start:
        raise ValueError("Holdout test split starts before the holdout grid")
    if (
        manifest["engine_version"] != candidate["engine_version"]
        or manifest["config_hash"] != candidate["config_hash"]
        or manifest.get("feature_version", candidate["feature_version"])
        != candidate["feature_version"]
    ):
        raise ValueError("Holdout engine, config or feature version mismatch")
    if not set(candidate["models"]) <= set(protocol["events"]):
        raise ValueError("Frozen candidate event is not present in the holdout protocol")
    decisions, events = _load_rows(holdout_dir)
    rows_by_event: dict[str, list[dict]] = defaultdict(list)
    seen_rows = set()
    for event in events:
        event_id = event["event_id"]
        if event_id not in candidate["models"]:
            continue
        decision = decisions[event["decision_id"]]
        if not _scope_matches(decision, candidate) or not _slot_matches(event, candidate):
            continue
        label = _label(event_id, event["status"])
        if label is None or not event.get("due_date") or not event.get("label_available_at"):
            continue
        timestamps = _intraday_times(event, decision)
        if timestamps is None:
            continue
        decision_date = date.fromisoformat(decision["decision_date"])
        due_date = date.fromisoformat(event["due_date"])
        label_available_at = _as_utc(datetime.fromisoformat(event["label_available_at"]))
        if not (start <= decision_date <= stop) or not _label_within_cutoff(
            event, decision, cutoff_value
        ):
            continue
        zone = _market_zone(decision) if timestamps else UTC
        decision_time = timestamps.get("decision_at") or _as_utc(
            datetime.fromisoformat(decision["as_of"])
        )
        if decision_time <= independent_after or (
            timestamps and not start <= decision_time.astimezone(zone).date() <= stop
        ):
            continue
        identity = (
            decision["symbol"],
            _timestamp(decision["last_bar_end_at"]) if timestamps else decision_date,
            event_id,
            event.get("slot"),
            None if timestamps else event.get("candidate_key"),
        )
        if identity in seen_rows:
            raise ValueError("Duplicate holdout row for a symbol/decision/position")
        seen_rows.add(identity)
        rows_by_event[event_id].append(
            {
                "event_id": event_id,
                "decision_date": decision_date,
                "due_date": due_date,
                "symbol": decision["symbol"],
                "decision": decision,
                "features": event.get("features"),
                "slot": event.get("slot"),
                "label_available_at": label_available_at,
                **timestamps,
                "y": label,
            }
        )
    evidence = (
        _forward_evidence(
            candidate, candidate_id, manifest, rows_by_event, seal_path, issued_reports
        )
        if seal_path is not None and issued_reports is not None and forward
        else None
    )
    models = {}
    for event_id, frozen in candidate["models"].items():
        rows = rows_by_event.get(event_id, [])
        counts = {**frozen.get("sample_counts", {}), "test": len(rows)}
        if not rows:
            models[event_id] = {
                **frozen,
                "status": "INSUFFICIENT_EVIDENCE",
                "reason": "no holdout rows",
                "reasons": ["no holdout rows"],
                "sample_counts": counts,
            }
            continue
        if "feature_order" not in frozen or "weights" not in frozen:
            models[event_id] = {
                **frozen,
                "status": "INSUFFICIENT_EVIDENCE",
                "reason": "frozen candidate has no fitted parameters",
                "reasons": ["frozen candidate has no fitted parameters"],
                "sample_counts": counts,
            }
            continue
        probabilities = _predict_frozen(rows, frozen, frozen["feature_order"])
        baselines = {}
        for name, baseline in frozen["baselines"].items():
            if name == "frequency":
                baselines[name] = np.tile(baseline["probabilities"], (len(rows), 1))
            else:
                baselines[name] = _predict_frozen(rows, baseline, baseline["feature_order"])
        metrics = forecast_evidence(
            event_id,
            rows,
            frozen["classes"],
            probabilities,
            baselines,
            candidate["release"],
            protocol["grid"]["cadence"],
        )
        baseline_metrics = metrics.pop("baseline_metrics")
        if (
            EVENT_SPECS[event_id].unit == "BARS"
            and candidate["scope"].get("asset_types") == ["INDEX"]
            and metrics["status"] == "APPROVED"
        ):
            metrics["index_holdout"] = _index_holdout(
                event_id,
                rows,
                frozen["classes"],
                probabilities,
                baselines,
                candidate["release"],
                protocol["grid"]["cadence"],
            )
            if not any(row["eligible"] for row in metrics["index_holdout"].values()):
                metrics["status"] = "INSUFFICIENT_EVIDENCE"
                metrics["reasons"].append("no index clears the index-specific holdout gates")
        if not forward and metrics["status"] == "APPROVED":
            metrics["status"] = "INSUFFICIENT_EVIDENCE"
            metrics["reasons"] = ["Retrospective study is ineligible for operational approval"]
        models[event_id] = {
            **frozen,
            **metrics,
            "sample_counts": counts,
            "baselines": {
                name: {**baseline, "metrics": baseline_metrics[name]}
                for name, baseline in frozen["baselines"].items()
            },
        }
    scope = dict(candidate["scope"])
    qualified_indices = {
        symbol
        for value in models.values()
        if value["status"] == "APPROVED"
        for symbol, check in value.get("index_holdout", {}).items()
        if check["eligible"]
    }
    if qualified_indices:
        scope["symbols"] = sorted(qualified_indices)
    if not scope.get("symbols"):
        scope["symbols"] = sorted(
            {row["symbol"] for row in decisions.values() if _scope_matches(row, candidate)}
        )
    model = {
        "schema_version": "1.2",
        "feature_version": candidate["feature_version"],
        "event_versions": {event_id: "V1" for event_id in models},
        "scope": scope,
        "splits": {
            **candidate["splits"],
            "test": {"start": str(start), "end": str(cutoff)},
        },
        "engine_version": candidate["engine_version"],
        "config_hash": candidate["config_hash"],
        "release": candidate["release"],
        "protocol_sha256": manifest["protocol_sha256"],
        "decisions_sha256": manifest["files"]["decisions.jsonl"],
        "events_sha256": manifest["files"]["events.parquet"],
        "frozen_candidate_id": candidate_id,
        "frozen_at": frozen_at.isoformat(),
        "development_label_available_at": candidate.get("development_label_available_at"),
        "development_end": candidate.get("development_end"),
        "holdout_source_sha256": manifest.get("source_sha256", ""),
        "data_status": data_status,
        "study_mode": study_mode,
        "operational_allowed": evidence is not None,
        "actual_span": {
            "test": _span(
                [row for rows in rows_by_event.values() for row in rows], "decision_date"
            ),
            "label_available_at": max(
                (
                    row["label_available_at"].isoformat()
                    for rows in rows_by_event.values()
                    for row in rows
                ),
                default=None,
            ),
        },
        "models": models,
    }
    if evidence is not None:
        model["forward_evidence"] = evidence
    if expiry := protocol.get("model_expires_at"):
        checked = _timestamp(expiry)
        if checked.date() <= cutoff:
            raise ValueError("Model expiration must follow the complete holdout")
        model["expires_at"] = checked.isoformat()
    model["model_id"] = digest(model)
    output_dir.mkdir(parents=True, exist_ok=True)
    atomic_write(output_dir / "model.json", canonical_json(model) + "\n")
    summary = {
        "model_id": model["model_id"],
        "frozen_candidate_id": candidate_id,
        "scope": model["scope"],
        "study_mode": study_mode,
        "operational_allowed": model["operational_allowed"],
        "models": {
            event_id: {
                "status": value.get("status"),
                "reasons": value.get("reasons"),
                "sample_counts": value.get("sample_counts"),
                "metrics": value.get("metrics"),
                "improvement": value.get("improvement"),
                "supported_coverage": value.get("supported_coverage"),
            }
            for event_id, value in models.items()
        },
    }
    atomic_write(output_dir / "summary.json", canonical_json(summary) + "\n")
    manifest_out = {
        "schema_version": "1.1",
        "model_id": model["model_id"],
        "frozen_candidate_id": candidate_id,
        "protocol_sha256": model["protocol_sha256"],
        "engine_version": model["engine_version"],
        "config_hash": model["config_hash"],
        "feature_version": model["feature_version"],
        "files": {
            name: hashlib.sha256((output_dir / name).read_bytes()).hexdigest()
            for name in ("model.json", "summary.json")
        },
    }
    atomic_write(output_dir / "manifest.json", canonical_json(manifest_out) + "\n")
    return summary


def main() -> int:
    if len(sys.argv) > 1 and sys.argv[1] in {"freeze", "seal", "score"}:
        parser = argparse.ArgumentParser(description=__doc__)
        commands = parser.add_subparsers(dest="command", required=True)
        freezer = commands.add_parser("freeze")
        freezer.add_argument("--protocol", type=Path, required=True)
        freezer.add_argument("--decisions", type=Path, required=True)
        freezer.add_argument("--output", type=Path, required=True)
        freezer.add_argument("--event", type=str, default=None)
        freezer.add_argument("--outlook-slot", type=str, default=None)
        freezer.add_argument("--frozen-at", help="Simulated historical split time for research")
        freezer.add_argument("--market", type=str, default=None)
        freezer.add_argument("--asset-type", type=str, default=None)
        freezer.add_argument("--timeframe", type=str, default=None)
        freezer.add_argument("--feed", type=str, default=None)
        freezer.add_argument("--adjustment", type=str, default=None)
        sealer = commands.add_parser("seal")
        sealer.add_argument("--candidate", type=Path, required=True)
        sealer.add_argument("--protocol", type=Path, required=True)
        sealer.add_argument("--output", type=Path, required=True)
        scorer = commands.add_parser("score")
        scorer.add_argument("--candidate", type=Path, required=True)
        scorer.add_argument("--holdout", type=Path, required=True)
        scorer.add_argument("--output-dir", type=Path, required=True)
        scorer.add_argument("--seal", type=Path)
        scorer.add_argument("--live-reports", type=Path)
        args = parser.parse_args()
        if args.command == "freeze":
            result = freeze(
                args.protocol,
                args.decisions,
                args.output,
                outlook_slot=args.outlook_slot,
                event_id=args.event,
                frozen_at=datetime.fromisoformat(args.frozen_at) if args.frozen_at else None,
                market=args.market,
                asset_type=args.asset_type,
                timeframe=args.timeframe,
                feed=args.feed,
                adjustment=args.adjustment,
            )
        elif args.command == "seal":
            result = seal(args.candidate, args.protocol, args.output)
        else:
            result = score(
                args.candidate,
                args.holdout,
                args.output_dir,
                seal_path=args.seal,
                issued_reports=args.live_reports,
            )
        print(json.dumps(result, indent=2, ensure_ascii=False))
        return 0
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--protocol", type=Path, required=True)
    parser.add_argument("--decisions", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--outlook-slot", type=str, default=None)
    args = parser.parse_args()
    summary = train(
        args.protocol,
        args.decisions,
        args.output_dir,
        outlook_slot=args.outlook_slot,
    )
    print(json.dumps(summary, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
