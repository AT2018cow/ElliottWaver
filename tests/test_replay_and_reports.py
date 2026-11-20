import hashlib
import json
import re
from datetime import timedelta

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from typer.testing import CliRunner

from eliotbot.cli.main import app
from eliotbot.data.router import route_symbol
from eliotbot.engine.indicators import calculate
from eliotbot.engine.zigzag import extract
from eliotbot.errors import DataError, ExportError
from eliotbot.evaluation.events import evaluate_event
from eliotbot.evaluation.replay import replay
from eliotbot.schemas.market import AnalysisRequest, Bar
from eliotbot.schemas.wave import LevelCondition, PriceZone
from eliotbot.service import AnalysisService, prepare_bundle
from eliotbot.util import digest
from eliotbot.visualization.chart_builder import build_plotly
from eliotbot.visualization.report_formatter import markdown_report, publish_report


def test_replay_matches_individual_prefix_analysis(bundle):
    service = AnalysisService()
    for result in replay(bundle, service, start=bundle.bars[-5].session_date):
        direct = service.analyze_bundle(
            AnalysisRequest(symbol="AAPL", as_of=result.as_of, end=result.market_asof), bundle
        )
        assert result.result_digest == direct.result_digest
        assert all(
            w.pivot.confirmed_at <= result.as_of
            for s in result.scenarios
            for w in s.completed_waves
        )


@given(multiplier=st.floats(min_value=0.2, max_value=20, allow_nan=False, allow_infinity=False))
@settings(max_examples=12, deadline=None)
def test_positive_price_scaling_preserves_causal_pivots(multiplier):
    from eliotbot.data.demo import demo_bundle

    bundle = demo_bundle()
    changed = tuple(
        Bar(
            **{
                **b.model_dump(),
                **{key: getattr(b, key) * multiplier for key in ("open", "high", "low", "close")},
            }
        )
        for b in bundle.bars
    )
    original = extract(bundle.bars, calculate(bundle.bars).atr, 2.4)
    scaled = extract(changed, calculate(changed).atr, 2.4, tick_size=0.01 * multiplier)
    assert [(p.index, p.kind) for p in original.confirmed] == [
        (p.index, p.kind) for p in scaled.confirmed
    ]


def test_later_asof_cannot_disguise_a_stale_snapshot(bundle):
    request = AnalysisRequest(symbol="AAPL", as_of=bundle.as_of + timedelta(days=20))
    with pytest.raises(DataError, match="STALE_DATA"):
        AnalysisService().analyze_bundle(request, bundle)
    result = AnalysisService().analyze_bundle(
        request.model_copy(update={"allow_stale": True}), bundle
    )
    assert result.status == "STALE_DATA"


def test_unknown_internal_gap_blocks_strict_analysis(bundle):
    values = bundle.model_dump()
    values["bars"] = bundle.bars[:210] + bundle.bars[211:]
    values["raw_bars"] = bundle.raw_bars[:210] + bundle.raw_bars[211:]
    changed = type(bundle)(**values)
    result = AnalysisService().analyze_bundle(
        AnalysisRequest(symbol="AAPL", as_of=changed.as_of), changed
    )
    assert result.status == "INSUFFICIENT_DATA" and not result.scenarios


def test_report_plot_and_json_share_the_same_canonical_levels(tmp_path, bundle):
    request = AnalysisRequest(symbol="AAPL", as_of=bundle.as_of)
    bundle = prepare_bundle(request, bundle)
    result = AnalysisService().analyze_bundle(request, bundle)
    figure = build_plotly(result, bundle)
    expected = result.scenarios[0].completed_waves
    assert list(figure.data[5].y) == [w.pivot.price for w in expected]
    path = publish_report(result, bundle, tmp_path, formats=("html", "md"))
    manifest = json.loads((path / "manifest.json").read_text())
    assert "plotly.js" in (path / "chart.html").read_text()
    for filename, checksum in manifest["files"].items():
        assert hashlib.sha256((path / filename).read_bytes()).hexdigest() == checksum
    assert json.loads((path / "analysis.json").read_text())["result_digest"] == result.result_digest


def test_markdown_report_flags_weight_denominator_when_structures_are_excluded(bundle):
    request = AnalysisRequest(symbol="AAPL", as_of=bundle.as_of)
    bundle = prepare_bundle(request, bundle)
    result = AnalysisService().analyze_bundle(request, bundle)
    flagged = result.model_copy(
        update={"warnings": result.warnings + ("EXCLUDED_STRUCTURE: fixture",)}
    )
    text = markdown_report(flagged)
    assert "候选权重仅在通过质量/覆盖门槛的候选之间归一化" in text


@pytest.mark.parametrize(
    "target_indices, expected",
    [
        ((2, 4), "S2、S4"),
        ((2, 3), "S2、S3"),
        ((3,), "S3"),
        ((), None),
    ],
)
def test_target_hint_names_only_candidates_that_have_targets(bundle, target_indices, expected):
    result = AnalysisService().analyze_bundle(
        AnalysisRequest(symbol="AAPL", as_of=bundle.as_of), bundle
    )
    example = next(s for s in result.scenarios if s.target_zones)
    scenarios = tuple(
        example.model_copy(
            update={
                "scenario_id": f"hint-fixture-{index}",
                "target_zones": example.target_zones if index in target_indices else (),
                "projected_path": example.projected_path if index in target_indices else (),
                "relative_weight": 0.25,
            }
        )
        for index in range(1, 5)
    )
    flagged = result.model_copy(
        update={
            "scenarios": scenarios,
            "omitted_weight": 0,
            "warnings": result.warnings + ("EXCLUDED_STRUCTURE: hint fixture",),
        }
    )
    hints = [
        line for line in markdown_report(flagged).splitlines() if "S1 当前无合法目标；" in line
    ]
    if expected is None:
        assert not hints
    else:
        assert hints == [f"S1 当前无合法目标；{expected} 提供条件性目标，可在图表中切换。"]


