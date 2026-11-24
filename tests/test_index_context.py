import json
from dataclasses import replace
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from typer.testing import CliRunner

from elliottwaver.brief import build_brief
from elliottwaver.cli.main import app
from elliottwaver.config import IndexGuidanceConfig, Settings
from elliottwaver.data.calendar import load_calendar
from elliottwaver.data.demo import demo_bundle
from elliottwaver.data.periods import intraday_windows
from elliottwaver.data.snapshots import SnapshotStore
from elliottwaver.history import HistoryStore
from elliottwaver.index_context import (
    describe_breadth,
    describe_events,
    describe_reference,
    describe_trend,
)
from elliottwaver.index_intraday import (
    ParentIssuance,
    build_intraday_tracking,
    relative_volume,
    verify_price_basis,
)
from elliottwaver.schemas.index_context import (
    ConstituentClose,
    ConstituentVersion,
    EventCoverage,
    EventVersion,
    IndexContextDataset,
)
from elliottwaver.schemas.market import AnalysisRequest, Bar, MarketDataBundle
from elliottwaver.util import digest
from elliottwaver.visualization.brief_formatter import publish_brief, read_brief_report


@pytest.fixture(scope="module")
def daily():
    source = demo_bundle("SPX")
    bar = source.bars[186]
    request = AnalysisRequest(
        symbol="SPX", as_of=bar.usable_at, end=bar.session_date, mode="FROZEN_DATA_REPLAY"
    )
    run = build_brief(request, source, Settings(), include_weekly=False, check_windows=False)
    tracks = []
    for track in run.brief.index_guidance.tracks:
        original = track.plan
        assert original
        plan = original.model_copy(
            update={
                "trigger": original.trigger.model_copy(update={"price": 100}),
                "targets": (original.targets[0].model_copy(update={"low": 108, "high": 110}),),
                "invalidations": (
                    original.invalidations[0].model_copy(
                        update={"price": 96, "side": "LOW", "op": "<"}
                    ),
                ),
                "stage_exits": (),
                "max_entry_index_price": 102,
            }
        )
        tracks.append(track.model_copy(update={"plan": plan}))
    b = run.brief.model_copy(
        update={
            "index_guidance": run.brief.index_guidance.model_copy(update={"tracks": tuple(tracks)})
        }
    )
    return replace(
        run,
        brief=b.model_copy(
            update={
                "brief_digest": digest(
                    b.model_dump(mode="json", exclude={"brief_digest", "generated_at"})
                )
            }
        ),
    )


def slot(opened, closed, day, quote, volume=1):
    high, low, close = quote
    return Bar(
        start_at=opened,
        end_at=closed,
        session_date=day,
        open=close,
        close=close,
        high=high,
        low=low,
        volume=volume,
        observed_at=closed,
    )


def child_bundle(daily, bars, *, expected=None):
    base = daily.prepared
    last = bars[-1]
    return MarketDataBundle.model_validate(
        base.model_copy(
            update={
                "bars": tuple(bars),
                "raw_bars": tuple(bars),
                "timeframe": "15m",
                "as_of": last.usable_at,
                "sessions": tuple(sorted({bar.session_date for bar in bars})),
                "expected_bar_starts": tuple(expected or (bar.start_at for bar in bars)),
                "daily_context": base.bars,
                "raw_daily_context": base.raw_bars,
                "provenance": base.provenance.model_copy(update={"basis_date": last.session_date}),
            }
        )
    )


