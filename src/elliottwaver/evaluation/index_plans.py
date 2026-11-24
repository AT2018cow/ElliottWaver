"""Index-only settlement and descriptive competing-event statistics."""

from collections import Counter, defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Literal

from elliottwaver.errors import DataError
from elliottwaver.index_guidance import TERMINAL_STATES, compatible_plan_input, observe_plan
from elliottwaver.schemas.brief import Brief
from elliottwaver.schemas.index_plan import IndexTrack, PlanTransition
from elliottwaver.schemas.market import MarketDataBundle

Event = Literal["TARGET", "INVALIDATION", "OTHER", "UNKNOWN", "CENSORED"]
HORIZONS = (2, 5, 10, 20, 30)


@dataclass(frozen=True)
class TimedOutcome:
    sessions: int
    event: Event


def settle_index_track(track: IndexTrack, source: MarketDataBundle) -> dict:
    plan = track.plan
    assert plan is not None
    prior = track.observation
    recorded_at = max(
        filter(
            None,
            (
                plan.issued_at,
                prior.through_observed_at,
                prior.activation_at,
                prior.next_index_price.available_at if prior.next_index_price else None,
                *(item.observable_at for item in track.transitions),
            ),
        )
    )
    observed_session = prior.exit_session or prior.through_session
    period = next((item for item in plan.periods if item.end_session == observed_session), None)
    through = next(
        (bar for bar in source.raw_bars if bar.session_date == prior.through_session), None
    )
    if (
        source.as_of < recorded_at
        or (period and source.as_of < period.end_at)
        or (prior.through_bar_id and (through is None or through.bar_id != prior.through_bar_id))
        or (
            prior.through_session
            and through is None
            and prior.through_session not in source.provenance.known_suspensions
        )
    ):
        raise DataError(
            "INDEX_SETTLEMENT_OBSERVATION_UNCOVERED: snapshot cannot substantiate the recorded observation"
        )
    signal = next((b for b in source.raw_bars if b.session_date == plan.decision_session), None)
    if not compatible_plan_input(plan, source):
        observation = prior.model_copy(
            update={"quality": "NOT_COMPARABLE", "reason": "PRICE_COORDINATE_CHANGED"}
        )
        changes = ()
    elif signal is None or signal.bar_id != plan.signal_bar_id:
        observation = prior.model_copy(
            update={
                "quality": "NOT_COMPARABLE" if signal is None else "DATA_REVISED",
                "reason": "NOT_COMPARABLE" if signal is None else "DATA_REVISED",
            }
        )
        changes = ()
    else:
        observation, changes = observe_plan(plan, source, prior)
    return {
        "symbol": plan.symbol,
        "mode": plan.mode,
        "feed": plan.feed,
        "adjustment": plan.adjustment,
        "policy_hash": plan.policy_hash,
        "config_hash": plan.analysis_config_hash,
        "engine_version": plan.engine_version,
        "coordinate_id": plan.index_coordinate_id,
        "horizon": plan.horizon,
        "holding_sessions": plan.holding_sessions,
        "plan_id": plan.plan_id,
        "revision": plan.revision,
        "source_snapshot_id": source.snapshot_id,
        "observation": observation.model_dump(mode="json"),
        "transitions": [t.model_dump(mode="json") for t in changes],
        "period_sessions": [p.end_session.isoformat() for p in plan.periods],
    }


