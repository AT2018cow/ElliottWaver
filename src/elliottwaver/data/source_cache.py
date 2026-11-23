"""Reusable source-block cache for intraday bars.

Blocks are stored per trading session and source timeframe. The cache keeps
immutable revisions so a refreshed bar does not erase the earlier evidence.
The final analysis bundle is assembled from the latest revision of each block.
"""

from __future__ import annotations

import fcntl
import json
import math
import sqlite3
from collections import defaultdict
from contextlib import closing, contextmanager
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path

from elliottwaver.data.adjustment import adjust_to_basis
from elliottwaver.data.calendar import TradingCalendar
from elliottwaver.data.periods import aggregate_intraday, expected_starts, uses_sparse_sina_auction
from elliottwaver.errors import DataError
from elliottwaver.schemas.market import (
    Bar,
    FactorPoint,
    Instrument,
    MarketDataBundle,
    Provenance,
)
from elliottwaver.timeframes import minutes
from elliottwaver.util import canonical_json, digest


@dataclass(frozen=True)
class SourceBlock:
    session_date: date
    revision: int
    block_hash: str
    bars: tuple[Bar, ...]
    raw_bars: tuple[Bar, ...]
    factors: tuple[FactorPoint, ...]
    fetched_at: datetime
    cutoff: datetime


def _same_provider_scale(raw: Bar, adjusted: Bar, new_raw: Bar, new_adjusted: Bar) -> bool:
    return all(
        math.isclose(
            getattr(adjusted, field) / getattr(raw, field),
            getattr(new_adjusted, field) / getattr(new_raw, field),
            rel_tol=1e-8,
            abs_tol=0,
        )
        for field in ("open", "high", "low", "close")
    )


def compatible_provider_basis(
    blocks: dict[date, SourceBlock], bundle: MarketDataBundle, start: date, end: date
) -> bool:
    """Check provider-adjusted overlap before combining retained and revised sessions."""
    if bundle.provenance.adjustment != "provider_all":
        return True
    revised = {
        (bar.start_at, bar.end_at): (raw, bar)
        for raw, bar in zip(bundle.raw_bars, bundle.bars, strict=True)
    }
    kept = overlap = False
    changed = False
    for day, block in blocks.items():
        if not start <= day <= end:
            continue
        pairs = [
            (raw, bar, revised.get((bar.start_at, bar.end_at)))
            for raw, bar in zip(block.raw_bars, block.bars, strict=True)
            if bar.usable_at <= bundle.as_of
        ]
        # A partial response keeps the entire old session block.
        kept |= any(pair is None for _, _, pair in pairs)
        for raw, bar, pair in pairs:
            if pair is None:
                continue
            overlap = True
            new_raw, new_bar = pair
            changed |= not _same_provider_scale(raw, bar, new_raw, new_bar)
    if kept and (changed or not overlap):
        return False
    return True


