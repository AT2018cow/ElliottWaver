"""Per-template binomial models for independent recognition labels."""

from __future__ import annotations

import csv
import hashlib
import json
import math
import random
from collections import defaultdict
from pathlib import Path

import numpy as np

from eliotbot.evaluation.calibration import standardize
from eliotbot.util import atomic_write, canonical_json, digest

TEMPLATES = ["IMPULSE", "ZIGZAG_ABC", "TRIANGLE", "FLAT", "DIAGONAL", "WXY", "OTHER"]
DEGREES = ["MINOR", "INTERMEDIATE", "PRIMARY", "CYCLE", "SUPERCYCLE", "UNKNOWN"]
DIRECTIONS = ["UP", "DOWN", "UNKNOWN"]
AMBIGUITIES = ["LOW", "MEDIUM", "HIGH"]
ASSET_TYPES = ["EQUITY", "INDEX"]
TIMEFRAMES = ["1d", "1w"]
CASE_TYPES = ["CANDIDATE", "EXCLUDED_STRUCTURE", "NO_CANDIDATE"]
FEATURE_ORDER = (
    [f"degree={value}" for value in DEGREES]
    + [f"direction={value}" for value in DIRECTIONS]
    + [f"ambiguity={value}" for value in AMBIGUITIES]
    + [f"asset_type={value}" for value in ASSET_TYPES]
    + [f"timeframe={value}" for value in TIMEFRAMES]
    + [f"case_type={value}" for value in CASE_TYPES]
    + ["endpoint_count", "bars_available"]
)
DEGREE_FEATURES = len(DEGREES)
DEFAULT_RELEASE = {
    "min_label_double_cases": 50,
    "min_label_positive": 10,
    "min_label_negative": 10,
    "min_label_kappa": 0.60,
    "min_test_cases": 50,
    "min_test_dates": 10,
    "min_test_symbols": 5,
    "min_bin_count": 50,
    "min_bin_dates": 10,
    "reliability_tolerance": 0.10,
    "min_supported_coverage": 0.5,
    "bootstrap_repetitions": 2000,
    "bootstrap_seed": 20261119,
    "block_size": 5,
}


def _one_hot(value, choices):
    return [1.0 if value == choice else 0.0 for choice in choices]


def case_features(case: dict) -> list[float]:
    degree = case.get("degree") if case.get("degree") in DEGREES else "UNKNOWN"
    direction = case.get("direction") if case.get("direction") in DIRECTIONS else "UNKNOWN"
    ambiguity = case.get("ambiguity") if case.get("ambiguity") in AMBIGUITIES else "UNKNOWN"
    asset_type = case.get("asset_type") if case.get("asset_type") in ASSET_TYPES else "EQUITY"
    timeframe = case.get("timeframe") if case.get("timeframe") in TIMEFRAMES else "1d"
    case_type = case.get("case_type") if case.get("case_type") in CASE_TYPES else "CANDIDATE"
    values = (
        _one_hot(degree, DEGREES)
        + _one_hot(direction, DIRECTIONS)
        + _one_hot(ambiguity, AMBIGUITIES)
        + _one_hot(asset_type, ASSET_TYPES)
        + _one_hot(timeframe, TIMEFRAMES)
        + _one_hot(case_type, CASE_TYPES)
        + [
            min(len(case.get("endpoints", [])), 10) / 10.0,
            1.0 if case.get("bars_available") else 0.0,
        ]
    )
    return values


def _sigmoid(values: np.ndarray) -> np.ndarray:
    return 1.0 / (1.0 + np.exp(-np.clip(values, -50, 50)))


def fit_binomial(
    x: np.ndarray,
    y: np.ndarray,
    *,
    l2: float = 1e-3,
    epochs: int = 800,
    learning_rate: float = 0.2,
) -> tuple[np.ndarray, float]:
    weights = np.zeros(x.shape[1])
    intercept = 0.0
    for _ in range(epochs):
        probabilities = _sigmoid(x @ weights + intercept)
        error = probabilities - y
        weights -= learning_rate * (x.T @ error / len(x) + l2 * weights)
        intercept -= learning_rate * float(error.mean())
    return weights, intercept


def predict_binomial(
    x: np.ndarray, weights: np.ndarray, intercept: float, temperature: float = 1.0
) -> np.ndarray:
    return _sigmoid((x @ weights + intercept) / temperature)


