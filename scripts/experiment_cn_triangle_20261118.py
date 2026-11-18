"""Run the pre-registered A-share triangle child-rule comparison.

Classic group A: at least four legs are verified zigzag-family structures and
at most one leg is complex. Loose group B: every leg passes its own allowed
child template, with no group-level zigzag quota.

The script uses frozen snapshots, the same search budget for both groups, and
symmetric ATR boundaries for the pre-registered outcome labels.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import math
import random
import subprocess
from collections import Counter
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from eliotbot import ENGINE_VERSION
from eliotbot.config import load_settings, long_term_settings
from eliotbot.data.adjustment import adjust_to_basis
from eliotbot.data.periods import as_weekly
from eliotbot.data.quality import missing_intervals
from eliotbot.data.resample import weekly_bars
from eliotbot.data.snapshots import SnapshotStore
from eliotbot.engine.indicators import calculate
from eliotbot.engine.matcher import Candidate, search
from eliotbot.engine.scorer import score
from eliotbot.engine.span_selector import select_anchors
from eliotbot.engine.subwaves import (
    certify_subwaves,
    classify_triangle_legs,
    is_completed_contracting_triangle,
)
from eliotbot.engine.zigzag import extract
from eliotbot.schemas.market import AnalysisRequest, MarketDataBundle
from eliotbot.service import prepare_bundle
from eliotbot.util import atomic_write, digest

DAILY_START = date(2023, 1, 2)
DAILY_END = date(2025, 12, 31)
WEEKLY_START = date(2023, 1, 2)
WEEKLY_END = date(2025, 12, 31)
DAILY_HORIZON = 20
WEEKLY_HORIZON = 8
SEED = 20261118
BOOTSTRAP = 1000
CLEAR_EVENTS = {"TARGET_FIRST", "ADVERSE_FIRST", "EXPIRED"}

# Fixed pilot sample. The protocol asks for a broader board sample; this run
# uses 2-3 names per board and four indices because only those frozen snapshots
# were available without changing the data source.
DAILY_SAMPLES: list[dict[str, Any]] = [
    {
        "symbol": "000001.SH",
        "group": "index",
        "snapshot_id": "195886c5027cfd4642100df4f8e9c9e53103ff1ca657be5e2f70e7589f350331",
    },
    {
        "symbol": "000300.SH",
        "group": "index",
        "snapshot_id": "9d87a58582943ad7145120c032ea8cc1505e9521084527b40cc4b18d30baebcf",
    },
    {
        "symbol": "399001.SZ",
        "group": "index",
        "snapshot_id": "22b98524009f903ce63c75675686bda77aca608f16923ccf5ab65aa6ac255bca",
    },
    {
        "symbol": "399006.SZ",
        "group": "index",
        "snapshot_id": "80f5b4d5c5bd7614d7c8a4a0dab0d4303a23d54ab8a377728a3f22c81fa7f488",
    },
    {
        "symbol": "600519.SH",
        "group": "sh_main",
        "snapshot_id": "8378a32d2ce12a8419cb853ad4c6558aa4644f11aae7302c3319d14530d87f8d",
    },
    {
        "symbol": "601318.SH",
        "group": "sh_main",
        "snapshot_id": "09c185c1cfe2c1701042aa401b32a51687e70415df2e4cf460a416e2d736fee7",
    },
    {
        "symbol": "600036.SH",
        "group": "sh_main",
        "snapshot_id": "265be915fd0890c942cdcf3acf3574a6741948c7c2a06d83df5558e293ff4b5b",
    },
    {
        "symbol": "000001.SZ",
        "group": "sz_main",
        "snapshot_id": "274c1b50afc753de8a11e564d21e0c96294f838306f8d721c3789413cec2516f",
    },
    {
        "symbol": "000333.SZ",
        "group": "sz_main",
        "snapshot_id": "6ccdc7db0d370a6f00b4e830a6209554791f6d203f0108378e79bba9a298d813",
    },
    {
        "symbol": "000651.SZ",
        "group": "sz_main",
        "snapshot_id": "33e22a5ee42bb9be72ec90f0f10bb95f66fa859be2d65163af1c1a1a462cdae2",
    },
    {
        "symbol": "688981.SH",
        "group": "star",
        "snapshot_id": "3973fd867fdc454cb31a252688c75a7cb82d458be93894fb289d9db4b932125f",
    },
    {
        "symbol": "688111.SH",
        "group": "star",
        "snapshot_id": "21aed03b58f9d2c4dcc4a47d4240dd01ec2401c52899c3024fc79cc8093c98c2",
    },
    {
        "symbol": "300750.SZ",
        "group": "chinext",
        "snapshot_id": "3cbd49e6f18ee7be621f60fb506f8d869e4a5151c5f4f4d2d6ef407639be5f50",
    },
    {
        "symbol": "300059.SZ",
        "group": "chinext",
        "snapshot_id": "1d06a261e97b281ce8cbe2665c32dfe99e7e660e256f0abb0eac1efaa7b4c090",
    },
    {
        "symbol": "300124.SZ",
        "group": "chinext",
        "snapshot_id": "be2c9b070b2b0b622532bd0cdf2f0e4ab1cb552829765382071bfffa0800c3c3",
    },
]

WEEKLY_SYMBOLS = {
    "000001.SH",
    "000300.SH",
    "399001.SZ",
    "399006.SZ",
    "600519.SH",
    "000001.SZ",
    "688981.SH",
    "300059.SZ",
}


def _snapshot_root() -> Path:
    return Path(".cache/eliotbot")


def _source_hash() -> str:
    root = Path(__file__).resolve().parents[1]
    return digest(
        {
            str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in sorted((root / "src").rglob("*.py"))
        }
    )


def _git_state() -> tuple[str, bool]:
    head = subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip()
    dirty = bool(subprocess.check_output(["git", "status", "--porcelain"], text=True).strip())
    return head, dirty


def _settings(timeframe: str):
    settings = load_settings()
    if timeframe == "1w":
        settings = long_term_settings(settings)
    search = settings.search.model_copy(update={"max_subwave_depth": 1})
    return settings.model_copy(update={"search": search})


def _load_bundle(snapshot_id: str) -> MarketDataBundle:
    return SnapshotStore(_snapshot_root()).load(snapshot_id)


def _decision_bars(bundle: MarketDataBundle, timeframe: str, start: date, end: date) -> list:
    eligible = [bar for bar in bundle.bars if start <= bar.session_date <= end]
    if timeframe == "1w":
        return eligible
    by_week: dict[tuple[int, int], Any] = {}
    for bar in eligible:
        by_week[bar.session_date.isocalendar()[:2]] = bar
    return sorted(by_week.values(), key=lambda bar: bar.session_date)


def _policy(certification) -> dict[str, Any]:
    policy = classify_triangle_legs(certification.evidence)
    return {
        **policy,
        "wxy_or_missing": any(item.status == "UNAVAILABLE" for item in certification.evidence),
        "loose": policy["all_verified"],
    }


def _nested_links(candidates: list[Candidate], timeframe: str, allow_nested: bool):
    import eliotbot.engine.parents as parents

    original = parents.allowed_child_templates

    def allowed(parent, leg_index):
        values = original(parent, leg_index)
        if parent == "TRIANGLE" and leg_index == 4:
            if allow_nested:
                return tuple(dict.fromkeys((*values, "TRIANGLE")))
            return tuple(value for value in values if value != "TRIANGLE")
        return values

    parents.allowed_child_templates = allowed
    try:
        return parents.attach_parent_context(candidates, timeframe)
    finally:
        parents.allowed_child_templates = original


def _nested_pairs(candidates: list[Candidate]) -> list[tuple[Candidate, Candidate]]:
    pairs = []
    for child in candidates:
        if child.template.name != "TRIANGLE" or child.stage != "COMPLETE":
            continue
        for parent in candidates:
            if parent.template.name != "TRIANGLE" or parent.key == child.key:
                continue
            if len(parent.points) < 6:
                continue
            left, right = parent.points[4], parent.points[5]
            if child.points[0].index != left.index or child.points[-1].index != right.index:
                continue
            direction = 1 if right.price > left.price else -1
            if child.direction == direction:
                pairs.append((parent, child))
    return pairs


def _future_bars(
    bundle: MarketDataBundle, decision_bar, timeframe: str, horizon: int
) -> tuple[tuple, str | None]:
    decision_date = decision_bar.session_date
    if timeframe == "1d":
        index = next(i for i, bar in enumerate(bundle.bars) if bar.session_date == decision_date)
        expected = [
            day for day in bundle.sessions if decision_date < day <= bundle.bars[-1].session_date
        ][:horizon]
        if len(expected) < horizon:
            return tuple(bundle.bars[index + 1 : index + 1 + horizon]), "RIGHT_CENSORED"
        if bundle.provenance.adjustment == "basis_date_qfq":
            future = adjust_to_basis(
                bundle.raw_bars[index + 1 : index + 1 + horizon],
                bundle.factors,
                basis_date=decision_date,
            )
        else:
            future = bundle.bars[index + 1 : index + 1 + horizon]
        if any(day not in {bar.session_date for bar in future} for day in expected):
            return tuple(future), "INCOMPLETE_DATA"
        return tuple(future), None

    if bundle.provenance.adjustment == "basis_date_qfq":
        raw = [bar for bar in bundle.raw_daily_context if bar.session_date > decision_date]
        adjusted = adjust_to_basis(raw, bundle.factors, basis_date=decision_date) if raw else ()
        weekly = weekly_bars(adjusted, bundle.sessions)
    else:
        weekly = [bar for bar in bundle.bars if bar.session_date > decision_date]
    future = tuple(weekly[:horizon])
    return future, None if len(future) == horizon else "RIGHT_CENSORED"


def _event_label(
    candidate: Candidate, decision_bar, future: tuple, atr: float, horizon: int
) -> tuple[str, date | None]:
    if not math.isfinite(atr) or atr <= 0:
        return "UNDEFINED_EVENT", None
    d_point, e_point = candidate.points[-2], candidate.points[-1]
    predicted = -1 if e_point.price > d_point.price else 1
    center = decision_bar.close
    target = center + predicted * atr
    adverse = center - predicted * atr
    for bar in future[:horizon]:
        if predicted > 0:
            hit_target = bar.high >= target
            hit_adverse = bar.low <= adverse
        else:
            hit_target = bar.low <= target
            hit_adverse = bar.high >= adverse
        if hit_target and hit_adverse:
            return "AMBIGUOUS_SAME_BAR", bar.session_date
        if hit_target:
            return "TARGET_FIRST", bar.session_date
        if hit_adverse:
            return "ADVERSE_FIRST", bar.session_date
    return "EXPIRED", future[horizon - 1].session_date


def _signal_payload(
    candidate: Candidate,
    certification,
    breakdown,
    decision_bar,
    atr: float,
    symbol: str,
    timeframe: str,
    raw_endpoint_ids: tuple[str, ...],
) -> dict[str, Any]:
    d_point, e_point = candidate.points[-2], candidate.points[-1]
    direction = -1 if e_point.price > d_point.price else 1
    target = decision_bar.close + direction * atr
    adverse = decision_bar.close - direction * atr
    key = digest(
        [
            symbol,
            timeframe,
            direction,
            raw_endpoint_ids,
        ]
    )[:24]
    return {
        "key": key,
        "candidate_key": candidate.key,
        "pattern": candidate.template.name,
        "direction": "UP" if direction > 0 else "DOWN",
        "d_date": d_point.extreme_at.date().isoformat(),
        "e_date": e_point.extreme_at.date().isoformat(),
        "d_price": d_point.price,
        "e_price": e_point.price,
        "legs": [
            {
                "leg": item.leg,
                "status": item.status,
                "pattern": item.pattern,
                "point_count": item.point_count,
                "scale": item.scale,
                "depth": item.depth,
            }
            for item in certification.evidence
        ],
        "policy": _policy(certification),
        "quality": breakdown.quality_score,
        "coverage": breakdown.coverage,
        "target": target,
        "adverse": adverse,
    }


def _analyze_decision(
    bundle: MarketDataBundle,
    decision_bar,
    timeframe: str,
    settings,
    allow_gaps: bool,
    horizon: int,
) -> dict[str, Any]:
    request = AnalysisRequest(
        symbol=bundle.instrument.canonical_symbol,
        as_of=decision_bar.usable_at,
        end=decision_bar.session_date,
        mode="FROZEN_DATA_REPLAY",
        timeframe=timeframe,
        allow_gaps=allow_gaps,
    )
    try:
        prepared = prepare_bundle(request, bundle)
    except Exception as exc:  # noqa: BLE001
        return {"status": "PREPARE_ERROR", "error": str(exc)}
    bars = prepared.bars
    if len(bars) < settings.analysis.warmup_bars + settings.analysis.min_anchor_bars:
        return {"status": "INSUFFICIENT_HISTORY", "bars": len(bars)}
    try:
        anchors, _ = select_anchors(prepared, request, settings.analysis)
    except Exception as exc:  # noqa: BLE001
        return {"status": "ANCHOR_ERROR", "error": str(exc)}
    gaps = missing_intervals(prepared, min(anchors))
    if gaps and not allow_gaps:
        return {"status": "INSUFFICIENT_DATA", "gaps": len(gaps)}
    indicators = calculate(bars)
    zigzags = [
        extract(bars, indicators.atr, scale, bundle.instrument.tick_size)
        for scale in settings.analysis.scales
    ]
    try:
        candidates, budget = search(bars, zigzags, anchors, settings.search)
    except Exception as exc:  # noqa: BLE001
        return {"status": "SEARCH_ERROR", "error": str(exc)}

    def endpoint_ids(candidate: Candidate) -> tuple[str, ...]:
        # Adjusted bar IDs change with the QFQ basis. Anchor identity to raw OHLC.
        return tuple(prepared.raw_bars[point.index].bar_id for point in candidate.points)

    pairs = _nested_pairs(candidates)
    current_links = _nested_links(candidates, timeframe, False) if pairs else {}
    nested_links = _nested_links(candidates, timeframe, True) if pairs else {}
    nested_rows = []
    for parent, child in pairs:
        nested = nested_links[child.key]
        if nested.parent_key != parent.key:
            continue
        left, right = parent.points[4], parent.points[5]
        endpoint_match = (
            child.points[0].index == left.index
            and child.points[-1].index == right.index
            and (child.points[0].price, child.points[0].kind) == (left.price, left.kind)
            and (child.points[-1].price, child.points[-1].kind) == (right.price, right.kind)
        )
        child_geometry_pass = all(rule.status == "PASS" for rule in child.rules)
        current = current_links[child.key]
        nested_rows.append(
            {
                "symbol": bundle.instrument.canonical_symbol,
                "timeframe": timeframe,
                "decision_date": decision_bar.session_date.isoformat(),
                "parent_key": parent.key,
                "child_key": child.key,
                "parent_endpoint_ids": endpoint_ids(parent),
                "child_endpoint_ids": endpoint_ids(child),
                "selected_parent_id": nested.parent_key,
                "parent_leg": "E",
                "endpoint_match": endpoint_match,
                "child_geometry_pass": child_geometry_pass,
                "without_status": current.status,
                "without_relation": current.relation,
                "with_status": nested.status,
                "with_relation": nested.relation,
            }
        )

    def qualified(breakdown) -> bool:
        groups = {item.group for item in breakdown.evidence if item.status == "AVAILABLE"}
        return (
            breakdown.quality_score >= settings.scoring.min_quality
            and breakdown.coverage >= settings.scoring.min_coverage
            and len(groups) >= 2
        )

    scored = []
    for candidate in candidates:
        classic = certify_subwaves(candidate, zigzags, settings.analysis.scales, 1, bars)
        is_triangle = is_completed_contracting_triangle(candidate)
        loose = (
            certify_subwaves(
                candidate, zigzags, settings.analysis.scales, 1, bars, classic_triangles=False
            )
            if is_triangle
            else classic
        )
        policy = None
        if is_triangle:
            policy = {
                "all_verified": loose.status == "VERIFIED",
                "classic": classic.status == "VERIFIED",
                "loose": loose.status == "VERIFIED",
            }
        classic_breakdown = score(
            candidate,
            bars,
            indicators,
            volume_comparable=prepared.provenance.volume_comparable,
            profile=settings.scoring.profile,
            subwave_fit=classic.fit,
        )
        loose_breakdown = (
            score(
                candidate,
                bars,
                indicators,
                volume_comparable=prepared.provenance.volume_comparable,
                profile=settings.scoring.profile,
                subwave_fit=loose.fit,
            )
            if loose.fit != classic.fit
            else classic_breakdown
        )
        scored.append(
            {
                "candidate": candidate,
                "classic_certification": classic,
                "loose_certification": loose,
                "policy": policy,
                "is_triangle": is_triangle,
                "classic_breakdown": classic_breakdown,
                "loose_breakdown": loose_breakdown,
                "classic_quality": classic_breakdown.quality_score,
                "loose_quality": loose_breakdown.quality_score,
                "classic_coverage": classic_breakdown.coverage,
                "loose_coverage": loose_breakdown.coverage,
                "classic_qualified": qualified(classic_breakdown),
                "loose_qualified": qualified(loose_breakdown),
            }
        )

    selected: dict[str, Any] = {}
    funnel: dict[str, int] = {
        "candidates": len(candidates),
        "completed_contracting_triangles": sum(item["is_triangle"] for item in scored),
        "all_five_legs_verified": sum(
            bool(item["policy"] and item["policy"]["all_verified"]) for item in scored
        ),
    }
    for policy in ("classic", "loose"):
        funnel[f"{policy}_policy_pass"] = sum(
            bool(item["policy"] and item["policy"][policy]) for item in scored
        )
        eligible = []
        for item in scored:
            if item["is_triangle"] and not item["policy"][policy]:
                continue
            if not item[f"{policy}_qualified"]:
                continue
            eligible.append(item)
        eligible.sort(
            key=lambda item: (
                -item[f"{policy}_quality"],
                -item[f"{policy}_coverage"],
                item["candidate"].key,
            )
        )
        top = eligible[: settings.analysis.top_k]
        funnel[f"{policy}_qualified"] = len(eligible)
        funnel[f"{policy}_triangle_qualified"] = sum(item["is_triangle"] for item in eligible)
        funnel[f"{policy}_topk_triangles"] = sum(item["is_triangle"] for item in top)
        selected[policy] = next(
            (item for item in top if item["is_triangle"]),
            None,
        )
        funnel[f"{policy}_signal"] = int(selected[policy] is not None)

    future, future_error = (
        _future_bars(bundle, decision_bar, timeframe, horizon)
        if any(item is not None for item in selected.values())
        else ((), None)
    )
    atr = float(indicators.atr[-1])
    decision_basis_bar = bars[-1]
    signals = {}
    for policy, item in selected.items():
        if item is None:
            signals[policy] = None
            continue
        payload = _signal_payload(
            item["candidate"],
            item[f"{policy}_certification"],
            item[f"{policy}_breakdown"],
            decision_basis_bar,
            atr,
            bundle.instrument.canonical_symbol,
            timeframe,
            endpoint_ids(item["candidate"]),
        )
        if future_error is None:
            event, event_date = _event_label(
                item["candidate"], decision_basis_bar, future, atr, horizon
            )
        else:
            event, event_date = future_error, None
        payload["event"] = event
        payload["event_date"] = event_date.isoformat() if event_date else None
        signals[policy] = payload

    return {
        "status": "OK",
        "candidates": len(candidates),
        "triangles": funnel["completed_contracting_triangles"],
        "search_truncated": budget.truncated,
        "classic": signals["classic"],
        "loose": signals["loose"],
        "funnel": funnel,
        "nested": nested_rows,
    }


def _metric(rows: list[dict], group: str, kind: str):
    values = []
    for row in rows:
        signal = row.get(group)
        if signal is None:
            if kind == "target_per_100":
                values.append(0.0)
            continue
        event = signal.get("event")
        if kind == "target_per_100":
            values.append(1.0 if event == "TARGET_FIRST" else 0.0)
        elif kind == "conditional" and event in CLEAR_EVENTS:
            values.append(1.0 if event == "TARGET_FIRST" else 0.0)
    if not values:
        return None
    return sum(values) / len(values)


def _bootstrap(rows: list[dict], timeframe: str, kind: str):
    dates = sorted({row["decision_date"] for row in rows})
    block = 20 if timeframe == "1d" else 8
    blocks = [dates[index : index + block] for index in range(0, len(dates), block)]
    if len(blocks) < 2:
        return None
    rng = random.Random(SEED)
    values = []
    for _ in range(BOOTSTRAP):
        sample = []
        for _ in blocks:
            chosen = rng.choice(blocks)
            sample.extend(row for row in rows if row["decision_date"] in chosen)
        classic = _metric(sample, "classic", kind)
        loose = _metric(sample, "loose", kind)
        if classic is None or loose is None:
            continue
        values.append((classic - loose) * (100 if kind == "target_per_100" else 1))
    if not values:
        return None
    values.sort()
    return [values[round(0.025 * (len(values) - 1))], values[round(0.975 * (len(values) - 1))]]


def _summary(rows: list[dict], timeframe: str) -> dict[str, Any]:
    analyzable = [row for row in rows if row.get("analyzable")]
    decisions = len(analyzable)
    excluded = Counter(row.get("status") for row in rows if not row.get("analyzable"))
    output = {
        "full_grid": len(rows),
        "analyzable": decisions,
        "excluded": dict(excluded),
        "groups": {},
    }
    for group in ("classic", "loose"):
        signals = [row[group] for row in analyzable if row.get(group) is not None]
        events = Counter(signal["event"] for signal in signals)
        clear = sum(events[event] for event in CLEAR_EVENTS)
        target = events["TARGET_FIRST"]
        output["groups"][group] = {
            "signals": len(signals),
            "coverage": len(signals) / decisions if decisions else None,
            "events": dict(events),
            "target_first_per_100_decisions": 100 * target / decisions if decisions else None,
            "conditional_hit_rate": target / clear if clear else None,
            "clear_outcomes": clear,
        }
    output["difference"] = {
        "target_first_per_100_decisions": (
            output["groups"]["classic"]["target_first_per_100_decisions"]
            - output["groups"]["loose"]["target_first_per_100_decisions"]
        ),
        "conditional_hit_rate": (
            (output["groups"]["classic"]["conditional_hit_rate"] or 0)
            - (output["groups"]["loose"]["conditional_hit_rate"] or 0)
        ),
        "target_ci": _bootstrap(analyzable, timeframe, "target_per_100"),
        "conditional_ci": _bootstrap(analyzable, timeframe, "conditional"),
    }
    funnels = [row["funnel"] for row in analyzable if row.get("funnel")]
    if funnels:
        keys = sorted({key for funnel in funnels for key in funnel})
        output["funnel"] = {key: sum(funnel.get(key, 0) for funnel in funnels) for key in keys}
    return output


def _divergences(rows: list[dict]) -> dict[str, Any]:
    counts = Counter()
    cases = []
    for row in rows:
        classic = row.get("classic")
        loose = row.get("loose")
        if classic is None and loose is None:
            counts["neither"] += 1
        elif classic is None:
            counts["loose_only"] += 1
            cases.append(
                {
                    "symbol": row["symbol"],
                    "timeframe": row["timeframe"],
                    "decision_date": row["decision_date"],
                    "side": "loose_only",
                    "loose": loose,
                }
            )
        elif loose is None:
            counts["classic_only"] += 1
            cases.append(
                {
                    "symbol": row["symbol"],
                    "timeframe": row["timeframe"],
                    "decision_date": row["decision_date"],
                    "side": "classic_only",
                    "classic": classic,
                }
            )
        elif classic["key"] == loose["key"]:
            counts["both_same"] += 1
        else:
            counts["both_different"] += 1
            cases.append(
                {
                    "symbol": row["symbol"],
                    "timeframe": row["timeframe"],
                    "decision_date": row["decision_date"],
                    "side": "both_different",
                    "classic": classic,
                    "loose": loose,
                }
            )
    return {"counts": dict(counts), "cases": cases}


def _nested_summary(nested: list[dict]) -> dict[str, Any]:
    unique: dict[tuple[str, str, tuple[str, ...], tuple[str, ...]], dict] = {}
    for row in nested:
        key = (
            row["symbol"],
            row["timeframe"],
            tuple(row["parent_endpoint_ids"]),
            tuple(row["child_endpoint_ids"]),
        )
        unique.setdefault(key, row)
    counts = Counter()
    examples = []
    for row in unique.values():
        counts[f"without_{row['without_status']}"] += 1
        counts[f"with_{row['with_status']}"] += 1
        counts["endpoint_match"] += int(row["endpoint_match"])
        counts["child_geometry_pass"] += int(row["child_geometry_pass"])
        counts["endpoint_and_geometry"] += int(row["endpoint_match"] and row["child_geometry_pass"])
        if row["without_status"] == "UNKNOWN" and row["with_status"] in {
            "PROVISIONAL",
            "VERIFIED",
        }:
            examples.append(row)
    return {
        "per_timepoint_rows": len(nested),
        "unique_pairs": len(unique),
        "counts": dict(counts),
        "new_relations": len(examples),
        "examples": examples[:20],
        "records": list(unique.values()),
    }


def _write_json(path: Path, value: Any) -> None:
    atomic_write(path, json.dumps(value, indent=2, ensure_ascii=False) + "\n")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=Path("reports/cn-triangle-experiment"))
    parser.add_argument("--max-daily", type=int, default=len(DAILY_SAMPLES))
    parser.add_argument("--max-weekly", type=int, default=len(WEEKLY_SYMBOLS))
    parser.add_argument("--allow-gaps", action="store_true")
    args = parser.parse_args()
    output_dir = args.output_dir
    output_dir.mkdir(parents=True, exist_ok=True)

    source = _source_hash()
    runner_sha256 = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    head, dirty = _git_state()
    daily = DAILY_SAMPLES[: args.max_daily]
    weekly_samples = [sample for sample in daily if sample["symbol"] in WEEKLY_SYMBOLS][
        : args.max_weekly
    ]
    protocol = {
        "experiment": "A-share triangle child-rule comparison",
        "protocol_document": "docs/reviews/2026-11-18-cn-triangle-experiment-protocol.md",
        "created_at": datetime.now(UTC).isoformat(),
        "engine_version": ENGINE_VERSION,
        "source_sha256": source,
        "runner_sha256": runner_sha256,
        "config_hashes": {
            timeframe: _settings(timeframe).computation_hash for timeframe in ("1d", "1w")
        },
        "git_head": head,
        "git_dirty": dirty,
        "windows": {
            "daily": [DAILY_START.isoformat(), DAILY_END.isoformat()],
            "weekly": [WEEKLY_START.isoformat(), WEEKLY_END.isoformat()],
        },
        "gap_policy": {
            "daily": "allow-gaps sensitivity" if args.allow_gaps else "strict",
            "weekly": "strict for indices; allow-gaps for stocks",
        },
        "horizons": {"daily": DAILY_HORIZON, "weekly": WEEKLY_HORIZON},
        "policies": {
            "classic": "all five legs verified; at least four zigzag-family legs; at most one complex leg",
            "loose": "all five legs verified under per-leg allowed templates; no group quota",
        },
        "thresholds": {
            "divergent_signals": 50,
            "target_first_per_100_decisions": 1.0,
            "paired_ci_lower": 0.0,
            "conditional_hit_rate_lower": -0.03,
            "bootstrap_resamples": BOOTSTRAP,
            "seed": SEED,
        },
        "sample_note": "Fixed pilot sample. Actual symbol lists are recorded below.",
        "daily_samples": daily,
        "weekly_symbols": sorted(sample["symbol"] for sample in weekly_samples),
    }
    _write_json(output_dir / "protocol.json", protocol)

    rows: list[dict] = []
    events: list[dict] = []
    nested: list[dict] = []
    seen: dict[str, set[str]] = {"classic": set(), "loose": set()}

    for sample in daily:
        bundle = _load_bundle(sample["snapshot_id"])
        settings = _settings("1d")
        decisions = _decision_bars(bundle, "1d", DAILY_START, DAILY_END)
        print(f"daily {sample['symbol']}: {len(decisions)} decisions", flush=True)
        for decision_bar in decisions:
            result = _analyze_decision(
                bundle,
                decision_bar,
                "1d",
                settings,
                allow_gaps=args.allow_gaps,
                horizon=DAILY_HORIZON,
            )
            row = {
                "symbol": sample["symbol"],
                "group": sample["group"],
                "timeframe": "1d",
                "decision_date": decision_bar.session_date.isoformat(),
                "status": result.get("status"),
                "analyzable": result.get("status") == "OK",
                "gaps": result.get("gaps"),
                "bars": result.get("bars"),
                "candidates": result.get("candidates"),
                "triangles": result.get("triangles"),
                "search_truncated": result.get("search_truncated"),
                "funnel": result.get("funnel"),
                "classic": result.get("classic"),
                "loose": result.get("loose"),
            }
            if result.get("status") == "OK":
                for policy in ("classic", "loose"):
                    signal = row[policy]
                    if signal is not None:
                        if signal["key"] in seen[policy]:
                            row[policy] = None
                        else:
                            seen[policy].add(signal["key"])
                            events.append(
                                {
                                    **signal,
                                    "policy": policy,
                                    "symbol": sample["symbol"],
                                    "group": sample["group"],
                                    "timeframe": "1d",
                                    "decision_date": decision_bar.session_date.isoformat(),
                                }
                            )
                nested.extend(result.get("nested", []))
            rows.append(row)

    for sample in weekly_samples:
        daily_bundle = _load_bundle(sample["snapshot_id"])
        try:
            bundle = as_weekly(daily_bundle)
        except Exception as exc:  # noqa: BLE001
            print(f"weekly {sample['symbol']}: skipped {exc}", flush=True)
            continue
        settings = _settings("1w")
        decisions = _decision_bars(bundle, "1w", WEEKLY_START, WEEKLY_END)
        print(f"weekly {sample['symbol']}: {len(decisions)} decisions", flush=True)
        for decision_bar in decisions:
            result = _analyze_decision(
                bundle,
                decision_bar,
                "1w",
                settings,
                allow_gaps=sample["group"] != "index",
                horizon=WEEKLY_HORIZON,
            )
            row = {
                "symbol": sample["symbol"],
                "group": sample["group"],
                "timeframe": "1w",
                "decision_date": decision_bar.session_date.isoformat(),
                "status": result.get("status"),
                "analyzable": result.get("status") == "OK",
                "gaps": result.get("gaps"),
                "bars": result.get("bars"),
                "candidates": result.get("candidates"),
                "triangles": result.get("triangles"),
                "search_truncated": result.get("search_truncated"),
                "funnel": result.get("funnel"),
                "classic": result.get("classic"),
                "loose": result.get("loose"),
            }
            if result.get("status") == "OK":
                for policy in ("classic", "loose"):
                    signal = row[policy]
                    if signal is not None:
                        if signal["key"] in seen[policy]:
                            row[policy] = None
                        else:
                            seen[policy].add(signal["key"])
                            events.append(
                                {
                                    **signal,
                                    "policy": policy,
                                    "symbol": sample["symbol"],
                                    "group": sample["group"],
                                    "timeframe": "1w",
                                    "decision_date": decision_bar.session_date.isoformat(),
                                }
                            )
                nested.extend(result.get("nested", []))
            rows.append(row)

    with (output_dir / "decisions.jsonl").open("w") as stream:
        for row in rows:
            stream.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")

    event_fields = [
        "policy",
        "symbol",
        "group",
        "timeframe",
        "decision_date",
        "key",
        "pattern",
        "direction",
        "d_date",
        "e_date",
        "d_price",
        "e_price",
        "quality",
        "coverage",
        "target",
        "adverse",
        "event",
        "event_date",
    ]
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=event_fields, extrasaction="ignore")
    writer.writeheader()
    for event in events:
        writer.writerow(event)
    atomic_write(output_dir / "events.csv", buffer.getvalue())

    summaries = {}
    for timeframe in ("1d", "1w"):
        subset = [row for row in rows if row["timeframe"] == timeframe]
        summaries[timeframe] = _summary(subset, timeframe)
        for group in sorted({row["group"] for row in subset}):
            summaries[f"{timeframe}:{group}"] = _summary(
                [row for row in subset if row["group"] == group], timeframe
            )
    divergences = _divergences([row for row in rows if row.get("analyzable")])
    nested_summary = _nested_summary(nested)
    summary = {
        "source_sha256": source,
        "engine_version": ENGINE_VERSION,
        "decision_rows": len(rows),
        "event_rows": len(events),
        "summaries": summaries,
        "divergences": divergences["counts"],
        "divergence_cases": divergences["cases"][:50],
        "nested": nested_summary,
    }
    _write_json(output_dir / "divergences.json", divergences)
    _write_json(output_dir / "nested_annotation.json", nested_summary)

    daily_summary = summaries["1d"]
    classic = daily_summary["groups"]["classic"]
    loose = daily_summary["groups"]["loose"]
    diff = daily_summary["difference"]
    divergent = (
        divergences["counts"].get("classic_only", 0)
        + divergences["counts"].get("loose_only", 0)
        + divergences["counts"].get("both_different", 0)
    )
    classic_meets = (
        divergent >= 50
        and (diff["target_first_per_100_decisions"] or 0) >= 1
        and diff["target_ci"] is not None
        and diff["target_ci"][0] > 0
        and diff["conditional_ci"] is not None
        and diff["conditional_ci"][0] >= -0.03
    )
    loose_meets = (
        divergent >= 50
        and -(diff["target_first_per_100_decisions"] or 0) >= 1
        and diff["target_ci"] is not None
        and diff["target_ci"][1] < 0
        and diff["conditional_ci"] is not None
        and diff["conditional_ci"][1] <= 0.03
    )
    if classic_meets:
        verdict = "classic"
    elif loose_meets:
        verdict = "loose"
    else:
        verdict = "undetermined"
    summary["verdict"] = verdict
    _write_json(output_dir / "summary.json", summary)
    conclusion = f"""# A 股三角形子浪规则对照实验结论

