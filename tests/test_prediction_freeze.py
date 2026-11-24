import hashlib
import json
from datetime import UTC, datetime, timedelta

import pytest

from elliottwaver.brief import build_brief
from elliottwaver.config import Settings
from elliottwaver.data.snapshots import SnapshotStore
from elliottwaver.evaluation.decisions import evaluate_protocol, read_evaluation_manifest
from elliottwaver.evaluation.features import (
    FEATURE_VERSION,
    OUTLOOK_FEATURES,
    SUPPORTED_FEATURE_VERSIONS,
    event_feature_order,
    outlook_features,
)
from elliottwaver.schemas.market import AnalysisRequest
from elliottwaver.schemas.outlook import OutlookCase, OutlookCondition
from elliottwaver.schemas.wave import LevelCondition, PriceZone, WaveScenario
from elliottwaver.service import prepare_bundle
from elliottwaver.util import canonical_json, digest
from elliottwaver.visualization.brief_formatter import publish_brief
from scripts import train_forecasts


def test_feature_version_four_adds_outlook_features():
    v3 = event_feature_order("ADVANCE_FIRST_5D_V1", "3")
    v4 = event_feature_order("ADVANCE_FIRST_5D_V1", "4")
    assert "4" in SUPPORTED_FEATURE_VERSIONS
    assert FEATURE_VERSION == "2"
    assert not set(OUTLOOK_FEATURES) & set(v3)
    assert v4[-len(OUTLOOK_FEATURES) :] == OUTLOOK_FEATURES
    assert "outlook_target_low_distance" in event_feature_order("OUTLOOK_TARGET_FIRST_5D_V1", "4")


def _case():
    return OutlookCase(
        scenario_id="scenario",
        structure_key="structure",
        slot="PRIMARY",
        current_leg_direction="DOWN",
        advance_direction="UP",
        target_mode="AFTER_ADVANCE",
        target_direction="UP",
        target=PriceZone(
            low=156,
            high=162,
            label="目标区1",
            methods=("fixture",),
            independent_method_count=1,
        ),
        conditions=(
            OutlookCondition(
                role="ADVANCE",
                condition=LevelCondition(
                    kind="CONFIRMATION",
                    side="HIGH",
                    op=">",
                    price=150,
                    active_from=datetime(2026, 1, 1, tzinfo=UTC),
                    reason="advance",
                    coordinate_id="fixture",
                ),
            ),
            OutlookCondition(
                role="INVALIDATION",
                condition=LevelCondition(
                    kind="INVALIDATION",
                    side="LOW",
                    op="<=",
                    price=130,
                    active_from=datetime(2026, 1, 1, tzinfo=UTC),
                    reason="stop",
                    coordinate_id="fixture",
                ),
            ),
        ),
    )


def _scenario():
    return WaveScenario.model_construct(
        scenario_id="scenario",
        subwave_status="VERIFIED",
        structure=None,
        variant="DOUBLE_THREE",
    )


def test_outlook_features_encode_geometry_and_wave_evidence():
    features = outlook_features(
        _case(),
        _scenario(),
        current=140,
        atr=2,
        wave_relations=({"daily_scenario_id": "scenario", "status": "VERIFIED"},),
        window_state="SAME_COUNT",
    )
    assert features["outlook_advance_distance"] == pytest.approx(5.0)
    assert features["outlook_stop_distance"] == pytest.approx(5.0)
    assert features["outlook_target_low_distance"] == pytest.approx(8.0)
    assert features["outlook_target_high_distance"] == pytest.approx(11.0)
    assert features["outlook_target_mode_after_advance"] == 1.0
    assert features["outlook_current_leg_down"] == 1.0
    assert features["outlook_advance_up"] == 1.0
    assert features["outlook_double_three"] == 1.0
    assert features["outlook_subwave_verified"] == 1.0
    assert features["outlook_relation_verified"] == 1.0
    assert features["outlook_window_same_count"] == 1.0


