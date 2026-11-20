"""Content-addressed Parquet snapshots plus an atomic SQLite cache index."""

import fcntl
import hashlib
import json
import os
import re
import shutil
import sqlite3
import tempfile
from contextlib import closing, contextmanager
from datetime import UTC, datetime
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

from eliotbot.data.router import canonical_us_symbol
from eliotbot.errors import DataError
from eliotbot.schemas.market import MarketDataBundle
from eliotbot.util import atomic_write, canonical_json, digest

SERIES_FIELDS = ("bars", "raw_bars", "auxiliary", "daily_context", "raw_daily_context")


class _AlpacaSnapshotIdentityError(DataError):
    """A cached Alpaca snapshot needs a fresh identity check from the provider."""


def _validate_alpaca_snapshot(bundle: MarketDataBundle) -> None:
    if bundle.provenance.provider != "alpaca":
        return
    instrument = bundle.instrument
    if (
        instrument.market != "US"
        or not instrument.identity_verified
        or canonical_us_symbol(instrument.provider_symbol) != instrument.canonical_symbol
    ):
        raise _AlpacaSnapshotIdentityError(
            "ASSET_IDENTITY_MISMATCH: Alpaca snapshot lacks a matching verified symbol"
        )


class SnapshotStore:
    def __init__(self, root: Path):
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)
        with (
            self.lock(digest("snapshot-index-init")),
            closing(sqlite3.connect(self.root / "index.sqlite3")) as db,
        ):
            if db.execute("PRAGMA journal_mode").fetchone()[0] != "wal":
                db.execute("PRAGMA journal_mode=WAL")
            db.execute(
                "CREATE TABLE IF NOT EXISTS cache (key TEXT PRIMARY KEY, snapshot TEXT NOT NULL, fetched TEXT NOT NULL)"
            )
            db.execute(
                """
                CREATE TABLE IF NOT EXISTS cache_cutoffs (
                    source_snapshot TEXT NOT NULL,
                    as_of TEXT NOT NULL,
                    snapshot TEXT NOT NULL,
                    PRIMARY KEY (source_snapshot, as_of)
                )
                """
            )
            db.commit()

    @contextmanager
    def lock(self, key: str):
        if not re.fullmatch(r"[a-f0-9]{64}", key):
            raise DataError("Invalid cache identity")
        locks = self.root / "locks"
        locks.mkdir(exist_ok=True)
        with (locks / key).open("a") as stream:
            fcntl.flock(stream, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(stream, fcntl.LOCK_UN)

    def _directory(self, snapshot_id: str) -> Path:
        if not re.fullmatch(r"[a-f0-9]{64}", snapshot_id):
            raise DataError("Snapshot ID must be a SHA256 digest")
        return self.root / "snapshots" / snapshot_id

    def save(self, bundle: MarketDataBundle) -> str:
        _validate_alpaca_snapshot(bundle)
        identity = bundle.snapshot_id
        destination = self._directory(identity)
        destination.parent.mkdir(parents=True, exist_ok=True)
        with self.lock(identity):
            if destination.exists():
                self.load(identity)
                return identity
            staging = Path(tempfile.mkdtemp(prefix=".snapshot-", dir=destination.parent))
            try:
                metadata = bundle.model_dump(mode="json", exclude=set(SERIES_FIELDS))
                atomic_write(staging / "metadata.json", canonical_json(metadata))
                for field in SERIES_FIELDS:
                    records = [bar.model_dump() for bar in getattr(bundle, field)]
                    pq.write_table(pa.Table.from_pylist(records), staging / f"{field}.parquet")
                hashes = {
                    path.name: hashlib.sha256(path.read_bytes()).hexdigest()
                    for path in staging.iterdir()
                }
                atomic_write(
                    staging / "manifest.json",
                    canonical_json(
                        {"schema_version": "1.1", "snapshot_id": identity, "files": hashes}
                    ),
                )
                os.replace(staging, destination)
            finally:
                if staging.exists():
                    shutil.rmtree(staging)
        return identity

    def load(self, snapshot_id: str) -> MarketDataBundle:
        directory = self._directory(snapshot_id)
        try:
            manifest = json.loads((directory / "manifest.json").read_text())
            expected_files = {"metadata.json", *(f"{field}.parquet" for field in SERIES_FIELDS)}
            if (
                set(manifest) != {"schema_version", "snapshot_id", "files"}
                or manifest["schema_version"] != "1.1"
                or manifest["snapshot_id"] != snapshot_id
                or set(manifest["files"]) != expected_files
            ):
                raise ValueError("unknown or mismatched snapshot contract")
            for filename, checksum in manifest["files"].items():
                if hashlib.sha256((directory / filename).read_bytes()).hexdigest() != checksum:
                    raise ValueError("snapshot checksum mismatch")
            values = json.loads((directory / "metadata.json").read_text())
            for field in SERIES_FIELDS:
                values[field] = pq.read_table(directory / f"{field}.parquet").to_pylist()
            bundle = MarketDataBundle.model_validate(values)
            if bundle.snapshot_id != snapshot_id:
                raise ValueError("snapshot content identity mismatch")
            _validate_alpaca_snapshot(bundle)
            return bundle
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise DataError(f"Cannot read verified snapshot {snapshot_id[:12]}: {exc}") from exc

    def remember(self, key: str, bundle: MarketDataBundle) -> str:
        identity = self.save(bundle)
        with closing(sqlite3.connect(self.root / "index.sqlite3")) as db:
            db.execute(
                "INSERT OR REPLACE INTO cache VALUES (?, ?, ?)",
                (key, identity, bundle.provenance.fetched_at.isoformat()),
            )
            db.commit()
        return identity

    def save_at_cutoff(self, source: MarketDataBundle, as_of: datetime) -> MarketDataBundle:
        derived = source.model_copy(update={"as_of": as_of.astimezone(UTC)})
        identity = self.save(derived)
        with closing(sqlite3.connect(self.root / "index.sqlite3")) as db:
            db.execute(
                "INSERT OR REPLACE INTO cache_cutoffs VALUES (?, ?, ?)",
                (source.snapshot_id, derived.as_of.isoformat(), identity),
            )
            db.commit()
        return derived

    def lookup(
        self, key: str, now: datetime, ttl_seconds: int, *, as_of: datetime | None = None
    ) -> MarketDataBundle | None:
        cutoff = as_of.astimezone(UTC).isoformat() if as_of is not None else None
        with closing(sqlite3.connect(self.root / "index.sqlite3")) as db:
            row = db.execute(
                """
                SELECT cache.snapshot, cache.fetched, cache_cutoffs.snapshot
                FROM cache
                LEFT JOIN cache_cutoffs
                    ON cache_cutoffs.source_snapshot = cache.snapshot AND cache_cutoffs.as_of = ?
                WHERE cache.key = ?
                """,
                (cutoff, key),
            ).fetchone()
        if row is None:
            return None
        age = (now - datetime.fromisoformat(row[1])).total_seconds()
        if not 0 <= age <= ttl_seconds:
            return None
        try:
            source_id, _, derived_id = row
            bundle = self.load(derived_id or source_id)
            if derived_id is not None and bundle.as_of != as_of:
                raise DataError("Cached snapshot does not match its indexed cutoff")
            return bundle
        except _AlpacaSnapshotIdentityError:
            # A legacy cache entry can be refreshed. Explicit snapshot reads still fail.
            return None
