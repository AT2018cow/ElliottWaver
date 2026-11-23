from datetime import date, datetime
from zoneinfo import ZoneInfo

import pandas as pd

from elliottwaver.config import DataConfig, Settings
from elliottwaver.data.router import route_symbol
from elliottwaver.data.tencent_provider import TencentProvider
from elliottwaver.data.workflow import fetch_bundle
from elliottwaver.schemas.market import AnalysisRequest

CST = ZoneInfo("Asia/Shanghai")


class FakeTencent(TencentProvider):
    def _request(self, code: str, period: str, end_marker: str, count: int = 320) -> dict:
        if period == "m60":
            rows = [
                ["202406031030", "3900", "3910", "3915", "3895", "1000", {}, "1.0"],
                ["202406031130", "3910", "3918", "3920", "3905", "1100", {}, "1.1"],
                ["202406031400", "3918", "3925", "3930", "3915", "1200", {}, "1.2"],
                ["202406031500", "3925", "3932", "3938", "3920", "1300", {}, "1.3"],
            ]
        else:
            rows = [
                ["202406030935", "3900", "3902", "3904", "3898", "100", {}, "0.1"],
                ["202406030940", "3902", "3905", "3907", "3900", "110", {}, "0.11"],
                ["202406030945", "3905", "3908", "3910", "3903", "120", {}, "0.12"],
            ]
        return {
            "code": 0,
            "data": {
                code: {
                    period: rows,
                    "qt": {code: [None, "测试指数"]},
                }
            },
        }


def test_tencent_index_60m_maps_bar_end_labels_to_windows():
    provider = FakeTencent(DataConfig(cn_index_intraday_feed="tencent-cn-index"))
    bundle = provider.fetch(
        route_symbol("000300.SH"),
        date(2024, 6, 3),
        date(2024, 6, 3),
        datetime(2024, 6, 3, 15, 5, tzinfo=CST),
        timeframe="60m",
    )
    assert len(bundle.bars) == 4
    assert [bar.start_at.hour for bar in bundle.bars] == [1, 2, 5, 6]
    assert bundle.provenance.feed == "tencent-cn-index"
    assert bundle.provenance.adjustment == "none"
    assert bundle.provenance.volume_comparable is False
    assert bundle.instrument.identity_verified is True
    assert bundle.instrument.name == "测试指数"


def test_tencent_index_5m_trims_expected_to_last_completed_bar():
    provider = FakeTencent(DataConfig(cn_index_intraday_feed="tencent-cn-index"))
    bundle = provider.fetch(
        route_symbol("000300.SH"),
        date(2024, 6, 3),
        date(2024, 6, 3),
        datetime(2024, 6, 3, 10, 0, tzinfo=CST),
        timeframe="5m",
    )
    assert len(bundle.bars) == 3
    assert bundle.expected_bar_starts[-1] == bundle.bars[-1].start_at


def test_workflow_routes_cn_index_to_tencent_when_configured(tmp_path):
    settings = Settings(
        cache={"directory": tmp_path},
        analysis={"history_years": 1},
        data={"cn_index_intraday_feed": "tencent-cn-index"},
    )
    request = AnalysisRequest(
        symbol="000300.SH",
        as_of=datetime(2024, 6, 3, 15, 5, tzinfo=CST),
        timeframe="60m",
    )
    bundle = fetch_bundle(request, settings, provider=FakeTencent(settings.data))
    assert bundle.instrument.asset_type == "INDEX"
    assert bundle.provenance.feed == "tencent-cn-index"
    assert bundle.bars[-1].session_date == date(2024, 6, 3)


def test_tencent_equity_60m_applies_sina_factors(monkeypatch):
    def factor_frame(**kwargs):
        return pd.DataFrame(
            [
                {"date": "2024-01-01", "hfq_factor": 1.0},
                {"date": "2024-06-03", "hfq_factor": 1.0},
            ]
        )

    import akshare as ak

    monkeypatch.setattr(ak, "stock_zh_a_hist_sina", factor_frame)
    provider = FakeTencent(
        DataConfig(cn_equity_intraday_feed="tencent-cn-equity"), feed="tencent-cn-equity"
    )
    bundle = provider.fetch(
        route_symbol("600519.SH"),
        date(2024, 6, 3),
        date(2024, 6, 3),
        datetime(2024, 6, 3, 15, 5, tzinfo=CST),
        timeframe="60m",
    )
    assert bundle.provenance.feed == "tencent-cn-equity"
    assert bundle.provenance.adjustment == "basis_date_qfq"
    assert bundle.factors
    assert bundle.bars == bundle.raw_bars


def test_workflow_routes_cn_equity_to_tencent(monkeypatch, tmp_path):
    def factor_frame(**kwargs):
        return pd.DataFrame([{"date": "2024-01-01", "hfq_factor": 1.0}])

    import akshare as ak

    monkeypatch.setattr(ak, "stock_zh_a_hist_sina", factor_frame)
    settings = Settings(
        cache={"directory": tmp_path},
        analysis={"history_years": 1},
        data={"cn_equity_intraday_feed": "tencent-cn-equity"},
    )
    request = AnalysisRequest(
        symbol="600519.SH",
        as_of=datetime(2024, 6, 3, 15, 5, tzinfo=CST),
        timeframe="60m",
    )
    bundle = fetch_bundle(
        request,
        settings,
        provider=FakeTencent(settings.data, feed="tencent-cn-equity"),
    )
    assert bundle.provenance.feed == "tencent-cn-equity"
    assert bundle.bars[-1].session_date == date(2024, 6, 3)
