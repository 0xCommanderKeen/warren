"""Every accepted mutating API request and how it turned out.

A queued action that later failed is traceable rather than silently gone.
"""

from collections.abc import Mapping, Sequence
from typing import Any

from steward.events import utc_now_iso
from steward.store._connection import _Connection
from steward.store.records import (
    RequestRecord,
    _dumps,
)
from steward.store.schema import _REQUEST_RESIDENT


class _RequestTables(_Connection):
    """The accepted request log."""

    # -- the request log -------------------------------------------------------------

    def log_request(
        self,
        *,
        request_id: str,
        method: str,
        path: str,
        outcome: str,
        detail: Mapping[str, Any] | None = None,
    ) -> RequestRecord:
        """Record an accepted mutating request, so its later outcome is traceable."""
        record = RequestRecord(
            request_id=request_id,
            received_at=utc_now_iso(),
            method=method,
            path=path,
            outcome=outcome,
            detail=dict(detail or {}),
        )
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT OR REPLACE INTO requests (request_id, received_at, method, path, "
                "outcome, detail) VALUES (?, ?, ?, ?, ?, ?)",
                (
                    record.request_id,
                    record.received_at,
                    record.method,
                    record.path,
                    record.outcome,
                    _dumps(dict(record.detail)),
                ),
            )
        return record

    def set_request_outcome(
        self, request_id: str, outcome: str, detail: Mapping[str, Any] | None = None
    ) -> None:
        """Update what became of an already-logged request. Unknown ids are ignored."""
        with self._lock, self._conn:
            if detail is None:
                self._conn.execute(
                    "UPDATE requests SET outcome = ? WHERE request_id = ?",
                    (outcome, request_id),
                )
            else:
                self._conn.execute(
                    "UPDATE requests SET outcome = ?, detail = ? WHERE request_id = ?",
                    (outcome, _dumps(dict(detail)), request_id),
                )

    def request(self, request_id: str) -> RequestRecord | None:
        """Return one logged request, or ``None``."""
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM requests WHERE request_id = ?", (request_id,)
            ).fetchone()
        return RequestRecord.from_row(row) if row else None

    def recent_requests(self, *, limit: int, resident: str | None = None) -> list[RequestRecord]:
        """Return at most ``limit`` requests, newest first, including insertion-order ties."""
        if limit < 1:
            raise ValueError("request limit must be positive")
        predicate = "" if resident is None else f" WHERE ({_REQUEST_RESIDENT}) = ?"
        parameters = (limit,) if resident is None else (resident, limit)
        with self._lock:
            rows = self._conn.execute(
                f"SELECT * FROM requests{predicate} "  # noqa: S608 — static SQL; values bound below
                "ORDER BY received_at DESC, rowid DESC LIMIT ?",
                parameters,
            ).fetchall()
        return [RequestRecord.from_row(row) for row in rows]

    def latest_routine_requests(self, keys: Sequence[str]) -> dict[str, RequestRecord]:
        """Look up one newest request per declared routine, without reading its history."""
        latest: dict[str, RequestRecord] = {}
        with self._lock:
            for key in dict.fromkeys(keys):
                row = self._conn.execute(
                    "SELECT * FROM requests WHERE json_type(detail, '$.routine') = 'text' "
                    "AND json_extract(detail, '$.routine') = ? "
                    "ORDER BY received_at DESC, rowid DESC LIMIT 1",
                    (key,),
                ).fetchone()
                if row is not None:
                    latest[key] = RequestRecord.from_row(row)
        return latest

    def export_request_history(self) -> list[RequestRecord]:
        """Return every logged request, oldest first. The audit trail, in one call."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM requests ORDER BY received_at, rowid"
            ).fetchall()
        return [RequestRecord.from_row(row) for row in rows]
