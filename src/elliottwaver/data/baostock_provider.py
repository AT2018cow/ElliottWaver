"""BaoStock free historical 5/15/30/60-minute adapter for CN equities.

BaoStock has no 1-minute endpoint and no index minute history. It is a useful
free historical fallback for stocks; it is not a real-time feed.
"""

from __future__ import annotations

import threading
from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo

from elliottwaver.config import DataConfig
from elliottwaver.data.adjustment import adjust_to_basis
from elliottwaver.data.calendar import load_calendar
from elliottwaver.data.periods import expected_starts, intraday_windows, target_session
from elliottwaver.data.quality import normalize_bars
from elliottwaver.data.reliability import retry_call
from elliottwaver.errors import DataError, SourceError
from elliottwaver.schemas.market import Bar, FactorPoint, Instrument, MarketDataBundle, Provenance
from elliottwaver.timeframes import minutes
from elliottwaver.util import digest

_LOCK = threading.Lock()


class BaoStockProvider:
    def __init__(self, config: DataConfig, *, feed: str = "baostock"):
        if feed != "baostock":
            raise ValueError(f"unsupported BaoStock feed {feed}")
        self.config = config
        self.feed = feed

    @staticmethod
    def _code(instrument: Instrument) -> str:
        digits, _, exchange = instrument.canonical_symbol.partition(".")
        return f"{exchange.lower()}.{digits}"

    @staticmethod
    def _login(bs):
        result = bs.login()
        if result.error_code != "0":
            raise SourceError(f"BAOSTOCK_LOGIN: {result.error_msg}")

    def fetch(
        self,
        instrument: Instrument,
        start: date,
        end: date,
        as_of: datetime,
        timeframe: str = "1d",
    ) -> MarketDataBundle:
        if instrument.market != "CN" or instrument.asset_type != "EQUITY":
            raise DataError("BaoStock minute adapter supports CN equities only")
        if timeframe == "1m":
            raise DataError("BAOSTOCK_1M_UNSUPPORTED: BaoStock has no 1-minute endpoint")
        if minutes(timeframe) is None:
            raise DataError("BaoStock minute adapter requires 5m/15m/30m/60m")
        try:
            import baostock as bs
        except ImportError as exc:
            raise SourceError("BAOSTOCK_NOT_INSTALLED: install the cn extra") from exc
        calendar = load_calendar(instrument, start, end)
        target = target_session(calendar, instrument, as_of, timeframe, end, 0)
        zone = ZoneInfo(instrument.timezone)
        code = self._code(instrument)
        observed = datetime.now(UTC)
        with _LOCK:
            self._login(bs)
            try:
                basic = bs.query_stock_basic(code=code)
                name = ""
                if basic.error_code == "0":
                    rows = []
                    while basic.next():
                        rows.append(basic.get_row_data())
                    if rows:
                        name = rows[0][1] if len(rows[0]) > 1 else ""
                query = retry_call(
                    lambda: bs.query_history_k_data_plus(
                        code,
                        "date,time,code,open,high,low,close,volume,amount,adjustflag",
                        start_date=start.isoformat(),
                        end_date=target.isoformat(),
                        frequency=timeframe[:-1],
                        adjustflag="3",
                    ),
                    attempts=self.config.max_attempts,
                )
                if query.error_code != "0":
                    raise SourceError(f"BAOSTOCK_QUERY: {query.error_msg}")
                raw_rows = []
                while query.next():
                    raw_rows.append(query.get_row_data())
                factors_query = retry_call(
                    lambda: bs.query_adjust_factor(
                        code=code,
                        start_date="1990-01-01",
                        end_date=target.isoformat(),
                    ),
                    attempts=self.config.max_attempts,
                )
                factors = ()
                if factors_query.error_code == "0":
                    rows = []
                    while factors_query.next():
                        rows.append(factors_query.get_row_data())
                    factors = tuple(
                        sorted(
                            (
                                FactorPoint(
                                    effective_date=date.fromisoformat(row[1]),
                                    factor=float(row[2]),
                                )
                                for row in rows
                                if row[1] and float(row[2]) > 0
                            ),
                            key=lambda point: point.effective_date,
                        )
                    )
            finally:
                bs.logout()
        windows = {}
        for session in calendar.sessions:
            for opened, closed in intraday_windows(session, minutes(timeframe)):
                windows[closed] = (opened, session.date)
        bars = []
        for row in raw_rows:
            stamp = str(row[1])
            if len(stamp) < 12:
                continue
            end_at = (
                datetime.strptime(stamp[:12], "%Y%m%d%H%M").replace(tzinfo=zone).astimezone(UTC)
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
                    open=float(row[3]),
                    high=float(row[4]),
                    low=float(row[5]),
                    close=float(row[6]),
                    volume=float(row[7]),
                    amount=float(row[8]) if row[8] else None,
                    source_revision=digest(row),
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
        if not raw or not expected:
            raise DataError("BAOSTOCK_NO_BARS: no completed stock bars")
        expected = tuple(item for item in expected if item <= raw[-1].start_at)
        if not expected or expected[-1] != raw[-1].start_at:
            raise DataError("STALE_INTRADAY: BaoStock bars do not align to the session grid")
        try:
            adjusted = adjust_to_basis(raw, factors, basis_date=target)
        except DataError as exc:
            raise DataError(f"BAOSTOCK_FACTOR_UNAVAILABLE: {exc}") from exc
        factor_hash = digest(factors)
        provenance = Provenance(
            provider="baostock",
            feed=self.feed,
            adjustment="basis_date_qfq",
            basis_date=target,
            price_coordinate_id=digest(["baostock", target, factor_hash]),
            factor_hash=factor_hash,
            parser_version=f"baostock-{getattr(bs, '__version__', '0.9')}",
            source_timeframe=timeframe,
            aggregation="none",
            coverage_start=raw[0].session_date,
            coverage_end=raw[-1].session_date,
            coverage_reason="BAOSTOCK_HISTORY",
            session_profile="cn_baostock_history",
            calendar_version=calendar.version,
            fetched_at=observed,
            requested_start=start,
            requested_end=end,
            amount_currency="CNY",
            volume_comparable=True,
            warnings=(
                "BAOSTOCK_HISTORY_ONLY: 历史分钟数据，不是实时源",
                "BAOSTOCK_ADJUSTMENT: 使用 foreAdjustFactor 重新计算基准日复权",
            ),
        )
        return MarketDataBundle(
            instrument=instrument.model_copy(
                update={"identity_verified": bool(name), "name": name or instrument.name}
            ),
            as_of=as_of,
            timeframe=timeframe,
            bars=adjusted,
            raw_bars=raw,
            factors=factors,
            sessions=calendar.dates,
            expected_bar_starts=expected,
            provenance=provenance,
        )
