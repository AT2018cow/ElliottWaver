"""Reusable source-block cache for intraday bars.

Blocks are stored per trading session and source timeframe. The cache keeps
immutable revisions so a refreshed bar does not erase the earlier evidence.
The final analysis bundle is assembled from the latest revision of each block.
"""

from __future__ import annotations

import json
import sqlite3
from collections import defaultdict
from contextlib import closing
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path

from elliottwaver.data.adjustment import adjust_to_basis
from elliottwaver.data.calendar import TradingCalendar
from elliottwaver.data.periods import aggregate_intraday, expected_starts
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


class SourceBlockStore:
    def __init__(self, root: Path):
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)
        self.path = self.root / "source-cache.sqlite3"
        with closing(sqlite3.connect(self.path)) as db:
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
                    updated_at TEXT NOT NULL
                )
                """
            )
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

    def coverage(self, source_key: str) -> tuple[date | None, date | None]:
        blocks = self.latest_blocks(source_key)
        if not blocks:
            return None, None
        return min(blocks), max(blocks)

    def revision_count(self, source_key: str, session: date | None = None) -> int:
        query = "SELECT COUNT(*) FROM source_blocks WHERE source_key = ?"
        values: list[object] = [source_key]
        if session is not None:
            query += " AND session_date = ?"
            values.append(session.isoformat())
        with closing(sqlite3.connect(self.path)) as db:
            return int(db.execute(query, values).fetchone()[0])

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
    ) -> int:
        bars_by_session: dict[date, list[Bar]] = defaultdict(list)
        raw_by_session: dict[date, list[Bar]] = defaultdict(list)
        for bar in bundle.bars:
            bars_by_session[bar.session_date].append(bar)
        for bar in bundle.raw_bars:
            raw_by_session[bar.session_date].append(bar)
        targets = sessions or tuple(sorted(set(bars_by_session) | set(raw_by_session)))
        inserted = 0
        now = datetime.now(bundle.as_of.tzinfo).isoformat()
        with closing(sqlite3.connect(self.path)) as db:
            db.execute(
                """
                INSERT INTO source_meta
                (source_key, instrument_json, provenance_json, source_timeframe, feed,
                 adjustment, session_profile, parser_version, calendar_version,
                 first_session, last_session, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                    min(targets).isoformat() if targets else None,
                    max(targets).isoformat() if targets else None,
                    now,
                ),
            )
            for session in sorted(targets):
                bars = tuple(sorted(bars_by_session.get(session, ()), key=lambda b: b.start_at))
                raw = tuple(sorted(raw_by_session.get(session, ()), key=lambda b: b.start_at))
                factors = tuple(
                    point for point in bundle.factors if point.effective_date <= session
                )
                block_hash = digest(
                    [
                        [bar.model_dump(mode="json") for bar in bars],
                        [bar.model_dump(mode="json") for bar in raw],
                        [point.model_dump(mode="json") for point in factors],
                    ]
                )
                latest = db.execute(
                    """
                    SELECT revision, block_hash FROM source_blocks
                    WHERE source_key = ? AND session_date = ?
                    ORDER BY revision DESC LIMIT 1
                    """,
                    (source_key, session.isoformat()),
                ).fetchone()
                if latest is not None and latest[1] == block_hash:
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
            if bundle.daily_context or bundle.raw_daily_context:
                context_end = bundle.bars[-1].session_date.isoformat()
                db.execute(
                    """
                    INSERT OR REPLACE INTO source_context
                    (source_key, context_end, raw_json, adjusted_json, fetched_at)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        source_key,
                        context_end,
                        canonical_json(list(bundle.raw_daily_context)),
                        canonical_json(list(bundle.daily_context)),
                        now,
                    ),
                )
            db.commit()
        return inserted

    def _context(
        self, source_key: str, end: date, as_of: datetime
    ) -> tuple[tuple[Bar, ...], tuple[Bar, ...]]:
        with closing(sqlite3.connect(self.path)) as db:
            row = db.execute(
                """
                SELECT raw_json, adjusted_json FROM source_context
                WHERE source_key = ? AND context_end <= ?
                ORDER BY context_end DESC LIMIT 1
                """,
                (source_key, end.isoformat()),
            ).fetchone()
        if row is None:
            return (), ()
        raw = tuple(
            bar
            for bar in (Bar.model_validate(item) for item in json.loads(row[0]))
            if bar.usable_at <= as_of
        )
        adjusted = tuple(
            bar
            for bar in (Bar.model_validate(item) for item in json.loads(row[1]))
            if bar.usable_at <= as_of
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
        if adjustment == "basis_date_qfq":
            try:
                bars = adjust_to_basis(raw, factors_tuple, basis_date=raw[-1].session_date)
            except Exception:
                return None
        if timeframe != source_timeframe:
            bars = aggregate_intraday(
                bars,
                calendar,
                instrument,
                timeframe,
                as_of,
                buffer_minutes=0,
            )
            raw = aggregate_intraday(
                raw,
                calendar,
                instrument,
                timeframe,
                as_of,
                buffer_minutes=0,
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
                buffer_minutes=0,
                through=end,
                since=start,
            )
            proposed = tuple(item for item in proposed if item <= bars[-1].start_at)
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
            except Exception:
                return None
        aggregation = (
            "none" if timeframe == source_timeframe else f"{source_timeframe}->{timeframe}"
        )
        provenance = provenance.model_copy(
            update={
                "basis_date": bars[-1].session_date,
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
