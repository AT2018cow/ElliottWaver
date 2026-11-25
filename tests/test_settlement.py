import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta

from typer.testing import CliRunner

from elliottwaver.brief import build_brief
from elliottwaver.cli.main import app
from elliottwaver.config import Settings
from elliottwaver.data.demo import demo_bundle
from elliottwaver.data.snapshots import SnapshotStore
from elliottwaver.forecast import ModelRegistry
from elliottwaver.schemas.brief import BriefForecast
from elliottwaver.schemas.market import AnalysisRequest
from elliottwaver.settlement import SettlementStore, SupportTestReceipt, support_matrix
from elliottwaver.util import digest, package_source_digest, support_test_inputs_digest
from elliottwaver.visualization.brief_formatter import publish_brief


def _run_and_publish(tmp_path):
    bundle = demo_bundle()
    decision = bundle.bars[-30]
    run = build_brief(
        AnalysisRequest(
            symbol="AAPL",
            as_of=decision.usable_at,
            end=decision.session_date,
        ),
        bundle,
        Settings(),
        include_weekly=False,
        check_windows=False,
    )
    path = publish_brief(run, tmp_path / "reports", formats=("md",))
    snapshot = SnapshotStore(tmp_path / ".cache/elliottwaver").save(run.source)
    return bundle, run, path, snapshot


def test_register_and_settle_is_idempotent_and_preserves_initial(tmp_path):
    bundle, run, path, _snapshot = _run_and_publish(tmp_path)
    store = SettlementStore(tmp_path / ".cache/elliottwaver")
    inserted = store.register_brief(run.brief, path)
    assert inserted >= 2
    assert store.register_brief(run.brief, path) == 0

    store.settle_brief(run.brief, path, bundle)
    rows = store.rows(brief_digest=run.brief.brief_digest)
    close_rows = [row for row in rows if row["event_id"].startswith("CLOSE_BAND_")]
    assert len(close_rows) == 2
    for row in close_rows:
        assert row["latest_revision"] == 1
        assert row["initial"]["is_initial"] == 1
        assert row["latest"]["is_initial"] == 1
        assert row["initial"]["outcome"] == row["latest"]["outcome"]

    before = {
        (row["event_id"], row["scope"]): len(row["revisions"])
        for row in store.rows(brief_digest=run.brief.brief_digest)
    }
    store.settle_brief(run.brief, path, bundle)
    after = {
        (row["event_id"], row["scope"]): len(row["revisions"])
        for row in store.rows(brief_digest=run.brief.brief_digest)
    }
    assert after == before


def test_revised_settlement_adds_revision_and_keeps_initial(tmp_path):
    bundle, run, path, _snapshot = _run_and_publish(tmp_path)
    store = SettlementStore(tmp_path / ".cache/elliottwaver")
    store.register_brief(run.brief, path)
    store.settle_brief(run.brief, path, bundle)

    decision_index = next(
        index for index, bar in enumerate(bundle.bars) if bar.session_date == run.brief.market_asof
    )
    future_index = decision_index + 5
    future = bundle.bars[future_index]
    original = next(
        item
        for item in store.rows(brief_digest=run.brief.brief_digest)
        if item["event_id"] == "CLOSE_BAND_5D_V1"
    )
    revised_close = (
        future.close * 2.0 if original["latest"]["outcome"] == "DOWN" else future.close * 0.5
    )
    revised = future.model_copy(
        update={
            "close": revised_close,
            "low": min(future.low, revised_close),
            "high": max(future.high, revised_close),
        }
    )
    raw_future = bundle.raw_bars[future_index]
    revised_raw = raw_future.model_copy(
        update={
            "close": revised_close,
            "low": min(raw_future.low, revised_close),
            "high": max(raw_future.high, revised_close),
        }
    )
    revised_bundle = bundle.model_copy(
        update={
            "bars": (*bundle.bars[:future_index], revised, *bundle.bars[future_index + 1 :]),
            "raw_bars": (
                *bundle.raw_bars[:future_index],
                revised_raw,
                *bundle.raw_bars[future_index + 1 :],
            ),
        }
    )
    revised_snapshot = SnapshotStore(tmp_path / ".cache/elliottwaver").save(revised_bundle)
    store.settle_brief(run.brief, path, revised_bundle)

    row = next(
        item
        for item in store.rows(brief_digest=run.brief.brief_digest)
        if item["event_id"] == "CLOSE_BAND_5D_V1"
    )
    assert row["latest_revision"] == 2
    assert len(row["revisions"]) == 2
    assert row["initial"]["is_initial"] == 1
    assert row["initial"]["outcome"] != row["latest"]["outcome"]
    assert row["latest"]["is_initial"] == 0
    assert row["latest"]["supersedes_revision"] == 1
    assert row["latest"]["settlement_snapshot_id"] == revised_snapshot
    assert row["initial"]["settlement_snapshot_id"] != revised_snapshot


