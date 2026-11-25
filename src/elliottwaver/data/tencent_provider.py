"""Tencent free minute K-line adapter for CN indices.

Tencent's public `mkline` endpoint returns up to 320 bars ending before a
supplied timestamp. The adapter pages backwards and maps bar-end labels onto
the exchange session grid. It is an unofficial free source: no key is needed,
but there is no SLA and volume units are not certified.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

from elliottwaver.config import DataConfig
from elliottwaver.data.adjustment import adjust_to_basis
from elliottwaver.data.calendar import load_calendar
from elliottwaver.data.periods import expected_starts, intraday_windows, target_session
from elliottwaver.data.quality import normalize_bars, stale_intraday_error
from elliottwaver.data.reliability import request_json
from elliottwaver.errors import DataError, SourceError
from elliottwaver.schemas.market import Bar, FactorPoint, Instrument, MarketDataBundle, Provenance
from elliottwaver.timeframes import minutes
from elliottwaver.util import digest

_API = "https://ifzq.gtimg.cn/appstock/app/kline/mkline"
_PAGE = 320
_PERIOD = {"1m": "m1", "5m": "m5", "15m": "m15", "30m": "m30", "60m": "m60"}
PARSER_VERSION = "tencent-mkline-v2"


class TencentProvider:
    def __init__(self, config: DataConfig, *, feed: str = "tencent-cn-index"):
        if feed not in {"tencent-cn-index", "tencent-cn-equity"}:
            raise ValueError(f"unsupported Tencent feed {feed}")
        self.config = config
        self.feed = feed

    def _request(self, code: str, period: str, end_marker: str, count: int = _PAGE) -> dict:
        payload = request_json(
            "GET",
            _API,
            params={"param": f"{code},{period},{end_marker},{count}"},
            timeout=(10, self.config.request_timeout_seconds),
        )
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
        if instrument.market != "CN":
            raise DataError("Tencent provider requires a CN instrument")
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
        rows: dict[str, tuple[list, datetime]] = {}
        first_data = None
        marker = (
            calendar.session(target).close_at.astimezone(zone) + timedelta(minutes=1)
        ).strftime("%Y%m%d%H%M")
        pages = 0
        while pages < self.config.tencent_max_pages:
            payload = self._request(instrument.provider_symbol, period, marker)
            observed = datetime.now(UTC)
            data = (payload.get("data") or {}).get(instrument.provider_symbol) or {}
            if first_data is None:
                first_data = data
            page = data.get(period) or []
            if not page:
                break
            for row in page:
                key = str(row[0])
                previous = rows.get(key)
                if previous is None or previous[0] != row:
                    rows[key] = (row, observed)
            pages += 1
            earliest = min(str(row[0]) for row in page)
            if len(page) < _PAGE or earliest <= start.strftime("%Y%m%d0000"):
                break
            marker = earliest
        bars = []
        for row, observed in rows.values():
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
        if expected[-1] != bars[-1].start_at:
            raise stale_intraday_error(instrument, bars[-1])
        name = ""
        if first_data is not None:
            name = (first_data.get("qt") or {}).get(instrument.provider_symbol, [None, ""])[1] or ""
        verified = instrument.model_copy(
            update={"name": name or instrument.name, "identity_verified": bool(name)}
        )
        factors: tuple[FactorPoint, ...] = ()
        adjusted = bars
        adjustment = "none"
        if instrument.asset_type == "EQUITY":
            try:
                import akshare as ak

                frame = ak.stock_zh_a_hist_sina(
                    symbol=instrument.provider_symbol,
                    start_date=start.isoformat(),
                    end_date=target.isoformat(),
                    adjust="hfq-factor",
                )
                factors = tuple(
                    sorted(
                        (
                            FactorPoint(
                                effective_date=date.fromisoformat(str(row["date"])[:10]),
                                factor=float(row["hfq_factor"]),
                            )
                            for row in frame.to_dict("records")
                            if date.fromisoformat(str(row["date"])[:10]) <= target
                        ),
                        key=lambda point: point.effective_date,
                    )
                )
                adjusted = adjust_to_basis(bars, factors, basis_date=target)
                adjustment = "basis_date_qfq"
            except Exception as exc:
                raise DataError(f"TENCENT_FACTOR_UNAVAILABLE: {exc}") from exc
        factor_hash = digest(factors)
        provenance = Provenance(
            provider="tencent",
            feed=self.feed,
            adjustment=adjustment,
            basis_date=bars[-1].session_date,
            price_coordinate_id=digest(
                ["tencent", self.feed, target, factor_hash, [bar.bar_id for bar in bars]]
            ),
            factor_hash=factor_hash,
            parser_version=PARSER_VERSION,
            source_timeframe=timeframe,
            aggregation="none",
            coverage_start=bars[0].session_date,
            coverage_end=bars[-1].session_date,
            coverage_reason="TENCENT_BACKWARD_PAGES",
            session_profile="cn_exchange_session",
            calendar_version=calendar.version,
            fetched_at=datetime.now(UTC),
            requested_start=start,
            requested_end=end,
            volume_unit="index_volume" if instrument.asset_type == "INDEX" else "vendor_unit",
            amount_currency="CNY",
            volume_comparable=False,
            warnings=(
                "TENCENT_UNOFFICIAL_FREE_SOURCE: 免费公开接口，无 SLA",
                "TENCENT_VOLUME_UNIT_UNVERIFIED: 成交量单位未认证，量价证据不可比",
            ),
        )
        return MarketDataBundle(
            instrument=verified,
            as_of=as_of,
            timeframe=timeframe,
            bars=adjusted,
            raw_bars=bars,
            factors=factors,
            sessions=calendar.dates,
            expected_bar_starts=expected,
            provenance=provenance,
        )
