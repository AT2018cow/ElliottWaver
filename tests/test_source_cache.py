import sqlite3
import threading
import time
from datetime import UTC, date, datetime, timedelta

import pytest

from elliottwaver.config import Settings
from elliottwaver.data.calendar import load_calendar
from elliottwaver.data.demo import demo_bundle
from elliottwaver.data.periods import SINA_AUCTION_PROFILE, intraday_windows, resample_bundle
from elliottwaver.data.quality import missing_intervals
from elliottwaver.data.snapshots import SnapshotStore
from elliottwaver.data.source_cache import SourceBlockStore
from elliottwaver.data.workflow import fetch_bundle
from elliottwaver.errors import DataError
from elliottwaver.schemas.market import AnalysisRequest, Bar, MarketDataBundle


class CountingProvider:
    def __init__(self, bundle: MarketDataBundle):
        def delayed(series):
            return tuple(
                bar.model_copy(update={"available_at": bar.end_at + timedelta(minutes=20)})
                for bar in series
            )

        bars = delayed(bundle.bars)
        self.bundle = bundle.model_copy(
            update={
                "as_of": bars[-1].usable_at,
                "bars": bars,
                "raw_bars": delayed(bundle.raw_bars),
                "provenance": bundle.provenance.model_copy(
                    update={"feed": "sip", "adjustment": "provider_all"}
                ),
            }
        )
        self.calls: list[tuple[date, date, str]] = []
        self.fail_next = False
        self.max_session: date | None = None
        self.delay = 0.0

    def fetch(self, instrument, start, end, as_of, timeframe="1d"):
        self.calls.append((start, end, timeframe))
        if self.delay:
            time.sleep(self.delay)
        if self.fail_next:
            self.fail_next = False
            raise DataError("fixture provider failure")
        effective_end = min(end, self.max_session) if self.max_session else end
        bars = tuple(
            bar
            for bar in self.bundle.bars
            if start <= bar.session_date <= effective_end and bar.usable_at <= as_of
        )
        raw = tuple(
            bar
            for bar in self.bundle.raw_bars
            if start <= bar.session_date <= effective_end and bar.usable_at <= as_of
        )
        if not bars:
            raise DataError("fixture has no bars in the requested range")
        expected = tuple(
            item for item in self.bundle.expected_bar_starts if item <= bars[-1].start_at
        )
        provenance = self.bundle.provenance.model_copy(
            update={
                "basis_date": bars[-1].session_date,
                "coverage_start": bars[0].session_date,
                "coverage_end": bars[-1].session_date,
            }
        )
        return self.bundle.model_copy(
            update={
                "as_of": as_of,
                "bars": bars,
                "raw_bars": raw,
                "expected_bar_starts": expected,
                "daily_context": tuple(
                    bar for bar in self.bundle.daily_context if bar.usable_at <= as_of
                ),
                "raw_daily_context": tuple(
                    bar for bar in self.bundle.raw_daily_context if bar.usable_at <= as_of
                ),
                "provenance": provenance,
            }
        )

    def revise_last(self, delta: float) -> None:
        bars = list(self.bundle.bars)
        raw = list(self.bundle.raw_bars)
        last = bars[-1]
        revised = last.close + delta
        bars[-1] = last.model_copy(
            update={
                "close": revised,
                "high": max(last.high, revised),
                "low": min(last.low, revised),
            }
        )
        raw_last = raw[-1]
        raw[-1] = raw_last.model_copy(
            update={
                "close": raw_last.close + delta,
                "high": max(raw_last.high, raw_last.close + delta),
                "low": min(raw_last.low, raw_last.close + delta),
            }
        )
        self.bundle = self.bundle.model_copy(update={"bars": tuple(bars), "raw_bars": tuple(raw)})


def _settings(tmp_path):
    return Settings(cache={"directory": tmp_path}, analysis={"history_years": 1})