def competing_risks(outcomes: Iterable[TimedOutcome]) -> dict:
    """Aalen–Johansen cumulative incidence on a trading-session clock."""
    grouped = defaultdict(Counter)
    for outcome in outcomes:
        if outcome.sessions < 0:
            raise ValueError("follow-up must count nonnegative sessions")
        grouped[outcome.sessions][outcome.event] += 1
    counts = sum(grouped.values(), Counter())
    n = sum(counts.values())

    def curve(unknown_as: Event):
        risk, survival = n - sum(grouped[0].values()), 1.0
        target, invalid = 0.0, 0.0
        result = {}
        for day in range(1, max(HORIZONS) + 1):
            daily = grouped[day]
            if day in HORIZONS:
                result[str(day)] = {"at_risk": risk}
            if risk:
                target_today = daily["TARGET"] + (daily["UNKNOWN"] if unknown_as == "TARGET" else 0)
                invalid_today = daily["INVALIDATION"] + (
                    daily["UNKNOWN"] if unknown_as == "INVALIDATION" else 0
                )
                competing = daily["TARGET"] + daily["INVALIDATION"] + daily["OTHER"]
                if unknown_as != "CENSORED":
                    competing += daily["UNKNOWN"]
                target += survival * target_today / risk
                invalid += survival * invalid_today / risk
                survival *= 1 - competing / risk
                risk -= sum(daily.values())
            if day in HORIZONS:
                result[str(day)].update(target=target, invalidation=invalid)
        return result

    center = curve("CENSORED")
    lower = curve("OTHER")
    target_upper = curve("TARGET")
    invalid_upper = curve("INVALIDATION")
    for day, estimates in center.items():
        estimates["target_ambiguity_range"] = [lower[day]["target"], target_upper[day]["target"]]
        estimates["invalidation_ambiguity_range"] = [
            lower[day]["invalidation"],
            invalid_upper[day]["invalidation"],
        ]
        if not n or n == sum(grouped[0].values()):
            for key in (
                "target",
                "invalidation",
                "target_ambiguity_range",
                "invalidation_ambiguity_range",
            ):
                estimates[key] = None
    return {"count": n, "counts": dict(counts), "by_session": center}


def _timed_outcome(row: dict, *, from_activation: bool) -> TimedOutcome | None:
    observed = row["observation"]
    sessions = row["period_sessions"]
    activation = observed["activation_session"]
    if from_activation and activation is None:
        return None
    first_index = sessions.index(activation) if from_activation else 0
    # A concurrent data issue never overwrites an already observed terminal event.
    last = observed["exit_session"] or observed["through_session"]
    if last in sessions:
        duration = max(0, sessions.index(last) + 1 - first_index)
    else:
        duration = sum(session <= last for session in sessions[first_index:]) if last else 0
    state = observed["state"]
    event = {
        "TARGET_REACHED": "TARGET",
        "INVALIDATED": "INVALIDATION",
        "AMBIGUOUS": ("UNKNOWN" if observed["reason"] == "SAME_BAR_TARGET_AND_EXIT" else "OTHER"),
    }.get(state, "OTHER" if state in TERMINAL_STATES else "CENSORED")
    return TimedOutcome(duration, event if duration else "CENSORED")


def summarize_index_plans(rows: Iterable[dict], issuance_keys: Iterable[tuple] = ()) -> list[dict]:
    groups: dict[tuple, dict] = defaultdict(dict)
    for row in rows:
        key = tuple(
            row[k]
            for k in (
                "symbol",
                "mode",
                "feed",
                "adjustment",
                "policy_hash",
                "config_hash",
                "engine_version",
                "coordinate_id",
                "horizon",
                "holding_sessions",
            )
        )
        groups[key][(row["plan_id"], row["revision"])] = row
    counts = Counter(key for key, _ in set(issuance_keys))
    for key in counts:
        groups.setdefault(key, {})
    return [
        {
            "cohort": key,
            "status": "RESEARCH_ONLY" if group else "NO_ISSUED_PLAN",
            "daily_decisions_observed": counts[key],
            "issued_plans": len(group),
            "activated_plans": sum(
                row["observation"]["activation_session"] is not None for row in group.values()
            ),
            "data_pending": sum(row["observation"]["quality"] != "OK" for row in group.values()),
            "from_issuance": competing_risks(
                item
                for row in group.values()
                if (item := _timed_outcome(row, from_activation=False)) is not None
            ),
            "from_activation": competing_risks(
                item
                for row in group.values()
                if (item := _timed_outcome(row, from_activation=True)) is not None
            ),
        }
        for key, group in sorted(groups.items())
    ]


