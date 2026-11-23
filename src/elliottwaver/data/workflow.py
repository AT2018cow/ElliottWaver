"""Live acquisition and cache orchestration, outside the pure engine."""

from contextlib import nullcontext
from datetime import UTC, datetime, timedelta
from importlib.metadata import PackageNotFoundError, version
from zoneinfo import ZoneInfo

from elliottwaver.config import Settings
from elliottwaver.data.alpaca_provider import AlpacaProvider
from elliottwaver.data.baostock_provider import BaoStockProvider
from elliottwaver.data.calendar import latest_analyzable, load_calendar
from elliottwaver.data.massive_provider import MassiveIndexProvider
from elliottwaver.data.periods import (
    SINA_AUCTION_PROFILE,
    as_weekly,
    intraday_windows,
    target_session,
)
from elliottwaver.data.router import route_symbol
from elliottwaver.data.sina_provider import SinaProvider
from elliottwaver.data.snapshots import SERIES_FIELDS, SnapshotStore
from elliottwaver.data.source_cache import SourceBlockStore, compatible_provider_basis
from elliottwaver.data.tencent_provider import TencentProvider
from elliottwaver.data.tushare_provider import PARSER_VERSION as TUSHARE_PARSER_VERSION
from elliottwaver.data.tushare_provider import TushareProvider
from elliottwaver.data.yahoo_provider import YahooIndexProvider
from elliottwaver.errors import DataError, InputError, SourceError
from elliottwaver.schemas.market import AnalysisRequest, MarketDataBundle
from elliottwaver.timeframes import minutes
from elliottwaver.util import digest