def _source_key(tmp_path) -> str:
    with sqlite3.connect(tmp_path / "source-cache.sqlite3") as db:
        return db.execute("SELECT source_key FROM source_meta LIMIT 1").fetchone()[0]


def test_cold_fetch_then_hot_hit_does_not_call_provider(tmp_path):
    provider = CountingProvider(demo_bundle("AAPL", timeframe="5m"))
    settings = _settings(tmp_path)
    request = AnalysisRequest(
        symbol="AAPL", as_of=provider.bundle.as_of, timeframe="5m", mode="FROZEN_DATA_REPLAY"
    )
    first = fetch_bundle(request, settings, provider=provider)
    assert len(provider.calls) == 1
    source_key = _source_key(tmp_path)
    assert SourceBlockStore(tmp_path).revision_count(source_key) > 0

    second = fetch_bundle(request, settings, provider=provider)
    assert len(provider.calls) == 1
    assert second.bars[-1].bar_id == first.bars[-1].bar_id


def test_tail_refresh_only_fetches_last_session_and_preserves_revision(tmp_path):
    provider = CountingProvider(demo_bundle("AAPL", timeframe="5m"))
    settings = _settings(tmp_path)
    request = AnalysisRequest(
        symbol="AAPL", as_of=provider.bundle.as_of, timeframe="5m", mode="FROZEN_DATA_REPLAY"
    )
    first = fetch_bundle(request, settings, provider=provider)
    source_key = _source_key(tmp_path)
    session = first.bars[-1].session_date
    before = SourceBlockStore(tmp_path).revision_count(source_key, session)
    provider.revise_last(1.0)
    refreshed = fetch_bundle(request, settings, provider=provider, refresh=True)
    assert len(provider.calls) == 2
    assert session - timedelta(days=7) <= provider.calls[-1][0] <= session
    assert SourceBlockStore(tmp_path).revision_count(source_key, session) == before + 1
    assert refreshed.bars[-1].close == first.bars[-1].close + 1.0


def test_shared_source_blocks_serve_a_coarser_timeframe_without_provider(tmp_path):
    provider = CountingProvider(demo_bundle("AAPL", timeframe="5m"))
    settings = _settings(tmp_path)
    request = AnalysisRequest(
        symbol="AAPL", as_of=provider.bundle.as_of, timeframe="5m", mode="FROZEN_DATA_REPLAY"
    )
    fetch_bundle(request, settings, provider=provider)
    assert len(provider.calls) == 1
    coarse = fetch_bundle(
        request.model_copy(update={"timeframe": "15m"}), settings, provider=provider
    )
    assert len(provider.calls) == 1
    assert coarse.timeframe == "15m"
    assert coarse.bars


def test_source_revision_invalidates_warm_coarse_snapshots(tmp_path):
    provider = CountingProvider(demo_bundle("AAPL", timeframe="5m"))
    now = datetime.now(UTC)
    provider.bundle = provider.bundle.model_copy(
        update={"provenance": provider.bundle.provenance.model_copy(update={"fetched_at": now})}
    )
    settings = _settings(tmp_path)
    fine_request = AnalysisRequest(symbol="AAPL", as_of=provider.bundle.as_of, timeframe="5m")
    coarse_request = fine_request.model_copy(update={"timeframe": "15m"})
    fine = fetch_bundle(fine_request, settings, provider=provider)
    old = fetch_bundle(coarse_request, settings, provider=provider)
    revised_bar = next(bar for bar in fine.bars if bar.end_at == old.bars[-1].end_at)
    revised_high = old.bars[-1].high + 30.0
    provider.bundle = provider.bundle.model_copy(
        update={
            field: tuple(
                bar.model_copy(update={"high": revised_high})
                if bar.start_at == revised_bar.start_at
                else bar
                for bar in getattr(provider.bundle, field)
            )
            for field in ("bars", "raw_bars")
        }
    )

    fetch_bundle(fine_request, settings, provider=provider, refresh=True)
    new = fetch_bundle(coarse_request, settings, provider=provider)
    assert new.bars[-1].high == revised_high
    assert new.snapshot_id != old.snapshot_id
    assert SnapshotStore(tmp_path).load(old.snapshot_id).bars[-1].high == old.bars[-1].high
    later = coarse_request.model_copy(update={"as_of": coarse_request.as_of + timedelta(seconds=1)})
    assert fetch_bundle(later, settings, provider=provider).bars[-1].high == revised_high
    assert len(provider.calls) == 2


