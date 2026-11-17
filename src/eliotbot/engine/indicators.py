"""Backward-looking indicators with explicitly fixed initialization."""

from collections.abc import Sequence
from dataclasses import dataclass

import numpy as np

from eliotbot.schemas.market import Bar


@dataclass(frozen=True)
class Indicators:
    atr: np.ndarray
    dif: np.ndarray
    dea: np.ndarray
    histogram: np.ndarray
    volume_ma: np.ndarray


def ema(values: Sequence[float], period: int) -> np.ndarray:
    data = np.asarray(values, dtype=float)
    if period < 1 or not np.isfinite(data).all():
        raise ValueError("EMA requires a positive period and finite input")
    result = np.empty(len(data))
    if len(data):
        result[0] = data[0]
        alpha = 2 / (period + 1)
        for i in range(1, len(data)):
            result[i] = alpha * data[i] + (1 - alpha) * result[i - 1]
    return result


def calculate(bars: Sequence[Bar], period: int = 14) -> Indicators:
    if period < 1:
        raise ValueError("ATR period must be positive")
    close = np.asarray([b.close for b in bars])
    high = np.asarray([b.high for b in bars])
    low = np.asarray([b.low for b in bars])
    volume = np.asarray([b.volume for b in bars])
    previous = np.concatenate((close[:1], close[:-1]))
    tr = np.maximum(high - low, np.maximum(abs(high - previous), abs(low - previous)))
    atr = np.full(len(bars), np.nan)
    if len(bars) >= period:
        atr[period - 1] = tr[:period].mean()
        for i in range(period, len(bars)):
            atr[i] = (atr[i - 1] * (period - 1) + tr[i]) / period
    dif = ema(close, 12) - ema(close, 26)
    dea = ema(dif, 9)
    volume_ma = np.full(len(bars), np.nan)
    if len(bars) >= 20:
        volume_ma[19:] = np.convolve(volume, np.ones(20) / 20, mode="valid")
    return Indicators(atr, dif, dea, 2 * (dif - dea), volume_ma)
