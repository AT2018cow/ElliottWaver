import json
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta

import pytest

from elliottwaver.brief import build_brief
from elliottwaver.config import Settings
from elliottwaver.data.calendar import load_calendar
from elliottwaver.data.demo import demo_bundle
from elliottwaver.data.periods import intraday_windows
from elliottwaver.data.snapshots import SnapshotStore
from elliottwaver.evaluation.decisions import _write_artifacts, evaluate_protocol
from elliottwaver.evaluation.events import EVENT_SPECS
from elliottwaver.evaluation.features import (
    OUTLOOK_V5_PRICE_FEATURES,
    event_feature_order,
    intraday_outlook_features,
)
from elliottwaver.forecast import ModelRegistry, load_model
from elliottwaver.schemas.market import AnalysisRequest
from elliottwaver.schemas.outlook import OutlookCase, OutlookCondition
from elliottwaver.schemas.wave import LevelCondition
from elliottwaver.util import digest, package_source_digest
from elliottwaver.visualization.brief_formatter import publish_brief
from scripts import train_forecasts as training

EVENT_ID = "OUTLOOK_TARGET_FIRST_60M_5B_V1"
SPEC = EVENT_SPECS[EVENT_ID]


def test_v5_signed_geometry_and_price_baseline_parity():
    bundle = demo_bundle("SPX", timeframe="60m")

    def condition(role, price, op):
        return OutlookCondition(
            role=role,
            condition=LevelCondition(
                kind="CONFIRMATION",
                side="HIGH",
                op=op,
                price=price,
                reason="fixture",
                active_from=bundle.as_of,
                coordinate_id="coordinate",
            ),
        )

    case = OutlookCase(
        scenario_id="case",
        structure_key="case",
        slot="PRIMARY",
        conditions=(
            condition("ADVANCE", 104, ">"),
            condition("INVALIDATION", 96, "<"),
            condition("STAGE_EXIT", 100, "<="),
        ),
    )
    values = intraday_outlook_features(
        case,
        100,
        2,
        bundle,
        "coordinate",
        {
            "background_timeframe": "1d",
            "background_bias": "DOWN",
            "primary_bias": "UP",
            "state": "CONFLICT",
        },
    )
    assert (values["advance_signed_atr"], values["invalidation_signed_atr"]) == (2, -2)
    assert values["stage_exit_present"] == 1 and values["stage_exit_signed_atr"] == 0
    assert values["current_evidence_present"] == 0 and values["current_evidence_signed_atr"] == 0
    assert values["advance_op_>"] == values["advance_coordinate_matches"] == 1
    assert values["background_timeframe_1d"] == values["background_conflict"] == 1
    price_order = training._price_order(EVENT_ID, "5")
    assert price_order[-len(OUTLOOK_V5_PRICE_FEATURES) :] == OUTLOOK_V5_PRICE_FEATURES
    assert set(OUTLOOK_V5_PRICE_FEATURES) <= set(event_feature_order(EVENT_ID, "5"))
    assert not set(OUTLOOK_V5_PRICE_FEATURES) & set(event_feature_order(EVENT_ID, "4"))


