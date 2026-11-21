"""Measure latency of individual offline analyze and brief decisions."""

from __future__ import annotations

import argparse
import json
import math
import statistics
import time
from collections import deque
from pathlib import Path

from eliotbot.brief import build_brief
from eliotbot.config import load_settings
from eliotbot.data.snapshots import SnapshotStore
from eliotbot.evaluation.outcomes import period_dates
from eliotbot.schemas.market import AnalysisRequest
from eliotbot.service import AnalysisService, engine_rules_version
from eliotbot.util import atomic_write, canonical_json


def _cases(protocol: dict, limit: int):
    store = SnapshotStore(Path(protocol.get("cache_root", ".cache/eliotbot")))
    start, end = (protocol["grid"][key] for key in ("start", "end"))
    groups = deque()
    for sample in protocol["samples"]:
        if sample.get("source_timeframe", "1d") != "1d":
            continue
        bundle = store.load(sample["snapshot_id"])
        if bundle.provenance.provider in {"synthetic_fixture", "synthetic_demo"}:
            continue
        dates = set(
            period_dates(
                bundle.sessions, weekly=protocol["grid"]["cadence"] == "LAST_SESSION_OF_WEEK"
            )
        )
        selected = deque(
            bar
            for bar in reversed(bundle.bars[59:])
            if bar.session_date in dates and start <= str(bar.session_date) <= end
        )
        if selected:
            groups.append((bundle, selected))
    cases = []
    while groups and len(cases) < limit:
        bundle, selected = groups.popleft()
        bar = selected.popleft()
        request = AnalysisRequest(
            symbol=bundle.instrument.canonical_symbol,
            as_of=bar.usable_at,
            end=bar.session_date,
            timeframe=bundle.timeframe,
            mode="FROZEN_DATA_REPLAY",
            allow_gaps=protocol.get("allow_gaps", False),
        )
        cases.append((request, bundle))
        if selected:
            groups.append((bundle, selected))
    return cases


def _summary(values: list[float]) -> dict:
    ordered = sorted(values)
    return {
        "p50_ms": statistics.median(ordered),
        "p95_ms": ordered[math.ceil(0.95 * len(ordered)) - 1],
    }


def benchmark(protocol_path: Path, config_path: Path, *, limit: int, rounds: int) -> dict:
    if limit < 1 or rounds < 1:
        raise ValueError("limit and rounds must be positive")
    protocol = json.loads(protocol_path.read_text())
    settings = load_settings(config_path)
    cases = _cases(protocol, limit)
    if not cases:
        raise ValueError("No eligible frozen daily decisions in the benchmark protocol")
    service = AnalysisService(settings)
    for request, bundle in cases[:3]:
        service.analyze_bundle(request, bundle)
        build_brief(request, bundle, settings)
    analyze_ms: list[float] = []
    brief_ms: list[float] = []
    analyzable = set()
    truncated = 0
    verified = 0
    brief_requests = 0
    for _ in range(rounds):
        for request, bundle in cases:
            started = time.perf_counter()
            result = service.analyze_bundle(request, bundle)
            analyze_ms.append((time.perf_counter() - started) * 1000)
            started = time.perf_counter()
            run = build_brief(request, bundle, settings)
            brief_ms.append((time.perf_counter() - started) * 1000)
            if result.status not in {"STALE_DATA", "INSUFFICIENT_DATA"}:
                analyzable.add((request.symbol, request.as_of))
            comparison = run.brief.window_comparison
            truncated += any(
                analysis is not None and analysis.diagnostics.search_truncated
                for analysis in (run.result, run.weekly_result, run.alternate_result)
            ) or bool(comparison and comparison.search_limits)
            verified += bool(run.brief.cards and run.brief.cards[0].subwave_status == "VERIFIED")
            brief_requests += 1
    return {
        "schema_version": "1.1",
        "protocol": str(protocol_path),
        "config": str(config_path),
        "engine_profile": settings.analysis.engine_profile,
        "engine_version": engine_rules_version(settings),
        "config_hash": settings.computation_hash,
        "decisions": len(cases),
        "analyzable_decisions": len(analyzable),
        "rounds": rounds,
        "truncated_rate": truncated / brief_requests,
        "verified_primary_rate": verified / brief_requests,
        "analyze": _summary(analyze_ms),
        "brief": _summary(brief_ms),
        "p95_ms_per_decision": _summary(brief_ms)["p95_ms"],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--protocol", type=Path, required=True)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--baseline-config", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--limit", type=int, default=30)
    parser.add_argument("--rounds", type=int, default=3)
    args = parser.parse_args()
    result = benchmark(args.protocol, args.config, limit=args.limit, rounds=args.rounds)
    if args.baseline_config is not None:
        baseline = benchmark(
            args.protocol, args.baseline_config, limit=args.limit, rounds=args.rounds
        )
        ratio = result["brief"]["p95_ms"] / baseline["brief"]["p95_ms"]
        result["baseline"] = baseline
        result["brief_p95_ratio"] = ratio
        sufficient = min(result["analyzable_decisions"], baseline["analyzable_decisions"]) >= 30
        result["default_gate_2x"] = (
            "PASS"
            if sufficient and args.rounds >= 3 and ratio <= 2
            else "FAIL"
            if sufficient and args.rounds >= 3
            else "INSUFFICIENT_SAMPLE"
        )
    atomic_write(args.output, canonical_json(result) + "\n")
    print(canonical_json(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
