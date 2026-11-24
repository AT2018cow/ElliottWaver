import json
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

from elliottwaver.brief import build_brief
from elliottwaver.cli.main import app
from elliottwaver.config import IndexGuidanceConfig, Settings
from elliottwaver.data.demo import demo_bundle
from elliottwaver.data.snapshots import SnapshotStore
from elliottwaver.evaluation.index_plans import TimedOutcome, competing_risks
from elliottwaver.history import HistoryStore
from elliottwaver.index_guidance import (
    build_index_guidance,
    confirmation_evidence,
    next_index_price,
    observe_plan,
    reconcile_index_guidance,
    size_index_tool,
    target_distance,
)
from elliottwaver.schemas.index_plan import IndexSizingInput
from elliottwaver.schemas.market import AnalysisRequest, Bar, MarketDataBundle
from elliottwaver.schemas.outlook import OutlookCase, OutlookCondition
from elliottwaver.schemas.wave import LevelCondition, PriceZone
from elliottwaver.util import digest
from elliottwaver.visualization.brief_formatter import (
    DataError,
    brief_html,
    brief_markdown,
    publish_brief,
    read_brief_report,
)

TARGET = PriceZone(low=108, high=110, label="目标", methods=("test",), independent_method_count=1)


def condition(brief, kind, side, op, price):
    return LevelCondition(
        kind=kind,
        side=side,
        op=op,
        price=price,
        reason="fixture",
        coordinate_id=brief.price_coordinate_id,
        active_from=brief.as_of,
    )


@pytest.fixture(scope="module")
def origin():
    source = demo_bundle("SPX")
    settings = Settings()
    request = AnalysisRequest(symbol="SPX", as_of=source.as_of, mode="FROZEN_DATA_REPLAY")
    run = build_brief(request, source, settings, include_weekly=False, check_windows=False)
    return run


def projected(origin, policy=None, *, structure="A", close=98.0):
    b = origin.brief
    card = b.cards[0].model_copy(
        update={"structure_key": structure, "next_leg_bias": "UP", "target_zones": (TARGET,)}
    )
    case = OutlookCase(
        scenario_id=card.scenario_id,
        structure_key=structure,
        slot="PRIMARY",
        advance_direction="UP",
        target=TARGET,
        conditions=(
            OutlookCondition(
                role="ADVANCE", condition=condition(b, "CONFIRMATION", "HIGH", ">", 100)
            ),
            OutlookCondition(
                role="INVALIDATION", condition=condition(b, "INVALIDATION", "LOW", "<", 96)
            ),
        ),
    )
    brief = b.model_copy(
        update={
            "cards": (card,),
            "latest_price": close,
            "decision_atr": 4.0,
            "status": "OK",
            "outlook": b.outlook.model_copy(update={"cases": (case,)}),
        }
    )
    guidance = build_index_guidance(
        brief,
        origin.prepared,
        origin.qualified,
        [2.0] * len(origin.prepared.bars),
        policy or IndexGuidanceConfig(),
    )
    assert all(t.plan is not None for t in guidance.tracks), [
        t.observation for t in guidance.tracks
    ]
    brief = brief.model_copy(update={"index_guidance": guidance})
    return brief.model_copy(
        update={
            "brief_digest": digest(
                brief.model_dump(mode="json", exclude={"brief_digest", "generated_at"})
            )
        }
    )


def subsequent(origin, plan, quotes, *, dates=None, as_of=None):
    bars = [*origin.prepared.bars]
    for i, quote in enumerate(quotes):
        if quote is None:
            continue
        low, high, close = quote
        period = plan.periods[i]
        bars.append(
            Bar(
                start_at=period.start_at,
                end_at=period.end_at,
                session_date=period.end_session,
                open=close,
                close=close,
                high=high,
                low=low,
                volume=1,
                observed_at=period.end_at,
            )
        )
    last = bars[-1]
    return MarketDataBundle.model_validate(
        origin.prepared.model_copy(
            update={
                "bars": tuple(bars),
                "raw_bars": tuple(bars),
                "sessions": tuple(
                    sorted(
                        set(origin.prepared.sessions)
                        | {p.end_session for p in plan.periods}
                        | set(dates or [])
                    )
                ),
                "as_of": as_of or last.usable_at,
                "provenance": origin.prepared.provenance.model_copy(
                    update={"basis_date": last.session_date}
                ),
            }
        )
    )


