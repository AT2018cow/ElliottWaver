"""Small NumPy-only multinomial calibration helpers for forecast research."""

from __future__ import annotations

import math
import random

import numpy as np


def softmax(logits: np.ndarray) -> np.ndarray:
    values = logits - np.max(logits, axis=1, keepdims=True)
    exp = np.exp(values)
    return exp / np.sum(exp, axis=1, keepdims=True)


def fit_softmax(
    x: np.ndarray,
    y: np.ndarray,
    n_classes: int,
    *,
    l2: float = 1e-3,
    epochs: int = 800,
    learning_rate: float = 0.2,
) -> tuple[np.ndarray, np.ndarray]:
    n_features = x.shape[1]
    weights = np.zeros((n_features, n_classes))
    intercept = np.zeros(n_classes)
    one_hot = np.eye(n_classes)[y]
    for _ in range(epochs):
        logits = x @ weights + intercept
        probabilities = softmax(logits)
        error = probabilities - one_hot
        weights -= learning_rate * (x.T @ error / len(x) + l2 * weights)
        intercept -= learning_rate * error.mean(axis=0)
    return weights, intercept


def predict(
    x: np.ndarray, weights: np.ndarray, intercept: np.ndarray, temperature: float = 1.0
) -> np.ndarray:
    return softmax((x @ weights + intercept) / temperature)


