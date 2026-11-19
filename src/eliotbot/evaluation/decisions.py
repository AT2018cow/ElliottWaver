"""Offline decision grid and outcome evaluation for briefs."""

from __future__ import annotations

import hashlib
import json
import re
import statistics
from collections import Counter
from datetime import date
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

from eliotbot import ENGINE_VERSION
from eliotbot.config import Settings
from eliotbot.data.periods import as_weekly
from eliotbot.data.snapshots import SnapshotStore
from eliotbot.engine.indicators import calculate
from eliotbot.engine.zigzag import extract
from eliotbot.errors import DataError, InputError
from eliotbot.evaluation.features import (
    FEATURE_VERSION,
    SUPPORTED_FEATURE_VERSIONS,
    candidate_features,
    decision_features,
)
from eliotbot.evaluation.outcomes import close_band, first_touch, outcome_window, period_dates
from eliotbot.forecast import (
    ModelRegistry,
    event_horizon,
    forecasts_for_brief,
    load_model,
    validate_approval_time,
)
from eliotbot.schemas.market import AnalysisRequest, MarketDataBundle
from eliotbot.schemas.wave import WaveScenario
from eliotbot.service import AnalysisService, prepare_bundle
from eliotbot.util import atomic_write, canonical_json, digest

SUPPORTED_EVENTS = {
    "CLOSE_BAND_5D_V1",
    "CLOSE_BAND_20D_V1",
    "CONFIRM_FIRST_20D_V1",
    "TARGET_FIRST_20D_V1",
    "CLOSE_BAND_5W_V1",
    "CLOSE_BAND_20W_V1",
    "CONFIRM_FIRST_8W_V1",
    "TARGET_FIRST_8W_V1",
}
DAILY_EVENTS = {event for event in SUPPORTED_EVENTS if event.endswith("D_V1")}
WEEKLY_EVENTS = {event for event in SUPPORTED_EVENTS if event.endswith("W_V1")}
CLEAR_EVENTS = {
    "TARGET_FIRST",
    "INVALIDATED_FIRST",
    "CONFIRMED_FIRST",
    "EXPIRED",
}
EVENT_SUCCESS = {
    event: ("TARGET_FIRST", {"TARGET_FIRST", "INVALIDATED_FIRST", "EXPIRED"})
    for event in ("TARGET_FIRST_20D_V1", "TARGET_FIRST_8W_V1")
}
EVENT_SUCCESS.update(
    {
        event: ("CONFIRMED_FIRST", {"CONFIRMED_FIRST", "INVALIDATED_FIRST", "EXPIRED"})
        for event in ("CONFIRM_FIRST_20D_V1", "CONFIRM_FIRST_8W_V1")
    }
)


def _load_protocol(path: Path) -> dict:
    try:
        protocol = json.loads(path.read_text())
    except (OSError, ValueError) as exc:
        raise InputError(f"Invalid protocol: {exc}") from exc
    for key in ("protocol_version", "samples", "grid", "events"):
        if key not in protocol:
            raise InputError(f"Protocol is missing {key}")
    if protocol["protocol_version"] != "1":
        raise InputError("Unsupported protocol_version")
    if "protocol_id" in protocol and (
        not isinstance(protocol["protocol_id"], str) or not protocol["protocol_id"].strip()
    ):
        raise InputError("protocol_id must be a non-empty string")
    feature_version = str(protocol.get("feature_version", FEATURE_VERSION))
    if feature_version not in SUPPORTED_FEATURE_VERSIONS:
        raise InputError("Unsupported feature_version")
    if not protocol["samples"]:
        raise InputError("Protocol has no samples")
    for sample in protocol["samples"]:
        for key in ("symbol", "asset_type", "source_timeframe", "snapshot_id"):
            if key not in sample:
                raise InputError(f"Protocol sample is missing {key}")
        if not re.fullmatch(r"[a-f0-9]{64}", sample["snapshot_id"]):
            raise InputError(f"Invalid snapshot_id for {sample['symbol']}")
        if sample["source_timeframe"] not in {"1d", "1w"}:
            raise InputError(f"Unsupported source_timeframe for {sample['symbol']}")
    if set(protocol["events"]) - SUPPORTED_EVENTS:
        raise InputError("Protocol contains unsupported events")
    grid = protocol["grid"]
    if grid.get("cadence") not in {"EVERY_SESSION", "LAST_SESSION_OF_WEEK"}:
        raise InputError("Unsupported grid cadence")
    start, end = (date.fromisoformat(grid[key]) for key in ("start", "end"))
    if start > end:
        raise InputError("Grid start follows end")
    if grid.get("outcome_as_of"):
        date.fromisoformat(grid["outcome_as_of"])
    if "splits" in protocol:
        split_windows(protocol)
    return protocol


