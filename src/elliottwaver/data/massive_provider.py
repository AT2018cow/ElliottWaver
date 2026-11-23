"""Massive native US index aggregate adapter.

Massive exposes index aggregates under the `I:` ticker prefix. The endpoint
returns index values, not traded volume; this adapter preserves that limitation
instead of inventing volume evidence.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

import requests

from elliottwaver.config import DataConfig, massive_api_key
from elliottwaver.data.calendar import bar_window, load_calendar
from elliottwaver.data.periods import as_weekly, expected_starts, target_session
from elliottwaver.data.quality import normalize_bars
from elliottwaver.errors import DataError, SourceError
from elliottwaver.schemas.market import Bar, Instrument, MarketDataBundle, Provenance
from elliottwaver.timeframes import minutes
from elliottwaver.util import digest

_API = "https://api.massive.com"


class MassiveIndexProvider:
    def __init__(self, config: DataConfig, *, feed: str = "massive-index"):
        if feed != "massive-index":
            raise ValueError(f"unsupported Massive feed {feed}")
        self.config = config
        self.feed = feed

    def _request(self, path: str, params: dict) -> dict:
        try:
            response = requests.get(
                f"{_API}{path}",
                params={**params, "apiKey": massive_api_key()},
                timeout=(10, self.config.request_timeout_seconds),
            )
            response.raise_for_status()
        except requests.HTTPError as exc:
            status = exc.response.status_code if exc.response is not None else None
            if status in {401, 403}:
                raise SourceError(f"MASSIVE_AUTH: index access denied (HTTP {status})") from None
            raise SourceError(f"MASSIVE_HTTP: index request failed (HTTP {status})") from None
        except requests.RequestException:
            raise SourceError("MASSIVE_HTTP: index transport failed") from None
        try:
            payload = response.json()
        except ValueError:
            raise SourceError("MASSIVE_RESPONSE: invalid index response") from None
        if not isinstance(payload, dict):
            raise SourceError("MASSIVE_RESPONSE: invalid index response")
        if payload.get("status") not in {None, "OK"}:
            raise SourceError("MASSIVE_RESPONSE: index request failed")
        return payload

    def _rows(
        self,
        symbol: str,
        start: date,
        end: date,
        *,
        multiplier: int,
        timespan: str,
    ) -> list[dict]:
        payload = self._request(
            f"/v2/aggs/ticker/I:{symbol}/range/{multiplier}/{timespan}/"
            f"{start.isoformat()}/{end.isoformat()}",
            {"sort": "asc", "limit": 50000},
        )
        return payload.get("results") or []

    def _daily(
        self,
        instrument: Instrument,
        calendar,
        start: date,
        end: date,
        as_of: datetime,
    ) -> tuple[Bar, ...]:
        rows = self._rows(instrument.canonical_symbol, start, end, multiplier=1, timespan="day")
        observed = datetime.now(UTC)
        sessions = {session.date: session for session in calendar.sessions}
        zone = ZoneInfo(instrument.timezone)
        bars = []
        for row in rows:
            moment = datetime.fromtimestamp(row["t"] / 1000, UTC).astimezone(zone)
            session = sessions.get(moment.date())
            if session is None:
                continue
            opened, closed, available = bar_window(
                session, "US", self.config.delayed_access_buffer_minutes
            )
            if available > as_of:
                continue
            bars.append(
                Bar(
                    start_at=opened,
                    end_at=closed,
                    available_at=available,
                    observed_at=observed,
                    session_date=session.date,
                    open=float(row["o"]),
                    high=float(row["h"]),
                    low=float(row["l"]),
                    close=float(row["c"]),
                    volume=0.0,
                    amount=None,
                    source_revision=digest(row),
                )
            )
        return normalize_bars(bars)

    def fetch(
        self,
        instrument: Instrument,
        start: date,
        end: date,
        as_of: datetime,
        timeframe: str = "1d",
    ) -> MarketDataBundle:
        if instrument.market != "US" or instrument.asset_type != "INDEX":
            raise DataError("Massive index provider requires a US index instrument")
        calendar = load_calendar(instrument, start, end)
        target = target_session(
            calendar,
            instrument,
            as_of,
            timeframe,
            end,
            self.config.delayed_access_buffer_minutes,
        )
        daily = self._daily(instrument, calendar, start, target, as_of)
        if timeframe == "1w":
            if not daily:
                raise DataError("Massive returned no daily index bars")
            bundle = self._bundle(
                instrument,
                daily,
                daily,
                (),
                calendar,
                target,
                as_of,
                timeframe="1d",
                source_timeframe="1d",
                aggregation="none",
            )
            return as_weekly(bundle)
        if timeframe == "1d":
            if not daily or daily[-1].session_date != target:
                raise DataError("STALE_DATA: Massive lacks the required index session")
            return self._bundle(
                instrument,
                daily,
                daily,
                (),
                calendar,
                target,
                as_of,
                timeframe="1d",
                source_timeframe="1d",
                aggregation="none",
            )
        duration = minutes(timeframe)
        if duration is None:
            raise DataError("Massive intraday timeframe required")
        rows = self._rows(
            instrument.canonical_symbol,
            start,
            target,
            multiplier=duration,
            timespan="minute",
        )
        observed = datetime.now(UTC)
        zone = ZoneInfo(instrument.timezone)
        sessions = {session.date: session for session in calendar.sessions}
        bars = []
        for row in rows:
            start_at = datetime.fromtimestamp(row["t"] / 1000, UTC)
            end_at = start_at + timedelta(minutes=duration)
            session = sessions.get(start_at.astimezone(zone).date())
            if session is None:
                continue
            if start_at < session.open_at or end_at > session.close_at:
                continue
            available = end_at + timedelta(minutes=self.config.delayed_access_buffer_minutes)
            if available > as_of:
                continue
            bars.append(
                Bar(
                    start_at=start_at,
                    end_at=end_at,
                    available_at=available,
                    observed_at=observed,
                    session_date=session.date,
                    open=float(row["o"]),
                    high=float(row["h"]),
                    low=float(row["l"]),
                    close=float(row["c"]),
                    volume=0.0,
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
            buffer_minutes=self.config.delayed_access_buffer_minutes,
            through=target,
            since=start,
        )
        if not bars:
            raise DataError("Massive returned no intraday index bars")
        if not expected or expected[-1] != bars[-1].start_at:
            raise DataError("STALE_INTRADAY: Massive lacks the required completed bar")
        daily_context = tuple(
            bar for bar in daily if bar.usable_at <= as_of and bar.session_date <= target
        )
        return self._bundle(
            instrument,
            bars,
            bars,
            daily_context,
            calendar,
            target,
            as_of,
            timeframe=timeframe,
            source_timeframe=timeframe,
            aggregation="none",
            expected=expected,
        )

    def _bundle(
        self,
        instrument: Instrument,
        bars: tuple[Bar, ...],
        raw_bars: tuple[Bar, ...],
        daily_context: tuple[Bar, ...],
        calendar,
        target: date,
        as_of: datetime,
        *,
        timeframe: str,
        source_timeframe: str,
        aggregation: str,
        expected: tuple[datetime, ...] = (),
    ) -> MarketDataBundle:
        return MarketDataBundle(
            instrument=instrument.model_copy(update={"identity_verified": True}),
            timeframe=timeframe,
            as_of=as_of,
            bars=bars,
            raw_bars=raw_bars,
            sessions=calendar.dates,
            daily_context=daily_context,
            raw_daily_context=daily_context,
            expected_bar_starts=expected,
            provenance=Provenance(
                provider="massive",
                feed=self.feed,
                adjustment="none",
                basis_date=bars[-1].session_date,
                price_coordinate_id=digest(
                    ["massive-index", self.feed, target, [bar.bar_id for bar in bars]]
                ),
                parser_version="massive-v1",
                source_timeframe=source_timeframe,
                aggregation=aggregation,
                coverage_start=bars[0].session_date,
                coverage_end=bars[-1].session_date,
                coverage_reason="MASSIVE_INDEX_HISTORY",
                session_profile="us_index_rth",
                calendar_version=calendar.version,
                fetched_at=datetime.now(UTC),
                requested_start=bars[0].session_date,
                requested_end=target,
                amount_currency="USD",
                volume_unit="index_volume",
                volume_comparable=False,
                warnings=(
                    "MASSIVE_INDEX_NO_VOLUME: 指数聚合不提供成交量，量价证据不可比",
                    "MASSIVE_INDEX_IDENTITY_TICKER_ONLY: 身份由 I: 前缀和代码确认",
                ),
            ),
        )
