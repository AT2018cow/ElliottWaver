import json
import sqlite3
import threading
import time
from collections import Counter
from datetime import date

from typer.testing import CliRunner

from eliotbot.brief import BriefRun, attach_previous, build_brief
from eliotbot.cli.main import app
from eliotbot.config import Settings
from eliotbot.data.snapshots import SnapshotStore
from eliotbot.errors import SourceError
from eliotbot.history import HistoryStore, base_lineage_key, lineage_key
from eliotbot.schemas.market import AnalysisRequest, FactorPoint
from eliotbot.service import prepare_bundle
from eliotbot.util import digest
from eliotbot.visualization.brief_formatter import publish_brief


def _run(bundle):
    return build_brief(
        AnalysisRequest(symbol="AAPL", as_of=bundle.as_of),
        bundle,
        Settings(),
        include_weekly=False,
        check_windows=False,
    )


def test_history_register_query_and_idempotence(bundle, tmp_path):
    run = _run(bundle)
    path = publish_brief(run, tmp_path / "reports", formats=("md",))
    store = HistoryStore(tmp_path / ".cache/eliotbot")
    store.register(run.brief, path)
    store.register(run.brief, path)

    rows = store.query("AAPL")
    assert len(rows) == 1
    assert rows[0]["brief_digest"] == run.brief.brief_digest
    assert rows[0]["lineage_key"] == lineage_key(run.brief)
    assert rows[0]["base_lineage_key"] == base_lineage_key(run.brief)
    assert rows[0]["market_asof"] == str(run.brief.market_asof)
    assert store.load(rows[0]).brief_digest == run.brief.brief_digest


def test_history_migrates_old_base_lineage(bundle, tmp_path):
    run = _run(bundle)
    path = publish_brief(run, tmp_path / "reports", formats=("md",))
    root = tmp_path / ".cache/eliotbot"
    root.mkdir(parents=True)
    with sqlite3.connect(root / "history.sqlite3") as db:
        db.execute(
            """
            CREATE TABLE briefs (
                brief_digest TEXT PRIMARY KEY,
                lineage_key TEXT NOT NULL,
                symbol TEXT NOT NULL,
                market_asof TEXT NOT NULL,
                as_of TEXT NOT NULL,
                observed_at TEXT NOT NULL,
                source_snapshot_id TEXT NOT NULL,
                report_path TEXT NOT NULL,
                mode TEXT NOT NULL
            )
            """
        )
        db.execute(
            "INSERT INTO briefs VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                run.brief.brief_digest,
                lineage_key(run.brief),
                run.brief.symbol,
                run.brief.market_asof.isoformat(),
                run.brief.as_of.isoformat(),
                run.brief.generated_at.isoformat(),
                run.brief.source_snapshot_id,
                str(path),
                run.brief.mode,
            ),
        )

    store = HistoryStore(root)
    rows = store.query("AAPL")
    assert len(rows) == 1
    assert rows[0]["base_lineage_key"] == base_lineage_key(run.brief)
    with sqlite3.connect(root / "history.sqlite3") as db:
        version = db.execute(
            "SELECT value FROM history_meta WHERE key = 'base_lineage_version'"
        ).fetchone()
    assert version[0] == "2"


def test_attach_previous_marks_first_and_same_count(bundle, tmp_path):
    run = _run(bundle)
    path = publish_brief(run, tmp_path / "reports", formats=("md",))
    first = attach_previous(run, None)
    assert first.brief.previous is not None
    assert first.brief.previous.change == "FIRST_RECORD"

    same = attach_previous(
        run,
        run.brief,
        str(path),
        previous_source=run.source,
        current_source=run.source,
    )
    assert same.brief.previous is not None
    assert same.brief.previous.change == "SAME_COUNT"
    assert same.brief.previous.data_revision == "NONE"
    assert same.brief.previous.previous_digest == run.brief.brief_digest
    assert same.brief.brief_digest != run.brief.brief_digest