def standardize(train: np.ndarray, other: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    mean = train.mean(axis=0)
    scale = train.std(axis=0)
    scale[scale == 0] = 1.0
    return (train - mean) / scale, (other - mean) / scale, np.stack([mean, scale])


def temperature_scale(
    probabilities: np.ndarray,
    y: np.ndarray,
    temperatures=(0.5, 0.75, 1.0, 1.5, 2.0, 3.0),
) -> float:
    best = (math.inf, math.inf, 1.0)
    for temperature in temperatures:
        adjusted = np.clip(probabilities ** (1 / temperature), 1e-12, 1)
        adjusted = adjusted / adjusted.sum(axis=1, keepdims=True)
        loss = log_loss(adjusted, y)
        key = (loss, abs(temperature - 1.0), temperature)
        if key < best:
            best = key
    return best[2]


def log_loss(probabilities: np.ndarray, y: np.ndarray) -> float:
    selected = probabilities[np.arange(len(y)), y]
    return float(-np.mean(np.log(np.clip(selected, 1e-12, 1))))


def brier_loss(probabilities: np.ndarray, y: np.ndarray) -> np.ndarray:
    """Per-row Brier loss; binary events use the positive-class squared error."""
    if probabilities.shape[1] == 2:
        return (probabilities[:, 1] - (y == 1).astype(float)) ** 2
    one_hot = np.eye(probabilities.shape[1])[y]
    return np.sum((probabilities - one_hot) ** 2, axis=1)


def brier(probabilities: np.ndarray, y: np.ndarray) -> float:
    return float(np.mean(brier_loss(probabilities, y)))


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


def block_bootstrap_paired_improvement(
    y: np.ndarray,
    model_prob: np.ndarray,
    baseline_prob: np.ndarray,
    dates: list[str],
    *,
    block_size: int = 5,
    repetitions: int = 2000,
    seed: int = 20261119,
) -> dict | None:
    """Paired block bootstrap for Brier and log-loss improvement.

    The same sampled date blocks are applied to the model and baseline, so the
    interval is for ``baseline loss - model loss`` on identical observations.
    """
    blocks = _blocks(dates, block_size)
    if len(blocks) < 2:
        return None
    positions: dict[str, list[int]] = {}
    for index, day in enumerate(dates):
        positions.setdefault(day, []).append(index)
    rng = random.Random(seed)
    brier_values: list[float] = []
    log_loss_values: list[float] = []
    for _ in range(repetitions):
        sampled: list[str] = []
        for _ in blocks:
            sampled.extend(rng.choice(blocks))
        indices = np.asarray([index for day in sampled for index in positions[day]], dtype=int)
        if len(indices) == 0:
            continue
        model_rows = model_prob[indices]
        baseline_rows = baseline_prob[indices]
        labels = y[indices]
        model_brier = brier_loss(model_rows, labels)
        baseline_brier = brier_loss(baseline_rows, labels)
        brier_values.append(float(baseline_brier.mean() - model_brier.mean()))
        indices = np.arange(len(labels))
        model_ll = -np.log(np.clip(model_rows[indices, labels], 1e-12, 1))
        baseline_ll = -np.log(np.clip(baseline_rows[indices, labels], 1e-12, 1))
        log_loss_values.append(float(baseline_ll.mean() - model_ll.mean()))
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


def block_bootstrap_brier_ci(
    y: np.ndarray,
    model_prob: np.ndarray,
    baseline_prob: np.ndarray,
    dates: list[str],
    *,
    block_size: int = 5,
    repetitions: int = 2000,
    seed: int = 20261119,
) -> list[float] | None:
    result = block_bootstrap_paired_improvement(
        y,
        model_prob,
        baseline_prob,
        dates,
        block_size=block_size,
        repetitions=repetitions,
        seed=seed,
    )
    return None if result is None else result["brier_ci"]


def bootstrap_reliability(
    probabilities: np.ndarray,
    y: np.ndarray,
    dates: list[str],
    *,
    bins: tuple[float, ...] = (0.0, 0.2, 0.4, 0.6, 0.8, 1.0),
    block_size: int = 5,
    repetitions: int = 2000,
    seed: int = 20261119,
) -> list[dict]:
    """Per-class reliability bins with block-bootstrap intervals.

    Each row reports the mean predicted probability, observed rate and the
    bootstrap interval of ``mean_prediction - observed_rate``.
    """
    blocks = _blocks(dates, block_size)
    block_for_date = {day: index for index, days in enumerate(blocks) for day in days}
    positions: dict[str, list[int]] = {}
    for index, day in enumerate(dates):
        positions.setdefault(day, []).append(index)
    rng = random.Random(seed)
    samples: dict[tuple[int, int], list[float]] = {}
    for _ in range(repetitions):
        sampled: list[str] = []
        for _ in blocks:
            sampled.extend(rng.choice(blocks))
        indices = np.asarray([index for day in sampled for index in positions[day]], dtype=int)
        if len(indices) == 0:
            continue
        for class_index in range(probabilities.shape[1]):
            values = probabilities[indices, class_index]
            labels = y[indices]
            for bin_index, (low, high) in enumerate(zip(bins, bins[1:], strict=False)):
                in_bin = (values >= low) & (values < high if high < 1 else values <= high)
                if not in_bin.any():
                    continue
                samples.setdefault((class_index, bin_index), []).append(
                    float(values[in_bin].mean() - (labels[in_bin] == class_index).mean())
                )
    rows = []
    for class_index in range(probabilities.shape[1]):
        values = probabilities[:, class_index]
        for bin_index, (low, high) in enumerate(zip(bins, bins[1:], strict=False)):
            in_bin = (values >= low) & (values < high if high < 1 else values <= high)
            count = int(in_bin.sum())
            if count == 0:
                continue
            occupied_dates = {dates[index] for index in np.flatnonzero(in_bin)}
            occupied_blocks = len({block_for_date[day] for day in occupied_dates})
            rows.append(
                {
                    "class_index": class_index,
                    "low": low,
                    "high": high,
                    "count": count,
                    "dates": len(occupied_dates),
                    "occupied_blocks": occupied_blocks,
                    "mean_prediction": float(values[in_bin].mean()),
                    "observed_rate": float((y[in_bin] == class_index).mean()),
                    "difference_ci": (
                        _percentile_ci(samples.get((class_index, bin_index), []))
                        if occupied_blocks >= 2
                        else None
                    ),
                }
            )
    return rows


def reliability(
    probabilities: np.ndarray, y: np.ndarray, bins=(0.0, 0.2, 0.4, 0.6, 0.8, 1.0)
) -> list[dict]:
    rows = []
    confidence = probabilities.max(axis=1)
    prediction = probabilities.argmax(axis=1)
    for low, high in zip(bins, bins[1:], strict=False):
        mask = (confidence >= low) & (confidence < high if high < 1 else confidence <= high)
        if not mask.any():
            continue
        rows.append(
            {
                "low": low,
                "high": high,
                "count": int(mask.sum()),
                "mean_confidence": float(confidence[mask].mean()),
                "accuracy": float((prediction[mask] == y[mask]).mean()),
            }
        )
    return rows
