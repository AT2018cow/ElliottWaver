from collections import defaultdict
from collections.abc import Sequence
from datetime import date

from eliotbot.schemas.market import Bar


def weekly_bars(bars: Sequence[Bar], sessions: Sequence[date], *, monthly=False) -> tuple[Bar, ...]:
    groups = defaultdict(list)
    expected = defaultdict(set)
    for day in sessions:
        expected[(day.year, day.month) if monthly else day.isocalendar()[:2]].add(day)
    for bar in bars:
        groups[
            (bar.session_date.year, bar.session_date.month)
            if monthly
            else bar.session_date.isocalendar()[:2]
        ].append(bar)
    result = []
    for key, group in sorted(groups.items()):
        # Partial first/last weeks and unknown internal holes cannot anchor a wave.
        if {b.session_date for b in group} != expected[key]:
            continue
        result.append(
            Bar(
                start_at=group[0].start_at,
                end_at=group[-1].end_at,
                available_at=max(b.usable_at for b in group),
                observed_at=max(b.observed_at for b in group),
                session_date=group[-1].session_date,
                open=group[0].open,
                close=group[-1].close,
                high=max(b.high for b in group),
                low=min(b.low for b in group),
                volume=sum(b.volume for b in group),
                amount=sum(b.amount for b in group)
                if all(b.amount is not None for b in group)
                else None,
            )
        )
    return tuple(result)