def test_index_and_stock_use_separate_guidance_paths(origin):
    assert origin.brief.index_guidance is not None
    assert origin.brief.schema_version == "1.3"
    assert [t.holding_sessions for t in origin.brief.index_guidance.tracks] == [5, 20]
    assert all(t.plan is None for t in origin.brief.index_guidance.tracks)
    equity = demo_bundle("AAPL")
    b = build_brief(
        AnalysisRequest(symbol="AAPL", as_of=equity.as_of, mode="FROZEN_DATA_REPLAY"),
        equity,
        Settings(),
        include_weekly=False,
        check_windows=False,
    ).brief
    assert b.index_guidance is None
    assert "index_guidance" not in b.model_dump(mode="json")


def test_freeze_and_settle_the_same_two_horizons(origin):
    b = projected(origin)
    short, swing = (t.plan for t in b.index_guidance.tracks)
    assert short.plan_id != swing.plan_id
    assert short.trigger.side == "CLOSE"
    assert short.max_entry_index_price == 102
    assert short.activation_deadline == short.periods[1].end_at
    assert short.periods[0].start_at >= short.issued_at
    q = [(97, 109, 101), (98, 108, 107)]
    trigger, event = observe_plan(short, subsequent(origin, short, q[:1]))
    assert trigger.state == "TRIGGERED"
    assert trigger.activation_price == 101
    assert trigger.activation_target_distance.points == 7
    assert trigger.due_session == short.periods[4].end_session
    assert event[-1].source_bar_id == trigger.activation_bar_id
    for plan in (short, swing):
        obs, transitions = observe_plan(plan, subsequent(origin, short, q))
        assert [t.to_state for t in transitions] == ["TRIGGERED", "TARGET_REACHED"]
        assert obs.exit_session == plan.periods[1].end_session
        assert observe_plan(plan, subsequent(origin, short, q), obs) == (obs, ())


@pytest.mark.parametrize(
    "quotes,expected,reason",
    [
        ([(97, 104, 104), (97, 109, 108)], "CANCELLED", "INDEX_CHASE_LIMIT"),
        ([(95, 103, 101)], "AMBIGUOUS", "SAME_BAR_TRIGGER_AND_EXIT"),
        ([(97, 102, 101), (95, 109, 108)], "AMBIGUOUS", "SAME_BAR_TARGET_AND_EXIT"),
        ([(97, 100, 100), (97, 100, 100)], "EXPIRED", "ACTIVATION_DEADLINE"),
        ([(95, 99, 98)], "INVALIDATED", "HARD_INVALIDATION"),
        ([(97, 102, 101), *([(97, 105, 104)] * 4)], "EXPIRED", "HOLDING_DEADLINE"),
    ],
)
def test_index_outcomes_are_causal(origin, quotes, expected, reason):
    plan = projected(origin).index_guidance.tracks[0].plan
    obs, _ = observe_plan(plan, subsequent(origin, plan, quotes))
    assert (obs.state, obs.reason) == (expected, reason)


def test_missing_session_blocks_future_hits_and_late_last_close_cannot_activate(origin):
    plan = projected(origin).index_guidance.tracks[0].plan
    lost = subsequent(origin, plan, [None, (97, 103, 101), (97, 109, 107)])
    obs, events = observe_plan(plan, lost)
    assert obs.quality == "DATA_PENDING"
    assert not events
    repaired = subsequent(origin, plan, [(97, 100, 98), (97, 103, 101), (97, 109, 107)])
    assert observe_plan(plan, repaired, obs)[0].state == "TARGET_REACHED"

    last = subsequent(origin, plan, [(97, 100, 98), (97, 103, 101)])
    delayed_at = last.bars[-1].usable_at + timedelta(microseconds=1)
    delayed = (*last.bars[:-1], last.bars[-1].model_copy(update={"available_at": delayed_at}))
    last = last.model_copy(update={"bars": delayed, "raw_bars": delayed, "as_of": delayed_at})
    assert observe_plan(plan, last)[0].reason == "ACTIVATION_DEADLINE"