def binary_brier(probabilities: np.ndarray, y: np.ndarray) -> float:
    return float(np.mean((probabilities - y) ** 2))


def binary_log_loss(probabilities: np.ndarray, y: np.ndarray) -> float:
    p = np.clip(probabilities, 1e-12, 1 - 1e-12)
    return float(-np.mean(y * np.log(p) + (1 - y) * np.log(1 - p)))


def temperature_scale_binary(
    probabilities: np.ndarray,
    y: np.ndarray,
    temperatures=(0.5, 0.75, 1.0, 1.5, 2.0, 3.0),
) -> float:
    p = np.clip(probabilities, 1e-12, 1 - 1e-12)
    logits = np.log(p / (1 - p))
    best = (math.inf, math.inf, 1.0)
    for temperature in temperatures:
        adjusted = _sigmoid(logits / temperature)
        key = (binary_log_loss(adjusted, y), abs(temperature - 1.0), temperature)
        if key < best:
            best = key
    return best[2]


def _blocks(dates: list[str], block_size: int) -> list[list[str]]:
    unique = sorted(set(dates))
    size = max(1, int(block_size))
    return [unique[index : index + size] for index in range(0, len(unique), size)]


def _percentile_ci(values: list[float]) -> list[float] | None:
    if not values:
        return None
    return [
        float(np.percentile(values, 2.5)),
        float(np.percentile(values, 97.5)),
    ]


def bootstrap_paired_binary(
    y: np.ndarray,
    model_prob: np.ndarray,
    baseline_prob: np.ndarray,
    dates: list[str],
    *,
    block_size: int = 5,
    repetitions: int = 2000,
    seed: int = 20261119,
) -> dict | None:
    blocks = _blocks(dates, block_size)
    if len(blocks) < 2:
        return None
    positions: dict[str, list[int]] = {}
    for index, day in enumerate(dates):
        positions.setdefault(day, []).append(index)
    rng = random.Random(seed)
    brier_values = []
    log_loss_values = []
    for _ in range(repetitions):
        sampled: list[str] = []
        for _ in blocks:
            sampled.extend(rng.choice(blocks))
        indices = np.asarray([index for day in sampled for index in positions[day]], dtype=int)
        if len(indices) == 0:
            continue
        labels = y[indices]
        model_brier = binary_brier(model_prob[indices], labels)
        baseline_brier = binary_brier(baseline_prob[indices], labels)
        brier_values.append(baseline_brier - model_brier)
        log_loss_values.append(
            binary_log_loss(baseline_prob[indices], labels)
            - binary_log_loss(model_prob[indices], labels)
        )
    brier_ci = _percentile_ci(brier_values)
    log_loss_ci = _percentile_ci(log_loss_values)
    if brier_ci is None or log_loss_ci is None:
        return None
    return {
        "brier_ci": brier_ci,
        "log_loss_ci": log_loss_ci,
        "repetitions": len(brier_values),
        "block_size": block_size,
        "blocks": len(blocks),
    }


def reliability_binary(
    probabilities: np.ndarray,
    y: np.ndarray,
    dates: list[str],
    *,
    bins: tuple[float, ...] = (0.0, 0.2, 0.4, 0.6, 0.8, 1.0),
    block_size: int = 5,
    repetitions: int = 2000,
    seed: int = 20261119,
) -> list[dict]:
    blocks = _blocks(dates, block_size)
    block_for_date = {day: index for index, days in enumerate(blocks) for day in days}
    positions: dict[str, list[int]] = {}
    for index, day in enumerate(dates):
        positions.setdefault(day, []).append(index)
    rng = random.Random(seed)
    samples: dict[int, list[float]] = {}
    for _ in range(repetitions):
        sampled: list[str] = []
        for _ in blocks:
            sampled.extend(rng.choice(blocks))
        indices = np.asarray([index for day in sampled for index in positions[day]], dtype=int)
        if len(indices) == 0:
            continue
        values = probabilities[indices]
        labels = y[indices]
        for bin_index, (low, high) in enumerate(zip(bins, bins[1:], strict=False)):
            in_bin = (values >= low) & (values < high if high < 1 else values <= high)
            if not in_bin.any():
                continue
            samples.setdefault(bin_index, []).append(
                float(values[in_bin].mean() - labels[in_bin].mean())
            )
    rows = []
    for bin_index, (low, high) in enumerate(zip(bins, bins[1:], strict=False)):
        in_bin = (probabilities >= low) & (
            probabilities < high if high < 1 else probabilities <= high
        )
        count = int(in_bin.sum())
        if count == 0:
            continue
        occupied_dates = {dates[index] for index in np.flatnonzero(in_bin)}
        occupied_blocks = len({block_for_date[day] for day in occupied_dates})
        rows.append(
            {
                "low": low,
                "high": high,
                "count": count,
                "dates": len(occupied_dates),
                "occupied_blocks": occupied_blocks,
                "mean_prediction": float(probabilities[in_bin].mean()),
                "observed_rate": float(y[in_bin].mean()),
                "difference_ci": (
                    _percentile_ci(samples.get(bin_index, [])) if occupied_blocks >= 2 else None
                ),
            }
        )
    return rows


