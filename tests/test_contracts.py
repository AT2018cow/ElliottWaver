from datetime import timedelta

import pytest
from conftest import make_bar
from pydantic import ValidationError

from elliottwaver.config import Settings, load_settings
from elliottwaver.data.adjustment import adjust_to_basis
from elliottwaver.data.quality import normalize_bars
from elliottwaver.data.router import route_symbol
from elliottwaver.errors import DataError, InputError
from elliottwaver.schemas.market import AnalysisRequest, Bar, FactorPoint, MarketDataBundle


@pytest.mark.parametrize(
    "symbol, expected",
    [
        ("600519", "600519.SH"),
        ("SH.600519", "600519.SH"),
        ("000001.SZ", "000001.SZ"),
        ("bj920001", "920001.BJ"),
        ("brk-b", "BRK.B"),
    ],
)
def test_symbol_routing(symbol, expected):
    assert route_symbol(symbol).canonical_symbol == expected


@pytest.mark.parametrize(
    "symbol, canonical, asset_type",
    [
        ("000688.SH", "000688.SH", "INDEX"),
        ("sh000300", "000300.SH", "INDEX"),
        ("000001.SH", "000001.SH", "INDEX"),
        ("399001", "399001.SZ", "INDEX"),
        ("sz399006", "399006.SZ", "INDEX"),
        ("899050", "899050.BJ", "INDEX"),
        ("000001", "000001.SZ", "EQUITY"),
        ("000001.SZ", "000001.SZ", "EQUITY"),
        ("688981.SH", "688981.SH", "EQUITY"),
        ("600519.SH", "600519.SH", "EQUITY"),
    ],
)
def test_index_and_equity_routing(symbol, canonical, asset_type):
    instrument = route_symbol(symbol)
    assert instrument.canonical_symbol == canonical
    assert instrument.asset_type == asset_type


@pytest.mark.parametrize("symbol", ["399001.SH", "880001", "SH.880001", "000688.BJ"])
def test_invalid_index_exchange_ranges(symbol):
    with pytest.raises(InputError):
        route_symbol(symbol)


@pytest.mark.parametrize("symbol", ["sh600519.SZ", "600519.SZ", "12345", "", "AAPL/USD", "$(id)"])
def test_invalid_symbols(symbol):
    with pytest.raises(InputError):
        route_symbol(symbol)


@pytest.mark.parametrize(
    "update",
    [
        {"close": float("nan")},
        {"high": float("inf")},
        {"volume": -1},
        {"low": 12},
        {"start_at": "2024-01-01T09:00:00"},
    ],
)
def test_invalid_bar_values_rejected(update):
    values = make_bar(0, 10).model_dump()
    with pytest.raises(ValidationError):
        Bar(**{**values, **update})


def test_unknown_schema_and_future_bars_rejected(bundle):
    values = bundle.model_dump()
    with pytest.raises(ValidationError):
        MarketDataBundle(**{**values, "schema_version": "9.0"})
    with pytest.raises(ValidationError):
        MarketDataBundle(**{**values, "as_of": bundle.bars[-1].end_at - timedelta(seconds=1)})


def test_adjustment_direction_units_and_missing_head():
    raw = (make_bar(0, 100, volume=1000), make_bar(2, 50, volume=2000))
    factors = (
        FactorPoint(effective_date=raw[0].session_date, factor=1),
        FactorPoint(effective_date=raw[0].session_date + timedelta(days=1), factor=2),
    )
    result = adjust_to_basis(raw, factors)
    assert [b.close for b in result] == [50, 50]
    assert [b.volume for b in result] == [1000, 2000]
    with pytest.raises(DataError, match="Missing adjustment"):
        adjust_to_basis(raw, factors[1:])


def test_dedupe_preserves_different_dates_and_rejects_conflicts():
    first, second = make_bar(0, 10), make_bar(1, 10)
    assert len(normalize_bars([first, second, first])) == 2
    with pytest.raises(DataError):
        normalize_bars([first, first.model_copy(update={"close": 9, "low": 9})])


def test_config_extra_fields_and_environment_precedence(tmp_path, monkeypatch):
    with pytest.raises(ValidationError):
        Settings(typo=True)
    path = tmp_path / "config.yaml"
    path.write_text("analysis:\n  top_k: 2\n  warmup_bars: 150\n")
    monkeypatch.setenv("ELLIOTTWAVER_ANALYSIS__TOP_K", "4")
    settings = load_settings(path)
    assert settings.analysis.top_k == 4
    assert settings.analysis.warmup_bars == 150


def test_scoring_profiles_are_explicit_and_part_of_config_identity():
    current = Settings()
    legacy = Settings(scoring={"profile": "ew_score_v1"})
    assert current.scoring.profile == "ew_score_v2"
    assert current.computation_hash != legacy.computation_hash
    with pytest.raises(ValidationError):
        Settings(scoring={"profile": "unknown"})


def test_request_range(bundle):
    with pytest.raises(ValidationError):
        AnalysisRequest(symbol="AAPL", as_of=bundle.as_of, start="2024-02-01", end="2024-01-01")
