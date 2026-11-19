"""CLI entrypoints; a minimal argparse bootstrap works before optional UI install."""

import argparse
import json
import time
import webbrowser
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, date, datetime
from functools import wraps
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

from pydantic import ValidationError
from rich.console import Console
from rich.table import Table

from eliotbot import ENGINE_VERSION, __version__
from eliotbot.brief import attach_previous, build_brief
from eliotbot.cli.console_render import render_brief, render_result
from eliotbot.config import (
    alpaca_credentials,
    large_degree_settings,
    load_settings,
    long_term_settings,
)
from eliotbot.data.calendar import parse_as_of
from eliotbot.data.demo import demo_bundle
from eliotbot.data.router import route_symbol
from eliotbot.data.snapshots import SnapshotStore
from eliotbot.data.workflow import fetch_bundle
from eliotbot.errors import DataError, EliotError, InputError
from eliotbot.evaluation.replay import replay
from eliotbot.forecast import ModelRegistry, load_model
from eliotbot.history import HistoryStore, base_lineage_key
from eliotbot.schemas.market import AnalysisRequest
from eliotbot.service import AnalysisService, prepare_bundle
from eliotbot.settlement import SettlementStore, support_matrix
from eliotbot.timeframes import TIMEFRAMES, normalize_timeframe
from eliotbot.util import atomic_write, canonical_json, digest
from eliotbot.visualization.brief_formatter import publish_brief, read_brief_report
from eliotbot.visualization.report_formatter import publish_report


def guarded(function):
    @wraps(function)
    def wrapped(*args, **kwargs):
        try:
            return function(*args, **kwargs)
        except EliotError as exc:
            Console(stderr=True).print(f"{exc.code}: {exc}", markup=False)
            raise SystemExit(exc.exit_code) from exc
        except (ValidationError, ValueError) as exc:
            Console(stderr=True).print(f"INVALID_INPUT: {exc}", markup=False)
            raise SystemExit(2) from exc

    return wrapped


def _day(value):
    return date.fromisoformat(value) if value else None


@guarded
def run_analyze(
    symbol,
    *,
    snapshot=None,
    offline=False,
    as_of=None,
    start=None,
    end=None,
    anchor=None,
    feed=None,
    top_k=None,
    config=None,
    output_dir=None,
    format=None,
    renderer="plotly",
    json_output=False,
    refresh=False,
    open_browser=False,
    allow_stale=False,
    allow_gaps=False,
    long_term=False,
    large_degree=False,
    subwave_depth=None,
    timeframe=None,
):
    settings = load_settings(Path(config) if config else None)
    if subwave_depth is not None and subwave_depth not in {0, 1, 2}:
        raise InputError("--subwave-depth must be 0, 1, or 2")
    if long_term:
        settings = long_term_settings(settings)
    elif large_degree:
        settings = large_degree_settings(settings)
    if subwave_depth is not None:
        settings = settings.model_copy(
            update={
                "search": settings.search.model_copy(update={"max_subwave_depth": subwave_depth})
            }
        )
    store = SnapshotStore(settings.cache.directory)
    instrument = route_symbol(symbol)
    if offline and not snapshot:
        raise InputError("--offline requires --snapshot <snapshot-id>")
    if snapshot:
        bundle = store.load(snapshot)
        cutoff = (
            parse_as_of(
                as_of, instrument, datetime.now(UTC), settings.data.delayed_access_buffer_minutes
            )
            if as_of
            else bundle.as_of
        )
    else:
        cutoff = parse_as_of(
            as_of, instrument, datetime.now(UTC), settings.data.delayed_access_buffer_minutes
        )
    request = AnalysisRequest(
        symbol=symbol,
        as_of=cutoff,
        start=_day(start),
        end=_day(end),
        anchor=_day(anchor),
        feed=feed,
        top_k=top_k,
        allow_stale=allow_stale,
        allow_gaps=allow_gaps,
        mode="FROZEN_DATA_REPLAY" if snapshot or as_of else "LIVE",
        timeframe=normalize_timeframe(
            timeframe or (bundle.timeframe if snapshot else settings.analysis.timeframe)
        ),
    )
    if not snapshot:
        bundle = fetch_bundle(request, settings, refresh=refresh)
    bundle = prepare_bundle(request, bundle)
    store.save(bundle)
    result = AnalysisService(settings).analyze_bundle(request, bundle)
    formats = tuple(format.split(",")) if format is not None else settings.output.formats
    formats = tuple(x.strip() for x in formats if x.strip() and x.strip() != "json")
    output = publish_report(
        result,
        bundle,
        Path(output_dir) if output_dir else settings.output.directory,
        formats,
        renderer,
    )
    if json_output:
        print(canonical_json(result))
    else:
        render_result(result)
        Console().print(f"报告：{output}", markup=False)
        Console().print(f"快照：{bundle.snapshot_id}")
    if open_browser and "html" in formats:
        webbrowser.open((output / "chart.html").resolve().as_uri())
    if result.status == "INSUFFICIENT_DATA" or (result.status == "STALE_DATA" and not allow_stale):
        raise SystemExit(3)
    return output


