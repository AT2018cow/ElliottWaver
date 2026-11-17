"""Multiplicative basis-date coordinates; raw volume is never dividend-adjusted."""

from bisect import bisect_right
from collections.abc import Sequence

from eliotbot.errors import DataError
from eliotbot.schemas.market import Bar, FactorPoint


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
