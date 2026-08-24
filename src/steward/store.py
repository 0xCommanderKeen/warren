"""The durable store behind steward's API: jobs, approvals, and the request log.

One SQLite file (``.steward/state/steward.db``, next to the scheduler's own state)
holds everything the API must remember across a restart. SQLite rather than a JSON
file because the two things stored here both need a *conditional* write to be atomic:
the job board's claim (steward #6) is ``UPDATE … WHERE status = 'open'``, and an
approval decision (steward #10) is ``UPDATE … WHERE status = 'pending'`` — the first
writer wins and every later one reads back what was recorded. A read-modify-write over
a JSON file cannot promise that, and "two residents both hold task 7" is exactly the
kind of lie this project refuses to tell.

Nothing here emits, renders, or decides. It records facts and hands them back:

- ``jobs`` — posted work, with the status the board reports.
- ``approvals`` — a gated action waiting on a human, and the decision it received.
- ``requests`` — every accepted mutating API request and how it turned out, so a
  queued action that later failed is traceable rather than silently gone.
"""

import json
import sqlite3
import threading
import uuid
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Self

from steward.events import utc_now_iso
from steward.scheduler import default_state_path

__all__ = [
    "APPROVAL_DECISIONS",
    "DECIDED_BY_EXPIRY",
    "JOB_STATUSES",
    "ApprovalRecord",
    "JobRecord",
    "RequestRecord",
    "Store",
    "default_db_path",
]

#: What a human may answer a gated action with. ``edit`` carries a modified detail.
APPROVAL_DECISIONS = ("approve", "deny", "edit")

#: Who a request is recorded as decided by when nobody answered in time. Deny-by-default
#: is a decision steward makes on its own, and the ledger says so out loud.
DECIDED_BY_EXPIRY = "expiry"

STATUS_OPEN = "open"
STATUS_CLAIMED = "claimed"
STATUS_DONE = "done"
STATUS_FAILED = "failed"
STATUS_PENDING = "pending"
STATUS_RESOLVED = "resolved"

#: Every status a task on the board can be in. The board reports these and no others.
JOB_STATUSES = (STATUS_OPEN, STATUS_CLAIMED, STATUS_DONE, STATUS_FAILED)