def test_censored_then_mature_advances_only_pending(tmp_path):
    bundle, run, path, _snapshot = _run_and_publish(tmp_path)
    decision_index = next(
        index for index, bar in enumerate(bundle.bars) if bar.session_date == run.brief.market_asof
    )
    truncated = bundle.model_copy(
        update={
            "bars": bundle.bars[: decision_index + 1],
            "raw_bars": bundle.raw_bars[: decision_index + 1],
        }
    )
    store = SettlementStore(tmp_path / ".cache/elliottwaver")
    store.register_brief(run.brief, path)
    store.settle_brief(run.brief, path, truncated)
    row = next(
        item
        for item in store.rows(brief_digest=run.brief.brief_digest)
        if item["event_id"] == "CLOSE_BAND_5D_V1"
    )
    assert row["latest"]["status"] == "RIGHT_CENSORED"
    assert row["latest_revision"] == 1

    store.settle_brief(run.brief, path, bundle)
    row = next(
        item
        for item in store.rows(brief_digest=run.brief.brief_digest)
        if item["event_id"] == "CLOSE_BAND_5D_V1"
    )
    assert row["latest_revision"] == 2
    assert row["latest"]["status"] == "SETTLED"
    assert row["initial"]["status"] == "RIGHT_CENSORED"


def test_model_probabilities_are_not_rewritten_by_settlement(tmp_path):
    bundle, run, path, _snapshot = _run_and_publish(tmp_path)
    brief = run.brief.model_copy(
        update={
            "forecasts": (
                BriefForecast(
                    event_id="CLOSE_BAND_5D_V1",
                    status="APPROVED",
                    horizon_sessions=5,
                    probabilities={"UP": 0.4, "RANGE": 0.4, "DOWN": 0.2},
                    model_id="model-1",
                ),
            )
        }
    )
    identity = digest(brief.model_dump(mode="json", exclude={"brief_digest", "generated_at"}))
    brief = brief.model_copy(update={"brief_digest": identity})
    path = publish_brief(replace(run, brief=brief), tmp_path / "reports", formats=("md",))
    store = SettlementStore(tmp_path / ".cache/elliottwaver")
    store.register_brief(brief, path)
    store.settle_brief(brief, path, bundle)
    row = next(
        item
        for item in store.rows(brief_digest=brief.brief_digest)
        if item["event_id"] == "CLOSE_BAND_5D_V1"
    )
    assert row["model_id"] == "model-1"
    assert json.loads(row["probabilities"]) == {"UP": 0.4, "RANGE": 0.4, "DOWN": 0.2}


def test_support_matrix_marks_model_status_and_reasons():
    matrix = support_matrix(
        {
            "model_id": "model-1",
            "models": {
                "CLOSE_BAND_5D_V1": {
                    "status": "INSUFFICIENT_EVIDENCE",
                    "reasons": ["momentum baseline not beaten"],
                }
            },
        }
    )
    assert len(matrix["rows"]) == 46
    close = next(
        row
        for row in matrix["rows"]
        if row["asset_type"] == "EQUITY"
        and row["timeframe"] == "1d"
        and row["event_id"] == "CLOSE_BAND_5D_V1"
    )
    assert close["model_status"] == "INSUFFICIENT_EVIDENCE"
    assert close["applicable_model"] is False
    assert "momentum baseline" in close["reason"]
    assert close["tests"]

    unregistered = support_matrix()
    unknown = next(
        row
        for row in unregistered["rows"]
        if row["asset_type"] == "INDEX"
        and row["timeframe"] == "1w"
        and row["event_id"] == "CLOSE_BAND_20W_V1"
    )
    assert unknown["model_status"] == "UNCALIBRATED"
    assert unknown["reason"] == "未注册批准模型"


