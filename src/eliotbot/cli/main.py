"""CLI entrypoints; a minimal argparse bootstrap works before optional UI install."""

import argparse
import json
import webbrowser
from datetime import UTC, date, datetime
from functools import wraps
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

from pydantic import ValidationError
from rich.console import Console

from eliotbot import __version__
from eliotbot.cli.console_render import render_result
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
from eliotbot.errors import EliotError, InputError
from eliotbot.evaluation.replay import replay
from eliotbot.schemas.market import AnalysisRequest
from eliotbot.service import AnalysisService, prepare_bundle
from eliotbot.timeframes import TIMEFRAMES, normalize_timeframe
from eliotbot.util import atomic_write, canonical_json
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