def test_target_distance_signed_and_competing_event_math():
    zone = TARGET.model_copy(update={"low": 104, "high": 106})
    lower = TARGET.model_copy(update={"low": 94, "high": 96})
    assert target_distance(zone, 100, 2, "UP").atr == 2
    assert target_distance(lower, 100, 2, "DOWN").atr == 2
    assert target_distance(zone, 105, 0, "UP").location == "IN_ZONE"
    assert target_distance(zone, 107, None, "UP").points == -3
    complete = [
        TimedOutcome(2, "TARGET"),
        TimedOutcome(3, "INVALIDATION"),
        TimedOutcome(5, "TARGET"),
    ]
    report = competing_risks(complete)["by_session"]
    assert report["2"]["target"] == pytest.approx(1 / 3)
    assert report["5"]["target"] == pytest.approx(2 / 3)
    report = competing_risks([*complete[:2], TimedOutcome(4, "CENSORED")])["by_session"]
    assert report["5"]["target"] == pytest.approx(1 / 3)
    assert report["5"]["at_risk"] == 0
    report = competing_risks([TimedOutcome(2, "UNKNOWN")])["by_session"]
    assert report["2"]["target_ambiguity_range"] == [0, 1]


def test_confirmation_delay_uses_only_visible_same_structure(origin):
    plan = projected(origin).index_guidance.tracks[0].plan
    evidence = plan.confirmation.model_copy(
        update={"confirmation_price": 104, "confirmation_atr": 2}
    )
    assert (
        confirmation_evidence(
            origin.qualified[0],
            origin.prepared.model_copy(
                update={"as_of": evidence.confirmed_at - timedelta(seconds=1)}
            ),
            [2] * len(origin.prepared.bars),
        )
        != plan.confirmation
    )
    bar = subsequent(origin, plan, [(105, 107, 106)]).bars[-1]
    target = TARGET.model_copy(update={"low": 110, "high": 112})
    quote = next_index_price(evidence, bar, bar.bar_id, target)
    assert (quote.moved_points, quote.moved_atr, quote.remaining.atr) == (2, 1, 2)
    assert quote.available_at > evidence.confirmed_at


def next_brief(old, base, source, *, key=None):
    last = source.bars[-1]
    tracks = []
    for track in base.index_guidance.tracks:
        draft = track.plan.model_copy(update={"structure_key": key or track.plan.structure_key})
        tracks.append(track.model_copy(update={"plan": draft}))
    base = base.model_copy(
        update={
            "last_bar_end_at": last.end_at,
            "last_bar_id": last.bar_id,
            "as_of": source.as_of,
            "market_asof": last.session_date,
            "input_snapshot_id": source.snapshot_id,
            "index_guidance": base.index_guidance.model_copy(update={"tracks": tuple(tracks)}),
        }
    )
    comparison = SimpleNamespace(change="SAME_COUNT", data_revision="NONE")
    return base.model_copy(
        update={"index_guidance": reconcile_index_guidance(base, old, source, comparison)}
    )


def test_transient_recount_preserves_frozen_plan_and_invalidates_once(origin):
    a = projected(origin, IndexGuidanceConfig(activation_sessions=5))
    first = a.index_guidance.tracks[0].plan
    quotes = [(97, 99, 98), (97, 99, 98), (97, 99, 98), (95, 99, 98)]
    current = a
    for i, key in enumerate(("B", "A", "A", "A"), 1):
        source = subsequent(origin, first, quotes[:i])
        current = next_brief(current, a, source, key=key)
        track = current.index_guidance.tracks[0]
        assert track.plan.plan_id == first.plan_id
        if i == 1:
            assert track.pending_structure_key == "B"
    assert track.observation.state == "INVALIDATED"
    assert [t.to_state for t in track.transitions] == ["INVALIDATED"]
    assert observe_plan(first, source, track.observation)[1] == ()


def test_sizing_tool_inputs_use_whole_lots_and_preserve_sellable_quantity(origin):
    sizing = IndexSizingInput(
        instrument="TEST.ETF",
        as_of=origin.brief.as_of,
        valid_until=origin.brief.as_of,
        risk_budget=Decimal(1000),
        risk_per_unit=Decimal(2),
        cost_per_unit=Decimal(10),
        cash=Decimal(3200),
        liquidity_limit=250,
        lot_size=100,
        sellable_quantity=50,
    )
    limit = size_index_tool(sizing)
    assert (limit.buy_limit, limit.sell_limit) == (200, 50)
    policy = IndexGuidanceConfig(sizing={"SPX": sizing})
    assert policy.policy_hash == IndexGuidanceConfig().policy_hash


