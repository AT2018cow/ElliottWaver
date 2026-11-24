"""Observe frozen daily index plans on completed 15-minute bars."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from statistics import median
from zoneinfo import ZoneInfo

from elliottwaver.config import IndexGuidanceConfig
from elliottwaver.data.calendar import load_calendar
from elliottwaver.data.periods import (
    available_pairs,
    background_bundle,
    bar_available,
    intraday_windows,
)
from elliottwaver.errors import DataError
from elliottwaver.evaluation.events import condition_hit
from elliottwaver.index_guidance import WAIT_STATES
from elliottwaver.schemas.brief import Brief
from elliottwaver.schemas.index_context import (
    IndexIntraday,
    IntradayInput,
    IntradayTrack,
    IntradayVariant,
    IntradayVolume,
)
from elliottwaver.schemas.index_plan import IndexPlan
from elliottwaver.schemas.market import MarketDataBundle
from elliottwaver.util import digest

METHODS = ("TOUCH", "CLOSE", "RETEST")


@dataclass(frozen=True)
class ParentIssuance:
    digest: str
    visible_at: datetime
    daily: MarketDataBundle


def _input(
    bundle: MarketDataBundle, age: int | None = None, *, live: bool = False
) -> IntradayInput:
    raw, bar = bundle.raw_bars[-1], bundle.bars[-1]
    return IntradayInput(
        timeframe=bundle.timeframe,
        snapshot_id=bundle.snapshot_id,
        feed=bundle.provenance.feed,
        adjustment=bundle.provenance.adjustment,
        coordinate_id=bundle.provenance.price_coordinate_id,
        last_bar_id=raw.bar_id,
        last_bar_usable_at=bar_available(bar, raw, live),
        last_bar_end_at=bar.end_at,
        max_age_minutes=age,
    )


def relative_volume(bundle: MarketDataBundle, policy: IndexGuidanceConfig) -> IntradayVolume:
    if not bundle.provenance.volume_comparable:
        return IntradayVolume(status="VOLUME_NOT_COMPARABLE")
    cutoff = bundle.as_of
    pairs = available_pairs(bundle.bars, bundle.raw_bars, cutoff)
    if not pairs:
        return IntradayVolume(status="NO_VISIBLE_BAR")
    current = pairs[-1][1]
    timezone = ZoneInfo(bundle.instrument.timezone)
    slot = current.start_at.astimezone(timezone).timetz().replace(tzinfo=None)
    elapsed = current.end_at - current.start_at
    prior_dates = set(
        tuple(day for day in bundle.sessions if day < current.session_date)[
            -policy.volume_history_sessions :
        ]
    )
    volumes = [
        raw.volume
        for _, raw in pairs
        if raw.session_date in prior_dates
        and raw.end_at - raw.start_at == elapsed
        and raw.start_at.astimezone(timezone).timetz().replace(tzinfo=None) == slot
    ]
    if len(volumes) < policy.min_volume_sessions:
        return IntradayVolume(status="INSUFFICIENT_SAMPLE", sample_days=len(volumes))
    center = median(volumes)
    if center <= 0:
        return IntradayVolume(status="ZERO_BASELINE", sample_days=len(volumes))
    return IntradayVolume(
        status="AVAILABLE", sample_days=len(volumes), value=current.volume / center
    )


def verify_price_basis(
    plan: IndexPlan,
    origin: ParentIssuance,
    child: MarketDataBundle,
    *,
    observed_cutoff: datetime | None = None,
) -> tuple[str, tuple[date, ...], datetime | None]:
    daily = origin.daily
    underlying = daily.instrument
    if (
        daily.snapshot_id != plan.input_snapshot_id
        or daily.timeframe != "1d"
        or underlying.canonical_symbol != plan.symbol
        or child.instrument.canonical_symbol != plan.symbol
        or underlying.currency != child.instrument.currency
        or underlying.market != child.instrument.market
        or underlying.instrument_id != child.instrument.instrument_id
        or daily.provenance.feed != plan.feed
        or daily.provenance.adjustment != plan.adjustment
        or child.provenance.adjustment != plan.adjustment
        or daily.raw_bars[-1].bar_id != plan.signal_bar_id
    ):
        return "PRICE_BASIS_UNVERIFIED", (), None
    if child.provenance.feed == plan.feed:
        if (
            plan.adjustment in {"none", "synthetic_no_actions"}
            or child.provenance.price_coordinate_id == plan.index_coordinate_id
        ):
            return "SAME_FEED", (), origin.visible_at
        return "PRICE_BASIS_UNVERIFIED", (), None
    if plan.adjustment != "none":
        return "PRICE_BASIS_UNVERIFIED", (), None
    # Require two overlapping, completely observed sessions on both feeds.
    daily_bars = {
        raw.session_date: raw
        for _, raw in available_pairs(daily.bars, daily.raw_bars, plan.decision_at)
    }
    live = plan.mode == "LIVE"
    upper = observed_cutoff or child.as_of
    child_pairs = {
        raw.start_at: (raw, bar)
        for bar, raw in available_pairs(child.bars, child.raw_bars, child.as_of)
        if bar_available(bar, raw, live) <= upper
    }
    child_bars = {start: raw for start, (raw, _) in child_pairs.items()}
    if not child_bars:
        return "PRICE_BASIS_UNVERIFIED", (), None
    try:
        first = max(min(daily_bars), min(bar.session_date for bar in child_bars.values()))
        calendar = load_calendar(underlying, first, plan.decision_session)
    except (DataError, ValueError):
        return "PRICE_BASIS_UNVERIFIED", (), None
    tolerance = max(underlying.tick_size, child.instrument.tick_size) / 2
    verified = []
    proof_at = origin.visible_at
    for session in reversed(calendar.sessions):
        if session.date not in daily_bars or not first <= session.date <= plan.decision_session:
            continue
        slots = tuple(intraday_windows(session, 15))
        bars = [child_bars.get(opened) for opened, _ in slots]
        if not bars or any(
            bar is None or bar.end_at != closed
            for bar, (_, closed) in zip(bars, slots, strict=True)
        ):
            continue
        original = daily_bars[session.date]
        actual = (
            bars[0].open,
            max(bar.high for bar in bars),
            min(bar.low for bar in bars),
            bars[-1].close,
        )
        expected = (original.open, original.high, original.low, original.close)
        if any(abs(a - b) > tolerance for a, b in zip(actual, expected, strict=True)):
            return "PRICE_BASIS_UNVERIFIED", (), None
        verified.append(session.date)
        proof_at = max(proof_at, *(bar_available(*child_pairs[start], live) for start, _ in slots))
        if len(verified) == 2:
            return "CROSS_FEED_VERIFIED", tuple(sorted(verified)), proof_at
    return "PRICE_BASIS_UNVERIFIED", (), None


def _check_track(
    plan: IndexPlan,
    origin: ParentIssuance,
    child: MarketDataBundle,
    hourly: MarketDataBundle | None,
    ready: datetime,
) -> IntradayTrack:
    policy = plan.intraday_policy
    base = dict(
        horizon=plan.horizon,
        plan_id=plan.plan_id,
        revision=plan.revision,
        parent_brief_digest=origin.digest,
        parent_visible_at=origin.visible_at,
        deadline=plan.activation_deadline,
        threshold=plan.trigger.price,
    )
    if policy is None:
        return IntradayTrack(status="POLICY_NOT_FROZEN", **base)
    base["selected_method"] = policy.method
    basis, basis_days, proof_at = verify_price_basis(plan, origin, child, observed_cutoff=ready)
    if basis == "PRICE_BASIS_UNVERIFIED":
        return IntradayTrack(status=basis, **base)
    base.update(price_basis=basis, basis_sessions=basis_days, basis_observable_at=proof_at)
    issue = max(origin.visible_at, plan.issued_at)
    live = plan.mode == "LIVE"
    if issue > ready:
        return IntradayTrack(status="PARENT_NOT_VISIBLE", **base)
    try:
        calendar = load_calendar(
            child.instrument,
            plan.periods[0].end_session,
            plan.periods[plan.activation_sessions - 1].end_session,
        )
    except DataError:
        return IntradayTrack(status="CALENDAR_UNAVAILABLE", **base)
    paired = {
        bar.start_at: (bar, raw)
        for bar, raw in available_pairs(child.bars, child.raw_bars, child.as_of)
        if bar_available(bar, raw, live) <= ready
    }
    hours = available_pairs(hourly.bars, hourly.raw_bars, hourly.as_of) if hourly else ()
    idle_quality = (
        "RECEIPT_NOT_VISIBLE"
        if child.bars[-1].start_at not in paired
        else "STALE_15M"
        if ready - child.bars[-1].end_at > timedelta(minutes=policy.max_age_minutes)
        else None
    )
    last = child.bars[-1]
    nominal_delay = max(timedelta(), last.usable_at - last.end_at)
    scheduled = {period.end_session for period in plan.periods[: plan.activation_sessions]}
    results = {}
    quality = idle_quality or ("BACKGROUND_60M_NOT_READY" if not hours else None)
    prefix_at = max(issue, proof_at or issue)
    stopped = False
    breakout_at = None
    breakout_end = None
    for session in calendar.sessions:
        if session.date not in scheduled:
            continue
        for opened, closed in intraday_windows(session, 15):
            if (
                opened < issue
                or closed > plan.activation_deadline
                or closed + nominal_delay > ready
            ):
                continue
            item = paired.get(opened)
            if item is None or item[0].end_at != closed:
                quality = "MISSING_15M_SLOT"
                stopped = True
                break
            bar, raw = item
            prefix_at = max(prefix_at, bar_available(bar, raw, live))
            if prefix_at > plan.activation_deadline:
                stopped = True
                break
            threshold = plan.trigger.price
            touches = bar.high >= threshold
            closes = bar.close > threshold
            retests = (
                breakout_at is not None
                and breakout_at <= prefix_at
                and breakout_end <= opened
                and abs(bar.low - threshold) <= policy.retest_tolerance_points
                and closes
            )
            stops = any(
                prefix_at >= c.active_from
                and (c.side != "CLOSE" or closed == session.close_at)
                and condition_hit(c, bar)
                for c in (*plan.invalidations, *plan.stage_exits)
            )
            if stops and not (touches or closes or retests):
                quality = "PARENT_INVALIDATED"
                stopped = True
                break
            with_background = any(
                h.session_date == bar.session_date
                and bar_available(h, original, live) <= prefix_at
                and h.end_at <= bar.end_at
                and prefix_at - h.end_at <= timedelta(minutes=policy.background_max_age_minutes)
                for h, original in hours
            )
            if with_background or stops:
                for method, hit in zip(METHODS, (touches, closes, retests), strict=True):
                    if not hit or method in results:
                        continue
                    status = (
                        "AMBIGUOUS"
                        if stops
                        else "STALE_CROSSING"
                        if prefix_at - closed > timedelta(minutes=policy.max_age_minutes)
                        else "CHASE_LIMIT"
                        if bar.close > plan.max_entry_index_price
                        else "TRIGGERED"
                    )
                    results[method] = IntradayVariant(
                        method=method,
                        status=status,
                        raw_bar_id=raw.bar_id,
                        bar_end_at=bar.end_at,
                        observable_at=prefix_at,
                        price=threshold if method == "TOUCH" else bar.close,
                        transition_id=digest(
                            [
                                plan.mode,
                                plan.plan_id,
                                plan.revision,
                                digest(plan),
                                method,
                                status,
                                None
                                if status == "TRIGGERED" and method == policy.method
                                else raw.bar_id,
                            ]
                        )[:24],
                    )
            elif touches:
                quality = "BACKGROUND_60M_NOT_READY"
            if closes and not stops and breakout_at is None:
                breakout_at, breakout_end = prefix_at, closed
            if stops:
                quality = "PARENT_INVALIDATED"
                stopped = True
                break
        if stopped:
            break
    states = tuple(
        results.get(method) or IntradayVariant(method=method, status="WAITING")
        for method in METHODS
    )
    match = results.get(policy.method)
    status = (
        "AMBIGUOUS"
        if match is not None and match.status == "AMBIGUOUS"
        else quality
        if quality in {"PARENT_INVALIDATED", "MISSING_15M_SLOT"}
        else "PARENT_EXPIRED"
        if ready > plan.activation_deadline
        else idle_quality
        if idle_quality in {"STALE_15M", "RECEIPT_NOT_VISIBLE"}
        else match.status
        if match
        else quality or "WAITING"
    )
    return IntradayTrack(status=status, variants=states, **base)


def build_intraday_tracking(
    brief: Brief,
    prepared: MarketDataBundle,
    parent: Brief | None,
    issuances: Mapping[str, ParentIssuance],
    policy: IndexGuidanceConfig,
    *,
    hourly: MarketDataBundle | None = None,
) -> IndexIntraday:
    if hourly is None:
        try:
            hourly = background_bundle(prepared, "60m", as_of=brief.as_of)
        except DataError:
            hourly = None
    live = brief.mode == "LIVE"
    ready = max(brief.as_of, brief.generated_at) if live else brief.as_of
    evidence = {("15m", prepared.snapshot_id): _input(prepared, live=live)}
    if hourly is not None:
        evidence[("60m", hourly.snapshot_id)] = _input(hourly, live=live)
    latest = (
        {track.horizon: track for track in parent.index_guidance.tracks}
        if parent and parent.index_guidance
        else {}
    )
    tracks = []
    for horizon in ("SHORT", "SWING"):
        track = latest.get(horizon)
        plan = track.plan if track else None
        if plan is None:
            tracks.append(IntradayTrack(horizon=horizon, status="NO_DAILY_PLAN"))
            continue
        origin = issuances.get(plan.plan_id)
        if track.observation.state not in WAIT_STATES or track.observation.quality != "OK":
            status = "PARENT_INACTIVE"
        elif origin is None:
            status = "PARENT_RECEIPT_UNAVAILABLE"
        else:
            evidence[("1d", origin.daily.snapshot_id)] = _input(origin.daily, live=live)
            tracks.append(_check_track(plan, origin, prepared, hourly, ready))
            continue
        tracks.append(
            IntradayTrack(
                horizon=horizon,
                status=status,
                plan_id=plan.plan_id,
                revision=plan.revision,
                deadline=plan.activation_deadline,
                threshold=plan.trigger.price,
            )
        )
    parent_bias = brief.cross_timeframe.background_bias if brief.cross_timeframe else None
    applicable = next((track.selected_method for track in tracks if track.selected_method), None)
    return IndexIntraday(
        selected_method=applicable or policy.intraday_trigger,
        policy_hash=digest(
            [
                "index-15m-v1",
                [track.plan.intraday_policy if track.plan else None for track in latest.values()],
                policy.volume_history_sessions,
                policy.min_volume_sessions,
            ]
        ),
        inputs=tuple(evidence.values()),
        background_bias=parent_bias or "UNKNOWN",
        volume=relative_volume(prepared, policy),
        tracks=tuple(tracks),
    )