def trigger_fixture(daily, *, stop=False, hourly_ready=True):
    plan = daily.brief.index_guidance.tracks[0].plan
    day = plan.periods[0].end_session
    session = load_calendar(daily.prepared.instrument, day, day).session(day)
    slots = tuple(intraday_windows(session, 15))
    points = [(99, 97, 98)] * 4 if hourly_ready else []
    points += [(101, 95 if stop else 98, 99), (103, 99, 102), (102, 100, 101)]
    bars = [
        slot(start, end, day, quote) for (start, end), quote in zip(slots, points, strict=False)
    ]
    child = child_bundle(daily, bars)
    brief = daily.brief.model_copy(
        update={
            "as_of": child.as_of,
            "timeframe": "15m",
            "market_asof": day,
            "index_guidance": None,
        }
    )
    origins = {
        t.plan.plan_id: ParentIssuance(daily.brief.brief_digest, t.plan.issued_at, daily.prepared)
        for t in daily.brief.index_guidance.tracks
    }
    return child, brief, origins


def test_precise_touch_close_retest_and_missing_slot(daily):
    child, brief, origins = trigger_fixture(daily)
    tracked = build_intraday_tracking(brief, child, daily.brief, origins, IndexGuidanceConfig())
    assert {row.timeframe for row in tracked.inputs} == {"1d", "60m", "15m"}
    for track in tracked.tracks:
        assert track.status == "TRIGGERED"
        assert [row.bar_end_at for row in track.variants] == [bar.end_at for bar in child.bars[4:7]]
        assert track.parent_brief_digest == daily.brief.brief_digest
        assert track.price_basis == "SAME_FEED"
    lost = child_bundle(
        daily, [*child.bars[:5], child.bars[-1]], expected=child.expected_bar_starts
    )
    result = build_intraday_tracking(brief, lost, daily.brief, origins, IndexGuidanceConfig())
    assert all(track.status == "MISSING_15M_SLOT" for track in result.tracks)


def test_missing_background_ambiguity_and_future_parent(daily):
    child, brief, origins = trigger_fixture(daily, hourly_ready=False)
    assert all(
        t.status == "BACKGROUND_60M_NOT_READY"
        for t in build_intraday_tracking(
            brief, child, daily.brief, origins, IndexGuidanceConfig()
        ).tracks
    )
    child, brief, origins = trigger_fixture(daily, stop=True)
    value = build_intraday_tracking(brief, child, daily.brief, origins, IndexGuidanceConfig())
    assert value.tracks[0].status == "PARENT_INVALIDATED"
    assert value.tracks[0].variants[0].status == "AMBIGUOUS"
    later = {
        k: replace(v, visible_at=brief.as_of + timedelta(seconds=1)) for k, v in origins.items()
    }
    value = build_intraday_tracking(brief, child, daily.brief, later, IndexGuidanceConfig())
    assert value.tracks[0].status == "PARENT_NOT_VISIBLE"


def test_same_slot_volume_excludes_current_day_and_other_slot(daily):
    plan = daily.brief.index_guidance.tracks[0].plan
    first = plan.periods[0].end_session
    calendar = load_calendar(daily.prepared.instrument, first, first)
    days = [s for s in calendar.sessions if s.date >= first][:4]
    records = []
    for market_day, volume in zip(days, (80, 100, 120, 150), strict=True):
        opening = tuple(intraday_windows(market_day, 15))
        start, end = opening[0]
        records.append(slot(start, end, market_day.date, (105, 95, 101), volume))
        if volume != 150:
            start, end = opening[1]
            records.append(slot(start, end, market_day.date, (105, 95, 101), 999))
    child = child_bundle(daily, records)
    policy = IndexGuidanceConfig(min_volume_sessions=3)
    assert relative_volume(child, policy).value == 1.5
    incomparable = child.model_copy(
        update={"provenance": child.provenance.model_copy(update={"volume_comparable": False})}
    )
    assert relative_volume(incomparable, policy).status == "VOLUME_NOT_COMPARABLE"


