"""Heuristic wave degree from the bar span of a candidate."""

from collections.abc import Sequence
from typing import Literal

from eliotbot.schemas.wave import PivotPoint

Degree = Literal["MINOR", "INTERMEDIATE", "PRIMARY", "CYCLE", "SUPERCYCLE", "UNKNOWN"]


def wave_degree(points: Sequence[PivotPoint], timeframe: str) -> Degree:
    if len(points) < 2:
        return "UNKNOWN"
    span = points[-1].index - points[0].index
    if timeframe == "1w":
        thresholds = (13, 52, 260, 520)
    elif timeframe == "1d":
        thresholds = (60, 250, 1000, 2500)
    elif timeframe.endswith("m"):
        thresholds = (100, 500, 2000, 4000)
    else:
        return "UNKNOWN"
    if span <= thresholds[0]:
        return "MINOR"
    if span <= thresholds[1]:
        return "INTERMEDIATE"
    if span <= thresholds[2]:
        return "PRIMARY"
    if span <= thresholds[3]:
        return "CYCLE"
    return "SUPERCYCLE"
