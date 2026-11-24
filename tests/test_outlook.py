from datetime import UTC, datetime, timedelta

import pytest
from conftest import make_pivots

import elliottwaver.brief as brief_module
import elliottwaver.cli.main as cli
import elliottwaver.visualization.brief_formatter as formatter
from elliottwaver.brief import build_brief
from elliottwaver.config import Settings
from elliottwaver.data.demo import demo_bundle
from elliottwaver.engine.matcher import Candidate
from elliottwaver.engine.projector import project_with_gates
from elliottwaver.engine.templates import IMPULSE, WXY
from elliottwaver.history import HistoryStore
from elliottwaver.outlook import build_outlook
from elliottwaver.schemas.market import AnalysisRequest


def _as_of():
    return datetime(2026, 1, 1, tzinfo=UTC)


def _candidate(template, prices, completed, stage):
    bars, points = make_pivots(prices)
    return bars, Candidate(template, points[:completed], points[-1], 1, stage, (), (1.4,))


def test_project_with_gates_assigns_impulse_advance_role():
    bars, candidate = _candidate(IMPULSE, [100, 120, 110], 2, "W2_DEVELOPING")
    projection = project_with_gates(
        candidate,
        bars,
        atr=1.0,
        coordinate_id="fixture",
        as_of=_as_of(),
        tick=0.01,
    )
    assert projection.active_wave == "2"
    assert projection.current_leg_direction == "DOWN"
    assert projection.advance_direction == "UP"
    roles = {(item.role, item.condition.kind) for item in projection.conditions}
    assert ("ADVANCE", "CONFIRMATION") in roles
    assert ("INVALIDATION", "INVALIDATION") in roles


def test_project_with_gates_assigns_completed_stage_exit_only():
    bars, candidate = _candidate(IMPULSE, [100, 120, 110, 150, 132, 160], 6, "COMPLETE")
    projection = project_with_gates(
        candidate,
        bars,
        atr=1.0,
        coordinate_id="fixture",
        as_of=_as_of(),
        tick=0.01,
    )
    assert projection.active_wave is None
    assert projection.current_leg_direction == "UNKNOWN"
    assert {item.role for item in projection.conditions} == {"STAGE_EXIT"}
    assert projection.zones


def test_project_with_gates_suppresses_double_zigzag_specific_evidence():
    bars, candidate = _candidate(WXY, [100, 120, 110, 130], 3, "Y_DEVELOPING")
    projection = project_with_gates(
        candidate,
        bars,
        atr=1.0,
        coordinate_id="fixture",
        as_of=_as_of(),
        tick=0.01,
        variant="DOUBLE_THREE",
    )
    assert projection.zones == ()
    assert all(
        "双锯齿" not in item.condition.reason and "W终点" not in item.condition.reason
        for item in projection.conditions
    )


def test_build_outlook_has_five_sessions_and_cases():
    bundle = demo_bundle("AAPL")
    run = build_brief(
        AnalysisRequest(symbol="AAPL", as_of=bundle.as_of, mode="FROZEN_DATA_REPLAY"),
        bundle,
        Settings(),
        include_weekly=False,
        check_windows=False,
    )
    outlook = run.brief.outlook
    assert outlook is not None
    assert outlook.status == "AVAILABLE"
    assert outlook.horizon == 5
    assert outlook.horizon_unit == "SESSIONS"
    assert len(outlook.periods) == 5
    assert outlook.cases
    assert outlook.cases[0].slot == "PRIMARY"
    assert all(event.event_id for event in outlook.cases[0].events)


def test_build_outlook_is_unavailable_without_cards():
    bundle = demo_bundle("AAPL")
    run = build_brief(
        AnalysisRequest(symbol="AAPL", as_of=bundle.as_of),
        bundle,
        Settings(),
        include_weekly=False,
        check_windows=False,
    )
    outlook = build_outlook((), run.result, run.prepared, {})
    assert outlook.status == "UNAVAILABLE"
    assert "OUTLOOK_UNAVAILABLE" in (outlook.reason or "")


def test_project_with_gates_down_mirror():
    bars, points = make_pivots([200, 180, 190])
    candidate = Candidate(IMPULSE, points[:2], points[2], -1, "W2_DEVELOPING", (), (1.4,))
    projection = project_with_gates(
        candidate,
        bars,
        atr=1.0,
        coordinate_id="fixture",
        as_of=_as_of(),
        tick=0.01,
    )
    assert projection.active_wave == "2"
    assert projection.current_leg_direction == "UP"
    assert projection.advance_direction == "DOWN"
    assert any(item.role == "ADVANCE" for item in projection.conditions)


def test_build_outlook_weekly_has_two_periods():
    bundle = demo_bundle("AAPL", timeframe="1w")
    run = build_brief(
        AnalysisRequest(
            symbol="AAPL", as_of=bundle.as_of, timeframe="1w", mode="FROZEN_DATA_REPLAY"
        ),
        bundle,
        Settings(),
        include_weekly=False,
        check_windows=False,
    )
    outlook = run.brief.outlook
    assert outlook is not None
    assert outlook.status == "AVAILABLE"
    assert outlook.horizon == 2
    assert outlook.horizon_unit == "WEEKS"
    assert len(outlook.periods) == 2


