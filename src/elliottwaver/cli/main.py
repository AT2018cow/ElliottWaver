"""CLI entrypoints for daily analysis and research."""

import json
import time
import webbrowser
from concurrent.futures import ThreadPoolExecutor
from copy import copy
from dataclasses import replace
from datetime import UTC, date, datetime
from functools import wraps
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Annotated

import typer
from pydantic import ValidationError
from rich.console import Console
from rich.style import Style
from rich.table import Table
from rich.text import Text
from typer.core import TyperCommand, TyperGroup

from elliottwaver import __version__
from elliottwaver.brief import attach_previous, build_brief
from elliottwaver.cli.console_render import render_brief, render_brief_summary, render_result
from elliottwaver.cli.daily import (
    DAILY_HELP,
    daily_config_path,
    daily_instrument,
    init_index,
    intraday_availability,
    with_index_feed_defaults,
)
from elliottwaver.cli.index_context import attach_index_context, resolve_index_context
from elliottwaver.config import (
    Settings,
    alpaca_credentials,
    large_degree_settings,
    load_settings,
    long_term_settings,
    massive_api_key,
    tushare_token,
)
from elliottwaver.data.calendar import parse_as_of
from elliottwaver.data.demo import demo_bundle
from elliottwaver.data.quality import missing_intervals
from elliottwaver.data.router import route_symbol
from elliottwaver.data.snapshots import SnapshotStore
from elliottwaver.data.workflow import fetch_bundle
from elliottwaver.engine.templates import TEMPLATES
from elliottwaver.errors import DataError, ElliottWaverError, InputError, SourceError
from elliottwaver.evaluation.index_plans import (
    summarize_developing_evidence,
    summarize_index_plans,
    summarize_plan_stability,
)
from elliottwaver.evaluation.replay import replay
from elliottwaver.forecast import ModelRegistry, load_model
from elliottwaver.history import HistoryStore, base_lineage_key
from elliottwaver.schemas.market import AnalysisRequest
from elliottwaver.service import AnalysisService, engine_rules_version, prepare_bundle
from elliottwaver.settlement import SettlementStore, support_matrix
from elliottwaver.timeframes import TIMEFRAMES, Timeframe, normalize_timeframe
from elliottwaver.util import atomic_write, canonical_json, digest
from elliottwaver.visualization.brief_formatter import (
    BRIEF_FORMATS,
    PublicationWindowElapsed,
    publish_brief,
    read_brief_report,
    read_issuance,
)
from elliottwaver.visualization.brief_text import (
    market_time_text,
    mode_text,
    previous_change_text,
    status_text,
    timeframe_text,
)
from elliottwaver.visualization.report_formatter import publish_report
from elliottwaver.visualization.report_index import report_entry, write_report_index


def guarded(function):
    @wraps(function)
    def wrapped(*args, **kwargs):
        try:
            return function(*args, **kwargs)
        except ElliottWaverError as exc:
            Console(stderr=True).print(f"{exc.code}: {exc}", markup=False)
            raise SystemExit(exc.exit_code) from exc
        except (ValidationError, ValueError) as exc:
            Console(stderr=True).print(f"INVALID_INPUT: {exc}", markup=False)
            raise SystemExit(2) from exc

    return wrapped


def _day(value):
    return date.fromisoformat(value) if value else None


def _anchor(value):
    if not value:
        return None, None
    text = str(value).strip()
    if len(text) == 10:
        return date.fromisoformat(text), None
    moment = datetime.fromisoformat(text.replace("Z", "+00:00"))
    if moment.tzinfo is None:
        raise ValueError("anchor timestamp must include a timezone")
    moment = moment.astimezone(UTC)
    return moment.date(), moment


def _refresh_report_entry(output: Path) -> None:
    if output.parent.parent.name != "archive":
        return
    try:
        write_report_index(output.parents[2])
    except OSError as exc:
        Console(stderr=True).print(f"报告已保存，入口页更新失败 · {exc}", markup=False)


