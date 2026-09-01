"""Rebuildable SQLite acceleration for canonical JSONL delivery identities."""

import contextlib
import glob
import hashlib
import json
import os
import secrets
import sqlite3
from pathlib import Path

SCHEMA_VERSION = "2"
BATCH_SIZE = 512


class DeliveryIdIndex:
    """Exact derived membership index, reconciled from live/archive JSONL."""

    def __init__(self, events, archive_dir):
        self.events = Path(events).resolve()
        self.archive_dir = Path(archive_dir).resolve()
        self.path = Path(str(self.events) + ".delivery-index.sqlite3")
        self.publication = Path(str(self.events) + ".archives-generation")
        self._archives_verified = False

    def contains(self, delivery_id):
        """Return membership, or ``None`` when callers must scan JSONL safely."""
        try:
            if not self.path.exists():
                self._repair()
            with self._read_connection() as database:
                clean = self._clean(database)
                if clean:
                    return (
                        database.execute(
                            "SELECT 1 FROM delivery_ids WHERE delivery_id = ?",
                            (delivery_id,),
                        ).fetchone()
                        is not None
                    )
            self._repair()
            with self._read_connection() as database:
                return (
                    database.execute(
                        "SELECT 1 FROM delivery_ids WHERE delivery_id = ?",
                        (delivery_id,),
                    ).fetchone()
                    is not None
                )
        except (OSError, sqlite3.Error, UnicodeError, ValueError):
            self._discard_broken()
            try:
                self._repair()
                with self._read_connection() as database:
                    return (
                        database.execute(
                            "SELECT 1 FROM delivery_ids WHERE delivery_id = ?",
                            (delivery_id,),
                        ).fetchone()
                        is not None
                    )
            except (OSError, sqlite3.Error, UnicodeError, ValueError):
                self._discard_broken()
                return None

    def remember(self, delivery_id):
        """Record a just-fsynced canonical append and its new live cursor."""
        try:
            stat = self.events.stat()
            with self._write_connection() as database:
                database.execute(
                    "INSERT OR IGNORE INTO delivery_ids(delivery_id) VALUES (?)",
                    (delivery_id,),
                )
                self._set(database, "live", self._fingerprint(stat, stat.st_size))
        except (OSError, sqlite3.Error, UnicodeError, ValueError):
            self._discard_broken()

    def publish_archives(self):
        """Publish a collision-safe generation after canonical archive mutation."""
        self.publication.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.publication.with_name(
            f".{self.publication.name}.{os.getpid()}.{secrets.token_hex(8)}.tmp"
        )
        with temporary.open("x", encoding="ascii") as stream:
            stream.write(secrets.token_hex(32) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, self.publication)
        self._fsync_parent(self.publication)

    def _clean(self, database):
        if self._get(database, "schema") != SCHEMA_VERSION:
            return False
        if self._get(database, "archives_generation") != self._generation():
            return False
        if not self._archives_verified:
            recorded = self._decode(self._get(database, "archives"), {})
            if recorded != self._archive_fingerprints():
                return False
            self._archives_verified = True
        prior = self._decode(self._get(database, "live"), {})
        stat = self._stat(self.events)
        if stat is None:
            return not prior
        return prior == self._fingerprint(stat, prior.get("offset", -1))

    def _repair(self):
        rebuilt = False
        with self._write_connection() as database:
            if not self._clean(database):
                self._rebuild(database)
                rebuilt = True
        if rebuilt:
            with contextlib.closing(sqlite3.connect(self.path, timeout=30)) as database:
                database.execute("VACUUM")

    def _rebuild(self, database):
        database.execute("DELETE FROM delivery_ids")
        archives = self._archive_fingerprints()
        for name in sorted(archives):
            self._index_file(database, Path(name), 0)
        live_stat = self._stat(self.events)
        live_offset = self._index_file(database, self.events, 0) if live_stat else 0
        self._set(database, "schema", SCHEMA_VERSION)
        self._set(database, "archives", archives)
        self._set(database, "archives_generation", self._generation())
        self._set(
            database,
            "live",
            self._fingerprint(live_stat, live_offset) if live_stat else {},
        )
        self._archives_verified = True

    def _archive_fingerprints(self):
        base, ext = os.path.splitext(self.events.name)
        names = glob.glob(str(self.archive_dir / (base + "-*" + ext)))
        fingerprints = {}
        for name in names:
            try:
                digest = hashlib.sha256()
                with open(name, "rb") as stream:
                    for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                        digest.update(chunk)
                fingerprints[name] = digest.hexdigest()
            except OSError:
                continue
        return fingerprints

    def _index_file(self, database, path, offset):
        with path.open("rb") as stream:
            stream.seek(offset)
            rows = []
            complete_offset = offset
            for raw in stream:
                if not raw.endswith(b"\n"):
                    break
                complete_offset = stream.tell()
                try:
                    event = json.loads(raw)
                except (UnicodeDecodeError, ValueError):
                    continue
                delivery_id = (
                    event.get("delivery_id") if isinstance(event, dict) else None
                )
                if isinstance(delivery_id, str) and delivery_id:
                    rows.append((delivery_id,))
                if len(rows) >= BATCH_SIZE:
                    self._insert_rows(database, rows)
                    rows.clear()
            if rows:
                self._insert_rows(database, rows)
            return complete_offset

    @staticmethod
    def _insert_rows(database, rows):
        database.executemany(
            "INSERT OR IGNORE INTO delivery_ids(delivery_id) VALUES (?)", rows
        )

    @contextlib.contextmanager
    def _read_connection(self):
        database = self._open_read_connection()
        try:
            yield database
        finally:
            database.close()

    def _open_read_connection(self):
        return sqlite3.connect(
            f"file:{self.path}?mode=ro", uri=True, timeout=30, isolation_level=None
        )

    @contextlib.contextmanager
    def _write_connection(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        database = sqlite3.connect(self.path, timeout=30)
        try:
            database.execute("PRAGMA synchronous=NORMAL")
            database.execute("PRAGMA journal_mode=WAL")
            database.execute(
                "CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
            )
            database.execute(
                "CREATE TABLE IF NOT EXISTS delivery_ids (delivery_id TEXT PRIMARY KEY)"
            )
            with database:
                yield database
        finally:
            database.close()

    def _generation(self):
        try:
            return self.publication.read_text(encoding="ascii").strip()
        except FileNotFoundError:
            return ""

    @staticmethod
    def _stat(path):
        try:
            return path.stat()
        except OSError:
            return None

    @staticmethod
    def _fingerprint(stat, offset):
        return {
            "dev": stat.st_dev,
            "ino": stat.st_ino,
            "size": stat.st_size,
            "mtime_ns": stat.st_mtime_ns,
            "offset": offset,
        }

    @staticmethod
    def _decode(raw, default):
        if raw is None:
            return default
        value = json.loads(raw)
        return value if isinstance(value, type(default)) else default

    @staticmethod
    def _get(database, key):
        row = database.execute(
            "SELECT value FROM metadata WHERE key = ?", (key,)
        ).fetchone()
        return row[0] if row else None

    @staticmethod
    def _set(database, key, value):
        encoded = value if isinstance(value, str) else json.dumps(value, sort_keys=True)
        database.execute(
            "INSERT INTO metadata(key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, encoded),
        )

    @staticmethod
    def _fsync_parent(path):
        descriptor = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    def _discard_broken(self):
        for suffix in ("", "-journal", "-wal", "-shm"):
            try:
                os.unlink(str(self.path) + suffix)
            except OSError:
                pass
