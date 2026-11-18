"""Basic multi-scale subwave presence checks.

This is deliberately not a full Elliott subwave certification. It checks that a
finer-scale zigzag contains enough alternating pivots inside each parent leg.
"""

from collections.abc import Sequence
from itertools import pairwise

from eliotbot.engine.matcher import Candidate
from eliotbot.engine.zigzag import ZigZagResult


def _expected_internal(candidate: Candidate) -> list[int]:
    legs = len(candidate.points) - 1
    if candidate.template.name in {"IMPULSE", "DIAGONAL"}:
        pattern = [4, 2, 4, 2, 4]
    elif candidate.template.name == "ZIGZAG_ABC":
        pattern = [4, 2, 4]
    elif candidate.template.name == "TRIANGLE":
        pattern = [2, 2, 2, 2, 2]
    else:
        pattern = [2, 2, 2]
    return pattern[:legs]


def _presence(candidate: Candidate, finer: ZigZagResult) -> float:
    expected = _expected_internal(candidate)
    scores = []
    for (left, right), need in zip(pairwise(candidate.points), expected, strict=False):
        inside = [p for p in finer.confirmed if left.index < p.index < right.index]
        scores.append(min(1.0, len(inside) / need))
    return sum(scores) / len(scores) if scores else 0.0


def subwave_fit(
    candidate: Candidate,
    zigzags: Sequence[ZigZagResult],
    scales: Sequence[float],
    depth: int,
) -> float | None:
    if depth <= 0:
        return None
    finer = sorted(
        (
            (scale, zigzag)
            for scale, zigzag in zip(scales, zigzags, strict=True)
            if scale < min(candidate.scales)
        ),
        key=lambda item: item[0],
        reverse=True,
    )
    if not finer:
        return None
    first = _presence(candidate, finer[0][1])
    if depth == 1 or len(finer) < 2:
        return first

    second = finer[1][1]
    sub_scores = []
    parent_legs = tuple(pairwise(candidate.points))
    for left, right in pairwise(finer[0][1].confirmed):
        if not any(a.index <= left.index < right.index <= b.index for a, b in parent_legs):
            continue
        inside = [p for p in second.confirmed if left.index < p.index < right.index]
        sub_scores.append(min(1.0, len(inside) / 2))
    second_fit = sum(sub_scores) / len(sub_scores) if sub_scores else first
    return (first + second_fit) / 2