def test_reference_cutoff_scope_and_unchanged_primary(daily):
    main = daily.prepared
    ref = demo_bundle("NDX")
    ref = ref.model_copy(
        update={
            "bars": main.bars,
            "raw_bars": main.raw_bars,
            "as_of": main.as_of,
            "sessions": main.sessions,
            "provenance": ref.provenance.model_copy(
                update={"basis_date": main.bars[-1].session_date}
            ),
        }
    )
    context = describe_reference(main, ref, "NDX", main.as_of, 20)
    assert context.spread_pp == 0 and context.end == main.bars[-1].session_date
    assert describe_reference(main, ref, "SPX", main.as_of, 20).status == "SAME_INDEX"
    assert describe_reference(main, ref, "DJI", main.as_of, 20).status == "NOT_COMPARABLE"
    later = main.as_of + timedelta(minutes=1)
    raw = (*ref.raw_bars[:-1], ref.raw_bars[-1].model_copy(update={"available_at": later}))
    late = ref.model_copy(update={"raw_bars": raw, "as_of": later})
    assert describe_reference(main, late, "NDX", main.as_of, 20).status == "STALE_REFERENCE"
    assert describe_reference(main, late, "NDX", later, 20).spread_pp == 0
    own = describe_trend(main, main.as_of, 20, 10)
    assert own.prior_windows and own.volatility_percentile is not None
    assert own.return_pct == pytest.approx(100 * (main.bars[-1].close / main.bars[-21].close - 1))


def test_event_visibility_revisions_and_constituent_coverage(daily):
    zone = ZoneInfo(daily.prepared.instrument.timezone)
    day = daily.brief.market_asof
    cutoff = datetime(day.year, day.month, day.day, 10, 10, tzinfo=zone)
    yesterday = day - timedelta(days=1)
    rows = tuple(
        ConstituentVersion(
            symbol=f"S{i}",
            revision=1,
            announced_at=cutoff - timedelta(days=4),
            received_at=cutoff - timedelta(days=3),
            effective_from=yesterday - timedelta(days=5),
            weight={0: 0.3, 1: 0.2}.get(i, 0.0625),
        )
        for i in range(10)
    )
    closes = tuple(
        ConstituentClose(
            symbol=f"S{i}",
            session=session,
            usable_at=cutoff - timedelta(minutes=30),
            close=price,
            basis="raw",
        )
        for i in range(6)
        for session, price in ((yesterday, 100), (day, 101 if i < 4 else 99))
    )
    announce = cutoff - timedelta(minutes=5)
    receive = cutoff - timedelta(minutes=3)
    events = tuple(
        EventVersion(
            event_id=f"e{i}",
            revision=1,
            symbol=symbol,
            kind="EARNINGS",
            announcement_at=announce,
            received_at=receive,
            expected_at=cutoff + timedelta(hours=7),
            source="fixture",
        )
        for i, symbol in enumerate(("S0", "S0", "S1"))
    )
    delay = events[0].model_copy(
        update={
            "revision": 2,
            "received_at": cutoff + timedelta(days=1),
            "expected_at": cutoff + timedelta(days=100),
        }
    )
    dataset = IndexContextDataset(
        symbol="SPX",
        membership=rows,
        closes=closes,
        events=(*events, delay),
        event_coverage=(
            EventCoverage(
                kind="EARNINGS",
                source="fixture",
                known_at=cutoff - timedelta(days=1),
                start=yesterday,
                end=day + timedelta(days=60),
            ),
        ),
    )
    breadth = describe_breadth(dataset, day, yesterday, cutoff, 0.7)
    assert breadth.member_coverage == 0.6 and breadth.rising_fraction == pytest.approx(4 / 6)
    assert breadth.status == "INSUFFICIENT_COVERAGE"
    for minutes in (10, 4):
        pending = daily.brief.model_copy(
            update={"as_of": cutoff - timedelta(minutes=minutes), "index_guidance": None}
        )
        assert (
            describe_events(pending, daily.prepared, dataset, IndexGuidanceConfig())[0].events == ()
        )
    available = daily.brief.model_copy(update={"as_of": cutoff, "index_guidance": None})
    short = describe_events(available, daily.prepared, dataset, IndexGuidanceConfig())[0]
    assert len(short.events) == 3 and short.earnings_event_weight == 0.5
    assert short.events[0].revision == 1


