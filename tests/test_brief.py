import hashlib
import json
from datetime import UTC, datetime

import pytest
from typer.testing import CliRunner

from eliotbot.brief import _cross_timeframe, build_brief
from eliotbot.cli.main import _choose_brief_timeframe, app
from eliotbot.config import Settings
from eliotbot.data.snapshots import SnapshotStore
from eliotbot.evaluation.features import (
    CANDIDATE_FEATURES,
    DECISION_FEATURES,
    PATTERNS,
    STAGES,
)
from eliotbot.forecast import ModelRegistry
from eliotbot.schemas.market import AnalysisRequest
from eliotbot.service import prepare_bundle
from eliotbot.util import digest
from eliotbot.visualization.brief_formatter import publish_brief


def test_build_brief_has_window_cards_and_digest(bundle):
    request = AnalysisRequest(symbol="AAPL", as_of=bundle.as_of)
    run = build_brief(request, bundle, Settings(), timeframe_reason="测试日线")
    brief = run.brief

    assert brief.symbol == "AAPL"
    assert brief.timeframe == "1d"
    assert brief.timeframe_reason == "测试日线"
    assert brief.brief_digest
    assert brief.window.bar_count > 0
    assert brief.window.anchors
    assert brief.cards
    assert brief.cards[0].role == "PRIMARY"
    assert all(card.structure_key for card in brief.cards)
    assert all(card.role in {"PRIMARY", "ALTERNATE"} for card in brief.cards)
    assert brief.forecast_status == "UNCALIBRATED"


def test_publish_brief_writes_manifest_and_hashes(bundle, tmp_path):
    request = AnalysisRequest(symbol="AAPL", as_of=bundle.as_of)
    run = build_brief(request, bundle, Settings(), timeframe_reason="测试日线")
    path = publish_brief(run, tmp_path, formats=("html", "md"))

    assert (path / "brief.json").exists()
    assert (path / "daily.json").exists()
    assert (path / "report.md").exists()
    assert (path / "index.html").exists()
    assert (path / "daily.html").exists()
    markdown = (path / "report.md").read_text()
    html = (path / "index.html").read_text()
    assert "规则复核" in markdown and "<summary>规则复核</summary>" in html
    assert "端点时间递增且高低交替" in markdown and "端点时间递增且高低交替" in html
    manifest = json.loads((path / "manifest.json").read_text())
    assert manifest["brief_digest"] == run.brief.brief_digest
    assert manifest["source_snapshot_id"] == run.brief.source_snapshot_id
    assert manifest["input_snapshot_id"] == run.brief.input_snapshot_id
    for name, checksum in manifest["files"].items():
        assert hashlib.sha256((path / name).read_bytes()).hexdigest() == checksum


def test_plotly_brief_regenerates_after_render_version_changes(bundle, tmp_path, monkeypatch):
    from eliotbot.visualization import brief_formatter

    request = AnalysisRequest(symbol="AAPL", as_of=bundle.as_of, mode="FROZEN_DATA_REPLAY")
    run = build_brief(request, bundle, Settings(), include_weekly=False)
    with monkeypatch.context() as old_version:
        old_version.setattr(brief_formatter, "PLOTLY_RENDER_VERSION", "plotly-v1")
        old_version.setattr(brief_formatter, "render_html", lambda *_: "<html>old chart</html>")
        old = publish_brief(run, tmp_path, formats=("html",))
    current = publish_brief(run, tmp_path, formats=("html",))
    assert old != current
    assert (old / "daily.html").read_text() == "<html>old chart</html>"
    assert request.as_of.isoformat() in (current / "daily.html").read_text()
    assert "FROZEN_DATA_REPLAY" in (current / "daily.html").read_text()
    assert (old / "brief.json").read_bytes() == (current / "brief.json").read_bytes()


def test_brief_no_candidates_is_an_explicit_issue(bundle):
    request = AnalysisRequest(symbol="AAPL", as_of=bundle.as_of)
    run = build_brief(
        request,
        bundle,
        Settings(scoring={"min_quality": 100}),
        timeframe_reason="测试日线",
    )
    assert not run.brief.cards
    assert any(issue.code == "NO_QUALIFIED_CANDIDATE" for issue in run.brief.issues)