DB_FILENAME = "steward.db"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    task_id          TEXT PRIMARY KEY,
    title            TEXT NOT NULL,
    detail           TEXT NOT NULL DEFAULT '',
    required_skills  TEXT NOT NULL DEFAULT '[]',
    status           TEXT NOT NULL DEFAULT 'open',
    posted_by        TEXT NOT NULL,
    claimant         TEXT,
    created_at       TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS approvals (
    request_id   TEXT PRIMARY KEY,
    agent_id     TEXT NOT NULL,
    project      TEXT NOT NULL,
    action       TEXT NOT NULL,
    message      TEXT NOT NULL,
    detail       TEXT NOT NULL DEFAULT '{}',
    options      TEXT NOT NULL DEFAULT '[]',
    status       TEXT NOT NULL DEFAULT 'pending',
    decision     TEXT,
    decided_by   TEXT,
    decided_at   TEXT,
    edit         TEXT,
    expires_at   TEXT,
    created_at   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS requests (
    request_id   TEXT PRIMARY KEY,
    received_at  TEXT NOT NULL,
    method       TEXT NOT NULL,
    path         TEXT NOT NULL,
    outcome      TEXT NOT NULL,
    detail       TEXT NOT NULL DEFAULT '{}'
);
"""

#: Columns added after the first database was written. Applied with ``ALTER TABLE`` at
#: open time, because a steward that has been running since phase 3 already has a
#: ``steward.db`` full of real jobs and a migration that drops it is a migration that
#: loses work. Every one of these is nullable or defaulted, so an old row stays legible.
_ADDED_COLUMNS: Mapping[str, Mapping[str, str]] = {
    "jobs": {
        "claimed_at": "TEXT",
        "lease_expires_at": "TEXT",
        "finished_at": "TEXT",
        "outcome": "TEXT",
        "reason": "TEXT",
        "artifacts": "TEXT NOT NULL DEFAULT '[]'",
    },
    "approvals": {
        "resident": "TEXT NOT NULL DEFAULT ''",
        "delivered_at": "TEXT",
    },
}


def default_db_path() -> Path:
    """Return the database beside the scheduler's state file.

    One state directory, two files: ``$STEWARD_STATE`` already names where steward
    remembers things, so the API stores its own memory next to it rather than
    inventing a second location a backup script has to learn about.
    """
    return default_state_path().parent / DB_FILENAME


def new_id() -> str:
    """Return a fresh opaque identifier for a request, task, or approval."""
    return str(uuid.uuid4())


def _dumps(value: object) -> str:
    return json.dumps(value, ensure_ascii=False)


def _loads(raw: str, fallback: object) -> Any:  # noqa: ANN401 — JSON is Any by nature
    try:
        return json.loads(raw)
    except ValueError:  # pragma: no cover — only a hand-edited database gets here
        return fallback


# --------------------------------------------------------------------------------------
# records
# --------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class JobRecord:
    """One posted piece of work, as the board reports it."""

    task_id: str
    title: str
    detail: str
    required_skills: tuple[str, ...]
    status: str
    posted_by: str
    created_at: str
    claimant: str | None = None
    claimed_at: str | None = None
    lease_expires_at: str | None = None
    finished_at: str | None = None
    outcome: str | None = None
    reason: str | None = None
    artifacts: tuple[str, ...] = ()

    @property
    def claimable_by(self) -> frozenset[str]:
        """The skills a resident must hold before this task is claimable by it."""
        return frozenset(self.required_skills)

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> JobRecord:
        """Rebuild a job from its database row."""
        return cls(
            task_id=row["task_id"],
            title=row["title"],
            detail=row["detail"],
            required_skills=tuple(_loads(row["required_skills"], [])),
            status=row["status"],
            posted_by=row["posted_by"],
            created_at=row["created_at"],
            claimant=row["claimant"],
            claimed_at=row["claimed_at"],
            lease_expires_at=row["lease_expires_at"],
            finished_at=row["finished_at"],
            outcome=row["outcome"],
            reason=row["reason"],
            artifacts=tuple(_loads(row["artifacts"], [])),
        )

    def to_dict(self) -> dict[str, Any]:
        """Return the JSON view the API serves."""
        return {
            "task_id": self.task_id,
            "title": self.title,
            "detail": self.detail,
            "required_skills": list(self.required_skills),
            "status": self.status,
            "posted_by": self.posted_by,
            "claimant": self.claimant,
            "created_at": self.created_at,
            "claimed_at": self.claimed_at,
            "lease_expires_at": self.lease_expires_at,
            "finished_at": self.finished_at,
            "outcome": self.outcome,
            "reason": self.reason,
            "artifacts": list(self.artifacts),
        }


@dataclass(frozen=True, slots=True)
class ApprovalRecord:
    """A gated action waiting on a human, and the decision it eventually received."""

    request_id: str
    agent_id: str
    project: str
    action: str
    message: str
    detail: Mapping[str, Any]
    options: tuple[str, ...]
    status: str
    created_at: str
    resident: str = ""
    expires_at: str | None = None
    decision: str | None = None
    decided_by: str | None = None
    decided_at: str | None = None
    edit: Mapping[str, Any] | None = None
    #: When the decision was put in front of the resident that asked. ``None`` on a
    #: decided request means the resident has not been told yet, which is exactly the
    #: set the next session's preamble is built from.
    delivered_at: str | None = None

    @property
    def pending(self) -> bool:
        """True while no decision has been recorded."""
        return self.status == STATUS_PENDING

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> ApprovalRecord:
        """Rebuild an approval request from its database row."""
        edit = row["edit"]
        return cls(
            request_id=row["request_id"],
            agent_id=row["agent_id"],
            project=row["project"],
            action=row["action"],
            message=row["message"],
            detail=_loads(row["detail"], {}),
            options=tuple(_loads(row["options"], [])),
            status=row["status"],
            created_at=row["created_at"],
            resident=row["resident"] or "",
            expires_at=row["expires_at"],
            decision=row["decision"],
            decided_by=row["decided_by"],
            decided_at=row["decided_at"],
            edit=_loads(edit, None) if edit else None,
            delivered_at=row["delivered_at"],
        )

    def to_dict(self) -> dict[str, Any]:
        """Return the JSON view the API serves."""
        return {
            "request_id": self.request_id,
            "resident": self.resident,
            "agent_id": self.agent_id,
            "project": self.project,
            "action": self.action,
            "message": self.message,
            "detail": dict(self.detail),
            "options": list(self.options),
            "status": self.status,
            "created_at": self.created_at,
            "expires_at": self.expires_at,
            "decision": self.decision,
            "decided_by": self.decided_by,
            "decided_at": self.decided_at,
            "edit": dict(self.edit) if self.edit else None,
            "delivered_at": self.delivered_at,
        }


@dataclass(frozen=True, slots=True)
class RequestRecord:
    """One accepted mutating API request, and what became of it."""

    request_id: str
    received_at: str
    method: str
    path: str
    outcome: str
    detail: Mapping[str, Any]

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> RequestRecord:
        """Rebuild a logged request from its database row."""
        return cls(
            request_id=row["request_id"],
            received_at=row["received_at"],
            method=row["method"],
            path=row["path"],
            outcome=row["outcome"],
            detail=_loads(row["detail"], {}),
        )

    def to_dict(self) -> dict[str, Any]:
        """Return the JSON view of one logged request."""
        return {
            "request_id": self.request_id,
            "received_at": self.received_at,
            "method": self.method,
            "path": self.path,
            "outcome": self.outcome,
            "detail": dict(self.detail),
        }


# --------------------------------------------------------------------------------------
# the store
# --------------------------------------------------------------------------------------


class Store:
    """The one durable memory the API writes to. Safe to share across threads."""

    def __init__(self, path: Path | str | None = None) -> None:
        """Open (and migrate) the database at ``path``; ``:memory:`` for a scratch one."""
        self.path = Path(path) if path is not None and path != ":memory:" else path
        if isinstance(self.path, Path):
            self.path.parent.mkdir(parents=True, exist_ok=True)
        target = str(self.path) if self.path is not None else ":memory:"
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(target, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock, self._conn:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA foreign_keys=ON")
            self._conn.executescript(_SCHEMA)
            self._add_missing_columns()

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
                    self._conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {declaration}")

    @classmethod
    def open_default(cls) -> Store:
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

    # -- jobs ------------------------------------------------------------------------

    def post_job(
        self,
        *,
        title: str,
        detail: str = "",
        required_skills: Sequence[str] = (),
        posted_by: str = "api",
        task_id: str | None = None,
    ) -> JobRecord:
        """Record a task on the board and return it, open and unclaimed."""
        record = JobRecord(
            task_id=task_id or new_id(),
            title=title,
            detail=detail,
            required_skills=tuple(required_skills),
            status=STATUS_OPEN,
            posted_by=posted_by,
            created_at=utc_now_iso(),
        )
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO jobs (task_id, title, detail, required_skills, status, "
                "posted_by, claimant, created_at) VALUES (?, ?, ?, ?, ?, ?, NULL, ?)",
                (
                    record.task_id,
                    record.title,
                    record.detail,
                    _dumps(list(record.required_skills)),
                    record.status,
                    record.posted_by,
                    record.created_at,
                ),
            )
        return record

    def jobs(self, status: str | None = None) -> list[JobRecord]:
        """Return the board, oldest first, optionally narrowed to one status."""
        query = "SELECT * FROM jobs"
        params: tuple[str, ...] = ()
        if status is not None:
            query += " WHERE status = ?"
            params = (status,)
        with self._lock:
            rows = self._conn.execute(f"{query} ORDER BY created_at, rowid", params).fetchall()
        return [JobRecord.from_row(row) for row in rows]

    def job(self, task_id: str) -> JobRecord | None:
        """Return one task, or ``None`` when the board has never heard of it."""
        with self._lock:
            row = self._conn.execute("SELECT * FROM jobs WHERE task_id = ?", (task_id,)).fetchone()
        return JobRecord.from_row(row) if row else None

    def claim_next_job(
        self,
        *,
        claimant: str,
        skills: Iterable[str],
        lease_expires_at: str,
        now: str | None = None,
    ) -> JobRecord | None:
        """Atomically claim the oldest open task this claimant is qualified for.

        The whole promise of the board lives in one statement::

            UPDATE jobs SET status='claimed', claimant=?, lease_expires_at=?
            WHERE task_id=? AND status='open'

        ``rowcount == 0`` means somebody else got there first — SQLite serialised the two
        writes and this caller lost. Losing is not an error and it is not retried against
        the same row: the loop simply moves to the next candidate, so two residents waking
        in the same millisecond end up holding two different tasks, or one task and
        nothing, but never the same task twice.

        Skill matching happens in Python because ``required_skills`` is a JSON list, but
        it happens *before* the conditional update and is re-checked against the row the
        update touched, so a task can never be claimed by a resident that lacks a skill.
        """
        held = frozenset(skills)
        moment = now or utc_now_iso()
        with self._lock, self._conn:
            candidates = self._conn.execute(
                "SELECT * FROM jobs WHERE status = ? ORDER BY created_at, rowid",
                (STATUS_OPEN,),
            ).fetchall()
            for row in candidates:
                record = JobRecord.from_row(row)
                if not record.claimable_by <= held:
                    continue
                cursor = self._conn.execute(
                    "UPDATE jobs SET status = ?, claimant = ?, claimed_at = ?, "
                    "lease_expires_at = ? WHERE task_id = ? AND status = ?",
                    (
                        STATUS_CLAIMED,
                        claimant,
                        moment,
                        lease_expires_at,
                        record.task_id,
                        STATUS_OPEN,
                    ),
                )
                if cursor.rowcount == 0:
                    continue  # Lost the race for this row; try the next open task.
                claimed = self._conn.execute(
                    "SELECT * FROM jobs WHERE task_id = ?", (record.task_id,)
                ).fetchone()
                return JobRecord.from_row(claimed)
        return None

    def finish_job(  # noqa: PLR0913 — one keyword per column this write touches
        self,
        task_id: str,
        *,
        status: str,
        claimant: str,
        outcome: str | None = None,
        reason: str | None = None,
        artifacts: Sequence[str] = (),
        now: str | None = None,
    ) -> JobRecord | None:
        """Close out a claimed task. Only its own claimant may, and only once.

        Conditional on ``status = 'claimed' AND claimant = ?`` so a resident whose lease
        already expired — and whose task is open again, or held by somebody else — cannot
        come back and mark somebody else's work done.
        """
        with self._lock, self._conn:
            cursor = self._conn.execute(
                "UPDATE jobs SET status = ?, outcome = ?, reason = ?, artifacts = ?, "
                "finished_at = ?, lease_expires_at = NULL "
                "WHERE task_id = ? AND status = ? AND claimant = ?",
                (
                    status,
                    outcome,
                    reason,
                    _dumps(list(artifacts)),
                    now or utc_now_iso(),
                    task_id,
                    STATUS_CLAIMED,
                    claimant,
                ),
            )
            if cursor.rowcount == 0:
                return None
            row = self._conn.execute("SELECT * FROM jobs WHERE task_id = ?", (task_id,)).fetchone()
        return JobRecord.from_row(row)

    def expire_leases(self, now: str | None = None) -> list[JobRecord]:
        """Reopen every claimed task whose lease has run out, and say who dropped it.

        Returns the rows *as they were when the lease died* — status ``claimed``, with the
        claimant still named — because that is what a ``task_failed`` event has to carry.
        The row itself is back to ``open`` by the time this returns, so the next dispatch
        can hand it to somebody who will actually finish it.
        """
        moment = now or utc_now_iso()
        expired: list[JobRecord] = []
        with self._lock, self._conn:
            rows = self._conn.execute(
                "SELECT * FROM jobs WHERE status = ? AND lease_expires_at IS NOT NULL "
                "AND lease_expires_at <= ? ORDER BY created_at, rowid",
                (STATUS_CLAIMED, moment),
            ).fetchall()
            for row in rows:
                record = JobRecord.from_row(row)
                cursor = self._conn.execute(
                    "UPDATE jobs SET status = ?, claimant = NULL, claimed_at = NULL, "
                    "lease_expires_at = NULL WHERE task_id = ? AND status = ?",
                    (STATUS_OPEN, record.task_id, STATUS_CLAIMED),
                )
                if cursor.rowcount == 1:
                    expired.append(record)
        return expired

    # -- approvals -------------------------------------------------------------------

    def create_approval_request(  # noqa: PLR0913 — one keyword per column of the record
        self,
        *,
        agent_id: str,
        project: str,
        action: str,
        message: str,
        resident: str = "",
        detail: Mapping[str, Any] | None = None,
        options: Sequence[str] = APPROVAL_DECISIONS,
        expires_at: str | None = None,
        request_id: str | None = None,
    ) -> ApprovalRecord:
        """Record a gated action that is waiting on a human.

        The internal half of steward #10: a session that reaches a gated action raises it
        — through the ``<needs-human>`` block in its output or through
        ``steward approval raise`` — steward emits the structured ``needs_human``, and the
        human answers through ``POST /approvals/{request_id}``. The API only ever
        *answers* requests; it never invents one.

        ``resident`` is the manifest id, kept alongside the burrow ``agent_id`` because
        the decision has to find its way back to a directory under ``residents/`` on the
        resident's next wake-up, and an agent id is not that.
        """
        record = ApprovalRecord(
            request_id=request_id or new_id(),
            agent_id=agent_id,
            project=project,
            action=action,
            message=message,
            detail=dict(detail or {}),
            options=tuple(options),
            status=STATUS_PENDING,
            created_at=utc_now_iso(),
            resident=resident,
            expires_at=expires_at,
        )
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO approvals (request_id, agent_id, project, action, message, "
                "detail, options, status, created_at, expires_at, resident) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    record.request_id,
                    record.agent_id,
                    record.project,
                    record.action,
                    record.message,
                    _dumps(dict(record.detail)),
                    _dumps(list(record.options)),
                    record.status,
                    record.created_at,
                    record.expires_at,
                    record.resident,
                ),
            )
        return record

    def approval(self, request_id: str) -> ApprovalRecord | None:
        """Return one approval request, decided or not, or ``None`` if unknown."""
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM approvals WHERE request_id = ?", (request_id,)
            ).fetchone()
        return ApprovalRecord.from_row(row) if row else None

    def pending_approvals(self) -> list[ApprovalRecord]:
        """Return every request still waiting on a human, oldest first."""
        return self.approvals(status=STATUS_PENDING)

    def approvals(self, status: str | None = None) -> list[ApprovalRecord]:
        """Return approval requests, oldest first, optionally narrowed to one status.

        ``None`` is the whole ledger — the audit view, where a request and the decision
        it received sit in one row and "what did I approve, and when" has an answer.
        """
        query = "SELECT * FROM approvals"
        params: tuple[str, ...] = ()
        if status is not None:
            query += " WHERE status = ?"
            params = (status,)
        with self._lock:
            rows = self._conn.execute(f"{query} ORDER BY created_at, rowid", params).fetchall()
        return [ApprovalRecord.from_row(row) for row in rows]

    def expire_approvals(self, now: str | None = None) -> list[ApprovalRecord]:
        """Deny every pending request whose ``expires_at`` has passed, and return them.

        Deny-by-default, and it is the whole safety property: a gated action that nobody
        answered must not become a gated action that went ahead because the human was
        asleep. The recorded ``decided_by`` is ``expiry`` rather than a person, because
        pretending a human said no would be a different kind of lie.
        """
        moment = now or utc_now_iso()
        expired: list[ApprovalRecord] = []
        with self._lock, self._conn:
            rows = self._conn.execute(
                "SELECT * FROM approvals WHERE status = ? AND expires_at IS NOT NULL "
                "AND expires_at <= ? ORDER BY created_at, rowid",
                (STATUS_PENDING, moment),
            ).fetchall()
            for row in rows:
                request_id = row["request_id"]
                cursor = self._conn.execute(
                    "UPDATE approvals SET status = ?, decision = ?, decided_by = ?, "
                    "decided_at = ? WHERE request_id = ? AND status = ?",
                    (
                        STATUS_RESOLVED,
                        "deny",
                        DECIDED_BY_EXPIRY,
                        moment,
                        request_id,
                        STATUS_PENDING,
                    ),
                )
                if cursor.rowcount != 1:
                    continue  # A human answered in the same instant; their answer wins.
                decided = self._conn.execute(
                    "SELECT * FROM approvals WHERE request_id = ?", (request_id,)
                ).fetchone()
                expired.append(ApprovalRecord.from_row(decided))
        return expired

    def undelivered_decisions(self, resident: str) -> list[ApprovalRecord]:
        """Return this resident's decided-but-untold requests, oldest first."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM approvals WHERE resident = ? AND status = ? "
                "AND delivered_at IS NULL ORDER BY decided_at, rowid",
                (resident, STATUS_RESOLVED),
            ).fetchall()
        return [ApprovalRecord.from_row(row) for row in rows]

    def mark_delivered(self, request_ids: Sequence[str], now: str | None = None) -> int:
        """Record that these decisions have been put in front of the resident.

        Conditional on ``delivered_at IS NULL``, so a decision is delivered exactly once
        even if two sessions of the same resident start at the same moment. The return
        value is how many this call actually marked.
        """
        moment = now or utc_now_iso()
        marked = 0
        with self._lock, self._conn:
            for request_id in request_ids:
                cursor = self._conn.execute(
                    "UPDATE approvals SET delivered_at = ? "
                    "WHERE request_id = ? AND delivered_at IS NULL",
                    (moment, request_id),
                )
                marked += cursor.rowcount
        return marked

    def decide(
        self,
        request_id: str,
        decision: str,
        *,
        decided_by: str = "api",
        edit: Mapping[str, Any] | None = None,
    ) -> tuple[ApprovalRecord | None, bool]:
        """Record a decision. Returns the record and whether *this* call recorded it.

        The first decision wins. A replay — a double-tapped notification, a retried
        request — changes nothing and reads back what was already recorded, which is
        what makes the endpoint idempotent all the way down to the disk.
        """
        with self._lock, self._conn:
            existing = self._conn.execute(
                "SELECT request_id FROM approvals WHERE request_id = ?", (request_id,)
            ).fetchone()
            if existing is None:
                return None, False
            cursor = self._conn.execute(
                "UPDATE approvals SET status = ?, decision = ?, decided_by = ?, "
                "decided_at = ?, edit = ? WHERE request_id = ? AND status = ?",
                (
                    STATUS_RESOLVED,
                    decision,
                    decided_by,
                    utc_now_iso(),
                    _dumps(dict(edit)) if edit else None,
                    request_id,
                    STATUS_PENDING,
                ),
            )
            recorded = cursor.rowcount == 1
            row = self._conn.execute(
                "SELECT * FROM approvals WHERE request_id = ?", (request_id,)
            ).fetchone()
        return ApprovalRecord.from_row(row), recorded

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

    def requests(self) -> list[RequestRecord]:
        """Return every logged request, oldest first. The audit trail, in one call."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM requests ORDER BY received_at, rowid"
            ).fetchall()
        return [RequestRecord.from_row(row) for row in rows]
