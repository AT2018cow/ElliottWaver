"""Compute optional daily context without changing the primary index analysis."""

from __future__ import annotations

import math
from datetime import date, datetime, timedelta
from statistics import stdev
from zoneinfo import ZoneInfo

from elliottwaver.config import IndexGuidanceConfig
from elliottwaver.data.calendar import load_calendar
from elliottwaver.data.periods import available_pairs
from elliottwaver.errors import DataError
from elliottwaver.index_guidance import TERMINAL_STATES, WAIT_STATES
from elliottwaver.schemas.brief import Brief
from elliottwaver.schemas.index_context import (
    ConstituentVersion,
    EventVersion,
    EventWindow,
    IndexBreadth,
    IndexContext,
    IndexContextDataset,
    IndexReference,
    IndexTrend,
)
from elliottwaver.schemas.market import Bar, MarketDataBundle


def _closes(
    bundle: MarketDataBundle, cutoff: datetime, through: date | None = None
) -> dict[date, Bar]:
    return (
        {
            bar.session_date: bar
            for bar, _ in available_pairs(bundle.bars, bundle.raw_bars, cutoff, through=through)
        }
        if bundle.timeframe == "1d"
        else {}
    )


def describe_trend(
    bundle: MarketDataBundle,
    cutoff: datetime,
    window: int,
    minimum_windows: int,
    *,
    through: date | None = None,
) -> IndexTrend:
    closes = _closes(bundle, cutoff, through)
    first, last = (min(closes), max(closes)) if closes else (date.max, date.min)
    dates = tuple(day for day in bundle.sessions if first <= day <= last)
    if len(dates) <= window:
        return IndexTrend(status="INSUFFICIENT_HISTORY", sessions=window)
    sample = dates[-(window + 1) :]
    if any(day not in closes for day in sample):
        return IndexTrend(
            status="INCOMPLETE_HISTORY", sessions=window, start=sample[0], end=sample[-1]
        )

    def volatility(days):
        return stdev(
            math.log(closes[b].close / closes[a].close)
            for a, b in zip(days, days[1:], strict=False)
        )

    current = volatility(sample)
    # The reference windows end before the report's final close. Gaps are excluded.
    earlier = (dates[end - window - 1 : end] for end in range(window + 1, len(dates)))
    past = [volatility(part) for part in earlier if all(day in closes for day in part)]
    percentile = (
        100
        * sum(1 if value < current else 0.5 if value == current else 0 for value in past)
        / len(past)
        if len(past) >= minimum_windows
        else None
    )
    return IndexTrend(
        status="AVAILABLE" if percentile is not None else "PERCENTILE_HISTORY_INSUFFICIENT",
        sessions=window,
        start=sample[0],
        end=sample[-1],
        return_pct=100 * (closes[sample[-1]].close / closes[sample[0]].close - 1),
        volatility=current,
        volatility_percentile=percentile,
        latest_overnight_gap_pct=100 * (closes[sample[-1]].open / closes[sample[-2]].close - 1),
        prior_windows=len(past),
    )


def describe_reference(
    main: MarketDataBundle,
    reference: MarketDataBundle | None,
    expected: str | None,
    cutoff: datetime,
    window: int,
    *,
    unavailable: str | None = None,
    through: date | None = None,
) -> IndexReference:
    def missing(status):
        return IndexReference(status=status, symbol=expected)

    if not expected:
        return missing(unavailable or "NOT_CONFIGURED")
    if expected == main.instrument.canonical_symbol:
        return missing("SAME_INDEX")
    if reference is None:
        return missing(unavailable or "INPUT_MISSING")
    scope = dict(
        symbol=expected,
        feed=reference.provenance.feed,
        snapshot_id=reference.snapshot_id,
        adjustment=reference.provenance.adjustment,
        coordinate_id=reference.provenance.price_coordinate_id,
    )
    if (
        reference.instrument.canonical_symbol != expected
        or reference.instrument.asset_type != "INDEX"
        or reference.instrument.market != main.instrument.market
        or reference.instrument.currency != main.instrument.currency
        or reference.timeframe != "1d"
        or reference.provenance.adjustment != main.provenance.adjustment
    ):
        return IndexReference(status="NOT_COMPARABLE", **scope)
    own, other = _closes(main, cutoff, through), _closes(reference, cutoff, through)
    if not own or not other:
        return IndexReference(status="NO_VISIBLE_BAR", **scope)
    last = max(own)
    pair_time = other[max(other)].usable_at
    raw_last = next(bar for bar in reversed(reference.raw_bars) if bar.session_date == max(other))
    scope["latest_usable_at"] = max(pair_time, raw_last.usable_at)
    if last not in other:
        return IndexReference(status="STALE_REFERENCE", **scope)
    common_dates = tuple(day for day in main.sessions if day <= last)[-(window + 1) :]
    if len(common_dates) <= window or min(other) > common_dates[0] or min(own) > common_dates[0]:
        return IndexReference(status="INSUFFICIENT_HISTORY", **scope)
    if any(day not in own or day not in other for day in common_dates):
        return IndexReference(status="INCOMPLETE_COMMON_SESSIONS", **scope)
    start = common_dates[0]
    if tuple(day for day in reference.sessions if start <= day <= last) != common_dates:
        return IndexReference(status="CALENDAR_MISMATCH", **scope)
    own_return = 100 * (own[last].close / own[start].close - 1)
    other_return = 100 * (other[last].close / other[start].close - 1)
    return IndexReference(
        status="AVAILABLE",
        **scope,
        start=start,
        end=last,
        main_return_pct=own_return,
        reference_return_pct=other_return,
        spread_pp=own_return - other_return,
    )


