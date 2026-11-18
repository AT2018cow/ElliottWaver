"""Probe triangle child placement and scoring with independent price paths."""

import argparse
import hashlib
import json
from itertools import pairwise
from pathlib import Path

from acceptance_parent_subwave_20261118 import candidate, fixture

from eliotbot import ENGINE_VERSION
from eliotbot.engine.indicators import calculate
from eliotbot.engine.parents import attach_parent_context
from eliotbot.engine.rules import check_pattern
from eliotbot.engine.scorer import score
from eliotbot.engine.subwaves import certify_subwaves
from eliotbot.engine.templates import TRIANGLE, allowed_child_templates
from eliotbot.engine.zigzag import ZigZagResult
from eliotbot.util import atomic_write, digest

OUTER = ((0, 100), (100, 180), (220, 110), (320, 166), (430, 125), (520, 154))
INNER_E = ((430, 125), (446, 144), (460, 135), (485, 150), (500, 130), (520, 154))


def path_with_children(mode: str) -> list[tuple[int, float]]:
    path = list(OUTER)
    for (left, start), (right, end) in pairwise(OUTER):
        if mode == "nested_triangle" and right == OUTER[-1][0]:
            path.extend(INNER_E[1:-1])
            continue
        # A regular flat with B back to its origin and C equal to A. These
        # exact endpoints fail the engine's zigzag origin and progress rules.
        a, b = (
            (end, start)
            if mode == "five_flats"
            else (
                start + 0.7 * (end - start),
                start + 0.35 * (end - start),
            )
        )
        step = (right - left) // 3
        path.extend(((left + step, a), (left + 2 * step, b)))
    return sorted(path)


def probe(mode: str, direction: int) -> dict:
    path = path_with_children(mode)
    bars, points, finer = fixture(path, [index for index, _ in OUTER], direction)
    outer = candidate(TRIANGLE, points, bars, direction)
    row = {"direction": direction, "path_up": path, "outer_rule_failures": []}
    if mode == "nested_triangle":
        inner_points = tuple(point for point in finer if point.index >= INNER_E[0][0])
        inner = candidate(TRIANGLE, inner_points, bars, direction, (2.4,))
        link = attach_parent_context([outer, inner], "1d")[inner.key]
        row.update(
            inner_rule_failures=[],
            allowed_e=allowed_child_templates("TRIANGLE", 4),
            parent_context=link.status,
            parent_relation=link.relation,
            parent_leg=link.wave_label,
            passed=link.status == "PROVISIONAL" and link.wave_label == "E",
        )
        return row

    result = certify_subwaves(outer, [ZigZagResult(finer, None)], [2.4], 1, bars)
    breakdown = score(outer, bars, calculate(bars), volume_comparable=True, subwave_fit=result.fit)
    evidence = next(item for item in breakdown.evidence if item.feature == "subwave_structure")
    row.update(
        certificate=result.status,
        fit=result.fit,
        subwave_points=evidence.weight * evidence.fit,
        legs=[item.model_dump(mode="json") for item in result.evidence],
        passed=(result.status == "VERIFIED") == (mode == "five_zigzags"),
    )
    if mode == "five_flats":
        for left, right in pairwise(points):
            leg = tuple(point for point in finer if left.index <= point.index <= right.index)
            sign = 1 if right.price > left.price else -1
            assert all(
                rule.status == "PASS"
                for rule in check_pattern("FLAT", leg, bars, sign, developing=False)
            )
            assert any(
                rule.status == "FAIL"
                for rule in check_pattern("ZIGZAG_ABC", leg, bars, sign, developing=False)
            )
    return row


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output", type=Path, default=Path("reports/triangle-children-review.json")
    )
    args = parser.parse_args()
    import eliotbot

    src = Path(eliotbot.__file__).resolve().parents[1]
    record = {
        "engine_version": ENGINE_VERSION,
        "source_sha256": digest(
            {
                f"src/{path.relative_to(src)}": hashlib.sha256(path.read_bytes()).hexdigest()
                for path in sorted(src.rglob("*.py"))
            }
        ),
        "probe_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "checks": {
            mode: [probe(mode, direction) for direction in (1, -1)]
            for mode in ("nested_triangle", "five_flats", "five_zigzags")
        },
    }
    atomic_write(args.output, json.dumps(record, indent=2, ensure_ascii=False) + "\n")
    for mode, rows in record["checks"].items():
        print(mode, "PASS" if all(row["passed"] for row in rows) else "FAIL")
    return int(any(not row["passed"] for rows in record["checks"].values() for row in rows))


if __name__ == "__main__":
    raise SystemExit(main())
