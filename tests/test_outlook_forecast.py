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
        AnalysisRequest(symbol="AAPL", as_of=bundle.as_of, mode="FROZEN_DATA_REPLAY"),
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
        AnalysisRequest(symbol="AAPL", as_of=bundle.as_of, mode="FROZEN_DATA_REPLAY"),
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
        AnalysisRequest(symbol="AAPL", as_of=bundle.as_of, mode="FROZEN_DATA_REPLAY"),
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


def test_v5_intraday_registry_enforces_exact_scope_and_serves_only_approved_slot(tmp_path):
    from datetime import UTC, date, datetime, timedelta

    from elliottwaver.evaluation.events import EVENT_SPECS
    from elliottwaver.forecast import ModelRegistry
    from elliottwaver.service import engine_rules_version

    event_id = "OUTLOOK_TARGET_FIRST_60M_5B_V1"
    settings = Settings()
    model = _model(event_id=event_id)
    model.update(
        feature_version="5",
        engine_version=engine_rules_version(settings),
        config_hash=settings.computation_hash,
        expires_at="2024-03-01T00:00:00Z",
        actual_span={"label_available_at": "2024-01-20T01:00:00Z"},
    )
    model["scope"].update(
        timeframes=["60m"],
        symbols=["SPX"],
        asset_types=["INDEX"],
        outlook_version="1.1",
    )
    model["splits"]["test"]["end"] = "2024-01-19"
    entry = model["models"][event_id]
    order = event_feature_order(event_id, "5")
    entry.update(
        feature_order=order,
        mean=[0.0] * len(order),
        scale=[1.0] * len(order),
        weights=[[0.0, 0.0] for _ in order],
        event_contract=EVENT_SPECS[event_id].horizon_contract,
        index_holdout={"SPX": {"eligible": True}},
    )
    with pytest.raises(ValueError, match="support_bins class mismatch"):
        validate_model(model, operational=True)
    entry["index_holdout"]["SPX"]["support_bins"] = {
        "NO": [{**entry["support_bins"]["NO"][0], "low": 0.2, "high": 0.4}],
        "YES": [{**entry["support_bins"]["YES"][0], "low": 0.6, "high": 0.8}],
    }
    from math import log

    entry["intercept"] = [0.0, log(7 / 3)]
    assert validate_model(model, operational=True)
    entry["event_contract"] = {**entry["event_contract"], "unit": "SESSIONS"}
    with pytest.raises(ValueError, match="wrong event horizon"):
        validate_model(model, operational=True)
    entry["event_contract"] = EVENT_SPECS[event_id].horizon_contract
    model["model_id"] = digest({k: v for k, v in model.items() if k != "model_id"})
    artifact = tmp_path / "approved-fixture.json"
    artifact.write_text(json.dumps(model))
    registry = ModelRegistry(tmp_path / "registry")
    approved = datetime(2024, 2, 10, tzinfo=UTC)
    registry.register(artifact, approved_at=approved)
    bundle = demo_bundle("SPX", timeframe="60m")
    bar = next(b for b in bundle.bars if b.session_date == date(2024, 2, 21))
    assert registry.active_event_at(approved - timedelta(seconds=1), event_id) is None
    assert registry.active_event_at(model["expires_at"], event_id) is None
    assert registry.active_models_at(model["expires_at"]) == []

    assert registry.active_event_at(bar.usable_at, event_id, "ALTERNATE_1") is None
    assert registry.active_event_at(bar.usable_at, event_id, "PRIMARY", feed="OTHER") is None
    assert registry.active_event_at(bar.usable_at, event_id, "PRIMARY", timeframe="30m") is None
    active = registry.active_event_at(bar.usable_at, event_id, "PRIMARY", timeframe="60m")
    run = build_brief(
        AnalysisRequest(
            symbol="SPX", as_of=bar.usable_at, mode="FROZEN_DATA_REPLAY", timeframe="60m"
        ),
        bundle,
        settings,
        check_windows=False,
        model=active["model"],
    )
    (forecast,) = run.brief.forecasts
    assert forecast.status == "APPROVED"
    assert forecast.outlook_slot == "PRIMARY" and forecast.horizon_unit == "BARS"
    assert forecast.probabilities == pytest.approx({"NO": 0.3, "YES": 0.7})
    outside = active["model"]
    outside["models"][event_id]["intercept"] = [0.0, log(9)]
    rerun = build_brief(
        AnalysisRequest(
            symbol="SPX", as_of=bar.usable_at, mode="FROZEN_DATA_REPLAY", timeframe="60m"
        ),
        bundle,
        settings,
        check_windows=False,
        model=outside,
    )
    (forecast,) = rerun.brief.forecasts
    assert forecast.status == "UNSUPPORTED_BIN" and forecast.probabilities is None


