from elliottwaver.brief import build_brief
from elliottwaver.cli.console_render import render_brief
from elliottwaver.config import Settings
from elliottwaver.data.demo import demo_bundle
from elliottwaver.schemas.market import AnalysisRequest
from elliottwaver.settlement import SettlementStore, event_specs
from elliottwaver.visualization.brief_formatter import brief_html, brief_markdown, publish_brief


def _run(tmp_path):
    bundle = demo_bundle()
    decision = bundle.bars[-30]
    run = build_brief(
        AnalysisRequest(
            symbol="AAPL",
            as_of=decision.usable_at,
            end=decision.session_date,
            mode="FROZEN_DATA_REPLAY",
        ),
        bundle,
        Settings(),
        include_weekly=False,
        check_windows=False,
    )
    path = publish_brief(run, tmp_path / "reports", formats=("md", "html"))
    return bundle, run, path


def test_brief_markdown_html_and_terminal_put_readable_outlook_first(tmp_path, capsys, monkeypatch):
    monkeypatch.setenv("COLUMNS", "72")
    _bundle, run, path = _run(tmp_path)
    markdown = brief_markdown(run.brief, run.result)
    html = brief_html(run.brief, run.result)
    render_brief(run.brief, run.result)
    console = capsys.readouterr().out
    for report in (markdown, html, console):
        assert report.index("短期观察") < report.index("主选") < report.index("核对依据")
        assert "OUTLOOK_TARGET_FIRST_5D_V1" not in report
        assert "NO_ADVANCE_GATE" not in report
        assert "Top-K" not in report
        assert "质量分衡量形态吻合程度" in report.replace("\n", "")
    assert any("目标" in line for line in console.splitlines()[:12])
    payload = (path / "brief.json").read_text()
    assert "OUTLOOK_TARGET_FIRST_5D_V1" in payload
    assert run.brief.brief_digest in payload


def test_event_specs_include_outlook_events(tmp_path):
    _bundle, run, _path = _run(tmp_path)
    specs = event_specs(run.brief)
    ids = {event_id for event_id, _scope, _horizon, _unit in specs}
    assert "ADVANCE_FIRST_5D_V1" in ids
    assert "OUTLOOK_TARGET_FIRST_5D_V1" in ids


def test_settle_brief_records_outlook_events(tmp_path):
    bundle, run, path = _run(tmp_path)
    store = SettlementStore(tmp_path / ".cache/elliottwaver")
    store.register_brief(run.brief, path)
    store.settle_brief(run.brief, path, bundle)
    rows = store.rows(brief_digest=run.brief.brief_digest)
    ids = {row["event_id"] for row in rows}
    assert "ADVANCE_FIRST_5D_V1" in ids
    assert "OUTLOOK_TARGET_FIRST_5D_V1" in ids


def test_stage_exit_is_strict_at_the_boundary():
    from datetime import UTC, datetime

    from conftest import make_bar

    from elliottwaver.evaluation.events import EVENT_SPECS
    from elliottwaver.evaluation.outcomes import OutcomeWindow, evaluate_outlook_event
    from elliottwaver.schemas.outlook import OutlookCase, OutlookCondition
    from elliottwaver.schemas.wave import LevelCondition, PriceZone

    active = datetime(2026, 1, 1, tzinfo=UTC)
    case = OutlookCase(
        scenario_id="scenario",
        structure_key="structure",
        slot="PRIMARY",
        target=PriceZone(
            low=156,
            high=162,
            label="目标区1",
            methods=("fixture",),
            independent_method_count=1,
        ),
        target_mode="ACTIVE",
        target_direction="UP",
        conditions=(
            OutlookCondition(
                role="STAGE_EXIT",
                condition=LevelCondition(
                    kind="STAGE_EXIT",
                    side="LOW",
                    op="<",
                    price=150,
                    active_from=active,
                    reason="strict stage exit",
                    coordinate_id="fixture",
                ),
            ),
        ),
    )

    def outcome(low):
        bars = tuple(make_bar(index, 154, high=155, low=low) for index in range(5))
        window = OutcomeWindow(
            tuple(bar.session_date for bar in bars),
            bars,
            bars[-1].session_date,
        )
        return evaluate_outlook_event(EVENT_SPECS["OUTLOOK_TARGET_FIRST_5D_V1"], case, window)

    assert outcome(150).status == "EXPIRED"
    assert outcome(149.99).status == "STAGE_EXIT_FIRST"