def _choose_brief_timeframe(requested, source, start, cutoff):
    if requested not in {None, "auto"}:
        chosen = normalize_timeframe(requested)
        return chosen, "显式选择"
    if source is not None and source.timeframe == "1w":
        return "1w", "快照本身为周线"
    if start:
        start_day = _day(start)
        if start_day and (cutoff.date() - start_day).days > 5 * 365:
            return "1w", "请求跨度超过 5 年，自动使用周线"
    return "1d", "默认日线；长历史可用 --timeframe 1w"


def _run_brief_core(
    symbol,
    *,
    snapshot=None,
    offline=False,
    as_of=None,
    start=None,
    end=None,
    anchor=None,
    feed=None,
    top_k=None,
    config=None,
    output_dir=None,
    format=None,
    renderer="plotly",
    json_output=False,
    refresh=False,
    open_browser=False,
    allow_stale=False,
    allow_gaps=False,
    subwave_depth=None,
    weekly=True,
    window_check=True,
    model=None,
    model_registry=None,
    timeframe="auto",
):
    settings = load_settings(Path(config) if config else None)
    if subwave_depth is not None and subwave_depth not in {0, 1, 2}:
        raise InputError("--subwave-depth must be 0, 1, or 2")
    if subwave_depth is not None:
        settings = settings.model_copy(
            update={
                "search": settings.search.model_copy(update={"max_subwave_depth": subwave_depth})
            }
        )
    store = SnapshotStore(settings.cache.directory)
    instrument = route_symbol(symbol)
    if offline and not snapshot:
        raise InputError("--offline requires --snapshot <snapshot-id>")
    source = store.load(snapshot) if snapshot else None
    if as_of:
        cutoff = parse_as_of(
            as_of,
            instrument,
            datetime.now(UTC),
            settings.data.delayed_access_buffer_minutes,
        )
    elif source is not None:
        cutoff = source.as_of
    else:
        cutoff = parse_as_of(
            None,
            instrument,
            datetime.now(UTC),
            settings.data.delayed_access_buffer_minutes,
        )
    chosen, reason = _choose_brief_timeframe(timeframe, source, start, cutoff)
    if source is not None and source.timeframe == "1w" and chosen == "1d":
        raise InputError("周线快照不能降为日线；请使用 --timeframe 1w")
    request = AnalysisRequest(
        symbol=symbol,
        as_of=cutoff,
        start=_day(start),
        end=_day(end),
        anchor=_day(anchor),
        feed=feed,
        top_k=top_k,
        allow_stale=allow_stale,
        allow_gaps=allow_gaps,
        mode="FROZEN_DATA_REPLAY" if snapshot or as_of or end else "LIVE",
        timeframe=chosen,
    )
    if source is None:
        source = fetch_bundle(request, settings, refresh=refresh)
    model_cutoff = cutoff
    if end:
        decision_bundle = prepare_bundle(request, source)
        model_cutoff = min(cutoff, decision_bundle.bars[-1].usable_at)
    model_data = None
    approved_at = None
    model_issue = None
    filters = dict(
        engine_version=ENGINE_VERSION,
        config_hash=settings.computation_hash,
        timeframe=chosen,
        asset_type=source.instrument.asset_type,
        feed=source.provenance.feed,
        adjustment=source.provenance.adjustment,
        market=source.instrument.market,
        symbol=source.instrument.canonical_symbol,
    )
    try:
        if model:
            model_data = load_model(Path(model), operational=True)
            if not ModelRegistry._compatible(model_data, **filters):
                model_data = None
                model_issue = "模型与当前市场、周期或配置不兼容"
        else:
            registry_path = (
                Path(model_registry)
                if model_registry
                else Path(settings.cache.directory) / "models"
            )
            active = ModelRegistry(registry_path).active_at(model_cutoff, **filters)
            if active:
                model_data = active["model"]
                approved_at = datetime.fromisoformat(active["approved_at"])
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        model_data = None
        model_issue = "无法读取符合发布要求的概率模型"
    run = build_brief(
        request,
        source,
        settings,
        timeframe_reason=reason,
        include_weekly=weekly,
        check_windows=window_check,
        model=model_data,
        model_approved_at=approved_at,
        retrospective=bool(model and model_data),
        model_issue=model_issue,
    )
    history = HistoryStore(settings.cache.directory)
    with store.lock(digest(["brief-lineage", base_lineage_key(run.brief), run.brief.mode])):
        previous_source = None
        previous_result = None
        try:
            previous_row = history.previous(run.brief)
            previous = None
            if previous_row is not None:
                previous, previous_result = history.load_report(previous_row)
        except DataError:
            previous_row, previous, previous_result = None, None, None
        if previous_row is not None:
            try:
                previous_source = store.load(previous_row["source_snapshot_id"])
            except DataError:
                pass
        run = attach_previous(
            run,
            previous,
            previous_row["report_path"] if previous_row is not None else None,
            previous_source=previous_source,
            current_source=run.source,
            previous_result=previous_result,
        )
        store.save(run.source)
        store.save(run.prepared)
        if run.weekly_prepared is not None:
            store.save(run.weekly_prepared)
        formats = tuple(format.split(",")) if format is not None else settings.output.formats
        formats = tuple(x.strip() for x in formats if x.strip() and x.strip() != "json")
        output = publish_brief(
            run,
            Path(output_dir) if output_dir else settings.output.directory,
            formats,
            renderer,
        )
        history.register(run.brief, output)
    return run, output


