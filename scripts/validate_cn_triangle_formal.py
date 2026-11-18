"""Compare the formal AnalysisService entry with the experiment harness.

The check is deliberately small: a handful of frozen decisions that cover a
signal, a no-signal decision, a stock with gaps, and a weekly decision.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
from datetime import date
from pathlib import Path

from experiment_cn_triangle_20261118 import (
    DAILY_HORIZON,
    DAILY_SAMPLES,
    WEEKLY_HORIZON,
    _analyze_decision,
    _load_bundle,
    _settings,
)

from eliotbot import ENGINE_VERSION
from eliotbot.data.periods import as_weekly
from eliotbot.schemas.market import AnalysisRequest
from eliotbot.service import AnalysisService
from eliotbot.util import atomic_write, digest

CASES = [
    ("000001.SH", "1d", date(2023, 8, 18)),
    ("600519.SH", "1d", date(2025, 10, 17)),
    ("300750.SZ", "1d", date(2023, 6, 16)),
    ("000001.SH", "1d", date(2024, 6, 7)),
    ("000001.SH", "1w", date(2025, 5, 30)),
    ("600519.SH", "1w", date(2024, 7, 19)),
]

# Reconstructed from raw OHLC and the factors for each decision's price basis.
PRICE_CASES = {
    ("600519.SH", "1d", date(2025, 10, 17)): (
        1478.589095977158,
        1431.410904022842,
        "TARGET_FIRST",
        "2025-10-24",
    ),
    ("600519.SH", "1w", date(2024, 7, 19)): (
        1461.2693310548293,
        1589.9706689451705,
        "TARGET_FIRST",
        "2024-07-26",
    ),
}


def _sample(symbol: str) -> dict:
    return next(sample for sample in DAILY_SAMPLES if sample["symbol"] == symbol)


def _first_triangle(result) -> str | None:
    for scenario in result.scenarios:
        if (
            scenario.pattern_type == "TRIANGLE"
            and scenario.stage == "COMPLETE"
            and scenario.subwave_status == "VERIFIED"
            and any(
                rule.rule_id == "TRIANGLE_SHAPE"
                and rule.status == "PASS"
                and rule.observed is not None
                and rule.boundary is not None
                and rule.observed < rule.boundary
                for rule in scenario.rules
            )
        ):
            return scenario.scenario_id
    return None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("reports/cn-triangle-experiment/formal-validation.json"),
    )
    args = parser.parse_args()
    rows = []
    for symbol, timeframe, day in CASES:
        sample = _sample(symbol)
        bundle = _load_bundle(sample["snapshot_id"])
        if timeframe == "1w":
            bundle = as_weekly(bundle)
        settings = _settings(timeframe)
        decision = next(bar for bar in bundle.bars if bar.session_date >= day)
        request = AnalysisRequest(
            symbol=symbol,
            as_of=decision.usable_at,
            end=decision.session_date,
            mode="FROZEN_DATA_REPLAY",
            timeframe=timeframe,
            allow_gaps=timeframe == "1w" and sample["group"] != "index",
        )
        try:
            formal = AnalysisService(settings).analyze_bundle(request, bundle)
            formal_error = None
        except Exception as exc:  # noqa: BLE001
            formal = None
            formal_error = f"{type(exc).__name__}: {exc}"
        experiment = _analyze_decision(
            bundle,
            decision,
            timeframe,
            settings,
            allow_gaps=request.allow_gaps,
            horizon=DAILY_HORIZON if timeframe == "1d" else WEEKLY_HORIZON,
        )
        formal_analyzable = formal is not None and formal.status not in {
            "INSUFFICIENT_DATA",
            "STALE_DATA",
        }
        experiment_analyzable = experiment.get("status") == "OK"
        formal_key = _first_triangle(formal) if formal is not None else None
        signal = experiment.get("classic") or {}
        experiment_key = signal.get("candidate_key")
        checks = {
            "analyzable_match": formal_analyzable == experiment_analyzable,
            "candidate_count_match": (
                formal is not None
                and formal.diagnostics.candidates_found == experiment.get("candidates")
            ),
            "triangle_key_match": formal_key == experiment_key,
        }
        price_case = PRICE_CASES.get((symbol, timeframe, day))
        event = None
        if price_case is not None:
            target, adverse, label, event_date = price_case
            event = {
                name: signal.get(name) for name in ("target", "adverse", "event", "event_date")
            }
            checks["price_basis_match"] = (
                math.isclose(event.get("target") or 0.0, target, rel_tol=0, abs_tol=1e-6)
                and math.isclose(event.get("adverse") or 0.0, adverse, rel_tol=0, abs_tol=1e-6)
                and event["event"] == label
                and event["event_date"] == event_date
            )
        rows.append(
            {
                "symbol": symbol,
                "timeframe": timeframe,
                "decision_date": decision.session_date.isoformat(),
                "formal_status": formal.status if formal is not None else None,
                "formal_error": formal_error,
                "experiment_status": experiment.get("status"),
                "formal_candidates": (
                    formal.diagnostics.candidates_found if formal is not None else None
                ),
                "experiment_candidates": experiment.get("candidates"),
                "formal_triangle_key": formal_key,
                "experiment_triangle_key": experiment_key,
                "event": event,
                "checks": checks,
                "passed": all(checks.values()),
            }
        )
    source = digest(
        {
            str(path): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in sorted(Path("src").rglob("*.py"))
        }
    )
    record = {
        "engine_version": ENGINE_VERSION,
        "source_sha256": source,
        "rows": rows,
        "passed": sum(row["passed"] for row in rows),
        "total": len(rows),
    }
    atomic_write(args.output, json.dumps(record, indent=2, ensure_ascii=False) + "\n")
    for row in rows:
        print(
            row["symbol"],
            row["timeframe"],
            row["decision_date"],
            "PASS" if row["passed"] else "FAIL",
            row["checks"],
        )
    return int(any(not row["passed"] for row in rows))


if __name__ == "__main__":
    raise SystemExit(main())
