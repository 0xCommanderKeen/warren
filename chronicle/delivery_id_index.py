"""Rebuildable SQLite acceleration for canonical JSONL delivery identities."""

import contextlib
import glob
import json
import os
import sqlite3
from pathlib import Path


SCHEMA_VERSION = "1"


class DeliveryIdIndex:
    """Exact derived membership index, reconciled from live/archive JSONL."""

    def __init__(self, events, archive_dir):
        self.events = Path(events).resolve()
        self.archive_dir = Path(archive_dir).resolve()
        self.path = Path(str(self.events) + ".delivery-index.sqlite3")

    def contains(self, delivery_id):
        """Return membership, or ``None`` when callers must scan JSONL safely."""
        try:
            with self._connect() as database:
                self._reconcile(database)
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
            with self._connect() as database:
                database.execute(
                    "INSERT OR IGNORE INTO delivery_ids(delivery_id) VALUES (?)",
                    (delivery_id,),
                )
                self._set(database, "live", self._fingerprint(stat, stat.st_size))
        except (OSError, sqlite3.Error, UnicodeError, ValueError):
            self._discard_broken()

    @contextlib.contextmanager
    def _connect(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        database = sqlite3.connect(self.path, timeout=30, isolation_level="IMMEDIATE")
        try:
            database.execute("PRAGMA synchronous=FULL")
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

    def _reconcile(self, database):
        if self._get(database, "schema") != SCHEMA_VERSION:
            self._rebuild(database)
            return

        prior_live = self._decode(self._get(database, "live"), {})
        prior_archives = self._decode(self._get(database, "archives"), {})
        live_stat = self._stat(self.events)
        prior_archive_stamp = int(self._get(database, "archive_stamp") or 0)
        archive_stamp = self._archive_stamp()
        archives = (
            prior_archives
            if archive_stamp == prior_archive_stamp
            else self._archive_fingerprints()
        )

        if live_stat is None:
            if prior_live or archives != prior_archives:
                self._rebuild(database)
            return

        identity = self._fingerprint(live_stat, 0)
        same_file = all(prior_live.get(key) == identity[key] for key in ("dev", "ino"))
        offset = prior_live.get("offset", -1)
        archives_unchanged = archives == prior_archives
        if not same_file or not isinstance(offset, int) or live_stat.st_size < offset:
            self._rebuild(database)
            return
        if not archives_unchanged:
            old_names = set(prior_archives)
            new_names = set(archives)
            unchanged = all(
                prior_archives[name] == archives[name] for name in old_names
            )
            if not old_names <= new_names or not unchanged:
                self._rebuild(database)
                return
            for name in sorted(new_names - old_names):
                self._index_file(database, Path(name), 0)
            self._set(database, "archives", archives)
            self._set(database, "archive_stamp", str(archive_stamp))
        if live_stat.st_size > offset:
            offset = self._index_file(database, self.events, offset)
        elif prior_live.get("mtime_ns") != live_stat.st_mtime_ns:
            self._rebuild(database)
            return
        self._set(database, "live", self._fingerprint(live_stat, offset))

    def _rebuild(self, database):
        database.execute("DELETE FROM delivery_ids")
        archives = self._archive_fingerprints()
        for name in sorted(archives):
            self._index_file(database, Path(name), 0)
        live_stat = self._stat(self.events)
        live_offset = 0
        if live_stat is not None:
            live_offset = self._index_file(database, self.events, 0)
        self._set(database, "schema", SCHEMA_VERSION)
        self._set(database, "archives", archives)
        self._set(database, "archive_stamp", str(self._archive_stamp()))
        self._set(
            database,
            "live",
            self._fingerprint(live_stat, live_offset) if live_stat else {},
        )

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
            database.executemany(
                "INSERT OR IGNORE INTO delivery_ids(delivery_id) VALUES (?)", rows
            )
            return complete_offset

    def _archive_fingerprints(self):
        base, ext = os.path.splitext(self.events.name)
        names = glob.glob(str(self.archive_dir / (base + "-*" + ext)))
        return {
            name: self._fingerprint(stat, stat.st_size)
            for name in names
            if (stat := self._stat(Path(name))) is not None
        }

    def _archive_stamp(self):
        stat = self._stat(self.archive_dir)
        return stat.st_mtime_ns if stat is not None else 0

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

    def _discard_broken(self):
        for suffix in ("", "-journal", "-wal", "-shm"):
            try:
                os.unlink(str(self.path) + suffix)
            except OSError:
                pass