def test_attach_previous_detects_ohlc_revision(bundle):
    run = _run(bundle)
    index = 10
    changed = bundle.raw_bars[index].model_copy(
        update={
            "close": bundle.raw_bars[index].close + 1,
            "high": bundle.raw_bars[index].high + 1,
        }
    )
    previous_source = bundle.model_copy(
        update={"raw_bars": (*bundle.raw_bars[:index], changed, *bundle.raw_bars[index + 1 :])}
    )
    previous = attach_previous(
        run,
        run.brief,
        previous_source=previous_source,
        current_source=bundle,
    )
    assert previous.brief.previous is not None
    assert previous.brief.previous.change == "DATA_REVISED"
    assert previous.brief.previous.data_revision == "REVISED"
    assert previous.brief.previous.revision_count == 1
    assert previous.brief.previous.structure_change == "SAME_COUNT"


def test_attach_previous_detects_factor_revision(bundle):
    run = _run(bundle)
    effective = date(2025, 1, 2)
    previous_source = bundle.model_copy(
        update={"factors": (FactorPoint(effective_date=effective, factor=1.0),)}
    )
    current_source = bundle.model_copy(
        update={"factors": (FactorPoint(effective_date=effective, factor=2.0),)}
    )
    previous = attach_previous(
        run,
        run.brief,
        previous_source=previous_source,
        current_source=current_source,
    )
    assert previous.brief.previous is not None
    assert previous.brief.previous.change == "DATA_REVISED"
    assert previous.brief.previous.factor_revision_count == 1


def test_attach_previous_new_bars_are_not_revisions(bundle):
    run = _run(bundle)
    previous_source = bundle.model_copy(
        update={"raw_bars": bundle.raw_bars[:-1], "bars": bundle.bars[:-1]}
    )
    previous = attach_previous(
        run,
        run.brief,
        previous_source=previous_source,
        current_source=bundle,
    )
    assert previous.brief.previous is not None
    assert previous.brief.previous.change == "SAME_COUNT"
    assert previous.brief.previous.data_revision == "NONE"


def test_attach_previous_detects_config_hash_change(bundle):
    run = _run(bundle)
    previous_brief = run.brief.model_copy(update={"config_hash": "previous-config"})
    previous = attach_previous(
        run,
        previous_brief,
        previous_source=run.source,
        current_source=run.source,
    )
    assert previous.brief.previous is not None
    assert previous.brief.previous.change == "CONFIG_CHANGED"
    assert "config_hash" in previous.brief.previous.reason
    assert previous.brief.previous.structure_change == "SAME_COUNT"
    assert previous.brief.previous.data_revision == "NONE"


def test_attach_previous_detects_engine_version_change(bundle):
    run = _run(bundle)
    previous_brief = run.brief.model_copy(update={"engine_version": "old-engine"})
    previous = attach_previous(
        run,
        previous_brief,
        previous_source=run.source,
        current_source=run.source,
    )
    assert previous.brief.previous is not None
    assert previous.brief.previous.change == "CONFIG_CHANGED"
    assert "engine_version" in previous.brief.previous.reason


def test_attach_previous_source_mismatch_is_not_comparable(bundle):
    run = _run(bundle)
    previous_brief = run.brief.model_copy(update={"timeframe": "1w"})
    previous_source = bundle.model_copy(update={"timeframe": "1w"})
    previous = attach_previous(
        run,
        previous_brief,
        previous_source=previous_source,
        current_source=bundle,
    )
    assert previous.brief.previous is not None
    assert previous.brief.previous.change == "NOT_COMPARABLE"
    assert "timeframe" in previous.brief.previous.reason

    previous_feed = run.brief.model_copy(update={"feed": "other-feed"})
    previous_feed_source = bundle.model_copy(
        update={"provenance": bundle.provenance.model_copy(update={"feed": "other-feed"})}
    )
    feed = attach_previous(
        run,
        previous_feed,
        previous_source=previous_feed_source,
        current_source=bundle,
    )
    assert feed.brief.previous is not None
    assert feed.brief.previous.change == "NOT_COMPARABLE"
    assert "feed" in feed.brief.previous.reason


