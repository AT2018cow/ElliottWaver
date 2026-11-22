from collections.abc import Sequence
from datetime import date

from elliottwaver.config import AnalysisConfig
from elliottwaver.data.resample import weekly_bars
from elliottwaver.engine.indicators import calculate
from elliottwaver.engine.zigzag import extract
from elliottwaver.schemas.brief import BriefAnchor
from elliottwaver.schemas.market import AnalysisRequest, Bar, MarketDataBundle


def select_anchors(
    bundle: MarketDataBundle,
    request: AnalysisRequest,
    config: AnalysisConfig,
) -> tuple[tuple[date, ...], tuple[str, ...]]:
    anchors, warnings = select_anchor_details(bundle, request, config)
    return tuple(sorted({item.date for item in anchors})), warnings


def select_anchor_details(
    bundle: MarketDataBundle, request: AnalysisRequest, config: AnalysisConfig
) -> tuple[tuple[BriefAnchor, ...], tuple[str, ...]]:
    bars = bundle.bars
    if request.anchor:
        if request.anchor not in {b.session_date for b in bars}:
            from elliottwaver.errors import InputError

            raise InputError("Explicit anchor must identify an available bar")
        return (BriefAnchor(date=request.anchor, origin="EXPLICIT"),), ()
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
    eligible: dict[str, list[BriefAnchor]] = {"LOW": [], "HIGH": []}
    if len(weekly) >= 15:
        zz = extract(weekly, calculate(weekly).atr, config.macro_scale, bundle.instrument.tick_size)
        for pivot in zz.confirmed:
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
            if index >= earliest and config.min_anchor_bars <= age <= config.max_analysis_bars:
                eligible[pivot.kind].append(
                    BriefAnchor(
                        date=daily.session_date,
                        origin="MACRO_LOW" if pivot.kind == "LOW" else "MACRO_HIGH",
                        background_timeframe={"1d": "1w", "1w": "1mo"}.get(bundle.timeframe, "1d"),
                        confirmed_at=pivot.confirmed_at,
                    )
                )
    limit = max(1, config.max_anchors // 2)

    def choose(items: list[BriefAnchor]) -> list[BriefAnchor]:
        if len(items) <= limit:
            return items
        if config.anchor_mode == "spread":
            if limit == 1:
                return items[-1:]
            step = (len(items) - 1) / (limit - 1)
            return [items[round(index * step)] for index in range(limit)]
        return items[-limit:]

    anchors = choose(eligible["LOW"]) + choose(eligible["HIGH"])
    if anchors:
        days = sorted({anchor.date for anchor in anchors})[: config.max_anchors]
        return tuple(
            sorted(
                {anchor for anchor in anchors if anchor.date in days},
                key=lambda anchor: (anchor.date, anchor.origin, anchor.confirmed_at),
            )
        ), ()
    fallback = max(earliest, len(bars) - 252)
    return (BriefAnchor(date=bars[fallback].session_date, origin="FALLBACK"),), (
        "ANCHOR_FALLBACK: 未找到已确认的合格背景周期锚点，使用明确标记的252根K线窗口",
    )


def require_history(bars: Sequence[Bar], minimum: int = 60) -> None:
    if len(bars) < minimum:
        from elliottwaver.errors import DataError

        raise DataError(f"At least {minimum} completed bars are required, received {len(bars)}")