def test_freeze_writes_a_digest_identified_candidate(tmp_path, monkeypatch):
    protocol = {
        "protocol_version": "1",
        "protocol_id": "freeze-fixture",
        "cache_root": ".cache/elliottwaver",
        "samples": [
            {
                "symbol": "AAPL",
                "asset_type": "EQUITY",
                "source_timeframe": "1d",
                "snapshot_id": "a" * 64,
            }
        ],
        "grid": {
            "start": "2024-01-01",
            "end": "2024-03-31",
            "cadence": "EVERY_SESSION",
            "outcome_as_of": "2024-06-30",
        },
        "splits": {
            "train": {"start": "2023-01-01", "end": "2023-12-31"},
            "calibration": {"start": "2024-01-01", "end": "2024-02-29"},
            "test": {"start": "2024-03-01", "end": "2024-03-31"},
        },
        "events": ["ADVANCE_FIRST_5D_V1"],
        "settings": {},
        "release": {"min_test_rows": 1},
    }
    protocol_path = tmp_path / "protocol.json"
    protocol_path.write_text(json.dumps(protocol))
    model = {
        "feature_version": "4",
        "scope": {"markets": ["US"]},
        "splits": protocol["splits"],
        "engine_version": "engine",
        "config_hash": "config",
        "release": protocol["release"],
        "protocol_sha256": "protocol",
        "decisions_sha256": "decisions",
        "events_sha256": "events",
        "models": {"ADVANCE_FIRST_5D_V1": {"status": "INSUFFICIENT_EVIDENCE"}},
    }

    def fake_train(protocol_path, decisions_dir, output_dir, *, outlook_slot=None):
        output_dir.mkdir(parents=True, exist_ok=True)
        (output_dir / "model.json").write_text(json.dumps(model))
        return {"model_id": "fixture"}

    monkeypatch.setattr(train_forecasts, "train", fake_train)
    output = tmp_path / "candidate.json"
    before = datetime.now(UTC)
    candidate = train_forecasts.freeze(
        protocol_path,
        tmp_path / "decisions",
        output,
        outlook_slot="PRIMARY",
    )
    stored = json.loads(output.read_text())
    candidate_id = stored.pop("candidate_id")
    assert candidate_id == digest(stored)
    assert stored["stage"] == "FROZEN_CANDIDATE"
    assert before <= datetime.fromisoformat(stored["frozen_at"]) <= datetime.now(UTC)
    assert stored["historical_split_at"] is None
    assert stored["symbols"] == ["AAPL"]
    assert stored["scope"]["outlook_version"] == "1.0"
    assert stored["scope"]["outlook_slot"] == "PRIMARY"
    assert candidate["candidate_id"] == candidate_id


def test_forward_score_requires_a_presealed_plan_and_matching_live_receipts(
    bundle, tmp_path, monkeypatch
):
    store = SnapshotStore(tmp_path / "cache")
    market_bar = bundle.bars[-20]
    issued_at = market_bar.usable_at + timedelta(minutes=30)
    request = AnalysisRequest(symbol="AAPL", as_of=issued_at, mode="LIVE")
    issued_source = prepare_bundle(request, bundle)
    store.save(issued_source)
    run = build_brief(request, issued_source, Settings(), outlook_issued_at=issued_at)
    record = run.brief.model_copy(update={"generated_at": issued_at + timedelta(minutes=1)})
    from dataclasses import replace

    reports = tmp_path / "reports"
    publish_brief(
        replace(run, brief=record), reports, formats=(), clock=lambda: record.generated_at
    )
    decision_day = market_bar.session_date
    cutoff = bundle.bars[-1].session_date
    event = "CLOSE_BAND_5D_V1"
    protocol = {
        "protocol_version": "1",
        "data_status": "SEALED_FORWARD_HOLDOUT",
        "cache_root": str(store.root),
        "samples": [
            {
                "symbol": "AAPL",
                "asset_type": "EQUITY",
                "source_timeframe": "1d",
                "snapshot_id": store.save(bundle),
            }
        ],
        "grid": {
            "start": str(decision_day),
            "end": str(decision_day),
            "outcome_as_of": str(cutoff),
            "cadence": "EVERY_SESSION",
        },
        "events": [event],
    }
    path = tmp_path / "protocol.json"
    path.write_text(json.dumps(protocol))
    live = tmp_path / "live"
    replay = tmp_path / "replay"
    evaluate_protocol(path, live, issued_reports=reports)
    evaluate_protocol(path, replay)
    manifest = read_evaluation_manifest(live)
    development_end = decision_day - timedelta(days=8)
    candidate = {
        "schema_version": "1.1",
        "stage": "FROZEN_CANDIDATE",
        "frozen_at": (issued_at - timedelta(days=5)).isoformat(),
        "development_end": str(development_end),
        "development_source_sha256": manifest["source_sha256"],
        "feature_version": FEATURE_VERSION,
        "engine_version": manifest["engine_version"],
        "config_hash": manifest["config_hash"],
        "splits": {"test": {"start": str(development_end), "end": str(development_end)}},
        "release": {},
        "scope": {"symbols": ["AAPL"]},
        "decisions_sha256": "c" * 64,
        "events_sha256": "d" * 64,
        "models": {event: {"status": "INSUFFICIENT_EVIDENCE"}},
    }
    candidate["candidate_id"] = digest(candidate)
    model_path = tmp_path / "candidate.json"
    model_path.write_text(json.dumps(candidate))
    output = tmp_path / "forward-score"
    with pytest.raises(ValueError, match="requires a seal and verified live reports"):
        train_forecasts.score(model_path, replay, output)

    class SealClock(datetime):
        @classmethod
        def now(cls, tz=None):
            return issued_at - timedelta(days=4)

    monkeypatch.setattr(train_forecasts, "datetime", SealClock)
    seal = tmp_path / "seal.json"
    train_forecasts.seal(model_path, path, seal)
    with pytest.raises(ValueError, match="matching live report"):
        train_forecasts.score(model_path, replay, output, seal_path=seal, issued_reports=reports)
    summary = train_forecasts.score(
        model_path, live, output, seal_path=seal, issued_reports=reports
    )
    model = json.loads((output / "model.json").read_text())
    assert summary["study_mode"] == "DECISION_TIME"
    assert model["operational_allowed"] is True
    assert model["forward_evidence"]["receipt_count"] == 1
    assert model["models"][event]["status"] == "INSUFFICIENT_EVIDENCE"


