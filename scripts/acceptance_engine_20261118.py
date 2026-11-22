"""Reproduce engine acceptance findings and compare local frozen evidence."""

import argparse
import hashlib
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory

from typer.testing import CliRunner

from elliottwaver.cli.main import app
from elliottwaver.config import load_settings, long_term_settings
from elliottwaver.data.snapshots import SnapshotStore
from elliottwaver.engine.matcher import Candidate
from elliottwaver.engine.projector import project
from elliottwaver.engine.rules import check_pattern
from elliottwaver.engine.subwaves import certify_subwaves
from elliottwaver.engine.templates import DIAGONAL, FLAT, TRIANGLE, ZIGZAG
from elliottwaver.engine.zigzag import ZigZagResult
from elliottwaver.evaluation.replay import replay
from elliottwaver.schemas.market import AnalysisRequest, Bar
from elliottwaver.schemas.wave import PivotPoint
from elliottwaver.service import AnalysisService, prepare_bundle
from elliottwaver.util import atomic_write, digest
from elliottwaver.visualization.report_formatter import publish_report


def fixture(prices: list[int], indices: list[int] | None = None, direction: int = 1):
    indices = indices or list(range(len(prices)))
    start = datetime(2024, 1, 1, tzinfo=UTC)
    samples = dict(zip(indices, prices, strict=True))
    price = prices[0]
    bars, pivots = [], []
    for i in range(indices[-1] + 1):
        day = start + timedelta(days=i)
        price = samples.get(i, price)
        bar = Bar(
            start_at=day,
            end_at=day + timedelta(hours=1),
            session_date=day.date(),
            observed_at=day + timedelta(hours=1),
            open=price,
            high=price,
            low=price,
            close=price,
            volume=100,
        )
        bars.append(bar)
        if i in samples:
            kind = "LOW" if (len(pivots) % 2 == 0) == (direction == 1) else "HIGH"
            pivots.append(
                PivotPoint(
                    pivot_id=f"p{i}",
                    index=i,
                    extreme_bar_id=bar.bar_id,
                    extreme_at=bar.end_at,
                    confirmed_at=bar.end_at,
                    price=price,
                    kind=kind,
                    state="CONFIRMED",
                    scale=3.8,
                    threshold_at_extreme=1,
                )
            )
    return bars, tuple(pivots)


def geometry_checks() -> dict:
    mirrored = {}
    for direction in (1, -1):
        prices = [100, 130, 125, 145, 128, 160]
        bars, points = fixture(
            prices if direction == 1 else [300 - p for p in prices], direction=direction
        )
        rules = check_pattern(DIAGONAL.name, points, bars, direction, developing=False)
        mirrored[str(direction)] = [r.rule_id for r in rules if r.status == "FAIL"]

    # A-C rises 7 points per bar; B-D rises only 15/101 points per bar.
    bars, points = fixture([100, 120, 115, 134, 130, 148], [0, 10, 11, 12, 112, 113])
    triangle_rules = check_pattern(TRIANGLE.name, points, bars, 1, developing=False)
    shape = next(r.status for r in triangle_rules if r.rule_id == "TRIANGLE_SHAPE")

    bars, points = fixture([100, 120, 99])
    flat_rules = check_pattern(FLAT.name, points, bars, 1, developing=True)
    flat = Candidate(FLAT, points[:-1], points[-1], 1, "B_DEVELOPING", flat_rules, (3.8,))
    conditions, _, _ = project(flat, bars, 1, "fixture", bars[-1].end_at, 0.01)
    contradicted = [
        c.model_dump(mode="json")
        for c in conditions
        if c.kind == "INVALIDATION" and c.side == "LOW" and c.op == "<=" and bars[-1].low <= c.price
    ]
    return {
        "ENG-R01": {
            "status": "PASS"
            if all("W3_NOT_SHORTEST" in failures for failures in mirrored.values())
            else "FAIL",
            "leg_lengths": [30, 20, 32],
            "failed_rules": mirrored,
        },
        "ENG-R02": {
            "status": "PASS" if shape == "FAIL" else "FAIL",
            "rule": shape,
            "ac_slope": 7,
            "bd_slope": 15 / 101,
        },
        "ENG-R03": {
            "status": "PASS" if not contradicted else "FAIL",
            "hard_rule_failures": [r.rule_id for r in flat_rules if r.status == "FAIL"],
            "crossed_conditions": contradicted,
        },
    }