def _development(protocol, bundle, output):
    """Build deterministic labels and feature vectors to exercise the actual fitter."""
    settings = Settings()
    bars = load_calendar(bundle.instrument, date(2024, 1, 8), date(2024, 1, 18))
    rows, events = [], []
    for window in protocol["splits"].values():
        start, stop = (date.fromisoformat(window[k]) for k in ("start", "end"))
        for session in (row for row in bars.sessions if start <= row.date <= stop):
            intervals = tuple(intraday_windows(session, 60))
            for i in (1, 2):
                slots = intervals[i : i + 5]
                ready = slots[0][0]
                due = slots[-1][1]
                rec = {
                    "decision_id": digest([ready]),
                    "symbol": "SPX",
                    "market": "US",
                    "asset_type": "INDEX",
                    "timeframe": "60m",
                    "feed": "synthetic",
                    "adjustment": "synthetic_no_actions",
                    "source_snapshot_id": bundle.snapshot_id,
                    "as_of": ready.isoformat(),
                    "decision_at": ready.isoformat(),
                    "decision_date": session.date.isoformat(),
                    "last_bar_end_at": ready.isoformat(),
                    "status": "OK",
                    "analyzable": True,
                    "features": {"log_return_20": i / 10},
                }
                rows.append(rec)
                features = {key: float(i) / 10 for key in event_feature_order(EVENT_ID, "5")}
                features.update(pattern="IMPULSE", stage="COMPLETE")
                events.append(
                    {
                        "decision_id": rec["decision_id"],
                        "symbol": "SPX",
                        "event_id": EVENT_ID,
                        "horizon": 5,
                        "horizon_unit": "BARS",
                        "slot": "PRIMARY",
                        "outlook_version": "1.1",
                        "due_date": session.date.isoformat(),
                        "due_at": due.isoformat(),
                        "label_available_at": due.isoformat(),
                        "candidate_key": "same-structure",
                        "period_starts": [start.isoformat() for start, _ in slots],
                        "period_ends": [end.isoformat() for _, end in slots],
                        "status": "TARGET_FIRST" if i == 1 else "EXPIRED",
                        "features": features,
                    }
                )
    _write_artifacts(
        protocol,
        digest(protocol),
        rows,
        events,
        output,
        settings,
        source_sha256=package_source_digest(),
    )