def test_later_cutoff_refreshes_the_cached_session_tail(tmp_path):
    provider = CountingProvider(demo_bundle("AAPL", timeframe="5m"))
    settings = _settings(tmp_path)
    full = provider.bundle
    early = full.bars[-20].usable_at
    request = AnalysisRequest(symbol="AAPL", as_of=early, timeframe="5m", mode="FROZEN_DATA_REPLAY")
    first = fetch_bundle(request, settings, provider=provider)
    assert first.bars[-1].usable_at == early
    later = fetch_bundle(
        request.model_copy(update={"as_of": full.as_of}), settings, provider=provider
    )
    assert len(provider.calls) == 2
    assert provider.calls[-1][0] >= first.bars[-1].session_date - timedelta(days=7)
    assert later.bars[-1].usable_at > early


def test_cache_reuses_completed_blocks_across_trading_days(tmp_path):
    provider = CountingProvider(demo_bundle("AAPL", timeframe="5m"))
    settings = _settings(tmp_path)
    bars = provider.bundle.bars
    previous = next(bar for bar in reversed(bars) if bar.session_date < bars[-1].session_date)
    request = AnalysisRequest(symbol="AAPL", as_of=previous.usable_at, timeframe="5m")
    first = fetch_bundle(request, settings, provider=provider)
    later = fetch_bundle(
        request.model_copy(update={"as_of": provider.bundle.as_of}), settings, provider=provider
    )
    assert provider.calls[-1][0] >= previous.session_date - timedelta(days=7)
    assert [bar.bar_id for bar in first.bars] == [
        bar.bar_id for bar in later.bars if bar.usable_at <= request.as_of
    ]
    with sqlite3.connect(tmp_path / "source-cache.sqlite3") as db:
        assert db.execute("SELECT COUNT(*) FROM source_meta").fetchone()[0] == 1


def test_fetch_refuses_missing_completed_bar_before_remembering(tmp_path):
    provider = CountingProvider(demo_bundle("AAPL", timeframe="5m"))
    full = provider.bundle
    provider.bundle = full.model_copy(
        update={"bars": full.bars[:-1], "raw_bars": full.raw_bars[:-1]}
    )
    request = AnalysisRequest(symbol="AAPL", as_of=full.as_of, timeframe="5m")
    with pytest.raises(DataError, match="STALE_INTRADAY"):
        fetch_bundle(request, _settings(tmp_path), provider=provider)
    provider.bundle = full
    result = fetch_bundle(request, _settings(tmp_path), provider=provider)
    assert result.bars[-1].bar_id == full.bars[-1].bar_id
    assert len(provider.calls) == 2


def test_incomplete_same_session_refresh_keeps_existing_bars(tmp_path):
    provider = CountingProvider(demo_bundle("AAPL", timeframe="5m"))
    settings = _settings(tmp_path)
    full = provider.bundle
    target = full.bars[-1].session_date
    request = AnalysisRequest(symbol="AAPL", as_of=full.as_of, timeframe="5m")
    original = fetch_bundle(request, settings, provider=provider)
    source_key = _source_key(tmp_path)
    before = SourceBlockStore(tmp_path).revision_count(source_key, target)
    provider.bundle = full.model_copy(
        update={
            "bars": tuple(bar for bar in full.bars if bar.session_date < target) + full.bars[-2:],
            "raw_bars": tuple(bar for bar in full.raw_bars if bar.session_date < target)
            + full.raw_bars[-2:],
        }
    )
    refreshed = fetch_bundle(request, settings, provider=provider, refresh=True)
    assert refreshed.bars == original.bars
    store = SourceBlockStore(tmp_path)
    assert store.revision_count(source_key, target) == before
    assert any(row["session_date"] == target.isoformat() for row in store.failures(source_key))