def test_offline_cli_context_and_parent_receipts_roundtrip(daily, tmp_path):
    child, _, origins = trigger_fixture(daily)
    # Supply enough preceding official slots to run the regular wave engine.
    session = child.bars[0].session_date
    calendar = load_calendar(child.instrument, session - timedelta(days=15), session)
    history = []
    for day in [row for row in calendar.sessions if row.date < session][-4:]:
        history.extend(
            slot(start, end, day.date, (99, 96, 98)) for start, end in intraday_windows(day, 15)
        )
    child = child_bundle(daily, [*history, *child.bars])
    cache = tmp_path / "cache"
    reports = tmp_path / "reports"
    config = tmp_path / "config.yaml"
    config.write_text(f"cache:\n  directory: {cache}\noutput:\n  directory: {reports}\n")
    store = SnapshotStore(cache)
    store.save(daily.source)
    store.save(daily.prepared)
    sid = store.save(child)
    owner = HistoryStore(cache)
    original = publish_brief(daily, reports, formats=())
    owner.register(daily.brief, original)
    assert owner.plan_origin("SPX", "FROZEN_DATA_REPLAY", next(iter(origins)), 1)
    cli = CliRunner()
    args = [
        "brief",
        "SPX",
        "--snapshot",
        sid,
        "--offline",
        "--timeframe",
        "15m",
        "--config",
        str(config),
        "--format",
        "json",
        "--json",
    ]
    first = cli.invoke(app, args)
    assert first.exit_code == 0, first.output
    b = json.loads(first.output)
    assert b["schema_version"] == "1.4"
    assert b["index_context"]["intraday"]["tracks"][0]["status"] == "TRIGGERED"
    assert b["outlook"]["horizon_unit"] == "BARS" and b["outlook"]["horizon"] == 5
    saved = next(
        r for r in owner.query("SPX", timeframe="15m") if r["brief_digest"] == b["brief_digest"]
    )
    assert read_brief_report(__import__("pathlib").Path(saved["report_path"]))[0].index_context
    again = cli.invoke(app, args)
    assert again.exit_code == 0, again.output
    rows = owner.query("SPX", timeframe="15m")
    alerts = [
        r for row in rows for r in owner.intraday_alerts("SPX", brief_digest=row["brief_digest"])
    ]
    assert len(alerts) == 2
    assert {r["method"] for r in alerts} == {"CLOSE"}
    import sqlite3

    with sqlite3.connect(owner.path) as db:
        for i in range(51):
            db.execute(
                """INSERT INTO briefs
                (brief_digest, lineage_key, base_lineage_key, symbol, market_asof, as_of,
                 observed_at, source_snapshot_id, report_path, mode, timeframe, feed,
                 adjustment, last_bar_end_at, last_bar_id, last_bar_available_at)
                SELECT ?, lineage_key, base_lineage_key, symbol, '2027-01-01', as_of,
                       observed_at, source_snapshot_id, report_path, mode, timeframe, feed,
                       adjustment, '2027-01-01T21:00:00+00:00', last_bar_id, last_bar_available_at
                FROM briefs WHERE brief_digest = ?""",
                (f"newer-{i}", b["brief_digest"]),
            )
    listing = cli.invoke(
        app, ["history", "SPX", "--timeframe", "15m", "--config", str(config), "--json"]
    )
    assert listing.exit_code == 0, listing.output
    recent = json.loads(listing.output)
    assert len(recent) == 50 and all(row["brief_digest"] != b["brief_digest"] for row in recent)
    assert {alert["transition_id"] for alert in recent[0]["pending_intraday_alerts"]} == {
        alert["transition_id"] for alert in alerts
    }


def test_disparate_feed_requires_two_completed_matching_sessions(daily):
    child, _, origins = trigger_fixture(daily)
    plan = daily.brief.index_guidance.tracks[0].plan
    origin = origins[plan.plan_id]
    switched = child.model_copy(
        update={"provenance": child.provenance.model_copy(update={"feed": "other"})}
    )
    assert verify_price_basis(plan, origin, switched)[0] == "PRICE_BASIS_UNVERIFIED"