def test_score_rejects_a_candidate_with_a_bad_digest(tmp_path):
    candidate = {"schema_version": "1.0", "stage": "FROZEN_CANDIDATE", "candidate_id": "bad"}
    path = tmp_path / "candidate.json"
    path.write_text(canonical_json(candidate) + "\n")
    with pytest.raises(ValueError, match="Invalid frozen candidate"):
        train_forecasts.score(path, tmp_path / "holdout", tmp_path / "output")


def test_score_rejects_a_holdout_before_the_freeze(tmp_path):
    protocol = {
        "protocol_version": "1",
        "protocol_id": "holdout",
        "feature_version": "4",
        "samples": [
            {
                "symbol": "AAPL",
                "asset_type": "EQUITY",
                "source_timeframe": "1d",
                "snapshot_id": "a" * 64,
            }
        ],
        "grid": {
            "start": "2024-01-01",
            "end": "2024-03-31",
            "cadence": "EVERY_SESSION",
            "outcome_as_of": "2024-06-30",
        },
        "splits": {},
        "events": ["ADVANCE_FIRST_5D_V1"],
        "settings": {},
        "release": {"min_test_rows": 1},
        "data_status": "HISTORICAL_HOLDOUT",
    }
    candidate = {
        "schema_version": "1.0",
        "stage": "FROZEN_CANDIDATE",
        "frozen_at": "2024-02-01T00:00:00+00:00",
        "development_end": "2023-12-31",
        "feature_version": "4",
        "engine_version": "engine",
        "config_hash": "config",
        "splits": {
            "train": {"start": "2023-01-01", "end": "2023-06-30"},
            "calibration": {"start": "2023-07-01", "end": "2023-12-31"},
            "test": {"start": "2023-12-01", "end": "2023-12-31"},
        },
        "decisions_sha256": "c" * 64,
        "events_sha256": "d" * 64,
        "models": {"ADVANCE_FIRST_5D_V1": {}},
    }
    candidate["candidate_id"] = digest(candidate)
    candidate_path = tmp_path / "candidate.json"
    candidate_path.write_text(canonical_json(candidate) + "\n")
    holdout = tmp_path / "holdout"
    holdout.mkdir()
    (holdout / "decisions.jsonl").write_text("")
    (holdout / "events.parquet").write_bytes(b"")
    (holdout / "summary.json").write_text("{}")
    files = {
        name: hashlib.sha256((holdout / name).read_bytes()).hexdigest()
        for name in ("decisions.jsonl", "events.parquet", "summary.json")
    }
    manifest = {
        "schema_version": "1.1",
        "protocol": protocol,
        "protocol_sha256": "protocol",
        "engine_version": "engine",
        "config_hash": "config",
        "feature_version": "4",
        "files": files,
    }
    (holdout / "manifest.json").write_text(json.dumps(manifest))
    with pytest.raises(ValueError, match="Holdout must start after"):
        train_forecasts.score(candidate_path, holdout, tmp_path / "output")