def test_weekly_outlook_skips_the_current_week(monkeypatch):
    from datetime import UTC, date, datetime, time
    from types import SimpleNamespace

    from elliottwaver.data.calendar import Session
    from elliottwaver.outlook import _periods

    def session(day):
        return Session(
            date=day,
            open_at=datetime.combine(day, time(14, 30), tzinfo=UTC),
            close_at=datetime.combine(day, time(21), tzinfo=UTC),
        )

    days = [date(2026, 11, 18), date(2026, 11, 19), date(2026, 11, 20)]
    days += [
        date(2026, 11, 23) + __import__("datetime").timedelta(days=index) for index in range(10)
    ]
    fake = SimpleNamespace(sessions=tuple(session(day) for day in days))
    monkeypatch.setattr("elliottwaver.outlook.load_calendar", lambda *args, **kwargs: fake)
    bundle = demo_bundle("AAPL")
    periods, horizon, unit = _periods(bundle, date(2026, 11, 18), "1w")
    assert horizon == 2 and unit == "WEEKS"
    assert len(periods) == 2
    assert periods[0].sessions[0] == date(2026, 11, 23)


def test_daily_outlook_skips_holiday_sessions(monkeypatch):
    from datetime import UTC, date, datetime, time
    from types import SimpleNamespace

    from elliottwaver.data.calendar import Session
    from elliottwaver.outlook import _periods

    def session(day):
        return Session(
            date=day,
            open_at=datetime.combine(day, time(14, 30), tzinfo=UTC),
            close_at=datetime.combine(day, time(21), tzinfo=UTC),
        )

    days = [
        date(2026, 11, 16),
        date(2026, 11, 17),
        date(2026, 11, 19),
        date(2026, 11, 20),
        date(2026, 11, 23),
        date(2026, 11, 24),
    ]
    fake = SimpleNamespace(sessions=tuple(session(day) for day in days))
    monkeypatch.setattr("elliottwaver.outlook.load_calendar", lambda *args, **kwargs: fake)
    bundle = demo_bundle("AAPL")
    periods, horizon, unit = _periods(bundle, date(2026, 11, 16), "1d")
    assert horizon == 5 and unit == "SESSIONS"
    assert len(periods) == 5
    assert date(2026, 11, 18) not in [period.sessions[0] for period in periods]


def test_cn_split_settlement_matches_unsplit(tmp_path):
    from datetime import timedelta

    from elliottwaver.brief import build_brief
    from elliottwaver.config import Settings
    from elliottwaver.data.demo import demo_bundle
    from elliottwaver.data.snapshots import SnapshotStore
    from elliottwaver.schemas.market import AnalysisRequest, FactorPoint
    from elliottwaver.settlement import SettlementStore
    from elliottwaver.visualization.brief_formatter import publish_brief

    bundle = demo_bundle("600519.SH")
    factors = (FactorPoint(effective_date=bundle.bars[0].session_date, factor=1.0),)
    bundle = bundle.model_copy(
        update={
            "provenance": bundle.provenance.model_copy(update={"adjustment": "basis_date_qfq"}),
            "factors": factors,
        }
    )
    request = AnalysisRequest(
        symbol="600519.SH",
        as_of=bundle.bars[-24].usable_at,
        end=bundle.bars[-24].session_date,
        mode="FROZEN_DATA_REPLAY",
    )
    run = build_brief(request, bundle, Settings(), include_weekly=False, check_windows=False)
    split_date = request.end + timedelta(days=1)
    split_factors = (
        FactorPoint(effective_date=bundle.bars[0].session_date, factor=1.0),
        FactorPoint(effective_date=split_date, factor=2.0),
    )

    def half(bar):
        return bar.model_copy(
            update={field: getattr(bar, field) * 0.5 for field in ("open", "high", "low", "close")}
        )

    split = bundle.model_copy(
        update={
            "bars": tuple(
                bar if bar.session_date <= request.end else half(bar) for bar in bundle.bars
            ),
            "raw_bars": tuple(
                bar if bar.session_date <= request.end else half(bar) for bar in bundle.raw_bars
            ),
            "factors": split_factors,
            "provenance": bundle.provenance.model_copy(update={"price_coordinate_id": "split"}),
        }
    )
    cache = SnapshotStore(tmp_path / "cache")
    cache.save(run.source)
    cache.save(run.prepared)
    path = publish_brief(run, tmp_path / "reports", formats=("md",))
    ledger = SettlementStore(tmp_path / "cache")
    ledger.register_brief(run.brief, path)
    base_rows = ledger.settle_brief(run.brief, path, bundle)
    split_rows = ledger.settle_brief(run.brief, path, split)
    base = {(row["event_id"], row["scope"]): row["latest"] for row in base_rows}
    changed = {(row["event_id"], row["scope"]): row["latest"] for row in split_rows}
    assert base.keys() == changed.keys()
    for key in base:
        assert base[key]["outcome"] == changed[key]["outcome"]
        assert base[key]["value"] == changed[key]["value"]


