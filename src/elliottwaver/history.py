"""Local SQLite index of verified brief directories."""

from __future__ import annotations

import fcntl
import sqlite3
from contextlib import closing
from datetime import date
from pathlib import Path

from elliottwaver.schemas.brief import Brief
from elliottwaver.util import digest
from elliottwaver.visualization.brief_formatter import read_brief_report


def lineage_key(brief: Brief) -> str:
    return digest(
        [
            brief.symbol,
            brief.feed,
            brief.adjustment,
            brief.timeframe,
            brief.config_hash,
            brief.engine_version,
        ]
    )


def base_lineage_key(brief: Brief) -> str:
    """Stable symbol identity; config/feed/adjustment/timeframe changes remain comparable."""
    return digest([brief.symbol, "retrospective"] if brief.retrospective else [brief.symbol])


class HistoryStore:
    def __init__(self, root: Path):
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)
        self.path = self.root / "history.sqlite3"
        with (self.root / "history.init.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            with closing(sqlite3.connect(self.path)) as db:
                if db.execute("PRAGMA journal_mode").fetchone()[0] != "wal":
                    db.execute("PRAGMA journal_mode=WAL")
                db.execute(
                    """
                    CREATE TABLE IF NOT EXISTS briefs (
                        brief_digest TEXT PRIMARY KEY,
                        lineage_key TEXT NOT NULL,
                        base_lineage_key TEXT,
                        symbol TEXT NOT NULL,
                        market_asof TEXT NOT NULL,
                        as_of TEXT NOT NULL,
                        observed_at TEXT NOT NULL,
                        source_snapshot_id TEXT NOT NULL,
                        report_path TEXT NOT NULL,
                        mode TEXT NOT NULL,
                        timeframe TEXT,
                        feed TEXT,
                        adjustment TEXT,
                        last_bar_end_at TEXT,
                        last_bar_id TEXT,
                        last_bar_available_at TEXT
                    )
                    """
                )
                db.execute(
                    "CREATE TABLE IF NOT EXISTS history_meta "
                    "(key TEXT PRIMARY KEY, value TEXT NOT NULL)"
                )
                columns = {row[1] for row in db.execute("PRAGMA table_info(briefs)")}
                if "base_lineage_key" not in columns:
                    db.execute("ALTER TABLE briefs ADD COLUMN base_lineage_key TEXT")
                for column in (
                    "timeframe",
                    "feed",
                    "adjustment",
                    "last_bar_end_at",
                    "last_bar_id",
                    "last_bar_available_at",
                ):
                    if column not in columns:
                        db.execute(f"ALTER TABLE briefs ADD COLUMN {column} TEXT")
                self._migrate_base_lineage(db)
                self._migrate_intraday_columns(db)
                db.execute(
                    "CREATE INDEX IF NOT EXISTS brief_lineage "
                    "ON briefs(lineage_key, market_asof, observed_at)"
                )
                db.execute(
                    "CREATE INDEX IF NOT EXISTS brief_base_lineage "
                    "ON briefs(base_lineage_key, market_asof, observed_at)"
                )
                db.execute(
                    "CREATE INDEX IF NOT EXISTS brief_last_bar "
                    "ON briefs(base_lineage_key, timeframe, last_bar_end_at, observed_at)"
                )
                db.commit()

    @staticmethod
    def _migrate_base_lineage(db: sqlite3.Connection) -> None:
        row = db.execute(
            "SELECT value FROM history_meta WHERE key = 'base_lineage_version'"
        ).fetchone()
        current = row[0] if row else None
        if current == "2":
            rows = db.execute(
                "SELECT brief_digest, report_path, lineage_key FROM briefs "
                "WHERE base_lineage_key IS NULL"
            ).fetchall()
        else:
            rows = db.execute(
                "SELECT brief_digest, report_path, lineage_key FROM briefs"
            ).fetchall()
        for brief_digest, report_path, old_key in rows:
            try:
                brief = Brief.model_validate_json((Path(report_path) / "brief.json").read_text())
                value = base_lineage_key(brief)
            except (OSError, ValueError):
                value = old_key
            db.execute(
                "UPDATE briefs SET base_lineage_key = ? WHERE brief_digest = ?",
                (value, brief_digest),
            )
        if current != "2":
            db.execute(
                "INSERT OR REPLACE INTO history_meta (key, value) VALUES ('base_lineage_version', '2')"
            )

    @staticmethod
    def _migrate_intraday_columns(db: sqlite3.Connection) -> None:
        rows = db.execute(
            "SELECT brief_digest, report_path FROM briefs WHERE timeframe IS NULL"
        ).fetchall()
        for brief_digest, report_path in rows:
            try:
                brief = Brief.model_validate_json((Path(report_path) / "brief.json").read_text())
            except (OSError, ValueError):
                continue
            db.execute(
                """
                UPDATE briefs SET timeframe = ?, feed = ?, adjustment = ?,
                    last_bar_end_at = ?, last_bar_id = ?, last_bar_available_at = ?
                WHERE brief_digest = ?
                """,
                (
                    brief.timeframe,
                    brief.feed,
                    brief.adjustment,
                    brief.last_bar_end_at.isoformat() if brief.last_bar_end_at else None,
                    brief.last_bar_id,
                    brief.last_bar_available_at.isoformat()
                    if brief.last_bar_available_at
                    else None,
                    brief_digest,
                ),
            )
        db.commit()

    def register(self, brief: Brief, report_path: Path) -> None:
        report_path = report_path.resolve()
        brief, _ = read_brief_report(report_path, expected_digest=brief.brief_digest)
        with closing(sqlite3.connect(self.path)) as db:
            db.execute(
                """
                INSERT INTO briefs
                (brief_digest, lineage_key, base_lineage_key, symbol, market_asof, as_of,
                 observed_at, source_snapshot_id, report_path, mode, timeframe, feed,
                 adjustment, last_bar_end_at, last_bar_id, last_bar_available_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(brief_digest) DO NOTHING
                """,
                (
                    brief.brief_digest,
                    lineage_key(brief),
                    base_lineage_key(brief),
                    brief.symbol,
                    brief.market_asof.isoformat(),
                    brief.as_of.isoformat(),
                    brief.generated_at.isoformat(),
                    brief.source_snapshot_id,
                    str(report_path),
                    brief.mode,
                    brief.timeframe,
                    brief.feed,
                    brief.adjustment,
                    brief.last_bar_end_at.isoformat() if brief.last_bar_end_at else None,
                    brief.last_bar_id,
                    brief.last_bar_available_at.isoformat()
                    if brief.last_bar_available_at
                    else None,
                ),
            )
            db.commit()

    def query(
        self,
        symbol: str,
        *,
        as_of: date | None = None,
        mode: str | None = None,
        timeframe: str | None = None,
        limit: int = 50,
    ) -> list[dict]:
        clauses = ["symbol = ?"]
        values: list[object] = [symbol]
        if as_of is not None:
            clauses.append("market_asof <= ?")
            values.append(as_of.isoformat())
        if mode is not None:
            clauses.append("mode = ?")
            values.append(mode)
        if timeframe is not None:
            clauses.append("timeframe = ?")
            values.append(timeframe)
        values.append(limit)
        with closing(sqlite3.connect(self.path)) as db:
            db.row_factory = sqlite3.Row
            rows = db.execute(
                "SELECT * FROM briefs WHERE "
                + " AND ".join(clauses)
                + " ORDER BY COALESCE(last_bar_end_at, market_asof) DESC, observed_at DESC "
                "LIMIT ?",
                values,
            ).fetchall()
        return [dict(row) for row in rows]

    def previous(self, brief: Brief) -> dict | None:
        last_bar = brief.last_bar_end_at.isoformat() if brief.last_bar_end_at else None
        current_marker = last_bar or f"{brief.market_asof.isoformat()}T00:00:00+00:00"
        with closing(sqlite3.connect(self.path)) as db:
            db.row_factory = sqlite3.Row
            row = db.execute(
                """
                SELECT * FROM briefs
                WHERE base_lineage_key = ? AND mode = ? AND timeframe = ?
                AND brief_digest != ?
                AND (feed IS NULL OR feed = ?)
                AND (adjustment IS NULL OR adjustment = ?)
                AND COALESCE(last_bar_end_at, market_asof || 'T00:00:00+00:00') <= ?
                AND as_of <= ?
                AND (? != 'LIVE' OR julianday(observed_at) <= julianday(?))
                ORDER BY COALESCE(last_bar_end_at, market_asof) DESC, observed_at DESC
                LIMIT 1
                """,
                (
                    base_lineage_key(brief),
                    brief.mode,
                    brief.timeframe,
                    brief.brief_digest,
                    brief.feed,
                    brief.adjustment,
                    current_marker,
                    brief.as_of.isoformat(),
                    brief.mode,
                    brief.as_of.isoformat(),
                ),
            ).fetchone()
        return dict(row) if row else None

    def load(self, row: dict) -> Brief:
        return self.load_report(row)[0]

    def load_report(self, row: dict):
        return read_brief_report(Path(row["report_path"]), expected_digest=row["brief_digest"])
