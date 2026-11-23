from datetime import UTC, date, datetime, time
from zoneinfo import ZoneInfo

import pytest
import requests

from elliottwaver.config import DataConfig, Settings
from elliottwaver.data.massive_provider import MassiveIndexProvider
from elliottwaver.data.router import route_symbol
from elliottwaver.data.workflow import fetch_bundle
from elliottwaver.errors import SourceError
from elliottwaver.schemas.market import AnalysisRequest

ET = ZoneInfo("America/New_York")
AS_OF = datetime(2024, 6, 3, 10, 5, tzinfo=ET)


def _ms(day: date, hour: int, minute: int = 0) -> int:
    return int(datetime.combine(day, time(hour, minute), ET).astimezone(UTC).timestamp() * 1000)


class FakeMassive(MassiveIndexProvider):
    def _request(self, path: str, params: dict) -> dict:
        if "/day/" in path or path.endswith("/day/2024-05-31/2024-06-03"):
            return {
                "status": "OK",
                "results": [
                    {"t": _ms(date(2024, 5, 31), 0), "o": 5200, "h": 5250, "l": 5180, "c": 5230},
                    {"t": _ms(date(2024, 6, 3), 0), "o": 5230, "h": 5280, "l": 5210, "c": 5260},
                ],
            }
        return {
            "status": "OK",
            "results": [
                {"t": _ms(date(2024, 6, 3), 9, 30), "o": 5230, "h": 5235, "l": 5228, "c": 5232},
                {"t": _ms(date(2024, 6, 3), 9, 35), "o": 5232, "h": 5238, "l": 5230, "c": 5236},
                {"t": _ms(date(2024, 6, 3), 9, 40), "o": 5236, "h": 5240, "l": 5234, "c": 5238},
            ],
        }


def test_us_index_symbols_route_to_index_asset_type():
    assert route_symbol("SPX").asset_type == "INDEX"
    assert route_symbol("NDX").asset_type == "INDEX"
    assert route_symbol("SPY").asset_type == "EQUITY"


@pytest.mark.parametrize("status", [401, 403, None])
def test_massive_failures_redact_request_credentials(monkeypatch, status):
    key = "fixture-only-do-not-log"

    def get(*args, **kwargs):
        if status is None:
            raise requests.Timeout(key)
        response = requests.Response()
        response.status_code = status
        response.url = f"https://fixture.invalid/index?apiKey={key}"
        response.reason = "Forbidden"
        return response

    monkeypatch.setattr("elliottwaver.data.massive_provider.massive_api_key", lambda: key)
    monkeypatch.setattr("elliottwaver.data.massive_provider.requests.get", get)
    with pytest.raises(SourceError, match="MASSIVE_(HTTP|AUTH)") as error:
        MassiveIndexProvider(DataConfig())._request("/fixture", {})
    assert key not in str(error.value)
    assert error.value.__suppress_context__


def test_massive_index_5m_keeps_native_identity_and_no_volume():
    provider = FakeMassive(DataConfig(us_index_feed="massive-index"))
    bundle = provider.fetch(
        route_symbol("SPX"),
        date(2024, 5, 31),
        date(2024, 6, 3),
        AS_OF,
        timeframe="5m",
    )
    assert len(bundle.bars) == 3
    assert all(bar.volume == 0.0 and bar.amount is None for bar in bundle.bars)
    assert bundle.provenance.feed == "massive-index"
    assert bundle.provenance.adjustment == "none"
    assert bundle.provenance.source_timeframe == "5m"
    assert bundle.provenance.volume_comparable is False
    assert bundle.daily_context
    assert bundle.expected_bar_starts[-1] == bundle.bars[-1].start_at
    assert "MASSIVE_INDEX_NO_VOLUME" in bundle.provenance.warnings[0]


def test_workflow_routes_us_index_to_massive_when_configured(tmp_path):
    settings = Settings(
        cache={"directory": tmp_path},
        analysis={"history_years": 1},
        data={"us_index_feed": "massive-index"},
    )
    request = AnalysisRequest(symbol="SPX", as_of=AS_OF, timeframe="5m")
    bundle = fetch_bundle(request, settings, provider=FakeMassive(settings.data))
    assert bundle.instrument.asset_type == "INDEX"
    assert bundle.provenance.feed == "massive-index"
    assert bundle.bars[-1].session_date == date(2024, 6, 3)