def known_members(
    dataset: IndexContextDataset | None,
    day: date,
    cutoff: datetime,
) -> dict[str, ConstituentVersion]:
    latest = {}
    if dataset is None:
        return latest
    ordered = sorted(dataset.membership, key=lambda row: (row.effective_from, row.revision))
    for row in ordered:
        if row.effective_from <= day and max(row.announced_at, row.received_at) <= cutoff:
            latest[row.symbol] = row
    return {
        key: row
        for key, row in latest.items()
        if not row.withdrawn and (row.effective_through is None or day <= row.effective_through)
    }


def describe_breadth(
    dataset: IndexContextDataset | None,
    day: date,
    previous: date | None,
    cutoff: datetime,
    minimum_coverage: float,
) -> IndexBreadth:
    members = known_members(dataset, day, cutoff)
    if not members or previous is None:
        return IndexBreadth(status="MEMBERSHIP_UNAVAILABLE", session=day)
    closes = {}
    for row in sorted(dataset.closes, key=lambda item: item.usable_at):
        if row.symbol in members and row.session in {day, previous} and row.usable_at <= cutoff:
            closes[row.symbol, row.session] = row
    compared, rising, weight = 0, 0, 0.0
    for symbol, member in members.items():
        now, before = closes.get((symbol, day)), closes.get((symbol, previous))
        if now is None or before is None or now.basis != before.basis:
            continue
        compared += 1
        rising += now.close > before.close
        weight += member.weight
    total = sum(row.weight for row in members.values())
    coverage = compared / len(members)
    weight_coverage = weight / total if total else None
    sufficient = (
        compared and coverage >= minimum_coverage and (weight_coverage or 0) >= minimum_coverage
    )
    return IndexBreadth(
        status="AVAILABLE" if sufficient else "INSUFFICIENT_COVERAGE",
        session=day,
        members=len(members),
        comparable=compared,
        rising=rising,
        member_coverage=coverage,
        weight_coverage=weight_coverage,
        rising_fraction=rising / compared if compared else None,
    )


def _visible_events(
    dataset: IndexContextDataset | None, cutoff: datetime
) -> tuple[EventVersion, ...]:
    revisions = {}
    if dataset is None:
        return ()
    for row in sorted(dataset.events, key=lambda item: item.revision):
        if max(row.announcement_at, row.received_at) <= cutoff:
            revisions[row.event_id] = row
    return tuple(row for row in revisions.values() if not row.withdrawn)


