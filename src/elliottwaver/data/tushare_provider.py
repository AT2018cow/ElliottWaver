"""Tushare minute-bar adapter for CN equities and indices.

The adapter is deliberately thin: it normalizes Tushare rows into the same
left-closed/right-open UTC bar contract as the Sina and Alpaca providers. The
vendor labels a minute bar by its trade time; this adapter treats that label as
the bar end and records the assumption in the provenance warning.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

import requests

from elliottwaver.config import DataConfig, tushare_token
from elliottwaver.data.adjustment import adjust_to_basis
from elliottwaver.data.calendar import load_calendar
from elliottwaver.data.periods import expected_starts, target_session
from elliottwaver.data.quality import normalize_bars
from elliottwaver.errors import DataError, SourceError
from elliottwaver.schemas.market import Bar, FactorPoint, Instrument, MarketDataBundle, Provenance
from elliottwaver.timeframes import minutes
from elliottwaver.util import digest

_API = "https://api.tushare.pro"
PARSER_VERSION = "tushare-v2"
_FREQ = {"1m": "1min", "5m": "5min", "15m": "15min", "30m": "30min", "60m": "60min"}


def _date(value) -> date:
    text = str(value)
    if "-" in text:
        return date.fromisoformat(text[:10])
    return datetime.strptime(text[:8], "%Y%m%d").date()


def _require_symbol(rows: list[dict], symbol: str, endpoint: str) -> None:
    if any(row.get("ts_code") != symbol for row in rows):
        raise DataError(f"ASSET_IDENTITY_MISMATCH: Tushare {endpoint} expected {symbol}")


class TushareProvider:
    def __init__(self, config: DataConfig, *, feed: str):
        if feed not in {"tushare-cn-equity", "tushare-cn-index"}:
            raise ValueError(f"unsupported Tushare feed {feed}")
        self.config = config
        self.feed = feed

    def _request(self, api_name: str, params: dict, fields: str) -> list[dict]:
        response = requests.post(
            _API,
            json={
                "api_name": api_name,
                "token": tushare_token(),
                "params": params,
                "fields": fields,
            },
            timeout=(10, self.config.request_timeout_seconds),
        )
        response.raise_for_status()
        payload = response.json()
        if payload.get("code") != 0:
            raise SourceError(
                f"TUSHARE_{payload.get('code')}: {payload.get('msg') or 'request failed'}"
            )
        data = payload.get("data") or {}
        names = data.get("fields") or []
        return [dict(zip(names, values, strict=True)) for values in data.get("items") or []]

    def _factors(self, instrument: Instrument, start: date, end: date) -> tuple[FactorPoint, ...]:
        rows = self._request(
            "adj_factor",
            {
                "ts_code": instrument.canonical_symbol,
                "start_date": start.isoformat(),
                "end_date": end.isoformat(),
            },
            "ts_code,trade_date,adj_factor",
        )
        _require_symbol(rows, instrument.canonical_symbol, "adj_factor")
        return tuple(
            sorted(
                (
                    FactorPoint(
                        effective_date=_date(row["trade_date"]),
                        factor=float(row["adj_factor"]),
                    )
                    for row in rows
                ),
                key=lambda point: point.effective_date,
            )
        )

    def fetch(
        self,
        instrument: Instrument,
        start: date,
        end: date,
        as_of: datetime,
        timeframe: str = "1d",
    ) -> MarketDataBundle:
        if minutes(timeframe) is None:
            raise DataError("Tushare intraday provider requires a minute timeframe")
        calendar = load_calendar(instrument, start, end)
        target = target_session(calendar, instrument, as_of, timeframe, end, 0)
        api_name = "idx_mins" if instrument.asset_type == "INDEX" else "stock_mins"
        zone = ZoneInfo(instrument.timezone)
        rows = self._request(
            api_name,
            {
                "ts_code": instrument.canonical_symbol,
                "freq": _FREQ[timeframe],
                "start_date": f"{start.isoformat()} 00:00:00",
                "end_date": as_of.astimezone(zone).strftime("%Y-%m-%d %H:%M:%S"),
            },
            "ts_code,trade_time,open,high,low,close,vol,amount",
        )
        _require_symbol(rows, instrument.canonical_symbol, api_name)
        duration = timedelta(minutes=minutes(timeframe))
        observed = datetime.now(UTC)
        bars = []
        session_dates = set(calendar.dates)
        for row in rows:
            end_at = (
                datetime.fromisoformat(str(row["trade_time"])).replace(tzinfo=zone).astimezone(UTC)
            )
            start_at = end_at - duration
            session_day = end_at.astimezone(zone).date()
            if session_day not in session_dates:
                continue
            session = calendar.session(session_day)
            if end_at > as_of or end_at > session.close_at or start_at < session.open_at:
                continue
            bars.append(
                Bar(
                    start_at=start_at,
                    end_at=end_at,
                    available_at=end_at,
                    observed_at=observed,
                    session_date=session_day,
                    open=float(row["open"]),
                    high=float(row["high"]),
                    low=float(row["low"]),
                    close=float(row["close"]),
                    volume=float(row["vol"]) if row.get("vol") is not None else None,
                    amount=float(row["amount"]) if row.get("amount") is not None else None,
                    source_revision=digest({key: str(value) for key, value in row.items()}),
                )
            )
        raw = normalize_bars(bars)
        expected = expected_starts(
            calendar,
            instrument,
            as_of,
            timeframe,
            buffer_minutes=0,
            through=target,
            since=start,
        )
        if not raw or not expected or raw[-1].start_at != expected[-1]:
            raise DataError("STALE_INTRADAY: Tushare lacks the required final bar")
        factors: tuple[FactorPoint, ...] = ()
        if instrument.asset_type == "EQUITY":
            factors = self._factors(instrument, start, target)
            adjusted = adjust_to_basis(raw, factors, basis_date=target)
            adjustment = "basis_date_qfq"
        else:
            adjusted = raw
            adjustment = "none"
        coverage_start = raw[0].session_date if raw else None
        coverage_end = raw[-1].session_date if raw else None
        warnings = (
            "TUSHARE_MINUTE_TIME_LABEL_AS_END: 供应商分钟标签按 bar 结束时刻解析",
            "TUSHARE_IDENTITY_UNVERIFIED: 分钟接口未返回证券名称，身份由代码路由确认",
            "TUSHARE_HISTORICAL_ADJUSTMENT_VERSION: 复权因子为本次取得版本，不构成历史PIT证明",
        )
        if coverage_start and start < coverage_start:
            warnings += ("COVERAGE_TRUNCATED: 请求起点早于供应商实际返回范围",)
        return MarketDataBundle(
            instrument=instrument.model_copy(update={"identity_verified": False}),
            as_of=as_of,
            timeframe=timeframe,
            bars=adjusted,
            raw_bars=raw,
            factors=factors,
            sessions=calendar.dates,
            expected_bar_starts=expected,
            provenance=Provenance(
                provider="tushare",
                feed=self.feed,
                adjustment=adjustment,
                basis_date=target,
                price_coordinate_id=digest(
                    ["tushare", self.feed, adjustment, target, [bar.bar_id for bar in adjusted]]
                ),
                factor_hash=digest(factors),
                parser_version=PARSER_VERSION,
                source_timeframe=timeframe,
                aggregation="none",
                coverage_start=coverage_start,
                coverage_end=coverage_end,
                coverage_reason=(
                    "REQUESTED_START_BEFORE_PROVIDER_COVERAGE"
                    if coverage_start and start < coverage_start
                    else ""
                ),
                session_profile="cn_exchange_session",
                calendar_version=calendar.version,
                fetched_at=observed,
                requested_start=start,
                requested_end=end,
                amount_currency="CNY",
                volume_unit="index_volume" if instrument.asset_type == "INDEX" else "shares",
                volume_comparable=instrument.asset_type == "EQUITY",
                warnings=warnings,
            ),
        )