def split_windows(protocol: dict) -> dict[str, tuple[date, date]]:
    windows = {
        name: tuple(date.fromisoformat(protocol["splits"][name][key]) for key in ("start", "end"))
        for name in ("train", "calibration", "test")
    }
    previous_end = date.min
    for start, end in windows.values():
        if not previous_end < start <= end:
            raise InputError("Train, calibration and test must be ordered and disjoint")
        previous_end = end
    return windows


def _decision_bars(bundle: MarketDataBundle, grid: dict, timeframe: str):
    start, end = (date.fromisoformat(grid[key]) for key in ("start", "end"))
    sessions = bundle.sessions
    if sessions and (start < sessions[0] or end > sessions[-1]):
        if bundle.provenance.provider != "synthetic_demo":
            from eliotbot.data.calendar import load_calendar

            calendar = load_calendar(bundle.instrument, start, end)
            sessions = tuple(sorted(set(sessions) | set(calendar.dates)))
    dates = period_dates(
        sessions, weekly=timeframe == "1w" or grid["cadence"] == "LAST_SESSION_OF_WEEK"
    )
    by_date = {bar.session_date: bar for bar in bundle.bars}
    return [(day, by_date.get(day)) for day in dates if start <= day <= end]


def _structure_key(
    scenario: WaveScenario, bundle: MarketDataBundle, symbol: str, timeframe: str
) -> str:
    endpoints = []
    for wave in scenario.completed_waves:
        index = wave.pivot.index
        raw_id = (
            bundle.raw_bars[index].bar_id
            if 0 <= index < len(bundle.raw_bars)
            else wave.pivot.extreme_bar_id
        )
        endpoints.append((wave.label, wave.pivot.kind, raw_id))
    return digest(
        [
            symbol,
            timeframe,
            scenario.pattern_type,
            scenario.stage,
            scenario.pattern_direction,
            endpoints,
        ]
    )[:24]


def _confirmation_index(bars, pivot) -> int | None:
    if pivot.confirmed_at is None:
        return None
    for index, bar in enumerate(bars):
        if bar.usable_at >= pivot.confirmed_at:
            return index
    return None


def _confirmed_pivots(bars, raw_bars, atr, scales, tick_size) -> list[dict]:
    rows = []
    for scale in scales:
        zigzag = extract(bars, atr, scale, tick_size)
        for pivot in zigzag.confirmed:
            confirm_index = _confirmation_index(bars, pivot)
            raw_bar = raw_bars[pivot.index] if 0 <= pivot.index < len(raw_bars) else None
            raw_id = raw_bar.bar_id if raw_bar is not None else pivot.extreme_bar_id
            raw_price = pivot.price
            if raw_bar is not None:
                raw_price = raw_bar.high if pivot.kind == "HIGH" else raw_bar.low
            rows.append(
                {
                    "pivot_key": digest([raw_id, pivot.kind, str(scale)])[:24],
                    "pivot_id": pivot.pivot_id,
                    "scale": str(scale),
                    "state": pivot.state,
                    "kind": pivot.kind,
                    "index": pivot.index,
                    "extreme_date": pivot.extreme_at.date().isoformat(),
                    "confirmed_at": (
                        pivot.confirmed_at.isoformat() if pivot.confirmed_at else None
                    ),
                    "bars_to_confirm": (
                        None if confirm_index is None else max(0, confirm_index - pivot.index)
                    ),
                    "price": pivot.price,
                    "raw_price": raw_price,
                }
            )
    return rows


