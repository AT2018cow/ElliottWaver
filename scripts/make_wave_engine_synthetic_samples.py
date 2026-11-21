"""Create deterministic synthetic suspension, split and incomplete-week snapshots."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from eliotbot.data.adjustment import adjust_to_basis
from eliotbot.data.demo import demo_bundle
from eliotbot.data.snapshots import SnapshotStore
from eliotbot.schemas.market import FactorPoint
from eliotbot.util import atomic_write, canonical_json, digest


def _save(store: SnapshotStore, bundle):
    return store.save(bundle)


def build(cache_root: Path) -> dict:
    store = SnapshotStore(cache_root)
    fixed = datetime(2026, 1, 1, tzinfo=UTC)
    records = {}

    base = demo_bundle("600000")
    provenance = base.provenance.model_copy(
        update={"provider": "synthetic_fixture", "fetched_at": fixed}
    )
    bars = tuple(bar.model_copy(update={"observed_at": fixed}) for bar in base.bars)
    raw = tuple(bar.model_copy(update={"observed_at": fixed}) for bar in base.raw_bars)
    suspension = base.model_copy(
        update={
            "instrument": base.instrument.model_copy(
                update={"instrument_id": "fixture:suspension"}
            ),
            "provenance": provenance,
            "bars": bars[:100] + bars[101:],
            "raw_bars": raw[:100] + raw[101:],
        }
    )
    records["suspension"] = {
        "symbol": "600000.SH",
        "asset_type": "EQUITY",
        "source_timeframe": "1d",
        "snapshot_id": _save(store, suspension),
        "case": "one missing internal session",
    }

    split_base = demo_bundle("600001")
    split_raw = tuple(bar.model_copy(update={"observed_at": fixed}) for bar in split_base.raw_bars)
    factors = (
        FactorPoint(effective_date=split_raw[0].session_date, factor=1),
        FactorPoint(effective_date=split_raw[len(split_raw) // 2].session_date, factor=2),
    )
    split_bars = adjust_to_basis(split_raw, factors)
    split = split_base.model_copy(
        update={
            "instrument": split_base.instrument.model_copy(
                update={"instrument_id": "fixture:split"}
            ),
            "bars": split_bars,
            "raw_bars": split_raw,
            "factors": factors,
            "provenance": split_base.provenance.model_copy(
                update={
                    "provider": "synthetic_fixture",
                    "adjustment": "basis_date_qfq",
                    "basis_date": split_bars[-1].session_date,
                    "factor_hash": digest(factors),
                    "price_coordinate_id": digest(["fixture-split", split_bars[-1].session_date]),
                    "fetched_at": fixed,
                }
            ),
        }
    )
    records["split"] = {
        "symbol": "600001.SH",
        "asset_type": "EQUITY",
        "source_timeframe": "1d",
        "snapshot_id": _save(store, split),
        "case": "2:1 split factor in the middle of the sample",
    }

    week_base = demo_bundle("600002")
    week_bars = tuple(bar.model_copy(update={"observed_at": fixed}) for bar in week_base.bars)
    week_raw = tuple(bar.model_copy(update={"observed_at": fixed}) for bar in week_base.raw_bars)
    # Stop on a Wednesday so the current ISO week is incomplete.
    wednesday = max(index for index, bar in enumerate(week_bars) if bar.session_date.weekday() == 2)
    week = week_base.model_copy(
        update={
            "instrument": week_base.instrument.model_copy(update={"instrument_id": "fixture:week"}),
            "bars": week_bars[: wednesday + 1],
            "raw_bars": week_raw[: wednesday + 1],
            "as_of": week_bars[wednesday].usable_at,
            "provenance": week_base.provenance.model_copy(
                update={
                    "provider": "synthetic_fixture",
                    "basis_date": week_bars[wednesday].session_date,
                    "fetched_at": fixed,
                }
            ),
        }
    )
    records["incomplete_week"] = {
        "symbol": "600002.SH",
        "asset_type": "EQUITY",
        "source_timeframe": "1w",
        "snapshot_id": _save(store, week),
        "case": "daily source stops on Wednesday; weekly must use the previous complete week",
    }
    return records


def main() -> int:
    records = build(Path(".cache/eliotbot"))
    atomic_write(
        Path("docs/protocols/wave-engine-synthetic-samples.json"),
        canonical_json({"schema_version": "1.0", "samples": records}) + "\n",
    )
    print(json.dumps(records, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