def test_intraday_freeze_seal_score_and_brief_keep_single_index_unapproved(tmp_path, monkeypatch):
    bundle = demo_bundle("SPX", timeframe="60m")
    actual = tuple(bar.model_copy(update={"observed_at": bar.usable_at}) for bar in bundle.bars)
    bundle = bundle.model_copy(update={"bars": actual, "raw_bars": actual})
    store = SnapshotStore(tmp_path / "cache")
    source_id = store.save(bundle)
    sample = {
        "symbol": "SPX",
        "asset_type": "INDEX",
        "source_timeframe": "60m",
        "snapshot_id": source_id,
    }
    protocol = {
        "protocol_version": "1",
        "protocol_id": "minute-fixture",
        "feature_version": "5",
        "cache_root": str(store.root),
        "samples": [sample],
        "events": [EVENT_ID],
        "grid": {
            "start": "2024-01-08",
            "end": "2024-01-19",
            "cadence": "EVERY_BAR",
            "outcome_as_of": "2024-01-19",
        },
        "splits": {
            "train": {"start": "2024-01-08", "end": "2024-01-10"},
            "calibration": {"start": "2024-01-11", "end": "2024-01-16"},
            "test": {"start": "2024-01-17", "end": "2024-01-19"},
        },
        "release": {"bootstrap_repetitions": 100, "min_test_symbols": 1},
    }
    dev = tmp_path / "dev"
    _development(protocol, bundle, dev)
    request_path = tmp_path / "protocol.json"
    request_path.write_text(json.dumps(protocol))

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2024, 2, 16, 20, tzinfo=UTC)

    monkeypatch.setattr(training, "datetime", Clock)
    candidate_path = tmp_path / "candidate.json"
    candidate = training.freeze(
        request_path,
        dev,
        candidate_path,
        outlook_slot="PRIMARY",
        event_id=EVENT_ID,
    )
    assert candidate["scope"]["outlook_version"] == "1.1"
    assert candidate["models"][EVENT_ID]["event_contract"] == SPEC.horizon_contract
    assert candidate["models"][EVENT_ID]["sample_counts"]["train"] >= 5
    assert candidate["models"][EVENT_ID]["weights"]
    assert candidate["release"]["min_test_symbols"] == 5

    grid = {
        "start": "2024-02-21",
        "end": "2024-02-21",
        "cadence": "EVERY_BAR",
        "step": 3,
        "outcome_as_of": "2024-02-23T22:00:00+00:00",
    }
    pending = {
        **protocol,
        "grid": grid,
        "protocol_id": "future-minute",
        "data_status": "AWAITING_FORWARD_SNAPSHOT",
    }
    pending.pop("splits")
    request_path.write_text(json.dumps(pending))
    sealed = tmp_path / "seal.json"
    training.seal(candidate_path, request_path, sealed)
    from elliottwaver.service import prepare_bundle

    # Provider snapshots use the request's cutoff as as_of and stamp observed_at
    # when the response is received, after that cutoff.
    outcome = prepare_bundle(
        AnalysisRequest(
            symbol="SPX",
            timeframe="60m",
            mode="FROZEN_DATA_REPLAY",
            as_of=datetime(2024, 2, 23, 20, 30, tzinfo=UTC),
        ),
        bundle,
    )
    received_at = outcome.as_of + timedelta(seconds=30)
    outcome = outcome.model_copy(
        update={
            "bars": tuple(
                bar.model_copy(update={"observed_at": received_at}) for bar in outcome.bars
            ),
            "raw_bars": tuple(
                bar.model_copy(update={"observed_at": received_at}) for bar in outcome.raw_bars
            ),
        }
    )
    request_path.write_text(
        json.dumps(
            {
                **pending,
                "data_status": "SEALED_FORWARD_HOLDOUT",
                "samples": [{**sample, "snapshot_id": store.save(outcome)}],
            }
        )
    )
    reports = tmp_path / "live"
    day = date(2024, 2, 21)
    settings = Settings()
    issued_requests = []
    for bar in [item for item in bundle.bars if item.session_date == day][::3]:
        issue = bar.usable_at + timedelta(seconds=1)
        request = AnalysisRequest(symbol="SPX", as_of=bar.usable_at, timeframe="60m", mode="LIVE")
        result = build_brief(
            request, bundle, settings, check_windows=False, outlook_issued_at=issue
        )
        store.save(result.prepared)
        publish_brief(
            replace(result, brief=result.brief.model_copy(update={"generated_at": issue})),
            reports,
            formats=(),
            clock=lambda issue=issue: issue,
        )
        issued_requests.append(request)
    forward = tmp_path / "forward"
    evaluate_protocol(request_path, forward, issued_reports=reports)
    score = tmp_path / "score"
    evaluated = training.score(
        candidate_path, forward, score, seal_path=sealed, issued_reports=reports
    )
    assert evaluated["study_mode"] == "DECISION_TIME"
    assert evaluated["models"][EVENT_ID]["status"] == "INSUFFICIENT_EVIDENCE"
    assert evaluated["models"][EVENT_ID]["sample_counts"]["test"] >= 2
    with pytest.raises(ValueError, match="no APPROVED event"):
        ModelRegistry(tmp_path / "registry").register(score / "model.json")
    studied = load_model(score / "model.json")
    assert studied["actual_span"]["label_available_at"] == received_at.isoformat()
    replay = build_brief(
        issued_requests[0],
        bundle,
        settings,
        check_windows=False,
        model=studied,
        outlook_issued_at=issued_requests[0].as_of + timedelta(seconds=1),
    )
    (forecast,) = replay.brief.forecasts
    assert forecast.event_id == EVENT_ID and forecast.horizon_unit == "BARS"
    assert forecast.outlook_slot == "PRIMARY" and forecast.probabilities is None


def test_intraday_split_uses_market_timezone_and_actual_label_receipt():
    day = date(2024, 1, 10)
    cutoff = datetime(2024, 1, 10, 21, tzinfo=UTC)
    base = {
        "decision": {"market": "US"},
        "decision_date": day,
        "due_date": day,
        "decision_at": cutoff - timedelta(hours=5),
        "due_at": cutoff,
    }
    on_time = {**base, "label_available_at": datetime(2024, 1, 11, 0, 30, tzinfo=UTC)}
    late = {**base, "label_available_at": datetime(2024, 1, 11, 5, 0, tzinfo=UTC)}
    split = training._split_rows([on_time, late], {"train": (day, day)})
    assert split["train"] == [on_time]