def test_registry_approval_waits_for_last_label_receipt(tmp_path):
    from elliottwaver.forecast import ModelRegistry

    model = _model()
    model["actual_span"] = {"label_available_at": "2024-03-02T01:00:00Z"}
    model["model_id"] = digest({k: v for k, v in model.items() if k != "model_id"})
    artifact = tmp_path / "model.json"
    artifact.write_text(json.dumps(model))
    registry = ModelRegistry(tmp_path / "registry")
    with pytest.raises(ValueError, match="last holdout label receipt"):
        registry.register(artifact, approved_at="2024-03-02T00:00:00Z")
    registry.register(artifact, approved_at="2024-03-02T01:00:00Z")
    assert registry.active_at("2024-03-02T00:59:59Z") is None
    assert registry.active_at("2024-03-02T01:00:00Z")


def test_live_brief_rechecks_expiry_when_cases_stay_the_same(monkeypatch):
    from datetime import datetime, timedelta

    import elliottwaver.brief as brief_module

    bundle = demo_bundle("AAPL")
    model = _model(event_id="OUTLOOK_TARGET_FIRST_5D_V1")
    expiry = bundle.as_of + timedelta(seconds=2)
    model["expires_at"] = expiry.isoformat()
    now = expiry - timedelta(seconds=1)
    forecasted = []

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return now.astimezone(tz)

    actual = brief_module.forecasts_for_brief

    def forecast(*args, **kwargs):
        nonlocal now
        computed = actual(*args, **kwargs)
        forecasted.append((kwargs["outlook"].cases, computed))
        now = expiry + timedelta(seconds=1)
        return computed

    monkeypatch.setattr(brief_module, "datetime", Clock)
    monkeypatch.setattr(brief_module, "forecasts_for_brief", forecast)
    live = build_brief(
        AnalysisRequest(symbol="AAPL", as_of=bundle.as_of),
        bundle,
        Settings(),
        include_weekly=False,
        check_windows=False,
        model=model,
    ).brief
    first, final = forecasted
    assert first[0] == final[0]
    assert any(item.status == "APPROVED" for item in first[1])
    assert live.outlook.issued_at == now
    assert not live.forecasts and all(not card.forecasts for card in live.cards)


def test_cli_rerenders_if_model_expires_during_receipt_publication(tmp_path, monkeypatch):
    from datetime import datetime, timedelta

    import elliottwaver.brief as brief_module
    import elliottwaver.cli.main as cli
    from elliottwaver.forecast import ModelRegistry
    from elliottwaver.history import HistoryStore
    from elliottwaver.service import engine_rules_version
    from elliottwaver.visualization import brief_formatter as formatter

    bundle = demo_bundle("AAPL")
    settings = Settings()
    model = _model(event_id="OUTLOOK_TARGET_FIRST_5D_V1")
    expiry = bundle.as_of + timedelta(seconds=2)
    now = expiry - timedelta(seconds=1)
    model.update(
        expires_at=expiry.isoformat(),
        config_hash=settings.computation_hash,
        engine_version=engine_rules_version(settings),
    )
    model["model_id"] = digest({k: v for k, v in model.items() if k != "model_id"})
    file = tmp_path / "model.json"
    file.write_text(json.dumps(model))
    registry = ModelRegistry(tmp_path / "registry")
    registry.register(file, approved_at=bundle.as_of - timedelta(days=1))
    original_write = formatter.atomic_write
    receipt_writes = []

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return now.astimezone(tz)

    def write(path, value):
        nonlocal now
        original_write(path, value)
        if path.name == formatter.ISSUANCE_FILE:
            receipt_writes.append(path)
            now = expiry

    def publish(run, *args):
        return formatter.publish_brief(run, *args, clock=lambda: now)

    cache, reports = tmp_path / "cache", tmp_path / "reports"
    config = tmp_path / "config.yaml"
    config.write_text(f"cache:\n  directory: {cache}\n")
    monkeypatch.setattr(brief_module, "datetime", Clock)
    monkeypatch.setattr(cli, "fetch_bundle", lambda *_args, **_kwargs: bundle)
    monkeypatch.setattr(cli, "parse_as_of", lambda *_args, **_kwargs: bundle.as_of)
    monkeypatch.setattr(cli, "publish_brief", publish)
    monkeypatch.setattr(formatter, "atomic_write", write)
    issued, path = cli._run_brief_core(
        "AAPL",
        config=config,
        output_dir=reports,
        format="json",
        weekly=False,
        window_check=False,
        model_registry=registry.root,
    )
    assert len(receipt_writes) == 2 and receipt_writes[0] != receipt_writes[1]
    assert not receipt_writes[0].exists()
    assert not issued.brief.forecasts and all(not card.forecasts for card in issued.brief.cards)
    assert list(reports.rglob("brief.json")) == [path / "brief.json"]
    assert formatter.read_issuance(path, issued.brief) == expiry
    assert [row["brief_digest"] for row in HistoryStore(cache).query("AAPL")] == [
        issued.brief.brief_digest,
    ]
