"""Independently check parent placement and the impulse wave-3 subdivision rule.

A nonzero exit means an acceptance expectation failed. Frozen-data checks need
the local snapshot cache; synthetic checks do not need network or credentials.
"""

import argparse
import hashlib
import json
import subprocess
from collections.abc import Sequence
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import patch

from eliotbot import ENGINE_VERSION
from eliotbot.config import load_settings
from eliotbot.data.snapshots import SnapshotStore
from eliotbot.engine.indicators import calculate
from eliotbot.engine.matcher import Candidate, SearchBudget, search
from eliotbot.engine.parents import attach_parent_context
from eliotbot.engine.rules import check_pattern
from eliotbot.engine.scorer import score
from eliotbot.engine.subwaves import certify_subwaves
from eliotbot.engine.templates import IMPULSE, ZIGZAG, PatternTemplate
from eliotbot.engine.zigzag import ZigZagResult
from eliotbot.schemas.market import AnalysisRequest, Bar
from eliotbot.schemas.wave import PivotPoint
from eliotbot.service import AnalysisService
from eliotbot.util import atomic_write, digest


def fixture(
    path: Sequence[tuple[int, float]], parent_indices: Sequence[int], direction: int
) -> tuple[tuple[Bar, ...], tuple[PivotPoint, ...], tuple[PivotPoint, ...]]:
    """Interpolate every bar; supply confirmed, alternating pivots in one coordinate."""
    samples = dict(path)
    start = datetime(2024, 1, 1, tzinfo=UTC)
    values = []
    for (left, a), (right, b) in zip(path, path[1:], strict=False):
        values.extend(a + (b - a) * (i - left) / (right - left) for i in range(left, right))
    values.append(path[-1][1])
    # The final extreme has a later reversal bar available for confirmation.
    values.append(values[-1] + (-1 if len(path) % 2 == 0 else 1))
    bars = []
    for index, value in enumerate(values):
        value = value if direction == 1 else 300 - value
        now = start + timedelta(days=index)
        bars.append(
            Bar(
                start_at=now,
                end_at=now + timedelta(hours=1),
                session_date=now.date(),
                observed_at=now + timedelta(hours=1),
                open=value,
                high=value,
                low=value,
                close=value,
                volume=100,
            )
        )
    fine = tuple(
        PivotPoint(
            pivot_id=f"fine-{index}",
            index=index,
            extreme_bar_id=bars[index].bar_id,
            extreme_at=bars[index].start_at,
            confirmed_at=bars[-1].end_at,
            price=samples[index] if direction == 1 else 300 - samples[index],
            kind="LOW" if (position % 2 == 0) == (direction == 1) else "HIGH",
            state="CONFIRMED",
            scale=2.4,
            threshold_at_extreme=1,
        )
        for position, (index, _) in enumerate(path)
    )
    by_index = {point.index: point for point in fine}
    parent = tuple(by_index[index].model_copy(update={"scale": 3.8}) for index in parent_indices)
    return tuple(bars), parent, fine


def candidate(
    template: PatternTemplate,
    points: tuple[PivotPoint, ...],
    bars: Sequence[Bar],
    direction: int,
    scales: tuple[float, ...] = (3.8,),
) -> Candidate:
    rules = check_pattern(template.name, points, bars, direction, developing=False)
    assert all(rule.status == "PASS" for rule in rules), rules
    return Candidate(template, points, points[-1], direction, "COMPLETE", rules, scales)