def _show_report_entry(console: Console, output: Path) -> None:
    entry = report_entry(output)
    label = "报告入口" if entry != output else "报告目录"
    console.print(
        Text(f"{label} · {entry}", style=Style(link=entry.resolve().as_uri())),
        soft_wrap=True,
    )


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
    settings = load_settings(daily_config_path(config))
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
    anchor_day, anchor_at = _anchor(anchor)
    request = AnalysisRequest(
        symbol=symbol,
        as_of=cutoff,
        start=_day(start),
        end=_day(end),
        anchor=anchor_day,
        anchor_at=anchor_at,
        feed=feed,
        top_k=top_k,
        allow_stale=allow_stale,
        allow_gaps=allow_gaps,
        mode="FROZEN_DATA_REPLAY" if snapshot or as_of or end else "LIVE",
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
    _refresh_report_entry(output)
    if json_output:
        print(canonical_json(result))
    else:
        render_result(result)
        _show_report_entry(Console(), output)
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
    if source is not None and source.timeframe.endswith("m"):
        return source.timeframe, "快照本身为分钟线"
    if start:
        start_day = _day(start)
        if start_day and (cutoff.date() - start_day).days > 5 * 365:
            return "1w", "请求跨度超过 5 年，自动使用周线"
    return "1d", "默认日线；长历史可用 --timeframe 1w"


def _brief_formats(format: str | None, settings: Settings) -> tuple[str, ...]:
    if format is None:
        return tuple(item for item in settings.output.formats if item in BRIEF_FORMATS)
    return tuple(item for value in format.split(",") if (item := value.strip()) not in {"", "json"})


def _run_brief_core(
    symbol,
    *,
    snapshot=None,
    reference_snapshot=None,
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
    refresh=False,
    allow_stale=False,
    allow_gaps=False,
    subwave_depth=None,
    weekly=True,
    window_check=True,
    model=None,
    model_registry=None,
    timeframe="auto",
    settings: Settings | None = None,
):
    if settings is None:
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
    anchor_day, anchor_at = _anchor(anchor)
    request = AnalysisRequest(
        symbol=symbol,
        as_of=cutoff,
        start=_day(start),
        end=_day(end),
        anchor=anchor_day,
        anchor_at=anchor_at,
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
    models_data: tuple[dict, ...] = ()
    approved_at = None
    model_issue = None
    filters = dict(
        engine_version=engine_rules_version(settings),
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
            elif model_data is not None:
                models_data = (model_data,)
        else:
            registry_path = (
                Path(model_registry)
                if model_registry
                else Path(settings.cache.directory) / "models"
            )
            active_models = ModelRegistry(registry_path).active_models_at(model_cutoff, **filters)
            if active_models:
                models_data = tuple(item["model"] for item in active_models)
                approved_at = max(
                    datetime.fromisoformat(item["approved_at"]) for item in active_models
                )
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        model_data = None
        models_data = ()
        model_issue = "无法读取符合发布要求的概率模型"
    brief_options = dict(
        timeframe_reason=reason,
        include_weekly=weekly,
        check_windows=window_check,
        model=model_data,
        models=models_data,
        model_approved_at=approved_at,
        retrospective=bool(model and model_data),
        model_issue=model_issue,
    )
    run = build_brief(request, source, settings, **brief_options)
    history = HistoryStore(settings.cache.directory)
    formats = _brief_formats(format, settings)
    output_root = Path(output_dir) if output_dir else settings.output.directory
    context = resolve_index_context(
        run,
        request,
        settings,
        history,
        store,
        reference_snapshot=reference_snapshot,
    )
    request_key = (
        digest(
            [
                "live-brief-v1",
                request,
                source.snapshot_id,
                engine_rules_version(settings),
                settings.computation_hash,
                settings.index_guidance,
                brief_options,
                context.identity if context is not None else None,
                output_root.resolve(),
                sorted(set(formats)),
                renderer,
            ]
        )
        if request.mode == "LIVE"
        else None
    )
    with store.lock(digest(["brief-lineage", base_lineage_key(run.brief), run.brief.mode])):
        saved = history.lookup_issuance(request_key) if request_key else None
        if saved is not None:
            issued_path, issued_digest = saved
            issued, result = read_brief_report(issued_path, expected_digest=issued_digest)
            _refresh_report_entry(issued_path)
            return replace(run, brief=issued, result=result), issued_path
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
        for attempt in range(2):
            run = attach_previous(
                run,
                previous,
                previous_row["report_path"] if previous_row is not None else None,
                previous_source=previous_source,
                current_source=run.source,
                previous_result=previous_result,
            )
            run = attach_index_context(run, context, settings)
            store.save(run.source)
            store.save(run.prepared)
            if run.weekly_prepared is not None:
                store.save(run.weekly_prepared)
            try:
                output = publish_brief(
                    run,
                    output_root,
                    formats,
                    renderer,
                )
            except PublicationWindowElapsed:
                if attempt:
                    raise
                run = build_brief(request, source, settings, **brief_options)
            else:
                break
        if run.brief.mode == "LIVE":
            published, _ = read_brief_report(output, expected_digest=run.brief.brief_digest)
            run = replace(run, brief=published)
        history.register(run.brief, output, request_key=request_key)
    _refresh_report_entry(output)
    return run, output


def _brief_exit_code(status: str, *, allow_stale: bool = False) -> int:
    return 3 if status == "INSUFFICIENT_DATA" or (status == "STALE_DATA" and not allow_stale) else 0


@guarded
def run_automatic(symbol: str, *, config: Path | None = None) -> None:
    instrument = route_symbol(symbol)
    settings = with_index_feed_defaults(load_settings(daily_config_path(config)), instrument)
    console = Console()
    report = None

    def show_period(timeframe: Timeframe) -> int:
        nonlocal report
        try:
            run, output = _run_brief_core(
                instrument.canonical_symbol, timeframe=timeframe, settings=settings
            )
        except (DataError, SourceError) as exc:
            console.print(f"{timeframe_text(timeframe)}暂不可用 · {exc}", markup=False)
            return exc.exit_code
        render_brief_summary(
            run.brief, is_index=instrument.asset_type == "INDEX", automatic=True, console=console
        )
        report = output
        return _brief_exit_code(run.brief.status)

    daily_exit = show_period("1d")
    console.print()
    try:
        try:
            intraday = intraday_availability(
                instrument, datetime.now(UTC), settings.data.delayed_access_buffer_minutes
            )
        except DataError as exc:
            console.print(f"无法确定交易时段 · {exc}", markup=False)
            raise SystemExit(daily_exit or exc.exit_code) from exc
        if intraday == "AVAILABLE":
            show_period("15m")
        elif intraday == "AWAITING_BAR":
            console.print("盘中行情待更新 · 今天首根完整 15 分钟 K 线尚未到可用时间。")
        else:
            console.print("当前为非交易时段。")
    finally:
        if report is not None:
            _show_report_entry(console, report)
    if daily_exit:
        raise SystemExit(daily_exit)


@guarded
def run_brief(
    symbol=None,
    *,
    snapshot=None,
    reference_snapshot=None,
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
    details=False,
):
    settings = load_settings(daily_config_path(config))
    instrument = daily_instrument(symbol, settings.default_index)
    settings = with_index_feed_defaults(settings, instrument)
    formats = _brief_formats(format, settings)
    if open_browser and format is None and "html" not in formats:
        formats = (*formats, "html")
        format = ",".join(formats)
    run, output = _run_brief_core(
        symbol if symbol is not None else instrument.canonical_symbol,
        snapshot=snapshot,
        reference_snapshot=reference_snapshot,
        offline=offline,
        as_of=as_of,
        start=start,
        end=end,
        anchor=anchor,
        feed=feed,
        top_k=top_k,
        settings=settings,
        output_dir=output_dir,
        format=format,
        renderer=renderer,
        refresh=refresh,
        allow_stale=allow_stale,
        allow_gaps=allow_gaps,
        subwave_depth=subwave_depth,
        weekly=weekly,
        window_check=window_check,
        model=model,
        model_registry=model_registry,
        timeframe=timeframe,
    )
    if json_output:
        print(canonical_json(run.brief))
    else:
        if (
            instrument.asset_type == "INDEX"
            and run.brief.timeframe in {"1d", "15m"}
            and not details
        ):
            render_brief_summary(run.brief, is_index=True, config=config)
        else:
            render_brief(run.brief, run.result)
        _show_report_entry(Console(), output)
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
                "feed": None,
                "adjustment": None,
                "timeframe": None,
                "mode": None,
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
                    "feed": run.brief.feed,
                    "adjustment": run.brief.adjustment,
                    "timeframe": run.brief.timeframe,
                    "mode": run.brief.mode,
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
                    "feed": None,
                    "adjustment": None,
                    "timeframe": None,
                    "mode": None,
                    "path": None,
                    "error": "数据不足或陈旧",
                    "attempts": attempts,
                    "exit_code": int(exc.code or 3),
                }
            except ElliottWaverError as exc:
                last_error = {
                    "symbol": symbol,
                    "status": "ERROR",
                    "brief_digest": None,
                    "market_asof": None,
                    "feed": None,
                    "adjustment": None,
                    "timeframe": None,
                    "mode": None,
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
                    "feed": None,
                    "adjustment": None,
                    "timeframe": None,
                    "mode": None,
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
        table = Table("标的", "状态", "来源", "行情截止", "摘要", "尝试", "错误")
        for record in records:
            table.add_row(
                record["symbol"],
                record["status"],
                record.get("feed") or "—",
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
    plan_rows = {}
    issuance_keys = []
    guidance_runs = []
    guidance_receipts = {}
    cancellations = {}
    seen = set()
    exit_code = 0
    settlement_source = None
    for brief_path in paths:
        try:
            brief, _ = read_brief_report(brief_path.parent)
            if brief.brief_digest in seen:
                continue
            seen.add(brief.brief_digest)
            if snapshot and settlement_source is None:
                settlement_source = snapshots.load(snapshot)
            source = settlement_source or snapshots.load(brief.source_snapshot_id)
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
            if brief.index_guidance is not None:
                if brief.mode == "LIVE" and source.snapshot_id == brief.source_snapshot_id:
                    if visible := read_issuance(brief_path.parent, brief):
                        guidance_receipts[brief.brief_digest] = max(source.as_of, visible)
                guidance_runs.append((brief, source))
                for track in brief.index_guidance.tracks:
                    for event in track.transitions:
                        if event.reason in {"STRUCTURE_REPLACED", "POLICY_CHANGED"}:
                            cancellations[(event.plan_id, event.revision)] = event
                    group = (
                        brief.symbol,
                        brief.mode,
                        brief.feed,
                        brief.adjustment,
                        brief.index_guidance.policy_hash,
                        brief.config_hash,
                        brief.engine_version,
                        brief.price_coordinate_id,
                        track.horizon,
                        track.holding_sessions,
                    )
                    issuance_keys.append((group, brief.last_bar_end_at))
        except (ElliottWaverError, OSError, ValueError) as exc:
            exit_code = exit_code or (exc.exit_code if isinstance(exc, ElliottWaverError) else 3)
            records.append(
                {
                    "brief_path": str(brief_path),
                    "error": f"{type(exc).__name__}: {exc}",
                }
            )
    result = {"schema_version": "1.0", "records": records}
    if guidance_runs:
        for brief, source in guidance_runs:
            for row in store.settle_index_guidance(
                brief,
                source,
                cancellations=cancellations,
                receipt_cutoff=guidance_receipts.get(brief.brief_digest),
            ):
                key = (row["plan_id"], row["revision"])
                previous = plan_rows.get(key)
                rank = (brief.as_of, source.as_of, brief.brief_digest)
                if previous is None or rank >= previous[0]:
                    plan_rows[key] = (rank, row)
        plans = [row for _, row in plan_rows.values()]
        result.update(
            index_plans=plans,
            index_plan_statistics=summarize_index_plans(plans, issuance_keys),
            index_confirmation_research=summarize_developing_evidence(guidance_runs),
            index_plan_stability=summarize_plan_stability(guidance_runs),
        )
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
        if guidance_runs:
            alerts = Table("指数预案", "期限", "状态", "最后观测会话")
            from elliottwaver.visualization.brief_text import PLAN_STATE_TEXT

            for row in plans:
                obs = row["observation"]
                alerts.add_row(
                    row["plan_id"],
                    "短线" if row["horizon"] == "SHORT" else "波段",
                    PLAN_STATE_TEXT[obs["state"]] if obs["quality"] == "OK" else "数据待核",
                    obs["through_session"] or "—",
                )
            Console().print(alerts)
    if exit_code:
        raise SystemExit(exit_code)
    return result


@guarded
def run_support_matrix(
    *,
    model_summary=None,
    model_registry=None,
    brief_report=None,
    test_receipts=None,
    at=None,
    output=None,
    json_output=False,
):
    summary = json.loads(Path(model_summary).read_text()) if model_summary else None
    when = None
    if at is not None:
        try:
            when = datetime.fromisoformat(at)
            if when.utcoffset() is None:
                raise ValueError
        except ValueError as exc:
            raise InputError("--at requires an ISO 8601 timestamp with timezone") from exc
    brief, _ = read_brief_report(Path(brief_report)) if brief_report else (None, None)
    matrix = support_matrix(
        summary,
        registry=ModelRegistry(Path(model_registry)) if model_registry else None,
        brief=brief,
        at=when,
        test_receipts=Path(test_receipts) if test_receipts else None,
    )
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
    symbol=None,
    *,
    as_of=None,
    mode=None,
    timeframe=None,
    config=None,
    acknowledge=None,
    json_output=False,
):
    settings = load_settings(daily_config_path(config))
    instrument = daily_instrument(symbol, settings.default_index)
    daily_history = symbol is None and mode is None
    lineage = False if daily_history else None
    mode_value = {"live": "LIVE", "frozen": "FROZEN_DATA_REPLAY"}.get(mode, mode)
    if daily_history:
        mode_value = "LIVE"
    timeframe_value = normalize_timeframe(timeframe) if timeframe else None
    as_of_day = _day(as_of)
    store = HistoryStore(settings.cache.directory)
    canonical = instrument.canonical_symbol
    if acknowledge is not None and not store.acknowledge_reminder(
        canonical, acknowledge, mode=mode_value
    ):
        raise InputError("No matching single-index reminder")
    rows = store.query(
        canonical,
        as_of=as_of_day,
        mode=mode_value,
        timeframe=timeframe_value,
        retrospective=lineage,
    )
    records = []
    for row in rows:
        try:
            brief = store.load(row)
            primary = brief.cards[0].name if brief.cards else "无候选"
            records.append(
                {
                    "brief_digest": row["brief_digest"],
                    "timeframe": brief.timeframe,
                    "market_asof": row["market_asof"],
                    "as_of": row["as_of"],
                    "last_bar_end_at": brief.last_bar_end_at.isoformat()
                    if brief.last_bar_end_at
                    else None,
                    "last_bar_local": market_time_text(brief.last_bar_end_at, brief.symbol)
                    if brief.last_bar_end_at
                    else None,
                    "last_bar_id": brief.last_bar_id,
                    "mode": row["mode"],
                    "status": brief.status,
                    "primary": primary,
                    "previous_change": brief.previous.change if brief.previous else None,
                    "path": row["report_path"],
                    **(
                        {
                            "index_context": brief.index_context,
                            "intraday_alerts": store.intraday_alerts(
                                canonical, brief_digest=brief.brief_digest
                            ),
                        }
                        if brief.index_context is not None
                        else {}
                    ),
                    **(
                        {
                            "index_guidance": brief.index_guidance,
                            "reminders": store.reminders(
                                canonical, brief_digest=brief.brief_digest
                            ),
                        }
                        if brief.index_guidance is not None
                        else {"index_guidance_state": "NOT_RECORDED"}
                        if instrument.asset_type == "INDEX"
                        else {}
                    ),
                }
            )
        except DataError as exc:
            records.append(
                {
                    "brief_digest": row["brief_digest"],
                    "timeframe": row.get("timeframe"),
                    "market_asof": row["market_asof"],
                    "as_of": row["as_of"],
                    "last_bar_end_at": row.get("last_bar_end_at"),
                    "last_bar_local": None,
                    "last_bar_id": row.get("last_bar_id"),
                    "mode": row["mode"],
                    "status": "UNREADABLE",
                    "primary": None,
                    "previous_change": None,
                    "path": row["report_path"],
                    "error": str(exc),
                }
            )
    pending = (
        store.pending_intraday_alerts(
            canonical, mode=mode_value, as_of=as_of_day, retrospective=lineage
        )
        if timeframe_value in {None, "15m"} and instrument.asset_type == "INDEX"
        else []
    )
    if json_output:
        if pending:
            if records:
                records[0]["pending_intraday_alerts"] = pending
            else:
                records.append({"pending_intraday_alerts": pending})
        print(canonical_json(records))
    else:
        console = Console()
        if daily_history:
            console.print(f"{canonical} · 日常分析历史", markup=False)
        table = Table("周期", "行情截止", "模式", "状态", "主选", "与上次相比", "报告")
        for record in records:
            directory = Path(record["path"]).resolve()
            report = directory / "index.html"
            if not report.is_file():
                report = directory
            table.add_row(
                timeframe_text(record["timeframe"]) if record["timeframe"] else "—",
                record["last_bar_local"] or record["market_asof"],
                mode_text(record["mode"]),
                "报告不可读" if record["status"] == "UNREADABLE" else status_text(record["status"]),
                record["primary"] or "—",
                previous_change_text(record["previous_change"])
                if record["previous_change"]
                else "—",
                Text("打开", style=Style(link=report.as_uri())),
            )
        if records:
            console.print(table)
            _show_report_entry(console, Path(records[0]["path"]))
        reminders = (
            store.reminders(canonical, mode=mode_value, as_of=as_of_day, retrospective=lineage)
            if timeframe_value in {None, "1d"}
            else ()
        )
        if reminders:
            from elliottwaver.visualization.brief_text import PLAN_REASON_TEXT

            alerts = Table("本地提醒", "周期", "条件变化", "首次可见", "已查看")
            for item in reminders:
                alerts.add_row(
                    item["transition_id"],
                    "短线" if item["horizon"] == "SHORT" else "波段",
                    PLAN_REASON_TEXT.get(item["reason"], item["reason"]),
                    market_time_text(datetime.fromisoformat(item["first_seen_at"]), canonical),
                    item["acknowledged_at"] or "—",
                )
            console.print(alerts)
        if pending:
            pending_table = Table("未查看的盘中提醒", "周期", "触发可见时刻")
            for alert in pending:
                pending_table.add_row(
                    alert["transition_id"],
                    "短线" if alert["horizon"] == "SHORT" else "波段",
                    market_time_text(datetime.fromisoformat(alert["observable_at"]), canonical),
                )
            console.print(pending_table)
        if not records:
            message = "没有找到历史摘要。"
            if daily_history:
                message += " 可运行 elliottwaver brief 生成收盘判断。"
            console.print(message)
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
    _refresh_report_entry(output)
    if json_output:
        print(canonical_json(result))
    else:
        render_result(result)
        _show_report_entry(Console(), output)
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
    settings = load_settings(daily_config_path(config))
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
        mode="FROZEN_DATA_REPLAY" if as_of else "LIVE",
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
                "adjustment": bundle.provenance.adjustment,
                "market_asof": bundle.bars[-1].session_date,
                "as_of": bundle.as_of.isoformat(),
                "last_available_at": bundle.bars[-1].usable_at.isoformat(),
                "mode": request.mode,
                "first": bundle.bars[0].session_date,
                "last": bundle.bars[-1].session_date,
                "first_bar_start_at": bundle.bars[0].start_at.isoformat(),
                "last_bar_start_at": bundle.bars[-1].start_at.isoformat(),
                "last_bar_end_at": bundle.bars[-1].end_at.isoformat(),
                "coverage_start": bundle.provenance.coverage_start.isoformat()
                if bundle.provenance.coverage_start
                else None,
                "coverage_end": bundle.provenance.coverage_end.isoformat()
                if bundle.provenance.coverage_end
                else None,
                "coverage_reason": bundle.provenance.coverage_reason or None,
                "session_profile": bundle.provenance.session_profile,
                "source_timeframe": bundle.provenance.source_timeframe or None,
                "aggregation": bundle.provenance.aggregation or None,
                "price_coordinate_id": bundle.provenance.price_coordinate_id,
                "unknown_gap_count": len(missing_intervals(bundle)),
                "bars": len(bundle.bars),
                "timeframe": bundle.timeframe,
            }
        )
    )


