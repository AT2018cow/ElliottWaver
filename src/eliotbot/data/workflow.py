"""Live acquisition and cache orchestration, outside the pure engine."""

from datetime import UTC, datetime, timedelta
from importlib.metadata import PackageNotFoundError, version

from eliotbot.config import Settings
from eliotbot.data.alpaca_provider import AlpacaProvider
from eliotbot.data.calendar import load_calendar
from eliotbot.data.periods import as_weekly, expected_starts, target_session
from eliotbot.data.router import route_symbol
from eliotbot.data.sina_provider import SinaProvider
from eliotbot.data.snapshots import SnapshotStore
from eliotbot.errors import InputError
from eliotbot.schemas.market import AnalysisRequest, MarketDataBundle
from eliotbot.util import digest


def fetch_bundle(
    request: AnalysisRequest, settings: Settings, *, refresh=False, provider=None
) -> MarketDataBundle:
    instrument = route_symbol(request.symbol)
    end = request.end or request.as_of.date()
    start = end - timedelta(days=366 * settings.analysis.history_years)
    if request.start:
        start = min(start, request.start - timedelta(days=settings.analysis.warmup_bars * 2))
    if request.anchor:
        start = min(start, request.anchor - timedelta(days=settings.analysis.warmup_bars * 2))
    calendar = load_calendar(instrument, start, end)
    target = target_session(
        calendar,
        instrument,
        request.as_of,
        request.timeframe,
        end,
        settings.data.delayed_access_buffer_minutes,
    )
    if request.start and request.start > target:
        raise InputError("Analysis start follows the latest available session")
    feed = (request.feed or settings.data.us_feed) if instrument.market == "US" else "sina"
    if instrument.market == "CN" and request.feed:
        raise InputError("--feed sip/iex applies only to US equities")
    try:
        parser = "sina-v2/" + version("akshare") if instrument.market == "CN" else "alpaca-rest-v1"
    except PackageNotFoundError:
        parser = "unavailable"
    key = digest(
        [
            instrument.canonical_symbol,
            feed,
            start,
            target,
            calendar.version,
            parser,
            "all" if instrument.market == "US" else "basis_date_qfq",
            settings.data.delayed_access_buffer_minutes,
            request.timeframe,
            expected_starts(
                calendar,
                instrument,
                request.as_of,
                request.timeframe,
                buffer_minutes=settings.data.delayed_access_buffer_minutes,
                through=end,
                since=target,
                auction_sparse=instrument.market == "CN",
            )[-1:]
            if request.timeframe.endswith("m")
            else (),
            "snapshot-v1.1",
        ]
    )
    store = SnapshotStore(settings.cache.directory)
    with store.lock(key):
        if not refresh:
            cached = store.lookup(key, datetime.now(UTC), settings.data.cache_ttl_seconds)
            if cached is not None and cached.bars[-1].session_date == target:
                # Calendar validation above proves the completed target is unchanged.
                # Keep the actual fetched timestamp; never pretend the cache was downloaded again.
                return cached.model_copy(update={"as_of": request.as_of})
        source = provider or (
            AlpacaProvider(settings.data, feed=feed)
            if instrument.market == "US"
            else SinaProvider(settings.data)
        )
        provider_timeframe = "1d" if request.timeframe == "1w" else request.timeframe
        bundle = source.fetch(
            instrument, start, target, request.as_of, timeframe=provider_timeframe
        )
        if request.timeframe == "1w":
            bundle = as_weekly(bundle)
        if bundle.provenance.feed != feed or bundle.bars[-1].session_date != target:
            from eliotbot.errors import DataError

            raise DataError("Provider returned a different feed or incomplete target session")
        store.remember(key, bundle)
        return bundle