def test_engine_version_is_bound_into_result_identity(bundle):
    result = AnalysisService().analyze_bundle(
        AnalysisRequest(symbol="AAPL", as_of=bundle.as_of), bundle
    )
    assert result.engine_version.endswith("/ew_score_v2")
    payload = result.model_dump(
        mode="json", exclude={"run_id", "result_digest", "analyzed_at", "mode"}
    )
    assert digest(payload) == result.result_digest
    payload["engine_version"] += "/different-version"
    assert digest(payload) != result.result_digest


def test_markdown_report_shows_selected_scoring_profile(bundle):
    from eliotbot.config import Settings

    request = AnalysisRequest(symbol="AAPL", as_of=bundle.as_of)
    bundle = prepare_bundle(request, bundle)
    result = AnalysisService(Settings(scoring={"profile": "ew_score_v1"})).analyze_bundle(
        request, bundle
    )
    assert result.engine_version.endswith("/ew_score_v1")
    text = markdown_report(result)
    assert f"- 引擎：`{result.engine_version}`。" in text


def test_markdown_report_labels_index_instrument(bundle):
    result = AnalysisService().analyze_bundle(
        AnalysisRequest(symbol="AAPL", as_of=bundle.as_of), bundle
    )
    index_result = result.model_copy(update={"instrument": route_symbol("000688.SH")})
    assert "品种：指数；指数无复权，价格为原始点位。" in markdown_report(index_result)


def test_export_failure_does_not_publish_or_update_latest(tmp_path, bundle, monkeypatch):
    request = AnalysisRequest(symbol="AAPL", as_of=bundle.as_of)
    bundle = prepare_bundle(request, bundle)
    result = AnalysisService().analyze_bundle(request, bundle)

    def failed(*args, **kwargs):
        raise RuntimeError("renderer failed")

    monkeypatch.setattr("eliotbot.visualization.report_formatter.render_html", failed)
    with pytest.raises(ExportError):
        publish_report(result, bundle, tmp_path)
    assert not list(tmp_path.rglob("latest*.json"))
    assert not list(tmp_path.rglob("analysis.json"))


def test_plotly_report_separates_version_and_mode_from_result_identity(
    bundle, tmp_path, monkeypatch
):
    from eliotbot.visualization import report_formatter

    request = AnalysisRequest(symbol="AAPL", as_of=bundle.as_of)
    prepared = prepare_bundle(request, bundle)
    live = AnalysisService().analyze_prepared(request, prepared)
    replay = live.model_copy(update={"mode": "FROZEN_DATA_REPLAY"})
    with monkeypatch.context() as old_version:
        old_version.setattr(report_formatter, "PLOTLY_RENDER_VERSION", "plotly-v1")
        old_version.setattr(report_formatter, "render_html", lambda *_: "<html>old chart</html>")
        old = publish_report(replay, prepared, tmp_path, formats=("html",))
    live_path = publish_report(live, prepared, tmp_path, formats=("html",))
    replay_path = publish_report(replay, prepared, tmp_path, formats=("html",))
    assert len({old, live_path, replay_path}) == 3
    assert (old / "chart.html").read_text() == "<html>old chart</html>"
    for path, result in [(live_path, live), (replay_path, replay)]:
        metadata = json.loads((path / "analysis.json").read_text())
        assert metadata["result_digest"] == live.result_digest
        assert metadata["mode"] == result.mode
        chart = (path / "chart.html").read_text()
        subtitle = re.search(r'"subtitle"\s*:\s*\{\s*"text"\s*:\s*("(?:[^"\\]|\\.)*")', chart)
        assert subtitle is not None
        assert (
            json.loads(subtitle[1])
            == f"数据模式 {result.mode} · 决策截止 {result.as_of.isoformat()}"
        )


def test_event_same_bar_collision_and_right_censoring(bundle):
    request = AnalysisRequest(symbol="AAPL", as_of=bundle.as_of)
    result = AnalysisService().analyze_bundle(request, bundle)
    scenario = result.scenarios[0]
    bar = bundle.bars[-1]
    scenario = scenario.model_copy(
        update={
            "conditions": (
                LevelCondition(
                    kind="INVALIDATION",
                    side="LOW",
                    op="<=",
                    price=bar.close + 1,
                    active_from=bar.start_at,
                    reason="fixture",
                    coordinate_id="test",
                ),
            ),
            "target_zones": (
                PriceZone(
                    low=bar.low,
                    high=bar.high,
                    label="fixture",
                    methods=("fixture",),
                    independent_method_count=1,
                ),
            ),
        }
    )
    outcome = evaluate_event(scenario, bundle, bundle.bars[-2].session_date)
    assert outcome.status == "AMBIGUOUS_SAME_BAR"
    assert evaluate_event(scenario, bundle, bundle.bars[-1].session_date).status == "RIGHT_CENSORED"


def test_cli_demo_and_offline_json_parity(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()
    demo = runner.invoke(app, ["demo", "--format", "json", "--json"])
    assert demo.exit_code == 0, demo.output
    first = json.loads(demo.stdout)
    second = runner.invoke(
        app,
        [
            "analyze",
            "AAPL",
            "--offline",
            "--snapshot",
            first["input_snapshot_id"],
            "--format",
            "json",
            "--json",
        ],
    )
    assert second.exit_code == 0, second.output
    assert json.loads(second.stdout)["result_digest"] == first["result_digest"]
    bad = runner.invoke(app, ["analyze", "AAPL", "--offline"])
    assert bad.exit_code == 2
