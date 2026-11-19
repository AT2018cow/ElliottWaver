"""Independent regressions for the second roadmap review."""

import json
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta

import numpy as np
import pytest
from typer.testing import CliRunner

from eliotbot.brief import attach_previous, build_brief
from eliotbot.cli.main import app
from eliotbot.config import Settings
from eliotbot.data.demo import demo_bundle
from eliotbot.data.periods import as_weekly
from eliotbot.data.snapshots import SnapshotStore
from eliotbot.errors import InputError
from eliotbot.evaluation.calibration import bootstrap_reliability
from eliotbot.evaluation.decisions import evaluate_protocol, merge_evaluations
from eliotbot.evaluation.outcomes import close_band, first_touch, outcome_window
from eliotbot.forecast import ModelRegistry
from eliotbot.history import HistoryStore
from eliotbot.label_models import reliability_binary, train_label_models
from eliotbot.schemas.market import AnalysisRequest, FactorPoint
from eliotbot.schemas.wave import LevelCondition
from eliotbot.settlement import SettlementStore
from eliotbot.visualization.brief_formatter import brief_html, brief_markdown, publish_brief
from scripts.label_wave_cases import blind_html, export_brief_cases, score_labels
from scripts.train_forecasts import DEFAULT_RELEASE, _annotate_reliability


def _request(bundle, index):
    bar = bundle.bars[index]
    return AnalysisRequest(
        symbol=bundle.instrument.canonical_symbol,
        timeframe=bundle.timeframe,
        as_of=bar.usable_at,
        end=bar.session_date,
        mode="FROZEN_DATA_REPLAY",
    )


def _prices(bar, factor):
    return bar.model_copy(
        update={field: getattr(bar, field) * factor for field in ("open", "high", "low", "close")}
    )


@pytest.mark.parametrize("weekly", [False, True])
def test_provider_split_uses_verified_issuance_basis(bundle, tmp_path, weekly):
    bundle = bundle.model_copy(
        update={"provenance": bundle.provenance.model_copy(update={"adjustment": "provider_all"})}
    )
    if weekly:
        bundle = demo_bundle(timeframe="1w").model_copy(
            update={
                "provenance": demo_bundle(timeframe="1w").provenance.model_copy(
                    update={"adjustment": "provider_all"}
                )
            }
        )
    request = _request(bundle, -24)
    run = build_brief(request, bundle, Settings(), include_weekly=False, check_windows=False)
    snapshot = SnapshotStore(tmp_path / "cache")
    snapshot.save(run.source)
    snapshot.save(run.prepared)
    path = publish_brief(run, tmp_path / "reports", formats=("md",))
    ledger = SettlementStore(tmp_path / "cache")
    ledger.register_brief(run.brief, path)
    ledger.settle_brief(run.brief, path, bundle)

    bars = tuple(_prices(bar, 0.5) for bar in bundle.bars)
    daily = tuple(_prices(bar, 0.5) for bar in bundle.daily_context)
    raw = tuple(
        _prices(bar, 0.5) if bar.session_date > request.end else bar for bar in bundle.raw_bars
    )
    raw_daily = tuple(
        _prices(bar, 0.5) if bar.session_date > request.end else bar
        for bar in bundle.raw_daily_context
    )
    split = bundle.model_copy(
        update={
            "bars": bars,
            "raw_bars": raw,
            "daily_context": daily,
            "raw_daily_context": raw_daily,
            "provenance": bundle.provenance.model_copy(update={"price_coordinate_id": "split"}),
        }
    )
    rows = ledger.settle_brief(run.brief, path, split)
    close_rows = [row for row in rows if row["event_id"].startswith("CLOSE_BAND")]
    assert len(close_rows) == 2
    for row in close_rows:
        assert row["latest"]["outcome"] in {"UP", "RANGE", "DOWN"}
        assert row["latest"]["outcome"] == row["initial"]["outcome"]
        assert row["latest"]["value"] == pytest.approx(row["initial"]["value"])

    if not weekly:
        recent = bundle.bars[-21:-19]
        condition = LevelCondition(
            kind="INVALIDATION",
            side="CLOSE",
            op="<",
            price=min(bar.close for bar in bundle.bars[-23:-19]) - 1,
            active_from=request.as_of,
            coordinate_id=run.brief.price_coordinate_id,
            reason="test",
        )
        previous = run.brief.model_copy(
            update={"cards": (run.brief.cards[0].model_copy(update={"conditions": (condition,)}),)}
        )
        current = build_brief(_request(split, -20), split, Settings(), include_weekly=False)
        assert all(0.5 * bar.close < condition.price for bar in recent)
        history = attach_previous(
            current,
            previous,
            previous_source=bundle,
            current_source=split,
            previous_result=run.result,
        ).brief.previous
        assert history.triggered_conditions == ()

    # Break the issuance-day raw overlap. An independent ledger must leave it pending.
    raw_key = "raw_daily_context" if weekly else "raw_bars"
    no_evidence = split.model_copy(
        update={raw_key: tuple(_prices(bar, 0.9) for bar in getattr(split, raw_key))}
    )
    other_root = tmp_path / "unverified"
    SnapshotStore(other_root).save(run.prepared)
    other_ledger = SettlementStore(other_root)
    other_ledger.register_brief(run.brief, path)
    uncertain = other_ledger.settle_brief(run.brief, path, no_evidence)
    assert all(row["status"] == "PRICE_BASIS_UNVERIFIED" for row in uncertain)
    assert all(row["latest"]["value"] is None for row in uncertain)


