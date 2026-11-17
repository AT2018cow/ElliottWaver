from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from conftest import make_bar

from eliotbot.config import (
    DataConfig,
    SearchConfig,
    Settings,
    large_degree_settings,
    long_term_settings,
)
from eliotbot.data.calendar import load_calendar
from eliotbot.data.router import route_symbol
from eliotbot.data.sina_provider import SinaProvider
from eliotbot.engine.matcher import search
from eliotbot.engine.zigzag import ZigZagResult
from eliotbot.schemas.market import AnalysisRequest
from eliotbot.schemas.wave import PivotPoint
from eliotbot.service import AnalysisService


def test_xshg_calendar_clamps_pre_1991_start():
    calendar = load_calendar(route_symbol("000001.SZ"), date(1991, 1, 1), date(2026, 11, 16))
    assert calendar.dates[0] == date(1991, 1, 2)
    assert calendar.dates[-1] >= date(2026, 11, 16)


def test_sina_provider_skips_rows_before_calendar_and_warns():
    rows = [
        {
            "date": date(1990, 12, 19),
            "open": 100.0,
            "high": 101.0,
            "low": 99.0,
            "close": 100.0,
            "volume": 1000,
            "amount": 1000.0,
        },
        {
            "date": date(1991, 1, 2),
            "open": 101.0,
            "high": 102.0,
            "low": 100.0,
            "close": 101.0,
            "volume": 1100,
            "amount": 1100.0,
        },
        {
            "date": date(1991, 1, 3),
            "open": 102.0,
            "high": 103.0,
            "low": 101.0,
            "close": 102.0,
            "volume": 1200,
            "amount": 1200.0,
        },
    ]
    factors = [{"date": row["date"], "hfq_factor": 1.0} for row in rows]

    def fetcher(symbol, start, end, deadline, timeframe="1d", asset_type="EQUITY"):
        return {
            "raw": rows,
            "factors": factors,
            "name": "测试股票",
            "intraday": [],
            "version": "test",
        }

    as_of = datetime(1991, 1, 3, 15, 30, tzinfo=ZoneInfo("Asia/Shanghai"))
    provider = SinaProvider(DataConfig(), fetcher=fetcher)
    bundle = provider.fetch(route_symbol("000001.SZ"), date(1990, 12, 1), date(1991, 1, 3), as_of)

    assert bundle.bars[0].session_date == date(1991, 1, 2)
    assert len(bundle.bars) == 2
    assert any(w.startswith("CALENDAR_CLIPPED") for w in bundle.provenance.warnings)


def test_long_term_preset_uses_full_window_and_large_degree_search():
    settings = Settings()
    long_term = long_term_settings(settings)
    assert long_term.analysis.timeframe == "1w"
    assert long_term.analysis.history_years == 20
    assert long_term.analysis.max_analysis_bars == 5000
    assert long_term.analysis.max_anchors == 8
    assert long_term.analysis.anchor_mode == "spread"
    assert long_term.analysis.top_k == 8
    assert long_term.search.max_bridge_intervals == 21
    assert long_term.search.beam_width == 512
    assert long_term.search.max_expansions == 200000

    large = large_degree_settings(settings)
    assert large.analysis == settings.analysis
    assert large.search.max_bridge_intervals == 21


def test_analysis_window_truncation_is_visible(bundle):
    result = AnalysisService(Settings(analysis={"max_analysis_bars": 60})).analyze_bundle(
        AnalysisRequest(symbol="AAPL", as_of=bundle.as_of), bundle
    )
    assert any(w.startswith("ANALYSIS_WINDOW_TRUNCATED") for w in result.warnings)


def test_data_start_clipped_is_visible(bundle):
    request = AnalysisRequest(
        symbol="AAPL",
        as_of=bundle.as_of,
        start=bundle.bars[0].session_date - timedelta(days=10),
    )
    result = AnalysisService().analyze_bundle(request, bundle)
    assert any(w.startswith("DATA_START_CLIPPED") for w in result.warnings)


def test_allow_gaps_continues_with_explicit_warning(bundle):
    values = bundle.model_dump()
    values["bars"] = bundle.bars[:210] + bundle.bars[211:]
    values["raw_bars"] = bundle.raw_bars[:210] + bundle.raw_bars[211:]
    changed = type(bundle)(**values)

    blocked = AnalysisService().analyze_bundle(
        AnalysisRequest(symbol="AAPL", as_of=changed.as_of), changed
    )
    allowed = AnalysisService().analyze_bundle(
        AnalysisRequest(symbol="AAPL", as_of=changed.as_of, allow_gaps=True), changed
    )

    assert blocked.status == "INSUFFICIENT_DATA"
    assert any(w.startswith("UNKNOWN_SESSION_GAPS") for w in blocked.warnings)
    assert allowed.status != "INSUFFICIENT_DATA"
    assert any(w.startswith("ALLOWED_GAPS") for w in allowed.warnings)


def test_large_degree_bridge_finds_wide_impulse():
    prices = [100, 115, 103, 118, 108, 119, 112, 120, 110, 150, 135, 160]
    bars = tuple(make_bar(index, price) for index, price in enumerate(prices))
    points = tuple(
        PivotPoint(
            pivot_id=f"p{index}",
            index=index,
            extreme_bar_id=bars[index].bar_id,
            extreme_at=bars[index].start_at,
            confirmed_at=bars[index].end_at,
            price=price,
            kind="LOW" if index % 2 == 0 else "HIGH",
            state="CONFIRMED",
            scale=1.4,
            threshold_at_extreme=1,
        )
        for index, price in enumerate(prices)
    )
    zigzag = ZigZagResult(points, points[-1])
    anchors = [bars[0].session_date]

    narrow, _ = search(bars, [zigzag], anchors, SearchConfig(max_bridge_intervals=5))
    wide, _ = search(bars, [zigzag], anchors, SearchConfig(max_bridge_intervals=11))

    def has_wide_impulse(candidates):
        return any(
            candidate.template.name == "IMPULSE"
            and [point.index for point in candidate.completed] == [0, 7, 8, 9, 10, 11]
            for candidate in candidates
        )

    assert not has_wide_impulse(narrow)
    assert has_wide_impulse(wide)
