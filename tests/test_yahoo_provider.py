from datetime import UTC, date, datetime, time
from zoneinfo import ZoneInfo

from elliottwaver.config import DataConfig, Settings
from elliottwaver.data.router import route_symbol
from elliottwaver.data.workflow import fetch_bundle
from elliottwaver.data.yahoo_provider import YahooIndexProvider
from elliottwaver.schemas.market import AnalysisRequest

ET = ZoneInfo("America/New_York")
AS_OF = datetime(2024, 6, 3, 10, 30, tzinfo=ET)


def _stamp(day: date, hour: int, minute: int = 0) -> int:
    return int(datetime.combine(day, time(hour, minute), ET).astimezone(UTC).timestamp())


class FakeYahoo(YahooIndexProvider):
    def _chart(self, ticker: str, interval: str) -> dict:
        if interval == "1d":
            stamps = [_stamp(date(2024, 5, 31), 9, 30), _stamp(date(2024, 6, 3), 9, 30)]
            quote = {
                "open": [5200.0, 5230.0],
                "high": [5250.0, 5280.0],
                "low": [5180.0, 5210.0],
                "close": [5230.0, 5260.0],
                "volume": [0, 0],
            }
        else:
            stamps = [
                _stamp(date(2024, 6, 3), 9, 30),
                _stamp(date(2024, 6, 3), 9, 35),
                _stamp(date(2024, 6, 3), 9, 40),
            ]
            quote = {
                "open": [5230.0, 5232.0, 5236.0],
                "high": [5235.0, 5238.0, 5240.0],
                "low": [5228.0, 5230.0, 5234.0],
                "close": [5232.0, 5236.0, 5238.0],
                "volume": [0, 0, 0],
            }
        return {
            "meta": {"symbol": ticker, "exchangeTimezoneName": "America/New_York"},
            "timestamp": stamps,
            "indicators": {"quote": [quote]},
        }


def test_yahoo_index_5m_uses_free_source_identity():
    provider = FakeYahoo(DataConfig(us_index_feed="yahoo-index"))
    bundle = provider.fetch(
        route_symbol("SPX"),
        date(2024, 5, 31),
        date(2024, 6, 3),
        AS_OF,
        timeframe="5m",
    )
    assert route_symbol("SPX").provider_symbol == "^GSPC"
    assert len(bundle.bars) == 3
    assert bundle.provenance.feed == "yahoo-index"
    assert bundle.provenance.adjustment == "none"
    assert bundle.provenance.volume_comparable is False
    assert bundle.expected_bar_starts[-1] == bundle.bars[-1].start_at
    assert any(
        "YAHOO_INDEX_VOLUME_UNAVAILABLE" in warning for warning in bundle.provenance.warnings
    )


def test_workflow_routes_us_index_to_yahoo_when_configured(tmp_path):
    settings = Settings(
        cache={"directory": tmp_path},
        analysis={"history_years": 1},
        data={"us_index_feed": "yahoo-index"},
    )
    request = AnalysisRequest(symbol="SPX", as_of=AS_OF, timeframe="5m")
    bundle = fetch_bundle(request, settings, provider=FakeYahoo(settings.data, feed="yahoo-index"))
    assert bundle.instrument.asset_type == "INDEX"
    assert bundle.provenance.feed == "yahoo-index"
    assert bundle.bars[-1].session_date == date(2024, 6, 3)


def test_yahoo_us_equity_uses_free_feed_and_ticker_alias():
    provider = FakeYahoo(DataConfig(us_equity_feed="yahoo"), feed="yahoo")
    bundle = provider.fetch(
        route_symbol("BRK.B"),
        date(2024, 5, 31),
        date(2024, 6, 3),
        AS_OF,
        timeframe="5m",
    )
    assert bundle.instrument.asset_type == "EQUITY"
    assert bundle.provenance.feed == "yahoo"
    assert bundle.provenance.volume_comparable is True
    assert bundle.provenance.volume_unit == "shares"


def test_workflow_routes_us_equity_to_yahoo_when_configured(tmp_path):
    settings = Settings(
        cache={"directory": tmp_path},
        analysis={"history_years": 1},
        data={"us_equity_feed": "yahoo"},
    )
    request = AnalysisRequest(symbol="AAPL", as_of=AS_OF, timeframe="5m")
    bundle = fetch_bundle(request, settings, provider=FakeYahoo(settings.data, feed="yahoo"))
    assert bundle.instrument.asset_type == "EQUITY"
    assert bundle.provenance.feed == "yahoo"
    assert bundle.bars[-1].session_date == date(2024, 6, 3)
