from datetime import timedelta

from eliotbot.brief import build_brief, compare_decisions
from eliotbot.config import Settings
from eliotbot.data.demo import demo_bundle
from eliotbot.schemas.market import AnalysisRequest
from eliotbot.schemas.wave import LevelCondition, WaveStructure


def _run(*, advanced=True):
    bundle = demo_bundle("AAPL")
    decision = bundle.bars[-30]
    run = build_brief(
        AnalysisRequest(
            symbol="AAPL",
            as_of=decision.usable_at,
            end=decision.session_date,
        ),
        bundle,
        Settings(analysis={"engine_profile": "hierarchical_v1"}) if advanced else Settings(),
        include_weekly=False,
        check_windows=False,
    )
    return bundle, decision, run


def _with_condition(run, condition):
    card = run.brief.cards[0].model_copy(update={"conditions": (condition,)})
    brief = run.brief.model_copy(update={"cards": (card, *run.brief.cards[1:])})
    return brief


def test_tip_moved_is_reported_without_changing_confirmed_structure():
    bundle, _decision, run = _run()
    old = run.result.scenarios[0]
    tip = old.developing_tip.model_copy(
        update={
            "extreme_at": old.developing_tip.extreme_at + timedelta(days=1),
            "price": old.developing_tip.price + 0.1,
        }
    )
    current_result = run.result.model_copy(
        update={"scenarios": (old.model_copy(update={"developing_tip": tip}),)}
    )
    comparison = compare_decisions(
        run.brief,
        run.brief,
        previous_result=run.result,
        current_result=current_result,
        previous_source=bundle,
        current_source=bundle,
    )
    assert comparison.change == "TIP_MOVED"
    assert comparison.structure_change == "TIP_MOVED"
    assert comparison.events[0]["first_difference"] == "developing_tip"


def test_evidence_changed_is_reported_for_same_structure_key():
    bundle, _decision, run = _run()
    previous_card = run.brief.cards[0].model_copy(update={"subwave_status": "UNAVAILABLE"})
    current_card = previous_card.model_copy(update={"subwave_status": "VERIFIED"})
    previous = run.brief.model_copy(update={"cards": (previous_card,)})
    current = run.brief.model_copy(update={"cards": (current_card,)})
    comparison = compare_decisions(previous, current, previous_source=bundle, current_source=bundle)
    assert comparison.change == "EVIDENCE_CHANGED"
    assert comparison.events[0]["previous_status"] == "UNAVAILABLE"
    assert comparison.events[0]["current_status"] == "VERIFIED"


def test_history_reports_verified_depth_gain_at_the_same_subwave_status():
    bundle, _decision, run = _run()
    card = run.brief.cards[0]
    tree = WaveStructure(
        root_id="fixture",
        status="PENDING",
        requested_depth=2,
        verified_depth=0,
        nodes=(),
    )
    previous_card = card.model_copy(update={"structure": tree, "subwave_status": "UNVERIFIED"})
    current_card = previous_card.model_copy(
        update={"structure": tree.model_copy(update={"verified_depth": 1})}
    )
    previous = run.brief.model_copy(update={"cards": (previous_card,)})
    current = run.brief.model_copy(update={"cards": (current_card,)})
    comparison = compare_decisions(previous, current, previous_source=bundle, current_source=bundle)
    assert comparison.change == "EVIDENCE_CHANGED"
    assert comparison.events[0]["previous_verified_depth"] == 0
    assert comparison.events[0]["current_verified_depth"] == 1


def test_search_limited_is_reported_as_an_input_independent_change():
    bundle, _decision, run = _run()
    current = run.brief.model_copy(update={"status": "SEARCH_LIMITED"})
    comparison = compare_decisions(
        run.brief, current, previous_source=bundle, current_source=bundle
    )
    assert comparison.change == "SEARCH_LIMITED"
    assert "预算截断" in comparison.reason


