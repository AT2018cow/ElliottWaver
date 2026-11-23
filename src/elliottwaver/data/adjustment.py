"""Multiplicative basis-date coordinates; raw volume is never dividend-adjusted."""

import math
from bisect import bisect_right
from collections.abc import Sequence
from datetime import date, datetime

from elliottwaver.errors import DataError
from elliottwaver.schemas.market import Bar, FactorPoint, MarketDataBundle


def adjust_to_basis(
    bars: Sequence[Bar], factors: Sequence[FactorPoint], *, basis_date=None
) -> tuple[Bar, ...]:
    if not bars or not factors:
        raise DataError("Raw bars and adjustment factors are required")
    dates = [p.effective_date for p in factors]
    if dates != sorted(set(dates)):
        raise DataError("Factor dates must be ordered and unique")

    def factor(day):
        index = bisect_right(dates, day) - 1
        if index < 0:
            raise DataError(f"Missing adjustment evidence before {day}")
        return factors[index].factor

    basis = factor(basis_date or bars[-1].session_date)
    adjusted = []
    for bar in bars:
        coefficient = factor(bar.session_date) / basis
        values = bar.model_dump()
        values.update(
            {field: getattr(bar, field) * coefficient for field in ("open", "high", "low", "close")}
        )
        adjusted.append(Bar(**values))
    return tuple(adjusted)


def adjust_from_issuance(
    bars: Sequence[Bar],
    current_factors: Sequence[FactorPoint],
    issuance_factors: Sequence[FactorPoint],
    decision: date,
) -> tuple[Bar, ...]:
    """Retain the issuing factor history and append subsequent changes."""
    return adjust_to_basis(
        bars,
        (
            *(point for point in issuance_factors if point.effective_date <= decision),
            *(point for point in current_factors if point.effective_date > decision),
        ),
        basis_date=decision,
    )


def provider_price_scale(
    issued: MarketDataBundle,
    current: MarketDataBundle,
    decision: date,
    *,
    issued_as_of: datetime | None = None,
) -> float | None:
    """Infer a uniform bridge only from unchanged raw sessions at issuance."""

    def daily(bundle):
        if bundle.timeframe == "1w":
            return bundle.raw_daily_context, bundle.daily_context
        return bundle.raw_bars, bundle.bars

    old_raw, old_adjusted = daily(issued)
    new_raw, new_adjusted = daily(current)
    old = [
        (raw, bar)
        for raw, bar in zip(old_raw, old_adjusted, strict=True)
        if bar.session_date <= decision and (issued_as_of is None or bar.usable_at <= issued_as_of)
    ][-2:]
    new = {bar.start_at: (raw, bar) for raw, bar in zip(new_raw, new_adjusted, strict=True)}
    if len(old) < 2 or old[-1][1].session_date != decision:
        return None
    ratios = []
    for raw, bar in old:
        other = new.get(bar.start_at)
        if other is None:
            return None
        new_raw_bar, revised = other
        if raw.end_at != new_raw_bar.end_at:
            return None
        for field in ("open", "high", "low", "close"):
            if getattr(raw, field) != getattr(new_raw_bar, field):
                return None
            ratios.append(getattr(bar, field) / getattr(revised, field))
    first = ratios[0]
    if not math.isfinite(first) or first <= 0:
        return None
    return first if all(math.isclose(value, first, rel_tol=1e-8) for value in ratios) else None
