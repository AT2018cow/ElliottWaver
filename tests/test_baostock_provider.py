import sys
from datetime import date, datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from elliottwaver.config import DataConfig, Settings
from elliottwaver.data.baostock_provider import BaoStockProvider
from elliottwaver.data.router import route_symbol
from elliottwaver.data.workflow import fetch_bundle
from elliottwaver.errors import DataError
from elliottwaver.schemas.market import AnalysisRequest

CST = ZoneInfo("Asia/Shanghai")
AS_OF = datetime(2024, 6, 3, 15, 5, tzinfo=CST)


class _Query:
    def __init__(self, rows):
        self.rows = rows
        self.index = -1
        self.error_code = "0"
        self.error_msg = "success"

    def next(self):
        self.index += 1
        return self.index < len(self.rows)

    def get_row_data(self):
        return self.rows[self.index]


def _fake_baostock(monkeypatch):
    rows = [
        [
            "2024-06-03",
            "20240603103000000",
            "sh.600519",
            "100",
            "102",
            "99",
            "101",
            "1000",
            "100000",
            "3",
        ],
        [
            "2024-06-03",
            "20240603113000000",
            "sh.600519",
            "101",
            "103",
            "100",
            "102",
            "1100",
            "110000",
            "3",
        ],
        [
            "2024-06-03",
            "20240603140000000",
            "sh.600519",
            "102",
            "104",
            "101",
            "103",
            "1200",
            "120000",
            "3",
        ],
        [
            "2024-06-03",
            "20240603150000000",
            "sh.600519",
            "103",
            "105",
            "102",
            "104",
            "1300",
            "130000",
            "3",
        ],
    ]
    factors = [["sh.600519", "2024-01-01", "1.0", "7.0", "1.0"]]
    module = SimpleNamespace(
        __version__="0.9.4",
        login=lambda: SimpleNamespace(error_code="0", error_msg="success"),
        logout=lambda: SimpleNamespace(error_code="0", error_msg="success"),
        query_stock_basic=lambda code: _Query([["sh.600519", "贵州茅台"]]),
        query_history_k_data_plus=lambda *args, **kwargs: _Query(rows),
        query_adjust_factor=lambda *args, **kwargs: _Query(factors),
    )
    monkeypatch.setitem(sys.modules, "baostock", module)
    return module


def test_baostock_60m_uses_history_only_qfq(monkeypatch):
    _fake_baostock(monkeypatch)
    provider = BaoStockProvider(DataConfig(cn_equity_intraday_feed="baostock"))
    bundle = provider.fetch(
        route_symbol("600519.SH"),
        date(2024, 6, 3),
        date(2024, 6, 3),
        AS_OF,
        timeframe="60m",
    )
    assert len(bundle.bars) == 4
    assert bundle.provenance.feed == "baostock"
    assert bundle.provenance.adjustment == "basis_date_qfq"
    assert bundle.provenance.session_profile == "cn_baostock_history"
    assert bundle.factors
    assert bundle.instrument.name == "贵州茅台"
    assert bundle.expected_bar_starts[-1] == bundle.bars[-1].start_at


def test_baostock_1m_is_rejected_before_login(monkeypatch):
    _fake_baostock(monkeypatch)
    provider = BaoStockProvider(DataConfig(cn_equity_intraday_feed="baostock"))
    with pytest.raises(DataError, match="BAOSTOCK_1M_UNSUPPORTED"):
        provider.fetch(
            route_symbol("600519.SH"),
            date(2024, 6, 3),
            date(2024, 6, 3),
            AS_OF,
            timeframe="1m",
        )


def test_workflow_routes_cn_equity_to_baostock(monkeypatch, tmp_path):
    _fake_baostock(monkeypatch)
    settings = Settings(
        cache={"directory": tmp_path},
        analysis={"history_years": 1},
        data={"cn_equity_intraday_feed": "baostock"},
    )
    request = AnalysisRequest(symbol="600519.SH", as_of=AS_OF, timeframe="60m")
    bundle = fetch_bundle(
        request,
        settings,
        provider=BaoStockProvider(settings.data),
    )
    assert bundle.provenance.feed == "baostock"
    assert bundle.bars[-1].session_date == date(2024, 6, 3)