def test_report_history_reminder_and_settle_roundtrip(origin, tmp_path):
    b = projected(origin)
    run = replace(origin, brief=b)
    out = publish_brief(run, tmp_path / "reports", formats=("md",))
    saved, _ = read_brief_report(out)
    assert saved.index_guidance == b.index_guidance
    assert json.loads((out / "manifest.json").read_text())["schema_version"] == "1.4"
    for text in (brief_markdown(b), brief_html(b)):
        assert text.index("单指数预案") < text.index("短期观察")
        assert "5 会话" in text and "20 会话" in text
    cache = tmp_path / "cache"
    history = HistoryStore(cache)
    history.register(b, out)
    rerender = publish_brief(run, tmp_path / "reports", formats=())
    history.register(b, rerender)
    assert len(history.reminders("SPX")) == 2
    identifier = history.reminders("SPX")[0]["transition_id"]
    assert history.acknowledge_reminder("SPX", identifier, mode="FROZEN_DATA_REPLAY")
    assert history.reminders("SPX")[0]["acknowledged_at"]

    plan = b.index_guidance.tracks[0].plan
    source = subsequent(origin, plan, [(97, 105, 101), (98, 108, 107)])
    store = SnapshotStore(cache)
    store.save(origin.prepared)
    sid = store.save(source)
    result = CliRunner().invoke(
        app, ["settle", str(out), "--cache-root", str(cache), "--snapshot", sid, "--json"]
    )
    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    assert len(data["index_plans"]) == 2
    assert {row["observation"]["state"] for row in data["index_plans"]} == {"TARGET_REACHED"}
    assert all(row["daily_decisions_observed"] == 1 for row in data["index_plan_statistics"])

    path = Path(rerender) / "manifest.json"
    manifest = json.loads(path.read_text())
    manifest["mode"] = "LIVE"
    path.write_text(json.dumps(manifest))
    with pytest.raises(DataError, match="BRIEF_REPORT_INVALID"):
        read_brief_report(rerender)


def test_default_wave_engine_can_issue_a_real_synthetic_index_prefix():
    source = demo_bundle("SPX")
    bar = source.bars[186]
    request = AnalysisRequest(
        symbol="SPX",
        end=bar.session_date,
        as_of=bar.usable_at,
        mode="FROZEN_DATA_REPLAY",
    )
    brief = build_brief(
        request, source, Settings(), include_weekly=False, check_windows=False
    ).brief
    assert brief.cards[0].stage == "X_DEVELOPING"
    assert brief.index_guidance.direction == "UP"
    assert all(t.plan.trigger.price == pytest.approx(120.15) for t in brief.index_guidance.tracks)


def test_stable_recount_gets_a_new_revision_on_the_next_complete_session(origin):
    initial = projected(origin, IndexGuidanceConfig(activation_sessions=5))
    plan = initial.index_guidance.tracks[0].plan
    first = subsequent(origin, plan, [(97, 99, 98)])
    second = subsequent(origin, plan, [(97, 99, 98), (97, 99, 98)])
    first_brief = next_brief(initial, initial, first, key="B")
    assert first_brief.index_guidance.tracks[0].plan.plan_id == plan.plan_id
    draft = projected(origin, IndexGuidanceConfig(activation_sessions=5), structure="B")
    # The distinct candidate represents the second day's new frozen evidence.
    tracks = tuple(
        track.model_copy(
            update={"plan": track.plan.model_copy(update={"plan_id": f"new-{track.horizon}"})}
        )
        for track in draft.index_guidance.tracks
    )
    draft = draft.model_copy(
        update={"index_guidance": draft.index_guidance.model_copy(update={"tracks": tracks})}
    )
    second_brief = next_brief(first_brief, draft, second, key="B")
    revised = second_brief.index_guidance.tracks[0]
    assert revised.plan.revision == 2 and revised.plan.parent_plan_id == plan.plan_id
    assert revised.plan.plan_id != plan.plan_id
    assert [t.reason for t in revised.transitions] == ["STRUCTURE_REPLACED", "PLAN_ISSUED"]


def test_revised_signal_cannot_change_a_previously_seen_trigger(origin):
    b = projected(origin)
    plan = b.index_guidance.tracks[0].plan
    finished = subsequent(origin, plan, [(98, 104, 101)])
    prior, _ = observe_plan(plan, finished)
    altered = finished.raw_bars[-2].model_copy(update={"source_revision": "changed"})
    raw = (*finished.raw_bars[:-2], altered, finished.raw_bars[-1])
    updated = finished.model_copy(update={"raw_bars": raw})
    obs, transitions = observe_plan(plan, updated, prior)
    assert obs.quality == "DATA_REVISED"
    assert obs.state == "TRIGGERED" and obs.activation_bar_id == prior.activation_bar_id
    assert not transitions