def _kappa(left: list[bool], right: list[bool]) -> float | None:
    if not left:
        return None
    po = sum(a == b for a, b in zip(left, right, strict=True)) / len(left)
    p1 = sum(left) / len(left)
    p2 = sum(right) / len(right)
    pe = p1 * p2 + (1 - p1) * (1 - p2)
    return (po - pe) / (1 - pe) if pe < 1 else None


def case_target(case: dict, template: str) -> float | None:
    valid = [row for row in case.get("labels", []) if not row["abstain"]]
    if not valid:
        return None
    return float(sum(template in row["selected_patterns"] for row in valid) / len(valid))


def _label_gate(train_cases: list[dict], template: str, release: dict) -> dict:
    left: list[bool] = []
    right: list[bool] = []
    double_positive = 0
    double_negative = 0
    discordant: list[str] = []
    for case in train_cases:
        valid = [row for row in case["labels"] if not row["abstain"]]
        if len(valid) != 2:
            continue
        first = template in valid[0]["selected_patterns"]
        second = template in valid[1]["selected_patterns"]
        left.append(first)
        right.append(second)
        if first and second:
            double_positive += 1
        elif not first and not second:
            double_negative += 1
        else:
            discordant.append(case["case_id"])
    kappa = _kappa(left, right)
    reasons = []
    if len(left) < release["min_label_double_cases"]:
        reasons.append(f"double-valid cases {len(left)} < {release['min_label_double_cases']}")
    if double_positive < release["min_label_positive"]:
        reasons.append(f"double-positive cases {double_positive} < {release['min_label_positive']}")
    if double_negative < release["min_label_negative"]:
        reasons.append(f"double-negative cases {double_negative} < {release['min_label_negative']}")
    if kappa is None:
        reasons.append("Cohen kappa unavailable")
    elif kappa < release["min_label_kappa"]:
        reasons.append(f"Cohen kappa {kappa:.3f} < {release['min_label_kappa']:.2f}")
    if not reasons:
        status = "PASS"
    elif (
        len(left) >= release["min_label_double_cases"]
        and double_positive >= release["min_label_positive"]
        and double_negative >= release["min_label_negative"]
    ):
        status = "LOW_AGREEMENT"
    else:
        status = "INSUFFICIENT_EVIDENCE"
    return {
        "status": status,
        "double_valid_cases": len(left),
        "double_positive": double_positive,
        "double_negative": double_negative,
        "discordant_cases": discordant,
        "cohen_kappa": kappa,
        "reasons": reasons,
    }


def _load_cases(path: Path) -> dict[str, dict]:
    payload = json.loads(path.read_text())
    cases = payload.get("cases", payload) if isinstance(payload, dict) else payload
    if not isinstance(cases, list):
        raise ValueError("cases must be a JSON list")
    return {case["case_id"]: case for case in cases}


def validate_label_identities(rows: list[dict]) -> None:
    seen = set()
    for row in rows:
        case_id = str(row.get("case_id") or "").strip()
        annotator_id = str(row.get("annotator_id") or "").strip()
        if not case_id or not annotator_id:
            raise ValueError("labels require nonempty case_id and annotator_id")
        key = case_id, annotator_id
        if key in seen:
            raise ValueError(f"duplicate annotation for case {case_id} by {annotator_id}")
        seen.add(key)


