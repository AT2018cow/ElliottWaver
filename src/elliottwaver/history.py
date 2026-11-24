"""Local SQLite index of verified brief directories."""

from __future__ import annotations

import fcntl
import json
import sqlite3
from contextlib import closing
from datetime import UTC, date, datetime
from pathlib import Path

from elliottwaver.schemas.brief import Brief
from elliottwaver.util import canonical_json, digest
from elliottwaver.visualization.brief_formatter import read_brief_report, read_issuance


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
                db.execute(
                    """
                    CREATE TABLE IF NOT EXISTS index_reminders (
                        transition_id TEXT PRIMARY KEY,
                        brief_digest TEXT NOT NULL,
                        symbol TEXT NOT NULL,
                        mode TEXT NOT NULL,
                        decision_session TEXT NOT NULL,
                        state TEXT NOT NULL,
                        first_seen_at TEXT NOT NULL,
                        acknowledged_at TEXT,
                        payload TEXT NOT NULL
                    )
                    """
                )
                db.execute(
                    """
                    CREATE TABLE IF NOT EXISTS live_issuances (
                        request_key TEXT PRIMARY KEY, brief_digest TEXT NOT NULL, report_path TEXT NOT NULL
                    )
                    """
                )
                db.execute(
                    "CREATE INDEX IF NOT EXISTS index_reminder_history "
                    "ON index_reminders(symbol, mode, decision_session)"
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

    def lookup_issuance(self, key: str) -> tuple[Path, str] | None:
        with closing(sqlite3.connect(self.path)) as db:
            row = db.execute(
                "SELECT report_path, brief_digest FROM live_issuances WHERE request_key = ?", (key,)
            ).fetchone()
        return (Path(row[0]), row[1]) if row is not None else None

    def register(self, brief: Brief, report_path: Path, *, request_key: str | None = None) -> None:
        report_path = report_path.resolve()
        brief, _ = read_brief_report(report_path, expected_digest=brief.brief_digest)
        visible_at = read_issuance(report_path, brief)
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
                    (visible_at or brief.generated_at).isoformat(),
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
            if request_key is not None:
                if visible_at is None:
                    raise ValueError("live issuance lookup requires a verified receipt")
                db.execute(
                    "INSERT INTO live_issuances VALUES (?, ?, ?) ON CONFLICT(request_key) DO NOTHING",
                    (request_key, brief.brief_digest, str(report_path)),
                )
            if brief.index_guidance is not None:
                first_seen = (visible_at or datetime.now(UTC)).isoformat()
                db.executemany(
                    """
                    INSERT INTO index_reminders
                    (transition_id, brief_digest, symbol, mode, decision_session, state,
                     first_seen_at, payload)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(transition_id) DO NOTHING
                    """,
                    (
                        (
                            item.transition_id,
                            brief.brief_digest,
                            brief.symbol,
                            brief.mode,
                            item.session.isoformat(),
                            item.to_state,
                            first_seen,
                            canonical_json(item),
                        )
                        for track in brief.index_guidance.tracks
                        for item in track.transitions
                    ),
                )
            db.commit()

    def reminders(
        self,
        symbol: str,
        *,
        mode: str | None = None,
        as_of: date | None = None,
        brief_digest: str | None = None,
    ) -> list[dict]:
        clauses = ["symbol = ?"]
        values = [symbol]
        for key, value in (
            ("mode =", mode),
            ("decision_session <=", as_of.isoformat() if as_of else None),
            ("brief_digest =", brief_digest),
        ):
            if value is not None:
                clauses.append(f"{key} ?")
                values.append(value)
        with closing(sqlite3.connect(self.path)) as db:
            db.row_factory = sqlite3.Row
            rows = db.execute(
                "SELECT * FROM index_reminders WHERE "
                + " AND ".join(clauses)
                + " ORDER BY CASE state WHEN 'INVALIDATED' THEN 0 WHEN 'STAGE_EXIT' THEN 0"
                + " WHEN 'TRIGGERED' THEN 1 WHEN 'NEAR' THEN 2 ELSE 3 END,"
                + " first_seen_at DESC, transition_id LIMIT 100",
                values,
            ).fetchall()
        return [
            {
                **json.loads(row["payload"]),
                "brief_digest": row["brief_digest"],
                "mode": row["mode"],
                "first_seen_at": row["first_seen_at"],
                "acknowledged_at": row["acknowledged_at"],
            }
            for row in rows
        ]

    def acknowledge_reminder(self, symbol: str, transition_id: str, *, mode: str | None) -> bool:
        with closing(sqlite3.connect(self.path)) as db:
            cursor = db.execute(
                """
                UPDATE index_reminders SET acknowledged_at = COALESCE(acknowledged_at, ?)
                WHERE symbol = ? AND transition_id = ? AND (? IS NULL OR mode = ?)
                """,
                (datetime.now(UTC).isoformat(), symbol, transition_id, mode, mode),
            )
            db.commit()
            return bool(cursor.rowcount)

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