def test_refresh_cannot_publish_a_new_tail_with_the_session_prefix_missing(tmp_path):
    provider = CountingProvider(demo_bundle("AAPL", timeframe="5m"))
    settings = _settings(tmp_path)
    full = provider.bundle
    request = AnalysisRequest(symbol="AAPL", as_of=full.bars[-8].usable_at, timeframe="5m")
    early = fetch_bundle(request, settings, provider=provider)
    day = early.bars[-1].session_date
    prior = max(bar.session_date for bar in early.bars if bar.session_date < day)
    provider.bundle = full.model_copy(
        update={
            field: tuple(bar for bar in getattr(full, field) if bar.session_date == prior)
            + getattr(full, field)[-2:]
            for field in ("bars", "raw_bars")
        }
    )

    later_request = request.model_copy(update={"as_of": full.as_of})
    with pytest.raises(DataError, match="STALE_INTRADAY"):
        fetch_bundle(later_request, settings, provider=provider, refresh=True)
    store = SourceBlockStore(tmp_path)
    key = _source_key(tmp_path)
    assert store.latest_blocks(key)[day].bars == tuple(
        bar for bar in early.bars if bar.session_date == day
    )
    assert any(row["reason"] == "PARTIAL_PROVIDER_COVERAGE" for row in store.failures(key))
    assert fetch_bundle(request, settings, provider=provider).bars == early.bars
    provider.bundle = full
    assert fetch_bundle(later_request, settings, provider=provider).bars == full.bars


def test_sina_sparse_auction_survives_source_cache_and_five_minute_resample(tmp_path):
    fixture = demo_bundle("600519.SH", timeframe="1m")
    day = date(2024, 6, 3)
    cutoff = datetime(2024, 6, 3, 7, tzinfo=UTC)
    calendar = load_calendar(fixture.instrument, day, day)
    bars = tuple(
        Bar(
            start_at=start,
            end_at=end,
            observed_at=cutoff,
            session_date=day,
            open=100,
            low=99,
            high=104,
            close=103 if end == cutoff else 101,
            volume=1,
        )
        for start, end in intraday_windows(calendar.session(day), 1, auction_sparse=True)
    )
    provenance = fixture.provenance.model_copy(
        update={
            "feed": "sina",
            "session_profile": SINA_AUCTION_PROFILE,
            "basis_date": day,
            "requested_start": day,
            "requested_end": day,
        }
    )
    store = SourceBlockStore(tmp_path)
    for key, original in (("full", bars), ("unknown_gap", (*bars[:-3], *bars[-2:]))):
        bundle = MarketDataBundle(
            instrument=fixture.instrument,
            as_of=cutoff,
            timeframe="1m",
            bars=original,
            raw_bars=original,
            sessions=calendar.dates,
            provenance=provenance,
            expected_bar_starts=tuple(bar.start_at for bar in bars),
        )
        store.store_bundle(
            key,
            bundle,
            source_timeframe="1m",
            session_profile=SINA_AUCTION_PROFILE,
            parser_version="fixture",
            calendar_version=calendar.version,
        )
        assembled = store.assemble(
            key,
            instrument=fixture.instrument,
            calendar=calendar,
            timeframe="1m",
            source_timeframe="1m",
            as_of=cutoff,
            start=day,
            end=day,
            buffer_minutes=0,
        )
        assert assembled is not None
        assert missing_intervals(assembled) == (() if key == "full" else (bars[-3].start_at,))
        legacy_slots = tuple(start for start, _ in intraday_windows(calendar.session(day), 1))
        legacy = assembled.model_copy(
            update={
                "expected_bar_starts": legacy_slots,
                "provenance": assembled.provenance.model_copy(
                    update={"session_profile": "cn_exchange_session"}
                ),
            }
        )
        assert missing_intervals(legacy) == missing_intervals(assembled)
        coarse = resample_bundle(assembled, "5m")
        if key == "full":
            assert len(assembled.expected_bar_starts) == 238
            assert coarse.bars[-1].end_at == cutoff
            assert coarse.bars[-1].close == 103 and coarse.bars[-1].volume == 3
        else:
            assert coarse.bars[-1].end_at == cutoff - timedelta(minutes=5)


