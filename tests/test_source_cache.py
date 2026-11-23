import sqlite3
import threading
import time
from datetime import date, timedelta

from elliottwaver.config import Settings
from elliottwaver.data.demo import demo_bundle
from elliottwaver.data.source_cache import SourceBlockStore
from elliottwaver.data.workflow import fetch_bundle
from elliottwaver.errors import DataError
from elliottwaver.schemas.market import AnalysisRequest, MarketDataBundle


class CountingProvider:
    def __init__(self, bundle: MarketDataBundle):
        self.bundle = bundle.model_copy(
            update={
                "provenance": bundle.provenance.model_copy(
                    update={"feed": "sip", "adjustment": "provider_all"}
                )
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
    assert later.bars[-1].usable_at > early


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
