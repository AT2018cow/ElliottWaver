"""Project daily wave evidence into a frozen single-index plan and observe its lifecycle."""

from __future__ import annotations

import math
from collections.abc import Sequence
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

from elliottwaver.config import IndexGuidanceConfig
from elliottwaver.data.calendar import load_calendar
from elliottwaver.data.periods import available_pairs, bar_available
from elliottwaver.errors import DataError
from elliottwaver.evaluation.events import condition_hit
from elliottwaver.schemas.brief import Brief
from elliottwaver.schemas.index_plan import (
    ConfirmationEvidence,
    DevelopingEvidence,
    IndexGuidance,
    IndexPlan,
    IndexSizing,
    IndexSizingInput,
    IndexTrack,
    IntradayPlanPolicy,
    NextIndexPrice,
    PlanObservation,
    PlanState,
    PlanTransition,
    TargetDistance,
)
from elliottwaver.schemas.market import Bar, MarketDataBundle
from elliottwaver.schemas.outlook import OutlookPeriod
from elliottwaver.schemas.wave import PriceZone, WaveScenario
from elliottwaver.util import digest

if TYPE_CHECKING:
    from elliottwaver.brief import DecisionComparison


TERMINAL_STATES = {
    "TARGET_REACHED",
    "INVALIDATED",
    "STAGE_EXIT",
    "EXPIRED",
    "CANCELLED",
    "AMBIGUOUS",
}
WAIT_STATES = {"WAITING", "NEAR"}


def target_distance(
    zone: PriceZone, price: float, atr: float | None, direction: str
) -> TargetDistance:
    side = 1 if direction == "UP" else -1
    boundary = zone.low if side == 1 else zone.high
    points = side * (boundary - price)
    location = "IN_ZONE" if zone.low <= price <= zone.high else "AHEAD" if points > 0 else "PASSED"
    return TargetDistance(
        zone=zone,
        boundary=boundary,
        points=points,
        atr=points / atr if atr and math.isfinite(atr) else None,
        location=location,
    )


