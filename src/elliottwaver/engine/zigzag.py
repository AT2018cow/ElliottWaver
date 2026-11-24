"""One-pass ATR ZigZag. An extreme is not known until a later reversal bar."""

from collections.abc import Sequence
from dataclasses import dataclass

import numpy as np

from elliottwaver.schemas.market import Bar
from elliottwaver.schemas.wave import PivotPoint
from elliottwaver.util import digest


@dataclass(frozen=True)
class ZigZagResult:
    confirmed: tuple[PivotPoint, ...]
    tip: PivotPoint | None
    ambiguous_bars: int = 0


def extract(
    bars: Sequence[Bar], atr: Sequence[float], scale: float, tick_size: float = 0.01
) -> ZigZagResult:
    if scale <= 0 or tick_size <= 0 or not np.isfinite([scale, tick_size]).all():
        raise ValueError("scale and tick must be finite and positive")
    if len(bars) != len(atr):
        raise ValueError("ATR and bar lengths differ")
    start = next((i for i, value in enumerate(atr) if np.isfinite(value)), None)
    if start is None:
        return ZigZagResult((), None)
    pivots: list[PivotPoint] = []
    state = "INIT"
    lo = hi = start
    extreme = start
    ambiguous = 0

    def threshold(i: int) -> float:
        return max(scale * float(atr[i]), 2 * tick_size)

    def pivot(i: int, kind: str, status: str, confirm: int | None = None) -> PivotPoint:
        price = bars[i].high if kind == "HIGH" else bars[i].low
        return PivotPoint(
            pivot_id=digest([bars[i].bar_id, kind, scale])[:24],
            index=i,
            extreme_bar_id=bars[i].bar_id,
            extreme_at=bars[i].start_at,
            confirmed_at=bars[confirm].usable_at if confirm is not None else None,
            confirmed_bar_id=bars[confirm].bar_id if confirm is not None else None,
            price=price,
            kind=kind,
            state=status,
            scale=scale,
            threshold_at_extreme=threshold(i),
        )

    for i in range(start + 1, len(bars)):
        bar = bars[i]
        if not np.isfinite(atr[i]):
            raise ValueError("ATR has an internal invalid value")
        if state == "INIT":
            if bar.low < bars[lo].low:
                lo = i
            if bar.high > bars[hi].high:
                hi = i
            up = lo < i and bar.close >= bars[lo].low + threshold(lo)
            down = hi < i and bar.close <= bars[hi].high - threshold(hi)
            if up and down:
                ambiguous += 1
            elif up:
                pivots.append(pivot(lo, "LOW", "SEED", i))
                state, extreme = "UP", i
            elif down:
                pivots.append(pivot(hi, "HIGH", "SEED", i))
                state, extreme = "DOWN", i
        elif state == "UP":
            if bar.high > bars[extreme].high:
                extreme = i
            elif bar.close <= bars[extreme].high - threshold(extreme):
                pivots.append(pivot(extreme, "HIGH", "CONFIRMED", i))
                state, extreme = "DOWN", i
        else:
            if bar.low < bars[extreme].low:
                extreme = i
            elif bar.close >= bars[extreme].low + threshold(extreme):
                pivots.append(pivot(extreme, "LOW", "CONFIRMED", i))
                state, extreme = "UP", i
    tip = (
        pivot(extreme, "HIGH" if state == "UP" else "LOW", "DEVELOPING")
        if state != "INIT"
        else None
    )
    return ZigZagResult(tuple(pivots), tip, ambiguous)
