from datetime import timedelta

import pytest

from eliotbot.data.snapshots import SnapshotStore
from eliotbot.errors import DataError, InputError
from eliotbot.schemas.market import AnalysisRequest
from eliotbot.service import AnalysisService


def test_parquet_snapshot_roundtrip_and_analysis_parity(tmp_path, bundle):
    store = SnapshotStore(tmp_path)
    identity = store.save(bundle)
    loaded = store.load(identity)
    assert loaded == bundle
    request = AnalysisRequest(symbol="AAPL", as_of=bundle.as_of)
    assert (
        AnalysisService().analyze_bundle(request, loaded).result_digest
        == AnalysisService().analyze_bundle(request, bundle).result_digest
    )


def test_snapshot_corruption_and_path_traversal_rejected(tmp_path, bundle):
    store = SnapshotStore(tmp_path)
    identity = store.save(bundle)
    file = tmp_path / "snapshots" / identity / "metadata.json"
    file.write_text(file.read_text() + " ")
    with pytest.raises(DataError, match="checksum"):
        store.load(identity)
    with pytest.raises(DataError):
        store.load("../../etc/passwd")


def test_cache_age_is_not_refreshed_on_read(tmp_path, bundle):
    store = SnapshotStore(tmp_path)
    store.remember("fixture", bundle)
    fetched = bundle.provenance.fetched_at
    assert store.lookup("fixture", fetched + timedelta(seconds=20), 60) == bundle
    assert store.lookup("fixture", fetched + timedelta(seconds=61), 60) is None
    assert store.lookup("fixture", fetched - timedelta(seconds=1), 60) is None


def test_offline_identity_and_feed_cannot_be_substituted(bundle):
    service = AnalysisService()
    with pytest.raises(InputError, match="identity"):
        service.analyze_bundle(AnalysisRequest(symbol="MSFT", as_of=bundle.as_of), bundle)
    with pytest.raises(InputError, match="feed"):
        service.analyze_bundle(
            AnalysisRequest(symbol="AAPL", as_of=bundle.as_of, feed="iex"), bundle
        )
