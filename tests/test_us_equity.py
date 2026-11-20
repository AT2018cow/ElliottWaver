from datetime import UTC, date, datetime, timedelta

import pytest

from eliotbot.config import DataConfig, Settings
from eliotbot.data.alpaca_provider import AlpacaProvider
from eliotbot.data.calendar import bar_window, load_calendar, parse_as_of
from eliotbot.data.demo import demo_bundle
from eliotbot.data.periods import as_weekly, target_session
from eliotbot.data.router import route_symbol
from eliotbot.data.snapshots import SnapshotStore
from eliotbot.errors import DataError, SourceError
from eliotbot.schemas.market import AnalysisRequest
from eliotbot.service import AnalysisService


class Responses:
    def __init__(self, pages):
        self.pages = iter(pages)
        self.requests = []

    def get_json(self, url, params=None):
        self.requests.append((url, dict(params) if params else None))
        return next(self.pages)


def _asset(symbol="AAPL", asset_id="fixture-asset"):
    return {"id": asset_id, "class": "us_equity", "symbol": symbol, "exchange": "NASDAQ"}


def _bar(day="2024-07-03", *, close=101.0, stamp=None):
    return {
        "t": stamp or f"{day}T04:00:00Z",
        "o": close,
        "h": close + 2,
        "l": close - 2,
        "c": close,
        "v": 1000,
    }


def _provider(pages, feed="sip"):
    fake = Responses(pages)
    return AlpacaProvider(DataConfig(), feed=feed, http=fake), fake


def _fetch(provider, symbol="AAPL", *, start=date(2024, 7, 1), end=date(2024, 7, 3)):
    return provider.fetch(
        route_symbol(symbol),
        start,
        end,
        datetime(2024, 7, 4, 12, tzinfo=UTC),
    )


def test_alpaca_asset_symbol_mismatch_stops_before_bars():
    provider, fake = _provider([_asset("MSFT")])
    with pytest.raises(DataError, match="ASSET_IDENTITY_MISMATCH"):
        _fetch(provider)
    assert len(fake.requests) == 1
    assert fake.requests[0][0].endswith("/v2/assets/AAPL")


def test_alpaca_share_class_aliases_resolve_to_one_asset():
    for symbol in ("BRK-B", "BRK.B"):
        provider, fake = _provider(
            [
                _asset("BRK.B", "fixture-brk"),
                {"bars": {"BRK.B": [_bar()]}, "next_page_token": None},
                {"bars": {"BRK.B": [_bar()]}, "next_page_token": None},
            ]
        )
        bundle = _fetch(provider, symbol)
        assert bundle.instrument.instrument_id == "alpaca:fixture-brk"
        assert fake.requests[0][0].endswith("/v2/assets/BRK.B")


def test_alpaca_empty_page_with_token_then_final_page():
    provider, _ = _provider(
        [
            {"bars": {"AAPL": []}, "next_page_token": "p2"},
            {"bars": {"AAPL": [_bar()]}, "next_page_token": None},
        ]
    )
    rows = provider._pages(
        "AAPL",
        datetime(2024, 7, 1, tzinfo=UTC),
        datetime(2024, 7, 4, tzinfo=UTC),
        "all",
        date(2024, 7, 3),
    )
    assert len(rows) == 1


def test_alpaca_missing_tail_page_is_a_source_error():
    provider, _ = _provider([{"bars": {"AAPL": []}, "next_page_token": "missing"}])
    with pytest.raises(SourceError, match="ended before"):
        provider._pages(
            "AAPL",
            datetime(2024, 7, 1, tzinfo=UTC),
            datetime(2024, 7, 4, tzinfo=UTC),
            "all",
            date(2024, 7, 3),
        )


def test_alpaca_missing_target_session_is_stale():
    provider, _ = _provider(
        [
            _asset(),
            {"bars": {"AAPL": [_bar("2024-07-02")]}, "next_page_token": None},
            {"bars": {"AAPL": [_bar("2024-07-02")]}, "next_page_token": None},
        ]
    )
    with pytest.raises(DataError, match="STALE_DATA"):
        _fetch(provider)


def test_alpaca_raw_missing_a_day_is_alignment_error():
    provider, _ = _provider(
        [
            _asset(),
            {"bars": {"AAPL": [_bar("2024-07-02")]}, "next_page_token": None},
            {
                "bars": {"AAPL": [_bar("2024-07-02"), _bar("2024-07-03")]},
                "next_page_token": None,
            },
        ]
    )
    with pytest.raises(DataError, match="ALIGNMENT_ERROR"):
        _fetch(provider)


def test_alpaca_timestamp_requires_a_timezone():
    provider, _ = _provider(
        [
            _asset(),
            {"bars": {"AAPL": [_bar(stamp="2024-07-03T04:00:00")]}, "next_page_token": None},
            {"bars": {"AAPL": [_bar()]}, "next_page_token": None},
        ]
    )
    with pytest.raises(SourceError, match="timezone"):
        _fetch(provider)