def test_developing_research_pairs_first_visible_evidence_with_later_confirmation(origin):
    from elliottwaver.evaluation.index_plans import summarize_developing_evidence
    from elliottwaver.schemas.index_plan import DevelopingEvidence

    b = projected(origin)
    plan = b.index_guidance.tracks[0].plan
    source = subsequent(origin, plan, [(97, 100, 99), (100, 108, 104), (101, 111, 106)])
    preview = DevelopingEvidence(
        pivot_id="known-pivot",
        kind="LOW",
        extreme_at=b.as_of,
        extreme_price=96,
        first_seen_at=b.as_of,
        reference_price=99,
        target=TARGET,
        early_eligible=True,
        comparable_entry_ceiling=102,
        hard_boundary=plan.invalidations[0],
    )
    first = b.model_copy(
        update={
            "index_guidance": b.index_guidance.model_copy(
                update={"developing": preview, "confirmation": None}
            )
        }
    )
    bar = source.bars[-1]
    conf = plan.confirmation.model_copy(
        update={"pivot_id": "known-pivot", "confirmed_at": bar.usable_at, "confirmation_price": 106}
    )
    after = b.model_copy(
        update={
            "as_of": bar.usable_at,
            "last_bar_end_at": bar.end_at,
            "index_guidance": b.index_guidance.model_copy(
                update={"confirmation": conf, "developing": None}
            ),
        }
    )
    (report,) = summarize_developing_evidence([(after, source), (first, source), (first, source)])
    (pair,) = report["paired"]
    assert report["counts"]["early_observations"] == 1
    assert pair["confirmed_price_minus_preview_points"] == 7
    assert pair["target_observed_during_wait"] is True
    assert pair["within_preview_limit"] is False


@pytest.mark.parametrize(
    "gap,reason",
    [(2.99, "NEAR_TIE"), (3.0, "NEAR_TIE"), (3.01, "WAIT_FOR_CLOSE")],
)
def test_close_opposing_cards_at_the_frozen_score_boundary(origin, gap, reason):
    brief = projected(origin)
    first = brief.cards[0].model_copy(update={"quality_score": 60, "coverage": 0.85})
    alternative = first.model_copy(
        update={
            "next_leg_bias": "DOWN",
            "role": "ALTERNATE",
            "quality_score": 60 - gap,
            "structure_key": "other",
            "scenario_id": "other",
        }
    )
    draft = brief.model_copy(update={"cards": (first, alternative)})
    guidance = build_index_guidance(
        draft,
        origin.prepared,
        origin.qualified,
        [2.0] * len(origin.prepared.bars),
        IndexGuidanceConfig(),
    )
    assert guidance.tracks[0].observation.reason == reason


def test_late_prefix_evidence_moves_confirmation_to_its_actual_availability(origin):
    brief = projected(origin)
    plan = brief.index_guidance.tracks[0].plan
    selected = next(s for s in origin.qualified if s.scenario_id == plan.scenario_id)
    earlier = origin.prepared.raw_bars[0]
    delayed_at = plan.confirmation.confirmed_at + timedelta(microseconds=1)
    raw = (earlier.model_copy(update={"available_at": delayed_at}), *origin.prepared.raw_bars[1:])
    altered = origin.prepared.model_copy(update={"raw_bars": raw})
    confirmation = confirmation_evidence(selected, altered, [2.0] * len(altered.bars))
    assert confirmation.engine_confirmed_at == plan.confirmation.confirmed_at
    assert confirmation.confirmed_at == delayed_at
    assert (
        confirmation.extreme_to_confirmation_seconds
        == (delayed_at - confirmation.extreme_at).total_seconds()
    )


