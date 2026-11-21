"""Fixed-sample candidate recall and acceptance comparison.

The small exhaustive sample measures search recall against an independent
reference. The large acceptance sample reports candidate coverage, qualified
candidate counts, truncation, primary-switch stability and p95 timing.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from dataclasses import replace
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tests"))

from conftest import make_pivots  # noqa: E402
from test_exhaustive_search import _reference  # noqa: E402

from eliotbot.config import SearchConfig  # noqa: E402
from eliotbot.engine.matcher import search  # noqa: E402
from eliotbot.engine.zigzag import ZigZagResult  # noqa: E402
from eliotbot.util import canonical_json  # noqa: E402

_PIVOTS = [100, 120, 110, 150, 132, 160, 145, 175, 158, 190, 172, 205, 188, 220]


def _exhaustive_recall(config: SearchConfig, *, advanced: bool = False) -> dict:
    bars, pivots = make_pivots([*_PIVOTS, 204], developing=True)
    points, tip = pivots[:-1], pivots[-1]
    anchor = bars[points[0].index].session_date
    reference = _reference(bars, points, tip, anchor, 13)
    developing = {key for key, candidate in reference.items() if candidate.developing}
    if not developing:
        raise ValueError("The exhaustive fixture needs legal developing counts")
    found, budget = search(
        bars,
        [ZigZagResult(points, tip)],
        [anchor],
        config,
        tick_size=0.01,
        advanced=advanced,
    )
    # The common denominator is the classic geometry. A newly introduced
    # subtype with the same geometry must count only once here.
    found_keys = {replace(candidate, variant=None).key for candidate in found}
    matched = found_keys & reference.keys()
    return {
        "config": {
            "beam_width": config.beam_width,
            "max_expansions": config.max_expansions,
            "max_bridge_intervals": config.max_bridge_intervals,
        },
        "reference_candidates": len(reference),
        "found_candidates": len(found_keys),
        "matched_candidates": len(matched),
        "reference_developing_candidates": len(developing),
        "matched_developing_candidates": len(found_keys & developing),
        "recall": len(matched) / len(reference) if reference else None,
        "search_truncated": budget.truncated,
    }


def _candidate_stats(directory: Path) -> tuple[float | None, float | None, int]:
    rows = [
        json.loads(line)
        for line in (directory / "decisions.jsonl").read_text().splitlines()
        if line.strip()
    ]
    counts = [len(row.get("candidate_keys", [])) for row in rows]
    if not counts:
        return None, None, 0
    ordered = sorted(counts)
    middle = len(ordered) // 2
    median = ordered[middle] if len(ordered) % 2 else (ordered[middle - 1] + ordered[middle]) / 2
    return sum(counts) / len(counts), median, len(counts)


def _run_row(run: dict, benchmark: dict | None, directory: Path) -> dict:
    summary = json.loads((directory / "summary.json").read_text())
    structure = summary["by_asset_type"]
    decisions = summary["decisions"]
    analyzable = summary["analyzable"]
    signals = sum(
        item["analyzable"] - item["structure"]["no_signal"] for item in structure.values()
    )
    candidate_mean, candidate_median, candidate_decisions = _candidate_stats(directory)
    comparable = sum(item["structure"]["comparable_primary_pairs"] for item in structure.values())
    switches = sum(item["structure"]["primary_switches"] for item in structure.values())
    return {
        "engine_profile": run["engine_profile"],
        "decisions": decisions,
        "analyzable": analyzable,
        "signals": signals,
        "candidate_coverage": signals / analyzable if analyzable else None,
        "candidate_keys_mean": candidate_mean,
        "candidate_keys_median": candidate_median,
        "candidate_decisions": candidate_decisions,
        "search_truncated": sum(
            item["structure"]["search_truncated"] for item in structure.values()
        ),
        "primary_switch_rate": switches / comparable if comparable else None,
        "p95_ms": benchmark.get("p95_ms_per_decision") if benchmark else None,
    }


def _markdown(report: dict) -> str:
    exhaustive = report["exhaustive_14_pivot"]
    lines = [
        "# 候选召回与固定样本对照",
        "",
        "## 14 拐点穷举参照",
        "",
        "| 配置 | 参照候选 | 搜索找到 | 交集 | 召回 | 进行中命中 / 参照 | 截断 |",
        "| --- | ---: | ---: | ---: | ---: | ---: | --- |",
    ]
    for name in ("classic", "advanced"):
        item = exhaustive[name]
        recall = "n/a" if item["recall"] is None else f"{item['recall']:.1%}"
        lines.append(
            f"| {name} | {item['reference_candidates']} | {item['found_candidates']} | "
            f"{item['matched_candidates']} | {recall} | "
            f"{item['matched_developing_candidates']} / {item['reference_developing_candidates']} | "
            f"{item['search_truncated']} |"
        )
    lines += [
        "",
        "## 固定验收样本",
        "",
        "| 运行 | 引擎 | 决策 | 可分析 | 候选覆盖 | 平均合格候选 | 截断 | 主选切换率 | p95 |",
        "| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for name, row in report["fixed_sample"].items():
        coverage = (
            "n/a" if row["candidate_coverage"] is None else f"{row['candidate_coverage']:.1%}"
        )
        mean = "n/a" if row["candidate_keys_mean"] is None else f"{row['candidate_keys_mean']:.1f}"
        switch = (
            "n/a" if row["primary_switch_rate"] is None else f"{row['primary_switch_rate']:.1%}"
        )
        p95 = "n/a" if row["p95_ms"] is None else f"{row['p95_ms']:.1f} ms"
        lines.append(
            f"| {name} | {row['engine_profile']} | {row['decisions']} | {row['analyzable']} | "
            f"{coverage} | {mean} | {row['search_truncated']} | {switch} | {p95} |"
        )
    lines += [
        "",
        "候选覆盖 = 有至少一个合格候选的决策 / 可分析决策；平均合格候选取自 `candidate_keys`。",
        "14 拐点召回以独立穷举枚举器为参照；经典配置使用默认 beam/bridge，高级配置使用固定大预算。",
        "",
    ]
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--comparison",
        default="reports/wave-engine/acceptance/comparison.json",
    )
    parser.add_argument(
        "--benchmark",
        default="reports/wave-engine/benchmark/comparison.json",
    )
    parser.add_argument(
        "--acceptance-root",
        default="reports/wave-engine/acceptance",
    )
    parser.add_argument("--output", default="reports/wave-engine/recall/comparison.json")
    parser.add_argument("--markdown", default="reports/wave-engine/recall/report.md")
    args = parser.parse_args()

    comparison = json.loads(Path(args.comparison).read_text())
    benchmark = json.loads(Path(args.benchmark).read_text())
    acceptance_root = Path(args.acceptance_root)
    directories = {
        "cn-classic": acceptance_root / "classic",
        "cn-advanced": acceptance_root / "advanced",
        "us-classic": acceptance_root / "us-classic",
        "us-advanced": acceptance_root / "us-advanced",
    }
    classic_config = SearchConfig()
    advanced_config = SearchConfig(
        beam_width=1024,
        max_expansions=1000000,
        max_bridge_intervals=21,
    )
    report = {
        "schema_version": "1.0",
        "exhaustive_14_pivot": {
            "classic": _exhaustive_recall(classic_config),
            "advanced": _exhaustive_recall(advanced_config, advanced=True),
        },
        "fixed_sample": {
            "cn-classic": _run_row(
                comparison["runs"]["cn-classic"],
                benchmark["classic"],
                directories["cn-classic"],
            ),
            "cn-advanced": _run_row(
                comparison["runs"]["cn-advanced"],
                benchmark["advanced"],
                directories["cn-advanced"],
            ),
            "us-classic": _run_row(
                comparison["runs"]["us-classic"], None, directories["us-classic"]
            ),
            "us-advanced": _run_row(
                comparison["runs"]["us-advanced"], None, directories["us-advanced"]
            ),
        },
    }
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(canonical_json(report) + "\n")
    Path(args.markdown).write_text(_markdown(report) + "\n")
    manifest = {
        "schema_version": "1.0",
        "files": {
            "comparison.json": hashlib.sha256(output.read_bytes()).hexdigest(),
            "report.md": hashlib.sha256(Path(args.markdown).read_bytes()).hexdigest(),
        },
    }
    (output.parent / "manifest.json").write_text(canonical_json(manifest) + "\n")
    print(_markdown(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
