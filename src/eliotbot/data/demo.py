"""Deterministic synthetic market input, explicitly labeled as demonstration data."""

from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import numpy as np

from eliotbot.data.calendar import Session
from eliotbot.data.periods import as_weekly, intraday_windows
from eliotbot.data.router import route_symbol
from eliotbot.schemas.market import Bar, MarketDataBundle, Provenance
from eliotbot.timeframes import minutes, normalize_timeframe
from eliotbot.util import digest


def demo_bundle(symbol: str = "AAPL", *, bearish: bool = False, timeframe="1d") -> MarketDataBundle:
    timeframe = normalize_timeframe(timeframe)
    instrument = route_symbol(symbol)
    prices = list(100 + 0.15 * np.sin(np.arange(950 if timeframe == "1w" else 150) / 7))
    volumes = [1000.0] * len(prices)
    for origin, target, count, volume in [
        (prices[-1], 100, 4, 1000),
        (100, 120, 20, 1500),
        (120, 107.64, 14, 700),
        (107.64, 140, 32, 2400),
        (140, 127.64, 30, 600),
        (127.64, 147.64, 25, 1200),
        (147.64, 142, 8, 800),
    ]:
        prices.extend(np.linspace(origin, target, count + 1)[1:])
        volumes.extend([float(volume)] * count)
    if bearish:
        prices = [300 - value for value in prices]
    days = []
    day = date(2018, 1, 2) if timeframe == "1w" else date(2024, 1, 2)
    # Synthetic sessions are declared fixture input, never a live-calendar fallback.
    while len(days) < len(prices) + 10:
        if day.weekday() < 5:
            days.append(day)
        day += timedelta(days=1)
    zone = ZoneInfo(instrument.timezone)
    observed = datetime(2026, 1, 1, tzinfo=UTC)
    bars = []
    duration = minutes(timeframe)
    windows = []
    if duration:
        for day in days:
            session = Session(
                day,
                datetime.combine(day, time(9, 30), zone),
                datetime.combine(day, time(16) if instrument.market == "US" else time(15), zone),
                datetime.combine(day, time(11, 30), zone) if instrument.market == "CN" else None,
                datetime.combine(day, time(13), zone) if instrument.market == "CN" else None,
            )
            windows.extend(
                (opened, closed, day) for opened, closed in intraday_windows(session, duration)
            )
            if len(windows) >= len(prices):
                break
    for i, (close, volume) in enumerate(zip(prices, volumes, strict=True)):
        opened = float(prices[i - 1]) if i else float(close)
        start = datetime.combine(days[i], time(9, 30), zone)
        end = datetime.combine(days[i], time(16) if instrument.market == "US" else time(15), zone)
        session_date = days[i]
        if duration:
            start, end, session_date = windows[i]
        bars.append(
            Bar(
                start_at=start,
                end_at=end,
                available_at=end,
                observed_at=observed,
                session_date=session_date,
                open=opened,
                close=float(close),
                high=max(opened, float(close)) + 0.15,
                low=min(opened, float(close)) - 0.15,
                volume=volume,
                amount=volume * float(close),
                source_revision="synthetic-v1",
            )
        )
    last = bars[-1].session_date
    provenance = Provenance(
        provider="synthetic_demo",
        feed="synthetic",
        adjustment="synthetic_no_actions",
        price_coordinate_id=digest(["synthetic-v1", symbol, bearish]),
        basis_date=last,
        session_profile="synthetic_declared_sessions",
        calendar_version="synthetic-fixture-v1",
        fetched_at=observed,
        requested_start=days[0],
        requested_end=last,
        amount_currency=instrument.currency,
        warnings=("SYNTHETIC_DATA: 这是合成演示行情，不能用作真实股票结论或效果验证",),
    )
    bundle = MarketDataBundle(
        instrument=instrument,
        as_of=bars[-1].usable_at,
        bars=tuple(bars),
        raw_bars=tuple(bars),
        provenance=provenance,
        sessions=tuple(days),
        timeframe=timeframe if duration else "1d",
        expected_bar_starts=tuple(b.start_at for b in bars) if duration else (),
    )
    return as_weekly(bundle) if timeframe == "1w" else bundle