def test_old_report_without_outlook_reads_and_renders(tmp_path):
    from dataclasses import replace

    from elliottwaver.brief import build_brief
    from elliottwaver.config import Settings
    from elliottwaver.data.demo import demo_bundle
    from elliottwaver.schemas.market import AnalysisRequest
    from elliottwaver.util import digest
    from elliottwaver.visualization.brief_formatter import (
        brief_html,
        brief_markdown,
        publish_brief,
        read_brief_report,
    )

    bundle = demo_bundle("AAPL")
    run = build_brief(
        AnalysisRequest(symbol="AAPL", as_of=bundle.as_of),
        bundle,
        Settings(),
        include_weekly=False,
        check_windows=False,
    )
    old_brief = run.brief.model_copy(update={"outlook": None})
    old_brief = old_brief.model_copy(
        update={
            "brief_digest": digest(
                old_brief.model_dump(mode="json", exclude={"brief_digest", "generated_at"})
            )
        }
    )
    old_run = replace(run, brief=old_brief)
    path = publish_brief(old_run, tmp_path / "old-reports", formats=("md", "html"))
    loaded, _result = read_brief_report(path)
    assert loaded.outlook is None
    assert "短期观察" not in brief_markdown(loaded)
    assert "短期观察" not in brief_html(loaded)


def test_outcome_window_explicit_cutoff_is_right_censored():
    from datetime import timedelta

    from elliottwaver.data.demo import demo_bundle
    from elliottwaver.evaluation.outcomes import outcome_window

    bundle = demo_bundle("AAPL")
    decision = bundle.bars[-30].session_date
    window = outcome_window(
        bundle,
        decision,
        5,
        outcome_as_of=decision + timedelta(days=1),
    )
    assert len(window.dates) == 5
    assert window.bars[-1] is None
    assert window.missing(window.dates[-1]).status == "RIGHT_CENSORED"


def test_previous_brief_replay_preserves_outlook(tmp_path):
    from elliottwaver.brief import attach_previous, build_brief
    from elliottwaver.config import Settings
    from elliottwaver.data.demo import demo_bundle
    from elliottwaver.schemas.market import AnalysisRequest
    from elliottwaver.visualization.brief_formatter import publish_brief

    bundle = demo_bundle("AAPL")
    previous = build_brief(
        AnalysisRequest(
            symbol="AAPL",
            as_of=bundle.bars[-30].usable_at,
            end=bundle.bars[-30].session_date,
        ),
        bundle,
        Settings(),
        include_weekly=False,
        check_windows=False,
    )
    current = build_brief(
        AnalysisRequest(
            symbol="AAPL",
            as_of=bundle.bars[-20].usable_at,
            end=bundle.bars[-20].session_date,
        ),
        bundle,
        Settings(),
        include_weekly=False,
        check_windows=False,
    )
    path = publish_brief(previous, tmp_path / "reports", formats=("md",))
    attached = attach_previous(
        current,
        previous.brief,
        str(path),
        previous_source=previous.source,
        current_source=current.source,
        previous_result=previous.result,
    )
    assert attached.brief.previous is not None
    assert attached.brief.previous.change in {
        "SAME_COUNT",
        "TIP_MOVED",
        "STAGE_ADVANCED",
        "COUNT_CHANGED",
        "EVIDENCE_CHANGED",
        "HARD_INVALIDATED",
    }
    assert attached.brief.outlook is not None
