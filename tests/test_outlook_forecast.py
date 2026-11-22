import json

import pytest

from elliottwaver.brief import build_brief
from elliottwaver.config import Settings
from elliottwaver.data.demo import demo_bundle
from elliottwaver.engine.indicators import calculate
from elliottwaver.evaluation.features import event_feature_order
from elliottwaver.forecast import forecasts_for_brief, validate_approval_time, validate_model
from elliottwaver.schemas.market import AnalysisRequest
from elliottwaver.schemas.outlook import BriefOutlook, OutlookCase, OutlookEvent
from elliottwaver.settlement import support_matrix
from elliottwaver.util import digest


def _model(*, slot="PRIMARY", include_scope=True, event_id="ADVANCE_FIRST_5D_V1"):
    order = event_feature_order(event_id, "4")
    classes = ["UP", "RANGE", "DOWN"] if event_id == "CLOSE_BAND_2W_V1" else ["NO", "YES"]
    scope = {
        "markets": ["US"],
        "timeframes": ["1d"],
        "asset_types": ["EQUITY"],
        "feed": ["synthetic"],
        "adjustment": ["synthetic_no_actions"],
        "symbols": ["AAPL"],
    }
    if include_scope:
        scope.update({"outlook_version": "1.0", "outlook_slot": slot})
    model = {
        "schema_version": "1.2",
        "feature_version": "4",
        "engine_version": "engine",
        "config_hash": "config",
        "frozen_candidate_id": "candidate",
        "holdout_source_sha256": "4" * 64,
        "data_status": "SEALED_FORWARD_HOLDOUT",
        "study_mode": "DECISION_TIME",
        "operational_allowed": True,
        "frozen_at": "2023-12-28T00:00:00+00:00",
        "forward_evidence": {
            "seal_id": "5" * 64,
            "receipt_sha256": "6" * 64,
            "receipt_count": 5,
            "sealed_at": "2023-12-29T00:00:00+00:00",
        },
        "scope": scope,
        "splits": {
            "train": {"start": "2023-01-01", "end": "2023-06-30"},
            "calibration": {"start": "2023-07-01", "end": "2023-12-31"},
            "test": {"start": "2024-01-01", "end": "2024-03-01"},
        },
        "protocol_sha256": "1" * 64,
        "decisions_sha256": "2" * 64,
        "events_sha256": "3" * 64,
        "release": {
            "min_test_rows": 50,
            "min_test_dates": 10,
            "min_test_symbols": 5,
            "min_bin_count": 50,
            "min_bin_dates": 10,
            "min_supported_coverage": 0.5,
            "reliability_tolerance": 0.10,
        },
        "models": {
            event_id: {
                "status": "APPROVED",
                "feature_order": order,
                "classes": classes,
                "mean": [0.0] * len(order),
                "scale": [1.0] * len(order),
                "weights": [[0.0] * len(classes) for _ in order],
                "intercept": [0.0] * len(classes),
                "temperature": 1.0,
                "reasons": [],
                "test_rows": 50,
                "test_dates": 10,
                "test_symbols": 5,
                "supported_coverage": 1.0,
                "support_bins": {
                    cls: [
                        {
                            "low": 0.0,
                            "high": 1.0,
                            "count": 50,
                            "dates": 10,
                            "occupied_blocks": 2,
                            "calibration_gap_ci": [-0.02, 0.02],
                        }
                    ]
                    for cls in classes
                },
                "improvement": {
                    name: {"brier_ci": [0.01, 0.03], "log_loss_ci": [0.01, 0.03]}
                    for name in ("frequency", "momentum", "price")
                },
            }
        },
    }
    model["model_id"] = digest(model)
    return model


def test_outlook_model_requires_scope_fields():
    model = _model(include_scope=False)
    try:
        validate_model(model, operational=True)
    except ValueError as exc:
        assert "outlook_version" in str(exc)
    else:
        raise AssertionError("missing outlook scope was accepted")
    assert validate_model(_model(), operational=True)


def _run_and_outlook():
    bundle = demo_bundle("AAPL")
    run = build_brief(
        AnalysisRequest(symbol="AAPL", as_of=bundle.as_of),
        bundle,
        Settings(),
        include_weekly=False,
        check_windows=False,
    )
    scenario = run.result.scenarios[0]
    case = OutlookCase(
        scenario_id=scenario.scenario_id,
        structure_key="fixture",
        slot="PRIMARY",
        current_leg_direction="UP",
        advance_direction="UP",
        events=(OutlookEvent(event_id="ADVANCE_FIRST_5D_V1", state="DEFINED"),),
    )
    outlook = BriefOutlook(
        horizon=5,
        horizon_unit="SESSIONS",
        periods=run.brief.outlook.periods,
        status="AVAILABLE",
        cases=(case,),
    )
    return run, outlook