def test_exact_cutoff_reuses_cached_bundle_with_zero_requests(tmp_path):
    provider = CountingProvider(demo_bundle("AAPL", timeframe="5m"))
    settings = _settings(tmp_path)
    full = provider.bundle
    request = AnalysisRequest(
        symbol="AAPL", as_of=full.as_of, timeframe="5m", mode="FROZEN_DATA_REPLAY"
    )
    fetch_bundle(request, settings, provider=provider)
    cutoff = full.bars[-20].usable_at
    sliced = fetch_bundle(request.model_copy(update={"as_of": cutoff}), settings, provider=provider)
    assert len(provider.calls) == 1
    assert sliced.as_of == cutoff
    assert sliced.bars[-1].usable_at <= cutoff


def test_source_lock_serializes_shared_timeframes(tmp_path):
    provider = CountingProvider(demo_bundle("AAPL", timeframe="5m"))
    provider.delay = 0.2
    settings = _settings(tmp_path)
    base = AnalysisRequest(
        symbol="AAPL", as_of=provider.bundle.as_of, timeframe="5m", mode="FROZEN_DATA_REPLAY"
    )
    errors = []

    def run(request):
        try:
            fetch_bundle(request, settings, provider=provider)
        except Exception as exc:  # pragma: no cover - diagnostic path
            errors.append(exc)

    threads = [
        threading.Thread(target=run, args=(base,)),
        threading.Thread(target=run, args=(base.model_copy(update={"timeframe": "15m"}),)),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert not errors
    assert len(provider.calls) == 1


def test_provider_failure_preserves_blocks_and_records_failure(tmp_path):
    provider = CountingProvider(demo_bundle("AAPL", timeframe="5m"))
    settings = _settings(tmp_path)
    request = AnalysisRequest(
        symbol="AAPL", as_of=provider.bundle.as_of, timeframe="5m", mode="FROZEN_DATA_REPLAY"
    )
    first = fetch_bundle(request, settings, provider=provider)
    source_key = _source_key(tmp_path)
    before = SourceBlockStore(tmp_path).revision_count(source_key)
    provider.fail_next = True
    try:
        fetch_bundle(request, settings, provider=provider, refresh=True)
    except DataError:
        pass
    else:  # pragma: no cover - the provider must fail
        raise AssertionError("expected provider failure")
    store = SourceBlockStore(tmp_path)
    assert store.revision_count(source_key) == before
    assert store.latest_blocks(source_key)[first.bars[-1].session_date].bars
    failures = store.failures(source_key)
    assert failures
    assert any("fixture provider failure" in row["reason"] for row in failures)


def test_partial_provider_coverage_keeps_prior_blocks_and_marks_gap(tmp_path):
    provider = CountingProvider(demo_bundle("AAPL", timeframe="5m"))
    settings = _settings(tmp_path)
    request = AnalysisRequest(
        symbol="AAPL", as_of=provider.bundle.as_of, timeframe="5m", mode="FROZEN_DATA_REPLAY"
    )
    first = fetch_bundle(request, settings, provider=provider)
    source_key = _source_key(tmp_path)
    provider.max_session = date(2024, 1, 4)
    result = fetch_bundle(request, settings, provider=provider, refresh=True)
    assert result.bars[-1].session_date == first.bars[-1].session_date
    failures = SourceBlockStore(tmp_path).failures(source_key)
    assert any(row["session_date"] == "2024-01-05" for row in failures)