def _brief_exit_code(status: str, *, allow_stale: bool = False) -> int:
    return 3 if status == "INSUFFICIENT_DATA" or (status == "STALE_DATA" and not allow_stale) else 0


@guarded
def run_brief(
    symbol,
    *,
    snapshot=None,
    offline=False,
    as_of=None,
    start=None,
    end=None,
    anchor=None,
    feed=None,
    top_k=None,
    config=None,
    output_dir=None,
    format=None,
    renderer="plotly",
    json_output=False,
    refresh=False,
    open_browser=False,
    allow_stale=False,
    allow_gaps=False,
    subwave_depth=None,
    weekly=True,
    window_check=True,
    model=None,
    model_registry=None,
    timeframe="auto",
):
    run, output = _run_brief_core(
        symbol,
        snapshot=snapshot,
        offline=offline,
        as_of=as_of,
        start=start,
        end=end,
        anchor=anchor,
        feed=feed,
        top_k=top_k,
        config=config,
        output_dir=output_dir,
        format=format,
        renderer=renderer,
        json_output=json_output,
        refresh=refresh,
        open_browser=open_browser,
        allow_stale=allow_stale,
        allow_gaps=allow_gaps,
        subwave_depth=subwave_depth,
        weekly=weekly,
        window_check=window_check,
        model=model,
        model_registry=model_registry,
        timeframe=timeframe,
    )
    formats = (
        tuple(format.split(","))
        if format is not None
        else load_settings(Path(config) if config else None).output.formats
    )
    formats = tuple(x.strip() for x in formats if x.strip() and x.strip() != "json")
    if json_output:
        print(canonical_json(run.brief))
    else:
        render_brief(run.brief, run.result)
        Console().print(f"摘要：{output}", markup=False)
        Console().print(f"源快照：{run.brief.source_snapshot_id}")
        Console().print(f"分析输入：{run.brief.input_snapshot_id}")
    if open_browser and "html" in formats:
        webbrowser.open((output / "index.html").resolve().as_uri())
    exit_code = _brief_exit_code(run.brief.status, allow_stale=allow_stale)
    if exit_code:
        raise SystemExit(exit_code)
    return output


def _read_watchlist(path):
    entries = []
    seen = set()
    try:
        lines = Path(path).read_text().splitlines()
    except (OSError, UnicodeError) as exc:
        raise InputError(f"Unable to read watchlist: {path}") from exc
    for line in lines:
        value = line.strip()
        if not value or value.startswith("#"):
            continue
        try:
            symbol = route_symbol(value).canonical_symbol
        except InputError as exc:
            entries.append((value, exc))
            continue
        if symbol not in seen:
            seen.add(symbol)
            entries.append((symbol, None))
    return entries


