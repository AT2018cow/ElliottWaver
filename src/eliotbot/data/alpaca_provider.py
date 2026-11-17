"""Alpaca Market Data v2 REST adapter with complete, bounded pagination."""

from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from eliotbot.config import DataConfig, alpaca_asset_endpoint, alpaca_credentials
from eliotbot.data.calendar import bar_window, latest_analyzable, load_calendar
from eliotbot.data.http import HTTPClient
from eliotbot.data.periods import aggregate_intraday, expected_starts, target_session
from eliotbot.data.quality import normalize_bars
from eliotbot.errors import DataError, SourceError
from eliotbot.schemas.market import Bar, Instrument, MarketDataBundle, Provenance
from eliotbot.util import digest


class AlpacaProvider:
    def __init__(self, config: DataConfig, *, feed: str = "sip", http=None):
        if feed not in {"sip", "iex"}:
            raise ValueError("feed must be explicitly sip or iex")
        self.config, self.feed = config, feed
        if http is None:
            key, secret = alpaca_credentials()
            http = HTTPClient(
                config,
                {
                    "APCA-API-KEY-ID": key.get_secret_value(),
                    "APCA-API-SECRET-KEY": secret.get_secret_value(),
                },
            )
        self.http = http

    def _pages(
        self,
        symbol: str,
        start: datetime,
        end: datetime,
        adjustment: str,
        asof: date,
        timeframe="1Day",
    ):
        params = {
            "symbols": symbol,
            "timeframe": timeframe,
            "start": start.isoformat(),
            "end": end.isoformat(),
            "adjustment": adjustment,
            "feed": self.feed,
            "asof": asof.isoformat(),
            "sort": "asc",
            "limit": 10000,
        }
        rows = []
        tokens = set()
        for _ in range(1000):
            payload = self.http.get_json("https://data.alpaca.markets/v2/stocks/bars", params)
            if not isinstance(payload, dict) or not isinstance(payload.get("bars"), dict):
                raise SourceError("Malformed Alpaca bars response")
            page = payload["bars"].get(symbol, [])
            if not isinstance(page, list):
                raise SourceError("Malformed Alpaca symbol page")
            rows.extend(page)
            token = payload.get("next_page_token")
            if not token:
                return rows
            if token in tokens:
                raise SourceError("Repeated Alpaca page token; refusing an incomplete response")
            tokens.add(token)
            params = {**params, "page_token": token}
        raise SourceError("Alpaca pagination exceeded its safety limit")

    def fetch(
        self, instrument: Instrument, start: date, end: date, as_of: datetime, timeframe="1d"
    ) -> MarketDataBundle:
        calendar = load_calendar(instrument, start, end)
        target = latest_analyzable(
            calendar, instrument, as_of, self.config.delayed_access_buffer_minutes, through=end
        )
        asset = self.http.get_json(
            f"{alpaca_asset_endpoint()}/v2/assets/{instrument.provider_symbol}"
        )
        if not isinstance(asset, dict) or not asset.get("id") or asset.get("class") != "us_equity":
            raise DataError("Alpaca did not verify this US equity's identity")
        instrument = instrument.model_copy(
            update={
                "instrument_id": f"alpaca:{asset['id']}",
                "exchange": asset.get("exchange", "UNKNOWN"),
                "name": asset.get("name", ""),
                "identity_verified": True,
                "provider_symbol": asset.get("symbol", instrument.provider_symbol),
            }
        )
        zone = ZoneInfo(instrument.timezone)
        begin = datetime.combine(start, time.min, zone).astimezone(UTC)
        cutoff = min(
            datetime.combine(target + timedelta(days=1), time.min, zone).astimezone(UTC),
            datetime.now(UTC) - timedelta(minutes=self.config.delayed_access_buffer_minutes),
        )
        raw = self._pages(instrument.provider_symbol, begin, cutoff, "raw", target)
        adjusted = self._pages(instrument.provider_symbol, begin, cutoff, "all", target)
        observed = datetime.now(UTC)

        def convert(rows):
            bars = []
            for row in rows:
                stamp = datetime.fromisoformat(row["t"].replace("Z", "+00:00"))
                day = stamp.astimezone(zone).date()
                if not start <= day <= target:
                    continue
                opened, closed, available = bar_window(
                    calendar.session(day), "US", self.config.delayed_access_buffer_minutes
                )
                if available > as_of:
                    continue
                bars.append(
                    Bar(
                        start_at=opened,
                        end_at=closed,
                        available_at=available,
                        session_date=day,
                        observed_at=observed,
                        open=row["o"],
                        high=row["h"],
                        low=row["l"],
                        close=row["c"],
                        volume=row["v"],
                        source_revision=digest(row),
                    )
                )
            return normalize_bars(bars)

        raw_bars, bars = convert(raw), convert(adjusted)
        if not bars or bars[-1].session_date != target:
            raise DataError(f"STALE_DATA: Alpaca {self.feed} lacks the required {target} session")
        coordinate = digest(["alpaca", self.feed, "all", target, adjusted])
        daily, raw_daily = (), ()
        expected = ()
        profile = "provider_native_daily"
        if timeframe.endswith("m"):
            target = target_session(
                calendar,
                instrument,
                as_of,
                timeframe,
                end,
                self.config.delayed_access_buffer_minutes,
            )
            daily, raw_daily = bars, raw_bars
            minute_start = max(start, target - timedelta(days=126))
            native = "1Min" if timeframe == "1m" else "5Min"
            duration = 1 if timeframe == "1m" else 5
            begin = datetime.combine(minute_start, time.min, zone).astimezone(UTC)
            cutoff = min(as_of, datetime.now(UTC)) - timedelta(
                minutes=self.config.delayed_access_buffer_minutes
            )

            def convert_minutes(rows):
                result = []
                for row in rows:
                    opened = datetime.fromisoformat(row["t"].replace("Z", "+00:00"))
                    if opened.tzinfo is None:
                        raise SourceError("Alpaca timestamp lacks a timezone")
                    closed = opened + timedelta(minutes=duration)
                    day = opened.astimezone(zone).date()
                    if not minute_start <= day <= target or day not in calendar.dates:
                        continue
                    session = calendar.session(day)
                    available = closed + timedelta(
                        minutes=self.config.delayed_access_buffer_minutes
                    )
                    if opened < session.open_at or closed > session.close_at or available > as_of:
                        continue
                    result.append(
                        Bar(
                            start_at=opened,
                            end_at=closed,
                            available_at=available,
                            session_date=day,
                            observed_at=observed,
                            open=row["o"],
                            high=row["h"],
                            low=row["l"],
                            close=row["c"],
                            volume=row["v"],
                            source_revision=digest(row),
                        )
                    )
                return normalize_bars(result)

            raw_native = convert_minutes(
                self._pages(instrument.provider_symbol, begin, cutoff, "raw", target, native)
            )
            adjusted_native = convert_minutes(
                self._pages(instrument.provider_symbol, begin, cutoff, "all", target, native)
            )
            raw_bars = aggregate_intraday(
                raw_native,
                calendar,
                instrument,
                timeframe,
                as_of,
                self.config.delayed_access_buffer_minutes,
            )
            bars = aggregate_intraday(
                adjusted_native,
                calendar,
                instrument,
                timeframe,
                as_of,
                self.config.delayed_access_buffer_minutes,
            )
            expected = expected_starts(
                calendar,
                instrument,
                as_of,
                timeframe,
                buffer_minutes=self.config.delayed_access_buffer_minutes,
                through=end,
                since=minute_start,
            )
            if not bars or not expected or bars[-1].start_at != expected[-1]:
                raise DataError("STALE_INTRADAY: Alpaca lacks the required final RTH bar")
            profile = "us_rth_open_aligned"
            coordinate = digest(
                ["alpaca", self.feed, "all", timeframe, target, [b.bar_id for b in bars]]
            )
        return MarketDataBundle(
            instrument=instrument,
            timeframe=timeframe,
            as_of=as_of,
            bars=bars,
            raw_bars=raw_bars,
            sessions=calendar.dates,
            daily_context=daily,
            raw_daily_context=raw_daily,
            expected_bar_starts=expected,
            provenance=Provenance(
                provider="alpaca",
                feed=self.feed,
                adjustment="provider_all",
                price_coordinate_id=coordinate,
                basis_date=target,
                session_profile=profile,
                calendar_version=calendar.version,
                fetched_at=observed,
                requested_start=start,
                requested_end=end,
                factor_hash=digest(adjusted),
                amount_currency="USD",
                warnings=("历史复权采用本次下载版本，历史截断不构成PIT证明",)
                + (("IEX is a single-exchange feed",) if self.feed == "iex" else ()),
            ),
        )
