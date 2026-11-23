import json
from dataclasses import replace

from typer.testing import CliRunner

from elliottwaver.brief import build_brief
from elliottwaver.cli.main import app
from elliottwaver.config import Settings
from elliottwaver.data.demo import demo_bundle
from elliottwaver.data.snapshots import SnapshotStore
from elliottwaver.schemas.brief import BriefForecast
from elliottwaver.schemas.market import AnalysisRequest
from elliottwaver.settlement import SettlementStore, support_matrix
from elliottwaver.util import digest
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