def test_hard_invalidation_replays_the_condition_and_bar():
    bundle, decision, run = _run()
    decision_index = bundle.bars.index(decision)
    index = decision_index + 10
    future = bundle.bars[index]
    price = min(bar.low for bar in bundle.bars[decision_index + 1 : index]) - 0.01
    condition = LevelCondition(
        kind="INVALIDATION",
        side="LOW",
        op="<=",
        price=price,
        active_from=decision.usable_at,
        reason="fixture invalidation",
        coordinate_id=run.brief.price_coordinate_id,
    )
    previous = _with_condition(run, condition)
    modified = future.model_copy(
        update={"low": price - 1, "high": future.high, "close": future.close}
    )
    current_source = bundle.model_copy(
        update={
            "bars": (*bundle.bars[:index], modified, *bundle.bars[index + 1 :]),
            "raw_bars": (*bundle.raw_bars[:index], modified, *bundle.raw_bars[index + 1 :]),
        }
    )
    current = previous.model_copy(
        update={"market_asof": future.session_date, "as_of": future.usable_at}
    )
    comparison = compare_decisions(
        previous,
        current,
        previous_result=run.result,
        current_result=run.result,
        previous_source=bundle,
        current_source=current_source,
    )
    assert comparison.change == "HARD_INVALIDATED"
    assert comparison.triggered_conditions == (condition,)
    condition_event = next(event for event in comparison.events if event["type"] == "condition")
    assert condition_event["kind"] == "INVALIDATION"
    assert condition_event["session"] == future.session_date.isoformat()
    assert condition_event["bar_id"] == modified.bar_id


def test_history_does_not_replay_a_condition_before_activation():
    bundle, decision, run = _run()
    bar = bundle.bars[bundle.bars.index(decision) + 1]
    condition = LevelCondition(
        kind="INVALIDATION",
        side="LOW",
        op="<=",
        price=bar.low,
        active_from=bar.usable_at + timedelta(days=1),
        reason="later activation",
        coordinate_id=run.brief.price_coordinate_id,
    )
    previous = _with_condition(run, condition)
    current = previous.model_copy(update={"market_asof": bar.session_date, "as_of": bar.usable_at})
    comparison = compare_decisions(previous, current, previous_source=bundle, current_source=bundle)
    assert comparison.change != "HARD_INVALIDATED"
    assert not comparison.triggered_conditions


def test_history_reports_a_later_touch_without_asserting_first_trigger_after_gap():
    bundle, decision, run = _run()
    i = bundle.bars.index(decision) + 1
    later = bundle.bars[i + 1]
    missing = bundle.model_copy(
        update={
            "bars": bundle.bars[:i] + bundle.bars[i + 1 :],
            "raw_bars": bundle.raw_bars[:i] + bundle.raw_bars[i + 1 :],
        }
    )
    condition = LevelCondition(
        kind="INVALIDATION",
        side="LOW",
        op="<=",
        price=later.low,
        active_from=decision.usable_at,
        reason="gap before observed touch",
        coordinate_id=run.brief.price_coordinate_id,
    )
    previous = _with_condition(run, condition)
    current = previous.model_copy(
        update={"market_asof": later.session_date, "as_of": later.usable_at}
    )
    comparison = compare_decisions(
        previous, current, previous_source=bundle, current_source=missing
    )
    assert not comparison.triggered_conditions
    observed = next(event for event in comparison.events if event["type"] == "condition")
    assert observed["status"] == "OBSERVED_AFTER_GAP"
    assert observed["session"] == later.session_date.isoformat()


def test_split_replay_keeps_the_frozen_bar_id_and_issuance_price():
    bundle, decision, run = _run()
    bar = bundle.bars[bundle.bars.index(decision) + 1]
    old = bundle.model_copy(
        update={
            "provenance": bundle.provenance.model_copy(
                update={
                    "adjustment": "provider_all",
                    "price_coordinate_id": "issued",
                }
            )
        }
    )
    revised_bars = tuple(
        row.model_copy(
            update={field: getattr(row, field) / 2 for field in ("open", "high", "low", "close")}
        )
        for row in bundle.bars
    )
    revised = old.model_copy(
        update={
            "bars": revised_bars,
            "provenance": old.provenance.model_copy(update={"price_coordinate_id": "split"}),
        }
    )
    level = LevelCondition(
        kind="INVALIDATION",
        side="LOW",
        op="<=",
        price=bar.low,
        active_from=decision.usable_at,
        coordinate_id="issued",
        reason="fixture invalidation",
    )
    previous = _with_condition(run, level).model_copy(
        update={
            "adjustment": "provider_all",
            "price_coordinate_id": "issued",
        }
    )
    current = previous.model_copy(
        update={
            "market_asof": bar.session_date,
            "as_of": bar.usable_at,
            "price_coordinate_id": "split",
        }
    )
    comparison = compare_decisions(previous, current, previous_source=old, current_source=revised)
    event = next(row for row in comparison.events if row["type"] == "condition")
    frozen_bar = revised_bars[bundle.bars.index(bar)]
    assert event["bar_id"] == frozen_bar.bar_id != bar.bar_id
    assert event["observed"] == 2 * frozen_bar.low == bar.low
    assert event["coordinate_id"] == "issued"