def test_alpaca_rejects_a_bar_outside_the_exchange_calendar():
    provider, _ = _provider(
        [
            _asset(),
            {"bars": {"AAPL": [_bar("2024-07-04")]}, "next_page_token": None},
            {"bars": {"AAPL": [_bar("2024-07-04")]}, "next_page_token": None},
        ]
    )
    with pytest.raises(DataError, match="outside the verified calendar"):
        provider.fetch(
            route_symbol("AAPL"),
            date(2024, 7, 1),
            date(2024, 7, 5),
            datetime(2024, 7, 8, 12, tzinfo=UTC),
        )


def test_alpaca_conflicting_duplicate_timestamp_is_rejected():
    provider, _ = _provider(
        [
            _asset(),
            {
                "bars": {
                    "AAPL": [
                        _bar("2024-07-03", close=101),
                        _bar("2024-07-03", close=99),
                    ]
                },
                "next_page_token": None,
            },
            {"bars": {"AAPL": [_bar("2024-07-03")]}, "next_page_token": None},
        ]
    )
    with pytest.raises(DataError, match="Conflicting"):
        _fetch(provider)


def _us_bars(calendar, days, *, start_price=100.0):
    from eliotbot.schemas.market import Bar

    observed = datetime(2024, 1, 1, tzinfo=UTC)
    result = []
    for index, day in enumerate(days):
        opened, closed, available = bar_window(calendar.session(day), "US")
        close = start_price + index
        result.append(
            Bar(
                start_at=opened,
                end_at=closed,
                available_at=available,
                session_date=day,
                observed_at=observed,
                open=close,
                high=close + 1,
                low=close - 1,
                close=close,
                volume=100,
            )
        )
    return tuple(result)


def test_us_dst_daily_availability_is_local_next_midnight_plus_buffer():
    calendar = load_calendar(route_symbol("AAPL"), date(2024, 3, 1), date(2024, 3, 20))
    assert bar_window(calendar.session(date(2024, 3, 8)), "US")[2] == datetime(
        2024, 3, 9, 5, 20, tzinfo=UTC
    )
    assert bar_window(calendar.session(date(2024, 3, 11)), "US")[2] == datetime(
        2024, 3, 12, 4, 20, tzinfo=UTC
    )


def test_us_date_only_as_of_uses_local_next_midnight():
    instrument = route_symbol("AAPL")
    now = datetime(2024, 3, 20, tzinfo=UTC)
    assert parse_as_of("2024-03-08", instrument, now) == datetime(2024, 3, 9, 5, 20, tzinfo=UTC)
    assert parse_as_of("2024-03-11", instrument, now) == datetime(2024, 3, 12, 4, 20, tzinfo=UTC)


def test_us_holiday_and_early_close_are_calendar_facts():
    calendar = load_calendar(route_symbol("AAPL"), date(2024, 3, 25), date(2024, 11, 30))
    assert date(2024, 3, 29) not in calendar.dates
    assert calendar.session(date(2024, 11, 29)).close_at == datetime(2024, 11, 29, 18, tzinfo=UTC)


def test_us_shortened_week_is_complete_but_current_partial_week_is_not():
    from eliotbot.data.resample import weekly_bars

    calendar = load_calendar(route_symbol("AAPL"), date(2024, 3, 18), date(2024, 4, 5))
    days = [day for day in calendar.dates if date(2024, 3, 18) <= day <= date(2024, 4, 1)]
    weeks = weekly_bars(_us_bars(calendar, days), calendar.dates)
    assert weeks[-1].session_date == date(2024, 3, 28)
    assert weeks[-1].open == 105
    assert weeks[-1].close == 108
    assert weeks[-1].volume == 400
    assert all(week.session_date != date(2024, 4, 1) for week in weeks)


def test_us_midweek_weekly_target_is_previous_complete_week():
    calendar = load_calendar(route_symbol("AAPL"), date(2024, 3, 18), date(2024, 3, 29))
    target = target_session(
        calendar,
        route_symbol("AAPL"),
        datetime(2024, 3, 27, 20, 20, tzinfo=UTC),
        "1w",
        date(2024, 3, 29),
    )
    assert target == date(2024, 3, 22)


def test_us_provider_weekly_uses_adjusted_bars_and_preserves_raw():
    raw = [
        _bar("2024-07-01", close=200),
        _bar("2024-07-02", close=202),
        _bar("2024-07-03", close=204),
        _bar("2024-07-05", close=206),
    ]
    adjusted = [
        _bar("2024-07-01", close=100),
        _bar("2024-07-02", close=101),
        _bar("2024-07-03", close=102),
        _bar("2024-07-05", close=103),
    ]
    provider, _ = _provider(
        [
            _asset(),
            {"bars": {"AAPL": raw}, "next_page_token": None},
            {"bars": {"AAPL": adjusted}, "next_page_token": None},
        ]
    )
    bundle = provider.fetch(
        route_symbol("AAPL"),
        date(2024, 7, 1),
        date(2024, 7, 5),
        datetime(2024, 7, 8, 12, tzinfo=UTC),
    )
    weekly = as_weekly(bundle)
    assert weekly.bars[0].open == 100
    assert weekly.bars[0].close == 103
    assert weekly.raw_bars[0].open == 200
    assert weekly.raw_bars[0].close == 206
    assert weekly.bars[0].volume == 4000
    assert weekly.raw_bars[0].volume == 4000