def test_verified_cross_feed_uses_completed_overlap_and_original_signal(daily):
    child, _, origins = trigger_fixture(daily)
    plan = daily.brief.index_guidance.tracks[0].plan
    daily_source = daily.prepared
    native = daily_source.model_copy(
        update={"provenance": daily_source.provenance.model_copy(update={"adjustment": "none"})}
    )
    issue = ParentIssuance(origins[plan.plan_id].digest, plan.issued_at, native)
    plan = plan.model_copy(update={"input_snapshot_id": native.snapshot_id, "adjustment": "none"})
    first, last = native.raw_bars[-2:]
    sessions = load_calendar(native.instrument, first.session_date, last.session_date)
    bars = []
    for raw in (first, last):
        windows = tuple(intraday_windows(sessions.session(raw.session_date), 15))
        for j, (opened, closed) in enumerate(windows):
            item = slot(opened, closed, raw.session_date, (raw.high, raw.low, raw.close))
            if j == 0:
                item = item.model_copy(update={"open": raw.open})
            bars.append(item)
    cross = child_bundle(daily, [*bars, *child.bars])
    cross = cross.model_copy(
        update={
            "provenance": cross.provenance.model_copy(
                update={"adjustment": "none", "feed": "cross"}
            )
        }
    )
    status, overlap, proof = verify_price_basis(plan, issue, cross)
    assert status == "CROSS_FEED_VERIFIED" and overlap == (first.session_date, last.session_date)
    assert proof == plan.issued_at
    changed = bars[-1].model_copy(update={"high": bars[-1].high + 2})
    broken = (*cross.raw_bars[: len(bars) - 1], changed, *cross.raw_bars[len(bars) :])
    assert (
        verify_price_basis(plan, issue, cross.model_copy(update={"raw_bars": broken}))[0]
        == "PRICE_BASIS_UNVERIFIED"
    )


def test_holiday_context_lists_only_known_market_closure_without_vendor_events(daily):
    cutoff = datetime.fromisoformat("2024-11-28T10:00:00-05:00")
    brief = daily.brief.model_copy(update={"as_of": cutoff, "index_guidance": None})
    event, *rest = describe_events(brief, daily.prepared, None, IndexGuidanceConfig())
    assert cutoff.date() in event.closed_weekdays
    assert event.next_open_at == datetime.fromisoformat("2024-11-29T09:30:00-05:00")
    assert event.earnings_status == "COVERAGE_UNAVAILABLE" and event.earnings_event_weight is None


def test_disabling_context_does_not_reuse_context_from_a_previous_report(daily):
    from elliottwaver.cli.index_context import ResolvedIndexContext, attach_index_context

    enriched = attach_index_context(daily, ResolvedIndexContext(), Settings())
    assert enriched.brief.index_context is not None
    plain = attach_index_context(enriched, None, Settings())
    assert plain.brief.index_context is None
    assert plain.brief.brief_digest == daily.brief.brief_digest
    assert plain.brief.index_guidance == daily.brief.index_guidance


def test_operational_trigger_uses_frozen_policy_and_surfaces_ambiguity(daily):
    child, brief, origins = trigger_fixture(daily)
    changed = IndexGuidanceConfig(intraday_trigger="TOUCH", retest_tolerance_points=10)
    track = build_intraday_tracking(brief, child, daily.brief, origins, changed).tracks[0]
    assert track.selected_method == "CLOSE"
    assert track.variants[1].bar_end_at == child.bars[5].end_at
    bar = child.bars[4].model_copy(update={"high": 103, "low": 95, "close": 101})
    ambiguous = child_bundle(daily, [*child.bars[:4], bar])
    track = build_intraday_tracking(
        brief.model_copy(update={"as_of": ambiguous.as_of}),
        ambiguous,
        daily.brief,
        origins,
        changed,
    ).tracks[0]
    assert track.status == track.variants[1].status == "AMBIGUOUS"