def _load_labels(path: Path) -> list[dict]:
    if path.suffix.lower() == ".json":
        payload = json.loads(path.read_text())
        rows = payload.get("labels", payload) if isinstance(payload, dict) else payload
    else:
        with path.open(newline="") as stream:
            rows = list(csv.DictReader(stream))
    validate_label_identities(rows)
    output = []
    for row in rows:
        selected = row.get("selected_patterns", "")
        if isinstance(selected, str):
            selected = {
                item.strip() for item in selected.replace(",", "|").split("|") if item.strip()
            }
        else:
            selected = {str(item).strip() for item in selected if str(item).strip()}
        output.append(
            {
                "case_id": str(row["case_id"]).strip(),
                "annotator_id": str(row["annotator_id"]).strip(),
                "selected_patterns": selected,
                "abstain": str(row.get("abstain", "false")).lower() in {"1", "true", "yes", "y"},
                "split": str(row.get("split") or "ALL"),
                "direction": str(row.get("direction") or "ABSTAIN").upper(),
            }
        )
    return output


def _fit_model(
    x_train: np.ndarray,
    y_train: np.ndarray,
    x_cal: np.ndarray,
    y_cal: np.ndarray,
    x_test: np.ndarray,
) -> tuple[np.ndarray, dict]:
    x_train_s, x_cal_s, stats = standardize(x_train, x_cal)
    x_test_s = (x_test - stats[0]) / stats[1]
    weights, intercept = fit_binomial(x_train_s, y_train)
    cal_prob = predict_binomial(x_cal_s, weights, intercept)
    temperature = temperature_scale_binary(cal_prob, y_cal)
    test_prob = predict_binomial(x_test_s, weights, intercept, temperature)
    return test_prob, {
        "mean": stats[0],
        "scale": stats[1],
        "weights": weights,
        "intercept": intercept,
        "temperature": temperature,
    }


def _metrics(probabilities: np.ndarray, y: np.ndarray) -> dict:
    return {
        "brier": binary_brier(probabilities, y),
        "log_loss": binary_log_loss(probabilities, y),
    }


def _serialize(parameters: dict) -> dict:
    return {
        "mean": parameters["mean"].tolist(),
        "scale": parameters["scale"].tolist(),
        "weights": parameters["weights"].tolist(),
        "intercept": float(parameters["intercept"]),
        "temperature": parameters["temperature"],
    }