def _sip_bundle():
    bundle = demo_bundle("AAPL")
    provenance = bundle.provenance.model_copy(
        update={"feed": "sip", "adjustment": "provider_all", "provider": "alpaca"}
    )
    instrument = bundle.instrument.model_copy(update={"identity_verified": True})
    return bundle.model_copy(update={"provenance": provenance, "instrument": instrument})


def test_brief_surfaces_feed_adjustment_mode_and_cutoff():
    from eliotbot.brief import build_brief
    from eliotbot.visualization.brief_formatter import brief_html, brief_markdown
    from eliotbot.visualization.chart_builder import render_html

    source = _sip_bundle()
    decision = source.bars[-30]
    request = AnalysisRequest(
        symbol="AAPL",
        as_of=decision.usable_at,
        end=decision.session_date,
        mode="FROZEN_DATA_REPLAY",
    )
    run = build_brief(request, source, Settings(), include_weekly=False, check_windows=False)
    for text in (
        brief_markdown(run.brief, run.result),
        brief_html(run.brief, run.result),
        render_html(run.result, run.prepared),
    ):
        assert "sip" in text
        assert "provider_all" in text
        assert "FROZEN_DATA_REPLAY" in text
        assert request.as_of.isoformat() in text


def test_console_brief_and_result_surface_feed_and_adjustment(capsys):
    from eliotbot.brief import build_brief
    from eliotbot.cli.console_render import render_brief, render_result

    source = _sip_bundle()
    decision = source.bars[-30]
    request = AnalysisRequest(symbol="AAPL", as_of=decision.usable_at, end=decision.session_date)
    run = build_brief(request, source, Settings(), include_weekly=False, check_windows=False)
    render_brief(run.brief, run.result)
    render_result(run.result)
    output = capsys.readouterr().out
    assert "sip" in output
    assert "provider_all" in output


def test_offline_brief_rejects_a_different_feed(tmp_path):
    from typer.testing import CliRunner

    from eliotbot.cli.main import app

    cache = tmp_path / "cache"
    snapshot = SnapshotStore(cache).save(_sip_bundle())
    config = tmp_path / "config.yaml"
    config.write_text(f"cache:\n  directory: {cache}\n")
    result = CliRunner().invoke(
        app,
        [
            "brief",
            "AAPL",
            "--offline",
            "--snapshot",
            snapshot,
            "--feed",
            "iex",
            "--no-weekly",
            "--no-window-check",
            "--config",
            str(config),
            "--json",
        ],
    )
    assert result.exit_code == 2
    assert "feed" in result.output.lower()


