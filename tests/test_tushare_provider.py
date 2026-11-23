from datetime import date, datetime
from zoneinfo import ZoneInfo

from elliottwaver.config import DataConfig, Settings
from elliottwaver.data.router import route_symbol
from elliottwaver.data.tushare_provider import TushareProvider
from elliottwaver.data.workflow import fetch_bundle
from elliottwaver.schemas.market import AnalysisRequest

AS_OF = datetime(2024, 6, 3, 15, 5, tzinfo=ZoneInfo("Asia/Shanghai"))


def _minute_rows(symbol: str) -> list[dict]:
    times = ("10:30:00", "11:30:00", "14:00:00", "15:00:00")
    return [
        {
            "ts_code": symbol,
            "trade_time": f"2024-06-03 {moment}",
            "open": 100.0 + index,
            "high": 101.0 + index,
            "low": 99.0 + index,
            "close": 100.5 + index,
            "vol": 1000.0 + index,
            "amount": 100000.0 + index,
        }
        for index, moment in enumerate(times)
    ]


class FakeTushare(TushareProvider):
    def __init__(self, feed: str):
        super().__init__(DataConfig(cn_index_intraday_feed="tushare-cn-index"), feed=feed)

    def _request(self, api_name: str, params: dict, fields: str) -> list[dict]:
        if api_name == "idx_mins":
            return _minute_rows("000300.SH")
        if api_name == "stock_mins":
            return _minute_rows("600519.SH")
        if api_name == "adj_factor":
            return [{"ts_code": "600519.SH", "trade_date": "20240603", "adj_factor": 1.0}]
        raise AssertionError(api_name)


def test_tushare_index_60m_builds_four_completed_windows():
    provider = FakeTushare("tushare-cn-index")
    bundle = provider.fetch(
        route_symbol("000300.SH"), date(2024, 6, 3), date(2024, 6, 3), AS_OF, timeframe="60m"
    )
    assert len(bundle.bars) == 4
    assert [bar.start_at.hour for bar in bundle.bars] == [1, 2, 5, 6]
    assert bundle.provenance.feed == "tushare-cn-index"
    assert bundle.provenance.source_timeframe == "60m"
    assert bundle.provenance.adjustment == "none"
    assert bundle.provenance.coverage_start == date(2024, 6, 3)
    assert bundle.expected_bar_starts[-1] == bundle.bars[-1].start_at


def test_tushare_equity_60m_applies_basis_date_factors():
    provider = FakeTushare("tushare-cn-equity")
    bundle = provider.fetch(
        route_symbol("600519.SH"), date(2024, 6, 3), date(2024, 6, 3), AS_OF, timeframe="60m"
    )
    assert bundle.provenance.feed == "tushare-cn-equity"
    assert bundle.provenance.adjustment == "basis_date_qfq"
    assert bundle.factors
    assert bundle.bars == bundle.raw_bars


def test_workflow_routes_cn_index_intraday_to_configured_tushare_feed(tmp_path):
    settings = Settings(
        cache={"directory": tmp_path},
        analysis={"history_years": 1},
        data={"cn_index_intraday_feed": "tushare-cn-index"},
    )
    request = AnalysisRequest(symbol="000300.SH", as_of=AS_OF, timeframe="60m")
    provider = FakeTushare("tushare-cn-index")
    bundle = fetch_bundle(request, settings, provider=provider)
    assert bundle.timeframe == "60m"
    assert bundle.provenance.feed == "tushare-cn-index"
    assert bundle.bars[-1].session_date == date(2024, 6, 3)
