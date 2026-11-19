import json
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

import pytest
from typer.testing import CliRunner

from eliotbot.brief import build_brief
from eliotbot.cli.main import app
from eliotbot.config import Settings
from eliotbot.data.adjustment import adjust_to_basis
from eliotbot.data.demo import demo_bundle
from eliotbot.data.periods import as_weekly
from eliotbot.data.snapshots import SnapshotStore
from eliotbot.engine.indicators import calculate
from eliotbot.errors import DataError
from eliotbot.evaluation.decisions import _decision_bars
from eliotbot.evaluation.features import decision_features
from eliotbot.evaluation.outcomes import close_band, first_touch, outcome_window
from eliotbot.forecast import ModelRegistry
from eliotbot.history import HistoryStore
from eliotbot.schemas.market import AnalysisRequest, FactorPoint
from eliotbot.schemas.wave import LevelCondition
from eliotbot.service import AnalysisService, prepare_bundle
from eliotbot.settlement import SettlementStore
from eliotbot.util import digest
from eliotbot.visualization.brief_formatter import publish_brief


def _request(bundle, day=None, **kw):
    bar = day or bundle.bars[-1]
    return AnalysisRequest(
        symbol=bundle.instrument.canonical_symbol,
        as_of=bar.usable_at,
        end=bar.session_date,
        mode="FROZEN_DATA_REPLAY",
        timeframe=bundle.timeframe,
        **kw,
    )


def _without(bundle, index):
    return bundle.model_copy(
        update={
            key: (*values[:index], *values[index + 1 :])
            for key, values in (("bars", bundle.bars), ("raw_bars", bundle.raw_bars))
        }
    )


def test_brief_uses_approval_time_and_marks_forced_recalculation(tmp_path, bundle, approved_model):
    cache = tmp_path / "cache"
    config = tmp_path / "config.yml"
    config.write_text(f"cache:\n  directory: {cache}\n")
    snapshot = SnapshotStore(cache).save(bundle)
    path = tmp_path / "model.json"
    model = approved_model()
    registry = ModelRegistry(cache / "models")
    legacy = {"models": model["models"]}
    legacy["model_id"] = digest(legacy)
    path.write_text(json.dumps(legacy))
    with pytest.raises(ValueError, match="release evidence"):
        registry.register(path)
    path.write_text(json.dumps(model))
    registry.register(path, approved_at=bundle.as_of + timedelta(days=1))
    command = [
        "brief",
        "AAPL",
        "--offline",
        "--snapshot",
        snapshot,
        "--config",
        str(config),
        "--output-dir",
        str(tmp_path / "reports"),
        "--format",
        "json",
        "--json",
    ]
    runner = CliRunner()
    old = runner.invoke(app, command)
    assert old.exit_code == 0, old.output
    assert json.loads(old.stdout)["forecasts"] == []
    forced = runner.invoke(app, [*command, "--model", str(path)])
    assert forced.exit_code == 0, forced.output
    assert json.loads(forced.stdout)["retrospective"] is True


def test_decision_grid_and_close_label_keep_missing_sessions(bundle):
    index = len(bundle.bars) - 25
    before, missing, following = bundle.bars[index : index + 3]
    gappy = _without(bundle, index + 1)
    grid = {
        "start": str(before.session_date),
        "end": str(following.session_date),
        "cadence": "EVERY_SESSION",
    }
    assert _decision_bars(gappy, grid, "1d") == [
        (before.session_date, before),
        (missing.session_date, None),
        (following.session_date, following),
    ]
    friday = next(
        i for i in range(index, len(bundle.bars)) if bundle.bars[i].session_date.weekday() == 4
    )
    week_end = bundle.bars[friday].session_date
    grid = {"start": str(week_end), "end": str(week_end), "cadence": "LAST_SESSION_OF_WEEK"}
    assert _decision_bars(_without(bundle, friday), grid, "1d") == [(week_end, None)]
    window = outcome_window(gappy, before.session_date, 5)
    assert window.bars[0] is None
    actual = close_band(window, before.close, 1, 5)
    assert actual.session == bundle.bars[index + 5].session_date
    assert actual.value == bundle.bars[index + 5].close
    late_gap = _without(bundle, index + 6)
    assert (
        close_band(outcome_window(late_gap, before.session_date, 20), before.close, 1, 5) == actual
    )
    missing_due = _without(bundle, index + 5)
    assert (
        close_band(outcome_window(missing_due, before.session_date, 5), before.close, 1, 5).status
        == "INCOMPLETE_DATA"
    )

    weekly = as_weekly(bundle)
    week = weekly.bars[-9]
    window = outcome_window(_without(weekly, len(weekly.bars) - 8), week.session_date, 5)
    assert window.bars[0] is None
    assert close_band(window, week.close, 1, 5).session == weekly.bars[-4].session_date