def _pivot_rewrite_counts(previous: list[dict], current: list[dict]) -> tuple[int, int]:
    previous_map = {pivot["pivot_key"]: pivot for pivot in previous}
    current_map = {pivot["pivot_key"]: pivot for pivot in current}
    removed = set(previous_map) - set(current_map)
    changed = {
        pivot_key
        for pivot_key in set(previous_map) & set(current_map)
        if (
            previous_map[pivot_key]["raw_price"],
            previous_map[pivot_key]["extreme_date"],
        )
        != (
            current_map[pivot_key]["raw_price"],
            current_map[pivot_key]["extreme_date"],
        )
    }
    return len(removed) + len(changed), len(set(current_map) - set(previous_map))


def _distribution(values: list[int]) -> dict:
    if not values:
        return {"count": 0, "mean": None, "median": None, "p90": None, "max": None}
    ordered = sorted(values)
    return {
        "count": len(ordered),
        "mean": statistics.mean(ordered),
        "median": statistics.median(ordered),
        "p90": ordered[int(0.9 * (len(ordered) - 1))],
        "max": ordered[-1],
    }


def _structure_stats(decisions: list[dict]) -> dict:
    analyzable = [row for row in decisions if row["analyzable"]]
    with_primary = [row for row in analyzable if row.get("first_structure_key")]
    comparable = [row for row in decisions if row.get("primary_switch") is not None]
    switches = sum(bool(row["primary_switch"]) for row in comparable)
    rewrite_pairs = [row for row in decisions if row.get("pivot_rewrite_count") is not None]
    rewrites = sum(int(row["pivot_rewrite_count"]) for row in rewrite_pairs)
    pairs_with_rewrite = sum(1 for row in rewrite_pairs if int(row["pivot_rewrite_count"]) > 0)
    new_pivots = sum(int(row.get("pivot_new_count") or 0) for row in rewrite_pairs)
    unique_pivots: dict[str, dict] = {}
    for row in decisions:
        pivots = row.get("new_pivots")
        if pivots is None:
            pivots = row.get("confirmed_pivots", [])
        for pivot in pivots:
            key = pivot.get("pivot_key") or pivot.get("pivot_id")
            if key is not None:
                unique_pivots.setdefault(key, pivot)
    delays = [
        pivot["bars_to_confirm"]
        for pivot in unique_pivots.values()
        if pivot.get("bars_to_confirm") is not None
    ]
    delays_by_scale: dict[str, list[int]] = {}
    for pivot in unique_pivots.values():
        if pivot.get("bars_to_confirm") is None:
            continue
        delays_by_scale.setdefault(pivot["scale"], []).append(pivot["bars_to_confirm"])
    return {
        "confirmed_pivots": len(unique_pivots),
        "confirmed_pivot_observations": sum(
            int(
                row.get(
                    "confirmed_pivot_count",
                    len(row.get("confirmed_pivots", [])),
                )
            )
            for row in decisions
        ),
        "pivot_rewrite_pairs": len(rewrite_pairs),
        "pivot_rewrites": rewrites,
        "pivot_rewrites_per_pair": (rewrites / len(rewrite_pairs) if rewrite_pairs else None),
        "pivot_rewrite_rate": (pairs_with_rewrite / len(rewrite_pairs) if rewrite_pairs else None),
        "new_pivots": new_pivots,
        "confirmation_delay": _distribution(delays),
        "confirmation_delay_by_scale": {
            scale: _distribution(values) for scale, values in sorted(delays_by_scale.items())
        },
        "signals": len(with_primary),
        "prediction_coverage": len(with_primary) / len(analyzable) if analyzable else None,
        "no_signal": len(analyzable) - len(with_primary),
        "primary_switches": switches,
        "comparable_primary_pairs": len(comparable),
        "primary_switch_rate": switches / len(comparable) if comparable else None,
        "search_truncated": sum(bool(row.get("search_truncated")) for row in decisions),
    }


