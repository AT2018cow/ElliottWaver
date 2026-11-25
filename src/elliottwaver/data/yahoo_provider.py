"""Yahoo Finance free chart adapter for native US indices.

Yahoo's public chart endpoint needs no API key but is unofficial. It provides
1m bars for a short recent window and 5m/15m/30m/60m bars for a longer window.
Index volume is not used as comparable evidence.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

from elliottwaver.config import DataConfig
from elliottwaver.data.calendar import bar_window, load_calendar
from elliottwaver.data.periods import aggregate_intraday, expected_starts, target_session
from elliottwaver.data.quality import normalize_bars, stale_intraday_error
from elliottwaver.data.reliability import request_json
from elliottwaver.errors import DataError, SourceError
from elliottwaver.schemas.market import Bar, Instrument, MarketDataBundle, Provenance
from elliottwaver.timeframes import minutes
from elliottwaver.util import digest

_API = "https://query1.finance.yahoo.com/v8/finance/chart"
_INTERVAL = {"1m": "1m", "5m": "5m", "15m": "15m", "30m": "30m", "60m": "60m", "1d": "1d"}
_RANGE = {"1m": "7d", "5m": "60d", "15m": "60d", "30m": "60d", "60m": "60d", "1d": "2y"}


class YahooIndexProvider:
    def __init__(self, config: DataConfig, *, feed: str = "yahoo-index"):
        if feed not in {"yahoo-index", "yahoo"}:
            raise ValueError(f"unsupported Yahoo feed {feed}")
        self.config = config
        self.feed = feed

    @staticmethod
    def _ticker(instrument: Instrument) -> str:
        if instrument.asset_type == "INDEX":
            return instrument.provider_symbol
        return instrument.provider_symbol.replace(".", "-")

    def _chart(self, ticker: str, interval: str) -> dict:
        payload = request_json(
            "GET",
            f"{_API}/{ticker}",
            params={"interval": interval, "range": _RANGE[interval], "includePrePost": "false"},
            headers={"User-Agent": "Mozilla/5.0"},
            timeout=(10, self.config.request_timeout_seconds),
        )
        chart = payload.get("chart") or {}
        if chart.get("error"):
            raise SourceError(f"YAHOO_CHART: {chart['error']}")
        result = (chart.get("result") or [None])[0]
        if not result:
            raise SourceError("YAHOO_CHART: empty result")
        return result

    def _instrument_chart(self, instrument: Instrument, interval: str) -> dict:
        ticker = self._ticker(instrument)
        result = self._chart(ticker, interval)
        symbol = (result.get("meta") or {}).get("symbol")
        if not isinstance(symbol, str) or symbol.upper() != ticker.upper():
            raise SourceError("ASSET_IDENTITY_MISMATCH: Yahoo chart lacks the requested symbol")
        return result

    def _daily(
        self,
        instrument: Instrument,
        calendar,
        as_of: datetime,
    ) -> tuple[Bar, ...]:
        result = self._instrument_chart(instrument, "1d")
        meta = result.get("meta") or {}
        zone = ZoneInfo(meta.get("exchangeTimezoneName", instrument.timezone))
        sessions = {session.date: session for session in calendar.sessions}
        stamps = result.get("timestamp") or []
        quote = ((result.get("indicators") or {}).get("quote") or [{}])[0]
        observed = datetime.now(UTC)
        bars = []
        for index, stamp in enumerate(stamps):
            values = {
                key: (quote.get(key) or [None] * len(stamps))[index]
                for key in ("open", "high", "low", "close", "volume")
            }
            if any(values[key] is None for key in ("open", "high", "low", "close")):
                continue
            moment = datetime.fromtimestamp(stamp, UTC).astimezone(zone)
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
                    open=float(values["open"]),
                    high=float(values["high"]),
                    low=float(values["low"]),
                    close=float(values["close"]),
                    volume=float(values["volume"] or 0.0),
                    amount=None,
                    source_revision=digest(values),
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
        if instrument.market != "US":
            raise DataError("Yahoo provider requires a US instrument")
        if instrument.asset_type == "INDEX" and self.feed != "yahoo-index":
            raise DataError("Yahoo index requires feed yahoo-index")
        if instrument.asset_type == "EQUITY" and self.feed != "yahoo":
            raise DataError("Yahoo equity requires feed yahoo")
        interval = _INTERVAL.get(timeframe)
        if interval is None:
            raise DataError("Yahoo index provider requires 1m/5m/15m/30m/60m or 1d")
        source_interval = "30m" if timeframe == "60m" else interval
        calendar = load_calendar(instrument, start, end)
        target = target_session(
            calendar,
            instrument,
            as_of,
            timeframe,
            end,
            self.config.delayed_access_buffer_minutes,
        )
        daily = self._daily(instrument, calendar, as_of)
        if timeframe == "1d":
            bars = tuple(
                bar
                for bar in daily
                if start <= bar.session_date <= target and bar.usable_at <= as_of
            )
            if not bars or bars[-1].session_date != target:
                raise DataError("STALE_DATA: Yahoo lacks the required index session")
            return self._bundle(
                instrument,
                bars,
                bars,
                (),
                calendar,
                target,
                as_of,
                timeframe="1d",
                source_timeframe="1d",
                expected=(),
            )
        result = self._instrument_chart(instrument, source_interval)
        meta = result.get("meta") or {}
        zone = ZoneInfo(meta.get("exchangeTimezoneName", instrument.timezone))
        sessions = {session.date: session for session in calendar.sessions}
        stamps = result.get("timestamp") or []
        quote = ((result.get("indicators") or {}).get("quote") or [{}])[0]
        observed = datetime.now(UTC)
        duration = timedelta(minutes=minutes(source_interval))
        bars = []
        for index, stamp in enumerate(stamps):
            values = {
                key: (quote.get(key) or [None] * len(stamps))[index]
                for key in ("open", "high", "low", "close", "volume")
            }
            if any(values[key] is None for key in ("open", "high", "low", "close")):
                continue
            start_at = datetime.fromtimestamp(stamp, UTC)
            end_at = start_at + duration
            session = sessions.get(start_at.astimezone(zone).date())
            if session is None or start_at < session.open_at or end_at > session.close_at:
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
                    open=float(values["open"]),
                    high=float(values["high"]),
                    low=float(values["low"]),
                    close=float(values["close"]),
                    volume=float(values["volume"] or 0.0),
                    amount=None,
                    source_revision=digest(values),
                )
            )
        bars = normalize_bars(bars)
        expected_source = expected_starts(
            calendar,
            instrument,
            as_of,
            source_interval,
            buffer_minutes=self.config.delayed_access_buffer_minutes,
            through=target,
            since=start,
        )
        if not bars or not expected_source:
            raise DataError("YAHOO_NO_BARS: no completed index bars")
        if expected_source[-1] != bars[-1].start_at:
            raise stale_intraday_error(instrument, bars[-1])
        if timeframe != source_interval:
            bars = aggregate_intraday(
                bars,
                calendar,
                instrument,
                timeframe,
                as_of,
                buffer_minutes=self.config.delayed_access_buffer_minutes,
            )
            if not bars:
                raise DataError("YAHOO_AGGREGATION_EMPTY: no completed index bars")
        expected = expected_starts(
            calendar,
            instrument,
            as_of,
            timeframe,
            buffer_minutes=self.config.delayed_access_buffer_minutes,
            through=target,
            since=start,
        )
        if not expected or expected[-1] != bars[-1].start_at:
            raise stale_intraday_error(instrument, bars[-1])
        return self._bundle(
            instrument,
            bars,
            bars,
            daily,
            calendar,
            target,
            as_of,
            timeframe=timeframe,
            source_timeframe=source_interval,
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
        expected: tuple[datetime, ...],
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
                provider="yahoo",
                feed=self.feed,
                adjustment="none",
                basis_date=bars[-1].session_date,
                price_coordinate_id=digest(
                    ["yahoo", self.feed, target, [bar.bar_id for bar in bars]]
                ),
                parser_version="yahoo-chart-v1",
                source_timeframe=source_timeframe,
                aggregation="none",
                coverage_start=bars[0].session_date,
                coverage_end=bars[-1].session_date,
                coverage_reason="YAHOO_FREE_RANGE",
                session_profile="us_index_rth"
                if instrument.asset_type == "INDEX"
                else "us_rth_open_aligned",
                calendar_version=calendar.version,
                fetched_at=datetime.now(UTC),
                requested_start=bars[0].session_date,
                requested_end=target,
                amount_currency="USD",
                volume_unit="index_volume" if instrument.asset_type == "INDEX" else "shares",
                volume_comparable=instrument.asset_type != "INDEX",
                warnings=("YAHOO_UNOFFICIAL_FREE_SOURCE: 免费公开接口，无 SLA",)
                + (
                    ("YAHOO_INDEX_VOLUME_UNAVAILABLE: 指数成交量不作为可比证据",)
                    if instrument.asset_type == "INDEX"
                    else ()
                ),
            ),
        )