def train_label_models(
    cases_path: Path,
    labels_path: Path,
    output_dir: Path,
    *,
    release: dict | None = None,
) -> dict:
    thresholds = dict(DEFAULT_RELEASE)
    thresholds.update(release or {})
    cases = _load_cases(cases_path)
    labels = _load_labels(labels_path)
    by_case: dict[str, list[dict]] = defaultdict(list)
    for row in labels:
        if row["case_id"] not in cases:
            raise ValueError(f"label references unknown case: {row['case_id']}")
        by_case[row["case_id"]].append(row)
    for case_id, rows in by_case.items():
        splits = {row["split"] for row in rows}
        if len(splits) > 1:
            raise ValueError(f"case {case_id} spans multiple splits: {sorted(splits)}")
        cases[case_id]["labels"] = rows
        cases[case_id]["split"] = next(iter(splits))
    output_dir.mkdir(parents=True, exist_ok=True)
    models = {}
    for template in TEMPLATES:
        train_cases = [
            case
            for case in cases.values()
            if case.get("split") == "train"
            and any(not row["abstain"] for row in case.get("labels", []))
        ]
        calibration_cases = [
            case
            for case in cases.values()
            if case.get("split") == "calibration"
            and any(not row["abstain"] for row in case.get("labels", []))
        ]
        test_cases = [
            case
            for case in cases.values()
            if case.get("split") == "test"
            and any(not row["abstain"] for row in case.get("labels", []))
        ]
        counts = {
            "train": len(train_cases),
            "calibration": len(calibration_cases),
            "test": len(test_cases),
        }
        label_gate = _label_gate(train_cases, template, thresholds)
        if min(counts.values()) < 5 or label_gate["status"] != "PASS":
            models[template] = {
                "status": "INSUFFICIENT_EVIDENCE",
                "template": template,
                "label": "INDEPENDENT_RECOGNITION",
                "reasons": (
                    [f"{name} cases {value} < 5" for name, value in counts.items() if value < 5]
                    + label_gate["reasons"]
                ),
                "sample_counts": counts,
                "label_gate": label_gate,
            }
            continue

        y_train = np.asarray([case_target(case, template) for case in train_cases], dtype=float)
        y_cal = np.asarray([case_target(case, template) for case in calibration_cases], dtype=float)
        y_test = np.asarray([case_target(case, template) for case in test_cases], dtype=float)
        x_train = np.asarray([case_features(case) for case in train_cases], dtype=float)
        x_cal = np.asarray([case_features(case) for case in calibration_cases], dtype=float)
        x_test = np.asarray([case_features(case) for case in test_cases], dtype=float)
        test_prob, parameters = _fit_model(x_train, y_train, x_cal, y_cal, x_test)
        frequency = float(y_train.mean())
        frequency_prob = np.full(len(y_test), frequency)
        degree_train = x_train[:, :DEGREE_FEATURES]
        degree_cal = x_cal[:, :DEGREE_FEATURES]
        degree_test = x_test[:, :DEGREE_FEATURES]
        degree_prob, degree_parameters = _fit_model(
            degree_train, y_train, degree_cal, y_cal, degree_test
        )
        metrics = _metrics(test_prob, y_test)
        baseline_metrics = {
            "frequency": _metrics(frequency_prob, y_test),
            "degree": _metrics(degree_prob, y_test),
        }
        test_dates = [case["decision_date"] for case in test_cases]
        improvements = {}
        for name, baseline_prob in (
            ("frequency", frequency_prob),
            ("degree", degree_prob),
        ):
            improvement = {
                "brier": baseline_metrics[name]["brier"] - metrics["brier"],
                "log_loss": baseline_metrics[name]["log_loss"] - metrics["log_loss"],
            }
            bootstrap = bootstrap_paired_binary(
                y_test,
                test_prob,
                baseline_prob,
                test_dates,
                block_size=thresholds["block_size"],
                repetitions=thresholds["bootstrap_repetitions"],
                seed=thresholds["bootstrap_seed"],
            )
            improvement.update(
                bootstrap
                or {
                    "brier_ci": None,
                    "log_loss_ci": None,
                    "repetitions": 0,
                    "block_size": thresholds["block_size"],
                    "blocks": 0,
                }
            )
            improvements[name] = improvement
        reliability = reliability_binary(
            test_prob,
            y_test,
            test_dates,
            block_size=thresholds["block_size"],
            repetitions=thresholds["bootstrap_repetitions"],
            seed=thresholds["bootstrap_seed"],
        )
        support_bins = []
        for row in reliability:
            ci = row["difference_ci"]
            if (
                row["count"] >= thresholds["min_bin_count"]
                and row["dates"] >= thresholds["min_bin_dates"]
                and row["occupied_blocks"] >= 2
                and ci is not None
                and ci[0] >= -thresholds["reliability_tolerance"]
                and ci[1] <= thresholds["reliability_tolerance"]
            ):
                row["status"] = "SUPPORTED"
                support_bins.append(
                    {
                        "low": row["low"],
                        "high": row["high"],
                        "occupied_blocks": row["occupied_blocks"],
                    }
                )
            elif (
                row["count"] < thresholds["min_bin_count"]
                or row["dates"] < thresholds["min_bin_dates"]
                or row["occupied_blocks"] < 2
            ):
                row["status"] = "INSUFFICIENT_SUPPORT"
            else:
                row["status"] = "MISCALIBRATED"
        coverage = (
            float(
                np.mean(
                    [
                        any(
                            item["low"] <= probability < item["high"]
                            or (item["high"] == 1.0 and probability == 1.0)
                            for item in support_bins
                        )
                        for probability in test_prob
                    ]
                )
            )
            if support_bins
            else 0.0
        )
        has_low = any(item["low"] < 0.5 for item in support_bins)
        has_high = any(item["high"] > 0.5 for item in support_bins)
        reasons = []
        if len(test_cases) < thresholds["min_test_cases"]:
            reasons.append(f"test cases {len(test_cases)} < {thresholds['min_test_cases']}")
        test_dates_count = len(set(test_dates))
        if test_dates_count < thresholds["min_test_dates"]:
            reasons.append(f"test dates {test_dates_count} < {thresholds['min_test_dates']}")
        test_symbols = len({case["symbol"] for case in test_cases})
        if test_symbols < thresholds["min_test_symbols"]:
            reasons.append(f"test symbols {test_symbols} < {thresholds['min_test_symbols']}")
        for name in ("frequency", "degree"):
            improvement = improvements[name]
            if improvement["brier"] <= 0:
                reasons.append(f"{name}: Brier improvement <= 0")
            if improvement["brier_ci"] is None or improvement["brier_ci"][0] <= 0:
                reasons.append(f"{name}: Brier improvement 95% CI lower bound <= 0")
            if improvement["log_loss"] <= 0:
                reasons.append(f"{name}: log-loss improvement <= 0")
            if improvement["log_loss_ci"] is None or improvement["log_loss_ci"][0] <= 0:
                reasons.append(f"{name}: log-loss improvement 95% CI lower bound <= 0")
        if not has_low or not has_high:
            reasons.append("no supported reliability bins on both sides of 0.5")
        if coverage < thresholds["min_supported_coverage"]:
            reasons.append(
                f"supported-bin coverage {coverage:.3f} < {thresholds['min_supported_coverage']}"
            )
        status = "APPROVED" if not reasons else "INSUFFICIENT_EVIDENCE"
        models[template] = {
            "status": status,
            "template": template,
            "label": "INDEPENDENT_RECOGNITION",
            "reasons": reasons,
            "feature_order": FEATURE_ORDER,
            "sample_counts": counts,
            "label_gate": label_gate,
            "metrics": metrics,
            "baselines": {
                "frequency": {
                    "metrics": baseline_metrics["frequency"],
                    "probability": frequency,
                },
                "degree": {
                    "metrics": baseline_metrics["degree"],
                    **_serialize(degree_parameters),
                },
            },
            "improvement": improvements,
            "reliability": reliability,
            "support_bins": support_bins,
            "supported_coverage": coverage,
            "test_cases": len(test_cases),
            "test_dates": test_dates_count,
            "test_symbols": test_symbols,
            **_serialize(parameters),
        }
    model = {
        "schema_version": "1.0",
        "label_protocol_version": "1.0",
        "label_kind": "INDEPENDENT_RECOGNITION",
        "feature_order": FEATURE_ORDER,
        "release": thresholds,
        "models": models,
    }
    model["model_id"] = digest(model)
    atomic_write(output_dir / "label-model.json", canonical_json(model) + "\n")
    summary = {
        "model_id": model["model_id"],
        "label_kind": model["label_kind"],
        "release": thresholds,
        "models": {
            template: {
                "status": value["status"],
                "reasons": value.get("reasons"),
                "sample_counts": value.get("sample_counts"),
                "label_gate": value.get("label_gate"),
                "metrics": value.get("metrics"),
                "baselines": {
                    name: baseline.get("metrics")
                    for name, baseline in value.get("baselines", {}).items()
                },
                "improvement": value.get("improvement"),
                "support_bins": value.get("support_bins"),
                "supported_coverage": value.get("supported_coverage"),
                "test_cases": value.get("test_cases"),
                "test_dates": value.get("test_dates"),
                "test_symbols": value.get("test_symbols"),
            }
            for template, value in models.items()
        },
    }
    atomic_write(output_dir / "summary.json", canonical_json(summary) + "\n")
    files = ["label-model.json", "summary.json"]
    manifest = {
        "schema_version": "1.0",
        "model_id": model["model_id"],
        "label_protocol_version": model["label_protocol_version"],
        "feature_order": FEATURE_ORDER,
        "files": {
            name: hashlib.sha256((output_dir / name).read_bytes()).hexdigest() for name in files
        },
    }
    atomic_write(output_dir / "manifest.json", canonical_json(manifest) + "\n")
    return summary