def test_history_previous_finds_config_change(bundle, tmp_path):
    base = _run(bundle)

    def publish_variant(brief, name):
        brief = brief.model_copy(update={"mode": "FROZEN_DATA_REPLAY"})
        digest_value = digest(
            brief.model_dump(mode="json", exclude={"brief_digest", "generated_at"})
        )
        variant_brief = brief.model_copy(update={"brief_digest": digest_value})
        variant = BriefRun(
            brief=variant_brief,
            result=base.result,
            prepared=base.prepared,
            source=base.source,
        )
        return variant_brief, publish_brief(variant, tmp_path / name, formats=("md",))

    first_brief, first_path = publish_variant(
        base.brief.model_copy(update={"market_asof": date(2025, 1, 1), "config_hash": "config-a"}),
        "first",
    )
    second_brief, second_path = publish_variant(
        base.brief.model_copy(update={"market_asof": date(2025, 1, 2), "config_hash": "config-b"}),
        "second",
    )
    store = HistoryStore(tmp_path / ".cache/eliotbot")
    store.register(first_brief, first_path)
    store.register(second_brief, second_path)

    rows = store.query("AAPL")
    assert len(rows) == 2
    assert rows[0]["base_lineage_key"] == rows[1]["base_lineage_key"]
    assert rows[0]["lineage_key"] != rows[1]["lineage_key"]
    previous_row = store.previous(second_brief)
    assert previous_row is not None
    assert previous_row["brief_digest"] == first_brief.brief_digest
    assert store.load(previous_row).config_hash == "config-a"


def test_attach_previous_brief_scope_change_without_source(bundle):
    run = _run(bundle)
    previous_brief = run.brief.model_copy(update={"timeframe": "1w"})
    previous = attach_previous(run, previous_brief)
    assert previous.brief.previous is not None
    assert previous.brief.previous.change == "NOT_COMPARABLE"
    assert "timeframe" in previous.brief.previous.reason


def test_attach_previous_missing_source_is_not_comparable(bundle):
    run = _run(bundle)
    previous = attach_previous(run, run.brief)
    assert previous.brief.previous is not None
    assert previous.brief.previous.change == "NOT_COMPARABLE"
    assert previous.brief.previous.data_revision == "NOT_COMPARABLE"


def test_watchlist_dedupes_and_isolates_failures(bundle, tmp_path, monkeypatch):
    import eliotbot.cli.main as cli

    run = _run(bundle)
    output = tmp_path / "out"

    def fake(symbol, **kwargs):
        if symbol == "AAPL":
            return run, output
        raise SourceError("SOURCE_UNAVAILABLE: fixture")

    monkeypatch.setattr(cli, "_run_brief_core", fake)
    watchlist = tmp_path / "watchlist.txt"
    watchlist.write_text("AAPL\n\nAAPL\nMSFT\n")
    result = CliRunner().invoke(
        app,
        ["watchlist", str(watchlist), "--output-dir", str(output), "--json"],
    )
    assert result.exit_code == 4
    records = json.loads(result.stdout)
    assert [record["symbol"] for record in records] == ["AAPL", "MSFT"]
    assert records[0]["status"] == run.brief.status
    assert records[1]["status"] == "ERROR"
    assert "SOURCE_UNAVAILABLE" in records[1]["error"]


