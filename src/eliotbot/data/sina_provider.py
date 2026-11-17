"""Sina daily data through a version-aware, killable AKShare boundary."""

import importlib.metadata
import multiprocessing as mp
import re
import sys
from contextlib import redirect_stdout
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

from eliotbot.config import DataConfig
from eliotbot.data.adjustment import adjust_to_basis
from eliotbot.data.calendar import bar_window, latest_analyzable, load_calendar
from eliotbot.data.periods import expected_starts, intraday_windows, target_session
from eliotbot.data.quality import normalize_bars
from eliotbot.errors import DataError, SourceError
from eliotbot.schemas.market import Bar, FactorPoint, Instrument, MarketDataBundle, Provenance
from eliotbot.util import digest


def _sina_worker(connection, symbol: str, start: str, end: str, timeframe="1d"):
    with redirect_stdout(sys.stderr):
        _sina_worker_body(connection, symbol, start, end, timeframe)


def _sina_worker_body(connection, symbol, start, end, timeframe):
    stage = "dependencies"
    try:
        import akshare as ak
        import requests

        daily = getattr(ak, "stock_zh_a_hist_sina", None) or getattr(ak, "stock_zh_a_daily", None)
        if daily is None:
            connection.send({"error": "UNSUPPORTED_AKSHARE_INTERFACE"})
            return
        stage = "sina_identity"
        quote = requests.get(
            f"https://hq.sinajs.cn/list={symbol}",
            headers={"Referer": "https://finance.sina.com.cn/", "User-Agent": "Mozilla/5.0"},
            timeout=(10, 20),
        )
        quote.raise_for_status()
        quote.encoding = "gb18030"
        match = re.search(r"var\s+hq_str_" + re.escape(symbol) + r'="([^",]+),', quote.text)
        if not match:
            connection.send({"error": "SECURITY_IDENTITY_UNAVAILABLE"})
            return
        stage = "daily_history"
        raw = daily(symbol=symbol, start_date=start, end_date=end, adjust="")
        stage = "adjustment_factors"
        factors = daily(symbol=symbol, start_date=start, end_date=end, adjust="hfq-factor")
        intraday = []
        if timeframe.endswith("m"):
            stage = "intraday_history"
            minute = getattr(ak, "stock_zh_a_hist_min_sina", None) or getattr(
                ak, "stock_zh_a_minute", None
            )
            if minute is None:
                raise ValueError("unsupported minute interface")
            intraday = minute(symbol=symbol, period=timeframe[:-1], adjust="").to_dict("records")
        connection.send(
            {
                "raw": raw.to_dict("records"),
                "factors": factors.to_dict("records"),
                "name": match.group(1),
                "intraday": intraday,
                "version": importlib.metadata.version("akshare"),
            }
        )
    except Exception as exc:
        connection.send({"error": f"{stage}:{type(exc).__name__}"})
    finally:
        connection.close()


def fetch_sina_payload(symbol: str, start: date, end: date, deadline: float, timeframe="1d"):
    context = mp.get_context("spawn")
    parent, child = context.Pipe(duplex=False)
    process = context.Process(
        target=_sina_worker,
        args=(child, symbol, start.strftime("%Y%m%d"), end.strftime("%Y%m%d"), timeframe),
    )
    process.start()
    child.close()
    try:
        if not parent.poll(deadline):
            raise SourceError("Sina retrieval exceeded its total deadline")
        try:
            payload = parent.recv()
        except EOFError as exc:
            raise SourceError("Sina worker exited without a complete response") from exc
        if payload.get("error"):
            raise SourceError(
                f"Sina retrieval failed ({payload['error']}); check AKShare and network access"
            )
        return payload
    finally:
        parent.close()
        process.join(timeout=0.2)
        if process.is_alive():
            process.terminate()
            process.join(timeout=2)
        if process.is_alive():
            process.kill()
            process.join()