def test_weekly_close_comes_from_friday_even_without_monday(bundle):
    weekly_days = as_weekly(bundle).bars
    decision = weekly_days[-4].session_date
    end = weekly_days[-3].session_date
    missed = next(i for i, bar in enumerate(bundle.bars) if decision < bar.session_date < end)
    friday = next(bar for bar in bundle.bars if bar.session_date == end)
    gapped = bundle.model_copy(
        update={
            "bars": tuple(bar for i, bar in enumerate(bundle.bars) if i != missed),
            "raw_bars": tuple(bar for i, bar in enumerate(bundle.raw_bars) if i != missed),
        }
    )
    window = outcome_window(as_weekly(gapped), decision, 1)
    assert window.dates == (end,)
    assert window.bars == (None,)
    assert close_band(window, friday.close - 2, 1, 1).status == "UP"
    assert close_band(window, friday.close, 1, 1).value == friday.close

    run = build_brief(_request(bundle, -1), bundle, Settings(), include_weekly=False)
    scenario = run.result.scenarios[0].model_copy(
        update={
            "conditions": tuple(
                LevelCondition(
                    kind=kind,
                    side=side,
                    op=op,
                    price=friday.close,
                    active_from=bundle.as_of,
                    coordinate_id="test",
                    reason="test",
                )
                for kind, side, op in [("CONFIRMATION", "HIGH", ">"), ("INVALIDATION", "LOW", "<")]
            )
        }
    )
    assert first_touch(scenario, window, 1, confirmation=True).status == "INCOMPLETE_DATA"


def test_only_old_prefix_enters_revision_comparison(bundle):
    before = build_brief(_request(bundle, -25), bundle, Settings(), include_weekly=False)
    i = len(bundle.bars) - 5
    revised = _prices(bundle.bars[i], 0.9)
    future_factor = FactorPoint(effective_date=revised.session_date, factor=1.1)
    changed = bundle.model_copy(
        update={
            "bars": (*bundle.bars[:i], revised, *bundle.bars[i + 1 :]),
            "raw_bars": (*bundle.raw_bars[:i], revised, *bundle.raw_bars[i + 1 :]),
            "factors": (future_factor,),
        }
    )
    later = build_brief(_request(changed, -22), changed, Settings(), include_weekly=False)
    compared = attach_previous(
        later,
        before.brief,
        previous_source=bundle,
        current_source=changed,
        previous_result=before.result,
    ).brief.previous
    assert (compared.data_revision, compared.revision_count, compared.factor_revision_count) == (
        "NONE",
        0,
        0,
    )


def test_single_occupied_calibration_block_cannot_support_bin():
    dates = [
        (date(2025, 1, 1) + timedelta(days=d)).isoformat() for d in range(40) for _ in range(5)
    ]
    p = np.array([0.4] * 50 + [0.15] * 150)
    y = np.array([0, 1, 0, 1, 0] * 40)
    options = dict(bins=(0, 0.3, 0.6, 1), block_size=20, repetitions=50)
    regular = bootstrap_reliability(np.column_stack((1 - p, p)), y, dates, **options)
    binary = reliability_binary(p, y, dates, **options)
    for row in (
        next(r for r in regular if r["class_index"] == 1 and r["low"] == 0.3),
        next(r for r in binary if r["low"] == 0.3),
    ):
        assert row["mean_prediction"] == pytest.approx(row["observed_rate"])
        assert (row["count"], row["dates"], row["occupied_blocks"], row["difference_ci"]) == (
            50,
            10,
            1,
            None,
        )
    _annotate_reliability(regular, ["NO", "YES"], DEFAULT_RELEASE)
    assert (
        next(r for r in regular if r["class_index"] == 1 and r["low"] == 0.3)["status"]
        == "INSUFFICIENT_SUPPORT"
    )