@guarded
def run_watchlist(
    file,
    *,
    output_dir="reports/watchlist",
    as_of=None,
    timeframe="auto",
    config=None,
    json_output=False,
    allow_gaps=False,
    weekly=True,
    window_check=True,
    subwave_depth=None,
    model=None,
    model_registry=None,
    retries=1,
    workers=1,
    retry_delay=0.5,
    refresh=False,
):
    if retries not in {0, 1, 2, 3, 4, 5}:
        raise InputError("--retries must be between 0 and 5")
    if workers not in {1, 2, 4, 8}:
        raise InputError("--workers must be 1, 2, 4, or 8")
    if retry_delay < 0:
        raise InputError("--retry-delay must be non-negative")
    entries = _read_watchlist(file)

    def run_one(entry):
        symbol, route_error = entry
        if route_error is not None:
            return {
                "symbol": symbol,
                "status": "ERROR",
                "brief_digest": None,
                "market_asof": None,
                "path": None,
                "error": f"{route_error.code}: {route_error}",
                "attempts": 0,
                "exit_code": route_error.exit_code,
            }
        attempts = 0
        last_error = None
        for attempt in range(retries + 1):
            attempts = attempt + 1
            try:
                run, output = _run_brief_core(
                    symbol,
                    as_of=as_of,
                    config=config,
                    output_dir=output_dir,
                    format="json",
                    refresh=refresh,
                    allow_gaps=allow_gaps,
                    subwave_depth=subwave_depth,
                    weekly=weekly,
                    window_check=window_check,
                    model=model,
                    model_registry=model_registry,
                    timeframe=timeframe,
                )
                exit_code = _brief_exit_code(run.brief.status)
                return {
                    "symbol": symbol,
                    "status": run.brief.status,
                    "brief_digest": run.brief.brief_digest,
                    "market_asof": str(run.brief.market_asof),
                    "path": str(output),
                    "error": "数据不足或陈旧" if exit_code else None,
                    "exit_code": exit_code,
                    "attempts": attempts,
                }
            except SystemExit as exc:
                return {
                    "symbol": symbol,
                    "status": "INSUFFICIENT_DATA",
                    "brief_digest": None,
                    "market_asof": None,
                    "path": None,
                    "error": "数据不足或陈旧",
                    "attempts": attempts,
                    "exit_code": int(exc.code or 3),
                }
            except EliotError as exc:
                last_error = {
                    "symbol": symbol,
                    "status": "ERROR",
                    "brief_digest": None,
                    "market_asof": None,
                    "path": None,
                    "error": f"{exc.code}: {exc}",
                    "attempts": attempts,
                    "exit_code": exc.exit_code,
                }
            except Exception as exc:  # noqa: BLE001
                last_error = {
                    "symbol": symbol,
                    "status": "ERROR",
                    "brief_digest": None,
                    "market_asof": None,
                    "path": None,
                    "error": f"{type(exc).__name__}: {exc}",
                    "attempts": attempts,
                    "exit_code": 6,
                }
            if attempt < retries and retry_delay:
                time.sleep(retry_delay)
        return last_error

    if workers == 1:
        records = [run_one(entry) for entry in entries]
    else:
        with ThreadPoolExecutor(max_workers=workers) as executor:
            records = list(executor.map(run_one, entries))
    first_exit = next(
        (record["exit_code"] for record in records if record.get("exit_code")),
        0,
    )
    if json_output:
        print(canonical_json(records))
    else:
        table = Table("标的", "状态", "行情截止", "摘要", "尝试", "错误")
        for record in records:
            table.add_row(
                record["symbol"],
                record["status"],
                record["market_asof"] or "—",
                (record["brief_digest"] or "—")[:12],
                str(record["attempts"]),
                record["error"] or "—",
            )
        Console().print(table)
    if first_exit:
        raise SystemExit(first_exit)
    return records


@guarded
def run_settle(
    reports,
    *,
    cache_root=None,
    snapshot=None,
    output=None,
    config=None,
    json_output=False,
):
    settings = load_settings(Path(config) if config else None)
    root = Path(cache_root) if cache_root else settings.cache.directory
    source_path = Path(reports)
    if source_path.is_file():
        if source_path.name != "brief.json":
            raise InputError("Settlement requires a brief.json file or a report directory")
        paths = [source_path]
    elif source_path.is_dir():
        paths = sorted(source_path.rglob("brief.json"))
    else:
        raise InputError(f"Settlement report path does not exist: {source_path}")
    paths = [path for path in paths if not any(part.startswith(".brief-") for part in path.parts)]
    if not paths:
        raise InputError(f"No published briefs found: {source_path}")
    store = SettlementStore(root)
    snapshots = SnapshotStore(root)
    records = []
    seen = set()
    exit_code = 0
    for brief_path in paths:
        try:
            brief, _ = read_brief_report(brief_path.parent)
            if brief.brief_digest in seen:
                continue
            seen.add(brief.brief_digest)
            source = (
                snapshots.load(snapshot) if snapshot else snapshots.load(brief.source_snapshot_id)
            )
            store.register_brief(brief, brief_path.parent)
            for row in store.settle_brief(brief, brief_path.parent, source):
                records.append(
                    {
                        "symbol": row["symbol"],
                        "brief_digest": row["brief_digest"],
                        "market_asof": row["market_asof"],
                        "event_id": row["event_id"],
                        "scope": row["scope"],
                        "status": row["status"],
                        "latest_revision": row["latest_revision"],
                        "initial": row["initial"],
                        "latest": row["latest"],
                    }
                )
        except (EliotError, OSError, ValueError) as exc:
            exit_code = exit_code or (exc.exit_code if isinstance(exc, EliotError) else 3)
            records.append(
                {
                    "brief_path": str(brief_path),
                    "error": f"{type(exc).__name__}: {exc}",
                }
            )
    result = {"schema_version": "1.0", "records": records}
    if output:
        atomic_write(Path(output), canonical_json(result) + "\n")
    if json_output:
        print(canonical_json(result))
    else:
        table = Table("标的", "行情截止", "事件", "作用域", "状态", "修订", "最新结果")
        for record in records:
            if "error" in record:
                table.add_row("—", "—", "—", "—", "ERROR", "—", record["error"])
                continue
            latest = record["latest"] or {}
            table.add_row(
                record["symbol"],
                record["market_asof"],
                record["event_id"],
                record["scope"],
                record["status"],
                str(record["latest_revision"]),
                str(latest.get("outcome") or "—"),
            )
        Console().print(table)
    if exit_code:
        raise SystemExit(exit_code)
    return result