@guarded
def run_inspect(snapshot, *, config=None):
    settings = load_settings(daily_config_path(config))
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
                "first_bar_start_at": bundle.bars[0].start_at.isoformat(),
                "last_bar_start_at": bundle.bars[-1].start_at.isoformat(),
                "last_bar_end_at": bundle.bars[-1].end_at.isoformat(),
                "last_available_at": bundle.bars[-1].usable_at.isoformat(),
                "unknown_gap_count": len(missing_intervals(bundle)),
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

        atomic_write(
            Path(str(destination) + ".manifest.json"),
            canonical_json(
                {
                    "schema_version": "1.0",
                    "mode": "FROZEN_DATA_REPLAY",
                    "source_snapshot_id": bundle.snapshot_id,
                    "engine_version": engine_rules_version(settings),
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
def run_doctor(market=None, *, config=None):
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
    try:
        tushare_token()
        tushare_credentials = True
    except InputError:
        tushare_credentials = False
    try:
        massive_api_key()
        massive_credentials = True
    except InputError:
        massive_credentials = False
    config_path = daily_config_path(config)
    settings = load_settings(config_path)
    if market is None:
        market = (
            daily_instrument(None, settings.default_index).market
            if settings.default_index
            else "US"
        )
    market = market.upper()
    if market not in {"CN", "US"}:
        raise InputError("市场应为 CN 或 US。")
    index_feed = (
        settings.data.cn_index_intraday_feed if market == "CN" else settings.data.us_index_feed
    )
    print(
        json.dumps(
            {
                "elliottwaver": __version__,
                "market": market,
                "default_index": settings.default_index,
                "config": str(config_path) if config_path else None,
                "packages": packages,
                "alpaca_credentials_configured": credentials,
                "tushare_token_configured": tushare_credentials,
                "massive_api_key_configured": massive_credentials,
                "cn_equity_intraday_feed": settings.data.cn_equity_intraday_feed,
                "cn_index_intraday_feed": settings.data.cn_index_intraday_feed or None,
                "us_index_feed": settings.data.us_index_feed or None,
                "network_smoke": "NOT_RUN",
                "supported_timeframes": TIMEFRAMES,
                "index_timeframes": list(TIMEFRAMES) if index_feed else ["1d", "1w"],
                "supported_patterns": [template.name for template in TEMPLATES],
                "bj_live": "INDEX_ONLY",
                "calibrated_probability": "UNAVAILABLE",
            },
            ensure_ascii=False,
            indent=2,
        )
    )


DAILY_COMMANDS = "日常分析"
ANALYSIS_COMMANDS = "深入分析与本地数据"
RESEARCH_COMMANDS = "批量与研究"
ADVANCED_OPTIONS = "高级选项"


class SymbolCommandGroup(TyperGroup):
    def resolve_command(self, ctx, args):
        token = args[0]
        if not token.startswith("-") and self.get_command(ctx, token) is None:
            return "", self.get_command(ctx, "scan"), args
        return super().resolve_command(ctx, args)


class BriefCommand(TyperCommand):
    def format_help(self, ctx, formatter):
        if ctx.meta.get("brief_help_all"):
            return super().format_help(ctx, formatter)
        summary = copy(self)
        summary.params = [
            param
            for param in self.params
            if getattr(param, "rich_help_panel", None) != ADVANCED_OPTIONS
        ]
        return TyperCommand.format_help(summary, ctx, formatter)


def _show_all_brief_options(ctx: typer.Context, value: bool) -> None:
    if value and not ctx.resilient_parsing:
        ctx.meta["brief_help_all"] = True
        typer.echo(ctx.get_help())
        raise typer.Exit()


app = typer.Typer(
    cls=SymbolCommandGroup,
    no_args_is_help=True,
    help="ElliottWaver 波浪分析。\n\n" + DAILY_HELP,
    subcommand_metavar="代码 | 命令",
)
data_app = typer.Typer(no_args_is_help=True)
app.add_typer(data_app, name="data", help="获取或查看行情快照。", rich_help_panel=ANALYSIS_COMMANDS)


def _show_version(value: bool) -> None:
    if value:
        typer.echo(__version__)
        raise typer.Exit()


@app.callback()
def global_options(
    show_version: bool = typer.Option(
        False,
        "--version",
        callback=_show_version,
        is_eager=True,
        help="显示程序版本。",
    ),
) -> None:
    """处理全局选项。"""


@app.command("scan", hidden=True, help="按市场时段自动分析指定股票或指数。")
def scan_command(
    symbol: str,
    config: Annotated[Path | None, typer.Option(help="使用指定配置文件。")] = None,
):
    run_automatic(symbol, config=config)


@app.command("init", help="为单项命令设置可选的默认指数。", rich_help_panel=DAILY_COMMANDS)
@guarded
def init_command(
    symbol: str,
    config: Annotated[Path | None, typer.Option(help="沿用指定文件的配置。")] = None,
    yes: bool = typer.Option(False, "--yes", help="保存设置，跳过交互确认。"),
):
    init_index(symbol, config=config, yes=yes)


@app.command("analyze", help="查看单周期的完整数浪与规则。", rich_help_panel=ANALYSIS_COMMANDS)
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


@app.command(
    "brief",
    cls=BriefCommand,
    help="查看收盘波浪判断、短线与波段预案。\n\n"
    "直接运行 elliottwaver brief，或附上指数代码临时查看。"
    "首次可用 elliottwaver init 000688.SH 设置默认指数。",
    rich_help_panel=DAILY_COMMANDS,
)
def brief_command(
    symbol: str | None = typer.Argument(None, help="临时指定标的。省略时使用默认指数。"),
    details: bool = typer.Option(False, "--details", help="查看完整终端简报和规则复核。"),
    open_browser: bool = typer.Option(False, "--open", help="用浏览器打开 HTML 图表。"),
    refresh: bool = typer.Option(False, "--refresh", help="重新向行情源请求数据。"),
    json_output: bool = typer.Option(False, "--json", help="在终端输出结构化 JSON。"),
    format: str | None = typer.Option(None, help="选择保存格式。json 表示只保存 JSON 文件。"),
    help_all: bool = typer.Option(
        False,
        "--help-all",
        is_eager=True,
        callback=_show_all_brief_options,
        help="显示快照、日期、周期和模型等高级选项。",
    ),
    snapshot: str | None = typer.Option(None, rich_help_panel=ADVANCED_OPTIONS),
    reference_snapshot: str | None = typer.Option(None, rich_help_panel=ADVANCED_OPTIONS),
    offline: bool = typer.Option(False, rich_help_panel=ADVANCED_OPTIONS),
    as_of: str | None = typer.Option(None, rich_help_panel=ADVANCED_OPTIONS),
    start: str | None = typer.Option(None, rich_help_panel=ADVANCED_OPTIONS),
    end: str | None = typer.Option(None, rich_help_panel=ADVANCED_OPTIONS),
    anchor: str | None = typer.Option(None, rich_help_panel=ADVANCED_OPTIONS),
    feed: str | None = typer.Option(None, rich_help_panel=ADVANCED_OPTIONS),
    top_k: int | None = typer.Option(None, rich_help_panel=ADVANCED_OPTIONS),
    config: Annotated[Path | None, typer.Option(rich_help_panel=ADVANCED_OPTIONS)] = None,
    output_dir: Annotated[Path | None, typer.Option(rich_help_panel=ADVANCED_OPTIONS)] = None,
    renderer: str = typer.Option("plotly", rich_help_panel=ADVANCED_OPTIONS),
    allow_stale: bool = typer.Option(False, rich_help_panel=ADVANCED_OPTIONS),
    allow_gaps: bool = typer.Option(False, rich_help_panel=ADVANCED_OPTIONS),
    subwave_depth: int | None = typer.Option(None, rich_help_panel=ADVANCED_OPTIONS),
    model: Annotated[Path | None, typer.Option(rich_help_panel=ADVANCED_OPTIONS)] = None,
    model_registry: Annotated[Path | None, typer.Option(rich_help_panel=ADVANCED_OPTIONS)] = None,
    weekly: bool = typer.Option(True, rich_help_panel=ADVANCED_OPTIONS),
    window_check: bool = typer.Option(True, rich_help_panel=ADVANCED_OPTIONS),
    timeframe: str = typer.Option("auto", rich_help_panel=ADVANCED_OPTIONS),
):
    del help_all
    run_brief(**locals())


@app.command(
    "intraday",
    help="查看 15 分钟波浪判断与 60 分钟背景，指数另核对日线预案。",
    rich_help_panel=DAILY_COMMANDS,
)
def intraday_command(
    symbol: str | None = typer.Argument(None, help="指定股票或指数。省略时使用默认指数。"),
    details: bool = typer.Option(False, "--details", help="查看完整终端简报。"),
    open_browser: bool = typer.Option(False, "--open", help="用浏览器打开 HTML 图表。"),
    refresh: bool = typer.Option(False, "--refresh", help="重新向行情源请求数据。"),
    json_output: bool = typer.Option(False, "--json", help="在终端输出结构化 JSON。"),
    config: Annotated[Path | None, typer.Option(help="使用指定配置文件。")] = None,
):
    run_brief(**locals(), timeframe="15m")


@app.command("watchlist", help="为一组标的生成摘要。", rich_help_panel=RESEARCH_COMMANDS)
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


@app.command("settle", help="用行情快照核对已发行预案。", rich_help_panel=RESEARCH_COMMANDS)
def settle_command(
    reports: Path,
    cache_root: Path | None = None,
    snapshot: str | None = None,
    output: Path | None = None,
    config: Path | None = None,
    json_output: bool = typer.Option(False, "--json"),
):
    run_settle(**locals())


@app.command("support-matrix", help="查看研究与模型支持状态。", rich_help_panel=RESEARCH_COMMANDS)
def support_matrix_command(
    model_summary: Path | None = None,
    model_registry: Path | None = None,
    brief_report: Path | None = None,
    test_receipts: Path | None = None,
    at: str | None = None,
    output: Path | None = None,
    json_output: bool = typer.Option(False, "--json"),
):
    run_support_matrix(**locals())


@app.command("history", help="查询最近判断和预案变化。", rich_help_panel=DAILY_COMMANDS)
def history_command(
    symbol: str | None = typer.Argument(None, help="临时指定标的。省略时查看默认指数。"),
    as_of: str | None = typer.Option(None, help="只查看此日期及以前的行情判断。"),
    mode: str | None = typer.Option(None, help="按 live 或 frozen 筛选。"),
    timeframe: str | None = typer.Option(None, help="只查看指定周期。"),
    config: Annotated[Path | None, typer.Option(help="使用指定配置文件。")] = None,
    acknowledge: str | None = typer.Option(None, help="把指定变化记录标记为已查看。"),
    json_output: bool = typer.Option(False, "--json", help="输出 JSON，含各条报告的完整路径。"),
):
    run_history(**locals())


@app.command("demo", help="用合成行情体验分析。", rich_help_panel=ANALYSIS_COMMANDS)
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


@app.command("doctor", help="检查本地依赖和配置。", rich_help_panel=ANALYSIS_COMMANDS)
def doctor_command(
    market: str | None = typer.Option(None, help="指定 CN 或 US。默认检查已设指数所属市场。"),
    config: Annotated[Path | None, typer.Option(help="使用指定配置文件。")] = None,
):
    run_doctor(market, config=config)


@app.command("replay", help="用已保存的行情逐步回放。", rich_help_panel=RESEARCH_COMMANDS)
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