def test_brief_cli_reports_config_change(bundle, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    request = AnalysisRequest(symbol="AAPL", as_of=bundle.as_of)
    prepared = prepare_bundle(request, bundle)
    snapshot_id = SnapshotStore(tmp_path / ".cache/eliotbot").save(prepared)
    common = [
        "brief",
        "AAPL",
        "--offline",
        "--snapshot",
        snapshot_id,
        "--format",
        "json",
        "--json",
        "--no-weekly",
        "--no-window-check",
    ]
    first = CliRunner().invoke(
        app,
        common
        + [
            "--as-of",
            "2025-01-28",
            "--output-dir",
            str(tmp_path / "first-out"),
        ],
    )
    assert first.exit_code == 0, first.output
    second = CliRunner().invoke(
        app,
        common
        + [
            "--as-of",
            "2025-01-29",
            "--subwave-depth",
            "1",
            "--output-dir",
            str(tmp_path / "second-out"),
        ],
    )
    assert second.exit_code == 0, second.output
    payload = json.loads(second.stdout)
    assert payload["previous"]["change"] == "CONFIG_CHANGED"
    assert "config_hash" in payload["previous"]["reason"]


def test_watchlist_retries_transient_failure(bundle, tmp_path, monkeypatch):
    import eliotbot.cli.main as cli

    run = _run(bundle)
    output = tmp_path / "out"
    calls = Counter()

    def fake(symbol, **kwargs):
        calls[symbol] += 1
        if symbol == "AAPL" and calls[symbol] == 1:
            raise SourceError("transient source failure")
        return run, output

    monkeypatch.setattr(cli, "_run_brief_core", fake)
    watchlist = tmp_path / "watchlist.txt"
    watchlist.write_text("AAPL\nMSFT\n")
    result = CliRunner().invoke(
        app,
        [
            "watchlist",
            str(watchlist),
            "--output-dir",
            str(output),
            "--retries",
            "1",
            "--retry-delay",
            "0",
            "--json",
        ],
    )
    assert result.exit_code == 0
    records = json.loads(result.stdout)
    assert records[0]["attempts"] == 2
    assert records[1]["attempts"] == 1
    assert calls["AAPL"] == 2


def test_watchlist_passes_model_registry_to_workers(bundle, tmp_path, monkeypatch):
    import eliotbot.cli.main as cli

    run = _run(bundle)
    output = tmp_path / "out"
    captured = {}

    def fake(symbol, **kwargs):
        captured.update(kwargs)
        return run, output

    monkeypatch.setattr(cli, "_run_brief_core", fake)
    watchlist = tmp_path / "watchlist.txt"
    watchlist.write_text("AAPL\n")
    result = CliRunner().invoke(
        app,
        [
            "watchlist",
            str(watchlist),
            "--output-dir",
            str(output),
            "--model-registry",
            str(tmp_path / "registry"),
            "--retries",
            "0",
            "--json",
        ],
    )
    assert result.exit_code == 0
    assert captured["model_registry"] == tmp_path / "registry"


def test_watchlist_uses_bounded_concurrency(bundle, tmp_path, monkeypatch):
    import eliotbot.cli.main as cli

    run = _run(bundle)
    output = tmp_path / "out"
    threads = set()
    lock = threading.Lock()

    def fake(symbol, **kwargs):
        with lock:
            threads.add(threading.current_thread().name)
        time.sleep(0.05)
        return run, output

    monkeypatch.setattr(cli, "_run_brief_core", fake)
    watchlist = tmp_path / "watchlist.txt"
    watchlist.write_text("AAPL\nMSFT\nGOOG\nAMZN\n")
    result = CliRunner().invoke(
        app,
        [
            "watchlist",
            str(watchlist),
            "--output-dir",
            str(output),
            "--workers",
            "2",
            "--json",
        ],
    )
    assert result.exit_code == 0
    assert len(threads) >= 2


def test_history_cli_reads_published_brief(bundle, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    run = _run(bundle)
    path = publish_brief(run, tmp_path / "reports", formats=("md",))
    store = HistoryStore(tmp_path / ".cache/eliotbot")
    store.register(run.brief, path)

    result = CliRunner().invoke(app, ["history", "AAPL", "--json"])
    assert result.exit_code == 0, result.output
    rows = json.loads(result.stdout)
    assert len(rows) == 1
    assert rows[0]["brief_digest"] == run.brief.brief_digest
    assert rows[0]["status"] == run.brief.status