def parent_checks() -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    rows, controls = [], []
    for direction in (1, -1):
        for corrective in (True, False):
            tail = (
                [(230, 145), (260, 130), (300, 180)]
                if corrective
                else [(220, 150), (240, 135), (260, 165), (280, 155), (300, 180)]
            )
            bars, points, fine = fixture(
                [(0, 100), (100, 150), (200, 120), *tail], [0, 100, 200, 300], direction
            )
            parent = candidate(ZIGZAG, points, bars, direction)
            child = candidate(
                ZIGZAG if corrective else IMPULSE,
                tuple(point for point in fine if point.index >= 200),
                bars,
                direction,
                (2.4,),
            )
            link = attach_parent_context([parent, child], "1d")[child.key]
            row = {
                "direction": direction,
                "parent_pattern": parent.template.name,
                "child_pattern": child.template.name,
                "parent_hard_rule_failures": [],
                "child_hard_rule_failures": [],
                "observed": asdict(link),
            }
            if corrective:
                row["expected"] = "A zigzag cannot fill the entire motive C leg of a zigzag"
                row["status"] = "PASS" if link.parent_key is None else "FAIL"
                rows.append(row)
            else:
                row["status"] = (
                    "PASS"
                    if link.status == "VERIFIED"
                    and link.relation == "EXACT_LEG"
                    and link.wave_label == "C"
                    else "FAIL"
                )
                controls.append(row)
    return rows, controls


def subwave_checks() -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    rows, controls = [], []
    for direction in (1, -1):
        for diagonal in (True, False):
            prices = {
                0: 100,
                10: 120,
                20: 110,
                30: 150,
                40: 135,
                50: 160,
                2: 105,
                4: 102,
                6: 112,
                8: 106,
                12: 116,
                16: 119,
                22: 125,
                24: 118,
                26: 140,
                28: 132,
                32: 144,
                36: 148,
                42: 145,
                44: 140,
                46: 155,
                48: 148,
            }
            if diagonal:
                # Affine copy of [100,130,115,141,128,149] between parent 2 and 3.
                for index, value in zip((22, 24, 26, 28), (30, 15, 41, 28), strict=True):
                    prices[index] = 110 + value * 40 / 49
            bars, points, fine = fixture(sorted(prices.items()), [0, 10, 20, 30, 40, 50], direction)
            parent = candidate(IMPULSE, points, bars, direction)
            certification = certify_subwaves(parent, [ZigZagResult(fine, None)], [2.4], 1, bars)
            result = score(
                parent,
                bars,
                calculate(bars),
                volume_comparable=True,
                subwave_fit=certification.fit,
            )
            subwave_score = next(e for e in result.evidence if e.feature == "subwave_structure")
            row = {
                "direction": direction,
                "parent_points": [point.price for point in points],
                "parent_hard_rule_failures": [],
                "observed": asdict(certification),
                "subwave_score": subwave_score.model_dump(mode="json"),
                "awarded_subwave_points": subwave_score.weight * subwave_score.fit,
            }
            if diagonal:
                child_points = tuple(point for point in fine if 20 <= point.index <= 30)
                row["third_wave_points"] = [point.price for point in child_points]
                row["third_as_impulse_failures"] = [
                    rule.rule_id
                    for rule in check_pattern(
                        "IMPULSE", child_points, bars, direction, developing=False
                    )
                    if rule.status == "FAIL"
                ]
                row["expected"] = "Impulse subwave 3 must itself be an impulse, never a diagonal"
                row["status"] = (
                    "PASS"
                    if certification.status == "UNVERIFIED"
                    and certification.evidence[2].status == "UNVERIFIED"
                    and abs(subwave_score.weight * subwave_score.fit - 12) < 1e-9
                    else "FAIL"
                )
                rows.append(row)
            else:
                row["status"] = (
                    "PASS"
                    if certification.status == "VERIFIED" and certification.fit == 1
                    else "FAIL"
                )
                controls.append(row)
    return rows, controls


