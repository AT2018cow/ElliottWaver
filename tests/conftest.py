from datetime import UTC, date, datetime, time, timedelta

import pytest

from eliotbot.data.demo import demo_bundle
from eliotbot.schemas.market import Bar
from eliotbot.schemas.wave import PivotPoint


@pytest.fixture
def bundle():
    return demo_bundle()


def make_bar(i, price, *, high=None, low=None, volume=100):
    start = datetime.combine(date(2024, 1, 1) + timedelta(days=i), time(9), UTC)
    return Bar(
        start_at=start,
        end_at=start + timedelta(hours=6),
        session_date=start.date(),
        observed_at=datetime(2026, 1, 1, tzinfo=UTC),
        open=price,
        close=price,
        high=high if high is not None else price,
        low=low if low is not None else price,
        volume=volume,
    )


def make_pivots(prices, *, developing=False):
    bars = [make_bar(i, price) for i, price in enumerate(prices)]
    points = tuple(
        PivotPoint(
            pivot_id=f"p{i}",
            index=i,
            extreme_bar_id=bar.bar_id,
            extreme_at=bar.start_at,
            confirmed_at=None if developing and i == len(bars) - 1 else bar.end_at,
            price=bar.close,
            kind="LOW" if i % 2 == 0 else "HIGH",
            state="DEVELOPING" if developing and i == len(bars) - 1 else "CONFIRMED",
            scale=1.4,
            threshold_at_extreme=1,
        )
        for i, bar in enumerate(bars)
    )
    return bars, points