def apply_published_cancellation(row: dict, cancellation: PlanTransition) -> dict:
    """Keep a causally issued cancellation when replay spans several report directories."""
    obs = {
        **row["observation"],
        "state": "CANCELLED",
        "reason": cancellation.reason,
        "exit_session": cancellation.session.isoformat(),
        "through_session": cancellation.session.isoformat(),
        "through_observed_at": cancellation.observable_at.isoformat(),
        "through_bar_id": cancellation.source_bar_id,
        "quality": "OK",
        "activation_session": None,
        "activation_at": None,
        "activation_bar_id": None,
        "activation_price": None,
        "due_session": None,
        "due_at": None,
        "activation_target_distance": None,
    }
    observed_quote = obs.get("next_index_price")
    if observed_quote and observed_quote["available_at"] > cancellation.observable_at.isoformat():
        obs["next_index_price"] = None
    return {
        **row,
        "observation": obs,
        "transitions": [
            t
            for t in row["transitions"]
            if t["to_state"] in {"WAITING", "NEAR"}
            and t["observable_at"] < cancellation.observable_at.isoformat()
        ]
        + [cancellation.model_dump(mode="json")],
    }


def summarize_developing_evidence(runs: Iterable[tuple[Brief, MarketDataBundle]]) -> list[dict]:
    """Compare what each report knew at first observation with later confirmation."""
    from elliottwaver.data.periods import available_pairs
    from elliottwaver.evaluation.events import condition_hit

    groups = defaultdict(dict)
    for brief, source in runs:
        guidance = brief.index_guidance
        if guidance is None:
            continue
        key = (
            brief.symbol,
            brief.mode,
            brief.feed,
            brief.adjustment,
            brief.price_coordinate_id,
            brief.config_hash,
            brief.engine_version,
            guidance.policy_hash,
        )
        existing = groups[key].get(brief.last_bar_end_at)
        if existing is None or (brief.as_of, brief.brief_digest) > (
            existing[0].as_of,
            existing[0].brief_digest,
        ):
            groups[key][brief.last_bar_end_at] = (brief, source)
    result = []
    for key, cases in sorted(groups.items()):
        pending = {}
        seen_openings = set()
        metrics = Counter()
        delays = []
        for brief, source in sorted(cases.values(), key=lambda item: item[0].as_of):
            guidance = brief.index_guidance
            opening, confirmed = guidance.developing, guidance.confirmation
            for identity, observation in tuple(pending.items()):
                if (
                    brief.status != "OK"
                    or brief.issues
                    and any(
                        issue.code in {"ALLOWED_GAPS", "UNKNOWN_SESSION_GAPS", "STALE_SNAPSHOT"}
                        for issue in brief.issues
                    )
                ):
                    continue
                if (
                    confirmed
                    and confirmed.pivot_id == identity
                    and confirmed.confirmed_at > observation.first_seen_at
                ):
                    metrics["confirmed_after_observation"] += 1
                    delay = (confirmed.confirmed_at - observation.first_seen_at).total_seconds()
                    premium = confirmed.confirmation_price - observation.reference_price
                    within_limit = (
                        confirmed.confirmation_price <= observation.comparable_entry_ceiling
                    )
                    metrics["confirmation_within_early_price_limit"] += within_limit
                    same_coordinate = (
                        source.timeframe == "1d"
                        and source.instrument.canonical_symbol == brief.symbol
                        and source.provenance.feed == brief.feed
                        and source.provenance.adjustment == brief.adjustment
                        and source.provenance.price_coordinate_id == brief.price_coordinate_id
                    )
                    target_seen = invalidation_seen = None
                    if same_coordinate:
                        paired = available_pairs(
                            source.bars, source.raw_bars, confirmed.confirmed_at
                        )
                        observed = [
                            bar
                            for bar, raw in paired
                            if bar.end_at > observation.first_seen_at
                            and max(bar.usable_at, raw.usable_at) < confirmed.confirmed_at
                        ]
                        if observed:
                            target_seen = any(
                                bar.high >= observation.target.low for bar in observed
                            )
                            invalidation_seen = any(
                                condition_hit(observation.hard_boundary, bar) for bar in observed
                            )
                            metrics["target_observed_during_confirmation_wait"] += target_seen
                            metrics["invalidation_observed_during_confirmation_wait"] += (
                                invalidation_seen
                            )
                    delays.append(
                        {
                            "pivot_id": identity,
                            "preview_observed_at": observation.first_seen_at,
                            "confirmed_at": confirmed.confirmed_at,
                            "delay_seconds": delay,
                            "confirmed_price_minus_preview_points": premium,
                            "within_preview_limit": within_limit,
                            "target_observed_during_wait": target_seen,
                            "invalidation_observed_during_wait": invalidation_seen,
                        }
                    )
                    pending.pop(identity)
                elif (
                    opening
                    and opening.pivot_id != identity
                    and opening.extreme_at >= observation.extreme_at
                ):
                    metrics["developing_extreme_replaced"] += 1
                    pending.pop(identity)
            if opening and opening.early_eligible and opening.pivot_id not in seen_openings:
                pending[opening.pivot_id] = opening
                seen_openings.add(opening.pivot_id)
                metrics["early_observations"] += 1
        result.append(
            {
                "cohort": key,
                "status": "RESEARCH_ONLY" if metrics else "NO_EARLY_OBSERVATIONS",
                "counts": dict(metrics),
                "waiting_or_right_censored": len(pending),
                "paired": delays,
            }
        )
    return result