def _event_rows(protocol, decision, source, prepared, qualified, atr):
    rows = []
    for event_id in protocol["events"]:
        if event_id not in (DAILY_EVENTS if prepared.timeframe == "1d" else WEEKLY_EVENTS):
            continue
        horizon = event_horizon(event_id)
        cutoff = protocol["grid"].get("outcome_as_of")
        window = outcome_window(
            source,
            prepared.bars[-1].session_date,
            horizon,
            outcome_as_of=date.fromisoformat(cutoff) if cutoff else None,
        )
        current = prepared.bars[-1].close
        is_close = event_id.startswith("CLOSE_BAND_")
        targets = [(0, None)] if is_close else enumerate(qualified, 1)
        due_date = window.dates[-1] if len(window.dates) == horizon else None
        due_bar = (
            next((bar for bar in source.bars if bar.session_date == due_date), None)
            if due_date is not None
            else None
        )
        label_available_at = due_bar.usable_at.isoformat() if due_bar is not None else None
        for rank, scenario in targets:
            outcome = (
                close_band(window, current, atr, horizon)
                if is_close
                else first_touch(
                    scenario, window, horizon, confirmation=event_id.startswith("CONFIRM_")
                )
            )
            rows.append(
                {
                    "decision_id": decision["decision_id"],
                    "symbol": decision["symbol"],
                    "as_of": decision["as_of"],
                    "event_id": event_id,
                    "horizon": horizon,
                    "due_date": str(due_date) if due_date is not None else None,
                    "label_available_at": label_available_at,
                    "candidate_key": scenario.scenario_id if scenario else None,
                    "status": outcome.status,
                    "session": str(outcome.session) if outcome.session else None,
                    "value": outcome.value,
                    "features": candidate_features(scenario, current, atr, rank=rank)
                    if scenario
                    else None,
                }
            )
    return rows


def _event_stats(events: list[dict], analyzable: int) -> dict[str, dict]:
    output = {}
    for event_id in sorted({row["event_id"] for row in events}):
        subset = [row for row in events if row["event_id"] == event_id]
        statuses = Counter(row["status"] for row in subset)
        success_status, clear_statuses = EVENT_SUCCESS.get(event_id, (None, set()))
        success = statuses[success_status] if success_status is not None else 0
        clear = sum(statuses[status] for status in (clear_statuses or {"UP", "RANGE", "DOWN"}))
        decision_count = len({row["decision_id"] for row in subset})
        hit_decisions = len(
            {row["decision_id"] for row in subset if row["status"] == success_status}
        )
        output[event_id] = {
            "rows": len(subset),
            "coverage": decision_count / analyzable if analyzable else None,
            "statuses": dict(statuses),
            "status_rates": {status: count / len(subset) for status, count in statuses.items()}
            if subset
            else {},
            "success_status": success_status,
            "success_count": success,
            "target_first": success if success_status == "TARGET_FIRST" else None,
            "clear_outcomes": clear,
            "success_per_100_decisions": 100 * hit_decisions / analyzable
            if analyzable and success_status
            else None,
            "conditional_hit_rate": success / clear if clear and success_status else None,
        }
    return output


def _model_usage(decisions: list[dict]) -> dict:
    with_model = [row for row in decisions if row.get("model_id")]
    retrospective = [row for row in decisions if row.get("retrospective")]
    if not with_model:
        study_mode = "NONE"
    elif retrospective:
        study_mode = "RETROSPECTIVE"
    else:
        study_mode = "DECISION_TIME"
    return {
        "study_mode": study_mode,
        "decisions_with_model": len(with_model),
        "decisions_without_model": len(decisions) - len(with_model),
        "retrospective_decisions": len(retrospective),
        "model_ids": sorted({row["model_id"] for row in with_model if row.get("model_id")}),
    }


def _breakdown(decisions: list[dict], events: list[dict]) -> dict:
    analyzable = [row for row in decisions if row["analyzable"]]
    return {
        "decisions": len(decisions),
        "analyzable": len(analyzable),
        "excluded": dict(Counter(row["status"] for row in decisions if not row["analyzable"])),
        "structure": _structure_stats(decisions),
        "model_usage": _model_usage(decisions),
        "events": _event_stats(events, len(analyzable)),
    }