@pytest.mark.parametrize(
    ("cutoff", "expected"),
    (
        ("2024-02-21T23:50:00-05:00", 1),
        ("2024-02-21T22:30:00-05:00", 0),
        ("2024-02-21", 0),
        ("2024-02-22", 1),
    ),
)
def test_score_applies_exact_cutoff_before_exchange_local_date(
    tmp_path, monkeypatch, cutoff, expected
):
    event_id = "OUTLOOK_TARGET_FIRST_5M_5B_V1"
    begins = datetime(2024, 2, 22, 3, 5, tzinfo=UTC)
    slots = [begins + timedelta(minutes=5 * i) for i in range(6)]
    candidate = {
        "schema_version": "1.1",
        "frozen_at": "2024-02-20T00:00:00Z",
        "development_end": "2024-02-19",
        "splits": {"test": {"start": "2024-02-10", "end": "2024-02-19"}},
        "scope": {"markets": ["CN"], "outlook_slot": "PRIMARY"},
        "feature_version": "5",
        "engine_version": "engine",
        "config_hash": "config",
        "release": {},
        "decisions_sha256": "development-d",
        "events_sha256": "development-e",
        "models": {event_id: {"sample_counts": {"test": 7}}},
    }
    manifest = {
        "protocol": {
            "events": [event_id],
            "data_status": "HISTORICAL_HOLDOUT",
            "grid": {
                "start": "2024-02-22",
                "end": "2024-02-22",
                "outcome_as_of": cutoff,
            },
        },
        "engine_version": "engine",
        "config_hash": "config",
        "feature_version": "5",
        "protocol_sha256": "protocol",
        "files": {"decisions.jsonl": "held-d", "events.parquet": "held-e"},
    }
    decision = {
        "symbol": "000300.SH",
        "market": "CN",
        "timeframe": "5m",
        "decision_date": "2024-02-22",
        "as_of": begins.isoformat(),
        "last_bar_end_at": begins.isoformat(),
    }
    event = {
        "event_id": event_id,
        "decision_id": "row",
        "horizon": 5,
        "horizon_unit": "BARS",
        "outlook_version": "1.1",
        "slot": "PRIMARY",
        "status": "EXPIRED",
        "due_date": "2024-02-22",
        "due_at": slots[-1].isoformat(),
        "label_available_at": (slots[-1] + timedelta(seconds=2)).isoformat(),
        "period_starts": [stamp.isoformat() for stamp in slots[:-1]],
        "period_ends": [stamp.isoformat() for stamp in slots[1:]],
    }
    monkeypatch.setattr(training, "_read_candidate", lambda _: ("candidate", candidate))
    monkeypatch.setattr(training, "read_evaluation_manifest", lambda _: manifest)
    monkeypatch.setattr(training, "_load_rows", lambda _: ({"row": decision}, [event]))
    scored = training.score(tmp_path / "candidate.json", tmp_path, tmp_path / "score")
    assert scored["models"][event_id]["sample_counts"]["test"] == expected


def test_index_holdout_preserves_per_class_calibration_bins(monkeypatch):
    import numpy as np

    support = {"YES": [{"low": 0.6, "high": 0.8}], "NO": [{"low": 0.2, "high": 0.4}]}
    evidence = {
        "status": "APPROVED",
        "test_rows": 1,
        "test_dates": 1,
        "reasons": [],
        "improvement": {},
        "supported_coverage": 1.0,
        "support_bins": support,
    }
    monkeypatch.setattr(training, "forecast_evidence", lambda *_: evidence)
    decisions = [{"symbol": "SPX", "decision_date": date(2024, 2, 22), "y": 1}]
    local = training._index_holdout(
        EVENT_ID,
        decisions,
        ["NO", "YES"],
        np.array([[0.3, 0.7]]),
        {},
        {"min_test_rows": 1, "min_test_dates": 1},
        "EVERY_BAR",
    )
    assert local["SPX"]["eligible"] and local["SPX"]["support_bins"] == support