def test_live_track_accounts_for_first_receipt_of_the_complete_prefix(daily):
    child, brief, origins = trigger_fixture(daily)
    cutoff = brief.as_of + timedelta(hours=2)
    delayed = [
        bar.model_copy(update={"observed_at": cutoff}) if i == 4 else bar
        for i, bar in enumerate(child.bars)
    ]
    child = child_bundle(daily, delayed)
    parent = daily.brief.model_copy(update={"mode": "LIVE"})
    parent = parent.model_copy(
        update={
            "index_guidance": parent.index_guidance.model_copy(
                update={
                    "tracks": tuple(
                        track.model_copy(
                            update={"plan": track.plan.model_copy(update={"mode": "LIVE"})}
                        )
                        for track in parent.index_guidance.tracks
                    )
                }
            )
        }
    )
    live = brief.model_copy(update={"mode": "LIVE", "generated_at": cutoff})
    recorded = build_intraday_tracking(live, child, parent, origins, IndexGuidanceConfig())
    assert recorded.tracks[0].status != "TRIGGERED"
    assert recorded.tracks[0].variants[1].status == "STALE_CROSSING"
    assert recorded.tracks[0].variants[1].observable_at == cutoff


def test_retest_can_be_confirmed_with_feed_delay_and_on_the_next_session(daily):
    child, brief, origins = trigger_fixture(daily)
    delayed = child_bundle(
        daily,
        [
            bar.model_copy(update={"available_at": bar.end_at + timedelta(minutes=20)})
            for bar in child.bars
        ],
    )
    result = build_intraday_tracking(
        brief.model_copy(update={"as_of": delayed.as_of}),
        delayed,
        daily.brief,
        origins,
        IndexGuidanceConfig(),
    )
    assert result.tracks[0].variants[2].observable_at == delayed.as_of

    plan = daily.brief.index_guidance.tracks[0].plan
    days = plan.periods[:2]
    calendar = load_calendar(child.instrument, days[0].end_session, days[1].end_session)
    first, second = (calendar.session(row.end_session) for row in days)
    bars = [
        slot(start, end, first.date, (99, 97, 98)) for start, end in intraday_windows(first, 15)
    ]
    bars[-1] = slot(bars[-1].start_at, bars[-1].end_at, first.date, (102, 99, 101))
    for i, (start, end) in enumerate(intraday_windows(second, 15)):
        bars.append(slot(start, end, second.date, (102, 100, 101) if i == 4 else (99, 97, 98)))
        if i == 4:
            break
    overnight = child_bundle(daily, bars)
    result = build_intraday_tracking(
        brief.model_copy(update={"as_of": overnight.as_of, "market_asof": second.date}),
        overnight,
        daily.brief,
        origins,
        IndexGuidanceConfig(),
    )
    assert result.tracks[0].variants[2].bar_end_at == bars[-1].end_at