def _summary(decisions: list[dict], events: list[dict]) -> dict:
    output = _breakdown(decisions, events)
    output["by_symbol"] = {}
    for symbol in sorted({row["symbol"] for row in decisions}):
        output["by_symbol"][symbol] = _breakdown(
            [row for row in decisions if row["symbol"] == symbol],
            [row for row in events if row["symbol"] == symbol],
        )
    output["by_asset_type"] = {}
    for asset_type in sorted({row["asset_type"] for row in decisions}):
        output["by_asset_type"][asset_type] = _breakdown(
            [row for row in decisions if row["asset_type"] == asset_type],
            [
                row
                for row in events
                if row["symbol"]
                in {
                    decision["symbol"]
                    for decision in decisions
                    if decision["asset_type"] == asset_type
                }
            ],
        )
    output["by_timeframe"] = {}
    for timeframe in sorted({row["timeframe"] for row in decisions}):
        output["by_timeframe"][timeframe] = _breakdown(
            [row for row in decisions if row["timeframe"] == timeframe],
            [
                row
                for row in events
                if row["decision_id"]
                in {
                    decision["decision_id"]
                    for decision in decisions
                    if decision["timeframe"] == timeframe
                }
            ],
        )
    output["by_pattern"] = {}
    by_id = {row["decision_id"]: row for row in decisions if row.get("decision_id")}
    patterns = {row.get("first_pattern") or "NONE" for row in decisions}
    patterns.update(row["features"]["pattern"] for row in events if row.get("candidate_key"))
    for pattern in sorted(patterns):
        subset = [
            row
            for row in events
            if (
                (row.get("features") or {}).get("pattern")
                or by_id[row["decision_id"]].get("first_pattern")
                or "NONE"
            )
            == pattern
        ]
        keys = {row["decision_id"] for row in subset}
        output["by_pattern"][pattern] = _breakdown(
            [
                row
                for row in decisions
                if row.get("decision_id") in keys or (row.get("first_pattern") or "NONE") == pattern
            ],
            subset,
        )
    output["by_horizon"] = {}
    for horizon in sorted({row["horizon"] for row in events}):
        subset = [row for row in events if row["horizon"] == horizon]
        output["by_horizon"][str(horizon)] = _event_stats(subset, output["analyzable"])
    return output