def test_first_confirmation_can_settle_before_day_20(bundle):
    tail = len(bundle.bars) - 25
    decision = bundle.bars[tail]
    request = _request(bundle, decision)
    scenario = AnalysisService(Settings()).analyze_bundle(request, bundle).scenarios[0]
    scenario = scenario.model_copy(
        update={
            "conditions": (
                LevelCondition(
                    kind="CONFIRMATION",
                    side="HIGH",
                    op=">",
                    price=1,
                    active_from=decision.usable_at,
                    coordinate_id="test",
                    reason="test",
                ),
                LevelCondition(
                    kind="INVALIDATION",
                    side="LOW",
                    op="<=",
                    price=1,
                    active_from=decision.usable_at,
                    coordinate_id="test",
                    reason="test",
                ),
            )
        }
    )
    result = first_touch(
        scenario,
        outcome_window(
            bundle, decision.session_date, 20, outcome_as_of=bundle.bars[tail + 1].session_date
        ),
        20,
        confirmation=True,
    )
    assert (result.status, result.session) == (
        "CONFIRMED_FIRST",
        bundle.bars[tail + 1].session_date,
    )


def test_settlement_freezes_issued_prices_and_history_rejects_changes(bundle, tmp_path):
    index = len(bundle.bars) - 25
    run = build_brief(_request(bundle, bundle.bars[index]), bundle, Settings())
    path = publish_brief(run, tmp_path / "reports", formats=("md",))
    store = SettlementStore(tmp_path / "cache")
    store.register_brief(run.brief, path)
    before = store.settle_brief(run.brief, path, bundle)
    changed = tuple(
        bar.model_copy(
            update={key: getattr(bar, key) * 1.2 for key in ("open", "high", "low", "close")}
        )
        if i <= index
        else bar
        for i, bar in enumerate(bundle.bars)
    )
    after = store.settle_brief(
        run.brief, path, bundle.model_copy(update={"bars": changed, "raw_bars": changed})
    )

    def event(rows):
        return next(row for row in rows if row["event_id"] == "CLOSE_BAND_5D_V1")

    assert event(before)["latest"]["outcome"] == "UP"
    assert event(after)["latest_revision"] == 1
    history = HistoryStore(tmp_path / "cache")
    history.register(run.brief, path)
    row = history.query("AAPL")[0]
    body = json.loads((path / "brief.json").read_bytes())
    body["latest_price"] *= 100
    (path / "brief.json").write_text(json.dumps(body))
    with pytest.raises(DataError, match="checksum mismatch"):
        history.load(row)
    with pytest.raises(DataError, match="checksum mismatch"):
        store.settle_brief(run.brief, path, bundle)


def test_blind_cases_and_outcomes_use_decision_basis(tmp_path):
    from scripts.label_wave_cases import _bars_for_brief

    base = demo_bundle("600519")
    index = len(base.bars) - 12
    decision = base.bars[index]
    factors = (
        FactorPoint(effective_date=base.bars[0].session_date, factor=2),
        FactorPoint(effective_date=base.bars[index + 2].session_date, factor=4),
    )
    source = base.model_copy(
        update={
            "bars": adjust_to_basis(base.raw_bars, factors),
            "provenance": base.provenance.model_copy(update={"adjustment": "basis_date_qfq"}),
            "factors": factors,
        }
    )
    SnapshotStore(tmp_path).save(source)
    run = build_brief(_request(source, decision), source, Settings(), include_weekly=False)
    bars, valid = _bars_for_brief(run.brief.model_dump(mode="json"), tmp_path, 50)
    assert valid
    assert bars[-1]["close"] == decision.close == run.brief.latest_price
    assert bars[-1]["close"] != source.bars[index].close
    partial = outcome_window(source, decision.session_date, 20)
    assert partial.bars[1].close == base.raw_bars[index + 2].close * 2


