from datetime import UTC, date, datetime, timedelta

from eliotbot.config import DataConfig
from eliotbot.data.alpaca_provider import AlpacaProvider
from eliotbot.data.calendar import load_calendar
from eliotbot.data.router import route_symbol


class Responses:
    def __init__(self, pages):
        self.pages = iter(pages)
        self.requests = []

    def get_json(self, url, params=None):
        self.requests.append((url, dict(params) if params else None))
        return next(self.pages)


def test_alpaca_raw_and_all_are_kept_separate(monkeypatch):
    monkeypatch.delenv("ALPACA_API_ENDPOINT", raising=False)
    raw = {"t": "2024-07-03T04:00:00Z", "o": 200, "h": 206, "l": 196, "c": 202, "v": 100}
    adjusted = {"t": raw["t"], "o": 100, "h": 103, "l": 98, "c": 101, "v": 200}
    fake = Responses(
        [
            {"id": "fixture-asset", "class": "us_equity", "symbol": "AAPL", "exchange": "NASDAQ"},
            {"bars": {"AAPL": [raw]}, "next_page_token": None},
            {"bars": {"AAPL": [adjusted]}, "next_page_token": None},
        ]
    )
    bundle = AlpacaProvider(DataConfig(), http=fake).fetch(
        route_symbol("AAPL"),
        date(2024, 7, 1),
        date(2024, 7, 3),
        datetime(2024, 7, 4, 12, tzinfo=UTC),
    )
    assert bundle.instrument.identity_verified
    assert bundle.raw_bars[0].close == 202
    assert bundle.bars[0].close == 101
    assert bundle.bars[0].volume == 200
    assert bundle.provenance.feed == "sip"


def test_alpaca_rth_hourly_includes_only_completed_native_subbars(monkeypatch):
    monkeypatch.delenv("ALPACA_API_ENDPOINT", raising=False)
    daily = {"t": "2024-07-02T04:00:00Z", "o": 100, "h": 103, "l": 98, "c": 101, "v": 1000}
    opened = datetime(2024, 7, 3, 13, 30, tzinfo=UTC)
    native = [
        {
            "t": (opened + timedelta(minutes=5 * i)).isoformat(),
            "o": 100,
            "h": 101,
            "l": 99,
            "c": 100,
            "v": 10,
        }
        for i in range(12)
    ]
    fake = Responses(
        [
            {"id": "fixture-asset", "class": "us_equity", "symbol": "AAPL"},
            {"bars": {"AAPL": [daily]}, "next_page_token": None},
            {"bars": {"AAPL": [daily]}, "next_page_token": None},
            {"bars": {"AAPL": native}, "next_page_token": None},
            {"bars": {"AAPL": native}, "next_page_token": None},
        ]
    )
    bundle = AlpacaProvider(DataConfig(), http=fake).fetch(
        route_symbol("AAPL"),
        date(2024, 7, 1),
        date(2024, 7, 3),
        datetime(2024, 7, 3, 15, tzinfo=UTC),
        timeframe="60m",
    )
    assert len(bundle.bars) == 1
    assert bundle.bars[0].start_at == opened
    assert bundle.bars[0].end_at == opened + timedelta(hours=1)
    assert bundle.bars[0].volume == 120
    assert bundle.daily_context[0].session_date == date(2024, 7, 2)
    assert fake.requests[-1][1]["timeframe"] == "5Min"


def test_cn_calendar_buffer_can_start_on_a_national_day_holiday():
    calendar = load_calendar(route_symbol("600519"), date(2021, 11, 12), date(2026, 11, 16))
    assert date(2021, 10, 8) in calendar.dates
