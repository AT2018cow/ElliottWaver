"""Tencent free minute K-line adapter for CN indices.

Tencent's public `mkline` endpoint returns up to 320 bars ending before a
supplied timestamp. The adapter pages backwards and maps bar-end labels onto
the exchange session grid. It is an unofficial free source: no key is needed,
but there is no SLA and volume units are not certified.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

import requests

from elliottwaver.config import DataConfig
from elliottwaver.data.calendar import load_calendar
from elliottwaver.data.periods import expected_starts, intraday_windows, target_session
from elliottwaver.data.quality import normalize_bars
from elliottwaver.errors import DataError, SourceError
from elliottwaver.schemas.market import Bar, Instrument, MarketDataBundle, Provenance
from elliottwaver.timeframes import minutes
from elliottwaver.util import digest

_API = "https://ifzq.gtimg.cn/appstock/app/kline/mkline"
_PAGE = 320
_PERIOD = {"1m": "m1", "5m": "m5", "15m": "m15", "30m": "m30", "60m": "m60"}


class TencentProvider:
    def __init__(self, config: DataConfig, *, feed: str = "tencent-cn-index"):
        if feed != "tencent-cn-index":
            raise ValueError(f"unsupported Tencent feed {feed}")
        self.config = config
        self.feed = feed

    def _request(self, code: str, period: str, end_marker: str, count: int = _PAGE) -> dict:
        response = requests.get(
            _API,
            params={"param": f"{code},{period},{end_marker},{count}"},
            timeout=(10, self.config.request_timeout_seconds),
        )
        response.raise_for_status()
        payload = response.json()
        if payload.get("code") != 0:
            raise SourceError(f"TENCENT_{payload.get('code')}: minute request failed")
        return payload

    def fetch(
        self,
        instrument: Instrument,
        start: date,
        end: date,
        as_of: datetime,
        timeframe: str = "1d",
    ) -> MarketDataBundle:
        if instrument.market != "CN" or instrument.asset_type != "INDEX":
            raise DataError("Tencent provider currently requires a CN index instrument")
        period = _PERIOD.get(timeframe)
        if period is None:
            raise DataError("Tencent provider requires an intraday timeframe")
        calendar = load_calendar(instrument, start, end)
        target = target_session(calendar, instrument, as_of, timeframe, end, 0)
        zone = ZoneInfo(instrument.timezone)
        windows = {}
        for session in calendar.sessions:
            for opened, closed in intraday_windows(session, minutes(timeframe)):
                windows[closed] = (opened, session.date)
        observed = datetime.now(UTC)
        rows: dict[str, list] = {}
        first_data = None
        marker = (
            calendar.session(target).close_at.astimezone(zone) + timedelta(minutes=1)
        ).strftime("%Y%m%d%H%M")
        pages = 0
        while pages < self.config.tencent_max_pages:
            payload = self._request(instrument.provider_symbol, period, marker)
            data = (payload.get("data") or {}).get(instrument.provider_symbol) or {}
            if first_data is None:
                first_data = data
            page = data.get(period) or []
            if not page:
                break
            for row in page:
                rows[str(row[0])] = row
            pages += 1
            earliest = min(str(row[0]) for row in page)
            if len(page) < _PAGE or earliest <= start.strftime("%Y%m%d0000"):
                break
            marker = earliest
        bars = []
        for row in rows.values():
            end_at = (
                datetime.strptime(str(row[0]), "%Y%m%d%H%M").replace(tzinfo=zone).astimezone(UTC)
            )
            match = windows.get(end_at)
            if match is None:
                continue
            opened, session_day = match
            if session_day < start or session_day > target or end_at > as_of:
                continue
            bars.append(
                Bar(
                    start_at=opened,
                    end_at=end_at,
                    available_at=end_at,
                    observed_at=observed,
                    session_date=session_day,
                    open=float(row[1]),
                    high=float(row[3]),
                    low=float(row[4]),
                    close=float(row[2]),
                    volume=float(row[5]),
                    amount=None,
                    source_revision=digest(row),
                )
            )
        bars = normalize_bars(bars)
        expected = expected_starts(
            calendar,
            instrument,
            as_of,
            timeframe,
            buffer_minutes=0,
            through=target,
            since=start,
        )
        if not bars or not expected:
            raise DataError("TENCENT_NO_BARS: no completed index bars")
        expected = tuple(item for item in expected if item <= bars[-1].start_at)
        if not expected or expected[-1] != bars[-1].start_at:
            raise DataError("STALE_INTRADAY: Tencent index bars do not align to the session grid")
        name = ""
        if first_data is not None:
            name = (first_data.get("qt") or {}).get(instrument.provider_symbol, [None, ""])[1] or ""
        verified = instrument.model_copy(
            update={"name": name or instrument.name, "identity_verified": bool(name)}
        )
        provenance = Provenance(
            provider="tencent",
            feed=self.feed,
            adjustment="none",
            basis_date=bars[-1].session_date,
            price_coordinate_id=digest(
                ["tencent-index", self.feed, target, [bar.bar_id for bar in bars]]
            ),
            parser_version="tencent-mkline-v1",
            source_timeframe=timeframe,
            aggregation="none",
            coverage_start=bars[0].session_date,
            coverage_end=bars[-1].session_date,
            coverage_reason="TENCENT_BACKWARD_PAGES",
            session_profile="cn_exchange_session",
            calendar_version=calendar.version,
            fetched_at=observed,
            requested_start=start,
            requested_end=end,
            volume_unit="index_volume",
            amount_currency="CNY",
            volume_comparable=False,
            warnings=(
                "TENCENT_UNOFFICIAL_FREE_SOURCE: 免费公开接口，无 SLA",
                "TENCENT_VOLUME_UNIT_UNVERIFIED: 指数成交量单位未认证，量价证据不可比",
            ),
        )
        return MarketDataBundle(
            instrument=verified,
            as_of=as_of,
            timeframe=timeframe,
            bars=bars,
            raw_bars=bars,
            sessions=calendar.dates,
            expected_bar_starts=expected,
            provenance=provenance,
        )