def test_duplicate_judgments_are_rejected_before_score_and_training(tmp_path):
    labels = tmp_path / "labels.json"
    duplicated = {"case_id": "s1", "annotator_id": "one", "selected_patterns": ["IMPULSE"]}
    labels.write_text(json.dumps({"labels": [duplicated, {**duplicated, "annotator_id": " one "}]}))
    cases = tmp_path / "cases.json"
    cases.write_text(json.dumps({"cases": [{"case_id": "s1"}]}))
    with pytest.raises(ValueError, match="duplicate annotation"):
        score_labels(labels, tmp_path / "score.json")
    with pytest.raises(ValueError, match="duplicate annotation"):
        train_label_models(cases, labels, tmp_path / "model")


def _protocol(bundle, tmp_path):
    store = SnapshotStore(tmp_path / "cache")
    snapshot = store.save(bundle)
    protocol = tmp_path / "protocol.json"
    day = str(bundle.bars[-25].session_date)
    protocol.write_text(
        json.dumps(
            {
                "protocol_version": "1",
                "protocol_id": "review-smoke",
                "cache_root": str(store.root),
                "samples": [
                    {
                        "symbol": "AAPL",
                        "asset_type": "EQUITY",
                        "source_timeframe": "1d",
                        "snapshot_id": snapshot,
                    }
                ],
                "grid": {"start": day, "end": day, "cadence": "EVERY_SESSION"},
                "events": ["CLOSE_BAND_5D_V1"],
            }
        )
    )
    return protocol


def test_forced_approval_must_follow_holdout_and_bad_model_falls_back(
    bundle, tmp_path, approved_model
):
    model = tmp_path / "model.json"
    model.write_text(json.dumps(approved_model(name="older")))
    registry = ModelRegistry(tmp_path / "models")
    registry.register(model)
    later = tmp_path / "later.json"
    later.write_text(json.dumps(approved_model(name="newest")))
    registry.register(later)
    later.write_text("[]")
    assert registry.load()["model_id"] == json.loads(model.read_text())["model_id"]
    protocol = _protocol(bundle, tmp_path)
    with pytest.raises(InputError, match="precedes the completed holdout"):
        evaluate_protocol(
            protocol, tmp_path / "reports", model=model, model_approved_at="2024-03-01T23:59:00Z"
        )
    assert not (tmp_path / "reports").exists()


def test_merge_rejects_mixed_source_and_preserves_generation_hash(bundle, tmp_path):
    protocol = _protocol(bundle, tmp_path)
    first, second = tmp_path / "first", tmp_path / "second"
    evaluate_protocol(protocol, first)
    evaluate_protocol(protocol, second)
    for directory, char in ((first, "a"), (second, "b")):
        manifest = directory / "manifest.json"
        body = json.loads(manifest.read_text())
        body["source_sha256"] = char * 64
        manifest.write_text(json.dumps(body))
    with pytest.raises(InputError, match="Mismatched generating source"):
        merge_evaluations(protocol, [first, second], tmp_path / "mixed")
    merge_evaluations(protocol, [first], tmp_path / "merged")
    assert json.loads((tmp_path / "merged/manifest.json").read_text())["source_sha256"] == "a" * 64


def test_watchlist_reports_invalid_and_unavailable_without_losing_good_symbols(
    bundle,
    tmp_path,
    monkeypatch,
):
    import eliotbot.cli.main as cli

    run = build_brief(_request(bundle, -1), bundle, Settings(), include_weekly=False)
    from dataclasses import replace

    invalid = replace(run, brief=run.brief.model_copy(update={"status": "INSUFFICIENT_DATA"}))

    def fake(symbol, **kwargs):
        return (run if symbol == "AAPL" else invalid), tmp_path

    monkeypatch.setattr(cli, "_run_brief_core", fake)
    targets = tmp_path / "symbols.txt"
    targets.write_text("AAPL\nbad!\naapl\nMSFT\n")
    cmd = ["watchlist", str(targets), "--workers", "2", "--retries", "0", "--json"]
    r = CliRunner().invoke(app, cmd)
    assert r.exit_code == 2, r.output
    rows = json.loads(r.stdout)
    assert [row["symbol"] for row in rows] == ["AAPL", "bad!", "MSFT"]
    assert [row["exit_code"] for row in rows] == [0, 2, 3]
    targets.write_text("MSFT\n")
    only_missing = CliRunner().invoke(app, cmd)
    assert only_missing.exit_code == 3