def test_support_matrix_uses_test_receipts_and_never_approves_a_summary(tmp_path):
    now = datetime.now(UTC)
    name = "tests/test_forecasts.py"
    receipt = SupportTestReceipt(
        command=("python", "-m", "pytest", name, "-q"),
        source_sha256=package_source_digest(),
        test_inputs_sha256=support_test_inputs_digest(),
        started_at=now - timedelta(minutes=1),
        completed_at=now,
        exit_code=0,
    )
    file = tmp_path / "test_forecasts.json"
    file.write_text(receipt.model_dump_json())
    matrix = support_matrix(
        {"models": {"TARGET_FIRST_20D_V1": {"status": "APPROVED"}}},
        test_receipts=tmp_path,
        at=now,
    )
    assert not any(row["applicable_model"] for row in matrix["rows"])
    assert any(row["research_status"] == "APPROVED" for row in matrix["rows"])
    assert matrix["rows"][0]["tests"][name] == "PASS"
    file.write_text(receipt.model_copy(update={"test_inputs_sha256": "0" * 64}).model_dump_json())
    assert support_matrix(test_receipts=tmp_path)["rows"][0]["tests"][name] == "UNVERIFIED"
    assert matrix["rows"][0]["tests"]["tests/test_evaluation.py"] == "UNVERIFIED"
    file.write_text(receipt.model_copy(update={"exit_code": 1}).model_dump_json())
    assert support_matrix(test_receipts=tmp_path)["rows"][0]["tests"][name] == "FAIL"
    file.write_text(receipt.model_copy(update={"source_sha256": "0" * 64}).model_dump_json())
    assert support_matrix(test_receipts=tmp_path)["rows"][0]["tests"][name] == "UNVERIFIED"


def test_test_inputs_digest_ignores_generated_preferences(tmp_path):
    (tmp_path / "pyproject.toml").touch()
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests/conftest.py").touch()
    (tmp_path / "scripts").mkdir()
    example = tmp_path / "config.example.yaml"
    example.write_text("default_index: SPX\n")
    baseline = support_test_inputs_digest(tmp_path)
    assert baseline is not None
    preferences = tmp_path / "config.local.yaml"
    preferences.write_text("default_index: 000688.SH\n")
    assert support_test_inputs_digest(tmp_path) == baseline
    preferences.write_text("default_index: NDX\n")
    assert support_test_inputs_digest(tmp_path) == baseline
    example.write_text("default_index: NDX\n")
    assert support_test_inputs_digest(tmp_path) != baseline


def test_support_matrix_checks_registry_against_brief_and_withdrawal(tmp_path, approved_model):
    bundle = demo_bundle()
    request = AnalysisRequest(symbol="AAPL", as_of=bundle.as_of, mode="FROZEN_DATA_REPLAY")
    model = approved_model()
    run = build_brief(request, bundle, Settings(), model=model, check_windows=False)
    assert any(item.status == "APPROVED" for item in run.brief.forecasts)
    registry = ModelRegistry(tmp_path / "registry")
    path = tmp_path / "model.json"
    path.write_text(json.dumps(model))
    when = bundle.as_of
    registry.register(path, approved_at=when)

    def applied(brief=run.brief, at=when):
        return [
            row
            for row in support_matrix(registry=registry, brief=brief, at=at)["rows"]
            if row["applicable_model"]
        ]

    assert [row["model_id"] for row in applied()] == [model["model_id"]]
    research = {
        "scope": {"symbols": ["MSFT"], "asset_types": ["EQUITY"], "timeframes": ["1d"]},
        "models": {"CLOSE_BAND_5D_V1": {"status": "APPROVED"}},
    }
    assert not any(
        row["applicable_model"] and row["symbols"] == ["MSFT"]
        for row in support_matrix(research, registry=registry, brief=run.brief, at=when)["rows"]
    )
    assert not applied(at=when - timedelta(microseconds=1))
    assert not applied(run.brief.model_copy(update={"feed": "unrelated"}))
    assert not applied(run.brief.model_copy(update={"retrospective": True}))
    withdrawn = when + timedelta(seconds=1)
    registry.withdraw(model["model_id"], withdrawn_at=withdrawn)
    assert not applied(at=withdrawn)
    assert applied()
    path.write_text("{}")
    assert not applied()


def test_settle_and_support_matrix_cli(tmp_path):
    _bundle, _run, _path, _snapshot = _run_and_publish(tmp_path)
    cache = tmp_path / ".cache/elliottwaver"
    result = CliRunner().invoke(
        app,
        [
            "settle",
            str(tmp_path / "reports"),
            "--cache-root",
            str(cache),
            "--json",
        ],
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert any(record.get("event_id") == "CLOSE_BAND_5D_V1" for record in payload["records"])

    matrix = CliRunner().invoke(app, ["support-matrix", "--json"])
    assert matrix.exit_code == 0, matrix.output
    matrix_payload = json.loads(matrix.stdout)
    assert len(matrix_payload["rows"]) == 46
