"""Independently verify ext5 analysis and acceptance fixes against the root commit.

Run from the repository root. This is an offline acceptance probe, not a normal
pytest test: exit code 1 means at least one acceptance condition failed.
"""

import argparse
import hashlib
import json
import math
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

from elliottwaver import ENGINE_VERSION
from elliottwaver.config import load_settings
from elliottwaver.data.demo import demo_bundle
from elliottwaver.data.snapshots import SnapshotStore
from elliottwaver.engine.indicators import calculate
from elliottwaver.engine.matcher import Candidate
from elliottwaver.engine.projector import project
from elliottwaver.engine.rules import check_pattern
from elliottwaver.engine.scorer import score
from elliottwaver.engine.templates import IMPULSE, ZIGZAG, PatternTemplate
from elliottwaver.schemas.market import AnalysisRequest, Bar
from elliottwaver.schemas.wave import PivotPoint
from elliottwaver.service import AnalysisService
from elliottwaver.util import atomic_write, canonical_json, digest
from elliottwaver.visualization.report_formatter import markdown_report


def fixture(
    prices: list[float],
    direction: int,
    developing: bool = False,
    template: PatternTemplate = IMPULSE,
) -> tuple[tuple[Bar, ...], Candidate]:
    """Use zero-width OHLC and 20 observations per leg; do not reuse test fixtures."""
    start = datetime(2020, 1, 1, tzinfo=UTC)
    closes = [prices[0]]
    for left, right in zip(prices, prices[1:], strict=False):
        closes.extend(left + (right - left) * step / 20 for step in range(1, 21))
    closes.append(prices[-1] - direction * 0.1)
    bars = tuple(
        Bar(
            start_at=start + timedelta(days=index),
            end_at=start + timedelta(days=index + 1),
            session_date=(start + timedelta(days=index)).date(),
            open=price,
            high=price,
            low=price,
            close=price,
            volume=1000,
            observed_at=start + timedelta(days=len(closes) + 1),
        )
        for index, price in enumerate(closes)
    )
    points = tuple(
        PivotPoint(
            pivot_id=f"p{index}",
            index=index * 20,
            extreme_bar_id=bars[index * 20].bar_id,
            extreme_at=bars[index * 20].end_at,
            confirmed_at=None
            if developing and index == len(prices) - 1
            else bars[index * 20 + 1].end_at,
            price=price,
            kind="LOW" if (index % 2 == 0) == (direction > 0) else "HIGH",
            state="DEVELOPING" if developing and index == len(prices) - 1 else "CONFIRMED",
            scale=1.4,
            threshold_at_extreme=1,
        )
        for index, price in enumerate(prices)
    )
    rules = check_pattern(template.name, points, bars, direction, developing=developing)
    assert not any(rule.status == "FAIL" for rule in rules), rules
    return bars, Candidate(
        template,
        points[:-1] if developing else points,
        points[-1],
        direction,
        "W5_DEVELOPING" if developing else "COMPLETE",
        rules,
        (1.4,),
    )


def fifth_fit(ratio1: float, ratio3: float) -> float:
    """Independent scalar oracle for the new documented best-of-two policy."""
    alternatives = []
    for actual, primary, secondary in (
        (ratio1, (0.618, 1, 1.618, 2.618), (0.382, 4.236)),
        (ratio3, (0.618, 1, 1.618), (0.382, 2.618)),
    ):
        alternatives.extend(math.exp(-0.5 * ((actual - value) / 0.15) ** 2) for value in primary)
        alternatives.extend(
            0.7 * math.exp(-0.5 * ((actual - value) / 0.15) ** 2) for value in secondary
        )
    return max(alternatives)


def root_commit() -> str:
    """Return the repository root commit without hard-coding a rewritten hash."""
    return subprocess.check_output(
        ["git", "rev-list", "--max-parents=0", "HEAD"], text=True
    ).strip()


