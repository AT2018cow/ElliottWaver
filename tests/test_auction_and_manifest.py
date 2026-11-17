import hashlib
import json
from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo

from typer.testing import CliRunner

from eliotbot.cli.main import app
from eliotbot.data.calendar import load_calendar
from eliotbot.data.periods import expected_starts
from eliotbot.data.router import route_symbol
from eliotbot.schemas.market import AnalysisRequest
from eliotbot.service import AnalysisService


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
    assert (
        manifest["sha256"] == hashlib.sha256((tmp_path / "replay.jsonl").read_bytes()).hexdigest()
    )