def test_offline_brief_reports_actual_source_metadata(tmp_path):
    import json

    from typer.testing import CliRunner

    from eliotbot.cli.main import app

    cache = tmp_path / "cache"
    source = _sip_bundle()
    snapshot = SnapshotStore(cache).save(source)
    config = tmp_path / "config.yaml"
    config.write_text(f"cache:\n  directory: {cache}\n")
    result = CliRunner().invoke(
        app,
        [
            "brief",
            "AAPL",
            "--offline",
            "--snapshot",
            snapshot,
            "--no-weekly",
            "--no-window-check",
            "--config",
            str(config),
            "--format",
            "json",
            "--json",
        ],
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["feed"] == "sip"
    assert payload["adjustment"] == "provider_all"
    assert payload["mode"] == "FROZEN_DATA_REPLAY"
    from eliotbot.brief import build_brief

    direct = build_brief(
        AnalysisRequest(
            symbol="AAPL",
            as_of=source.as_of,
            mode="FROZEN_DATA_REPLAY",
            timeframe="1d",
        ),
        source,
        Settings(),
        include_weekly=False,
        check_windows=False,
    )
    assert payload["result_digest"] == direct.result.result_digest


def test_watchlist_reports_feed_and_keeps_item_failures(tmp_path, monkeypatch):
    import json

    from typer.testing import CliRunner

    from eliotbot.cli import main as cli_main
    from eliotbot.cli.main import app

    cache = tmp_path / "cache"
    source = _sip_bundle()
    config = tmp_path / "config.yaml"
    config.write_text(f"cache:\n  directory: {cache}\n")
    watchlist = tmp_path / "watchlist.txt"
    watchlist.write_text("AAPL\nAAPL\nBRK-B\nBRK.B\n600519\nBAD/SYM\n")
    cn_source = demo_bundle("600519").model_copy(update={"as_of": source.as_of})

    def fake_fetch(request, settings, **kwargs):
        if request.symbol == "AAPL":
            return source
        if request.symbol in {"BRK-B", "BRK.B"}:
            instrument = route_symbol("BRK.B").model_copy(
                update={"instrument_id": "alpaca:fixture-brk-b", "identity_verified": True}
            )
            return source.model_copy(update={"instrument": instrument})
        return cn_source

    monkeypatch.setattr(cli_main, "fetch_bundle", fake_fetch)
    result = CliRunner().invoke(
        app,
        [
            "watchlist",
            str(watchlist),
            "--config",
            str(config),
            "--no-weekly",
            "--no-window-check",
            "--retries",
            "0",
            "--as-of",
            source.as_of.isoformat(),
            "--output-dir",
            str(tmp_path / "reports"),
            "--json",
        ],
    )
    assert result.exit_code == 2
    payload = json.loads(result.stdout)
    assert [row["symbol"] for row in payload] == ["AAPL", "BRK.B", "600519.SH", "BAD/SYM"]
    assert payload[0]["feed"] == "sip"
    assert payload[0]["adjustment"] == "provider_all"
    assert payload[1]["feed"] == "sip"
    assert payload[2]["feed"] == "synthetic"
    assert payload[3]["status"] == "ERROR"


def test_prefix_analysis_matches_full_analysis_at_the_same_decision():
    from eliotbot.data.demo import demo_bundle

    bundle = demo_bundle("AAPL")
    decision = bundle.bars[-30]
    index = bundle.bars.index(decision)
    prefix = bundle.model_copy(
        update={
            "bars": bundle.bars[: index + 1],
            "raw_bars": bundle.raw_bars[: index + 1],
        }
    )
    request = AnalysisRequest(
        symbol="AAPL",
        as_of=decision.usable_at,
        end=decision.session_date,
        mode="FROZEN_DATA_REPLAY",
    )
    service = AnalysisService()
    full = service.analyze_bundle(request, bundle)
    truncated = service.analyze_bundle(request, prefix)
    assert [
        (scenario.scenario_id, scenario.pattern_type, scenario.stage) for scenario in full.scenarios
    ] == [
        (scenario.scenario_id, scenario.pattern_type, scenario.stage)
        for scenario in truncated.scenarios
    ]
    assert [
        [(condition.kind, condition.price) for condition in scenario.conditions]
        for scenario in full.scenarios
    ] == [
        [(condition.kind, condition.price) for condition in scenario.conditions]
        for scenario in truncated.scenarios
    ]


def test_provider_price_scale_bridges_a_clean_split():
    from eliotbot.data.adjustment import provider_price_scale

    issued = demo_bundle("AAPL")
    current = issued.model_copy(
        update={
            "bars": tuple(
                bar.model_copy(
                    update={
                        "open": bar.open / 2,
                        "high": bar.high / 2,
                        "low": bar.low / 2,
                        "close": bar.close / 2,
                    }
                )
                for bar in issued.bars
            )
        }
    )
    assert provider_price_scale(issued, current, issued.bars[-1].session_date) == 2.0


def test_alpaca_credentials_are_required_and_alias_conflicts_are_rejected(tmp_path, monkeypatch):
    from eliotbot.config import alpaca_asset_endpoint, alpaca_credentials
    from eliotbot.errors import InputError

    monkeypatch.chdir(tmp_path)
    for name in (
        "ALPACA_API_KEY",
        "ALPACA_API_SECRET",
        "ALPACA_SECRET_KEY",
        "ALPACA_API_ENDPOINT",
    ):
        monkeypatch.delenv(name, raising=False)
    with pytest.raises(InputError, match="ALPACA_API_KEY"):
        alpaca_credentials()
    monkeypatch.setenv("ALPACA_API_KEY", "key")
    monkeypatch.setenv("ALPACA_API_SECRET", "secret")
    monkeypatch.setenv("ALPACA_SECRET_KEY", "different")
    with pytest.raises(InputError, match="conflict"):
        alpaca_credentials()
    monkeypatch.setenv("ALPACA_SECRET_KEY", "secret")
    assert alpaca_credentials()[1].get_secret_value() == "secret"
    monkeypatch.setenv("ALPACA_API_ENDPOINT", "https://example.invalid")
    with pytest.raises(InputError, match="official"):
        alpaca_asset_endpoint()


def test_http_retry_after_beyond_deadline_stops_without_sleeping():
    from eliotbot.data.http import HTTPClient
    from eliotbot.errors import SourceError

    class Response:
        status_code = 429
        headers = {"Retry-After": "999"}

    class Session:
        count = 0

        def get(self, *args, **kwargs):
            self.count += 1
            return Response()

    session = Session()
    with pytest.raises(SourceError, match="beyond"):
        HTTPClient(DataConfig(total_fetch_deadline_seconds=1), session=session).get_json(
            "https://example.invalid"
        )
    assert session.count == 1


def test_http_malformed_json_is_a_source_error():
    from eliotbot.data.http import HTTPClient
    from eliotbot.errors import SourceError

    class Response:
        status_code = 200

        def json(self):
            raise ValueError("bad json")

    class Session:
        def get(self, *args, **kwargs):
            return Response()

    with pytest.raises(SourceError, match="malformed JSON"):
        HTTPClient(DataConfig(), session=Session()).get_json("https://example.invalid")


def test_fetch_cli_reports_source_and_availability(tmp_path, monkeypatch):
    import json

    from typer.testing import CliRunner

    from eliotbot.cli import main as cli_main
    from eliotbot.cli.main import app

    cache = tmp_path / "cache"
    source = _sip_bundle()
    config = tmp_path / "config.yaml"
    config.write_text(f"cache:\n  directory: {cache}\n")

    def fake_fetch(request, settings, **kwargs):
        return source

    monkeypatch.setattr(cli_main, "fetch_bundle", fake_fetch)
    result = CliRunner().invoke(app, ["data", "fetch", "AAPL", "--config", str(config)])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["feed"] == "sip"
    assert payload["adjustment"] == "provider_all"
    assert payload["market_asof"] == str(source.bars[-1].session_date)
    assert payload["last_available_at"] == source.bars[-1].usable_at.isoformat()


def test_offline_analyze_matches_direct_analysis_and_inspect_metadata(tmp_path):
    import json

    from typer.testing import CliRunner

    from eliotbot.cli.main import app

    source = _sip_bundle()
    cache = tmp_path / "cache"
    snapshot = SnapshotStore(cache).save(source)
    config = tmp_path / "config.yaml"
    config.write_text(f"cache:\n  directory: {cache}\n")
    request = AnalysisRequest(
        symbol="AAPL",
        as_of=source.as_of,
        mode="FROZEN_DATA_REPLAY",
        timeframe="1d",
    )
    direct = AnalysisService().analyze_bundle(request, source)
    result = CliRunner().invoke(
        app,
        [
            "analyze",
            "AAPL",
            "--offline",
            "--snapshot",
            snapshot,
            "--config",
            str(config),
            "--format",
            "json",
            "--json",
        ],
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["result_digest"] == direct.result_digest
    assert payload["feed"] == "sip"
    assert payload["adjustment"] == "provider_all"

    inspect = CliRunner().invoke(app, ["data", "inspect", snapshot, "--config", str(config)])
    assert inspect.exit_code == 0, inspect.output
    inspected = json.loads(inspect.stdout)
    assert inspected["last_available_at"] == source.bars[-1].usable_at.isoformat()
    assert inspected["provenance"]["feed"] == "sip"


def test_us_sip_and_iex_caches_are_separate(tmp_path, monkeypatch):
    import sqlite3

    from eliotbot.data import workflow
    from eliotbot.data.workflow import fetch_bundle
    from eliotbot.schemas.market import MarketDataBundle, Provenance
    from eliotbot.util import digest

    calendar = load_calendar(route_symbol("AAPL"), date(2024, 6, 1), date(2024, 7, 8))
    days = [day for day in calendar.dates if day <= date(2024, 7, 5)][-80:]
    bars = _us_bars(calendar, days)
    as_of = datetime(2024, 7, 8, 12, tzinfo=UTC)
    fetched_at = datetime.now(UTC)
    instrument = route_symbol("AAPL").model_copy(
        update={"instrument_id": "alpaca:fixture", "identity_verified": True}
    )

    def bundle_for(feed):
        adjusted = tuple(
            bar.model_copy(
                update={
                    "open": bar.open * (2 if feed == "iex" else 1),
                    "high": bar.high * (2 if feed == "iex" else 1),
                    "low": bar.low * (2 if feed == "iex" else 1),
                    "close": bar.close * (2 if feed == "iex" else 1),
                }
            )
            for bar in bars
        )
        return MarketDataBundle(
            instrument=instrument,
            as_of=as_of,
            bars=adjusted,
            raw_bars=bars,
            provenance=Provenance(
                provider="alpaca",
                feed=feed,
                adjustment="provider_all",
                price_coordinate_id=digest(["alpaca", feed, days[-1]]),
                basis_date=days[-1],
                session_profile="provider_native_daily",
                calendar_version=calendar.version,
                fetched_at=fetched_at,
                requested_start=days[0],
                requested_end=days[-1],
                amount_currency="USD",
            ),
            sessions=calendar.dates,
        )

    calls = []

    class FakeAlpaca:
        def __init__(self, config, *, feed):
            self.feed = feed

        def fetch(self, instrument, start, end, as_of, timeframe="1d"):
            calls.append(self.feed)
            return bundle_for(self.feed)

    monkeypatch.setattr(workflow, "AlpacaProvider", FakeAlpaca)
    settings = Settings(cache={"directory": tmp_path / "cache"})
    request = AnalysisRequest(
        symbol="AAPL",
        as_of=as_of,
        end=date(2024, 7, 5),
        timeframe="1d",
        feed="sip",
    )
    sip = fetch_bundle(request, settings)
    sip_again = fetch_bundle(request, settings)
    iex = fetch_bundle(request.model_copy(update={"feed": "iex"}), settings)
    later_request = request.model_copy(update={"as_of": as_of + timedelta(seconds=1)})
    later = fetch_bundle(later_request, settings)
    earlier_request = request.model_copy(update={"as_of": as_of - timedelta(seconds=1)})
    earlier = fetch_bundle(earlier_request, settings)
    assert calls == ["sip", "iex"]
    assert sip.snapshot_id == sip_again.snapshot_id
    assert sip.snapshot_id != iex.snapshot_id
    assert sip.provenance.feed == "sip" and iex.provenance.feed == "iex"
    assert sip.bars[0].close != iex.bars[0].close
    assert later.snapshot_id != sip.snapshot_id
    assert later.as_of == later_request.as_of
    assert earlier.snapshot_id != sip.snapshot_id
    assert earlier.as_of == earlier_request.as_of
    store = SnapshotStore(settings.cache.directory)
    assert store.load(later.snapshot_id) == later
    assert store.load(earlier.snapshot_id) == earlier
    assert earlier.provenance.fetched_at == later.provenance.fetched_at == fetched_at
    with sqlite3.connect(settings.cache.directory / "index.sqlite3") as db:
        cached_ids = {row[0] for row in db.execute("SELECT snapshot FROM cache")}
    assert cached_ids == {sip.snapshot_id, iex.snapshot_id}

    loaded = []
    load_snapshot = SnapshotStore.load

    def record_load(self, snapshot_id):
        loaded.append(snapshot_id)
        return load_snapshot(self, snapshot_id)

    monkeypatch.setattr(SnapshotStore, "load", record_load)
    assert fetch_bundle(later_request, settings) == later
    assert loaded == [later.snapshot_id]
    assert calls == ["sip", "iex"]


@pytest.mark.parametrize("cutoffs", [(5, 1, 5), (1, 5, 1)])
def test_us_intraday_cache_separates_newly_available_daily_context(tmp_path, cutoffs):
    from eliotbot.data.workflow import fetch_bundle

    july_2 = _bar("2024-07-02")
    july_3 = _bar("2024-07-03")
    opened = datetime(2024, 7, 3, 13, 30, tzinfo=UTC)
    minutes = [_bar(stamp=(opened + timedelta(minutes=5 * i)).isoformat()) for i in range(42)]
    settings = Settings(cache={"directory": tmp_path / "cache"})
    results = []
    request_counts = []
    for hour in cutoffs:
        daily_rows = [july_2] if hour == 1 else [july_2, july_3]
        daily_page = {"bars": {"AAPL": daily_rows}, "next_page_token": None}
        minute_page = {"bars": {"AAPL": minutes}, "next_page_token": None}
        provider, fake = _provider([_asset(), daily_page, daily_page, minute_page, minute_page])
        request = AnalysisRequest(
            symbol="AAPL",
            as_of=datetime(2024, 7, 4, hour, tzinfo=UTC),
            feed="sip",
            timeframe="1h",
        )
        result = fetch_bundle(request, settings, provider=provider)
        results.append(result)
        request_counts.append(len(fake.requests))
        assert result.bars[-1].start_at == datetime(2024, 7, 3, 16, 30, tzinfo=UTC)
        expected = date(2024, 7, 2 if hour == 1 else 3)
        assert result.daily_context[-1].session_date == expected
        assert result.raw_daily_context[-1].session_date == expected
    assert results[0].snapshot_id == results[2].snapshot_id != results[1].snapshot_id
    assert request_counts == [5, 5, 0]


def test_legacy_alpaca_snapshot_is_rejected_and_cache_is_refetched(tmp_path, monkeypatch):
    from typer.testing import CliRunner

    from eliotbot.cli.main import app
    from eliotbot.data import snapshots, workflow

    source = _long_daily_bundle()
    settings = Settings(cache={"directory": tmp_path / "cache"})
    request = AnalysisRequest(
        symbol="AAPL", as_of=source.as_of, end=source.bars[-1].session_date, feed="sip"
    )
    wrong_symbol = source.instrument.model_copy(
        update={"provider_symbol": "MSFT", "instrument_id": "alpaca:msft"}
    )
    legacy = source.model_copy(update={"instrument": wrong_symbol})
    calls = []

    class FixtureProvider:
        def __init__(self, bundle):
            self.bundle = bundle

        def fetch(self, instrument, start, end, as_of, timeframe):
            calls.append(instrument.canonical_symbol)
            return self.bundle

    with monkeypatch.context() as old_version:
        old_version.setattr(snapshots, "_validate_alpaca_snapshot", lambda _: None)
        workflow.fetch_bundle(request, settings, provider=FixtureProvider(legacy))

    config = tmp_path / "config.yaml"
    config.write_text(f"cache:\n  directory: {settings.cache.directory}\n")
    result = CliRunner().invoke(
        app,
        [
            "brief",
            "AAPL",
            "--offline",
            "--snapshot",
            legacy.snapshot_id,
            "--config",
            str(config),
            "--output-dir",
            str(tmp_path / "reports"),
        ],
    )
    assert result.exit_code == 3
    assert "ASSET_IDENTITY_MISMATCH" in result.output
    assert not (tmp_path / "reports").exists()

    assert workflow.fetch_bundle(request, settings, provider=FixtureProvider(source)) == source
    assert workflow.fetch_bundle(request, settings, provider=FixtureProvider(legacy)) == source
    assert calls == ["AAPL", "AAPL"]


def test_earlier_cache_cutoff_does_not_reuse_unavailable_context(tmp_path):
    from eliotbot.data.workflow import fetch_bundle

    source = _long_daily_bundle()
    pending_at = source.as_of + timedelta(minutes=30)
    context = source.bars[-1].model_copy(update={"available_at": pending_at})
    later_source = source.model_copy(
        update={"as_of": pending_at, "daily_context": (context,), "raw_daily_context": (context,)}
    )

    class FixtureProvider:
        def fetch(self, instrument, start, end, as_of, timeframe):
            return later_source if as_of == pending_at else source

    settings = Settings(cache={"directory": tmp_path / "cache"})
    earlier = AnalysisRequest(symbol="AAPL", as_of=source.as_of, end=source.bars[-1].session_date)
    later = earlier.model_copy(update={"as_of": pending_at})
    provider = FixtureProvider()
    assert fetch_bundle(later, settings, provider=provider) == later_source
    actual = fetch_bundle(earlier, settings, provider=provider)
    assert actual == source
    assert SnapshotStore(settings.cache.directory).load(actual.snapshot_id) == source


def test_us_week_with_an_internal_missing_session_is_not_complete():
    from eliotbot.data.resample import weekly_bars

    calendar = load_calendar(route_symbol("AAPL"), date(2024, 3, 18), date(2024, 4, 5))
    days = [
        day
        for day in calendar.dates
        if date(2024, 3, 18) <= day <= date(2024, 3, 28) and day != date(2024, 3, 26)
    ]
    weeks = weekly_bars(_us_bars(calendar, days), calendar.dates)
    assert [week.session_date for week in weeks] == [date(2024, 3, 22)]


def _long_daily_bundle():
    from eliotbot.schemas.market import MarketDataBundle, Provenance
    from eliotbot.util import digest

    calendar = load_calendar(route_symbol("AAPL"), date(2022, 6, 1), date(2024, 7, 5))
    days = [day for day in calendar.dates if day <= date(2024, 7, 5)][-420:]
    bars = _us_bars(calendar, days)
    instrument = route_symbol("AAPL").model_copy(
        update={"instrument_id": "alpaca:fixture", "identity_verified": True}
    )
    return MarketDataBundle(
        instrument=instrument,
        as_of=bars[-1].usable_at,
        bars=bars,
        raw_bars=bars,
        provenance=Provenance(
            provider="alpaca",
            feed="sip",
            adjustment="provider_all",
            price_coordinate_id=digest(["alpaca", "sip", days[-1]]),
            basis_date=days[-1],
            session_profile="provider_native_daily",
            calendar_version=calendar.version,
            fetched_at=datetime.now(UTC),
            requested_start=days[0],
            requested_end=days[-1],
            amount_currency="USD",
        ),
        sessions=calendar.dates,
    )


def test_offline_weekly_brief_matches_derived_weekly_analysis(tmp_path):
    import json

    from typer.testing import CliRunner

    from eliotbot.brief import build_brief
    from eliotbot.cli.main import app

    source = _long_daily_bundle()
    weekly = as_weekly(source)
    cache = tmp_path / "cache"
    snapshot = SnapshotStore(cache).save(source)
    config = tmp_path / "config.yaml"
    config.write_text(f"cache:\n  directory: {cache}\n")
    request = AnalysisRequest(
        symbol="AAPL",
        as_of=source.as_of,
        mode="FROZEN_DATA_REPLAY",
        timeframe="1w",
    )
    direct = build_brief(request, source, Settings(), include_weekly=False, check_windows=False)
    derived = AnalysisService().analyze_bundle(
        request.model_copy(update={"timeframe": "1w"}), weekly
    )
    assert direct.result.result_digest == derived.result_digest
    result = CliRunner().invoke(
        app,
        [
            "brief",
            "AAPL",
            "--offline",
            "--snapshot",
            snapshot,
            "--timeframe",
            "1w",
            "--no-weekly",
            "--no-window-check",
            "--config",
            str(config),
            "--format",
            "json",
            "--json",
        ],
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["timeframe"] == "1w"
    assert payload["result_digest"] == direct.result.result_digest


def test_replay_manifest_records_source_and_frozen_mode(tmp_path):
    import json

    from typer.testing import CliRunner

    from eliotbot.cli.main import app

    source = _long_daily_bundle()
    cache = tmp_path / "cache"
    snapshot = SnapshotStore(cache).save(source)
    config = tmp_path / "config.yaml"
    config.write_text(f"cache:\n  directory: {cache}\n")
    output = tmp_path / "replay.jsonl"
    start = source.bars[-5].session_date.isoformat()
    result = CliRunner().invoke(
        app,
        [
            "replay",
            snapshot,
            "--start",
            start,
            "--output",
            str(output),
            "--config",
            str(config),
        ],
    )
    assert result.exit_code == 0, result.output
    manifest = json.loads((tmp_path / "replay.jsonl.manifest.json").read_text())
    assert manifest["mode"] == "FROZEN_DATA_REPLAY"
    assert manifest["source_snapshot_id"] == snapshot
    assert manifest["reports"] == 5
    first = json.loads(output.read_text().splitlines()[0])
    assert first["mode"] == "FROZEN_DATA_REPLAY"
    assert first["input_snapshot_id"] != snapshot
    assert first["as_of"] >= first["market_asof"]


def test_alpaca_forbidden_asset_request_does_not_fall_back_to_iex():
    calls = []

    class Forbidden:
        def get_json(self, url, params=None):
            calls.append((url, params))
            raise SourceError("SOURCE_FORBIDDEN: HTTP 403")

    provider = AlpacaProvider(DataConfig(), feed="sip", http=Forbidden())
    with pytest.raises(SourceError, match="FORBIDDEN"):
        _fetch(provider)
    assert len(calls) == 1
    assert calls[0][0].endswith("/v2/assets/AAPL")
    assert all((params or {}).get("feed") != "iex" for _, params in calls)


def test_provider_price_scale_bridges_a_dividend_adjustment():
    from eliotbot.data.adjustment import provider_price_scale

    issued = demo_bundle("AAPL")
    current = issued.model_copy(
        update={
            "bars": tuple(
                bar.model_copy(
                    update={
                        "open": bar.open * 0.98,
                        "high": bar.high * 0.98,
                        "low": bar.low * 0.98,
                        "close": bar.close * 0.98,
                    }
                )
                for bar in issued.bars
            )
        }
    )
    scale = provider_price_scale(issued, current, issued.bars[-1].session_date)
    assert scale == pytest.approx(1 / 0.98)


def test_us_weekends_and_holidays_are_not_reported_as_gaps():
    from eliotbot.data.quality import missing_sessions

    bundle = _long_daily_bundle()
    assert missing_sessions(bundle) == ()


def test_alpaca_non_string_asset_id_is_rejected_before_bars():
    provider, fake = _provider(
        [{"id": 123, "class": "us_equity", "symbol": "AAPL", "exchange": "NASDAQ"}]
    )
    with pytest.raises(DataError, match="ASSET_IDENTITY_UNAVAILABLE"):
        _fetch(provider)
    assert len(fake.requests) == 1


def _weekly_demo_daily_bundle():
    from datetime import time, timedelta
    from zoneinfo import ZoneInfo

    from eliotbot.data.demo import demo_bundle
    from eliotbot.schemas.market import Bar, MarketDataBundle, Provenance
    from eliotbot.util import digest

    source_weekly = demo_bundle("AAPL", timeframe="1w")
    zone = ZoneInfo("America/New_York")
    bars = []
    sessions = []
    for week in source_weekly.bars:
        days = []
        day = week.session_date
        while len(days) < 5:
            if day.weekday() < 5:
                days.append(day)
            day -= timedelta(days=1)
        days.sort()
        sessions.extend(days)
        for day in days:
            start = datetime.combine(day, time.min, zone).astimezone(UTC)
            end = datetime.combine(day + timedelta(days=1), time.min, zone).astimezone(UTC)
            bars.append(
                Bar(
                    start_at=start,
                    end_at=end,
                    available_at=end + timedelta(minutes=20),
                    session_date=day,
                    observed_at=datetime(2026, 1, 1, tzinfo=UTC),
                    open=week.open,
                    high=week.high,
                    low=week.low,
                    close=week.close,
                    volume=week.volume / 5,
                )
            )
    instrument = route_symbol("AAPL").model_copy(
        update={"instrument_id": "alpaca:fixture", "identity_verified": True}
    )
    provenance = Provenance(
        provider="alpaca",
        feed="sip",
        adjustment="provider_all",
        price_coordinate_id=digest(["alpaca", "sip", "split-fixture"]),
        basis_date=sessions[-1],
        session_profile="provider_native_daily",
        calendar_version="fixture",
        fetched_at=datetime.now(UTC),
        requested_start=sessions[0],
        requested_end=sessions[-1],
        amount_currency="USD",
    )
    raw = tuple(
        bar.model_copy(
            update={
                "open": bar.open * 2,
                "high": bar.high * 2,
                "low": bar.low * 2,
                "close": bar.close * 2,
            }
        )
        for bar in bars
    )
    return MarketDataBundle(
        instrument=instrument,
        as_of=bars[-1].usable_at,
        bars=tuple(bars),
        raw_bars=raw,
        provenance=provenance,
        sessions=tuple(sessions),
    ), source_weekly


def test_us_provider_all_daily_to_weekly_projection_stays_in_adjusted_coordinate():
    from eliotbot.schemas.market import AnalysisRequest

    daily, source_weekly = _weekly_demo_daily_bundle()
    weekly = as_weekly(daily)
    assert len(weekly.bars) == len(source_weekly.bars)
    for actual, expected in zip(weekly.bars, source_weekly.bars, strict=True):
        assert actual.session_date == expected.session_date
        assert (actual.open, actual.high, actual.low, actual.close) == (
            expected.open,
            expected.high,
            expected.low,
            expected.close,
        )
    for adjusted, raw in zip(weekly.bars, weekly.raw_bars, strict=True):
        assert (raw.open, raw.high, raw.low, raw.close) == (
            adjusted.open * 2,
            adjusted.high * 2,
            adjusted.low * 2,
            adjusted.close * 2,
        )
    result = AnalysisService().analyze_bundle(
        AnalysisRequest(symbol="AAPL", as_of=weekly.as_of, timeframe="1w"), weekly
    )
    assert result.status == "OK"
    assert result.scenarios
    adjusted_min = min(bar.low for bar in weekly.bars)
    raw_min = min(bar.low for bar in weekly.raw_bars)
    for scenario in result.scenarios:
        prices = [condition.price for condition in scenario.conditions]
        prices += [zone.low for zone in scenario.target_zones]
        prices += [zone.high for zone in scenario.target_zones]
        assert prices
        assert all(adjusted_min <= price < raw_min for price in prices)