def test_brief_format_variants_and_concurrent_publish(bundle, tmp_path):
    run = build_brief(_request(bundle), bundle, Settings())
    first = publish_brief(run, tmp_path, formats=("md",))
    with ThreadPoolExecutor(2) as pool:
        outcomes = list(
            pool.map(
                lambda _: publish_brief(run, tmp_path, formats=("html",), renderer="svg"), range(2)
            )
        )
    assert outcomes[0] == outcomes[1] != first
    assert (first / "report.md").exists()
    assert (outcomes[0] / "index.html").exists()
    history = HistoryStore(tmp_path / "cache")
    history.register(run.brief, first)
    history.register(run.brief, outcomes[0])
    assert len(history.query("AAPL")) == 1


def test_features_use_all_scenarios_regardless_of_top_k_and_anchor_details(bundle):
    request = _request(bundle, top_k=1)
    prepared = prepare_bundle(request, bundle)
    service = AnalysisService(Settings())
    one = service.analyze_with_context(request, prepared)
    many = service.analyze_with_context(request.model_copy(update={"top_k": 12}), prepared)
    assert len(one.result.scenarios) == 1 < len(many.result.scenarios)
    weekly = one.result.model_copy(update={"timeframe": "1w", "status": "OK"})
    features = decision_features(
        one.result, prepared, calculate(prepared.bars), qualified=one.qualified, weekly=weekly
    )
    assert features == decision_features(
        many.result, prepared, calculate(prepared.bars), qualified=many.qualified, weekly=weekly
    )
    assert features["candidate_count"] == len(one.qualified)
    assert features["missing_weekly"] == 0 and abs(features["weekly_bias"]) == 1
    assert features["up_weight"] + features["down_weight"] + features[
        "unknown_weight"
    ] == pytest.approx(1)
    run = build_brief(request, bundle, Settings())
    assert (
        tuple(sorted({x.date for x in run.brief.window.anchor_details}))
        == one.result.diagnostics.anchors
    )
    manual = request.model_copy(update={"anchor": run.brief.window.start})
    assert (
        build_brief(manual, bundle, Settings()).brief.window.anchor_details[0].origin == "EXPLICIT"
    )


def test_settlement_binds_probabilities_to_issuing_candidate(bundle, tmp_path):
    from dataclasses import replace

    from eliotbot.schemas.brief import BriefForecast

    run = build_brief(_request(bundle, bundle.bars[-25]), bundle, Settings())
    primary, alternate = run.brief.cards[:2]
    forecasts = tuple(
        BriefForecast(
            event_id="CONFIRM_FIRST_20D_V1",
            status="APPROVED",
            horizon_sessions=20,
            scenario_id=card.scenario_id,
            probabilities={"YES": p, "NO": 1 - p},
            model_id="test",
        )
        for card, p in ((primary, 0.25), (alternate, 0.75))
    )
    issued = run.brief.model_copy(update={"forecasts": forecasts})
    identity = digest(issued.model_dump(mode="json", exclude={"brief_digest", "generated_at"}))
    issued = issued.model_copy(update={"brief_digest": identity})
    path = publish_brief(replace(run, brief=issued), tmp_path / "reports", formats=())
    ledger = SettlementStore(tmp_path / "cache")
    ledger.register_brief(issued, path)
    rows = {
        row["scope"]: json.loads(row["probabilities"])
        for row in ledger.rows()
        if row["event_id"] == "CONFIRM_FIRST_20D_V1"
    }
    assert rows == {
        primary.scenario_id: {"YES": 0.25, "NO": 0.75},
        alternate.scenario_id: {"YES": 0.75, "NO": 0.25},
    }