@pytest.mark.parametrize("timeframe", ("1d", "1w"))
def test_live_render_crossing_open_requires_new_observation_window(
    tmp_path, monkeypatch, timeframe
):
    bundle = demo_bundle("AAPL", timeframe=timeframe)
    request = AnalysisRequest(symbol="AAPL", as_of=bundle.as_of, timeframe=timeframe)
    options = dict(include_weekly=False, check_windows=False)
    replay = build_brief(
        request.model_copy(update={"mode": "FROZEN_DATA_REPLAY"}), bundle, Settings(), **options
    )
    first, second = replay.brief.outlook.periods[:2]
    now = first.start_at

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return now.astimezone(tz)

    monkeypatch.setattr(brief_module, "datetime", Clock)
    original = build_brief(request, bundle, Settings(), **options)
    assert original.brief.outlook.periods[0] == first
    ticks = iter((now, now + timedelta(microseconds=1)))
    with pytest.raises(formatter.PublicationWindowElapsed):
        formatter.publish_brief(original, tmp_path, formats=(), clock=lambda: next(ticks))
    assert not list(tmp_path.rglob("brief.json"))

    now += timedelta(microseconds=1)
    later = build_brief(request, bundle, Settings(), **options)
    assert later.brief.outlook.periods[0] == second
    path = formatter.publish_brief(later, tmp_path, formats=(), clock=lambda: now)
    brief, _ = formatter.read_brief_report(path)
    assert formatter.read_issuance(path, brief) == now
    (path / formatter.ISSUANCE_FILE).unlink()
    with pytest.raises(formatter.DataError, match="BRIEF_REPORT_INVALID"):
        formatter.read_brief_report(path)


def test_cli_rerenders_and_registers_only_the_issued_window(tmp_path, monkeypatch):
    bundle = demo_bundle("AAPL")
    request = AnalysisRequest(symbol="AAPL", as_of=bundle.as_of, mode="FROZEN_DATA_REPLAY")
    run = build_brief(request, bundle, Settings(), include_weekly=False, check_windows=False)
    first, second = run.brief.outlook.periods[:2]
    now = first.start_at

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return now.astimezone(tz)

    def publish(run, *args):
        nonlocal now
        if run.brief.outlook.periods[0] == first:
            now += timedelta(microseconds=1)
        return formatter.publish_brief(run, *args, clock=lambda: now)

    cache = tmp_path / "cache"
    reports = tmp_path / "reports"
    config = tmp_path / "config.yaml"
    config.write_text(f"cache:\n  directory: {cache}\n")
    monkeypatch.setattr(brief_module, "datetime", Clock)
    monkeypatch.setattr(cli, "fetch_bundle", lambda *_args, **_kwargs: bundle)
    monkeypatch.setattr(cli, "parse_as_of", lambda *_args, **_kwargs: bundle.as_of)
    monkeypatch.setattr(cli, "publish_brief", publish)
    issued, path = cli._run_brief_core(
        "AAPL", config=config, output_dir=reports, format="json", weekly=False, window_check=False
    )
    assert issued.brief.outlook.periods[0] == second
    assert list(reports.rglob("issuance.json")) == [path / "issuance.json"]
    assert list(reports.rglob("brief.json")) == [path / "brief.json"]
    assert [r["brief_digest"] for r in HistoryStore(cache).query("AAPL")] == [
        issued.brief.brief_digest
    ]
    first_time = formatter.read_issuance(path, issued.brief)
    now += timedelta(minutes=1)
    retry, retry_path = cli._run_brief_core(
        "AAPL", config=config, output_dir=reports, format="json", weekly=False, window_check=False
    )
    assert (retry.brief.brief_digest, retry_path) == (issued.brief.brief_digest, path)
    assert formatter.read_issuance(path, retry.brief) == first_time
    assert len(list(reports.rglob("issuance.json"))) == 1


def test_receipt_write_crossing_open_leaves_no_issued_report(tmp_path, monkeypatch):
    bundle = demo_bundle("SPX")
    replay = build_brief(
        AnalysisRequest(symbol="SPX", as_of=bundle.as_of, mode="FROZEN_DATA_REPLAY"),
        bundle,
        Settings(),
        include_weekly=False,
        check_windows=False,
    )
    opening = replay.brief.outlook.periods[0].start_at
    now = opening

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return now.astimezone(tz)

    actual = formatter.atomic_write

    def slow_write(path, content):
        nonlocal now
        actual(path, content)
        if path.name == formatter.ISSUANCE_FILE:
            with pytest.raises(formatter.DataError, match="BRIEF_REPORT_INVALID"):
                formatter.read_brief_report(path.parent)
            now += timedelta(seconds=1)

    monkeypatch.setattr(formatter, "atomic_write", slow_write)
    monkeypatch.setattr(brief_module, "datetime", Clock)
    live = build_brief(
        AnalysisRequest(symbol="SPX", as_of=bundle.as_of),
        bundle,
        Settings(),
        include_weekly=False,
        check_windows=False,
    )
    with pytest.raises(formatter.PublicationWindowElapsed):
        formatter.publish_brief(live, tmp_path, formats=(), clock=lambda: now)
    assert not list(tmp_path.rglob("brief.json"))
