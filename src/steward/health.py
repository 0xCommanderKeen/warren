"""Durable operational failures that must survive when SQLite cannot write.

The journal lives beside the database rather than inside it deliberately: its most
important entry is evidence that the database stayed locked past SQLite's retry window.
Each JSONL record carries a cumulative count, so readers and writers only inspect a
bounded tail.  A process-wide advisory lock makes append, compaction, and counting one
operation; ``fsync`` makes a returned write durable across a restart.
"""

import fcntl
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from steward.events import redact_secrets, truncate_error, utc_now_iso

TAIL_BYTES = 256 * 1024
COMPACT_AT_BYTES = 1024 * 1024


@dataclass(frozen=True)
class HealthFailure:
    """The cumulative failure count and its latest safe, bounded context."""

    count: int
    kind: str
    resident: str
    run_id: str
    error: str
    failed_at: str


class HealthJournal:
    """A small, independent health journal associated with one on-disk database."""

    def __init__(self, database: Path | None) -> None:
        """Associate the journal with ``database``; memory stores have no sidecar."""
        self.path = (
            database.with_name(f"{database.name}.health.jsonl") if database is not None else None
        )

    def record(
        self, *, kind: str, resident: str, run_id: str, error: str, now: str | None = None
    ) -> None:
        """Append and sync one failure; do nothing for an in-memory scratch store."""
        if self.path is None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT | os.O_APPEND, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            latest = _latest(fd)
            record = {
                "version": 1,
                "count": (latest.count if latest else 0) + 1,
                "kind": kind,
                "resident": resident,
                "run_id": run_id,
                "error": truncate_error(redact_secrets(error)),
                "failed_at": now or utc_now_iso(),
            }
            line = json.dumps(record, separators=(",", ":"), ensure_ascii=True).encode() + b"\n"
            if os.fstat(fd).st_size + len(line) > COMPACT_AT_BYTES:
                os.ftruncate(fd, 0)
            os.lseek(fd, 0, os.SEEK_END)
            view = memoryview(line)
            while view:
                view = view[os.write(fd, view) :]
            os.fsync(fd)
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)

    def latest(self) -> HealthFailure | None:
        """Read the newest valid record from a bounded tail, skipping corrupt lines."""
        if self.path is None:
            return None
        try:
            fd = os.open(self.path, os.O_RDONLY)
        except FileNotFoundError:
            return None
        try:
            fcntl.flock(fd, fcntl.LOCK_SH)
            return _latest(fd)
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)


def _latest(fd: int) -> HealthFailure | None:
    size = os.fstat(fd).st_size
    start = max(0, size - TAIL_BYTES)
    os.lseek(fd, start, os.SEEK_SET)
    data = os.read(fd, TAIL_BYTES)
    lines = data.splitlines()
    if start and lines:
        lines = lines[1:]  # the bounded read may begin in the middle of a record
    for raw in reversed(lines):
        try:
            value: dict[str, Any] = json.loads(raw)
            if value.get("version") != 1 or int(value["count"]) < 1:
                continue
            return HealthFailure(
                count=int(value["count"]),
                kind=str(value["kind"]),
                resident=str(value["resident"]),
                run_id=str(value["run_id"]),
                error=str(value["error"]),
                failed_at=str(value["failed_at"]),
            )
        except KeyError, TypeError, ValueError, json.JSONDecodeError:
            continue
    return None
