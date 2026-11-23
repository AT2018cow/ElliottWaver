"""Live acquisition and cache orchestration, outside the pure engine."""

from datetime import UTC, datetime, timedelta
from importlib.metadata import PackageNotFoundError, version

from elliottwaver.config import Settings
from elliottwaver.data.alpaca_provider import AlpacaProvider
from elliottwaver.data.calendar import latest_analyzable, load_calendar
from elliottwaver.data.massive_provider import MassiveIndexProvider
from elliottwaver.data.periods import (
    as_weekly,
    expected_starts,
    resample_bundle,
    target_session,
)
from elliottwaver.data.router import route_symbol
from elliottwaver.data.sina_provider import SinaProvider
from elliottwaver.data.snapshots import SERIES_FIELDS, SnapshotStore
from elliottwaver.data.source_cache import SourceBlockStore
from elliottwaver.data.tencent_provider import TencentProvider
from elliottwaver.data.tushare_provider import TushareProvider
from elliottwaver.data.yahoo_provider import YahooIndexProvider
from elliottwaver.errors import DataError, InputError, SourceError
from elliottwaver.schemas.market import AnalysisRequest, MarketDataBundle
from elliottwaver.util import digest


def fetch_bundle(
    request: AnalysisRequest, settings: Settings, *, refresh=False, provider=None
) -> MarketDataBundle:
    instrument = route_symbol(request.symbol)
    end = request.end or request.as_of.date()
    start = end - timedelta(days=366 * settings.analysis.history_years)
    if request.start:
        start = min(start, request.start - timedelta(days=settings.analysis.warmup_bars * 2))
    anchor_day = request.anchor_at.date() if request.anchor_at else request.anchor
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
            parser = "alpaca-rest-v1"
        elif feed == "tencent-cn-index":
            parser = "tencent-mkline-v1"
        elif feed.startswith("tushare"):
            parser = "tushare-v1"
        elif instrument.asset_type == "INDEX":
            parser = "sina-index-v1/" + version("akshare")
        else:
            parser = "sina-v2/" + version("akshare")
    except PackageNotFoundError:
        parser = "unavailable"
    if instrument.market == "US" and instrument.asset_type == "INDEX":
        adjustment_key = "none"
    elif instrument.market == "US":
        adjustment_key = "all"
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
            "cn_sina_1m_auction_sparse_v1"
            if intraday and request.timeframe == "1m" and feed == "sina"
            else "cn_exchange_session"
        )
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
        if intraday
        else (),
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
                calendar.version,
                daily_context_end if intraday else None,
            ]
        )
        if intraday
        else None
    )
    with store.lock(key):
        if not refresh:
            cached = store.lookup(
                key, datetime.now(UTC), settings.data.cache_ttl_seconds, as_of=request.as_of
            )
            if (
                cached is not None
                and cached.bars[-1].session_date == target
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
            if instrument.market == "US"
            else TencentProvider(settings.data, feed=feed)
            if feed == "tencent-cn-index"
            else TushareProvider(settings.data, feed=feed)
            if feed.startswith("tushare")
            else SinaProvider(settings.data)
        )
        if source_store is not None and source_key is not None:
            with source_store.lock(source_key):
                desired = tuple(session for session in calendar.dates if start <= session <= target)
                blocks = source_store.latest_blocks(source_key)
                stale = tuple(
                    session
                    for session, block in blocks.items()
                    if block.cutoff < request.as_of and session >= start
                )
                missing = tuple(
                    session for session in desired if session not in blocks or session in stale
                )
                fetch_start = min(missing) if missing else None
                if refresh:
                    if blocks:
                        refresh_start = max(start, max(blocks))
                        fetch_start = (
                            min(fetch_start, refresh_start)
                            if fetch_start is not None
                            else refresh_start
                        )
                    else:
                        fetch_start = start
                if fetch_start is not None:
                    # Give providers enough prior daily context for the tail session.
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
                        buffer_minutes=settings.data.delayed_access_buffer_minutes,
                    )
                    if assembled is not None and assembled.bars[-1].session_date == target:
                        store.remember(key, assembled)
                        return assembled
                    fetch_start = start
                try:
                    native = source.fetch(
                        instrument, fetch_start, target, request.as_of, timeframe=source_timeframe
                    )
                except (DataError, SourceError) as exc:
                    source_store.record_failure(
                        source_key,
                        tuple(session for session in desired if session >= fetch_start),
                        str(exc),
                    )
                    raise
                coverage_end = native.bars[-1].session_date if native.bars else fetch_start
                source_store.store_bundle(
                    source_key,
                    native,
                    sessions=tuple(
                        session for session in desired if fetch_start <= session <= coverage_end
                    ),
                    source_timeframe=source_timeframe,
                    session_profile=session_profile,
                    parser_version=parser,
                    calendar_version=calendar.version,
                )
                if coverage_end < target:
                    source_store.record_failure(
                        source_key,
                        tuple(session for session in desired if coverage_end < session <= target),
                        "PARTIAL_PROVIDER_COVERAGE",
                    )
                assembled = source_store.assemble(
                    source_key,
                    instrument=instrument,
                    calendar=calendar,
                    timeframe=request.timeframe,
                    source_timeframe=source_timeframe,
                    as_of=request.as_of,
                    start=start,
                    end=target,
                    buffer_minutes=0,
                )
                bundle = (
                    assembled
                    if assembled is not None and assembled.bars[-1].session_date == target
                    else native
                    if source_timeframe == request.timeframe
                    else resample_bundle(
                        native,
                        request.timeframe,
                        as_of=request.as_of,
                        buffer_minutes=0,
                    )
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
        store.remember(key, bundle)
        return bundle