def test_exact_parent_origin_and_operational_lineage(daily, tmp_path):
    from elliottwaver.cli.index_context import _parents, resolve_index_context

    owner = HistoryStore(tmp_path / "cache")
    store = SnapshotStore(tmp_path / "cache")
    store.save(daily.prepared)
    original = daily.brief
    track = original.index_guidance.tracks[0]
    plan = track.plan
    other_plan = plan.model_copy(update={"max_entry_index_price": 103})
    other_track = track.model_copy(update={"plan": other_plan})
    alternative = original.model_copy(
        update={
            "index_guidance": original.index_guidance.model_copy(
                update={"tracks": (other_track, *original.index_guidance.tracks[1:])}
            )
        }
    )
    versions = []
    for item in (original, alternative, alternative.model_copy(update={"retrospective": True})):
        item = item.model_copy(
            update={
                "brief_digest": digest(
                    item.model_dump(mode="json", exclude={"brief_digest", "generated_at"})
                )
            }
        )
        path = publish_brief(replace(daily, brief=item), tmp_path / "reports", formats=())
        owner.register(item, path)
        versions.append(item)
    for version, exact in zip(versions, (plan, other_plan, other_plan), strict=True):
        row = owner.plan_origin(
            "SPX",
            version.mode,
            plan.plan_id,
            plan.revision,
            plan=exact,
            retrospective=version.retrospective,
        )
        assert row["brief_digest"] == version.brief_digest

    child, brief, _ = trigger_fixture(daily)
    parent, origins = _parents(brief, owner, store)
    assert parent is not None and not parent.retrospective and len(origins) == 2
    context = resolve_index_context(
        replace(daily, brief=brief, prepared=child),
        AnalysisRequest(symbol="SPX", as_of=brief.as_of, timeframe="15m", mode=brief.mode),
        Settings(index_guidance=IndexGuidanceConfig(event_context=True, intraday_tracking=False)),
        owner,
        store,
    )
    assert context.parent == parent and context.hourly is None


def test_event_turnover_postclose_deadline_and_effective_timestamp(daily):
    from elliottwaver.schemas.index_context import IndexContext
    from elliottwaver.visualization.brief_text import index_context_sections, market_time_text

    plan = daily.brief.index_guidance.tracks[0].plan
    cutoff = plan.activation_deadline + timedelta(minutes=1)
    zone = ZoneInfo(daily.prepared.instrument.timezone)
    day = cutoff.astimezone(zone).date()
    calendar = load_calendar(daily.prepared.instrument, day, day + timedelta(days=30))
    future = [session.date for session in calendar.sessions if session.open_at > cutoff]
    next_day = future[0]
    known = cutoff - timedelta(days=5)
    effective = cutoff - timedelta(seconds=30)
    dataset = IndexContextDataset(
        symbol="SPX",
        membership=(
            ConstituentVersion(
                symbol="OLD",
                revision=1,
                announced_at=known,
                received_at=known,
                effective_from=day - timedelta(days=2),
                effective_through=next_day - timedelta(days=1),
                weight=1,
            ),
            ConstituentVersion(
                symbol="NEW",
                revision=1,
                announced_at=known,
                received_at=known,
                effective_from=next_day,
                weight=1,
            ),
        ),
        events=(
            EventVersion(
                event_id="old",
                revision=1,
                symbol="OLD",
                kind="EARNINGS",
                announcement_at=known,
                received_at=known,
                expected_at=cutoff + timedelta(days=60),
                effective_at=effective,
                source="fixture",
            ),
            EventVersion(
                event_id="new",
                revision=1,
                symbol="NEW",
                kind="EARNINGS",
                announcement_at=known,
                received_at=known,
                expected_at=calendar.session(next_day).close_at,
                source="fixture",
            ),
        ),
        event_coverage=(
            EventCoverage(
                kind="EARNINGS",
                source="fixture",
                known_at=known,
                start=day,
                end=future[-1],
                symbols=("OLD",),
            ),
        ),
    )
    brief = daily.brief.model_copy(update={"as_of": cutoff})
    windows = describe_events(brief, daily.prepared, dataset, IndexGuidanceConfig())
    short = windows[0]
    assert short.end == future[4]
    assert {event.symbol for event in short.events} == {"OLD", "NEW"}
    assert short.earnings_event_weight == 1 and short.earnings_covered_weight == 0
    assert short.earnings_status == "INSUFFICIENT_COVERAGE"
    context = IndexContext(
        as_of=cutoff, main_snapshot_id=daily.prepared.snapshot_id, events=windows
    )
    rendered = str(index_context_sections(brief.model_copy(update={"index_context": context})))
    assert market_time_text(effective, "SPX") in rendered
    assert market_time_text(dataset.events[0].expected_at, "SPX") not in rendered
