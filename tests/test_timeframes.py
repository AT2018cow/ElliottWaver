from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from conftest import make_bar
from typer.testing import CliRunner

from elliottwaver.cli.main import app
from elliottwaver.data.adjustment import adjust_to_basis
from elliottwaver.data.calendar import Session, TradingCalendar, load_calendar
from elliottwaver.data.demo import demo_bundle
from elliottwaver.data.periods import aggregate_intraday, intraday_windows, target_session
from elliottwaver.schemas.market import AnalysisRequest, FactorPoint, MarketDataBundle
from elliottwaver.service import AnalysisService, prepare_bundle
from elliottwaver.timeframes import TIMEFRAMES


@pytest.mark.parametrize("symbol", ["AAPL", "600519"])
@pytest.mark.parametrize("timeframe", TIMEFRAMES)
def test_all_timeframes_share_the_same_engine(symbol, timeframe):
    bundle = demo_bundle(symbol, timeframe=timeframe)
    request = AnalysisRequest(symbol=symbol, as_of=bundle.as_of, timeframe=timeframe)
    result = AnalysisService().analyze_bundle(request, bundle)
    assert result.timeframe == timeframe
    assert result.status in {"OK", "UNCLEAR", "SEARCH_LIMITED"}
    assert all(
        w.pivot.confirmed_at <= result.as_of for s in result.scenarios for w in s.completed_waves
    )
    assert all(p.timeframe == timeframe for s in result.scenarios for p in s.projected_path)


def test_hour_alias_and_cli_option(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    import json

    response = CliRunner().invoke(app, ["demo", "--timeframe", "1h", "--format", "json", "--json"])
    assert response.exit_code == 0, response.output
    value = json.loads(response.stdout)
    assert value["timeframe"] == "60m"
    offline = CliRunner().invoke(
        app,
        [
            "analyze",
            "AAPL",
            "--offline",
            "--snapshot",
            value["input_snapshot_id"],
            "--format",
            "json",
            "--json",
        ],
    )
    assert offline.exit_code == 0, offline.output
    assert json.loads(offline.stdout)["result_digest"] == value["result_digest"]


def test_cn_hour_windows_do_not_span_the_lunch_break():
    from elliottwaver.data.router import route_symbol

    instrument = route_symbol("600519")
    calendar = load_calendar(instrument, date(2024, 6, 3), date(2024, 6, 7))
    zone = ZoneInfo("Asia/Shanghai")
    windows = list(intraday_windows(calendar.session(date(2024, 6, 3)), 60))
    assert [
        (a.astimezone(zone).strftime("%H:%M"), b.astimezone(zone).strftime("%H:%M"))
        for a, b in windows
    ] == [("09:30", "10:30"), ("10:30", "11:30"), ("13:00", "14:00"), ("14:00", "15:00")]


def test_us_early_close_preserves_a_partial_hour():
    from elliottwaver.data.router import route_symbol

    instrument = route_symbol("AAPL")
    calendar = load_calendar(instrument, date(2024, 7, 1), date(2024, 7, 8))
    windows = list(intraday_windows(calendar.session(date(2024, 7, 3)), 60))
    assert len(windows) == 4
    assert windows[-1][1] - windows[-1][0] == timedelta(minutes=30)
    after_first_hour = windows[0][1] + timedelta(minutes=20)
    assert target_session(calendar, instrument, after_first_hour, "60m", date(2024, 7, 3)) == date(
        2024, 7, 3
    )


def test_hour_aggregation_refuses_a_hidden_missing_subbar():
    from elliottwaver.data.router import route_symbol

    instrument = route_symbol("AAPL")
    opened = datetime(2024, 7, 1, 13, 30, tzinfo=UTC)
    session = Session(date(2024, 7, 1), opened, opened + timedelta(hours=1))
    calendar = TradingCalendar((session,), "fixture")
    bars = tuple(
        make_bar(i, 10).model_copy(
            update={
                "start_at": opened + timedelta(minutes=5 * i),
                "end_at": opened + timedelta(minutes=5 * (i + 1)),
                "session_date": session.date,
            }
        )
        for i in range(12)
    )
    cutoff = session.close_at + timedelta(minutes=20)
    assert len(aggregate_intraday(bars, calendar, instrument, "60m", cutoff)) == 1
    assert aggregate_intraday(bars[:4] + bars[5:], calendar, instrument, "60m", cutoff) == ()


def test_intraday_gap_on_the_same_date_is_detected():
    bundle = demo_bundle("600519", timeframe="1m")
    values = bundle.model_dump()
    values.update(
        bars=bundle.bars[:200] + bundle.bars[201:],
        raw_bars=bundle.raw_bars[:200] + bundle.raw_bars[201:],
    )
    modified = MarketDataBundle(**values)
    result = AnalysisService().analyze_bundle(
        AnalysisRequest(symbol="600519", as_of=bundle.as_of, timeframe="1m"), modified
    )
    assert result.status == "INSUFFICIENT_DATA"


def test_weekly_ohlc_is_adjusted_before_aggregation():
    base = demo_bundle("600519")
    raw = tuple(make_bar(i, 100 if i < 2 else 50) for i in range(5))
    factors = (
        FactorPoint(effective_date=date(2024, 1, 1), factor=1),
        FactorPoint(effective_date=date(2024, 1, 3), factor=2),
    )
    provenance = base.provenance.model_copy(
        update={"basis_date": raw[-1].session_date, "adjustment": "basis_date_qfq"}
    )
    bundle = MarketDataBundle(
        instrument=base.instrument,
        as_of=raw[-1].end_at,
        bars=adjust_to_basis(raw, factors),
        raw_bars=raw,
        factors=factors,
        provenance=provenance,
        sessions=tuple(
            date(2024, 1, 1) + timedelta(days=i)
            for i in range(12)
            if (date(2024, 1, 1) + timedelta(days=i)).weekday() < 5
        ),
    )
    weekly = prepare_bundle(
        AnalysisRequest(symbol="600519", as_of=bundle.as_of, timeframe="1w"), bundle
    )
    assert weekly.raw_bars[0].high == 100
    assert weekly.bars[0].high == 50
    assert weekly.bars[0].low == 50