## 判定

**{verdict}**

- 经典组信号：{classic["signals"]}；宽松组信号：{loose["signals"]}。
- 日线每 100 个决策时点的先达目标次数：经典 {classic["target_first_per_100_decisions"]:.3f}，宽松 {loose["target_first_per_100_decisions"]:.3f}。
- 配对差值（经典-宽松）：{diff["target_first_per_100_decisions"]:.3f}；95% 区间 {diff["target_ci"]}。
- 条件命中率差值：{diff["conditional_hit_rate"]:.3f}；95% 区间 {diff["conditional_ci"]}。
- 分歧信号：{divergent}（门槛 50）。
- E 腿嵌套唯一对：{nested_summary["unique_pairs"]}；新增 PROVISIONAL/VERIFIED：{nested_summary["new_relations"]}。

## 解释

本实验是固定样本 pilot。若判定为 undetermined，不表示两套规则等价，只表示当前样本量和分歧数不足以按协议门槛区分结果。结构上，五条平台同时获得满额认证不符合 Frost/Prechter 的收敛三角形要求；经典组仍应作为结构认证口径。宽松组可作为候选筛选变体，但不能替代经典认证标签。

## 文件

- `protocol.json`
- `decisions.jsonl`
- `events.csv`
- `divergences.json`
- `nested_annotation.json`
- `summary.json`
"""
    atomic_write(output_dir / "conclusion.md", conclusion)

    files = [
        "protocol.json",
        "decisions.jsonl",
        "events.csv",
        "divergences.json",
        "nested_annotation.json",
        "summary.json",
        "conclusion.md",
    ]
    manifest = {
        "source_sha256": source,
        "runner_sha256": runner_sha256,
        "engine_version": ENGINE_VERSION,
        "git_head": head,
        "git_dirty": dirty,
        "files": {
            name: hashlib.sha256((output_dir / name).read_bytes()).hexdigest() for name in files
        },
        "decision_rows": len(rows),
        "event_rows": len(events),
        "nested_rows": len(nested),
    }
    _write_json(output_dir / "manifest.json", manifest)
    print(json.dumps(summary["summaries"]["1d"], indent=2, ensure_ascii=False))
    print(f"Verdict: {verdict}")
    print(f"Evidence: {output_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