@guarded
def run_support_matrix(*, model_summary=None, output=None, json_output=False):
    summary = json.loads(Path(model_summary).read_text()) if model_summary else None
    matrix = support_matrix(summary)
    if output:
        atomic_write(Path(output), canonical_json(matrix) + "\n")
    if json_output:
        print(canonical_json(matrix))
    else:
        table = Table("资产", "周期", "事件", "模型状态", "适用模型", "未通过原因")
        for row in matrix["rows"]:
            table.add_row(
                row["asset_type"],
                row["timeframe"],
                row["event_id"],
                row["model_status"],
                "是" if row["applicable_model"] else "否",
                row["reason"] or "—",
            )
        Console().print(table)
    return matrix


@guarded
def run_history(
    symbol,
    *,
    as_of=None,
    mode=None,
    config=None,
    json_output=False,
):
    settings = load_settings(Path(config) if config else None)
    mode_value = {"live": "LIVE", "frozen": "FROZEN_DATA_REPLAY"}.get(mode, mode)
    store = HistoryStore(settings.cache.directory)
    rows = store.query(
        route_symbol(symbol).canonical_symbol,
        as_of=_day(as_of),
        mode=mode_value,
    )
    records = []
    for row in rows:
        try:
            brief = store.load(row)
            primary = brief.cards[0].name if brief.cards else "无候选"
            records.append(
                {
                    "brief_digest": row["brief_digest"],
                    "market_asof": row["market_asof"],
                    "as_of": row["as_of"],
                    "mode": row["mode"],
                    "status": brief.status,
                    "primary": primary,
                    "previous_change": brief.previous.change if brief.previous else None,
                    "path": row["report_path"],
                }
            )
        except DataError as exc:
            records.append(
                {
                    "brief_digest": row["brief_digest"],
                    "market_asof": row["market_asof"],
                    "as_of": row["as_of"],
                    "mode": row["mode"],
                    "status": "UNREADABLE",
                    "primary": None,
                    "previous_change": None,
                    "path": row["report_path"],
                    "error": str(exc),
                }
            )
    if json_output:
        print(canonical_json(records))
    else:
        table = Table("行情截止", "模式", "状态", "主选", "上次变化", "摘要")
        for record in records:
            table.add_row(
                record["market_asof"],
                record["mode"],
                record["status"],
                record["primary"] or "—",
                record["previous_change"] or "—",
                record["brief_digest"][:12],
            )
        Console().print(table)
        if not records:
            Console().print("没有找到历史摘要。")
    return records


@guarded
def run_demo(
    symbol="AAPL",
    *,
    bearish=False,
    output_dir="reports/demo",
    renderer="plotly",
    format="html,md",
    json_output=False,
    config=None,
    timeframe=None,
):
    settings = load_settings(Path(config) if config else None)
    bundle = demo_bundle(
        symbol, bearish=bearish, timeframe=timeframe or settings.analysis.timeframe
    )
    request = AnalysisRequest(symbol=symbol, as_of=bundle.as_of, timeframe=bundle.timeframe)
    bundle = prepare_bundle(request, bundle)
    SnapshotStore(settings.cache.directory).save(bundle)
    result = AnalysisService(settings).analyze_bundle(request, bundle)
    output = publish_report(
        result,
        bundle,
        Path(output_dir),
        tuple(x.strip() for x in format.split(",") if x.strip() != "json"),
        renderer,
    )
    if json_output:
        print(canonical_json(result))
    else:
        render_result(result)
        Console().print(f"合成行情演示报告：{output}", markup=False)
        Console().print(f"快照：{bundle.snapshot_id}")
    return output


