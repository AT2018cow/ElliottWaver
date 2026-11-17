from collections.abc import Sequence
from datetime import date

from eliotbot.config import AnalysisConfig
from eliotbot.data.resample import weekly_bars
from eliotbot.engine.indicators import calculate
from eliotbot.engine.zigzag import extract
from eliotbot.schemas.market import AnalysisRequest, Bar, MarketDataBundle


def select_anchors(
    bundle: MarketDataBundle,
    request: AnalysisRequest,
    config: AnalysisConfig,
) -> tuple[tuple[date, ...], tuple[str, ...]]:
    bars = bundle.bars
    if request.anchor:
        if request.anchor not in {b.session_date for b in bars}:
            from eliotbot.errors import InputError

            raise InputError("Explicit anchor must identify an available bar")
        return (request.anchor,), ()
    earliest = max(0, len(bars) - config.max_analysis_bars)
    if request.start:
        earliest = max(
            earliest,
            next((i for i, b in enumerate(bars) if b.session_date >= request.start), len(bars) - 1),
        )
    if bundle.timeframe == "1d":
        weekly = weekly_bars(bars, bundle.sessions)
    elif bundle.timeframe == "1w":
        weekly = weekly_bars(bundle.daily_context, bundle.sessions, monthly=True)
    else:
        weekly = bundle.daily_context
    anchors: list[date] = []
    if len(weekly) >= 15:
        zz = extract(weekly, calculate(weekly).atr, config.macro_scale, bundle.instrument.tick_size)
        counts = {"LOW": 0, "HIGH": 0}
        for pivot in reversed(zz.confirmed):
            if pivot.confirmed_at is None or pivot.confirmed_at > request.as_of:
                continue
            week = weekly[pivot.index]
            group = [b for b in bars if b.start_at < week.end_at and b.end_at > week.start_at]
            if not group:
                continue
            daily = (
                min(group, key=lambda b: b.low)
                if pivot.kind == "LOW"
                else max(group, key=lambda b: b.high)
            )
            index = next(i for i, b in enumerate(bars) if b.start_at == daily.start_at)
            age = len(bars) - 1 - index
            if (
                index >= earliest
                and config.min_anchor_bars <= age <= config.max_analysis_bars
                and counts[pivot.kind] < 2
            ):
                counts[pivot.kind] += 1
                anchors.append(daily.session_date)
                if len(anchors) >= config.max_anchors:
                    break
    if anchors:
        return tuple(sorted(set(anchors))), ()
    fallback = max(earliest, len(bars) - 252)
    return (bars[fallback].session_date,), (
        "ANCHOR_FALLBACK: 未找到已确认的合格背景周期锚点，使用明确标记的252根K线窗口",
    )


def require_history(bars: Sequence[Bar], minimum: int = 60) -> None:
    if len(bars) < minimum:
        from eliotbot.errors import DataError

        raise DataError(f"At least {minimum} completed bars are required, received {len(bars)}")