def summarize_plan_stability(runs: Iterable[tuple[Brief, MarketDataBundle]]) -> list[dict]:
    groups = defaultdict(dict)
    for brief, source in runs:
        guidance = brief.index_guidance
        if guidance is None:
            continue
        key = (
            brief.symbol,
            brief.mode,
            brief.feed,
            brief.adjustment,
            brief.price_coordinate_id,
            brief.config_hash,
            brief.engine_version,
            guidance.policy_hash,
        )
        old = groups[key].get(brief.market_asof)
        if old is None or (brief.as_of, brief.brief_digest) > (old[0].as_of, old[0].brief_digest):
            groups[key][brief.market_asof] = (brief, source)

    results = []
    for key, cases in sorted(groups.items()):
        previous = None
        pairs = switches = 0
        cancellations = set()
        pending_decisions = 0
        for day, (brief, source) in sorted(cases.items()):
            guidance = brief.index_guidance
            if (
                previous is not None
                and previous.index_guidance.structure_key
                and guidance.structure_key
                and brief.status == previous.status == "OK"
                and guidance.comparison
                not in {"CONFIG_CHANGED", "POLICY_CHANGED", "NOT_COMPARABLE", "DATA_REVISED"}
                and previous.market_asof in source.sessions
                and day in source.sessions
                and source.sessions.index(day) == source.sessions.index(previous.market_asof) + 1
            ):
                pairs += 1
                switches += previous.index_guidance.structure_key != guidance.structure_key
            pending_decisions += any(t.pending_structure_key is not None for t in guidance.tracks)
            cancellations.update(
                event.transition_id
                for track in guidance.tracks
                for event in track.transitions
                if event.reason in {"STRUCTURE_REPLACED", "POLICY_CHANGED"}
            )
            previous = brief
        results.append(
            {
                "cohort": key,
                "comparable_adjacent_pairs": pairs,
                "structure_switches": switches,
                "switch_rate": switches / pairs if pairs else None,
                "pending_recount_decisions": pending_decisions,
                "plan_cancellations": len(cancellations),
            }
        )
    return results