@guarded
def run_fetch(
    symbol,
    *,
    start=None,
    end=None,
    as_of=None,
    feed=None,
    config=None,
    refresh=False,
    long_term=False,
    large_degree=False,
    timeframe=None,
):
    settings = load_settings(Path(config) if config else None)
    if long_term:
        settings = long_term_settings(settings)
    elif large_degree:
        settings = large_degree_settings(settings)
    instrument = route_symbol(symbol)
    cutoff = parse_as_of(
        as_of, instrument, datetime.now(UTC), settings.data.delayed_access_buffer_minutes
    )
    request = AnalysisRequest(
        symbol=symbol,
        as_of=cutoff,
        start=_day(start),
        end=_day(end),
        feed=feed,
        timeframe=timeframe or settings.analysis.timeframe,
    )
    bundle = fetch_bundle(request, settings, refresh=refresh)
    print(
        canonical_json(
            {
                "snapshot_id": bundle.snapshot_id,
                "symbol": bundle.instrument.canonical_symbol,
                "asset_type": bundle.instrument.asset_type,
                "feed": bundle.provenance.feed,
                "first": bundle.bars[0].session_date,
                "last": bundle.bars[-1].session_date,
                "bars": len(bundle.bars),
                "timeframe": bundle.timeframe,
            }
        )
    )


@guarded
def run_inspect(snapshot, *, config=None):
    settings = load_settings(Path(config) if config else None)
    bundle = SnapshotStore(settings.cache.directory).load(snapshot)
    print(
        canonical_json(
            {
                "snapshot_id": bundle.snapshot_id,
                "instrument": bundle.instrument,
                "asset_type": bundle.instrument.asset_type,
                "bars": len(bundle.bars),
                "first": bundle.bars[0].session_date,
                "last": bundle.bars[-1].session_date,
                "provenance": bundle.provenance,
            }
        )
    )


@guarded
def run_replay(
    snapshot,
    *,
    start=None,
    end=None,
    step=1,
    output="reports/replay.jsonl",
    config=None,
    allow_gaps=False,
):
    settings = load_settings(Path(config) if config else None)
    bundle = SnapshotStore(settings.cache.directory).load(snapshot)
    destination = Path(output)
    # Stream into a temporary artifact; a failed replay never replaces a complete file.
    import os
    import tempfile

    destination.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=".replay-", dir=destination.parent)
    count = 0
    try:
        with os.fdopen(fd, "w") as stream:
            for result in replay(
                bundle,
                AnalysisService(settings),
                start=_day(start),
                end=_day(end),
                step=step,
                allow_gaps=allow_gaps,
            ):
                stream.write(canonical_json(result) + "\n")
                count += 1
            stream.flush()
            os.fsync(stream.fileno())
        if count == 0:
            raise InputError("No eligible sessions in the replay interval")
        os.replace(name, destination)
        import hashlib

        from eliotbot import ENGINE_VERSION

        atomic_write(
            Path(str(destination) + ".manifest.json"),
            canonical_json(
                {
                    "schema_version": "1.0",
                    "mode": "FROZEN_DATA_REPLAY",
                    "source_snapshot_id": bundle.snapshot_id,
                    "engine_version": ENGINE_VERSION,
                    "config_hash": settings.computation_hash,
                    "start": start,
                    "end": end,
                    "step": step,
                    "allow_gaps": allow_gaps,
                    "reports": count,
                    "sha256": hashlib.sha256(destination.read_bytes()).hexdigest(),
                }
            ),
        )
    finally:
        Path(name).unlink(missing_ok=True)
    print(
        canonical_json({"mode": "FROZEN_DATA_REPLAY", "reports": count, "output": str(destination)})
    )


@guarded
def run_doctor(market="US"):
    packages = {}
    for package in (
        "pydantic",
        "numpy",
        "pandas",
        "pyarrow",
        "typer",
        "plotly",
        "exchange-calendars",
        "akshare",
        "alpaca-py",
        "kaleido",
    ):
        try:
            packages[package] = version(package)
        except PackageNotFoundError:
            packages[package] = None
    try:
        alpaca_credentials()
        credentials = True
    except InputError:
        credentials = False
    print(
        json.dumps(
            {
                "eliotbot": __version__,
                "market": market,
                "packages": packages,
                "alpaca_credentials_configured": credentials,
                "network_smoke": "NOT_RUN",
                "supported_timeframes": TIMEFRAMES,
                "index_timeframes": ["1d", "1w"],
                "supported_patterns": ["IMPULSE", "ZIGZAG_ABC"],
                "bj_live": "INDEX_ONLY",
                "calibrated_probability": "UNAVAILABLE",
            },
            ensure_ascii=False,
            indent=2,
        )
    )