def load_label_model(path: Path) -> dict:
    model = json.loads(path.read_text())
    recorded = model.get("model_id")
    if recorded != digest({key: value for key, value in model.items() if key != "model_id"}):
        raise ValueError("LABEL_MODEL_INVALID: model_id does not match content")
    return model


def predict_label_probabilities(cases: list[dict], model: dict) -> list[dict]:
    predictions = []
    for case in cases:
        values = case_features(case)
        row = {"case_id": case.get("case_id"), "probabilities": {}}
        for template, entry in model.get("models", {}).items():
            if entry.get("status") != "APPROVED":
                continue
            mean = np.asarray(entry["mean"], dtype=float)
            scale = np.asarray(entry["scale"], dtype=float)
            normalized = (np.asarray([values], dtype=float) - mean) / scale
            probability = predict_binomial(
                normalized,
                np.asarray(entry["weights"], dtype=float),
                float(entry["intercept"]),
                float(entry.get("temperature", 1.0)),
            )[0]
            if any(
                item.get("occupied_blocks", 0) >= 2
                and (
                    item["low"] <= probability < item["high"]
                    or item["high"] == 1
                    and probability == 1
                )
                for item in entry.get("support_bins", [])
            ):
                row["probabilities"][template] = float(probability)
        predictions.append(row)
    return predictions
