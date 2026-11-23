from collections.abc import Sequence
from zoneinfo import ZoneInfo

from elliottwaver.data.periods import sina_auction_empty, uses_sparse_sina_auction
from elliottwaver.errors import DataError
from elliottwaver.schemas.market import Bar, MarketDataBundle


def normalize_bars(bars: Sequence[Bar]) -> tuple[Bar, ...]:
    by_time = {}
    for bar in bars:
        if bar.start_at in by_time and by_time[bar.start_at].bar_id != bar.bar_id:
            raise DataError("Conflicting source revisions at the same bar timestamp")
        by_time[bar.start_at] = bar
    return tuple(by_time[key] for key in sorted(by_time))


def missing_sessions(bundle: MarketDataBundle, start=None) -> tuple:
    first = start or bundle.bars[0].session_date
    last = bundle.bars[-1].session_date
    observed = {b.session_date for b in bundle.bars}
    suspensions = set(bundle.provenance.known_suspensions)
    return tuple(
        day
        for day in bundle.sessions
        if first <= day <= last and day not in observed and day not in suspensions
    )


def missing_intervals(bundle: MarketDataBundle, start=None) -> tuple:
    if not bundle.expected_bar_starts:
        return missing_sessions(bundle, start) if bundle.timeframe == "1d" else ()
    present = {b.start_at for b in bundle.bars}
    suspended = set(bundle.provenance.known_suspensions)
    zone = ZoneInfo(bundle.instrument.timezone)
    auction_sparse = uses_sparse_sina_auction(
        bundle.instrument, bundle.timeframe, bundle.provenance.feed
    )
    first, last = bundle.bars[0].start_at, bundle.bars[-1].start_at
    return tuple(
        stamp
        for stamp in bundle.expected_bar_starts
        if first <= stamp <= last
        and (start is None or stamp.astimezone(zone).date() >= start)
        and stamp not in present
        and stamp.astimezone(zone).date() not in suspended
        and not (auction_sparse and sina_auction_empty(stamp))
    )
