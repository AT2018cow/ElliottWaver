"""Market-aligned intraday windows and complete calendar-period aggregation."""

from collections.abc import Sequence
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from elliottwaver.data.calendar import Session, TradingCalendar, bar_window, load_calendar
from elliottwaver.data.resample import weekly_bars
from elliottwaver.errors import DataError
from elliottwaver.schemas.market import Bar, Instrument, MarketDataBundle
from elliottwaver.timeframes import minutes
from elliottwaver.util import digest

SINA_AUCTION_PROFILE = "cn_sina_1m_auction_sparse_v1"


def available_pairs(
    bars: Sequence[Bar], raw_bars: Sequence[Bar], cutoff: datetime, *, through=None, end=None
) -> tuple[tuple[Bar, Bar], ...]:
    """Filter the already timestamp-aligned series together at the decision cutoff."""
    return tuple(
        (bar, raw)
        for bar, raw in zip(bars, raw_bars, strict=True)
        if bar.is_final
        and raw.is_final
        and max(bar.usable_at, raw.usable_at) <= cutoff
        and (through is None or bar.session_date <= through)
        and (end is None or bar.end_at <= end)
    )


def uses_sparse_sina_auction(instrument: Instrument, timeframe: str, feed: str) -> bool:
    return (
        timeframe == "1m"
        and feed == "sina"
        and instrument.market == "CN"
        and instrument.asset_type == "EQUITY"
    )


def sina_auction_empty(opened: datetime) -> bool:
    local = opened.astimezone(ZoneInfo("Asia/Shanghai"))
    return local.date() >= date(2018, 8, 20) and local.time() in {time(14, 57), time(14, 58)}


def intraday_windows(session: Session, duration_minutes: int, *, auction_sparse=False):
    if duration_minutes < 1:
        raise ValueError("positive interval required")
    segments = [(session.open_at, session.close_at)]
    if session.break_start and session.break_end:
        segments = [(session.open_at, session.break_start), (session.break_end, session.close_at)]
    for start, end in segments:
        while start < end:
            closed = min(start + timedelta(minutes=duration_minutes), end)
            if not (auction_sparse and duration_minutes == 1 and sina_auction_empty(start)):
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
        for opened, closed in intraday_windows(
            s, duration, auction_sparse=auction_sparse and instrument.market == "CN"
        )
        if closed + delay <= as_of
    )


def aggregate_intraday(
    bars: Sequence[Bar],
    calendar: TradingCalendar,
    instrument: Instrument,
    timeframe: str,
    as_of: datetime,
    buffer_minutes=20,
    *,
    auction_sparse=False,
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
                while auction_sparse and cursor < bar.start_at and sina_auction_empty(cursor):
                    cursor += timedelta(minutes=1)
                if (
                    bar.start_at != cursor
                    or bar.end_at > closed
                    or not bar.is_final
                    or bar.usable_at > as_of
                ):
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
                    available_at=max(closed + delay, max(b.usable_at for b in group)),
                    observed_at=max(b.observed_at for b in group),
                    session_date=session.date,
                    open=group[0].open,
                    close=group[-1].close,
                    high=max(b.high for b in group),
                    low=min(b.low for b in group),
                    volume=sum(b.volume for b in group)
                    if all(b.volume is not None for b in group)
                    else None,
                    amount=sum(b.amount for b in group)
                    if all(b.amount is not None for b in group)
                    else None,
                    source_revision=digest([b.bar_id for b in group]),
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
        # Retain observed daily closes even when an incomplete week has no weekly OHLC.
        daily_context=bundle.bars,
        raw_daily_context=bundle.raw_bars,
        expected_bar_starts=expected,
    )