def baseline_path(baseline: str, relative: str) -> tuple[str, str]:
    """Find a source path and package name in the baseline tree across renames."""
    packages = subprocess.check_output(
        ["git", "ls-tree", "--name-only", f"{baseline}:src"], text=True
    ).splitlines()
    for package in packages:
        path = f"src/{package}/{relative}"
        probe = subprocess.run(
            ["git", "cat-file", "-e", f"{baseline}:{path}"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        if probe.returncode == 0:
            return path, package
    raise FileNotFoundError(f"baseline source not found: {relative}")


def run(output: Path, validation: Path) -> int:
    checks = []
    baseline = root_commit()

    def check(name: str, passed: bool, details: object) -> None:
        checks.append({"check": name, "status": "PASS" if passed else "FAIL", "details": details})

    # Check off-grid ratios as well as exact preferred and secondary ratios.
    scored = 0
    legacy_scored = 0
    maximum_error = 0.0
    mirrors = 0
    for ratio in (0.382, 0.618, 0.83, 1.0, 1.27, 1.618, 2.0, 2.618, 3.2, 4.236):
        for third in (20.0, 34.0, 80.0):
            prices = [100, 120, 112, 112 + third, 112 + 0.764 * third]
            prices.append(prices[-1] + 20 * ratio)
            developing = prices[-1] <= prices[3]
            for scale in (0.1, 1.0, 7.5):
                values = [value * scale for value in prices]
                bars, candidate = fixture(values, 1, developing)
                original = score(candidate, bars, calculate(bars), volume_comparable=True)
                evidence = next(e for e in original.evidence if e.feature == "extension_5")
                expected = fifth_fit(ratio, 20 * ratio / third) * (0.8 if developing else 1)
                maximum_error = max(maximum_error, abs(evidence.fit - expected))
                assert abs(evidence.fit - expected) < 1e-11
                assert abs(original.quality_score - original.fit_score * original.coverage) < 1e-10
                legacy = score(
                    candidate,
                    bars,
                    calculate(bars),
                    volume_comparable=True,
                    profile="ew_score_v1",
                )
                legacy_fit = next(e.fit for e in legacy.evidence if e.feature == "extension_5")
                legacy_expected = max(
                    math.exp(-0.5 * ((ratio - value) / 0.10) ** 2) for value in (0.618, 1.0)
                )
                legacy_expected = max(
                    legacy_expected, 0.7 * math.exp(-0.5 * ((ratio - 1.618) / 0.10) ** 2)
                ) * (0.8 if developing else 1)
                assert abs(legacy_fit - legacy_expected) < 1e-11
                legacy_scored += 1
                inverse, opposite = fixture([10000 - value for value in values], -1, developing)
                reflected = score(opposite, inverse, calculate(inverse), volume_comparable=True)
                reflected_evidence = next(
                    e for e in reflected.evidence if e.feature == "extension_5"
                )
                assert abs(reflected_evidence.fit - evidence.fit) < 1e-10
                assert abs(reflected.quality_score - original.quality_score) < 1e-8
                scored += 1
                mirrors += 1
    check(
        "fifth_score_arithmetic_and_symmetry",
        True,
        {
            "cases": scored,
            "legacy_cases": legacy_scored,
            "mirrors": mirrors,
            "maximum_error": maximum_error,
        },
    )

    projections = 0
    for template, prices in (
        (IMPULSE, [100, 130, 115, 150, 132, 160]),
        (ZIGZAG, [100, 130, 115, 160]),
    ):
        for current, expected_centers in (
            (160.0, [137.08, 130, 122.92, 112.84, 100]),
            (121.0, [112.84, 100]),
            (100.0, []),
            (90.0, []),
        ):
            for direction in (1, -1):
                values = prices if direction == 1 else [500 - value for value in prices]
                bars, candidate = fixture(values, direction, template=template)
                latest = current if direction == 1 else 500 - current
                bars = bars[:-1] + (
                    Bar(
                        **{
                            **bars[-1].model_dump(),
                            **dict.fromkeys(("open", "high", "low", "close"), latest),
                        }
                    ),
                )
                conditions, zones, path = project(
                    candidate, bars, 1, "probe", bars[-1].end_at, 0.01
                )
                actual = [(zone.low + zone.high) / 2 for zone in zones]
                expected = (
                    expected_centers
                    if direction == 1
                    else [500 - value for value in expected_centers]
                )
                assert len(actual) == len(expected), (actual, expected)
                assert all(abs(a - b) < 1e-10 for a, b in zip(actual, expected, strict=True))
                assert all(zone.independent_method_count == 1 for zone in zones)
                assert all(-direction * (value - latest) > 0.01 for value in actual)
                assert all(point.timeframe == "1d" for point in path)
                assert len(conditions) == 1 and conditions[0].kind == "STAGE_EXIT"
                projections += 1
    check("completed_deep_targets_and_exhaustion", True, {"cases": projections})

    settings = load_settings()
    service = AnalysisService(settings)
    bundle = demo_bundle()
    request = AnalysisRequest(symbol="AAPL", as_of=bundle.as_of)
    normal = service.analyze_bundle(request, bundle)
    strict = AnalysisService(
        settings.model_copy(
            update={"scoring": settings.scoring.model_copy(update={"min_quality": 99})}
        )
    ).analyze_bundle(request, bundle)
    excluded = [warning for warning in strict.warnings if warning.startswith("EXCLUDED_STRUCTURE:")]
    check(
        "excluded_candidates_do_not_receive_weight",
        not strict.scenarios and strict.omitted_weight == 0 and 1 <= len(excluded) <= 3,
        {
            "status": strict.status,
            "displayed": len(strict.scenarios),
            "excluded_messages": len(excluded),
        },
    )

    # An S2/S4 target set must not be advertised as a contiguous S2-S4 range.
    with_target = next(s for s in normal.scenarios if s.target_zones)
    scenarios = tuple(
        with_target.model_copy(
            update={
                "scenario_id": f"independent-{index}",
                "relative_weight": 0.25,
                "target_zones": with_target.target_zones if index in (2, 4) else (),
                "projected_path": with_target.projected_path if index in (2, 4) else (),
            }
        )
        for index in range(1, 5)
    )
    synthetic = normal.model_copy(
        update={
            "scenarios": scenarios,
            "omitted_weight": 0,
            "warnings": normal.warnings + ("EXCLUDED_STRUCTURE: independent fixture",),
        }
    )
    hint = next(
        line for line in markdown_report(synthetic).splitlines() if "S1 当前无合法目标" in line
    )
    check(
        "EXT5-R01_noncontiguous_target_hint",
        "S2-S4" not in hint and "S2" in hint and "S4" in hint,
        {
            "target_scenarios": ["S2", "S4"],
            "no_target_scenarios": ["S1", "S3"],
            "actual_hint": hint,
        },
    )

    old = json.loads(Path("docs/validation_20261117.json").read_text())
    new = json.loads(validation.read_text())
    reference_ext5 = {
        case["label"]: case
        for case in json.loads(Path("docs/validation_20261117_ext5.json").read_text())["cases"]
    }
    store = SnapshotStore(Path(".cache/live-smoke"))
    unchanged_business = []
    case_details = []
    original = {case["label"]: case for case in old["cases"]}
    for case in new["cases"]:
        frozen = store.load(case["source_snapshot_id"])
        request = AnalysisRequest(
            symbol=case["symbol"],
            timeframe=case["timeframe"],
            as_of=frozen.as_of,
            mode="FROZEN_DATA_REPLAY",
        )
        result = service.analyze_bundle(request, frozen)
        assert result.result_digest == case["result_digest"]
        assert (
            digest(
                result.model_dump(
                    mode="json", exclude={"run_id", "result_digest", "analyzed_at", "mode"}
                )
            )
            == result.result_digest
        )
        report = Path(case["report"])
        manifest = json.loads((report / "manifest.json").read_text())
        for filename, checksum in manifest["files"].items():
            assert hashlib.sha256((report / filename).read_bytes()).hexdigest() == checksum
        assert all(
            rule.status != "FAIL" for scenario in result.scenarios for rule in scenario.rules
        )
        assert all(
            w.pivot.confirmed_at <= result.as_of
            for scenario in result.scenarios
            for w in scenario.completed_waves
        )
        assert (
            abs(
                sum(s.relative_weight for s in result.scenarios)
                + result.omitted_weight
                - (1 if result.scenarios else 0)
            )
            < 1e-10
        )
        previous = json.loads(
            (Path(original[case["label"]]["report"]) / "analysis.json").read_text()
        )
        recorded = json.loads((report / "analysis.json").read_text())
        ignored = {"engine_version", "result_digest", "run_id", "analyzed_at"}
        business_changes = [
            key for key in previous if key not in ignored and previous[key] != recorded[key]
        ]
        reference = json.loads(
            (Path(reference_ext5[case["label"]]["report"]) / "analysis.json").read_text()
        )
        if all(previous[key] == reference[key] for key in previous if key not in ignored):
            assert previous["engine_version"] != reference["engine_version"]
            assert previous["result_digest"] != reference["result_digest"]
            unchanged_business.append(
                {
                    "label": case["label"],
                    "before_id": previous["result_digest"],
                    "after_id": reference["result_digest"],
                }
            )
        case_details.append(
            {
                "label": case["label"],
                "status": result.status,
                "business_changes": business_changes,
                "result_digest": result.result_digest,
            }
        )
    check("real_snapshot_recompute_and_artifact_integrity", True, {"cases": case_details})
    claim = "样本结果身份保持不变" in Path("docs/IMPLEMENTATION_STATUS.md").read_text()
    changed_ids = [case for case in unchanged_business if case["before_id"] != case["after_id"]]
    check(
        "EXT5-R02_result_identity_documentation",
        not (claim and changed_ids),
        {
            "claims_unchanged_identity": claim,
            "unchanged_business_but_changed_identity": changed_ids,
        },
    )

    source_hash = digest(
        {
            str(path): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in sorted(Path("src").rglob("*.py"))
        }
    )
    check(
        "submitted_evidence_matches_source",
        source_hash == new["source_sha256"] and settings.computation_hash == new["config_hash"],
        {
            "actual": source_hash,
            "recorded": new["source_sha256"],
            "actual_config": settings.computation_hash,
            "recorded_config": new["config_hash"],
        },
    )
    # Record the policy change separately; consistency with v2 is not proof that
    # v2 is statistically better, or that it implements the original v1 table.
    baseline_namespace = {"__name__": "ext5_baseline_scorer"}
    scorer_path, baseline_package = baseline_path(baseline, "engine/scorer.py")
    baseline_source = subprocess.check_output(["git", "show", f"{baseline}:{scorer_path}"]).decode()
    # The root commit predates the 0.6.0 package rename; its imports still use
    # the historical package name. The imported contracts are otherwise identical.
    baseline_source = baseline_source.replace(f"from {baseline_package}", "from elliottwaver")
    exec(compile(baseline_source, f"{baseline}:scorer.py", "exec"), baseline_namespace)
    policy_bars, policy_candidate = fixture([100, 120, 112, 146, 137.976, 170.336], 1)
    policy_indicators = calculate(policy_bars)
    old_score = baseline_namespace["score"](
        policy_candidate, policy_bars, policy_indicators, volume_comparable=True
    )
    new_score = score(policy_candidate, policy_bars, policy_indicators, volume_comparable=True)
    legacy_score = score(
        policy_candidate,
        policy_bars,
        policy_indicators,
        volume_comparable=True,
        profile="ew_score_v1",
    )
    assert abs(legacy_score.quality_score - old_score.quality_score) < 1e-12
    assert abs(legacy_score.fit_score - old_score.fit_score) < 1e-12
    assert legacy_score.coverage == old_score.coverage
    policy_observation = {
        "prices": [100, 120, 112, 146, 137.976, 170.336],
        "configured_profile": settings.scoring.profile,
        "baseline_fifth_fit": next(
            item.fit for item in old_score.evidence if item.feature == "extension_5"
        ),
        "current_fifth_fit": next(
            item.fit for item in new_score.evidence if item.feature == "extension_5"
        ),
        "baseline_quality": old_score.quality_score,
        "current_quality": new_score.quality_score,
        "legacy_profile_quality": legacy_score.quality_score,
        "classification": "Explicit v1/v2 profiles; v1 preserves baseline scoring",
    }
    names = subprocess.check_output(["git", "diff", "--name-only", baseline]).decode().splitlines()
    names = [
        name
        for name in names
        if Path(name).is_file()
        and not name.startswith("docs/reviews/")
        and name != "scripts/acceptance_deepseek_ext5.py"
    ]
    names.append("docs/validation_20261117_ext5.json")
    names.append(str(validation))
    summary = {
        "baseline_commit": baseline,
        "reviewed_at": datetime.now(UTC),
        "engine_version": ENGINE_VERSION,
        "probe_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "validation": str(validation),
        "reviewed_file_sha256": {
            name: hashlib.sha256(Path(name).read_bytes()).hexdigest() for name in names
        },
        "scope": "Offline ext5 acceptance; no live API calls, PIT certification, calibration, or profitability claim",
        "score_policy_observation": policy_observation,
        "checks": checks,
        "passed": sum(item["status"] == "PASS" for item in checks),
        "failed": sum(item["status"] == "FAIL" for item in checks),
    }
    atomic_write(output, canonical_json(summary) + "\n")
    for item in checks:
        print(item["status"], item["check"])
    print(f"Evidence: {output}")
    return int(summary["failed"] > 0)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("docs/reviews/2026-11-17-deepseek-ext5-fixed-evidence.json"),
    )
    parser.add_argument(
        "--validation", type=Path, default=Path("docs/validation_20261117_ext5_fixed.json")
    )
    arguments = parser.parse_args()
    raise SystemExit(run(arguments.output, arguments.validation))