def size_index_tool(inputs: IndexSizingInput) -> IndexSizing:
    count = min(
        inputs.risk_budget // inputs.risk_per_unit,
        inputs.cash // inputs.cost_per_unit,
        Decimal(inputs.liquidity_limit),
    )
    return IndexSizing(
        inputs=inputs,
        buy_limit=(int(count) // inputs.lot_size) * inputs.lot_size,
        sell_limit=inputs.sellable_quantity,
    )


def confirmation_evidence(
    scenario: WaveScenario, bundle: MarketDataBundle, atr: Sequence[float]
) -> ConfirmationEvidence | None:
    # A developing endpoint can be displayed, but cannot anchor an issued plan.
    pivots = [
        wave.pivot
        for wave in scenario.completed_waves
        if wave.pivot.state == "CONFIRMED"
        and wave.pivot.confirmed_at is not None
        and wave.pivot.confirmed_at <= bundle.as_of
    ]
    if not pivots:
        return None
    pivot = max(pivots, key=lambda item: item.extreme_at)
    matches = [
        (index, raw)
        for index, (bar, raw) in enumerate(zip(bundle.bars, bundle.raw_bars, strict=True))
        if index > pivot.index
        and (
            bar.bar_id == pivot.confirmed_bar_id
            if pivot.confirmed_bar_id
            else bar.usable_at == pivot.confirmed_at
        )
    ]
    if len(matches) == 1:
        index, raw = matches[0]
        bar = bundle.bars[index]
        value = float(atr[index])
        visible_at = max(
            max(prior.usable_at, raw_prior.usable_at)
            for prior, raw_prior in zip(
                bundle.bars[: index + 1],
                bundle.raw_bars[: index + 1],
                strict=True,
            )
        )
        if visible_at > bundle.as_of:
            return None
        return ConfirmationEvidence(
            pivot_id=pivot.pivot_id,
            extreme_at=pivot.extreme_at,
            extreme_price=pivot.price,
            confirmed_at=visible_at,
            engine_confirmed_at=pivot.confirmed_at,
            confirmation_bar_id=raw.bar_id,
            confirmation_price=bar.close,
            confirmation_atr=value if math.isfinite(value) and value > 0 else None,
            extreme_to_confirmation_seconds=(visible_at - pivot.extreme_at).total_seconds(),
        )
    return None


def next_index_price(
    evidence: ConfirmationEvidence, bar: Bar, raw_id: str, target: PriceZone
) -> NextIndexPrice:
    moved = bar.close - evidence.confirmation_price
    return NextIndexPrice(
        bar_id=raw_id,
        available_at=bar.usable_at,
        price=bar.close,
        confirmation_to_price_seconds=(bar.usable_at - evidence.confirmed_at).total_seconds(),
        moved_points=moved,
        moved_atr=moved / evidence.confirmation_atr if evidence.confirmation_atr else None,
        remaining=target_distance(target, bar.close, evidence.confirmation_atr, "UP"),
    )


def _schedule(
    bundle: MarketDataBundle, issued_at: datetime, horizon: int
) -> tuple[tuple[OutlookPeriod, ...], str]:
    local_day = issued_at.astimezone(ZoneInfo(bundle.instrument.timezone)).date()
    signal_day = bundle.bars[-1].session_date
    start = max(signal_day, local_day)
    for extension in (75, 50, 30, 15):
        try:
            calendar = load_calendar(bundle.instrument, start, start + timedelta(days=extension))
        except DataError:
            continue
        periods = tuple(
            OutlookPeriod(
                start_at=session.open_at,
                end_at=session.close_at,
                end_session=session.date,
                sessions=(session.date,),
            )
            for session in calendar.sessions
            if session.date > signal_day and session.open_at >= issued_at
        )[:horizon]
        if len(periods) >= horizon:
            return periods, calendar.version
    return (), bundle.provenance.calendar_version


def _market_gate(brief: Brief, policy: IndexGuidanceConfig) -> str | None:
    if brief.status != "OK" or any(
        issue.code in {"UNKNOWN_SESSION_GAPS", "ALLOWED_GAPS", "STALE_SNAPSHOT"}
        for issue in brief.issues
    ):
        return "DATA_PENDING" if brief.cards or brief.status != "UNCLEAR" else "NO_QUALIFIED_CARD"
    if not brief.cards:
        return "NO_QUALIFIED_CARD"
    if brief.weekly_state == "INSUFFICIENT_DATA":
        return "BACKGROUND_MISSING"
    if brief.cross_timeframe and brief.cross_timeframe.state == "CONFLICT":
        return "BACKGROUND_CONFLICT"
    if brief.window_comparison and brief.window_comparison.state == "CHANGED_BIAS":
        return "WINDOW_CONFLICT"
    if len(brief.cards) > 1:
        first, second = brief.cards[:2]
        gap = Decimal(str(first.quality_score)) - Decimal(str(second.quality_score))
        if (
            first.next_leg_bias != second.next_leg_bias
            and abs(gap) <= Decimal(str(policy.near_tie_score))
            and min(first.coverage, second.coverage) >= policy.near_tie_min_coverage
        ):
            return "NEAR_TIE"
    return None


def _initial_transition(plan: IndexPlan, state: PlanState) -> PlanTransition:
    return _transition(
        plan,
        None,
        state,
        "PLAN_ISSUED",
        plan.periods[0],
        plan.issued_at,
        plan.signal_bar_id,
        session=plan.decision_session,
    )


def build_index_guidance(
    brief: Brief,
    prepared: MarketDataBundle,
    scenarios: Sequence[WaveScenario],
    atr: Sequence[float],
    policy: IndexGuidanceConfig,
    *,
    projected_zones: tuple[PriceZone, ...] | None = None,
) -> IndexGuidance:
    primary = brief.cards[0] if brief.cards else None
    case = (
        next((item for item in brief.outlook.cases if item.slot == "PRIMARY"), None)
        if brief.outlook
        else None
    )
    direction = (
        case.advance_direction
        if case and case.advance_direction != "UNKNOWN"
        else primary.next_leg_bias
        if primary
        else "UNKNOWN"
    )
    scenario = next(
        (item for item in scenarios if primary and item.scenario_id == primary.scenario_id), None
    )
    issue = brief.outlook.issued_at if brief.outlook and brief.outlook.issued_at else brief.as_of
    current_atr = brief.decision_atr
    zones = tuple(primary.target_zones) if primary else ()
    if case and case.target and case.target not in zones:
        zones += (case.target,)
    distances = (
        tuple(
            sorted(
                (
                    target_distance(zone, brief.latest_price, current_atr, direction)
                    for zone in zones
                ),
                key=lambda d: (d.location == "PASSED", abs(d.points), d.zone.low, d.zone.high),
            )
        )
        if direction != "UNKNOWN"
        else ()
    )
    reason = _market_gate(brief, policy)
    confirmation = confirmation_evidence(scenario, prepared, atr) if scenario else None
    advance = ()
    invalidations = ()
    stage_exits = ()
    if case and primary:
        advance = tuple(
            item.condition
            for item in case.conditions
            if item.role == "ADVANCE" and item.condition.op in {">", ">="}
        )
        invalidations = tuple(
            item.condition for item in case.conditions if item.role == "INVALIDATION"
        )
        stage_exits = tuple(item.condition for item in case.conditions if item.role == "STAGE_EXIT")
    trigger = max(advance, key=lambda c: (c.price, c.op == ">"), default=None)
    targets = ()
    ceiling = None
    if trigger:
        trigger = trigger.model_copy(update={"side": "CLOSE"})
        eligible_zones = zones if projected_zones is None else projected_zones
        targets = tuple(
            sorted(
                (z for z in eligible_zones if z.low > trigger.price), key=lambda z: (z.low, z.high)
            )
        )
        if targets and current_atr:
            ceiling = min(
                trigger.price + policy.max_chase_atr * current_atr,
                targets[0].low - prepared.instrument.tick_size,
            )
    if not reason:
        if case and any(event.reason == "STOP_ALREADY_HIT" for event in case.events):
            reason = "STOP_ALREADY_HIT"
        elif direction != "UP":
            reason = "DIRECTION_NOT_LONG"
        elif not trigger:
            reason = "NO_ADVANCE_GATE"
        elif not (
            invalidations
            and all(c.op in {"<", "<="} and c.price < trigger.price for c in invalidations)
        ):
            reason = "NO_VALID_INVALIDATION"
        elif not targets:
            reason = "NO_UPSIDE_TARGET"
        elif not confirmation:
            reason = "UNCONFIRMED_PIVOT"
        elif ceiling is None or ceiling <= trigger.price:
            reason = "NO_ENTRY_RANGE"
        elif brief.latest_price >= targets[0].low:
            reason = "TARGET_ALREADY_REACHED"
    periods, calendar_id = (), prepared.provenance.calendar_version
    if not reason:
        periods, calendar_id = _schedule(
            prepared, issue, policy.activation_sessions + policy.swing_sessions - 1
        )
    sizing = policy.sizing.get(brief.symbol)
    sizing_ready = sizing is not None and sizing.as_of <= brief.as_of <= sizing.valid_until
    tracks = []
    for horizon, holding in (("SHORT", policy.short_sessions), ("SWING", policy.swing_sessions)):
        length = policy.activation_sessions + holding - 1
        if reason or len(periods) < length:
            missing_reason = reason or "CALENDAR_UNAVAILABLE"
            tracks.append(
                IndexTrack(
                    horizon=horizon,
                    holding_sessions=holding,
                    observation=PlanObservation(
                        reason=missing_reason,
                        quality="DATA_PENDING"
                        if missing_reason in {"DATA_PENDING", "CALENDAR_UNAVAILABLE"}
                        else "OK",
                    ),
                )
            )
            continue
        intraday_policy = IntradayPlanPolicy(
            method=policy.intraday_trigger,
            retest_tolerance_points=policy.retest_tolerance_points,
            max_age_minutes=policy.intraday_max_age_minutes,
            background_max_age_minutes=policy.background_max_age_minutes,
        )
        plan = IndexPlan(
            plan_id=digest(
                [
                    brief.symbol,
                    brief.mode,
                    brief.feed,
                    brief.adjustment,
                    brief.price_coordinate_id,
                    horizon,
                    policy.policy_hash,
                    primary.structure_key,
                    brief.last_bar_id,
                    periods[0].start_at,
                    brief.as_of,
                    brief.input_snapshot_id,
                    intraday_policy,
                ]
            )[:24],
            revision=1,
            symbol=brief.symbol,
            mode=brief.mode,
            horizon=horizon,
            holding_sessions=holding,
            structure_key=primary.structure_key,
            scenario_id=primary.scenario_id,
            policy_hash=policy.policy_hash,
            engine_version=brief.engine_version,
            analysis_config_hash=brief.config_hash,
            decision_at=brief.as_of,
            issued_at=issue,
            decision_session=brief.market_asof,
            source_snapshot_id=brief.source_snapshot_id,
            input_snapshot_id=brief.input_snapshot_id,
            signal_bar_id=prepared.raw_bars[-1].bar_id,
            feed=brief.feed,
            adjustment=brief.adjustment,
            index_coordinate_id=brief.price_coordinate_id,
            calendar_version=calendar_id,
            periods=periods[:length],
            activation_sessions=policy.activation_sessions,
            activation_deadline=periods[policy.activation_sessions - 1].end_at,
            trigger=trigger,
            invalidations=invalidations,
            stage_exits=stage_exits,
            targets=targets,
            reference_price=brief.latest_price,
            atr=current_atr,
            max_chase_atr=policy.max_chase_atr,
            near_atr=policy.near_atr,
            max_entry_index_price=ceiling,
            confirmation=confirmation,
            intraday_policy=intraday_policy,
        )
        state = _wait_state(plan, brief.latest_price)
        tracks.append(
            IndexTrack(
                horizon=horizon,
                holding_sessions=holding,
                plan=plan,
                observation=PlanObservation(
                    state=state,
                    next_index_price=next(
                        (
                            next_index_price(confirmation, bar, raw.bar_id, targets[0])
                            for bar, raw in zip(prepared.bars, prepared.raw_bars, strict=True)
                            if bar.usable_at > confirmation.confirmed_at
                            and bar.end_at > confirmation.confirmed_at
                        ),
                        None,
                    ),
                ),
                transitions=(_initial_transition(plan, state),),
            )
        )
    developing = (
        scenario.developing_tip
        if scenario and scenario.developing_tip.state == "DEVELOPING"
        else None
    )
    return IndexGuidance(
        policy_hash=policy.policy_hash,
        direction=direction,
        structure_key=primary.structure_key if primary else None,
        last_price=brief.latest_price,
        last_price_age_seconds=max(0, (brief.as_of - prepared.bars[-1].end_at).total_seconds()),
        target_distances=distances,
        tracks=tuple(_with_distances(track, brief.latest_price) for track in tracks),
        developing=DevelopingEvidence(
            pivot_id=developing.pivot_id,
            kind=developing.kind,
            extreme_at=developing.extreme_at,
            extreme_price=developing.price,
            first_seen_at=issue,
            reference_price=brief.latest_price,
            target=targets[0] if targets else None,
            early_eligible=(
                developing.kind == "LOW"
                and direction == "UP"
                and reason in {None, "UNCONFIRMED_PIVOT"}
                and ceiling is not None
                and brief.latest_price <= ceiling < targets[0].low
            ),
            comparable_entry_ceiling=ceiling,
            hard_boundary=max(invalidations, key=lambda item: item.price, default=None),
        )
        if developing
        else None,
        confirmation=confirmation,
        sizing=size_index_tool(sizing) if sizing_ready else None,
        tool_status=(
            "SIZING_READY"
            if sizing_ready
            else "SIZING_INPUT_OUTSIDE_WINDOW"
            if sizing is not None
            else "NOT_CONFIGURED"
        ),
    )


def _with_distances(track: IndexTrack, price: float) -> IndexTrack:
    if track.plan is None:
        return track
    plan = track.plan
    gap = plan.trigger.price - price
    return track.model_copy(
        update={
            "trigger_distance_points": gap,
            "trigger_distance_atr": gap / plan.atr,
            "current_targets": tuple(
                target_distance(z, price, plan.atr, "UP") for z in plan.targets
            ),
        }
    )


def _wait_state(plan: IndexPlan, price: float) -> PlanState:
    return "NEAR" if abs(plan.trigger.price - price) <= plan.atr * plan.near_atr else "WAITING"


def _transition(
    plan: IndexPlan,
    before: PlanState | None,
    after: PlanState,
    reason: str,
    period: OutlookPeriod,
    observed_at: datetime,
    source_id: str | None,
    *,
    session: date | None = None,
    effective_at: datetime | None = None,
) -> PlanTransition:
    session = session or period.end_session
    return PlanTransition(
        transition_id=digest(
            [plan.mode, plan.plan_id, plan.revision, before, after, reason, session, source_id]
        )[:24],
        plan_id=plan.plan_id,
        revision=plan.revision,
        horizon=plan.horizon,
        from_state=before,
        to_state=after,
        reason=reason,
        session=session,
        effective_at=effective_at or min(period.end_at, observed_at),
        observable_at=observed_at,
        source_bar_id=source_id,
        coordinate_id=plan.index_coordinate_id,
    )


def compatible_plan_input(plan: IndexPlan, bundle: MarketDataBundle) -> bool:
    return (
        bundle.timeframe == "1d"
        and bundle.instrument.canonical_symbol == plan.symbol
        and bundle.provenance.feed == plan.feed
        and bundle.provenance.adjustment == plan.adjustment
        and (
            bundle.provenance.price_coordinate_id == plan.index_coordinate_id
            or (bundle.instrument.asset_type == "INDEX" and plan.adjustment == "none")
        )
    )


def plan_bar_pairs(bundle: MarketDataBundle) -> dict[date, tuple[Bar, Bar]]:
    return {
        bar.session_date: (bar, raw)
        for bar, raw in available_pairs(bundle.bars, bundle.raw_bars, bundle.as_of)
    }


def plan_prefix_evidence(
    plan: IndexPlan,
    through: date,
    pairs: dict[date, tuple[Bar, Bar]],
    suspensions: set[date],
) -> str:
    evidence = []
    for period in plan.periods:
        day = period.end_session
        if day > through:
            break
        if pair := pairs.get(day):
            bar, raw = pair
            # Preparation can raise the analysis bar's availability to match the raw bar.
            if bar.usable_at < raw.usable_at:
                bar = bar.model_copy(update={"available_at": raw.usable_at})
            evidence.append((day, bar.bar_id, raw.bar_id))
        else:
            evidence.append((day, "SUSPENDED" if day in suspensions else "MISSING"))
    return digest(evidence)


def observe_plan(
    plan: IndexPlan,
    bundle: MarketDataBundle,
    prior: PlanObservation | None = None,
    *,
    receipt_cutoff: datetime | None = None,
) -> tuple[PlanObservation, tuple[PlanTransition, ...]]:
    obs = prior or PlanObservation()
    if obs.state in TERMINAL_STATES:
        return obs, ()
    if not compatible_plan_input(plan, bundle):
        return obs.model_copy(
            update={"quality": "NOT_COMPARABLE", "reason": "PRICE_COORDINATE_CHANGED"}
        ), ()
    pairs = plan_bar_pairs(bundle)
    signal = pairs.get(plan.decision_session)
    if signal is None or signal[1].bar_id != plan.signal_bar_id:
        quality = "NOT_COMPARABLE" if signal is None else "DATA_REVISED"
        return obs.model_copy(update={"quality": quality, "reason": quality}), ()
    scheduled = set(bundle.sessions)
    suspended = set(bundle.provenance.known_suspensions)
    if (
        obs.through_evidence
        and obs.through_session
        and plan_prefix_evidence(plan, obs.through_session, pairs, suspended)
        != obs.through_evidence
    ):
        return obs.model_copy(update={"quality": "DATA_REVISED", "reason": "DATA_REVISED"}), ()
    live = plan.mode == "LIVE"
    receipts_through = receipt_cutoff if live and receipt_cutoff is not None else bundle.as_of
    pending = None
    transitions = []
    prefix_available = max(
        plan.issued_at,
        obs.through_observed_at or plan.issued_at,
        obs.activation_at or plan.issued_at,
    )
    for index, period in enumerate(plan.periods):
        session = period.end_session
        if obs.through_session and session <= obs.through_session:
            # The previous observation already recorded when this prefix became visible.
            continue
        if period.end_at > bundle.as_of:
            break
        if obs.state in WAIT_STATES and index >= plan.activation_sessions:
            break
        if session not in scheduled:
            pending = "CALENDAR_NOT_COVERED"
            break
        pair = pairs.get(session)
        if pair is None and session not in suspended:
            pending = "MISSING_SESSION_BAR"
            break
        bar, raw = pair or (None, None)
        available = (
            max(prefix_available, bar_available(*pair, live))
            if pair
            else max(prefix_available, period.end_at)
        )
        if available > receipts_through:
            pending = "MISSING_SESSION_BAR"
            break
        prefix_available = available
        status, reason = obs.state, obs.reason
        patch = {
            "quality": "OK",
            "through_session": session,
            "through_observed_at": available,
            "through_bar_id": raw.bar_id if raw is not None else None,
        }
        if pair and (obs.state == "TRIGGERED" or available <= plan.activation_deadline):
            invalid = any(
                condition_hit(c, bar) for c in plan.invalidations if available >= c.active_from
            )
            stage = any(
                condition_hit(c, bar) for c in plan.stage_exits if available >= c.active_from
            )
            target = bar.high >= plan.targets[0].low
            if obs.next_index_price is None and available > plan.confirmation.confirmed_at:
                marked = bar.model_copy(update={"available_at": available})
                patch["next_index_price"] = next_index_price(
                    plan.confirmation, marked, raw.bar_id, plan.targets[0]
                )
            if obs.state == "TRIGGERED":
                if target and (invalid or stage):
                    status = "AMBIGUOUS"
                    reason = (
                        "SAME_BAR_TARGET_AND_EXIT" if invalid else "SAME_BAR_TARGET_AND_STAGE_EXIT"
                    )
                elif invalid:
                    status, reason = "INVALIDATED", "HARD_INVALIDATION"
                elif stage:
                    status, reason = "STAGE_EXIT", "STAGE_EXIT"
                elif target:
                    status, reason = "TARGET_REACHED", "INDEX_TARGET_REACHED"
            else:
                triggered = available >= plan.trigger.active_from and condition_hit(
                    plan.trigger, bar
                )
                if triggered and (invalid or stage):
                    status, reason = "AMBIGUOUS", "SAME_BAR_TRIGGER_AND_EXIT"
                elif invalid:
                    status, reason = "INVALIDATED", "HARD_INVALIDATION"
                elif stage:
                    status, reason = "STAGE_EXIT", "STAGE_EXIT"
                elif triggered and bar.close > plan.max_entry_index_price:
                    status, reason = "CANCELLED", "INDEX_CHASE_LIMIT"
                elif triggered:
                    status, reason = "TRIGGERED", "CLOSE_TRIGGER"
                    due = plan.periods[index + plan.holding_sessions - 1]
                    patch.update(
                        activation_session=session,
                        activation_at=available,
                        activation_bar_id=raw.bar_id,
                        activation_price=bar.close,
                        due_session=due.end_session,
                        due_at=due.end_at,
                        activation_target_distance=target_distance(
                            plan.targets[0], bar.close, plan.atr, "UP"
                        ),
                    )
                else:
                    status, reason = _wait_state(plan, bar.close), "WAIT_FOR_CLOSE"
        if status in WAIT_STATES and index == plan.activation_sessions - 1:
            status, reason = "EXPIRED", "ACTIVATION_DEADLINE"
        elif status == "TRIGGERED" and session == (obs.due_session or patch.get("due_session")):
            status, reason = "EXPIRED", "HOLDING_DEADLINE"
        if status in TERMINAL_STATES:
            patch["exit_session"] = session
        if status != obs.state:
            transitions.append(
                _transition(
                    plan, obs.state, status, reason, period, available, raw.bar_id if raw else None
                )
            )
        obs = obs.model_copy(update={**patch, "state": status, "reason": reason})
        if status in TERMINAL_STATES:
            break
    if obs.through_session and obs.through_session != (prior.through_session if prior else None):
        obs = obs.model_copy(
            update={
                "through_evidence": plan_prefix_evidence(
                    plan, obs.through_session, pairs, suspended
                )
            }
        )
    if pending:
        obs = obs.model_copy(update={"quality": "DATA_PENDING", "reason": pending})
    return obs, tuple(transitions)


def _next_track(
    draft: IndexTrack,
    older: IndexTrack,
    brief: Brief,
    bundle: MarketDataBundle,
    comparison: DecisionComparison,
    previous: Brief,
) -> IndexTrack:
    old_plan = older.plan
    if comparison.data_revision in {"REVISED", "NOT_COMPARABLE"}:
        quality = "DATA_REVISED" if comparison.data_revision == "REVISED" else "NOT_COMPARABLE"
        return older.model_copy(
            update={
                "observation": older.observation.model_copy(
                    update={"quality": quality, "reason": quality}
                ),
                "transitions": (),
            }
        )
    if old_plan is None:
        if previous.last_bar_end_at == brief.last_bar_end_at:
            return older.model_copy(update={"transitions": ()})
        return draft
    observation, changes = observe_plan(
        old_plan,
        bundle,
        older.observation,
        receipt_cutoff=max(brief.as_of, brief.generated_at),
    )
    kept = older.model_copy(update={"observation": observation, "transitions": changes})
    if observation.quality != "OK" or observation.state == "TRIGGERED":
        return kept
    plan = draft.plan
    new_bar = (
        brief.last_bar_end_at is not None
        and previous.last_bar_end_at is not None
        and (brief.last_bar_end_at > previous.last_bar_end_at)
    )
    policy_changed = (
        old_plan.policy_hash != brief.index_guidance.policy_hash
        or old_plan.analysis_config_hash != brief.config_hash
        or old_plan.engine_version != brief.engine_version
    )
    if policy_changed and observation.state in WAIT_STATES:
        observed = (
            brief.outlook.issued_at if brief.outlook and brief.outlook.issued_at else brief.as_of
        )
        changes += (
            _transition(
                old_plan,
                observation.state,
                "CANCELLED",
                "POLICY_CHANGED",
                old_plan.periods[0],
                observed,
                brief.last_bar_id,
                session=brief.market_asof,
                effective_at=observed,
            ),
        )
        observation = observation.model_copy(
            update={
                "state": "CANCELLED",
                "reason": "POLICY_CHANGED",
                "exit_session": brief.market_asof,
            }
        )
        kept = kept.model_copy(update={"observation": observation, "transitions": changes})
    if not new_bar:
        return kept
    if plan is None:
        # A conflicting current card delays new issuance. Issued terms remain frozen.
        if observation.state in WAIT_STATES and draft.observation.reason in {
            "BACKGROUND_CONFLICT",
            "WINDOW_CONFLICT",
            "BACKGROUND_MISSING",
            "NEAR_TIE",
        }:
            observation = observation.model_copy(update={"reason": draft.observation.reason})
        return kept.model_copy(
            update={
                "observation": observation,
                "candidate_wait_reason": draft.observation.reason,
                "pending_structure_key": None,
                "pending_since": None,
            }
        )
    if not policy_changed and plan.structure_key == old_plan.structure_key:
        if observation.state in WAIT_STATES:
            observation = observation.model_copy(update={"reason": "WAIT_FOR_CLOSE"})
        return kept.model_copy(
            update={
                "observation": observation,
                "candidate_wait_reason": "WAIT_FOR_INDEPENDENT_STRUCTURE"
                if observation.state in TERMINAL_STATES
                else None,
                "pending_structure_key": None,
                "pending_since": None,
            }
        )
    calendar = bundle.sessions
    consecutive = (
        brief.market_asof in calendar
        and previous.market_asof in calendar
        and calendar.index(brief.market_asof) == calendar.index(previous.market_asof) + 1
    )
    if observation.state in WAIT_STATES and (
        older.pending_structure_key != plan.structure_key
        or not consecutive
        or older.pending_since != previous.last_bar_end_at
    ):
        return kept.model_copy(
            update={
                "pending_structure_key": plan.structure_key,
                "pending_since": brief.last_bar_end_at,
                "observation": observation.model_copy(update={"reason": "WAIT_FOR_STABLE_RECOUNT"}),
            }
        )
    if observation.state in WAIT_STATES:
        changes += (
            _transition(
                old_plan,
                observation.state,
                "CANCELLED",
                "STRUCTURE_REPLACED",
                old_plan.periods[0],
                plan.issued_at,
                brief.last_bar_id,
                session=brief.market_asof,
                effective_at=plan.issued_at,
            ),
        )
    # A close that just terminated the old plan cannot also issue the replacement.
    if observation.exit_session == brief.market_asof and not policy_changed:
        return kept
    successor = plan.model_copy(
        update={"revision": old_plan.revision + 1, "parent_plan_id": old_plan.plan_id}
    )
    return draft.model_copy(
        update={
            "plan": successor,
            "transitions": (*changes, _initial_transition(successor, draft.observation.state)),
        }
    )


def reconcile_index_guidance(
    brief: Brief, previous: Brief | None, prepared: MarketDataBundle, comparison: DecisionComparison
) -> IndexGuidance | None:
    guidance = brief.index_guidance
    if guidance is None or previous is None or previous.index_guidance is None:
        return guidance
    earlier = previous.index_guidance
    if (
        previous.last_bar_end_at == brief.last_bar_end_at
        and previous.input_snapshot_id == brief.input_snapshot_id
        and earlier.policy_hash == guidance.policy_hash
        and guidance.sizing == earlier.sizing
    ):
        return earlier
    comparison_name = (
        "POLICY_CHANGED" if earlier.policy_hash != guidance.policy_hash else comparison.change
    )
    return guidance.model_copy(
        update={
            "comparison": comparison_name,
            "tracks": tuple(
                _with_distances(
                    _next_track(draft, old, brief, prepared, comparison, previous),
                    brief.latest_price,
                )
                for draft, old in zip(guidance.tracks, earlier.tracks, strict=True)
            ),
        }
    )