def resample_bundle(
    bundle: MarketDataBundle,
    target_timeframe: str,
    *,
    as_of: datetime | None = None,
    buffer_minutes: int = 0,
) -> MarketDataBundle:
    """Derive a coarser intraday/daily background from the same source bundle."""
    target_timeframe = str(target_timeframe)
    source_timeframe = bundle.timeframe
    if target_timeframe == source_timeframe:
        return bundle
    if source_timeframe == "1d" and target_timeframe == "1w":
        return as_weekly(bundle)
    cutoff = as_of or bundle.as_of
    calendar = load_calendar(bundle.instrument, bundle.sessions[0], bundle.sessions[-1])
    auction_sparse = uses_sparse_sina_auction(
        bundle.instrument, source_timeframe, bundle.provenance.feed
    )
    duration = minutes(target_timeframe)
    if duration is not None:
        pairs = available_pairs(bundle.bars, bundle.raw_bars, cutoff)
        analysis_input, raw_input = zip(*pairs, strict=True) if pairs else ((), ())
        bars = aggregate_intraday(
            analysis_input,
            calendar,
            bundle.instrument,
            target_timeframe,
            cutoff,
            buffer_minutes=buffer_minutes,
            auction_sparse=auction_sparse,
        )
        raw = aggregate_intraday(
            raw_input,
            calendar,
            bundle.instrument,
            target_timeframe,
            cutoff,
            buffer_minutes=buffer_minutes,
            auction_sparse=auction_sparse,
        )
        expected = expected_starts(
            calendar,
            bundle.instrument,
            cutoff,
            target_timeframe,
            buffer_minutes=buffer_minutes,
            since=bundle.sessions[0],
        )
        if bars:
            expected = tuple(item for item in expected if item <= bars[-1].start_at)
        aggregation = f"{source_timeframe}->{target_timeframe}"
    elif target_timeframe == "1d":
        source_duration = minutes(source_timeframe)
        if source_duration is None:
            raise DataError("Daily background requires intraday source bars")
        bars = []
        raw = []
        for session in calendar.sessions:
            windows = tuple(
                intraday_windows(session, source_duration, auction_sparse=auction_sparse)
            )
            group = [
                bar
                for bar in bundle.bars
                if bar.session_date == session.date and bar.usable_at <= cutoff
            ]
            raw_group = [
                bar
                for bar in bundle.raw_bars
                if bar.session_date == session.date and bar.usable_at <= cutoff
            ]
            if not windows or [bar.start_at for bar in group] != [opened for opened, _ in windows]:
                continue
            if [bar.start_at for bar in raw_group] != [bar.start_at for bar in group]:
                continue
            bars.append(
                Bar(
                    start_at=session.open_at,
                    end_at=session.close_at,
                    available_at=max(bar.usable_at for bar in group),
                    observed_at=max(bar.observed_at for bar in group),
                    session_date=session.date,
                    open=group[0].open,
                    close=group[-1].close,
                    high=max(bar.high for bar in group),
                    low=min(bar.low for bar in group),
                    volume=sum(bar.volume for bar in group)
                    if all(bar.volume is not None for bar in group)
                    else None,
                    amount=sum(bar.amount for bar in group)
                    if all(bar.amount is not None for bar in group)
                    else None,
                    source_revision=digest([bar.bar_id for bar in group]),
                )
            )
            raw.append(
                Bar(
                    **{
                        **bars[-1].model_dump(),
                        "open": raw_group[0].open,
                        "high": max(bar.high for bar in raw_group),
                        "low": min(bar.low for bar in raw_group),
                        "close": raw_group[-1].close,
                        "volume": sum(bar.volume for bar in raw_group)
                        if all(bar.volume is not None for bar in raw_group)
                        else None,
                        "amount": sum(bar.amount for bar in raw_group)
                        if all(bar.amount is not None for bar in raw_group)
                        else None,
                        "source_revision": digest([bar.bar_id for bar in raw_group]),
                    }
                )
            )
        expected = ()
        aggregation = f"{source_timeframe}->1d"
    else:
        raise DataError(f"Unsupported background resample: {source_timeframe}->{target_timeframe}")
    if not bars:
        raise DataError(f"Background resample produced no {target_timeframe} bars")
    if len(bars) != len(raw):
        raise DataError("Background raw and adjusted bars do not align")
    provenance = bundle.provenance.model_copy(
        update={
            "source_timeframe": source_timeframe,
            "aggregation": aggregation,
            "coverage_start": bars[0].session_date,
            "coverage_end": bars[-1].session_date,
            "coverage_reason": "DERIVED_BACKGROUND",
            "price_coordinate_id": digest(
                ["background", bundle.provenance.price_coordinate_id, aggregation]
            ),
            "basis_date": bars[-1].session_date,
            "warnings": (*bundle.provenance.warnings, f"DERIVED_BACKGROUND: {aggregation}"),
        }
    )
    return bundle.model_copy(
        update={
            "timeframe": target_timeframe,
            "bars": tuple(bars),
            "raw_bars": tuple(raw),
            "sessions": calendar.dates,
            "expected_bar_starts": expected,
            "provenance": provenance,
        }
    )


def background_bundle(
    bundle: MarketDataBundle, target_timeframe: str, *, as_of: datetime
) -> MarketDataBundle:
    """Use the source's native daily bars for the hourly brief's background."""
    if target_timeframe != "1d":
        return resample_bundle(bundle, target_timeframe, as_of=as_of)
    pairs = available_pairs(bundle.daily_context, bundle.raw_daily_context, as_of)
    if not pairs:
        raise DataError("NATIVE_DAILY_UNAVAILABLE: 截止时刻没有可用的原生日线")
    bars, raw = zip(*pairs, strict=True)
    calendar = load_calendar(bundle.instrument, bars[0].session_date, bars[-1].session_date)
    provenance = bundle.provenance.model_copy(
        update={
            "source_timeframe": "1d",
            "aggregation": "none",
            "coverage_start": bars[0].session_date,
            "coverage_end": bars[-1].session_date,
            "coverage_reason": "NATIVE_DAILY_BACKGROUND",
            "session_profile": "provider_native_daily",
            "calendar_version": calendar.version,
            "price_coordinate_id": digest(
                ["native-daily-background", bundle.provenance.price_coordinate_id, bars]
            ),
            "basis_date": bars[-1].session_date,
        }
    )
    return bundle.model_copy(
        update={
            "timeframe": "1d",
            "bars": bars,
            "raw_bars": raw,
            "sessions": calendar.dates,
            "expected_bar_starts": (),
            "provenance": provenance,
        }
    )
