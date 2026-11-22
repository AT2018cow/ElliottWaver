from datetime import UTC, date, datetime, timedelta

import pytest

from elliottwaver.config import DataConfig
from elliottwaver.data.alpaca_provider import AlpacaProvider
from elliottwaver.data.calendar import (
    Session,
    TradingCalendar,
    bar_window,
    latest_analyzable,
    load_calendar,
)
from elliottwaver.data.http import HTTPClient
from elliottwaver.data.resample import weekly_bars
from elliottwaver.data.router import route_symbol
from elliottwaver.errors import SourceError


class FakeHTTP:
    def __init__(self, pages):
        self.pages = iter(pages)
        self.requests = []

    def get_json(self, url, params=None):
        self.requests.append((url, dict(params) if params else None))
        return next(self.pages)


def test_alpaca_pagination_is_complete_and_never_changes_feed():
    fake = FakeHTTP(
        [
            {"bars": {"AAPL": [{"c": 10}]}, "next_page_token": "p2"},
            {"bars": {"AAPL": [{"c": 11}]}, "next_page_token": None},
        ]
    )
    provider = AlpacaProvider(DataConfig(), feed="sip", http=fake)
    rows = provider._pages(
        "AAPL",
        datetime(2024, 1, 1, tzinfo=UTC),
        datetime(2024, 2, 1, tzinfo=UTC),
        "all",
        date(2024, 2, 1),
    )
    assert [row["c"] for row in rows] == [10, 11]
    assert fake.requests[1][1]["page_token"] == "p2"
    assert all(
        params["feed"] == "sip"
        and params["adjustment"] == "all"
        and params["symbols"] == "AAPL"
        and params["asof"] == "2024-02-01"
        and params["timeframe"] == "1Day"
        and params["end"]
        for _, params in fake.requests
    )


def test_repeated_page_token_is_not_a_partial_success():
    fake = FakeHTTP([{"bars": {"AAPL": []}, "next_page_token": "loop"}] * 2)
    provider = AlpacaProvider(DataConfig(), http=fake)
    with pytest.raises(SourceError, match="Repeated"):
        provider._pages(
            "AAPL",
            datetime(2024, 1, 1, tzinfo=UTC),
            datetime(2024, 2, 1, tzinfo=UTC),
            "raw",
            date(2024, 2, 1),
        )


def test_http_auth_failure_is_not_retried():
    class Response:
        status_code = 403

    class Session:
        count = 0

        def get(self, *args, **kwargs):
            self.count += 1
            return Response()

    session = Session()
    with pytest.raises(SourceError, match="FORBIDDEN"):
        HTTPClient(DataConfig(), session=session).get_json("https://example.invalid")
    assert session.count == 1


def test_http_deadline_applies_before_another_request():
    client = HTTPClient(DataConfig(total_fetch_deadline_seconds=1))
    client.started -= 2
    with pytest.raises(SourceError, match="deadline"):
        client.get_json("https://example.invalid")


def test_us_access_delay_is_distinct_from_native_daily_completion():
    session = Session(
        date(2024, 7, 3),
        datetime(2024, 7, 3, 13, 30, tzinfo=UTC),
        datetime(2024, 7, 3, 17, tzinfo=UTC),
    )
    start, end, available = bar_window(session, "US")
    assert end == datetime(2024, 7, 4, 4, tzinfo=UTC)
    assert available == end + timedelta(minutes=20)
    assert available > session.close_at + timedelta(minutes=20)
    calendar = TradingCalendar((session,), "fixture")
    with pytest.raises(Exception, match="No completed"):
        latest_analyzable(calendar, route_symbol("AAPL"), session.close_at + timedelta(minutes=20))


def test_real_calendar_holidays_dst_and_early_close_are_local_data():
    instrument = route_symbol("AAPL")
    calendar = load_calendar(instrument, date(2024, 1, 1), date(2024, 12, 31))
    assert date(2024, 1, 1) not in calendar.dates
    assert calendar.session(date(2024, 7, 3)).close_at.hour == 17
    assert calendar.session(date(2024, 1, 3)).open_at.hour == 14
    assert calendar.session(date(2024, 7, 5)).open_at.hour == 13
    target = latest_analyzable(
        calendar, instrument, datetime(2024, 7, 8, 12, tzinfo=UTC), through=date(2024, 7, 7)
    )
    assert target == date(2024, 7, 5)


def test_partial_week_cannot_be_a_confirmed_macro_anchor(bundle):
    cutoff = 101
    while bundle.bars[cutoff].session_date.weekday() != 1:
        cutoff += 1
    prefix = bundle.bars[: cutoff + 1]
    weeks = weekly_bars(prefix, bundle.sessions)
    assert all(
        week.session_date.isocalendar()[:2] != prefix[-1].session_date.isocalendar()[:2]
        for week in weeks
    )