class SourceBlockStore:
    def __init__(self, root: Path):
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)
        self.path = self.root / "source-cache.sqlite3"
        with self.lock("init"), closing(sqlite3.connect(self.path)) as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute(
                """
                CREATE TABLE IF NOT EXISTS source_meta (
                    source_key TEXT PRIMARY KEY,
                    instrument_json TEXT NOT NULL,
                    provenance_json TEXT NOT NULL,
                    source_timeframe TEXT NOT NULL,
                    feed TEXT NOT NULL,
                    adjustment TEXT NOT NULL,
                    session_profile TEXT NOT NULL,
                    parser_version TEXT NOT NULL,
                    calendar_version TEXT NOT NULL,
                    first_session TEXT,
                    last_session TEXT,
                    requested_start TEXT,
                    updated_at TEXT NOT NULL
                )
                """
            )
            meta_columns = {row[1] for row in db.execute("PRAGMA table_info(source_meta)")}
            if "requested_start" not in meta_columns:
                db.execute("ALTER TABLE source_meta ADD COLUMN requested_start TEXT")
            db.execute(
                """
                CREATE TABLE IF NOT EXISTS source_blocks (
                    source_key TEXT NOT NULL,
                    session_date TEXT NOT NULL,
                    revision INTEGER NOT NULL,
                    block_hash TEXT NOT NULL,
                    bars_json TEXT NOT NULL,
                    raw_bars_json TEXT NOT NULL,
                    factors_json TEXT NOT NULL,
                    fetched_at TEXT NOT NULL,
                    cutoff TEXT NOT NULL,
                    PRIMARY KEY (source_key, session_date, revision)
                )
                """
            )
            columns = {row[1] for row in db.execute("PRAGMA table_info(source_blocks)")}
            if "cutoff" not in columns:
                db.execute("ALTER TABLE source_blocks ADD COLUMN cutoff TEXT")
                db.execute("UPDATE source_blocks SET cutoff = fetched_at WHERE cutoff IS NULL")
            db.execute(
                """
                CREATE TABLE IF NOT EXISTS source_failures (
                    source_key TEXT NOT NULL,
                    session_date TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    fetched_at TEXT NOT NULL,
                    PRIMARY KEY (source_key, session_date, reason)
                )
                """
            )
            db.execute(
                """
                CREATE TABLE IF NOT EXISTS source_context (
                    source_key TEXT NOT NULL,
                    context_end TEXT NOT NULL,
                    raw_json TEXT NOT NULL,
                    adjusted_json TEXT NOT NULL,
                    fetched_at TEXT NOT NULL,
                    PRIMARY KEY (source_key, context_end)
                )
                """
            )
            db.commit()

    @contextmanager
    def lock(self, source_key: str):
        path = self.root / f"source-{source_key}.lock"
        with path.open("a") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle, fcntl.LOCK_UN)

    def latest_blocks(self, source_key: str) -> dict[date, SourceBlock]:
        with closing(sqlite3.connect(self.path)) as db:
            rows = db.execute(
                """
                SELECT session_date, revision, block_hash, bars_json, raw_bars_json,
                       factors_json, fetched_at, cutoff
                FROM source_blocks b
                WHERE source_key = ?
                AND revision = (
                    SELECT MAX(revision) FROM source_blocks x
                    WHERE x.source_key = b.source_key AND x.session_date = b.session_date
                )
                """,
                (source_key,),
            ).fetchall()
        result = {}
        for session, revision, block_hash, bars, raw, factors, fetched, cutoff in rows:
            result[date.fromisoformat(session)] = SourceBlock(
                date.fromisoformat(session),
                revision,
                block_hash,
                tuple(Bar.model_validate(item) for item in json.loads(bars)),
                tuple(Bar.model_validate(item) for item in json.loads(raw)),
                tuple(FactorPoint.model_validate(item) for item in json.loads(factors)),
                datetime.fromisoformat(fetched),
                datetime.fromisoformat(cutoff),
            )
        return result

    def record_failure(self, source_key: str, sessions: tuple[date, ...], reason: str) -> None:
        now = datetime.now(UTC).isoformat()
        with closing(sqlite3.connect(self.path)) as db:
            self._record_failures(db, source_key, sessions, reason, now)
            db.commit()

    @staticmethod
    def _record_failures(db, source_key, sessions, reason, now) -> None:
        db.executemany(
            """
            INSERT OR REPLACE INTO source_failures
            (source_key, session_date, reason, fetched_at)
            VALUES (?, ?, ?, ?)
            """,
            [(source_key, session.isoformat(), reason[:500], now) for session in sessions],
        )

    def failures(self, source_key: str) -> tuple[dict, ...]:
        with closing(sqlite3.connect(self.path)) as db:
            db.row_factory = sqlite3.Row
            rows = db.execute(
                "SELECT * FROM source_failures WHERE source_key = ? ORDER BY session_date",
                (source_key,),
            ).fetchall()
        return tuple(dict(row) for row in rows)

    def coverage(self, source_key: str) -> tuple[date | None, date | None]:
        blocks = self.latest_blocks(source_key)
        if not blocks:
            return None, None
        return min(blocks), max(blocks)

    def requested_start(self, source_key: str) -> date | None:
        with closing(sqlite3.connect(self.path)) as db:
            row = db.execute(
                "SELECT requested_start FROM source_meta WHERE source_key = ?", (source_key,)
            ).fetchone()
        return date.fromisoformat(row[0]) if row and row[0] else None

    def context_through(self, source_key: str) -> date | None:
        with closing(sqlite3.connect(self.path)) as db:
            end = db.execute(
                "SELECT MAX(context_end) FROM source_context WHERE source_key = ?", (source_key,)
            ).fetchone()[0]
        return date.fromisoformat(end) if end else None

    def revision_count(self, source_key: str, session: date | None = None) -> int:
        query = "SELECT COUNT(*) FROM source_blocks WHERE source_key = ?"
        values: list[object] = [source_key]
        if session is not None:
            query += " AND session_date = ?"
            values.append(session.isoformat())
        with closing(sqlite3.connect(self.path)) as db:
            return int(db.execute(query, values).fetchone()[0])

    def revision_token(self, source_key: str, start: date, end: date, as_of: datetime) -> str:
        """Hash the current blocks and only the daily context available at this cutoff."""
        with closing(sqlite3.connect(self.path)) as db:
            blocks = db.execute(
                """
                SELECT session_date, block_hash FROM source_blocks b
                WHERE source_key = ? AND session_date BETWEEN ? AND ?
                  AND revision = (
                    SELECT MAX(revision) FROM source_blocks x
                    WHERE x.source_key = b.source_key AND x.session_date = b.session_date
                  )
                ORDER BY session_date
                """,
                (source_key, start.isoformat(), end.isoformat()),
            ).fetchall()
            context = self._context_row(db, source_key)
        visible_context = [
            [
                {key: value for key, value in row.items() if key != "observed_at"}
                for row in json.loads(series)
                if date.fromisoformat(row["session_date"]) <= end
                and datetime.fromisoformat(row["available_at"] or row["end_at"]) <= as_of
            ]
            for series in context or ()
        ]
        return digest([blocks, visible_context if any(visible_context) else ()])

    def store_bundle(
        self,
        source_key: str,
        bundle: MarketDataBundle,
        *,
        sessions: tuple[date, ...] | None = None,
        source_timeframe: str,
        session_profile: str,
        parser_version: str,
        calendar_version: str,
        requested_start: date | None = None,
    ) -> int:
        bars_by_session: dict[date, list[Bar]] = defaultdict(list)
        raw_by_session: dict[date, list[Bar]] = defaultdict(list)
        for bar in bundle.bars:
            bars_by_session[bar.session_date].append(bar)
        for bar in bundle.raw_bars:
            raw_by_session[bar.session_date].append(bar)
        targets = sessions if sessions is not None else tuple(sorted(bars_by_session))
        covered = tuple(session for session in targets if bars_by_session.get(session))
        inserted = 0
        now = datetime.now(UTC).isoformat()
        requested = (requested_start or bundle.provenance.requested_start).isoformat()
        daily, adjusted_daily = (), ()
        if bundle.daily_context:
            old_raw, old_adjusted = self._context(
                source_key, date.max, datetime.max.replace(tzinfo=UTC)
            )
            if old_raw and bundle.provenance.adjustment == "provider_all":
                old = {
                    bar.start_at: (raw, bar) for raw, bar in zip(old_raw, old_adjusted, strict=True)
                }
                overlaps = [
                    (*old[bar.start_at], raw, bar)
                    for raw, bar in zip(bundle.raw_daily_context, bundle.daily_context, strict=True)
                    if bar.start_at in old
                ]
                if not overlaps or not all(_same_provider_scale(*pair) for pair in overlaps):
                    old_raw, old_adjusted = (), ()

            def merge(previous, incoming):
                by_start = {bar.start_at: bar for bar in (*previous, *incoming)}
                return tuple(by_start[start] for start in sorted(by_start))

            daily = merge(old_raw, bundle.raw_daily_context)
            adjusted_daily = merge(old_adjusted, bundle.daily_context)
        with closing(sqlite3.connect(self.path)) as db:
            previous = db.execute(
                "SELECT instrument_json FROM source_meta WHERE source_key = ?", (source_key,)
            ).fetchone()
            if previous and (
                Instrument.model_validate_json(previous[0]).instrument_id
                != bundle.instrument.instrument_id
            ):
                raise DataError("ASSET_IDENTITY_MISMATCH: source-block asset identity changed")
            db.execute(
                """
                INSERT INTO source_meta
                (source_key, instrument_json, provenance_json, source_timeframe, feed,
                 adjustment, session_profile, parser_version, calendar_version,
                 first_session, last_session, requested_start, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(source_key) DO UPDATE SET
                    instrument_json=excluded.instrument_json,
                    provenance_json=excluded.provenance_json,
                    source_timeframe=excluded.source_timeframe,
                    feed=excluded.feed,
                    adjustment=excluded.adjustment,
                    session_profile=excluded.session_profile,
                    parser_version=excluded.parser_version,
                    calendar_version=excluded.calendar_version,
                    first_session=CASE
                        WHEN source_meta.first_session IS NULL THEN excluded.first_session
                        WHEN excluded.first_session IS NULL THEN source_meta.first_session
                        ELSE MIN(source_meta.first_session, excluded.first_session)
                    END,
                    last_session=CASE
                        WHEN source_meta.last_session IS NULL THEN excluded.last_session
                        WHEN excluded.last_session IS NULL THEN source_meta.last_session
                        ELSE MAX(source_meta.last_session, excluded.last_session)
                    END,
                    requested_start=MIN(COALESCE(source_meta.requested_start, excluded.requested_start),
                                        excluded.requested_start),
                    updated_at=excluded.updated_at
                """,
                (
                    source_key,
                    canonical_json(bundle.instrument),
                    canonical_json(bundle.provenance),
                    source_timeframe,
                    bundle.provenance.feed,
                    bundle.provenance.adjustment,
                    session_profile,
                    parser_version,
                    calendar_version,
                    min(covered).isoformat() if covered else None,
                    max(covered).isoformat() if covered else None,
                    requested,
                    now,
                ),
            )
            for session in sorted(targets):
                bars = tuple(sorted(bars_by_session.get(session, ()), key=lambda b: b.start_at))
                raw = tuple(sorted(raw_by_session.get(session, ()), key=lambda b: b.start_at))
                if not bars or [(b.start_at, b.end_at) for b in bars] != [
                    (b.start_at, b.end_at) for b in raw
                ]:
                    self._record_failures(
                        db, source_key, (session,), "PARTIAL_PROVIDER_COVERAGE", now
                    )
                    continue
                factors = tuple(
                    point for point in bundle.factors if point.effective_date <= session
                )
                block_hash = digest(
                    [
                        [bar.bar_id for bar in bars],
                        [bar.bar_id for bar in raw],
                        [point.model_dump(mode="json") for point in factors],
                    ]
                )
                latest = db.execute(
                    """
                    SELECT revision, block_hash, bars_json FROM source_blocks
                    WHERE source_key = ? AND session_date = ?
                    ORDER BY revision DESC LIMIT 1
                    """,
                    (source_key, session.isoformat()),
                ).fetchone()
                if latest is not None:
                    previous_slots = {
                        (
                            datetime.fromisoformat(bar["start_at"]),
                            datetime.fromisoformat(bar["end_at"]),
                        )
                        for bar in json.loads(latest[2])
                    }
                    received_slots = {(bar.start_at, bar.end_at) for bar in bars}
                    if not previous_slots <= received_slots:
                        self._record_failures(
                            db, source_key, (session,), "PARTIAL_PROVIDER_COVERAGE", now
                        )
                        continue
                if latest is not None and latest[1] == block_hash:
                    db.execute(
                        "UPDATE source_blocks SET cutoff = MAX(cutoff, ?) "
                        "WHERE source_key = ? AND session_date = ? AND revision = ?",
                        (bundle.as_of.isoformat(), source_key, session.isoformat(), latest[0]),
                    )
                    continue
                revision = (latest[0] + 1) if latest is not None else 1
                db.execute(
                    """
                    INSERT INTO source_blocks
                    (source_key, session_date, revision, block_hash, bars_json,
                     raw_bars_json, factors_json, fetched_at, cutoff)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        source_key,
                        session.isoformat(),
                        revision,
                        block_hash,
                        canonical_json(list(bars)),
                        canonical_json(list(raw)),
                        canonical_json(list(factors)),
                        now,
                        bundle.as_of.isoformat(),
                    ),
                )
                inserted += 1
            if adjusted_daily:
                context_end = adjusted_daily[-1].session_date.isoformat()
                db.execute(
                    """
                    INSERT OR REPLACE INTO source_context
                    (source_key, context_end, raw_json, adjusted_json, fetched_at)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        source_key,
                        context_end,
                        canonical_json(list(daily)),
                        canonical_json(list(adjusted_daily)),
                        now,
                    ),
                )
            db.commit()
        return inserted

    @staticmethod
    def _context_row(db, source_key: str) -> tuple[str, str] | None:
        return db.execute(
            """
            SELECT raw_json, adjusted_json FROM source_context
            WHERE source_key = ?
            ORDER BY context_end DESC LIMIT 1
            """,
            (source_key,),
        ).fetchone()

    def _context(
        self, source_key: str, end: date, as_of: datetime
    ) -> tuple[tuple[Bar, ...], tuple[Bar, ...]]:
        with closing(sqlite3.connect(self.path)) as db:
            row = self._context_row(db, source_key)
        if row is None:
            return (), ()
        raw = tuple(
            bar
            for bar in (Bar.model_validate(item) for item in json.loads(row[0]))
            if bar.session_date <= end and bar.usable_at <= as_of
        )
        adjusted = tuple(
            bar
            for bar in (Bar.model_validate(item) for item in json.loads(row[1]))
            if bar.session_date <= end and bar.usable_at <= as_of
        )
        return raw, adjusted

    def assemble(
        self,
        source_key: str,
        *,
        instrument: Instrument,
        calendar: TradingCalendar,
        timeframe: str,
        source_timeframe: str,
        as_of: datetime,
        start: date,
        end: date,
        buffer_minutes: int,
    ) -> MarketDataBundle | None:
        blocks = self.latest_blocks(source_key)
        selected = [block for session, block in sorted(blocks.items()) if start <= session <= end]
        if not selected:
            return None
        bars = tuple(bar for block in selected for bar in block.bars if bar.usable_at <= as_of)
        raw = tuple(bar for block in selected for bar in block.raw_bars if bar.usable_at <= as_of)
        factors = {point.effective_date: point for block in selected for point in block.factors}
        factors_tuple = tuple(factors[key] for key in sorted(factors))
        if not bars or not raw:
            return None
        with closing(sqlite3.connect(self.path)) as db:
            meta = db.execute(
                "SELECT instrument_json, provenance_json, adjustment FROM source_meta "
                "WHERE source_key = ?",
                (source_key,),
            ).fetchone()
        if meta is None:
            return None
        stored_instrument = Instrument.model_validate(json.loads(meta[0]))
        if (
            stored_instrument.canonical_symbol == instrument.canonical_symbol
            and stored_instrument.market == instrument.market
        ):
            instrument = stored_instrument
        provenance = Provenance.model_validate(json.loads(meta[1]))
        adjustment = meta[2]
        auction_sparse = uses_sparse_sina_auction(instrument, source_timeframe, provenance.feed)
        if adjustment == "basis_date_qfq":
            try:
                bars = adjust_to_basis(raw, factors_tuple, basis_date=raw[-1].session_date)
            except DataError:
                return None
        if timeframe != source_timeframe:
            bars = aggregate_intraday(
                bars,
                calendar,
                instrument,
                timeframe,
                as_of,
                buffer_minutes=buffer_minutes,
                auction_sparse=auction_sparse and source_timeframe == "1m",
            )
            raw = aggregate_intraday(
                raw,
                calendar,
                instrument,
                timeframe,
                as_of,
                buffer_minutes=buffer_minutes,
                auction_sparse=auction_sparse and source_timeframe == "1m",
            )
        if not bars or len(bars) != len(raw):
            return None
        duration = minutes(timeframe)
        expected: tuple[datetime, ...] = ()
        if duration is not None:
            proposed = expected_starts(
                calendar,
                instrument,
                as_of,
                timeframe,
                buffer_minutes=buffer_minutes,
                through=end,
                since=start,
                auction_sparse=auction_sparse,
            )
            if proposed and proposed[-1] == bars[-1].start_at:
                expected = proposed
            else:
                return None
        raw_context, adjusted_context = self._context(source_key, end, as_of)
        if adjustment == "basis_date_qfq" and raw_context:
            try:
                adjusted_context = adjust_to_basis(
                    raw_context, factors_tuple, basis_date=bars[-1].session_date
                )
            except DataError:
                return None
        aggregation = (
            "none" if timeframe == source_timeframe else f"{source_timeframe}->{timeframe}"
        )
        provenance = provenance.model_copy(
            update={
                "basis_date": bars[-1].session_date,
                "calendar_version": calendar.version,
                "requested_start": start,
                "requested_end": end,
                "source_timeframe": source_timeframe,
                "aggregation": aggregation,
                "coverage_start": bars[0].session_date,
                "coverage_end": bars[-1].session_date,
                "coverage_reason": "SOURCE_CACHE_HIT",
                "price_coordinate_id": digest(
                    ["source-cache", source_key, timeframe, [bar.bar_id for bar in bars]]
                ),
                "warnings": (*provenance.warnings, "SOURCE_CACHE_HIT"),
            }
        )
        return MarketDataBundle(
            instrument=instrument,
            timeframe=timeframe,
            as_of=as_of,
            bars=bars,
            raw_bars=raw,
            factors=factors_tuple,
            provenance=provenance,
            sessions=calendar.dates,
            auxiliary=(),
            daily_context=adjusted_context,
            raw_daily_context=raw_context,
            expected_bar_starts=expected,
        )