def describe_events(
    brief: Brief,
    bundle: MarketDataBundle,
    dataset: IndexContextDataset | None,
    policy: IndexGuidanceConfig,
) -> tuple[EventWindow, ...]:
    zone = ZoneInfo(bundle.instrument.timezone)
    today = brief.as_of.astimezone(zone).date()
    calendar = None
    trading_dates = ()
    next_open = None
    for days in (75, 50, 30, 15, 7):
        try:
            calendar = load_calendar(bundle.instrument, today, today + timedelta(days=days))
        except DataError:
            continue
        trading_dates = tuple(day for day in calendar.dates if day >= today)
        next_open = next(
            (row.open_at for row in calendar.sessions if row.open_at > brief.as_of), None
        )
        break
    known = _visible_events(dataset, brief.as_of)
    active_tracks = (
        {track.horizon: track for track in brief.index_guidance.tracks}
        if brief.index_guidance
        else {}
    )
    prospective = (
        tuple(
            session.date
            for session in calendar.sessions
            if session.date >= today and session.close_at > brief.as_of
        )
        if calendar
        else ()
    )
    windows = []
    for horizon, holding in (("SHORT", policy.short_sessions), ("SWING", policy.swing_sessions)):
        proposed = prospective[:holding]
        track = active_tracks.get(horizon)
        end = proposed[-1] if len(proposed) == holding else today
        if (
            track
            and track.plan
            and track.observation.state not in TERMINAL_STATES
            and track.observation.quality == "OK"
            and (
                track.observation.state not in WAIT_STATES
                or brief.as_of <= track.plan.activation_deadline
            )
        ):
            frozen_end = track.observation.due_session or track.plan.periods[-1].end_session
            if frozen_end >= today:
                end = frozen_end

        def day_for(row):
            return row.window_timestamp(brief.as_of).astimezone(zone).date()

        rows = tuple(
            sorted(
                (
                    row
                    for row in known
                    if today <= day_for(row) <= end
                    and (
                        (row.kind == "REBALANCE" and row.symbol == brief.symbol)
                        or (
                            row.kind == "EARNINGS"
                            and row.symbol in known_members(dataset, day_for(row), brief.as_of)
                        )
                    )
                ),
                key=lambda row: (day_for(row), row.event_id),
            )
        )
        covers = tuple(
            row
            for row in (dataset.event_coverage if dataset else ())
            if row.known_at <= brief.as_of and row.start <= today <= end <= row.end
        )
        rb = any(
            item.kind == "REBALANCE" and (not item.symbols or brief.symbol in item.symbols)
            for item in covers
        )
        earnings_coverage = [
            row
            for row in (dataset.event_coverage if dataset else ())
            if row.kind == "EARNINGS" and row.known_at <= brief.as_of
        ]
        hit_symbols = {row.symbol for row in rows if row.kind == "EARNINGS"}
        memberships = []
        # Compute exposure and coverage on each coherent in-window membership.
        # Peak exposure and minimum coverage summarize changing memberships without summing two universes.
        for offset in range((end - today).days + 1):
            day = today + timedelta(days=offset)
            members = known_members(dataset, day, brief.as_of)
            total = sum(member.weight for member in members.values())
            if not 0 < total <= 1.00000001:
                memberships.append(None)
                continue
            available = sum(
                member.weight
                for symbol, member in members.items()
                if any(
                    row.start <= day <= row.end and (not row.symbols or symbol in row.symbols)
                    for row in earnings_coverage
                )
            )
            exposure = sum(
                member.weight for symbol, member in members.items() if symbol in hit_symbols
            )
            memberships.append((min(1.0, available), min(1.0, exposure)))
        covered = bool(memberships and earnings_coverage) and all(
            item is not None and item[0] >= policy.breadth_min_coverage for item in memberships
        )
        known_weights = [item for item in memberships if item is not None]
        covered_weight = (
            min((item[0] if item else 0.0 for item in memberships), default=None)
            if known_weights and earnings_coverage
            else None
        )
        hit_weight = max((item[1] for item in known_weights), default=None)
        windows.append(
            EventWindow(
                horizon=horizon,
                start=today,
                end=end,
                next_open_at=next_open,
                calendar_status="AVAILABLE"
                if calendar and end in trading_dates
                else "CALENDAR_UNAVAILABLE",
                closed_weekdays=tuple(
                    day
                    for i in range((end - today).days + 1)
                    if (day := today + timedelta(days=i)).weekday() < 5 and day not in trading_dates
                )
                if calendar
                else (),
                rebalance_status="AVAILABLE" if rb else "COVERAGE_UNAVAILABLE",
                earnings_status="AVAILABLE"
                if covered
                else "INSUFFICIENT_COVERAGE"
                if known_weights and earnings_coverage
                else "COVERAGE_UNAVAILABLE",
                earnings_covered_weight=covered_weight,
                earnings_event_weight=hit_weight if hit_symbols or covered else None,
                events=rows,
            )
        )
    return tuple(windows)


def build_market_context(
    brief: Brief,
    prepared: MarketDataBundle,
    settings: IndexGuidanceConfig,
    *,
    daily: MarketDataBundle | None = None,
    parent: Brief | None = None,
    reference: MarketDataBundle | None = None,
    reference_symbol: str | None = None,
    reference_status: str | None = None,
    dataset: IndexContextDataset | None = None,
    dataset_sha256: str | None = None,
    dataset_status: str | None = None,
) -> IndexContext:
    base = daily
    if prepared.timeframe == "1d":
        base = prepared
    visible = _closes(base, brief.as_of) if base is not None else {}
    if prepared.timeframe.endswith("m") and visible:
        # Intraday context only compares completed earlier sessions.
        local = brief.as_of.astimezone(ZoneInfo(prepared.instrument.timezone)).date()
        visible = {day: bar for day, bar in visible.items() if day < local}
    last = max(visible, default=None)
    if settings.market_context and base is not None and last is not None:
        earlier = next((day for day in reversed(base.sessions) if day < last), None)
        trend = describe_trend(
            base,
            brief.as_of,
            settings.context_sessions,
            settings.min_volatility_windows,
            through=last,
        )
        levels = describe_reference(
            base,
            reference,
            reference_symbol,
            brief.as_of,
            settings.context_sessions,
            unavailable=reference_status,
            through=last,
        )
        breadth = describe_breadth(
            dataset, last, earlier, brief.as_of, settings.breadth_min_coverage
        )
    else:
        trend = levels = breadth = None
        if settings.market_context:
            trend = IndexTrend(status="DAILY_INPUT_UNAVAILABLE", sessions=settings.context_sessions)
    event_brief = brief
    if parent is not None and brief.index_guidance is None:
        event_brief = brief.model_copy(update={"index_guidance": parent.index_guidance})
    return IndexContext(
        as_of=brief.as_of,
        main_snapshot_id=prepared.snapshot_id,
        daily_snapshot_id=base.snapshot_id if base else None,
        dataset_sha256=dataset_sha256,
        dataset_status=dataset_status,
        trend=trend,
        reference=levels,
        breadth=breadth,
        events=describe_events(event_brief, prepared, dataset, settings)
        if settings.event_context
        else (),
    )