def fetch_bundle(
    request: AnalysisRequest, settings: Settings, *, refresh=False, provider=None
) -> MarketDataBundle:
    instrument = route_symbol(request.symbol)
    end = request.end or request.as_of.date()
    start = end - timedelta(days=366 * settings.analysis.history_years)
    if request.start:
        start = min(start, request.start - timedelta(days=settings.analysis.warmup_bars * 2))
    anchor_day = (
        request.anchor_at.astimezone(ZoneInfo(instrument.timezone)).date()
        if request.anchor_at
        else request.anchor
    )
    if anchor_day:
        start = min(start, anchor_day - timedelta(days=settings.analysis.warmup_bars * 2))
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
    intraday = request.timeframe.endswith("m")
    if instrument.market == "US" and instrument.asset_type == "INDEX":
        if not settings.data.us_index_feed:
            raise DataError(
                "US_INDEX_FEED_UNCONFIGURED: 配置 data.us_index_feed: massive-index 后才可请求原生美股指数"
            )
        feed = settings.data.us_index_feed
    elif instrument.market == "US":
        if settings.data.us_equity_feed == "yahoo":
            if request.feed:
                raise InputError("--feed sip/iex applies only to Alpaca US equities")
            feed = "yahoo"
        else:
            feed = request.feed or settings.data.us_feed
    elif instrument.asset_type == "INDEX":
        if intraday:
            if not settings.data.cn_index_intraday_feed:
                raise DataError(
                    "INDEX_INTRADAY_UNSUPPORTED: 配置 data.cn_index_intraday_feed 后才可请求指数盘中行情"
                )
            feed = settings.data.cn_index_intraday_feed
        else:
            feed = "sina-index"
    else:
        feed = settings.data.cn_equity_intraday_feed if intraday else "sina"
    if (instrument.market == "CN" or instrument.asset_type == "INDEX") and request.feed:
        raise InputError("--feed sip/iex applies only to US equities")
    try:
        if instrument.market == "US" and instrument.asset_type == "INDEX":
            parser = "yahoo-chart-v1" if feed == "yahoo-index" else "massive-v1"
        elif instrument.market == "US":
            parser = "yahoo-chart-v1" if feed == "yahoo" else "alpaca-rest-v1"
        elif feed == "tencent-cn-index":
            parser = "tencent-mkline-v1"
        elif feed == "tencent-cn-equity":
            parser = "tencent-mkline-v1"
        elif feed == "baostock":
            parser = "baostock-v1"
        elif feed.startswith("tushare"):
            parser = TUSHARE_PARSER_VERSION
        elif instrument.asset_type == "INDEX":
            parser = "sina-index-v1/" + version("akshare")
        else:
            parser = "sina-v2/" + version("akshare")
    except PackageNotFoundError:
        parser = "unavailable"
    if instrument.market == "US" and instrument.asset_type == "INDEX":
        adjustment_key = "none"
    elif instrument.market == "US":
        adjustment_key = "none" if feed == "yahoo" else "all"
    elif instrument.asset_type == "INDEX":
        adjustment_key = "none"
    else:
        adjustment_key = "basis_date_qfq"
    if instrument.market == "US" and instrument.asset_type == "INDEX":
        if feed == "yahoo-index" and request.timeframe == "60m":
            source_timeframe = "30m"
            aggregation = "30m->60m"
        else:
            source_timeframe = request.timeframe if intraday else "1d"
            aggregation = "none"
        session_profile = "us_index_rth"
    elif instrument.market == "US":
        if feed == "yahoo":
            if request.timeframe == "60m":
                source_timeframe = "30m"
                aggregation = "30m->60m"
            else:
                source_timeframe = request.timeframe if intraday else "1d"
                aggregation = "none"
            session_profile = "us_rth_open_aligned"
        else:
            source_timeframe = "1m" if request.timeframe == "1m" else "5m"
            aggregation = (
                "none"
                if request.timeframe == "1m"
                else f"5m->{request.timeframe}"
                if intraday
                else "none"
            )
            session_profile = "us_rth_open_aligned" if intraday else "provider_native_daily"
    else:
        source_timeframe = request.timeframe
        aggregation = "none"
        session_profile = (
            SINA_AUCTION_PROFILE
            if intraday and request.timeframe == "1m" and feed == "sina"
            else "cn_baostock_history"
            if feed == "baostock"
            else "cn_exchange_session"
        )
    auction_sparse = session_profile == SINA_AUCTION_PROFILE
    buffer = settings.data.delayed_access_buffer_minutes
    delay = timedelta(minutes=buffer if instrument.market == "US" else 0)
    required_interval = None
    if intraday:
        required_interval = next(
            (
                slot
                for slot in reversed(
                    tuple(
                        intraday_windows(
                            calendar.session(target),
                            minutes(request.timeframe),
                            auction_sparse=auction_sparse,
                        )
                    )
                )
                if slot[1] + delay <= request.as_of
            ),
            None,
        )
        if required_interval is None:
            raise DataError("STALE_INTRADAY: no completed interval for the requested session")

    def current_tail(bundle):
        last = bundle.bars[-1]
        return last.session_date == target and (
            required_interval is None or (last.start_at, last.end_at) == required_interval
        )

    def check_native(bundle):
        if (
            bundle.instrument.canonical_symbol != instrument.canonical_symbol
            or bundle.instrument.market != instrument.market
            or bundle.instrument.asset_type != instrument.asset_type
            or bundle.provenance.feed != feed
            or bundle.timeframe != source_timeframe
            or bundle.as_of != request.as_of
            or bundle.bars[-1].session_date > target
        ):
            raise DataError("Provider returned a different symbol, feed, timeframe or cutoff")

    cache_identity = [
        instrument.canonical_symbol,
        instrument.asset_type,
        feed,
        start,
        target,
        calendar.version,
        parser,
        adjustment_key,
        settings.data.delayed_access_buffer_minutes,
        request.timeframe,
        session_profile,
        source_timeframe,
        aggregation,
        (required_interval, "intraday-grid-v2") if intraday else (),
        "snapshot-v1.1",
    ]
    if intraday:
        # Native daily bars can become available while the final intraday bar stays the same.
        daily_context_end = latest_analyzable(
            calendar,
            instrument,
            request.as_of,
            settings.data.delayed_access_buffer_minutes,
            through=target,
        )
        cache_identity.append(daily_context_end)
    key = digest(cache_identity)
    store = SnapshotStore(settings.cache.directory)
    source_store = SourceBlockStore(settings.cache.directory) if intraday else None
    source_key = (
        digest(
            [
                instrument.canonical_symbol,
                instrument.market,
                instrument.asset_type,
                feed,
                source_timeframe,
                session_profile,
                parser,
                adjustment_key,
                calendar.version.rsplit("/", 1)[0],
                buffer,
                "source-block-v2",
            ]
        )
        if intraday
        else None
    )
    source_lock = source_store.lock(source_key) if source_store is not None else nullcontext()
    with store.lock(key), source_lock:
        lookup_key = (
            digest([key, source_store.revision_token(source_key, start, target, request.as_of)])
            if source_store is not None
            else key
        )
        if not refresh:
            cached = store.lookup(
                lookup_key, datetime.now(UTC), settings.data.cache_ttl_seconds, as_of=request.as_of
            )
            if (
                cached is not None
                and current_tail(cached)
                and cached.instrument.canonical_symbol == instrument.canonical_symbol
                and cached.instrument.market == instrument.market
                and cached.provenance.feed == feed
                and cached.timeframe == request.timeframe
            ):
                if request.as_of == cached.as_of:
                    return cached
                if request.as_of > cached.as_of or all(
                    bar.usable_at <= request.as_of
                    for field in SERIES_FIELDS
                    for bar in getattr(cached, field)
                ):
                    return store.save_at_cutoff(cached, request.as_of)
        source = provider or (
            YahooIndexProvider(settings.data, feed=feed)
            if instrument.market == "US"
            and instrument.asset_type == "INDEX"
            and feed == "yahoo-index"
            else MassiveIndexProvider(settings.data, feed=feed)
            if instrument.market == "US" and instrument.asset_type == "INDEX"
            else AlpacaProvider(settings.data, feed=feed)
            if instrument.market == "US" and feed in {"sip", "iex"}
            else YahooIndexProvider(settings.data, feed=feed)
            if instrument.market == "US" and feed == "yahoo"
            else TencentProvider(settings.data, feed=feed)
            if feed in {"tencent-cn-index", "tencent-cn-equity"}
            else BaoStockProvider(settings.data, feed=feed)
            if feed == "baostock"
            else TushareProvider(settings.data, feed=feed)
            if feed.startswith("tushare")
            else SinaProvider(settings.data)
        )
        if source_store is not None and source_key is not None:
            blocks = source_store.latest_blocks(source_key)
            first = min(blocks, default=target)
            previous_start = source_store.requested_start(source_key)
            backfill = (
                not blocks
                or target < first
                or (
                    (request.start is not None or anchor_day is not None)
                    and previous_start is not None
                    and start < previous_start
                )
            )
            floor = start if backfill else max(start, first)
            desired = tuple(s for s in calendar.sessions if floor <= s.date <= target)
            missing = []
            for session in desired:
                block = blocks.get(session.date)
                if block is None:
                    missing.append(session.date)
                    continue
                slots = tuple(
                    intraday_windows(
                        session, minutes(source_timeframe), auction_sparse=auction_sparse
                    )
                )
                grid = set(slots)
                if any((bar.start_at, bar.end_at) not in grid for bar in block.raw_bars):
                    missing.append(session.date)
                    continue
                tail = next(
                    (slot for slot in reversed(slots) if slot[1] + delay <= request.as_of), None
                )
                if tail is not None and (block.bars[-1].start_at, block.bars[-1].end_at) < tail:
                    missing.append(session.date)
            if refresh:
                missing.append(target)
            context_through = source_store.context_through(source_key)
            if context_through and daily_context_end and context_through < daily_context_end:
                missing.append(daily_context_end)
            fetch_start = min(missing, default=None)
            if fetch_start is not None:
                # Overlap the tail for price-basis validation and daily background.
                fetch_start = max(start, fetch_start - timedelta(days=7))
            if fetch_start is None:
                assembled = source_store.assemble(
                    source_key,
                    instrument=instrument,
                    calendar=calendar,
                    timeframe=request.timeframe,
                    source_timeframe=source_timeframe,
                    as_of=request.as_of,
                    start=start,
                    end=target,
                    buffer_minutes=buffer,
                )
                if assembled is not None and current_tail(assembled):
                    store.remember(lookup_key, assembled)
                    return assembled
                fetch_start = max(start, target - timedelta(days=7))
            try:
                native = source.fetch(
                    instrument, fetch_start, target, request.as_of, timeframe=source_timeframe
                )
                check_native(native)
                if not compatible_provider_basis(blocks, native, start, target):
                    if fetch_start > start:
                        fetch_start = start
                        native = source.fetch(
                            instrument, start, target, request.as_of, timeframe=source_timeframe
                        )
                        check_native(native)
                    if not compatible_provider_basis(blocks, native, start, target):
                        raise DataError(
                            "PRICE_BASIS_UNVERIFIED: cached source blocks cannot be aligned"
                        )
            except (DataError, SourceError) as exc:
                source_store.record_failure(
                    source_key,
                    tuple(session.date for session in desired if session.date >= fetch_start),
                    str(exc),
                )
                raise
            coverage_start = native.bars[0].session_date
            coverage_end = native.bars[-1].session_date
            source_store.store_bundle(
                source_key,
                native,
                sessions=tuple(
                    session.date
                    for session in desired
                    if coverage_start <= session.date <= coverage_end
                ),
                source_timeframe=source_timeframe,
                session_profile=session_profile,
                parser_version=parser,
                calendar_version=calendar.version,
                requested_start=fetch_start,
            )
            if coverage_end < target:
                source_store.record_failure(
                    source_key,
                    tuple(session.date for session in desired if coverage_end < session.date),
                    "PARTIAL_PROVIDER_COVERAGE",
                )
            bundle = source_store.assemble(
                source_key,
                instrument=instrument,
                calendar=calendar,
                timeframe=request.timeframe,
                source_timeframe=source_timeframe,
                as_of=request.as_of,
                start=start,
                end=target,
                buffer_minutes=buffer,
            )
            if bundle is None or not current_tail(bundle):
                error = "STALE_INTRADAY: source blocks lack the required completed bar"
                source_store.record_failure(source_key, (target,), error)
                raise DataError(error)
            lookup_key = digest(
                [key, source_store.revision_token(source_key, start, target, request.as_of)]
            )
        else:
            provider_timeframe = "1d" if request.timeframe == "1w" else request.timeframe
            bundle = source.fetch(
                instrument, start, target, request.as_of, timeframe=provider_timeframe
            )
            if request.timeframe == "1w":
                bundle = as_weekly(bundle)
        if (
            bundle.instrument.canonical_symbol != instrument.canonical_symbol
            or bundle.instrument.market != instrument.market
            or bundle.provenance.feed != feed
            or bundle.timeframe != request.timeframe
            or bundle.bars[-1].session_date != target
        ):
            raise DataError(
                "Provider returned a different symbol, feed, timeframe or target session"
            )
        store.remember(lookup_key, bundle)
        return bundle