def evaluate_protocol(
    protocol_path: Path,
    output_dir: Path,
    *,
    model_registry: Path | None = None,
    model: Path | None = None,
    model_approved_at: str | None = None,
) -> dict:
    protocol = _load_protocol(protocol_path)
    protocol_hash = hashlib.sha256(protocol_path.read_bytes()).hexdigest()
    package = Path(__file__).resolve().parents[1]
    source_sha256 = digest(
        {
            "src/eliotbot/" + path.relative_to(package).as_posix(): hashlib.sha256(
                path.read_bytes()
            ).hexdigest()
            for path in sorted(package.rglob("*.py"))
        }
    )
    protocol_identity = protocol.get("protocol_id") or protocol_hash
    cache_root = Path(protocol.get("cache_root", ".cache/eliotbot"))
    settings = Settings(**protocol.get("settings", {}))
    store = SnapshotStore(cache_root)
    service = AnalysisService(settings)
    forced_model = load_model(Path(model), operational=True) if model else None
    registry = ModelRegistry(Path(model_registry)) if model_registry else None
    if model_approved_at and forced_model is None:
        raise InputError("An approval date requires --model")
    try:
        forced_approved_at = (
            validate_approval_time(forced_model, model_approved_at) if model_approved_at else None
        )
    except ValueError as exc:
        raise InputError(str(exc)) from exc
    decisions = []
    events = []
    for sample in protocol["samples"]:
        symbol = sample["symbol"]
        snapshot_id = sample["snapshot_id"]
        source_timeframe = sample.get("source_timeframe", "1d")
        if source_timeframe not in {"1d", "1w"}:
            raise InputError(f"Unsupported source_timeframe for {symbol}")
        bundle = store.load(snapshot_id)
        if (
            bundle.instrument.canonical_symbol != symbol
            or bundle.instrument.asset_type != sample["asset_type"]
        ):
            raise InputError(f"Snapshot identity mismatch for {symbol}")
        if source_timeframe == "1w":
            if bundle.timeframe == "1d":
                bundle = as_weekly(bundle)
            elif bundle.timeframe != "1w":
                raise InputError(f"Snapshot timeframe is not daily or weekly for {symbol}")
        elif bundle.timeframe != "1d":
            raise InputError(f"Daily evaluation requires a daily snapshot for {symbol}")
        timeframe = source_timeframe
        previous_pivots: list[dict] | None = None
        previous_decision: dict | None = None
        for decision_day, decision_bar in _decision_bars(bundle, protocol["grid"], timeframe):
            decision_id = digest([protocol_identity, symbol, decision_day, timeframe, snapshot_id])[
                :24
            ]
            identity = {
                "decision_id": decision_id,
                "symbol": symbol,
                "asset_type": bundle.instrument.asset_type,
                "market": bundle.instrument.market,
                "timeframe": timeframe,
                "feed": bundle.provenance.feed,
                "adjustment": bundle.provenance.adjustment,
                "decision_date": str(decision_day),
                "source_snapshot_id": snapshot_id,
                "as_of": decision_bar.usable_at.isoformat() if decision_bar is not None else None,
            }
            if decision_bar is None:
                decisions.append(
                    {
                        **identity,
                        "status": "INPUT_UNAVAILABLE",
                        "analyzable": False,
                        "error": "Missing scheduled bar",
                        "features": None,
                    }
                )
                previous_pivots = previous_decision = None
                continue
            request = AnalysisRequest(
                symbol=symbol,
                as_of=decision_bar.usable_at,
                end=decision_bar.session_date,
                mode="FROZEN_DATA_REPLAY",
                timeframe=timeframe,
                allow_gaps=protocol.get("allow_gaps", False),
            )
            try:
                prepared = prepare_bundle(request, bundle)
                context = service.analyze_with_context(request, prepared)
                result = context.result
                weekly = None
                if timeframe == "1d":
                    try:
                        weekly = service.weekly_background(request, bundle)[0].result
                    except DataError:
                        pass
                analyzable = result.status not in {"INSUFFICIENT_DATA", "STALE_DATA"}
                indicators = calculate(prepared.bars)
                filters = dict(
                    engine_version=ENGINE_VERSION,
                    config_hash=settings.computation_hash,
                    timeframe=timeframe,
                    asset_type=bundle.instrument.asset_type,
                    feed=bundle.provenance.feed,
                    adjustment=bundle.provenance.adjustment,
                    market=bundle.instrument.market,
                    symbol=bundle.instrument.canonical_symbol,
                )
                if forced_model is not None and ModelRegistry._compatible(forced_model, **filters):
                    active_model = forced_model
                    active_approved_at = (
                        forced_approved_at.isoformat() if forced_approved_at else None
                    )
                    retrospective = (
                        forced_approved_at is None or decision_bar.usable_at < forced_approved_at
                    )
                elif registry is not None:
                    active = registry.active_at(decision_bar.usable_at, **filters)
                    active_model = active["model"] if active else None
                    active_approved_at = active["approved_at"] if active else None
                    retrospective = False
                else:
                    active_model = None
                    active_approved_at = None
                    retrospective = False
                forecasts = forecasts_for_brief(
                    result,
                    prepared,
                    indicators,
                    active_model,
                    qualified=context.qualified,
                    weekly=weekly,
                )
                pivots = _confirmed_pivots(
                    prepared.bars,
                    prepared.raw_bars,
                    indicators.atr,
                    settings.analysis.scales,
                    prepared.instrument.tick_size,
                )
                pivot_rewrite_count = None
                pivot_new_count = None
                new_pivots = pivots
                if previous_pivots is not None:
                    pivot_rewrite_count, pivot_new_count = _pivot_rewrite_counts(
                        previous_pivots, pivots
                    )
                    previous_keys = {pivot["pivot_key"] for pivot in previous_pivots}
                    new_pivots = [
                        pivot for pivot in pivots if pivot["pivot_key"] not in previous_keys
                    ]
                scenario = result.scenarios[0] if result.scenarios else None
                first_key = (
                    _structure_key(scenario, prepared, symbol, timeframe)
                    if scenario is not None
                    else None
                )
                primary_switch = None
                if (
                    previous_decision is not None
                    and previous_decision.get("analyzable")
                    and analyzable
                    and previous_decision.get("first_structure_key")
                    and first_key
                ):
                    primary_switch = previous_decision["first_structure_key"] != first_key
                decision = {
                    **identity,
                    "input_snapshot_id": prepared.snapshot_id,
                    "status": result.status,
                    "analyzable": analyzable,
                    "first_structure_key": first_key,
                    "first_pattern": scenario.pattern_type if scenario is not None else None,
                    "first_stage": scenario.stage if scenario is not None else None,
                    "candidate_keys": [item.scenario_id for item in result.scenarios],
                    "anchors": [str(day) for day in result.diagnostics.anchors],
                    "price_coordinate_id": result.price_coordinate_id,
                    "search_truncated": result.diagnostics.search_truncated,
                    "confirmed_pivot_count": len(pivots),
                    "new_pivots": new_pivots,
                    "pivot_rewrite_count": pivot_rewrite_count,
                    "pivot_new_count": pivot_new_count,
                    "primary_switch": primary_switch,
                    "model_id": (
                        active_model.get("model_id") if active_model is not None else None
                    ),
                    "model_approved_at": active_approved_at,
                    "retrospective": retrospective,
                    "forecasts": [item.model_dump(mode="json") for item in forecasts],
                    "features": decision_features(
                        result, prepared, indicators, qualified=context.qualified, weekly=weekly
                    ),
                }
                if analyzable and result.status != "SEARCH_LIMITED":
                    events.extend(
                        _event_rows(
                            protocol,
                            decision,
                            bundle,
                            prepared,
                            context.qualified,
                            float(indicators.atr[-1]),
                        )
                    )
                decisions.append(decision)
                previous_pivots = pivots
                previous_decision = decision
            except DataError as exc:
                decisions.append(
                    {
                        **identity,
                        "status": "INPUT_UNAVAILABLE",
                        "analyzable": False,
                        "features": None,
                        "error": str(exc),
                    }
                )
                previous_pivots = None
                previous_decision = None
    return _write_artifacts(
        protocol,
        protocol_hash,
        decisions,
        events,
        output_dir,
        settings,
        source_sha256=source_sha256,
    )