def test_shared_cache_concurrent_initialization(tmp_path):
    cache = tmp_path / "fresh"

    def initialize(index):
        return (SnapshotStore(cache).root, HistoryStore(cache).path)

    with ThreadPoolExecutor(8) as workers:
        results = list(workers.map(initialize, range(16)))
    assert results == [(cache, cache / "history.sqlite3")] * 16


def test_settle_cli_deduplicates_reports_and_propagates_errors(bundle, tmp_path):
    cache = tmp_path / "cache"
    SnapshotStore(cache).save(bundle)
    run = build_brief(_request(bundle, -25), bundle, Settings(), include_weekly=False)
    reports = tmp_path / "reports"
    publish_brief(run, reports, formats=("md",))
    publish_brief(run, reports, formats=("html",), renderer="svg")
    base = ["settle", str(reports), "--cache-root", str(cache), "--json"]
    ok = CliRunner().invoke(app, base)
    assert ok.exit_code == 0, ok.output
    rows = json.loads(ok.stdout)["records"]
    assert len(rows) == 2 + 2 * len(run.brief.cards)
    assert len(rows) == len({(row["brief_digest"], row["event_id"], row["scope"]) for row in rows})

    error = CliRunner().invoke(app, [*base, "--snapshot", "f" * 64])
    assert error.exit_code == 3 and json.loads(error.stdout)["records"][0]["error"]
    missing = CliRunner().invoke(app, ["settle", str(tmp_path / "missing"), "--json"])
    assert missing.exit_code == 2


def test_blind_export_skips_tampered_and_staging_reports(bundle, tmp_path):
    from shutil import copytree

    run = build_brief(_request(bundle, -1), bundle, Settings(), include_weekly=False)
    reports = tmp_path / "reports"
    original = publish_brief(run, reports, formats=("md",))
    staging = original.parent / ".brief-not-published"
    copytree(original, staging)
    path = original / "daily.json"
    changed = json.loads(path.read_text())
    changed["latest_price"] += 20
    path.write_text(json.dumps(changed))
    exported = export_brief_cases(reports, tmp_path / "cases.json")
    assert exported["cases"] == []
    assert any("checksum mismatch" in msg for msg in exported["warnings"])
    assert any("unpublished staging" in msg for msg in exported["warnings"])


def test_blind_page_uses_neutral_endpoints_and_per_case_controls():
    example = {
        "symbol": "AAPL",
        "timeframe": "1d",
        "decision_date": "2025-01-01",
        "stratum": "all",
        "endpoints": [{"label": "HIDDEN_WAVE_ROLE", "price": 1, "kind": "LOW"}],
    }
    page = blind_html([{**example, "case_id": key} for key in ("a", "b")])
    assert page.count("name='direction-1'") == page.count("name='direction-2'") == 3
    assert "HIDDEN_WAVE_ROLE" not in page
    assert "if (!section.querySelector(\"input[name='reviewed']\").checked) return;" in page
    assert 'section.querySelector("input[data-direction]:checked")' in page


def test_human_briefs_show_frozen_cutoff_valid_forecast_and_omissions(
    bundle,
    approved_model,
    capsys,
    monkeypatch,
):
    from eliotbot.cli.console_render import render_brief

    monkeypatch.setenv("COLUMNS", "160")
    request = _request(bundle, -1).model_copy(update={"top_k": 15})
    run = build_brief(request, bundle, Settings(), include_weekly=False, model=approved_model())
    assert run.brief.omitted_top_k_count == len(run.result.scenarios) - len(run.brief.cards) > 0
    displayed = sum(card.relative_weight for card in run.brief.cards)
    assert displayed + run.brief.omitted_top_k_weight + run.brief.omitted_weight == pytest.approx(1)
    render_brief(run.brief)
    console = capsys.readouterr().out
    for representation in (brief_markdown(run.brief), brief_html(run.brief), console):
        assert "FROZEN_DATA_REPLAY" in representation
        assert run.brief.as_of.isoformat() in representation
        assert "CLOSE_BAND_5D_V1" in representation
        assert "33.3%" in representation and "尚未校准" not in representation
