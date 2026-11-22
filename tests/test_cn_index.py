from datetime import date, datetime
from zoneinfo import ZoneInfo

import pytest

from elliottwaver.config import DataConfig, Settings
from elliottwaver.data.router import route_symbol
from elliottwaver.data.sina_provider import SinaProvider
from elliottwaver.data.snapshots import SnapshotStore
from elliottwaver.data.workflow import fetch_bundle
from elliottwaver.errors import DataError
from elliottwaver.schemas.market import AnalysisRequest
from elliottwaver.service import prepare_bundle

DATES = (date(2024, 1, 2), date(2024, 1, 3), date(2024, 1, 4), date(2024, 1, 5))
AS_OF = datetime(2024, 1, 5, 15, 30, tzinfo=ZoneInfo("Asia/Shanghai"))
RAW = [
    {
        "date": day,
        "open": 100.0 + index,
        "high": 102.0 + index,
        "low": 99.0 + index,
        "close": 101.0 + index,
        "volume": 1000 + index * 100,
    }
    for index, day in enumerate(DATES)
]
STOCK_RAW = [{**row, "amount": 5000.0} for row in RAW]
FACTORS = [{"date": day, "hfq_factor": 1.0} for day in DATES]


def _index_fetcher(calls):
    def fetch(symbol, start, end, deadline, timeframe="1d", asset_type="EQUITY"):
        calls.append((symbol, start, end, timeframe, asset_type))
        return {
            "raw": RAW,
            "factors": [],
            "name": "测试指数",
            "intraday": [],
            "version": "test",
        }

    return fetch


def _stock_fetcher(calls):
    def fetch(symbol, start, end, deadline, timeframe="1d", asset_type="EQUITY"):
        calls.append((symbol, start, end, timeframe, asset_type))
        return {
            "raw": STOCK_RAW,
            "factors": FACTORS,
            "name": "测试股票",
            "intraday": [],
            "version": "test",
        }

    return fetch


def test_sina_index_daily_bundle_has_no_adjustment():
    calls = []
    provider = SinaProvider(DataConfig(), fetcher=_index_fetcher(calls))
    instrument = route_symbol("000688.SH")
    assert instrument.asset_type == "INDEX"

    bundle = provider.fetch(instrument, DATES[0], DATES[-1], AS_OF)

    assert calls == [("sh000688", DATES[0], DATES[-1], "1d", "INDEX")]
    assert bundle.instrument.asset_type == "INDEX"
    assert bundle.instrument.name == "测试指数"
    assert bundle.instrument.identity_verified
    assert bundle.provenance.provider == "sina-index"
    assert bundle.provenance.feed == "sina-index"
    assert bundle.provenance.adjustment == "none"
    assert bundle.provenance.factor_hash == ""
    assert bundle.provenance.volume_unit == "index_volume"
    assert bundle.factors == ()
    assert bundle.bars == bundle.raw_bars
    assert len(bundle.bars) == 4
    assert bundle.bars[-1].session_date == DATES[-1]
    assert bundle.bars[0].amount is None
    assert "指数无复权因子" in bundle.provenance.warnings[0]


def test_sina_index_intraday_is_rejected_before_fetch():
    calls = []
    provider = SinaProvider(DataConfig(), fetcher=_index_fetcher(calls))
    with pytest.raises(DataError, match="INDEX_INTRADAY_UNSUPPORTED"):
        provider.fetch(route_symbol("000688.SH"), DATES[0], DATES[-1], AS_OF, timeframe="5m")
    assert calls == []


def test_sina_stock_path_remains_qfq():
    calls = []
    provider = SinaProvider(DataConfig(), fetcher=_stock_fetcher(calls))
    bundle = provider.fetch(route_symbol("600519.SH"), DATES[0], DATES[-1], AS_OF)

    assert calls[0][-1] == "EQUITY"
    assert bundle.provenance.provider == "sina"
    assert bundle.provenance.feed == "sina"
    assert bundle.provenance.adjustment == "basis_date_qfq"
    assert bundle.provenance.factor_hash
    assert len(bundle.factors) == 4
    assert bundle.bars[0].amount == 5000.0


def test_fetch_bundle_uses_sina_index_feed_and_cache(tmp_path):
    calls = []
    settings = Settings(cache={"directory": tmp_path}, analysis={"history_years": 1})
    request = AnalysisRequest(symbol="000688.SH", as_of=AS_OF, timeframe="1d")
    provider = SinaProvider(settings.data, fetcher=_index_fetcher(calls))

    bundle = fetch_bundle(request, settings, provider=provider)

    assert bundle.provenance.feed == "sina-index"
    assert bundle.instrument.asset_type == "INDEX"
    assert len(list((tmp_path / "snapshots").iterdir())) == 1
    fetched = len(calls)
    cached = fetch_bundle(request, settings, provider=provider)
    assert cached.provenance.feed == "sina-index"
    assert len(calls) == fetched


def test_prepare_bundle_aggregates_index_weekly_without_adjustment():
    provider = SinaProvider(DataConfig(), fetcher=_index_fetcher([]))
    bundle = provider.fetch(route_symbol("000688.SH"), DATES[0], DATES[-1], AS_OF)
    request = AnalysisRequest(symbol="000688.SH", as_of=AS_OF, timeframe="1w")

    prepared = prepare_bundle(request, bundle)

    assert prepared.timeframe == "1w"
    assert prepared.provenance.adjustment == "none"
    assert len(prepared.bars) == 1
    assert prepared.bars[-1].session_date == DATES[-1]
    assert len(prepared.daily_context) == 4


def test_index_snapshot_round_trip(tmp_path):
    provider = SinaProvider(DataConfig(), fetcher=_index_fetcher([]))
    bundle = provider.fetch(route_symbol("000688.SH"), DATES[0], DATES[-1], AS_OF)
    store = SnapshotStore(tmp_path)

    snapshot_id = store.save(bundle)
    loaded = store.load(snapshot_id)

    assert loaded.instrument.asset_type == "INDEX"
    assert loaded.provenance.feed == "sina-index"
    assert loaded.snapshot_id == snapshot_id
