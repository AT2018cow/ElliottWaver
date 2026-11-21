"""Create deterministic synthetic K-line snapshots for WE-3 variant acceptance."""

from __future__ import annotations

import json
from datetime import UTC, datetime, time, timedelta
from pathlib import Path

from eliotbot.data.router import route_symbol
from eliotbot.data.snapshots import SnapshotStore
from eliotbot.schemas.market import Bar, MarketDataBundle, Provenance
from eliotbot.util import atomic_write, canonical_json

PATHS = {
    "flat_regular": [130, 100, 140, 101, 141, 130],
    "flat_expanded": [130, 100, 140, 92, 149, 130],
    "flat_running": [130, 100, 140, 92, 127, 116],
    "barrier_triangle": [130, 100, 120, 105, 116, 107, 111, 100],
    "truncated_fifth": [130, 100, 125, 113, 152, 135, 149, 130],
    "contracting_diagonal": [180, 100, 150, 120, 160, 140, 165, 155],
    "wxy_double_three": [
        300,
        500,
        300,
        462,
        262,
        462,
        162,
        300,
        180,
        440,
        150,
        250,
        140,
        354,
    ],
    "ending_diagonal": [
        150.0,
        156.25,
        162.5,
        168.75,
        175.0,
        181.25,
        187.5,
        193.75,
        200.0,
        194.74,
        189.47,
        184.21,
        178.95,
        173.68,
        168.42,
        163.16,
        157.89,
        152.63,
        147.37,
        142.11,
        136.84,
        131.58,
        126.32,
        121.05,
        115.79,
        110.53,
        105.26,
        100.0,
        113.33,
        126.67,
        140.0,
        153.33,
        166.67,
        180.0,
        155,
        170,
        120,
        135,
        125,
        150,
        125,
        140,
        100,
        112,
        105,
        125,
        105,
        118,
        85,
        130,
    ],
    "leading_diagonal": [
        80,
        90,
        100,
        160,
        140,
        200,
        170,
        185,
        140,
        190,
        170,
        220,
        195,
        205,
        170,
        200,
        185,
        230,
        225,
        220,
        215,
        210,
        205,
        200,
        195,
        190,
        185,
        180,
        175,
        170,
        165,
        160,
        155,
        150,
    ],
}

# A fixture may need a different bar density so the parent degree and the
# coarse ZigZag threshold separate the parent leg from its children.
PER = {
    "barrier_triangle": 12,
    "wxy_double_three": 7,
    "ending_diagonal": 8,
    "leading_diagonal": 8,
}

# The leading fixture needs the diagonal start as an explicit anchor; the
# parent impulse starts at the same bar, so both candidates are in scope.
ANCHORS = {"leading_diagonal": 13}


def _bundle(symbol: str, pivots: list[float], per: int = 16) -> MarketDataBundle:
    prices = []
    for left, right in zip(pivots, pivots[1:], strict=False):
        prices.extend(left + (right - left) * index / per for index in range(per))
    prices.append(pivots[-1])
    observed = datetime(2026, 1, 1, tzinfo=UTC)
    bars = []
    day = datetime(2024, 1, 2, tzinfo=UTC).date()
    for price in prices:
        while day.weekday() >= 5:
            day += timedelta(days=1)
        start = datetime.combine(day, time.min, tzinfo=UTC)
        end = start + timedelta(hours=6)
        bars.append(
            Bar(
                start_at=start,
                end_at=end,
                available_at=end,
                session_date=day,
                observed_at=observed,
                open=price,
                high=price + 0.1,
                low=price - 0.1,
                close=price,
                volume=100,
            )
        )
        day += timedelta(days=1)
    instrument = route_symbol(symbol).model_copy(update={"identity_verified": True})
    provenance = Provenance(
        provider="synthetic_fixture",
        feed="synthetic",
        adjustment="none",
        price_coordinate_id=f"fixture-{symbol}",
        basis_date=bars[-1].session_date,
        session_profile="fixture",
        calendar_version="fixture",
        fetched_at=observed,
        requested_start=bars[0].session_date,
        requested_end=bars[-1].session_date,
        amount_currency="USD",
    )
    return MarketDataBundle(
        instrument=instrument,
        as_of=bars[-1].usable_at,
        bars=tuple(bars),
        raw_bars=tuple(bars),
        provenance=provenance,
        sessions=tuple(bar.session_date for bar in bars),
    )


def main() -> int:
    store = SnapshotStore(Path(".cache/wave-engine-variants"))
    records = {}
    for index, (name, pivots) in enumerate(PATHS.items(), 1):
        symbol = f"90{index:04d}.SZ" if index <= 9 else "AAPL"
        bundle = _bundle(symbol, pivots, per=PER.get(name, 16))
        record = {
            "symbol": symbol,
            "snapshot_id": store.save(bundle),
            "pivots": pivots,
        }
        if name in ANCHORS:
            record["anchor"] = bundle.bars[ANCHORS[name]].session_date.isoformat()
        records[name] = record
    atomic_write(
        Path("docs/protocols/wave-engine-variant-fixtures.json"),
        canonical_json({"schema_version": "1.0", "fixtures": records}) + "\n",
    )
    print(json.dumps(records, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