def frozen_check() -> dict[str, Any]:
    evidence = json.loads(Path("docs/validation_20261118_cn_index_v3.json").read_text())
    case = next(item for item in evidence["cases"] if item["label"] == "szcomp_day")
    settings = load_settings()
    assert settings.computation_hash == evidence["config_hash"]
    bundle = SnapshotStore(Path(".cache/eliotbot")).load(case["source_snapshot_id"])
    request = AnalysisRequest(
        symbol=case["symbol"],
        timeframe=case["timeframe"],
        as_of=bundle.as_of,
        mode="FROZEN_DATA_REPLAY",
    )
    captured = []

    def capture(*args: Any, **kwargs: Any) -> tuple[list[Candidate], SearchBudget]:
        found, budget = search(*args, **kwargs)
        captured.extend(found)
        return found, budget

    with patch("eliotbot.service.search", side_effect=capture):
        result = AnalysisService(settings).analyze_bundle(request, bundle)
    assert result.input_snapshot_id == case["analyzed_snapshot_id"]
    by_id = {item.key: item for item in captured}
    # Check the original pair is still enumerated and the child still appears in the report.
    child = by_id["5834610ff7db5b795abe8d1a"]
    parent = by_id["fbc1f1e2fe37fc06d73db4d2"]
    scenario = next(item for item in result.scenarios if item.scenario_id == child.key)
    assert child.template.name == "IMPULSE" and child.stage == "COMPLETE"
    assert parent.template.name == "TRIANGLE"
    assert all(rule.status != "FAIL" for rule in (*parent.rules, *child.rules))
    assert (child.points[0].index, child.points[-1].index) == (
        parent.points[2].index,
        parent.points[3].index,
    )
    invalid_link = scenario.parent_scenario_id == parent.key or (
        scenario.parent_pattern == "TRIANGLE" and scenario.parent_relation == "EXACT_LEG"
    )
    return {
        "status": "FAIL" if invalid_link else "PASS",
        "source_snapshot_id": bundle.snapshot_id,
        "prior_result_digest": case["result_digest"],
        "result_digest": result.result_digest,
        "config_hash": result.config_hash,
        "scenario_id": child.key,
        "rejected_parent_id": parent.key,
        "rejected_parent_leg": "C",
        "observed_parent_id": scenario.parent_scenario_id,
        "observed_parent_pattern": scenario.parent_pattern,
        "observed_parent_leg": scenario.parent_wave_label,
        "observed_parent_relation": scenario.parent_relation,
        "observed_parent_context": scenario.parent_context,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output", type=Path, default=Path("reports/parent-subwave-acceptance.json")
    )
    parser.add_argument("--frozen", action="store_true")
    parser.add_argument("--baseline", type=Path)
    args = parser.parse_args()
    source = digest(
        {
            str(path): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in sorted(Path("src").rglob("*.py"))
        }
    )
    parents, parent_controls = parent_checks()
    subwaves, subwave_controls = subwave_checks()
    checks = {"P3-R01": parents, "P3-R02": subwaves}
    record = {
        "head_commit": subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip(),
        "source_sha256": source,
        "probe_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "engine_version": ENGINE_VERSION,
        "validated_at": datetime.now(UTC).isoformat(),
        "checks": checks,
        "positive_controls": {"exact_motive_C": parent_controls, "five_three": subwave_controls},
    }
    if args.frozen:
        record["frozen_parent_placement"] = frozen_check()
    if args.baseline:
        baseline = json.loads(args.baseline.read_text())
        assert baseline["source_sha256"] == source
        record["baseline_regression"] = baseline
        record["baseline_digest_matches"] = sum(
            case["result_digest"] == case["prior_result_digest"]
            for case in baseline["frozen_cases"]
        )
    atomic_write(
        args.output,
        json.dumps(
            record, indent=2, ensure_ascii=False, default=lambda v: v.model_dump(mode="json")
        )
        + "\n",
    )
    failed = False
    for name, rows in (*checks.items(), *record["positive_controls"].items()):
        status = "PASS" if all(row["status"] == "PASS" for row in rows) else "FAIL"
        print(f"{name}: {status}")
        failed |= status == "FAIL"
    if args.frozen:
        status = record["frozen_parent_placement"]["status"]
        print(f"Frozen parent placement: {status}")
        failed |= status == "FAIL"
    print(f"Evidence: {args.output}")
    return int(failed)


if __name__ == "__main__":
    raise SystemExit(main())