def _write_artifacts(
    protocol: dict,
    protocol_hash: str,
    decisions: list[dict],
    events: list[dict],
    output_dir: Path,
    settings: Settings,
    *,
    source_sha256: str,
    merged_from: list[str] | None = None,
) -> dict:
    output_dir.mkdir(parents=True, exist_ok=True)
    with (output_dir / "decisions.jsonl").open("w") as stream:
        for row in decisions:
            stream.write(canonical_json(row) + "\n")
    pq.write_table(pa.Table.from_pylist(events), output_dir / "events.parquet")
    summary = _summary(decisions, events)
    atomic_write(output_dir / "summary.json", canonical_json(summary) + "\n")
    files = ["decisions.jsonl", "events.parquet", "summary.json"]
    manifest = {
        "schema_version": "1.1",
        "feature_version": str(protocol.get("feature_version", FEATURE_VERSION)),
        "protocol": protocol,
        "protocol_sha256": protocol_hash,
        "source_sha256": source_sha256,
        "engine_version": ENGINE_VERSION,
        "config_hash": settings.computation_hash,
        "model_usage": summary["model_usage"],
        "samples": protocol["samples"],
        "files": {
            name: hashlib.sha256((output_dir / name).read_bytes()).hexdigest() for name in files
        },
    }
    if merged_from:
        manifest["merged_from"] = merged_from
    atomic_write(output_dir / "manifest.json", canonical_json(manifest) + "\n")
    return summary


