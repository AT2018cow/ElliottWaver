"""Recompute each historical prefix; future outcomes are attached afterwards."""

from collections.abc import Iterator
from datetime import date

from elliottwaver.schemas.market import AnalysisRequest, MarketDataBundle
from elliottwaver.schemas.wave import AnalysisResult
from elliottwaver.service import AnalysisService


def replay(
    bundle: MarketDataBundle,
    service: AnalysisService,
    *,
    start: date | None = None,
    end: date | None = None,
    step: int = 1,
    allow_gaps: bool = False,
) -> Iterator[AnalysisResult]:
    if step < 1:
        raise ValueError("replay step must be positive")
    eligible = [
        b
        for i, b in enumerate(bundle.bars)
        if i >= 59
        and (start is None or b.session_date >= start)
        and (end is None or b.session_date <= end)
    ]
    for bar in eligible[::step]:
        request = AnalysisRequest(
            symbol=bundle.instrument.canonical_symbol,
            as_of=bar.usable_at,
            end=bar.session_date,
            mode="FROZEN_DATA_REPLAY",
            timeframe=bundle.timeframe,
            allow_gaps=allow_gaps,
        )
        yield service.analyze_bundle(request, bundle)
