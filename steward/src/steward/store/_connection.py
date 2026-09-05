"""One SQLite connection and lock, with migrations applied in order."""

import sqlite3
import threading
from pathlib import Path
from typing import Self

from steward.health import HealthJournal
from steward.store.records import (
    default_db_path,
)
from steward.store.schema import _ADDED_COLUMNS, _LATE_INDEXES, _REQUEST_RESIDENT, _SCHEMA


class _Connection:
    """The one durable memory the API writes to. Safe to share across threads."""

    def __init__(self, path: Path | str | None = None, *, busy_timeout_ms: int = 15_000) -> None:
        """Open (and migrate) the database at ``path``; ``:memory:`` for a scratch one."""
        self.path = Path(path) if path is not None and path != ":memory:" else path
        if isinstance(self.path, Path):
            self.path.parent.mkdir(parents=True, exist_ok=True)
        target = str(self.path) if self.path is not None else ":memory:"
        self._lock = threading.Lock()
        self.health = HealthJournal(self.path if isinstance(self.path, Path) else None)
        self._conn = sqlite3.connect(target, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock, self._conn:
            # The API, scheduler and CLI legitimately share this file. Make the wait
            # explicit (and testable) instead of depending on sqlite3's constructor
            # default, so a short-lived writer does not turn into missing spend.
            self._conn.execute(f"PRAGMA busy_timeout={int(busy_timeout_ms)}")
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA foreign_keys=ON")
            self._conn.executescript(_SCHEMA)
            self._add_missing_columns()
            self._conn.executescript(_LATE_INDEXES)
            self._conn.execute(
                f"CREATE INDEX IF NOT EXISTS requests_resident_received "
                f"ON requests(({_REQUEST_RESIDENT}), received_at)"
            )

    def _add_missing_columns(self) -> None:
        """Bring an older database up to the current shape without losing a row.

        Called under the open lock. ``ALTER TABLE … ADD COLUMN`` is the whole migration
        because every column added since the first release is nullable or defaulted: a
        job posted before claiming existed reads back as an open job with no claim,
        which is exactly what it is.
        """
        for table, columns in _ADDED_COLUMNS.items():
            present = {
                row["name"] for row in self._conn.execute(f"PRAGMA table_info({table})").fetchall()
            }
            for name, declaration in columns.items():
                if name not in present:
                    self._add_column(table, name, declaration)

    def _add_column(self, table: str, name: str, declaration: str) -> None:
        """Add one column, tolerating a neighbour that got there first.

        The API, scheduler, chat and watchdog all open this file at boot, and after a
        deploy that ships a new column all four read ``table_info`` before any of them
        has altered the table. Three of them then lose the ``ALTER`` race, and losing it
        must be nothing: the column they wanted exists, which is the whole point. Seen
        on the first boot after warren#400 (``duplicate column name: delivery``), where
        the loser's crash-and-restart tripped the deploy's liveness check.
        """
        try:
            self._conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {declaration}")
        except sqlite3.OperationalError as exc:
            if "duplicate column" not in str(exc).lower():
                raise

    @classmethod
    def open_default(cls) -> Self:
        """Open the database steward uses when nobody says otherwise."""
        return cls(default_db_path())

    def close(self) -> None:
        """Close the connection. Idempotent enough for a shutdown hook."""
        with self._lock:
            self._conn.close()

    def __enter__(self) -> Self:
        """Support ``with Store(...) as store:`` in tests and short-lived tools."""
        return self

    def __exit__(self, *_exc: object) -> None:
        """Close the connection on the way out."""
        self.close()