def test_brief_cli_uses_approved_model_registry(tmp_path, monkeypatch, bundle, approved_model):
    monkeypatch.chdir(tmp_path)
    model = approved_model()
    model_path = tmp_path / "model.json"
    model_path.write_text(json.dumps(model))
    registry = ModelRegistry(tmp_path / "registry")
    registry.register(model_path, approved_at=bundle.bars[-20].usable_at)

    request = AnalysisRequest(symbol="AAPL", as_of=bundle.as_of)
    prepared = prepare_bundle(request, bundle)
    snapshot_id = SnapshotStore(tmp_path / ".cache/eliotbot").save(prepared)
    result = CliRunner().invoke(
        app,
        [
            "brief",
            "AAPL",
            "--offline",
            "--snapshot",
            snapshot_id,
            "--model-registry",
            str(tmp_path / "registry"),
            "--output-dir",
            str(tmp_path / "out"),
            "--format",
            "json",
            "--json",
        ],
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["forecast_status"] == "APPROVED"
    assert payload["forecasts"][0]["status"] == "APPROVED"


def test_brief_cli_uses_default_model_registry(tmp_path, monkeypatch, bundle, approved_model):
    monkeypatch.chdir(tmp_path)
    model = approved_model()
    model_path = tmp_path / "model.json"
    model_path.write_text(json.dumps(model))
    registry = ModelRegistry(tmp_path / ".cache/eliotbot/models")
    registry.register(model_path, approved_at=bundle.bars[-20].usable_at)

    request = AnalysisRequest(symbol="AAPL", as_of=bundle.as_of)
    prepared = prepare_bundle(request, bundle)
    snapshot_id = SnapshotStore(tmp_path / ".cache/eliotbot").save(prepared)
    result = CliRunner().invoke(
        app,
        [
            "brief",
            "AAPL",
            "--offline",
            "--snapshot",
            snapshot_id,
            "--output-dir",
            str(tmp_path / "out"),
            "--format",
            "json",
            "--json",
        ],
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["forecast_status"] == "APPROVED"


def test_brief_cli_offline_json_and_snapshot(tmp_path, monkeypatch, bundle):
    monkeypatch.chdir(tmp_path)
    request = AnalysisRequest(symbol="AAPL", as_of=bundle.as_of)
    prepared = prepare_bundle(request, bundle)
    snapshot_id = SnapshotStore(tmp_path / ".cache/eliotbot").save(prepared)

    result = CliRunner().invoke(
        app,
        [
            "brief",
            "AAPL",
            "--offline",
            "--snapshot",
            snapshot_id,
            "--output-dir",
            str(tmp_path / "out"),
            "--format",
            "json",
            "--json",
        ],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["symbol"] == "AAPL"
    assert payload["source_snapshot_id"] == snapshot_id
    assert payload["input_snapshot_id"] == snapshot_id
    briefs = list((tmp_path / "out").rglob("brief.json"))
    assert len(briefs) == 1


def test_brief_can_disable_weekly_and_window_checks(bundle):
    request = AnalysisRequest(symbol="AAPL", as_of=bundle.as_of)
    run = build_brief(
        request,
        bundle,
        Settings(),
        include_weekly=False,
        check_windows=False,
    )
    assert run.brief.weekly_state == "NOT_REQUESTED"
    assert run.brief.weekly is None
    assert run.brief.window_comparison is not None
    assert run.brief.window_comparison.state == "NOT_APPLICABLE"


def test_cross_timeframe_alignment_states(bundle):
    request = AnalysisRequest(symbol="AAPL", as_of=bundle.as_of)
    run = build_brief(request, bundle, Settings(), include_weekly=False)
    primary = run.result
    aligned = primary.model_copy(
        update={
            "scenarios": tuple(
                scenario.model_copy(update={"next_leg_bias": "UP"})
                for scenario in primary.scenarios
            )
        }
    )
    conflict = aligned.model_copy(
        update={
            "scenarios": tuple(
                scenario.model_copy(update={"next_leg_bias": "DOWN"})
                for scenario in aligned.scenarios
            )
        }
    )
    unknown = primary.model_copy(
        update={
            "scenarios": tuple(
                scenario.model_copy(update={"next_leg_bias": "UNKNOWN"})
                for scenario in primary.scenarios
            )
        }
    )
    assert _cross_timeframe(aligned, aligned).state == "ALIGNED"
    assert _cross_timeframe(aligned, conflict).state == "CONFLICT"
    assert _cross_timeframe(unknown, aligned).state == "UNDETERMINED"
    for status in ("STALE_DATA", "SEARCH_LIMITED", "UNCLEAR", "INSUFFICIENT_DATA"):
        unusable = aligned.model_copy(update={"status": status})
        assert _cross_timeframe(unusable, aligned).state == "UNDETERMINED"
        assert _cross_timeframe(conflict, unusable).state == "UNDETERMINED"


def test_brief_window_comparison_is_visible_when_available(bundle):
    request = AnalysisRequest(symbol="AAPL", as_of=bundle.as_of)
    run = build_brief(request, bundle, Settings(), include_weekly=False)
    comparison = run.brief.window_comparison
    assert comparison is not None
    assert comparison.state in {
        "SAME_COUNT",
        "CHANGED_COUNT_SAME_BIAS",
        "CHANGED_BIAS",
        "INCONCLUSIVE",
        "NOT_APPLICABLE",
    }


def test_brief_can_attach_approved_forecasts(bundle):
    close_order = DECISION_FEATURES
    candidate_order = DECISION_FEATURES + CANDIDATE_FEATURES + PATTERNS + STAGES
    model = {
        "models": {
            "CLOSE_BAND_5D_V1": {
                "status": "APPROVED",
                "feature_order": close_order,
                "classes": ["UP", "RANGE", "DOWN"],
                "mean": [0.0] * len(close_order),
                "scale": [1.0] * len(close_order),
                "weights": [[0.0] * 3 for _ in close_order],
                "intercept": [0.0, 0.0, 0.0],
                "temperature": 1.0,
            },
            "CONFIRM_FIRST_20D_V1": {
                "status": "APPROVED",
                "feature_order": candidate_order,
                "classes": ["NO", "YES"],
                "mean": [0.0] * len(candidate_order),
                "scale": [1.0] * len(candidate_order),
                "weights": [[0.0] * 2 for _ in candidate_order],
                "intercept": [0.0, 0.0],
                "temperature": 1.0,
            },
        }
    }
    model["model_id"] = digest(model)
    request = AnalysisRequest(symbol="AAPL", as_of=bundle.as_of)
    run = build_brief(
        request,
        bundle,
        Settings(),
        include_weekly=False,
        check_windows=False,
        model=model,
    )
    assert run.brief.forecast_status == "APPROVED"
    by_event = {item.event_id: item for item in run.brief.forecasts}
    assert by_event["CLOSE_BAND_5D_V1"].status == "APPROVED"
    assert sum(by_event["CLOSE_BAND_5D_V1"].probabilities.values()) == pytest.approx(1.0)
    candidates = [item for item in run.brief.forecasts if item.event_id == "CONFIRM_FIRST_20D_V1"]
    assert {item.scenario_id for item in candidates} == {
        card.scenario_id for card in run.brief.cards
    }
    for item in candidates:
        scenario = next(s for s in run.result.scenarios if s.scenario_id == item.scenario_id)
        kinds = {condition.kind for condition in scenario.conditions}
        assert item.status == (
            "APPROVED" if {"CONFIRMATION", "INVALIDATION"} <= kinds else "UNDEFINED_EVENT"
        )


def _single_event_model(support_bins):
    model = {
        "models": {
            "CLOSE_BAND_5D_V1": {
                "status": "APPROVED",
                "feature_order": ["log_return_20"],
                "classes": ["UP", "RANGE", "DOWN"],
                "mean": [0.0],
                "scale": [1.0],
                "weights": [[0.0, 0.0, 0.0]],
                "intercept": [0.0, 0.0, 0.0],
                "temperature": 1.0,
                "support_bins": support_bins,
            }
        }
    }
    model["model_id"] = digest(model)
    return model


def test_brief_marks_probabilities_outside_supported_bins(bundle):
    support = {name: [{"low": 0.4, "high": 0.6}] for name in ("UP", "RANGE", "DOWN")}
    run = build_brief(
        AnalysisRequest(symbol="AAPL", as_of=bundle.as_of),
        bundle,
        Settings(),
        include_weekly=False,
        check_windows=False,
        model=_single_event_model(support),
    )
    forecast = run.brief.forecasts[0]
    assert forecast.status == "UNSUPPORTED_BIN"
    assert forecast.probabilities is None
    assert run.brief.forecast_status == "INSUFFICIENT_EVIDENCE"


def test_brief_allows_probabilities_inside_supported_bins(bundle):
    support = {name: [{"low": 0.2, "high": 0.4}] for name in ("UP", "RANGE", "DOWN")}
    run = build_brief(
        AnalysisRequest(symbol="AAPL", as_of=bundle.as_of),
        bundle,
        Settings(),
        include_weekly=False,
        check_windows=False,
        model=_single_event_model(support),
    )
    forecast = run.brief.forecasts[0]
    assert forecast.status == "APPROVED"
    assert forecast.probabilities is not None


def test_auto_timeframe_uses_weekly_for_long_request(bundle):
    cutoff = datetime(2026, 3, 31, tzinfo=UTC)
    chosen, reason = _choose_brief_timeframe("auto", bundle, "2015-01-01", cutoff)
    assert chosen == "1w"
    assert "周线" in reason
    assert _choose_brief_timeframe("1d", bundle, "2015-01-01", cutoff)[0] == "1d"