def _argparse_app():
    parser = argparse.ArgumentParser(prog="eliot", description="EliotBot Elliott wave analysis")
    parser.add_argument("--version", action="version", version=__version__)
    sub = parser.add_subparsers(dest="command", required=True)
    analyze = sub.add_parser("analyze")
    analyze.add_argument("symbol")
    for name in (
        "snapshot",
        "as-of",
        "start",
        "end",
        "anchor",
        "feed",
        "config",
        "output-dir",
        "format",
        "timeframe",
    ):
        analyze.add_argument(f"--{name}")
    analyze.add_argument("--top-k", type=int)
    analyze.add_argument("--renderer", choices=["plotly", "svg"], default="plotly")
    for option in ("offline", "refresh", "allow-stale", "allow-gaps", "long-term", "large-degree"):
        analyze.add_argument(f"--{option}", action="store_true")
    analyze.add_argument("--subwave-depth", type=int, choices=(0, 1, 2))
    analyze.add_argument("--open", dest="open_browser", action="store_true")
    analyze.add_argument("--json", dest="json_output", action="store_true")
    brief = sub.add_parser("brief")
    brief.add_argument("symbol")
    for name in (
        "snapshot",
        "as-of",
        "start",
        "end",
        "anchor",
        "feed",
        "config",
        "output-dir",
        "format",
        "timeframe",
    ):
        brief.add_argument(f"--{name}")
    brief.add_argument("--top-k", type=int)
    brief.add_argument("--renderer", choices=["plotly", "svg"], default="plotly")
    for option in ("offline", "refresh", "allow-stale", "allow-gaps"):
        brief.add_argument(f"--{option}", action="store_true")
    brief.add_argument("--subwave-depth", type=int, choices=(0, 1, 2))
    brief.add_argument("--model")
    brief.add_argument("--model-registry")
    brief.add_argument("--no-weekly", dest="weekly", action="store_false")
    brief.add_argument("--no-window-check", dest="window_check", action="store_false")
    brief.add_argument("--open", dest="open_browser", action="store_true")
    brief.add_argument("--json", dest="json_output", action="store_true")
    history = sub.add_parser("history")
    history.add_argument("symbol")
    history.add_argument("--as-of")
    history.add_argument("--mode", choices=["live", "frozen"])
    history.add_argument("--config")
    history.add_argument("--json", dest="json_output", action="store_true")
    watchlist = sub.add_parser("watchlist")
    watchlist.add_argument("file")
    for name in ("output-dir", "as-of", "timeframe", "config"):
        watchlist.add_argument(f"--{name}")
    watchlist.add_argument("--allow-gaps", action="store_true")
    watchlist.add_argument("--no-weekly", dest="weekly", action="store_false")
    watchlist.add_argument("--no-window-check", dest="window_check", action="store_false")
    watchlist.add_argument("--subwave-depth", type=int, choices=(0, 1, 2))
    watchlist.add_argument("--model")
    watchlist.add_argument("--model-registry")
    watchlist.add_argument("--retries", type=int, default=1)
    watchlist.add_argument("--workers", type=int, default=1)
    watchlist.add_argument("--retry-delay", type=float, default=0.5)
    watchlist.add_argument("--refresh", action="store_true")
    watchlist.add_argument("--json", dest="json_output", action="store_true")
    settle = sub.add_parser("settle")
    settle.add_argument("reports")
    settle.add_argument("--cache-root")
    settle.add_argument("--snapshot")
    settle.add_argument("--output")
    settle.add_argument("--config")
    settle.add_argument("--json", dest="json_output", action="store_true")
    matrix = sub.add_parser("support-matrix")
    matrix.add_argument("--model-summary")
    matrix.add_argument("--output")
    matrix.add_argument("--json", dest="json_output", action="store_true")
    demo = sub.add_parser("demo")
    demo.add_argument("--symbol", default="AAPL")
    demo.add_argument("--bearish", action="store_true")
    demo.add_argument("--output-dir", default="reports/demo")
    demo.add_argument("--renderer", choices=["plotly", "svg"], default="plotly")
    demo.add_argument("--format", default="html,md")
    demo.add_argument("--config")
    demo.add_argument("--timeframe")
    demo.add_argument("--json", dest="json_output", action="store_true")
    doctor = sub.add_parser("doctor")
    doctor.add_argument("--market", choices=["US", "CN"], default="US")
    replay_parser = sub.add_parser("replay")
    replay_parser.add_argument("snapshot")
    for name in ("start", "end", "config"):
        replay_parser.add_argument(f"--{name}")
    replay_parser.add_argument("--step", type=int, default=1)
    replay_parser.add_argument("--allow-gaps", action="store_true")
    replay_parser.add_argument("--output", default="reports/replay.jsonl")
    data = sub.add_parser("data").add_subparsers(dest="data_command", required=True)
    fetch = data.add_parser("fetch")
    fetch.add_argument("symbol")
    for name in ("start", "end", "as-of", "feed", "config", "timeframe"):
        fetch.add_argument(f"--{name}")
    fetch.add_argument("--refresh", action="store_true")
    inspect = data.add_parser("inspect")
    inspect.add_argument("snapshot")
    inspect.add_argument("--config")
    args = vars(parser.parse_args())
    command = args.pop("command")
    if command == "data":
        command = args.pop("data_command")
    return {
        "analyze": run_analyze,
        "brief": run_brief,
        "history": run_history,
        "watchlist": run_watchlist,
        "settle": run_settle,
        "support-matrix": run_support_matrix,
        "demo": run_demo,
        "doctor": run_doctor,
        "replay": run_replay,
        "fetch": run_fetch,
        "inspect": run_inspect,
    }[command](**args)


