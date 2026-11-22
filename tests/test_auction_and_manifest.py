import hashlib
import json
from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo

from typer.testing import CliRunner

from elliottwaver.cli.main import app
from elliottwaver.config import Settings
from elliottwaver.data.calendar import load_calendar
from elliottwaver.data.periods import expected_starts
from elliottwaver.data.router import route_symbol
from elliottwaver.data.snapshots import SnapshotStore
from elliottwaver.schemas.market import AnalysisRequest
from elliottwaver.service import AnalysisService


def test_sina_1m_auction_excludes_only_two_noncontinuous_slots():
    instrument = route_symbol("600519")
    calendar = load_calendar(instrument, date(2026, 11, 16), date(2026, 11, 16))
    cutoff = datetime(2026, 11, 16, 16, tzinfo=ZoneInfo("Asia/Shanghai"))
    all_slots = expected_starts(calendar, instrument, cutoff, "1m", since=date(2026, 11, 16))
    sparse = expected_starts(
        calendar, instrument, cutoff, "1m", since=date(2026, 11, 16), auction_sparse=True
    )
    excluded = sorted(set(all_slots) - set(sparse))
    assert [x.astimezone(ZoneInfo("Asia/Shanghai")).strftime("%H:%M") for x in excluded] == [
        "14:57",
        "14:58",
    ]
    assert len(sparse) == 238
    assert sparse[-1] in all_slots


def test_equivalent_asof_timezones_produce_identical_results(bundle):
    service = AnalysisService()
    utc = AnalysisRequest(symbol="AAPL", as_of=bundle.as_of.astimezone(UTC))
    singapore = AnalysisRequest(
        symbol="AAPL", as_of=bundle.as_of.astimezone(ZoneInfo("Asia/Singapore"))
    )
    assert (
        service.analyze_bundle(utc, bundle).result_digest
        == service.analyze_bundle(singapore, bundle).result_digest
    )


def test_cli_replay_has_source_identity_and_content_hash(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()
    demo = runner.invoke(app, ["demo", "--format", "json", "--json"])
    assert demo.exit_code == 0, demo.output
    value = json.loads(demo.stdout)
    source = value["input_snapshot_id"]
    replay = runner.invoke(
        app, ["replay", source, "--start", value["market_asof"], "--output", "replay.jsonl"]
    )
    assert replay.exit_code == 0, replay.output
    manifest = json.loads((tmp_path / "replay.jsonl.manifest.json").read_text())
    assert manifest["source_snapshot_id"] == source
    assert manifest["reports"] == 1
    assert manifest["allow_gaps"] is False
    assert (
        manifest["sha256"] == hashlib.sha256((tmp_path / "replay.jsonl").read_bytes()).hexdigest()
    )


def test_analyze_explicit_zero_overrides_configured_subwave_depth(tmp_path, monkeypatch, bundle):
    monkeypatch.chdir(tmp_path)
    config = tmp_path / "analysis.yaml"
    config.write_text("search:\n  max_subwave_depth: 1\n")
    source = SnapshotStore(tmp_path / ".cache/elliottwaver").save(bundle)
    args = [
        "analyze",
        "AAPL",
        "--snapshot",
        source,
        "--config",
        str(config),
        "--format",
        "json",
        "--json",
    ]
    runner = CliRunner()
    configured = runner.invoke(app, args)
    disabled = runner.invoke(app, [*args, "--subwave-depth", "0"])
    assert configured.exit_code == disabled.exit_code == 0
    assert (
        json.loads(configured.stdout)["config_hash"]
        == Settings(search={"max_subwave_depth": 1}).computation_hash
    )
    assert json.loads(disabled.stdout)["config_hash"] == Settings().computation_hash


def test_allow_gaps_replay_matches_cli_analysis_and_records_policy(tmp_path, monkeypatch, bundle):
    monkeypatch.chdir(tmp_path)
    gapped = bundle.model_copy(
        update={
            "bars": bundle.bars[:210] + bundle.bars[211:],
            "raw_bars": bundle.raw_bars[:210] + bundle.raw_bars[211:],
        }
    )
    source = SnapshotStore(tmp_path / ".cache/elliottwaver").save(gapped)
    last = gapped.bars[-1]
    runner = CliRunner()
    analyzed = runner.invoke(
        app,
        [
            "analyze",
            "AAPL",
            "--snapshot",
            source,
            "--allow-gaps",
            "--as-of",
            last.usable_at.isoformat(),
            "--end",
            last.session_date.isoformat(),
            "--format",
            "json",
            "--json",
        ],
    )
    assert analyzed.exit_code == 0, analyzed.output
    args = ["replay", source, "--start", str(last.session_date), "--output", "replay.jsonl"]
    strict = runner.invoke(app, args)
    assert strict.exit_code == 0, strict.output
    assert json.loads((tmp_path / "replay.jsonl").read_text())["status"] == "INSUFFICIENT_DATA"
    allowed = runner.invoke(app, [*args, "--allow-gaps"])
    assert allowed.exit_code == 0, allowed.output
    manifest = json.loads((tmp_path / "replay.jsonl.manifest.json").read_text())
    assert manifest["allow_gaps"] is True
    row = json.loads((tmp_path / "replay.jsonl").read_text())
    assert row["result_digest"] == json.loads(analyzed.stdout)["result_digest"]
    assert any(w.startswith("ALLOWED_GAPS") for w in row["warnings"])