def test_forecasts_for_brief_uses_outlook_slot_and_probabilities():
    run, outlook = _run_and_outlook()
    model = _model()
    forecasts = forecasts_for_brief(
        run.result,
        run.prepared,
        calculate(run.prepared.bars),
        model,
        qualified=run.result.scenarios,
        outlook=outlook,
    )
    forecast = next(item for item in forecasts if item.event_id == "ADVANCE_FIRST_5D_V1")
    assert forecast.status == "APPROVED"
    assert forecast.outlook_slot == "PRIMARY"
    assert forecast.outlook_version == "1.0"
    assert forecast.probabilities == {"NO": 0.5, "YES": 0.5}


def test_support_matrix_lists_new_outlook_events_and_scope():
    summary = {
        "model_id": "fixture",
        "scope": {
            "asset_types": ["EQUITY"],
            "timeframes": ["1d"],
            "outlook_version": "1.0",
            "outlook_slot": "PRIMARY",
        },
        "models": {"ADVANCE_FIRST_5D_V1": {"status": "APPROVED"}},
    }
    matrix = support_matrix(summary)
    row = next(
        item
        for item in matrix["rows"]
        if item["event_id"] == "ADVANCE_FIRST_5D_V1" and item["asset_type"] == "EQUITY"
    )
    assert row["outlook_version"] == "1.0"
    assert row["outlook_slot"] == "PRIMARY"


def test_build_brief_attaches_outlook_forecasts_to_cards():
    bundle = demo_bundle("AAPL")
    run = build_brief(
        AnalysisRequest(symbol="AAPL", as_of=bundle.as_of),
        bundle,
        Settings(),
        model=_model(),
        include_weekly=False,
        check_windows=False,
    )
    forecasts = [item for card in run.brief.cards for item in card.forecasts]
    assert forecasts
    assert all(item.outlook_slot for item in forecasts if item.event_id.startswith("ADVANCE_"))


def test_build_brief_shows_approved_target_probability_on_card():
    bundle = demo_bundle("AAPL")
    run = build_brief(
        AnalysisRequest(symbol="AAPL", as_of=bundle.as_of),
        bundle,
        Settings(),
        model=_model(event_id="OUTLOOK_TARGET_FIRST_5D_V1"),
        include_weekly=False,
        check_windows=False,
    )
    forecasts = [
        item
        for card in run.brief.cards
        for item in card.forecasts
        if item.event_id == "OUTLOOK_TARGET_FIRST_5D_V1"
    ]
    assert forecasts
    assert any(item.status == "APPROVED" for item in forecasts)
    assert all(item.outlook_slot == "PRIMARY" for item in forecasts)


def test_new_outlook_model_approval_requires_completed_holdout():
    model = _model()
    with pytest.raises(ValueError, match="approval precedes"):
        validate_approval_time(model, "2024-03-01T00:00:00+00:00")
    assert validate_approval_time(model, "2024-03-02T00:00:00+00:00")


def test_validate_model_rejects_multiple_new_outlook_events():
    model = _model()
    duplicate = dict(model["models"]["ADVANCE_FIRST_5D_V1"])
    model["models"]["OUTLOOK_TARGET_FIRST_5D_V1"] = duplicate
    model["model_id"] = digest(model)
    with pytest.raises(ValueError, match="exactly one event"):
        validate_model(model, operational=True)


def test_validate_model_requires_symbol_slot_for_close_event():
    model = _model(event_id="CLOSE_BAND_2W_V1", slot="SYMBOL")
    assert validate_model(model, operational=True)
    model["scope"]["outlook_slot"] = "PRIMARY"
    model["model_id"] = digest(model)
    with pytest.raises(ValueError, match="SYMBOL"):
        validate_model(model, operational=True)


def test_registry_queries_by_event_and_outlook_slot(tmp_path):
    from elliottwaver.forecast import ModelRegistry

    registry = ModelRegistry(tmp_path / "registry")
    advance = tmp_path / "advance.json"
    target = tmp_path / "target.json"
    advance.write_text(json.dumps(_model(event_id="ADVANCE_FIRST_5D_V1", slot="PRIMARY")))
    target.write_text(json.dumps(_model(event_id="OUTLOOK_TARGET_FIRST_5D_V1", slot="ALTERNATE_1")))
    registry.register(advance, approved_at="2025-01-01T00:00:00+00:00")
    registry.register(target, approved_at="2025-02-01T00:00:00+00:00")

    at = "2025-02-15T00:00:00+00:00"
    found = registry.active_event_at(at, "ADVANCE_FIRST_5D_V1", "PRIMARY")
    assert found is not None
    assert found["event_id"] == "ADVANCE_FIRST_5D_V1"
    assert found["outlook_slot"] == "PRIMARY"
    assert registry.load_event_at(at, "OUTLOOK_TARGET_FIRST_5D_V1", "ALTERNATE_1")
    assert registry.active_event_at(at, "OUTLOOK_TARGET_FIRST_5D_V1", "PRIMARY") is None
    assert registry.active_event_at(at, "UNKNOWN_EVENT", "PRIMARY") is None