def test_parallel_reminder_registration_and_file_write_retry(origin, tmp_path, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor

    import elliottwaver.visualization.brief_formatter as formatter

    b = projected(origin)
    run = replace(origin, brief=b)
    actual = formatter.atomic_write

    def broken(path, content):
        if path.name == "manifest.json":
            raise OSError("injected disk failure")
        return actual(path, content)

    monkeypatch.setattr(formatter, "atomic_write", broken)
    with pytest.raises(formatter.ExportError):
        publish_brief(run, tmp_path / "reports", formats=())
    assert not list((tmp_path / "reports").rglob("brief.json"))
    monkeypatch.setattr(formatter, "atomic_write", actual)
    journal = HistoryStore(tmp_path / "cache")

    def record(formats):
        path = publish_brief(run, tmp_path / "reports", formats=formats)
        journal.register(b, path)

    with ThreadPoolExecutor(max_workers=3) as pool:
        list(pool.map(record, [(), ("md",), ()]))
    reminders = journal.reminders("SPX")
    assert len(reminders) == 2
    assert len({entry["transition_id"] for entry in reminders}) == 2


def test_native_index_refresh_keeps_frozen_observation_basis(origin):
    from elliottwaver.evaluation.index_plans import settle_index_track

    draft = projected(origin).index_guidance.tracks[0]
    track = draft.model_copy(update={"plan": draft.plan.model_copy(update={"adjustment": "none"})})
    b = subsequent(origin, track.plan, [(97, 103, 101)])
    b = b.model_copy(
        update={
            "provenance": b.provenance.model_copy(
                update={
                    "adjustment": "none",
                    "price_coordinate_id": "next-snapshot-native-points",
                }
            )
        }
    )
    assert observe_plan(track.plan, b)[0].state == "TRIGGERED"
    assert settle_index_track(track, b)["observation"]["state"] == "TRIGGERED"
    wrong = b.model_copy(update={"provenance": b.provenance.model_copy(update={"feed": "other"})})
    assert observe_plan(track.plan, wrong)[0].quality == "NOT_COMPARABLE"


def test_activation_uses_complete_prefix_availability(origin):
    plan = projected(origin).index_guidance.tracks[0].plan
    source = subsequent(origin, plan, [(97, 100, 98), (97, 103, 101)])
    late_at = plan.activation_deadline + timedelta(seconds=10)
    late_raw = source.raw_bars[-2].model_copy(update={"available_at": late_at})
    raw = (*source.raw_bars[:-2], late_raw, source.raw_bars[-1])
    source = source.model_copy(update={"raw_bars": raw, "as_of": late_at})
    obs, transitions = observe_plan(plan, source)
    assert obs.reason == "ACTIVATION_DEADLINE"
    assert obs.activation_at is None
    assert transitions[-1].observable_at == late_at


def test_confirmation_identity_disambiguates_equal_availability(origin):
    plan = projected(origin).index_guidance.tracks[0].plan
    scenario = next(row for row in origin.qualified if row.scenario_id == plan.scenario_id)
    original = plan.confirmation
    index = next(
        i
        for i, bar in enumerate(origin.prepared.raw_bars)
        if bar.bar_id == original.confirmation_bar_id
    )
    altered = origin.prepared.bars[index - 1].model_copy(
        update={"available_at": original.engine_confirmed_at}
    )
    bars = (*origin.prepared.bars[: index - 1], altered, *origin.prepared.bars[index:])
    source = origin.prepared.model_copy(update={"bars": bars})
    actual = confirmation_evidence(scenario, source, [2.0] * len(bars))
    assert (actual.confirmation_price, actual.confirmation_bar_id) == (
        original.confirmation_price,
        original.confirmation_bar_id,
    )


def test_settlement_refuses_older_snapshot_and_bounds_actual_touches(origin):
    from elliottwaver.evaluation.index_plans import settle_index_track, summarize_index_plans

    track = projected(origin).index_guidance.tracks[0]
    activated = subsequent(origin, track.plan, [(97, 103, 101)])
    reached = subsequent(origin, track.plan, [(97, 103, 101), (97, 109, 108)])
    obs, changes = observe_plan(track.plan, reached)
    assert obs.state == "TARGET_REACHED"
    completed = track.model_copy(update={"observation": obs, "transitions": changes})
    with pytest.raises(DataError, match="INDEX_SETTLEMENT_OBSERVATION_UNCOVERED"):
        settle_index_track(completed, activated)
    trigger_collision = subsequent(origin, track.plan, [(95, 103, 101)])
    target_collision = subsequent(origin, track.plan, [(97, 103, 101), (95, 109, 108)])
    for source, upper in ((trigger_collision, 0.0), (target_collision, 1.0)):
        outcome = settle_index_track(track, source)
        (group,) = summarize_index_plans([outcome])
        assert group["from_issuance"]["by_session"]["2"]["target_ambiguity_range"] == [0.0, upper]
