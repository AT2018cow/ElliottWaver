"""Market-aligned intraday windows and complete calendar-period aggregation."""

from collections.abc import Sequence
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from eliotbot.data.calendar import Session, TradingCalendar, bar_window
from eliotbot.data.resample import weekly_bars
from eliotbot.errors import DataError
from eliotbot.schemas.market import Bar, Instrument, MarketDataBundle
from eliotbot.timeframes import minutes


def intraday_windows(session: Session, duration_minutes: int):
    if duration_minutes < 1:
        raise ValueError("positive interval required")
    segments = [(session.open_at, session.close_at)]
    if session.break_start and session.break_end:
        segments = [(session.open_at, session.break_start), (session.break_end, session.close_at)]
    for start, end in segments:
        while start < end:
            closed = min(start + timedelta(minutes=duration_minutes), end)
            yield start, closed
            start = closed


def target_session(
    calendar: TradingCalendar, instrument: Instrument, as_of, timeframe, end, buffer_minutes=20
):
    duration = minutes(timeframe)
    eligible = []
    if duration:
        delay = timedelta(minutes=buffer_minutes if instrument.market == "US" else 0)
        eligible = [
            s.date
            for s in calendar.sessions
            if s.date <= end
            and any(closed + delay <= as_of for _, closed in intraday_windows(s, duration))
        ]
    elif timeframe == "1w":
        groups = {}
        for s in calendar.sessions:
            groups.setdefault(s.date.isocalendar()[:2], []).append(s)
        eligible = [
            group[-1].date
            for group in groups.values()
            if group[-1].date <= end
            and bar_window(group[-1], instrument.market, buffer_minutes)[2] <= as_of
        ]
    else:
        eligible = [
            s.date
            for s in calendar.sessions
            if s.date <= end and bar_window(s, instrument.market, buffer_minutes)[2] <= as_of
        ]
    if not eligible:
        raise DataError("No completed available bar for this timeframe")
    return max(eligible)


def expected_starts(
    calendar,
    instrument,
    as_of,
    timeframe,
    *,
    buffer_minutes=20,
    through=None,
    since=None,
    auction_sparse=False,
):
    duration = minutes(timeframe)
    if not duration:
        return ()
    delay = timedelta(minutes=buffer_minutes if instrument.market == "US" else 0)
    return tuple(
        opened
        for s in calendar.sessions
        if (through is None or s.date <= through) and (since is None or s.date >= since)
        for opened, closed in intraday_windows(s, duration)
        if closed + delay <= as_of
        and not (
            auction_sparse
            and timeframe == "1m"
            and instrument.market == "CN"
            and s.date >= date(2018, 8, 20)
            and opened.astimezone(ZoneInfo(instrument.timezone)).time()
            in {time(14, 57), time(14, 58)}
        )
    )


def aggregate_intraday(
    bars: Sequence[Bar],
    calendar: TradingCalendar,
    instrument: Instrument,
    timeframe: str,
    as_of: datetime,
    buffer_minutes=20,
) -> tuple[Bar, ...]:
    duration = minutes(timeframe)
    if duration is None:
        raise ValueError("intraday timeframe required")
    result = []
    i = 0
    delay = timedelta(minutes=buffer_minutes if instrument.market == "US" else 0)
    for session in calendar.sessions:
        for opened, closed in intraday_windows(session, duration):
            if closed + delay > as_of:
                continue
            while i < len(bars) and bars[i].start_at < opened:
                i += 1
            j = i
            group = []
            cursor = opened
            while j < len(bars) and bars[j].start_at < closed:
                bar = bars[j]
                if bar.start_at != cursor or bar.end_at > closed:
                    break
                group.append(bar)
                cursor = bar.end_at
                j += 1
            if not group or cursor != closed:
                continue
            result.append(
                Bar(
                    start_at=opened,
                    end_at=closed,
                    available_at=closed + delay,
                    observed_at=max(b.observed_at for b in group),
                    session_date=session.date,
                    open=group[0].open,
                    close=group[-1].close,
                    high=max(b.high for b in group),
                    low=min(b.low for b in group),
                    volume=sum(b.volume for b in group),
                    amount=sum(b.amount for b in group)
                    if all(b.amount is not None for b in group)
                    else None,
                )
            )
            i = j
    return tuple(result)


def as_weekly(bundle: MarketDataBundle) -> MarketDataBundle:
    if bundle.timeframe != "1d":
        raise DataError("Weekly aggregation requires daily input")
    bars = weekly_bars(bundle.bars, bundle.sessions)
    raw = weekly_bars(bundle.raw_bars, bundle.sessions)
    if not bars:
        raise DataError("No complete weeks are available")
    provenance = bundle.provenance.model_copy(update={"basis_date": bars[-1].session_date})
    context = tuple(b for b in bundle.bars if b.end_at <= bars[-1].end_at)
    raw_context = tuple(b for b in bundle.raw_bars if b.end_at <= bars[-1].end_at)
    groups = {}
    for day in bundle.sessions:
        groups.setdefault(day.isocalendar()[:2], []).append(day)
    zone = ZoneInfo(bundle.instrument.timezone)
    start_time = bundle.bars[0].start_at.astimezone(zone).timetz()
    expected = tuple(
        datetime.combine(group[0], start_time)
        for group in groups.values()
        if group[-1] <= bars[-1].session_date
    )
    return MarketDataBundle(
        instrument=bundle.instrument,
        timeframe="1w",
        as_of=bundle.as_of,
        bars=bars,
        raw_bars=raw,
        provenance=provenance,
        sessions=bundle.sessions,
        factors=bundle.factors,
        daily_context=context,
        raw_daily_context=raw_context,
        expected_bar_starts=expected,
    )