def read_evaluation_manifest(directory: Path) -> dict:
    manifest = json.loads((directory / "manifest.json").read_text())
    protocol = manifest.get("protocol")
    if (
        manifest.get("schema_version") != "1.1"
        or not isinstance(protocol, dict)
        or manifest.get("feature_version") not in SUPPORTED_FEATURE_VERSIONS
        or manifest["feature_version"] != str(protocol.get("feature_version", FEATURE_VERSION))
        or set(manifest.get("files", {})) != {"decisions.jsonl", "events.parquet", "summary.json"}
    ):
        raise InputError(f"Unsupported evaluation manifest: {directory}")
    for name, expected in manifest["files"].items():
        if hashlib.sha256((directory / name).read_bytes()).hexdigest() != expected:
            raise InputError(f"Evaluation checksum mismatch: {directory / name}")
    return manifest


def merge_evaluations(protocol_path: Path, input_dirs: list[Path], output_dir: Path) -> dict:
    """Merge per-sample evaluation outputs into one protocol-consistent result."""
    protocol = _load_protocol(protocol_path)
    protocol_hash = hashlib.sha256(protocol_path.read_bytes()).hexdigest()
    settings = Settings(**protocol.get("settings", {}))
    decisions: dict[tuple, dict] = {}
    events: dict[tuple, dict] = {}
    seen_samples = set()
    source_sha256 = None
    for directory in input_dirs:
        manifest = read_evaluation_manifest(directory)
        identity = manifest.get("source_sha256")
        if not isinstance(identity, str) or not re.fullmatch(r"[a-f0-9]{64}", identity):
            raise InputError(f"Missing or invalid generating source fingerprint: {directory}")
        if source_sha256 is not None and source_sha256 != identity:
            raise InputError(f"Mismatched generating source fingerprint: {directory}")
        source_sha256 = identity
        partial = manifest["protocol"]
        if (
            {k: v for k, v in protocol.items() if k != "samples"}
            != {k: v for k, v in partial.items() if k != "samples"}
            or manifest["engine_version"] != ENGINE_VERSION
            or manifest["config_hash"] != settings.computation_hash
        ):
            raise InputError(f"Incompatible evaluation protocol: {directory}")
        if any(sample not in protocol["samples"] for sample in partial["samples"]):
            raise InputError(f"Unexpected evaluation samples: {directory}")
        seen_samples.update(digest(sample) for sample in partial["samples"])
        decisions_path = directory / "decisions.jsonl"
        events_path = directory / "events.parquet"
        if not decisions_path.exists() or not events_path.exists():
            raise InputError(f"Evaluation directory is incomplete: {directory}")
        with decisions_path.open() as stream:
            for line in stream:
                row = json.loads(line)
                key = row.get("decision_id") or (
                    row["symbol"],
                    row["timeframe"],
                    row["decision_date"],
                    row.get("source_snapshot_id"),
                    row["status"],
                )
                decisions[key] = row
        for row in pq.read_table(events_path).to_pylist():
            key = (
                row.get("decision_id"),
                row.get("event_id"),
                row.get("candidate_key"),
                row.get("symbol"),
                row.get("as_of"),
            )
            events[key] = row
    if source_sha256 is None or seen_samples != {digest(sample) for sample in protocol["samples"]}:
        raise InputError("Merged evaluation does not cover the protocol sample")
    decision_rows = sorted(
        decisions.values(),
        key=lambda row: (
            row["decision_date"],
            row["symbol"],
            row["timeframe"],
            row.get("decision_id") or "",
        ),
    )
    event_rows = sorted(
        events.values(),
        key=lambda row: (
            row["as_of"],
            row["symbol"],
            row["event_id"],
            row.get("candidate_key") or "",
        ),
    )
    return _write_artifacts(
        protocol,
        protocol_hash,
        decision_rows,
        event_rows,
        output_dir,
        settings,
        source_sha256=source_sha256,
        merged_from=[str(directory) for directory in input_dirs],
    )