class SinaProvider:
    def __init__(self, config: DataConfig, fetcher=fetch_sina_payload):
        self.config, self.fetcher = config, fetcher

    def fetch(
        self, instrument: Instrument, start: date, end: date, as_of: datetime, timeframe="1d"
    ) -> MarketDataBundle:
        if instrument.exchange == "BJ":
            raise DataError(
                "BJ_CAPABILITY_UNVERIFIED: Beijing live data is not enabled in this release"
            )
        calendar = load_calendar(instrument, start, end)
        target = target_session(
            calendar, instrument, as_of, timeframe, end, self.config.delayed_access_buffer_minutes
        )
        payload = self.fetcher(
            instrument.provider_symbol,
            start,
            target,
            self.config.total_fetch_deadline_seconds,
            timeframe,
        )
        observed = datetime.now(UTC)
        bars = []
        for row in payload["raw"]:
            day = date.fromisoformat(str(row["date"])[:10])
            if not start <= day <= target:
                continue
            opened, closed, available = bar_window(
                calendar.session(day), "CN", self.config.delayed_access_buffer_minutes
            )
            if available > as_of:
                continue
            bars.append(
                Bar(
                    start_at=opened,
                    end_at=closed,
                    available_at=available,
                    observed_at=observed,
                    session_date=day,
                    open=row["open"],
                    high=row["high"],
                    low=row["low"],
                    close=row["close"],
                    volume=row["volume"],
                    amount=row.get("amount"),
                    source_revision=digest({k: str(v) for k, v in row.items()}),
                )
            )
        raw = normalize_bars(bars)
        daily_target = latest_analyzable(
            calendar, instrument, as_of, self.config.delayed_access_buffer_minutes, through=end
        )
        if not raw or raw[-1].session_date != daily_target:
            raise DataError(f"STALE_DATA: Sina lacks the required {target} session")
        factors = tuple(
            sorted(
                (
                    FactorPoint(
                        effective_date=date.fromisoformat(str(row["date"])[:10]),
                        factor=float(row["hfq_factor"]),
                    )
                    for row in payload["factors"]
                    if date.fromisoformat(str(row["date"])[:10]) <= target
                ),
                key=lambda p: p.effective_date,
            )
        )
        adjusted = adjust_to_basis(raw, factors, basis_date=target)
        daily, raw_daily, expected = (), (), ()
        if timeframe.endswith("m"):
            daily, raw_daily = adjusted, raw
            zone = ZoneInfo(instrument.timezone)
            minute_start = max(start, target - timedelta(days=126))
            windows = {
                closed: (opened, s.date)
                for s in calendar.sessions
                if minute_start <= s.date <= target
                for opened, closed in intraday_windows(s, int(timeframe[:-1]))
                if closed <= as_of
            }
            minute_bars = []
            for row in payload.get("intraday", []):
                closed = datetime.fromisoformat(str(row["day"]))
                closed = closed.replace(tzinfo=zone) if closed.tzinfo is None else closed
                if closed not in windows:
                    continue
                opened, day = windows[closed]
                minute_bars.append(
                    Bar(
                        start_at=opened,
                        end_at=closed,
                        available_at=closed,
                        observed_at=observed,
                        session_date=day,
                        open=float(row["open"]),
                        high=float(row["high"]),
                        low=float(row["low"]),
                        close=float(row["close"]),
                        volume=float(row["volume"]),
                        amount=float(row["amount"]) if row.get("amount") is not None else None,
                        source_revision=digest({k: str(v) for k, v in row.items()}),
                    )
                )
            raw = normalize_bars(minute_bars)
            expected = expected_starts(
                calendar,
                instrument,
                as_of,
                timeframe,
                through=target,
                since=minute_start,
                auction_sparse=True,
            )
            if not raw or not expected or raw[-1].start_at != expected[-1]:
                raise DataError("STALE_INTRADAY: Sina lacks the required completed bar")
            adjusted = adjust_to_basis(raw, factors, basis_date=target)
        factor_hash = digest(factors)
        return MarketDataBundle(
            instrument=instrument.model_copy(
                update={"identity_verified": True, "name": payload["name"]}
            ),
            as_of=as_of,
            timeframe=timeframe,
            bars=adjusted,
            raw_bars=raw,
            factors=factors,
            sessions=calendar.dates,
            daily_context=daily,
            raw_daily_context=raw_daily,
            expected_bar_starts=expected,
            provenance=Provenance(
                provider="sina",
                feed="sina",
                adjustment="basis_date_qfq",
                basis_date=target,
                price_coordinate_id=digest(["sina", target, factor_hash]),
                factor_hash=factor_hash,
                parser_version=f"sina-v2/akshare/{payload['version']}",
                session_profile="cn_sina_1m_auction_sparse_v1"
                if timeframe == "1m"
                else "cn_exchange_session",
                calendar_version=calendar.version,
                fetched_at=observed,
                requested_start=start,
                requested_end=end,
                amount_currency="CNY",
                volume_comparable=len({p.factor for p in factors}) == 1,
                warnings=("历史因子为本次取得版本，不能据此声称历史PIT；跨公司行为量比未认证",)
                + (
                    (
                        "CN_CLOSING_AUCTION: 收盘集合竞价无成交的两个分钟时隙不补造K线，保留新浪收盘成交bar",
                    )
                    if timeframe == "1m"
                    else ()
                ),
            ),
        )