def subwave_check() -> dict:
    bars, points = fixture([100, 120, 110, 130], [10, 20, 25, 30])
    parent = Candidate(ZIGZAG, points, points[-1], 1, "COMPLETE", (), (3.8,))
    _, left_right = fixture([101, 132], [0, 40])
    _, outside = fixture([110, 102], [2, 4], direction=-1)
    mid = ZigZagResult(left_right, None)
    coarse = ZigZagResult(points, None)
    base = certify_subwaves(
        parent, [ZigZagResult((), None), mid, coarse], [1.4, 2.4, 3.8], 2, bars
    ).fit
    changed = certify_subwaves(
        parent, [ZigZagResult(outside, None), mid, coarse], [1.4, 2.4, 3.8], 2, bars
    ).fit
    return {
        "status": "PASS" if base == changed else "FAIL",
        "base_fit": base,
        "outside_only_fit": changed,
    }


def verify_report(report: Path, result_digest: str, snapshot_id: str) -> None:
    manifest = json.loads((report / "manifest.json").read_text())
    assert manifest["result_digest"] == result_digest
    assert manifest["input_snapshot_id"] == snapshot_id
    assert {"analysis.json", "report.md", "chart.html"} <= manifest["files"].keys()
    assert all(
        hashlib.sha256((report / name).read_bytes()).hexdigest() == sha
        for name, sha in manifest["files"].items()
    )
    record = json.loads((report / "analysis.json").read_text())
    assert (
        digest(
            {
                k: v
                for k, v in record.items()
                if k not in {"result_digest", "run_id", "analyzed_at", "mode"}
            }
        )
        == result_digest
    )