try:
    import typer
except ImportError:
    app = _argparse_app
else:
    app = typer.Typer(no_args_is_help=True, help="Reproducible Elliott wave analysis")
    data_app = typer.Typer(no_args_is_help=True)
    app.add_typer(data_app, name="data")

    @app.command("analyze")
    def analyze_command(
        symbol: str,
        snapshot: str | None = None,
        offline: bool = False,
        as_of: str | None = None,
        start: str | None = None,
        end: str | None = None,
        anchor: str | None = None,
        feed: str | None = None,
        top_k: int | None = None,
        config: Path | None = None,
        output_dir: Path | None = None,
        format: str | None = None,
        renderer: str = "plotly",
        json_output: bool = typer.Option(False, "--json"),
        refresh: bool = False,
        open_browser: bool = typer.Option(False, "--open"),
        allow_stale: bool = False,
        allow_gaps: bool = False,
        long_term: bool = False,
        large_degree: bool = False,
        subwave_depth: int | None = None,
        timeframe: str | None = None,
    ):
        run_analyze(**locals())

    @app.command("brief")
    def brief_command(
        symbol: str,
        snapshot: str | None = None,
        offline: bool = False,
        as_of: str | None = None,
        start: str | None = None,
        end: str | None = None,
        anchor: str | None = None,
        feed: str | None = None,
        top_k: int | None = None,
        config: Path | None = None,
        output_dir: Path | None = None,
        format: str | None = None,
        renderer: str = "plotly",
        json_output: bool = typer.Option(False, "--json"),
        refresh: bool = False,
        open_browser: bool = typer.Option(False, "--open"),
        allow_stale: bool = False,
        allow_gaps: bool = False,
        subwave_depth: int | None = None,
        model: Path | None = None,
        model_registry: Path | None = None,
        weekly: bool = True,
        window_check: bool = True,
        timeframe: str = "auto",
    ):
        run_brief(**locals())

    @app.command("watchlist")
    def watchlist_command(
        file: Path,
        output_dir: Path = Path("reports/watchlist"),
        as_of: str | None = None,
        timeframe: str = "auto",
        config: Path | None = None,
        json_output: bool = typer.Option(False, "--json"),
        allow_gaps: bool = False,
        weekly: bool = True,
        window_check: bool = True,
        subwave_depth: int | None = None,
        model: Path | None = None,
        model_registry: Path | None = None,
        retries: int = 1,
        workers: int = 1,
        retry_delay: float = 0.5,
        refresh: bool = False,
    ):
        run_watchlist(**locals())

    @app.command("settle")
    def settle_command(
        reports: Path,
        cache_root: Path | None = None,
        snapshot: str | None = None,
        output: Path | None = None,
        config: Path | None = None,
        json_output: bool = typer.Option(False, "--json"),
    ):
        run_settle(**locals())

    @app.command("support-matrix")
    def support_matrix_command(
        model_summary: Path | None = None,
        output: Path | None = None,
        json_output: bool = typer.Option(False, "--json"),
    ):
        run_support_matrix(**locals())

    @app.command("history")
    def history_command(
        symbol: str,
        as_of: str | None = None,
        mode: str | None = None,
        config: Path | None = None,
        json_output: bool = typer.Option(False, "--json"),
    ):
        run_history(**locals())

    @app.command("demo")
    def demo_command(
        symbol: str = "AAPL",
        bearish: bool = False,
        output_dir: Path = Path("reports/demo"),
        renderer: str = "plotly",
        format: str = "html,md",
        json_output: bool = typer.Option(False, "--json"),
        config: Path | None = None,
        timeframe: str | None = None,
    ):
        run_demo(**locals())

    @app.command("doctor")
    def doctor_command(market: str = "US"):
        run_doctor(market)

    @app.command("replay")
    def replay_command(
        snapshot: str,
        start: str | None = None,
        end: str | None = None,
        step: int = 1,
        output: Path = Path("reports/replay.jsonl"),
        config: Path | None = None,
        allow_gaps: bool = False,
    ):
        run_replay(**locals())

    @data_app.command("fetch")
    def fetch_command(
        symbol: str,
        start: str | None = None,
        end: str | None = None,
        as_of: str | None = None,
        feed: str | None = None,
        config: Path | None = None,
        refresh: bool = False,
        long_term: bool = False,
        large_degree: bool = False,
        timeframe: str | None = None,
    ):
        run_fetch(**locals())

    @data_app.command("inspect")
    def inspect_command(snapshot: str, config: Path | None = None):
        run_inspect(**locals())


if __name__ == "__main__":
    app()
