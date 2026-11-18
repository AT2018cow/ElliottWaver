"""Reproduce acceptance evidence from explicit local snapshot IDs, without networking."""

import argparse
import hashlib
import json
import time
from datetime import UTC, datetime
from pathlib import Path

from eliotbot import ENGINE_VERSION
from eliotbot.config import large_degree_settings, load_settings, long_term_settings
from eliotbot.data.snapshots import SnapshotStore
from eliotbot.evaluation.replay import replay
from eliotbot.schemas.market import AnalysisRequest
from eliotbot.service import AnalysisService, prepare_bundle
from eliotbot.timeframes import normalize_timeframe
from eliotbot.util import atomic_write, canonical_json, digest
from eliotbot.visualization.chart_builder import svg_chart
from eliotbot.visualization.report_formatter import publish_report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--case", action="append", required=True, help="label:snapshot-id:timeframe"
    )
    parser.add_argument("--cache-root", type=Path, default=Path(".cache/live-smoke"))
    parser.add_argument("--config", type=Path)
    parser.add_argument("--long-term", action="store_true")
    parser.add_argument("--large-degree", action="store_true")
    parser.add_argument("--subwave-depth", type=int, choices=(0, 1, 2))
    parser.add_argument("--allow-gaps", action="store_true")
    parser.add_argument("--output", type=Path, default=Path("reports/snapshot-validation.json"))
    parser.add_argument("--reports", type=Path, default=Path("reports/validation"))
    args = parser.parse_args()
    settings = load_settings(args.config)
    if args.long_term:
        settings = long_term_settings(settings)
    elif args.large_degree:
        settings = large_degree_settings(settings)
    if args.subwave_depth is not None:
        settings = settings.model_copy(
            update={
                "search": settings.search.model_copy(
                    update={"max_subwave_depth": args.subwave_depth}
                )
            }
        )
    service = AnalysisService(settings)
    store = SnapshotStore(args.cache_root)
    cases = []
    for value in args.case:
        label, source_id, period = value.split(":")
        bundle = store.load(source_id)
        period = normalize_timeframe(period)
        request = AnalysisRequest(
            symbol=bundle.instrument.canonical_symbol,
            timeframe=period,
            as_of=bundle.as_of,
            mode="FROZEN_DATA_REPLAY",
            allow_gaps=args.allow_gaps,
        )
        prepared = prepare_bundle(request, bundle)
        store.save(prepared)
        start = time.perf_counter()
        result = service.analyze_bundle(request, prepared)
        elapsed = time.perf_counter() - start
        if result.status in {"INSUFFICIENT_DATA", "STALE_DATA"}:
            raise RuntimeError(f"{label}: {result.status}")
        assert all(r.status != "FAIL" for s in result.scenarios for r in s.rules)
        assert (
            abs(
                sum(s.relative_weight for s in result.scenarios)
                + result.omitted_weight
                - (1 if result.scenarios else 0)
            )
            < 1e-10
        )
        assert all(
            w.pivot.confirmed_at <= result.as_of
            for s in result.scenarios
            for w in s.completed_waves
        )
        for historical in replay(
            prepared,
            service,
            start=prepared.bars[-3].session_date,
            step=max(1, len(prepared.bars) // 100),
            allow_gaps=args.allow_gaps,
        ):
            point = AnalysisRequest(
                symbol=prepared.instrument.canonical_symbol,
                timeframe=period,
                as_of=historical.as_of,
                end=historical.market_asof,
                allow_gaps=args.allow_gaps,
            )
            assert service.analyze_bundle(point, prepared).result_digest == historical.result_digest
        report = publish_report(result, prepared, args.reports)
        if not cases:
            atomic_write(args.reports / "preview.png", svg_chart(result, prepared, png=True))
        item = {
            "label": label,
            "symbol": prepared.instrument.canonical_symbol,
            "timeframe": period,
            "source_snapshot_id": source_id,
            "analyzed_snapshot_id": prepared.snapshot_id,
            "first_bar": prepared.bars[0].start_at,
            "last_bar": prepared.bars[-1].end_at,
            "bars": len(prepared.bars),
            "provider": prepared.provenance.provider,
            "feed": prepared.provenance.feed,
            "parser": prepared.provenance.parser_version,
            "status": result.status,
            "candidates": len(result.scenarios),
            "result_digest": result.result_digest,
            "compute_seconds": round(elapsed, 6),
            "report": str(report),
            "prefix_parity": "PASS",
            "hard_rules": "PASS",
            "warnings": result.warnings,
        }
        cases.append(item)
        print(canonical_json(item), flush=True)
    root = Path(__file__).resolve().parents[1]
    source_hash = digest(
        {
            str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted((root / "src").rglob("*.py"))
        }
    )
    evidence = {
        "schema_version": "1.0",
        "validated_at": datetime.now(UTC),
        "engine_version": ENGINE_VERSION,
        "source_sha256": source_hash,
        "config_hash": settings.computation_hash,
        "test_status": "NOT_RUN_BY_THIS_SCRIPT",
        "scope": "Frozen real Sina snapshots; not PIT, probability calibration, or profitability evidence",
        "alpaca_live": "NOT_RUN_BY_THIS_SCRIPT",
        "cases": cases,
    }
    atomic_write(args.output, canonical_json(evidence) + "\n")
    print(json.dumps({"evidence": str(args.output), "cases": len(cases)}))


if __name__ == "__main__":
    main()