def frozen_checks(reports: Path) -> tuple[list[dict], dict, dict]:
    summary = []
    profiles = (
        ("validation_20261118_long_history_v3", ".cache/elliottwaver", True),
        ("validation_20261118_cn_index_v3", ".cache/elliottwaver", False),
        ("validation_20261118_ext5_v3", ".cache/live-smoke", False),
    )
    prefix_count = 0
    for name, root, long in profiles:
        evidence = json.loads(Path(f"docs/{name}.json").read_text())
        settings = load_settings()
        if long:
            settings = long_term_settings(settings)
            settings = settings.model_copy(
                update={"search": settings.search.model_copy(update={"max_subwave_depth": 1})}
            )
        assert settings.computation_hash == evidence["config_hash"], name
        store = SnapshotStore(Path(root))
        service = AnalysisService(settings)
        for case in evidence["cases"]:
            bundle = store.load(case["source_snapshot_id"])
            request = AnalysisRequest(
                symbol=case["symbol"],
                timeframe=case["timeframe"],
                as_of=bundle.as_of,
                mode="FROZEN_DATA_REPLAY",
                allow_gaps=long,
            )
            prepared = prepare_bundle(request, bundle)
            result = service.analyze_bundle(request, prepared)
            assert bundle.snapshot_id == case["source_snapshot_id"]
            assert prepared.snapshot_id == case["analyzed_snapshot_id"]
            baseline_report = Path(case["report"])
            verify_report(baseline_report, case["result_digest"], prepared.snapshot_id)
            assert result.status not in {"STALE_DATA", "INSUFFICIENT_DATA"}
            assert all(r.status != "FAIL" for s in result.scenarios for r in s.rules)
            assert all(
                w.pivot.confirmed_at <= result.as_of
                for scenario in result.scenarios
                for w in scenario.completed_waves
            )
            weight = sum(s.relative_weight for s in result.scenarios) + result.omitted_weight
            assert abs(weight - int(bool(result.scenarios))) < 1e-9
            report = publish_report(result, prepared, reports)
            verify_report(report, result.result_digest, prepared.snapshot_id)
            summary.append(
                {
                    "label": case["label"],
                    "suite": name,
                    "status": result.status,
                    "prior_status": case["status"],
                    "prior_result_digest": case["result_digest"],
                    "result_digest": result.result_digest,
                    "input_snapshot_id": prepared.snapshot_id,
                    "report": str(report),
                }
            )
            print(f"{case['label']}: {case['status']} -> {result.status}", flush=True)
            if long:
                for row in replay(
                    bundle, service, start=bundle.bars[-2].session_date, allow_gaps=True
                ):
                    direct = service.analyze_bundle(
                        request.model_copy(update={"as_of": row.as_of, "end": row.market_asof}),
                        bundle,
                    )
                    assert direct.result_digest == row.result_digest
                    prefix_count += 1
            if case["label"] == "pingan_long":
                with TemporaryDirectory(prefix="elliottwaver-engine-review-", dir="/tmp") as tmp:
                    config = Path(tmp) / "config.json"
                    configured = settings.model_dump(mode="json")
                    configured["cache"]["directory"] = root
                    config.write_text(json.dumps(configured))
                    runner = CliRunner()
                    args = [
                        "analyze",
                        case["symbol"],
                        "--offline",
                        "--snapshot",
                        bundle.snapshot_id,
                        "--config",
                        str(config),
                        "--allow-gaps",
                        "--output-dir",
                        tmp,
                        "--format",
                        "json",
                        "--json",
                    ]
                    invocation = runner.invoke(app, [*args, "--subwave-depth", "0"])
                    assert invocation.exit_code == 0, invocation.output
                    actual = json.loads(invocation.stdout)
                    no_subwave = settings.model_copy(
                        update={
                            "search": settings.search.model_copy(update={"max_subwave_depth": 0})
                        }
                    )
                    cli_depth = {
                        "status": "PASS"
                        if actual["config_hash"] == no_subwave.computation_hash
                        else "FAIL",
                        "actual_config": actual["config_hash"],
                        "expected_config": no_subwave.computation_hash,
                    }
                    output = Path(tmp) / "replay.jsonl"
                    last = bundle.bars[-1]
                    single = runner.invoke(
                        app,
                        [
                            *args,
                            "--as-of",
                            last.usable_at.isoformat(),
                            "--end",
                            str(last.session_date),
                        ],
                    )
                    assert single.exit_code == 0, single.output
                    single_record = json.loads(single.stdout)
                    assert single_record["config_hash"] == settings.computation_hash
                    replay_args = [
                        "replay",
                        bundle.snapshot_id,
                        "--start",
                        str(last.session_date),
                        "--config",
                        str(config),
                        "--output",
                        str(output),
                    ]
                    strict = runner.invoke(app, replay_args)
                    assert strict.exit_code == 0, strict.output
                    strict_status = json.loads(output.read_text())["status"]
                    allowed = runner.invoke(app, [*replay_args, "--allow-gaps"])
                    assert allowed.exit_code == 0, allowed.output
                    row = json.loads(output.read_text())
                    manifest = json.loads(Path(str(output) + ".manifest.json").read_text())
                    cli_gaps = {
                        "status": "PASS"
                        if (
                            row["result_digest"] == single_record["result_digest"]
                            and manifest.get("allow_gaps") is True
                            and strict_status == "INSUFFICIENT_DATA"
                        )
                        else "FAIL",
                        "analyze_with_allow_gaps": single_record["status"],
                        "cli_replay_with_allow_gaps": row["status"],
                        "cli_replay_strict": strict_status,
                        "same_result_digest": row["result_digest"]
                        == single_record["result_digest"],
                        "manifest_allow_gaps": manifest.get("allow_gaps"),
                    }
    assert prefix_count == 10
    cli_gaps["prefixes_checked_with_api"] = prefix_count
    return summary, cli_depth, cli_gaps


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path("reports/engine-fix-20261118.json"))
    parser.add_argument("--reports", type=Path, default=Path("reports/validation-engine-fix"))
    args = parser.parse_args()
    checks = {**geometry_checks(), "ENG-R04": subwave_check()}
    frozen, checks["ENG-R05"], checks["ENG-R06"] = frozen_checks(args.reports)
    source = digest(
        {
            str(p): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(Path("src").rglob("*.py"))
        }
    )
    data = {
        "source_sha256": source,
        "probe_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "checks": checks,
        "frozen_cases": frozen,
    }
    atomic_write(args.output, json.dumps(data, indent=2, ensure_ascii=False) + "\n")
    for name, result in checks.items():
        print(name, result["status"], {k: v for k, v in result.items() if k != "status"})
    print(f"{len(frozen)} frozen cases reproduced. Evidence: {args.output}")
    return int(any(result["status"] == "FAIL" for result in checks.values()))


if __name__ == "__main__":
    raise SystemExit(main())
