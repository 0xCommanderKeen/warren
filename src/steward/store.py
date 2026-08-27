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

- ``jobs`` — posted work, with the status the board reports. A row with an ``assignee``
  is work one resident handed to another (:mod:`steward.delegation`): the same table,
  because a delegated item is a task addressed to somebody rather than to the fleet, and
  everything the board already does to a task — leasing it, sweeping it, closing it —
  applies to it unchanged.
- ``approvals`` — a gated action waiting on a human, and the decision it received.
- ``requests`` — every accepted mutating API request and how it turned out, so a
  queued action that later failed is traceable rather than silently gone.
- ``run_ledger`` — one row per finished session, with the tokens, money, and seconds it
  actually cost. This is what makes a daily budget survive a daemon restart: a cap that
  resets because the process bounced is not a cap (steward #8).
- ``budget_pauses`` — the residents steward has stopped firing, and the number that
  stopped them. One row per paused resident, inserted conditionally, which is what makes
  "exactly one knock at the door" true rather than hoped for.
- ``watchdog_attempts`` / ``watchdog_passes`` / ``unbracketed_runs`` — what the watchdog
  has already done, so a restart budget and a "run never reported back" complaint are
  each spent exactly once however often the watchdog ticks.
- ``open_runs`` — one row per session steward has started and not yet closed. The
  watchdog reads this to find runs that never reported back, instead of reading the
  undelivered-event log, which can only answer for the host that fired them and only
  while burrow was unreachable (steward #39).
"""

import json
import sqlite3
import threading
import uuid
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Self

from steward.events import utc_now_iso
from steward.scheduler import default_state_path

__all__ = [
    "APPROVAL_DECISIONS",
    "DECIDED_BY_EXPIRY",
    "DECIDED_BY_REPEAT",
    "JOB_STATUSES",
    "ORIGIN_UNATTRIBUTED",
    "RUN_KINDS",
    "STATUS_OPEN",
    "ApprovalRecord",
    "JobRecord",
    "LedgerEntry",
    "OpenRun",
    "OriginSpend",
    "PauseRecord",
    "RequestRecord",
    "Store",
    "WatchdogAttempt",
    "default_db_path",
]

#: What a human may answer a gated action with. ``edit`` carries a modified detail.
APPROVAL_DECISIONS = ("approve", "deny", "edit")

#: Who a request is recorded as decided by when nobody answered in time. Deny-by-default
#: is a decision steward makes on its own, and the ledger says so out loud.
DECIDED_BY_EXPIRY = "expiry"

#: Who a request is recorded as decided by when steward answered it with a deny a human
#: had already given for the same action — the repeat guard in
#: :mod:`steward.transitions.approval`. Also a decision steward makes on its own, and also
#: said out loud rather than filed as if a person had clicked deny a second time.
DECIDED_BY_REPEAT = "repeat"

STATUS_OPEN = "open"
STATUS_CLAIMED = "claimed"
STATUS_DONE = "done"
STATUS_FAILED = "failed"
STATUS_PENDING = "pending"
STATUS_RESOLVED = "resolved"


class _AtomicTaskCloseLostError(Exception):
    """Rollback sentinel for a task/run close that lost either conditional write."""


#: Every status a task on the board can be in. The board reports these and no others.
JOB_STATUSES = (STATUS_OPEN, STATUS_CLAIMED, STATUS_DONE, STATUS_FAILED)

#: Why a session ran. The ledger keeps them apart so "what did the board cost me this
#: week" and "what did Hob's own routines cost me" are two answerable questions.
RUN_ROUTINE = "routine"
RUN_TASK = "task"
RUN_DELEGATED = "delegated"
RUN_KINDS = (RUN_ROUTINE, RUN_TASK, RUN_DELEGATED)

#: Where spend lands when no task — and so no delegation origin — stands behind the run.
#: A resident's own routines are the ordinary case, and they are named rather than
#: dropped: money steward cannot attribute is still money somebody spent.
ORIGIN_UNATTRIBUTED = "unattributed"

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

CREATE TABLE IF NOT EXISTS run_ledger (
    entry_id      TEXT PRIMARY KEY,
    resident      TEXT NOT NULL,
    agent_id      TEXT NOT NULL,
    kind          TEXT NOT NULL,
    run_id        TEXT NOT NULL,
    ref           TEXT NOT NULL DEFAULT '',
    origin        TEXT NOT NULL DEFAULT '',
    outcome       TEXT NOT NULL DEFAULT '',
    input_tokens  INTEGER NOT NULL DEFAULT 0,
    output_tokens INTEGER NOT NULL DEFAULT 0,
    cost_usd      REAL NOT NULL DEFAULT 0.0,
    duration_s    REAL NOT NULL DEFAULT 0.0,
    usage_known   INTEGER NOT NULL DEFAULT 1,
    recorded_at   TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS run_ledger_window
    ON run_ledger (resident, recorded_at);

CREATE TABLE IF NOT EXISTS budget_pauses (
    resident    TEXT PRIMARY KEY,
    agent_id    TEXT NOT NULL,
    budget      TEXT NOT NULL,
    spent       REAL NOT NULL,
    cap         REAL NOT NULL,
    reason      TEXT NOT NULL DEFAULT '',
    request_id  TEXT,
    window_end  TEXT NOT NULL DEFAULT '',
    paused_at   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS budget_allowances (
    resident   TEXT PRIMARY KEY,
    until      TEXT NOT NULL,
    granted_by TEXT NOT NULL DEFAULT '',
    reason     TEXT NOT NULL DEFAULT '',
    granted_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS watchdog_attempts (
    resident        TEXT PRIMARY KEY,
    attempts        INTEGER NOT NULL DEFAULT 0,
    reason          TEXT NOT NULL DEFAULT '',
    last_attempt_at TEXT,
    next_attempt_at TEXT,
    gave_up_at      TEXT
);

CREATE TABLE IF NOT EXISTS watchdog_passes (
    id            INTEGER PRIMARY KEY CHECK (id = 1),
    last_pass_at  TEXT NOT NULL,
    passes        INTEGER NOT NULL DEFAULT 0,
    interventions INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS unbracketed_runs (
    run_id     TEXT PRIMARY KEY,
    agent_id   TEXT NOT NULL,
    routine    TEXT NOT NULL DEFAULT '',
    started_at TEXT NOT NULL,
    closed_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS open_runs (
    run_id     TEXT PRIMARY KEY,
    kind       TEXT NOT NULL DEFAULT 'routine',
    agent_id   TEXT NOT NULL,
    project    TEXT NOT NULL DEFAULT '',
    ref        TEXT NOT NULL DEFAULT '',
    timeout_s  REAL NOT NULL DEFAULT 0.0,
    started_at TEXT NOT NULL,
    heartbeat_at TEXT NOT NULL DEFAULT '',
    event_log_path TEXT NOT NULL DEFAULT '',
    evidence_version INTEGER NOT NULL DEFAULT 1,
    owner_token TEXT NOT NULL DEFAULT '',
    terminal_event TEXT,
    terminal_event_id TEXT,
    terminal_claimed_at TEXT,
    terminal_published_at TEXT,
    closed_at  TEXT
);

CREATE INDEX IF NOT EXISTS open_runs_still_open
    ON open_runs (closed_at, started_at);
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
        # Delegation (steward #7). A delegated item is a job addressed to one resident:
        # same table, same lease, same three closing events. ``assignee`` is what makes
        # it a letter rather than a notice — a row with one is never on the open board.
        "assignee": "TEXT",
        "delegated_by": "TEXT",
        "route": "TEXT",
        "parent_task_id": "TEXT",
        "origin": "TEXT",
        "depth": "INTEGER NOT NULL DEFAULT 0",
        # The currently leased attempt.  These are cleared together when the attempt
        # finishes or is swept; a retry therefore cannot inherit its predecessor's run.
        "run_id": "TEXT",
        "owner_token": "TEXT",
        "lease_duration_s": "REAL",
    },
    "approvals": {
        "resident": "TEXT NOT NULL DEFAULT ''",
        "delivered_at": "TEXT",
    },
    "run_ledger": {
        # Denormalized from the task the run came off (steward #45). Rolling spend up by
        # joining ``ref`` to ``jobs.task_id`` guessed: a routine whose ref happens to
        # equal some task's id would have inherited that task's bill. The row says what
        # it descends from, so the ledger is self-describing and a join cannot misread it.
        "origin": "TEXT NOT NULL DEFAULT ''",
    },
    "open_runs": {
        "heartbeat_at": "TEXT NOT NULL DEFAULT ''",
        "event_log_path": "TEXT NOT NULL DEFAULT ''",
        "evidence_version": "INTEGER NOT NULL DEFAULT 0",
        "owner_token": "TEXT NOT NULL DEFAULT ''",
        "terminal_event": "TEXT",
        "terminal_event_id": "TEXT",
        "terminal_claimed_at": "TEXT",
        "terminal_published_at": "TEXT",
    },
}

#: Indexes over columns that arrived after the first schema, and so can only be created
#: once :meth:`Store._add_missing_columns` has added them. ``approvals_denials`` is what
#: keeps the repeat-deny guard (:mod:`steward.transitions.approval`) a lookup rather than a
#: table scan on every knock: the table has grown one row per ask since phase 3.
_LATE_INDEXES = """
CREATE INDEX IF NOT EXISTS approvals_denials
    ON approvals (resident, action, decided_at);
"""


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
    #: The resident this work was handed to. ``None`` is an open notice anybody
    #: qualified may claim; a name is a letter addressed to one villager.
    assignee: str | None = None
    delegated_by: str | None = None
    route: str | None = None
    parent_task_id: str | None = None
    #: The accountable origin the whole chain rolls up to, so spend and work attribute
    #: to one root rather than to the last hop. See :mod:`steward.delegation`.
    origin: str | None = None
    depth: int = 0
    run_id: str | None = None
    owner_token: str | None = None
    lease_duration_s: float | None = None

    @property
    def claimable_by(self) -> frozenset[str]:
        """The skills a resident must hold before this task is claimable by it."""
        return frozenset(self.required_skills)

    @property
    def delegated(self) -> bool:
        """True when this work was handed to one named resident rather than posted."""
        return self.assignee is not None

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
            assignee=row["assignee"],
            delegated_by=row["delegated_by"],
            route=row["route"],
            parent_task_id=row["parent_task_id"],
            origin=row["origin"],
            depth=row["depth"] or 0,
            run_id=row["run_id"],
            owner_token=row["owner_token"],
            lease_duration_s=row["lease_duration_s"],
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
            "assignee": self.assignee,
            "delegated_by": self.delegated_by,
            "route": self.route,
            "parent_task_id": self.parent_task_id,
            "origin": self.origin,
            "depth": self.depth,
            "run_id": self.run_id,
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
class LedgerEntry:
    """What one finished session actually cost. Never an estimate.

    ``usage_known`` is the honest half. A ``claude`` run reports its usage and cost in
    the JSON steward already asks for; a ``codex`` or ``command`` run reports nothing,
    and steward writes zeroes with ``usage_known = False`` rather than inventing a
    number. A budget can then say "0.00 of 5.00 spent, and 3 of today's 4 runs did not
    report what they cost", which is a true sentence, where "0.00 spent" alone would be
    a comfortable lie.
    """

    entry_id: str
    resident: str
    agent_id: str
    kind: str
    run_id: str
    recorded_at: str
    ref: str = ""
    #: What this run descends from, in delegation's vocabulary (``task:``/``resident:``/
    #: ``human:``). Written at record time rather than inferred later — see
    #: :meth:`Store.spend_by_origin`. Empty only on rows written before the column existed.
    origin: str = ""
    outcome: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    duration_s: float = 0.0
    usage_known: bool = True

    @property
    def tokens(self) -> int:
        """Input plus output — the number a daily token budget is counted against."""
        return self.input_tokens + self.output_tokens

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> LedgerEntry:
        """Rebuild one ledger entry from its database row."""
        return cls(
            entry_id=row["entry_id"],
            resident=row["resident"],
            agent_id=row["agent_id"],
            kind=row["kind"],
            run_id=row["run_id"],
            recorded_at=row["recorded_at"],
            ref=row["ref"],
            origin=row["origin"],
            outcome=row["outcome"],
            input_tokens=row["input_tokens"],
            output_tokens=row["output_tokens"],
            cost_usd=row["cost_usd"],
            duration_s=row["duration_s"],
            usage_known=bool(row["usage_known"]),
        )

    def to_dict(self) -> dict[str, Any]:
        """Return the JSON view of one run's consumption."""
        return {
            "entry_id": self.entry_id,
            "resident": self.resident,
            "agent_id": self.agent_id,
            "kind": self.kind,
            "run_id": self.run_id,
            "ref": self.ref,
            "origin": self.origin,
            "outcome": self.outcome,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "cost_usd": self.cost_usd,
            "duration_s": round(self.duration_s, 3),
            "usage_known": self.usage_known,
            "recorded_at": self.recorded_at,
        }


@dataclass(frozen=True, slots=True)
class OriginSpend:
    """What one origin cost, rolled up across every hop of the chain it started.

    Produced by :meth:`Store.spend_by_origin`. ``origin`` is delegation's vocabulary —
    ``task:<id>``, ``resident:<id>``, ``human:<who>`` — or
    :data:`ORIGIN_UNATTRIBUTED` for a run that never came off a task at all.
    """

    origin: str
    runs: int
    cost_usd: float = 0.0
    tokens: int = 0
    duration_s: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        """Return the JSON view of one origin's spend."""
        return {
            "origin": self.origin,
            "runs": self.runs,
            "cost_usd": round(self.cost_usd, 6),
            "tokens": self.tokens,
            "duration_s": round(self.duration_s, 3),
        }


@dataclass(frozen=True, slots=True)
class PauseRecord:
    """A resident steward has stopped firing, and the number that stopped it."""

    resident: str
    agent_id: str
    budget: str
    spent: float
    cap: float
    paused_at: str
    reason: str = ""
    request_id: str | None = None
    #: The end of the window this pause was tripped in. Lifting the pause grants the
    #: resident the rest of *that* window and nothing more, so "carry on today" means
    #: today rather than forever.
    window_end: str = ""

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> PauseRecord:
        """Rebuild one pause from its database row."""
        return cls(
            resident=row["resident"],
            agent_id=row["agent_id"],
            budget=row["budget"],
            spent=row["spent"],
            cap=row["cap"],
            paused_at=row["paused_at"],
            reason=row["reason"],
            request_id=row["request_id"],
            window_end=row["window_end"],
        )

    def to_dict(self) -> dict[str, Any]:
        """Return the JSON view a fleet-ops panel renders as a stopped villager."""
        return {
            "resident": self.resident,
            "agent_id": self.agent_id,
            "budget": self.budget,
            "spent": self.spent,
            "cap": self.cap,
            "reason": self.reason,
            "request_id": self.request_id,
            "window_end": self.window_end or None,
            "paused_at": self.paused_at,
        }


@dataclass(frozen=True, slots=True)
class WatchdogAttempt:
    """How many times the watchdog has tried to bring one resident back, and when next."""

    resident: str
    attempts: int = 0
    reason: str = ""
    last_attempt_at: str | None = None
    next_attempt_at: str | None = None
    gave_up_at: str | None = None

    @property
    def gave_up(self) -> bool:
        """True once the watchdog stopped restarting and knocked at the door instead."""
        return self.gave_up_at is not None

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> WatchdogAttempt:
        """Rebuild the restart budget of one resident from its database row."""
        return cls(
            resident=row["resident"],
            attempts=row["attempts"],
            reason=row["reason"],
            last_attempt_at=row["last_attempt_at"],
            next_attempt_at=row["next_attempt_at"],
            gave_up_at=row["gave_up_at"],
        )

    def to_dict(self) -> dict[str, Any]:
        """Return the JSON view of one resident's restart history."""
        return {
            "resident": self.resident,
            "attempts": self.attempts,
            "reason": self.reason,
            "last_attempt_at": self.last_attempt_at,
            "next_attempt_at": self.next_attempt_at,
            "gave_up_at": self.gave_up_at,
        }


@dataclass(frozen=True, slots=True)
class OpenRun:
    """One session steward opened, and — once it closed — when.

    Steward's own record that a run exists, written where the opening event is emitted
    and closed where the closing one is. It is what the watchdog reads to find a run
    that never reported back, because the alternative it used to read — the
    undelivered-event log — is only written while burrow is unreachable and only on the
    host that fired the run (steward #39).

    ``timeout_s`` is the run's *effective* deadline, budget cap included, so the
    watchdog judges a run against the timeout the session was actually given rather
    than against the one its manifest declares.
    """

    #: This session, and only this one. A routine fired twice and a task claimed twice
    #: are two runs with two ids; what they have in common lives in ``ref``.
    run_id: str
    kind: str
    agent_id: str
    started_at: str
    project: str = ""
    #: What the run was about: a routine id, or the task id a board session claimed.
    #: Not unique — every attempt at one task carries the same ``ref``.
    ref: str = ""
    timeout_s: float = 0.0
    heartbeat_at: str = ""
    event_log_path: str = ""
    evidence_version: int = 0
    owner_token: str = ""
    terminal_event: str | None = None
    terminal_event_id: str | None = None
    terminal_claimed_at: str | None = None
    terminal_published_at: str | None = None
    closed_at: str | None = None

    @property
    def open(self) -> bool:
        """True while steward has heard nothing back about this run."""
        return self.closed_at is None

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> OpenRun:
        """Rebuild one run record from its database row."""
        return cls(
            run_id=row["run_id"],
            kind=row["kind"],
            agent_id=row["agent_id"],
            started_at=row["started_at"],
            project=row["project"],
            ref=row["ref"],
            timeout_s=row["timeout_s"],
            heartbeat_at=row["heartbeat_at"],
            event_log_path=row["event_log_path"],
            evidence_version=row["evidence_version"],
            owner_token=row["owner_token"],
            terminal_event=row["terminal_event"],
            terminal_event_id=row["terminal_event_id"],
            terminal_claimed_at=row["terminal_claimed_at"],
            terminal_published_at=row["terminal_published_at"],
            closed_at=row["closed_at"],
        )

    def to_dict(self) -> dict[str, Any]:
        """Return the JSON view of one open run."""
        return {
            "run_id": self.run_id,
            "kind": self.kind,
            "agent_id": self.agent_id,
            "project": self.project,
            "ref": self.ref,
            "timeout_s": self.timeout_s,
            "started_at": self.started_at,
            "heartbeat_at": self.heartbeat_at,
            "event_log_path": self.event_log_path,
            "closed_at": self.closed_at,
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
            self._conn.executescript(_LATE_INDEXES)

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

    def delegate_job(  # noqa: PLR0913 — one keyword per column of the record
        self,
        *,
        title: str,
        assignee: str,
        delegated_by: str,
        route: str,
        detail: str = "",
        parent_task_id: str | None = None,
        origin: str | None = None,
        depth: int = 1,
        task_id: str | None = None,
    ) -> JobRecord:
        """Record work handed to one named resident, and return it, open and unclaimed.

        The same table the board uses, because a delegated item *is* a task — it is worked
        as a session, leased, and closed with the same three events. The only difference
        is the addressee: an item with an ``assignee`` is never offered to the open board
        (:meth:`claim_next_job` skips it), and only that resident may pick it up.

        Nothing is validated here. Whether the sender may delegate, whether the receiver's
        route accepts the work, how deep the chain is and whether it loops are all
        steward's questions, answered in :mod:`steward.delegation` before this is called —
        the store records facts and refuses none.
        """
        record = JobRecord(
            task_id=task_id or new_id(),
            title=title,
            detail=detail,
            required_skills=(),
            status=STATUS_OPEN,
            posted_by=delegated_by,
            created_at=utc_now_iso(),
            assignee=assignee,
            delegated_by=delegated_by,
            route=route,
            parent_task_id=parent_task_id,
            origin=origin,
            depth=depth,
        )
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO jobs (task_id, title, detail, required_skills, status, "
                "posted_by, claimant, created_at, assignee, delegated_by, route, "
                "parent_task_id, origin, depth) "
                "VALUES (?, ?, ?, '[]', ?, ?, NULL, ?, ?, ?, ?, ?, ?, ?)",
                (
                    record.task_id,
                    record.title,
                    record.detail,
                    record.status,
                    record.posted_by,
                    record.created_at,
                    record.assignee,
                    record.delegated_by,
                    record.route,
                    record.parent_task_id,
                    record.origin,
                    record.depth,
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

    def inbox(self, assignee: str, status: str | None = STATUS_OPEN) -> list[JobRecord]:
        """Return one resident's delegated items, oldest first.

        The default is the pending inbox — what is waiting to be picked up. ``status=None``
        is everything ever addressed to this resident, which is the audit view.
        """
        query = "SELECT * FROM jobs WHERE assignee = ?"
        params: tuple[str, ...] = (assignee,)
        if status is not None:
            query += " AND status = ?"
            params = (assignee, status)
        with self._lock:
            rows = self._conn.execute(f"{query} ORDER BY created_at, rowid", params).fetchall()
        return [JobRecord.from_row(row) for row in rows]

    def inbox_count(self, assignee: str, status: str | None = STATUS_OPEN) -> int:
        """Return how many items sit in one resident's inbox, without reading them.

        ``doctor`` and the console want the size of the pile, not the letters — a count is
        the whole answer there, and reading every row to call ``len`` on it is a page of
        work to print one number.
        """
        query = "SELECT COUNT(*) FROM jobs WHERE assignee = ?"
        params: tuple[str, ...] = (assignee,)
        if status is not None:
            query += " AND status = ?"
            params = (assignee, status)
        with self._lock:
            row = self._conn.execute(query, params).fetchone()
        return int(row[0])

    def lineage(self, task_id: str) -> list[JobRecord]:
        """Return the chain this task belongs to, root first, ending at the task itself.

        Walks ``parent_task_id`` upwards, which is the only direction the chain is written
        in, and stops on an id it has already seen — a database somebody hand-edited into
        a loop is a corrupt database, not an infinite loop in a CLI. A task nobody
        delegated is a chain of one, which is a real answer.
        """
        chain: list[JobRecord] = []
        seen: set[str] = set()
        cursor: str | None = task_id
        while cursor is not None and cursor not in seen:
            seen.add(cursor)
            record = self.job(cursor)
            if record is None:
                break
            chain.append(record)
            cursor = record.parent_task_id
        return list(reversed(chain))

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
        lease_duration_s: float | None = None,
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

        Work addressed to somebody — a delegated item, with an ``assignee`` — is not on
        the open board and is never returned here, however well the skills match. Reading
        another villager's letter off the notice board is not claiming.
        """
        held = frozenset(skills)
        moment = now or utc_now_iso()
        with self._lock, self._conn:
            candidates = self._conn.execute(
                "SELECT * FROM jobs WHERE status = ? AND assignee IS NULL "
                "ORDER BY created_at, rowid",
                (STATUS_OPEN,),
            ).fetchall()
            for row in candidates:
                record = JobRecord.from_row(row)
                if not record.claimable_by <= held:
                    continue
                cursor = self._conn.execute(
                    "UPDATE jobs SET status = ?, claimant = ?, claimed_at = ?, "
                    "lease_expires_at = ?, lease_duration_s = ?, run_id = NULL, "
                    "owner_token = NULL WHERE task_id = ? AND status = ?",
                    (
                        STATUS_CLAIMED,
                        claimant,
                        moment,
                        lease_expires_at,
                        lease_duration_s,
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

    def claim_next_delegated(
        self,
        *,
        assignee: str,
        claimant: str,
        lease_expires_at: str,
        lease_duration_s: float | None = None,
        now: str | None = None,
    ) -> JobRecord | None:
        """Atomically pick up the oldest item waiting in one resident's inbox.

        The board's conditional write, narrowed by the addressee::

            UPDATE jobs SET status='claimed' … WHERE task_id=? AND status='open'
                AND assignee=?

        No skill matching: the sender named this resident and this resident's own manifest
        declares a route that accepts the work, which is the whole of the agreement. A
        second skills veto here would let steward silently drop a letter both ends said yes
        to, and a dropped letter is the one thing an inbox may not do.

        ``claimant`` is the burrow agent id the pickup is recorded and emitted under;
        ``assignee`` is the resident id the item was addressed to.
        """
        moment = now or utc_now_iso()
        with self._lock, self._conn:
            candidates = self._conn.execute(
                "SELECT * FROM jobs WHERE status = ? AND assignee = ? ORDER BY created_at, rowid",
                (STATUS_OPEN, assignee),
            ).fetchall()
            for row in candidates:
                record = JobRecord.from_row(row)
                cursor = self._conn.execute(
                    "UPDATE jobs SET status = ?, claimant = ?, claimed_at = ?, "
                    "lease_expires_at = ?, lease_duration_s = ?, run_id = NULL, "
                    "owner_token = NULL WHERE task_id = ? AND status = ? AND assignee = ?",
                    (
                        STATUS_CLAIMED,
                        claimant,
                        moment,
                        lease_expires_at,
                        lease_duration_s,
                        record.task_id,
                        STATUS_OPEN,
                        assignee,
                    ),
                )
                if cursor.rowcount == 0:
                    continue  # Two wake-ups of the same resident raced; one of them won.
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
        lease: str | None = None,
        now: str | None = None,
    ) -> JobRecord | None:
        """Close out a claimed task. Only its own claimant may, and only once.

        Conditional on ``status = 'claimed' AND claimant = ?`` so a resident whose lease
        already expired — and whose task is open again, or held by somebody else — cannot
        come back and mark somebody else's work done.

        ``lease`` is the token :meth:`claim_next_job` / :meth:`claim_next_delegated` handed
        back (the ``claimed_at`` stamp of *this* claim). When given, the close is also
        conditional on ``claimed_at = lease``, so a session whose lease expired, was swept,
        and re-claimed — by itself or by anyone — cannot come back and close the *live*
        claim it no longer holds (steward #72). A dead handle carries the old stamp; the
        row now carries the new one, and the stale close matches nothing.
        """
        with self._lock, self._conn:
            cursor = self._conn.execute(
                "UPDATE jobs SET status = ?, outcome = ?, reason = ?, artifacts = ?, "
                "finished_at = ?, lease_expires_at = NULL, run_id = NULL, owner_token = NULL "
                "WHERE task_id = ? AND status = ? AND claimant = ? "
                "AND (? IS NULL OR claimed_at = ?)",
                (
                    status,
                    outcome,
                    reason,
                    _dumps(list(artifacts)),
                    now or utc_now_iso(),
                    task_id,
                    STATUS_CLAIMED,
                    claimant,
                    lease,
                    lease,
                ),
            )
            if cursor.rowcount == 0:
                return None
            row = self._conn.execute("SELECT * FROM jobs WHERE task_id = ?", (task_id,)).fetchone()
        return JobRecord.from_row(row)

    def finish_job_and_claim_run_terminal(  # noqa: PLR0913
        self,
        task_id: str,
        *,
        run_id: str,
        event: str,
        event_id: str,
        status: str,
        claimant: str,
        outcome: str | None = None,
        reason: str | None = None,
        artifacts: Sequence[str] = (),
        lease: str | None = None,
        owner_token: str | None = None,
        stale_before: str | None = None,
        now: str | None = None,
    ) -> JobRecord | None:
        """Close one claim and choose its immutable terminal fact in one transaction."""
        moment = now or utc_now_iso()
        try:
            with self._lock, self._conn:
                if owner_token is not None:
                    run = self._conn.execute(
                        "UPDATE open_runs SET terminal_event = ?, terminal_event_id = ?, "
                        "terminal_claimed_at = ? WHERE run_id = ? AND closed_at IS NULL "
                        "AND terminal_event IS NULL AND owner_token = ?",
                        (event, event_id, moment, run_id, owner_token),
                    )
                else:
                    run = self._conn.execute(
                        "UPDATE open_runs SET terminal_event = ?, terminal_event_id = ?, "
                        "terminal_claimed_at = ? WHERE run_id = ? AND closed_at IS NULL "
                        "AND terminal_event IS NULL AND heartbeat_at <= ?",
                        (event, event_id, moment, run_id, stale_before),
                    )
                if run.rowcount == 0:
                    raise _AtomicTaskCloseLostError  # noqa: TRY301
                job = self._conn.execute(
                    "UPDATE jobs SET status = ?, outcome = ?, reason = ?, artifacts = ?, "
                    "finished_at = ?, lease_expires_at = NULL, run_id = NULL, owner_token = NULL "
                    "WHERE task_id = ? AND status = ? AND claimant = ? "
                    "AND (? IS NULL OR claimed_at = ?) AND run_id = ? AND owner_token = ?",
                    (
                        status,
                        outcome,
                        reason,
                        _dumps(list(artifacts)),
                        moment,
                        task_id,
                        STATUS_CLAIMED,
                        claimant,
                        lease,
                        lease,
                        run_id,
                        owner_token,
                    ),
                )
                if job.rowcount == 0:
                    raise _AtomicTaskCloseLostError  # noqa: TRY301
                row = self._conn.execute(
                    "SELECT * FROM jobs WHERE task_id = ?", (task_id,)
                ).fetchone()
        except _AtomicTaskCloseLostError:
            return None
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
                if record.run_id is not None:
                    # Bound attempts need a run-specific terminal choice; callers using
                    # the legacy API must not split those two durable facts.
                    continue
                # An upgraded database may contain a claimed job and its still-open run
                # from before the association columns existed.  Silence is safer than
                # reopening underneath a possibly live owner; a later operator/sweep can
                # resolve the legacy row once the run is answered.
                legacy = self._conn.execute(
                    "SELECT 1 FROM open_runs WHERE kind IN (?, ?) AND ref = ? "
                    "AND closed_at IS NULL LIMIT 1",
                    (RUN_TASK, RUN_DELEGATED, record.task_id),
                ).fetchone()
                if legacy is not None:
                    continue
                cursor = self._conn.execute(
                    "UPDATE jobs SET status = ?, claimant = NULL, claimed_at = NULL, "
                    "lease_expires_at = NULL, lease_duration_s = NULL WHERE task_id = ? "
                    "AND status = ? AND claimed_at = ?",
                    (STATUS_OPEN, record.task_id, STATUS_CLAIMED, record.claimed_at),
                )
                if cursor.rowcount == 1:
                    expired.append(record)
        return expired

    def expire_task_attempt_and_claim_terminal(  # noqa: PLR0913
        self,
        task_id: str,
        *,
        lease: str,
        run_id: str,
        owner_token: str,
        event: str,
        event_id: str,
        now: str | None = None,
    ) -> JobRecord | None:
        """Atomically reopen one dead attempt and choose its exact run failure."""
        moment = now or utc_now_iso()
        try:
            with self._lock, self._conn:
                row = self._conn.execute(
                    "SELECT * FROM jobs WHERE task_id = ? AND status = ? AND claimed_at = ? "
                    "AND run_id = ? AND owner_token = ? AND lease_expires_at <= ?",
                    (task_id, STATUS_CLAIMED, lease, run_id, owner_token, moment),
                ).fetchone()
                if row is None:
                    raise _AtomicTaskCloseLostError  # noqa: TRY301
                run = self._conn.execute(
                    "UPDATE open_runs SET terminal_event = ?, terminal_event_id = ?, "
                    "terminal_claimed_at = ? WHERE run_id = ? AND owner_token = ? "
                    "AND closed_at IS NULL AND terminal_event IS NULL",
                    (event, event_id, moment, run_id, owner_token),
                )
                job = self._conn.execute(
                    "UPDATE jobs SET status = ?, claimant = NULL, claimed_at = NULL, "
                    "lease_expires_at = NULL, lease_duration_s = NULL, run_id = NULL, "
                    "owner_token = NULL WHERE task_id = ? AND status = ? AND claimed_at = ? "
                    "AND run_id = ? AND owner_token = ? AND lease_expires_at <= ?",
                    (
                        STATUS_OPEN,
                        task_id,
                        STATUS_CLAIMED,
                        lease,
                        run_id,
                        owner_token,
                        moment,
                    ),
                )
                if run.rowcount != 1 or job.rowcount != 1:
                    raise _AtomicTaskCloseLostError  # noqa: TRY301
        except _AtomicTaskCloseLostError:
            return None
        return JobRecord.from_row(row)

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
        denied_by: str | None = None,
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

        ``denied_by``, when given, files the request already resolved as a deny rather
        than pending — a decision steward made itself, with nobody waiting on it. It is
        how the repeat-deny guard (:mod:`steward.transitions.approval`) records an ask it
        answered instead of knocking about: the row still exists, so the ledger shows
        the resident asked and what it was told, and the resident hears the answer in the
        next preamble like any other decision.
        """
        moment = utc_now_iso()
        denied = denied_by is not None
        record = ApprovalRecord(
            request_id=request_id or new_id(),
            agent_id=agent_id,
            project=project,
            action=action,
            message=message,
            detail=dict(detail or {}),
            options=tuple(options),
            status=STATUS_RESOLVED if denied else STATUS_PENDING,
            created_at=moment,
            resident=resident,
            expires_at=expires_at,
            decision="deny" if denied else None,
            decided_by=denied_by,
            decided_at=moment if denied else None,
        )
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO approvals (request_id, agent_id, project, action, message, "
                "detail, options, status, created_at, expires_at, resident, decision, "
                "decided_by, decided_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
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
                    record.decision,
                    record.decided_by,
                    record.decided_at,
                ),
            )
        return record

    def recent_denials(self, resident: str, action: str, since: str) -> int:
        """Count the times this resident was told no about this action since ``since``.

        A deny is ``status='resolved'`` with ``decision='deny'``, clocked by
        ``decided_at``, so both a human's deny and expiry's deny-by-default count: from
        the resident's side, "nobody answered in time" and "a person said no" are the same
        answer, and both mean the action did not happen.

        Steward's own repeat auto-denials (``decided_by='repeat'``) are **not** counted, on
        purpose. If they were, every swallowed ask would push the window out again and one
        deny would silence an action forever — a permanent ban nobody chose. The window is
        measured from a real decision.
        """
        with self._lock:
            row = self._conn.execute(
                "SELECT COUNT(*) AS denials FROM approvals WHERE resident = ? AND action = ? "
                "AND status = ? AND decision = ? AND decided_at IS NOT NULL AND decided_at >= ? "
                "AND (decided_by IS NULL OR decided_by != ?)",
                (resident, action, STATUS_RESOLVED, "deny", since, DECIDED_BY_REPEAT),
            ).fetchone()
        return int(row["denials"])

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

    def claim_undelivered_decisions(
        self, resident: str, now: str | None = None
    ) -> list[ApprovalRecord]:
        """Take this resident's decided-but-untold decisions, marking them delivered, atomically.

        The read and the mark are one transaction under one lock, so two sessions of the
        same resident waking at the same instant cannot both walk away believing they were
        handed the same answer (steward #74). Each ``UPDATE`` is conditional on
        ``delivered_at IS NULL``, so only the records *this* call actually flips are
        returned; a concurrent caller gets the ones it flipped, or none. Told once, to one
        session — the honest reading of "the decision reached the resident".
        """
        moment = now or utc_now_iso()
        claimed: list[ApprovalRecord] = []
        with self._lock, self._conn:
            rows = self._conn.execute(
                "SELECT * FROM approvals WHERE resident = ? AND status = ? "
                "AND delivered_at IS NULL ORDER BY decided_at, rowid",
                (resident, STATUS_RESOLVED),
            ).fetchall()
            for row in rows:
                cursor = self._conn.execute(
                    "UPDATE approvals SET delivered_at = ? "
                    "WHERE request_id = ? AND delivered_at IS NULL",
                    (moment, row["request_id"]),
                )
                if cursor.rowcount == 1:
                    fresh = self._conn.execute(
                        "SELECT * FROM approvals WHERE request_id = ?", (row["request_id"],)
                    ).fetchone()
                    claimed.append(ApprovalRecord.from_row(fresh))
        return claimed

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
        now: str | None = None,
    ) -> tuple[ApprovalRecord | None, bool]:
        """Record a decision. Returns the record and whether *this* call recorded it.

        The first decision wins. A replay — a double-tapped notification, a retried
        request — changes nothing and reads back what was already recorded, which is
        what makes the endpoint idempotent all the way down to the disk.

        An **expired** request is never decided, however it is answered (steward #66): the
        conditional write is narrowed by ``expires_at IS NULL OR expires_at > now``, so a
        human clicking *approve* a minute after the deadline cannot slip an action through
        ahead of the deny-by-default sweep. That case is distinct and readable in the
        return: ``recorded`` is ``False`` and the record comes back *still pending* (rather
        than resolved, which is what a replay of an already-decided request reads back), so
        a caller can tell "too late, it expired" from "somebody already answered". The
        sweep (:meth:`expire_approvals`) still denies it and closes the loop in the log.
        """
        moment = now or utc_now_iso()
        with self._lock, self._conn:
            existing = self._conn.execute(
                "SELECT request_id FROM approvals WHERE request_id = ?", (request_id,)
            ).fetchone()
            if existing is None:
                return None, False
            cursor = self._conn.execute(
                "UPDATE approvals SET status = ?, decision = ?, decided_by = ?, "
                "decided_at = ?, edit = ? WHERE request_id = ? AND status = ? "
                "AND (expires_at IS NULL OR expires_at > ?)",
                (
                    STATUS_RESOLVED,
                    decision,
                    decided_by,
                    moment,
                    _dumps(dict(edit)) if edit else None,
                    request_id,
                    STATUS_PENDING,
                    moment,
                ),
            )
            recorded = cursor.rowcount == 1
            row = self._conn.execute(
                "SELECT * FROM approvals WHERE request_id = ?", (request_id,)
            ).fetchone()
        return ApprovalRecord.from_row(row), recorded

    # -- the run ledger ----------------------------------------------------------------

    def record_run(  # noqa: PLR0913 — one keyword per column of the entry
        self,
        *,
        resident: str,
        agent_id: str,
        kind: str,
        run_id: str,
        ref: str = "",
        origin: str = "",
        outcome: str = "",
        input_tokens: int = 0,
        output_tokens: int = 0,
        cost_usd: float = 0.0,
        duration_s: float = 0.0,
        usage_known: bool = True,
        now: str | None = None,
    ) -> LedgerEntry:
        """Append what one finished session cost. Append-only, and never revised.

        Every run gets a row, including a failed one and one steward killed at its
        timeout: a session that burned four minutes and produced nothing still burned
        four minutes, and a budget that only counted successes would be a budget that
        rewards crashing.

        ``origin`` is what the run descends from, recorded here rather than reconstructed
        later: a caller that knows the chain says so, and the row stops depending on a
        join that can only guess.
        """
        entry = LedgerEntry(
            entry_id=new_id(),
            resident=resident,
            agent_id=agent_id,
            kind=kind,
            run_id=run_id,
            ref=ref,
            origin=origin,
            outcome=outcome,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cost_usd=cost_usd,
            duration_s=duration_s,
            usage_known=usage_known,
            recorded_at=now or utc_now_iso(),
        )
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO run_ledger (entry_id, resident, agent_id, kind, run_id, ref, "
                "origin, outcome, input_tokens, output_tokens, cost_usd, duration_s, "
                "usage_known, recorded_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    entry.entry_id,
                    entry.resident,
                    entry.agent_id,
                    entry.kind,
                    entry.run_id,
                    entry.ref,
                    entry.origin,
                    entry.outcome,
                    entry.input_tokens,
                    entry.output_tokens,
                    entry.cost_usd,
                    entry.duration_s,
                    int(entry.usage_known),
                    entry.recorded_at,
                ),
            )
        return entry

    def ledger(
        self,
        resident: str | None = None,
        *,
        since: str | None = None,
        until: str | None = None,
    ) -> list[LedgerEntry]:
        """Return ledger entries, oldest first, optionally within a half-open window.

        ``since``/``until`` are protocol timestamps and the window is ``[since, until)``,
        so two adjacent days can never both count the same run. The window is computed by
        the caller at *read* time from real calendar arithmetic — nothing here is reset,
        rolled over, or zeroed by a process starting up.
        """
        clauses: list[str] = []
        params: list[str] = []
        if resident is not None:
            clauses.append("resident = ?")
            params.append(resident)
        if since is not None:
            clauses.append("recorded_at >= ?")
            params.append(since)
        if until is not None:
            clauses.append("recorded_at < ?")
            params.append(until)
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        with self._lock:
            rows = self._conn.execute(
                f"SELECT * FROM run_ledger{where} ORDER BY recorded_at, rowid",  # noqa: S608
                params,
            ).fetchall()
        return [LedgerEntry.from_row(row) for row in rows]

    def spend_by_origin(
        self,
        *,
        since: str | None = None,
        until: str | None = None,
    ) -> list[OriginSpend]:
        """Roll today's ledger up by the origin each run's task descends from.

        The question delegation's ``origin`` column was recorded to answer: *what did the
        fleet spend answering one question?* A chain rolls up to its root — every hop
        inherits the same origin (:func:`steward.delegation.origin_for`) — so the cost of
        an answer stays with the answer rather than scattering across whichever residents
        happened to be in the line.

        The origin is read off the ledger row itself, written there by whoever recorded
        the run. It used to be inferred by joining ``run_ledger.ref`` to ``jobs.task_id``,
        which is only a task id for board and delegated runs — a routine whose ref
        collided with a task's id inherited that task's bill. The join survives for rows
        written before the column existed, and nothing else.

        A run that descends from nobody — one recorded without an origin, on an old
        database — is reported under :data:`ORIGIN_UNATTRIBUTED` rather than dropped:
        money steward cannot attribute is still money somebody spent.
        """
        clauses: list[str] = []
        params: list[str] = []
        if since is not None:
            clauses.append("l.recorded_at >= ?")
            params.append(since)
        if until is not None:
            clauses.append("l.recorded_at < ?")
            params.append(until)
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        with self._lock:
            rows = self._conn.execute(
                "SELECT COALESCE(NULLIF(l.origin, ''), NULLIF(j.origin, ''), ?) AS origin, "  # noqa: S608
                "COUNT(*) AS runs, "
                "SUM(l.cost_usd) AS cost_usd, "
                "SUM(l.input_tokens + l.output_tokens) AS tokens, "
                "SUM(l.duration_s) AS duration_s "
                "FROM run_ledger l LEFT JOIN jobs j ON j.task_id = l.ref"
                # Grouped by ordinal, not by name: both joined tables have an ``origin``
                # column now, so ``GROUP BY origin`` is ambiguous rather than the alias.
                f"{where} GROUP BY 1 ORDER BY cost_usd DESC, 1",
                (ORIGIN_UNATTRIBUTED, *params),
            ).fetchall()
        return [
            OriginSpend(
                origin=row["origin"],
                runs=row["runs"],
                cost_usd=row["cost_usd"] or 0.0,
                tokens=row["tokens"] or 0,
                duration_s=row["duration_s"] or 0.0,
            )
            for row in rows
        ]

    # -- budget pauses -------------------------------------------------------------------

    def pause_resident(  # noqa: PLR0913 — one keyword per column of the pause
        self,
        *,
        resident: str,
        agent_id: str,
        budget: str,
        spent: float,
        cap: float,
        reason: str = "",
        request_id: str | None = None,
        window_end: str = "",
        now: str | None = None,
    ) -> tuple[PauseRecord, bool]:
        """Stop firing this resident. Returns the pause and whether *this* call made it.

        ``INSERT … ON CONFLICT DO NOTHING`` is the whole dedupe: the first refusal writes
        the row and knocks at the door, and every later refusal — the next scheduled fire,
        the next board sweep, a run-now — reads back the same row and stays quiet. One
        knock per pause, not one per refused fire, without a flag anybody has to remember
        to clear.
        """
        moment = now or utc_now_iso()
        with self._lock, self._conn:
            cursor = self._conn.execute(
                "INSERT INTO budget_pauses (resident, agent_id, budget, spent, cap, reason, "
                "request_id, window_end, paused_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(resident) DO NOTHING",
                (resident, agent_id, budget, spent, cap, reason, request_id, window_end, moment),
            )
            created = cursor.rowcount == 1
            row = self._conn.execute(
                "SELECT * FROM budget_pauses WHERE resident = ?", (resident,)
            ).fetchone()
        return PauseRecord.from_row(row), created

    def budget_pause(self, resident: str) -> PauseRecord | None:
        """Return this resident's pause, or ``None`` when it is free to run."""
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM budget_pauses WHERE resident = ?", (resident,)
            ).fetchone()
        return PauseRecord.from_row(row) if row else None

    def budget_pauses(self) -> list[PauseRecord]:
        """Return every paused resident, oldest pause first. The 'who is stopped' view."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM budget_pauses ORDER BY paused_at, resident"
            ).fetchall()
        return [PauseRecord.from_row(row) for row in rows]

    def pause_for_request(self, request_id: str) -> PauseRecord | None:
        """Return the pause a given approval request was raised for, if there is one."""
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM budget_pauses WHERE request_id = ?", (request_id,)
            ).fetchone()
        return PauseRecord.from_row(row) if row else None

    def unpause_resident(self, resident: str) -> PauseRecord | None:
        """Lift a pause and return what it was. ``None`` when nothing was paused.

        Deleting rather than flagging, because a lifted pause is not a fact about the
        resident any more — what it *cost* is still in the ledger, which is where the
        history belongs.
        """
        with self._lock, self._conn:
            row = self._conn.execute(
                "SELECT * FROM budget_pauses WHERE resident = ?", (resident,)
            ).fetchone()
            if row is None:
                return None
            self._conn.execute("DELETE FROM budget_pauses WHERE resident = ?", (resident,))
        return PauseRecord.from_row(row)

    def grant_budget_allowance(
        self,
        resident: str,
        *,
        until: str,
        granted_by: str,
        reason: str = "",
        now: str | None = None,
    ) -> None:
        """Record that a human said carry on, and until when.

        Overwrites any earlier allowance for the same resident: a second "carry on" is a
        newer answer to the same question, not a second answer to be reconciled.
        """
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO budget_allowances (resident, until, granted_by, reason, granted_at) "
                "VALUES (?, ?, ?, ?, ?) ON CONFLICT(resident) DO UPDATE SET "
                "until = excluded.until, granted_by = excluded.granted_by, "
                "reason = excluded.reason, granted_at = excluded.granted_at",
                (resident, until, granted_by, reason, now or utc_now_iso()),
            )

    def budget_allowance(self, resident: str) -> dict[str, Any] | None:
        """Return the standing "carry on" for this resident, if a human granted one."""
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM budget_allowances WHERE resident = ?", (resident,)
            ).fetchone()
        if row is None:
            return None
        return {
            "resident": row["resident"],
            "until": row["until"],
            "granted_by": row["granted_by"],
            "reason": row["reason"],
            "granted_at": row["granted_at"],
        }

    # -- the watchdog's own memory -------------------------------------------------------

    def watchdog_attempt(self, resident: str) -> WatchdogAttempt:
        """Return this resident's restart budget. An untouched resident is a fresh one."""
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM watchdog_attempts WHERE resident = ?", (resident,)
            ).fetchone()
        return WatchdogAttempt.from_row(row) if row else WatchdogAttempt(resident=resident)

    def record_watchdog_attempt(
        self,
        resident: str,
        *,
        reason: str,
        next_attempt_at: str | None,
        now: str | None = None,
    ) -> WatchdogAttempt:
        """Count one restart against this resident's budget and say when the next may be."""
        moment = now or utc_now_iso()
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO watchdog_attempts (resident, attempts, reason, last_attempt_at, "
                "next_attempt_at) VALUES (?, 1, ?, ?, ?) ON CONFLICT(resident) DO UPDATE SET "
                "attempts = attempts + 1, reason = excluded.reason, "
                "last_attempt_at = excluded.last_attempt_at, "
                "next_attempt_at = excluded.next_attempt_at",
                (resident, reason, moment, next_attempt_at),
            )
            row = self._conn.execute(
                "SELECT * FROM watchdog_attempts WHERE resident = ?", (resident,)
            ).fetchone()
        return WatchdogAttempt.from_row(row)

    def give_up_on(self, resident: str, *, reason: str, now: str | None = None) -> bool:
        """Stop restarting this resident. Returns whether *this* call gave up.

        Conditional on ``gave_up_at IS NULL``, for the same reason a budget pause is
        conditional: the crash-loop knock is one knock, not one per pass for as long as
        the container stays down.
        """
        moment = now or utc_now_iso()
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO watchdog_attempts (resident, attempts, reason) "
                "VALUES (?, 0, ?) ON CONFLICT(resident) DO NOTHING",
                (resident, reason),
            )
            cursor = self._conn.execute(
                "UPDATE watchdog_attempts SET gave_up_at = ?, reason = ? "
                "WHERE resident = ? AND gave_up_at IS NULL",
                (moment, reason, resident),
            )
            return cursor.rowcount == 1

    def clear_watchdog_attempts(self, resident: str) -> None:
        """Forget a resident's restart history, because it came back healthy."""
        with self._lock, self._conn:
            self._conn.execute("DELETE FROM watchdog_attempts WHERE resident = ?", (resident,))

    def close_unbracketed_run(
        self, *, run_id: str, agent_id: str, routine: str, started_at: str, now: str | None = None
    ) -> bool:
        """Mark a run that never reported back as closed. Returns whether this call did it.

        The row is the receipt for a ``routine_failed`` steward emitted on the session's
        behalf. Conditional on the primary key, so a run that vanished is buried once even
        if the watchdog passes over it every minute for a week.
        """
        with self._lock, self._conn:
            cursor = self._conn.execute(
                "INSERT INTO unbracketed_runs (run_id, agent_id, routine, started_at, closed_at) "
                "VALUES (?, ?, ?, ?, ?) ON CONFLICT(run_id) DO NOTHING",
                (run_id, agent_id, routine, started_at, now or utc_now_iso()),
            )
            return cursor.rowcount == 1

    def closed_unbracketed_runs(self) -> set[str]:
        """Return every run steward has already buried, so nobody mourns one twice."""
        with self._lock:
            rows = self._conn.execute("SELECT run_id FROM unbracketed_runs").fetchall()
        return {row["run_id"] for row in rows}

    # -- the run registry ----------------------------------------------------------------

    def open_run(  # noqa: PLR0913 — one parameter per fact about the session that opened
        self,
        *,
        run_id: str,
        kind: str,
        agent_id: str,
        project: str = "",
        ref: str = "",
        timeout_s: float = 0.0,
        event_log_path: str = "",
        owner_token: str = "",
        now: str | None = None,
    ) -> bool:
        """Record that a session has started. Returns whether this call opened the row.

        Written where the opening event is emitted, so steward's own knowledge that a run
        exists does not depend on where that event ended up. Conditional on the primary
        key like its neighbours: a run id is one *session*, and a second open of the same
        id is a repeat rather than a second session.

        Which puts the burden on callers, and it is worth naming: whatever a session is
        about — a routine, a task — the id has to be the session's own. The board learnt
        this the hard way by keying rows on the task id, so a task claimed, dropped on a
        dead lease and re-claimed opened a row the first attempt had already closed, the
        insert was quietly dropped, and the retry ran unwatched. ``ref`` is where the
        thing the session was about goes; several rows may share one.
        """
        with self._lock, self._conn:
            opened_at = now or utc_now_iso()
            cursor = self._conn.execute(
                "INSERT INTO open_runs (run_id, kind, agent_id, project, ref, timeout_s, "
                "started_at, heartbeat_at, event_log_path, evidence_version, owner_token) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?) ON CONFLICT(run_id) DO NOTHING",
                (
                    run_id,
                    kind,
                    agent_id,
                    project,
                    ref,
                    float(timeout_s),
                    opened_at,
                    opened_at,
                    event_log_path,
                    owner_token,
                ),
            )
            return cursor.rowcount == 1

    def open_task_run(  # noqa: PLR0913
        self,
        *,
        task_id: str,
        lease: str,
        run_id: str,
        kind: str,
        agent_id: str,
        project: str = "",
        ref: str = "",
        timeout_s: float = 0.0,
        event_log_path: str = "",
        owner_token: str,
        now: str | None = None,
    ) -> bool:
        """Open and bind a task attempt in one transaction.

        The claim stamp fences the binding.  A swept/retried job cannot accidentally be
        attached to the late predecessor, and an inserted run is rolled back when the
        binding loses that race.
        """
        moment = now or utc_now_iso()
        try:
            with self._lock, self._conn:
                opened = self._conn.execute(
                    "INSERT INTO open_runs (run_id, kind, agent_id, project, ref, timeout_s, "
                    "started_at, heartbeat_at, event_log_path, evidence_version, owner_token) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?) ON CONFLICT(run_id) DO NOTHING",
                    (
                        run_id,
                        kind,
                        agent_id,
                        project,
                        ref,
                        float(timeout_s),
                        moment,
                        moment,
                        event_log_path,
                        owner_token,
                    ),
                )
                if opened.rowcount != 1:
                    raise _AtomicTaskCloseLostError  # noqa: TRY301
                bound = self._conn.execute(
                    "UPDATE jobs SET run_id = ?, owner_token = ? WHERE task_id = ? "
                    "AND status = ? AND claimed_at = ? AND run_id IS NULL",
                    (run_id, owner_token, task_id, STATUS_CLAIMED, lease),
                )
                if bound.rowcount != 1:
                    raise _AtomicTaskCloseLostError  # noqa: TRY301
        except _AtomicTaskCloseLostError:
            return False
        return True

    def renew_task_run(
        self,
        run_id: str,
        *,
        owner_token: str,
        now: str | None = None,
    ) -> bool:
        """Atomically renew the run heartbeat and its exact job attempt lease."""
        moment = now or utc_now_iso()
        try:
            with self._lock, self._conn:
                row = self._conn.execute(
                    "SELECT task_id, claimed_at, lease_duration_s FROM jobs WHERE run_id = ? "
                    "AND owner_token = ? AND status = ?",
                    (run_id, owner_token, STATUS_CLAIMED),
                ).fetchone()
                if row is None or row["lease_duration_s"] is None:
                    raise _AtomicTaskCloseLostError  # noqa: TRY301
                lease_end = utc_now_iso(
                    datetime.fromisoformat(moment)
                    + timedelta(seconds=float(row["lease_duration_s"]))
                )
                run = self._conn.execute(
                    "UPDATE open_runs SET heartbeat_at = ? WHERE run_id = ? AND closed_at IS NULL "
                    "AND terminal_event IS NULL AND owner_token = ?",
                    (moment, run_id, owner_token),
                )
                job = self._conn.execute(
                    "UPDATE jobs SET lease_expires_at = ? WHERE task_id = ? AND status = ? "
                    "AND claimed_at = ? AND run_id = ? AND owner_token = ?",
                    (
                        lease_end,
                        row["task_id"],
                        STATUS_CLAIMED,
                        row["claimed_at"],
                        run_id,
                        owner_token,
                    ),
                )
                if run.rowcount != 1 or job.rowcount != 1:
                    raise _AtomicTaskCloseLostError  # noqa: TRY301
        except _AtomicTaskCloseLostError:
            return False
        return True

    def renew_run(self, run_id: str, *, owner_token: str = "", now: str | None = None) -> bool:
        """Renew an open run's ownership lease; a terminal run stays terminal."""
        with self._lock, self._conn:
            cursor = self._conn.execute(
                "UPDATE open_runs SET heartbeat_at = ? WHERE run_id = ? AND closed_at IS NULL "
                "AND terminal_event IS NULL AND owner_token = ?",
                (now or utc_now_iso(), run_id, owner_token),
            )
            return cursor.rowcount == 1

    def close_stale_run(self, run_id: str, *, stale_before: str, now: str | None = None) -> bool:
        """Atomically close a run only if its ownership lease remains expired."""
        with self._lock, self._conn:
            cursor = self._conn.execute(
                "UPDATE open_runs SET closed_at = ? WHERE run_id = ? AND closed_at IS NULL "
                "AND heartbeat_at <= ?",
                (now or utc_now_iso(), run_id, stale_before),
            )
            return cursor.rowcount == 1

    def claim_run_terminal(  # noqa: PLR0913 - authority is owner token or stale cutoff
        self,
        run_id: str,
        *,
        event: str,
        event_id: str,
        owner_token: str | None = None,
        stale_before: str | None = None,
        now: str | None = None,
    ) -> bool:
        """Choose one immutable terminal fact, by the live owner or an expired watchdog."""
        with self._lock, self._conn:
            prefix = (
                "UPDATE open_runs SET terminal_event = ?, terminal_event_id = ?, "
                "terminal_claimed_at = ? WHERE run_id = ? AND closed_at IS NULL "
                "AND terminal_event IS NULL AND "
            )
            if owner_token is not None:
                cursor = self._conn.execute(
                    prefix + "owner_token = ?",
                    (event, event_id, now or utc_now_iso(), run_id, owner_token),
                )
            else:
                cursor = self._conn.execute(
                    prefix + "heartbeat_at <= ?",
                    (event, event_id, now or utc_now_iso(), run_id, stale_before),
                )
            return cursor.rowcount == 1

    def terminal_runs(self) -> list[OpenRun]:
        """Return chosen terminal facts that still need durable publication/finalization."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM open_runs WHERE terminal_event IS NOT NULL AND closed_at IS NULL "
                "ORDER BY terminal_claimed_at, rowid"
            ).fetchall()
        return [OpenRun.from_row(row) for row in rows]

    def mark_run_terminal_published(
        self, run_id: str, event_id: str, *, now: str | None = None
    ) -> bool:
        """Finalize only the immutable chosen event identified by ``event_id``."""
        moment = now or utc_now_iso()
        with self._lock, self._conn:
            cursor = self._conn.execute(
                "UPDATE open_runs SET terminal_published_at = COALESCE(terminal_published_at, ?), "
                "closed_at = ? WHERE run_id = ? AND terminal_event_id = ? AND closed_at IS NULL",
                (moment, moment, run_id, event_id),
            )
            return cursor.rowcount == 1

    def close_run(self, run_id: str, *, now: str | None = None) -> bool:
        """Record that a session reported back. Returns whether this call closed the row.

        Conditional on ``closed_at IS NULL``, so the answer is about *this* call: a run
        the watchdog already buried does not get closed a second time by a session that
        turned up late, and the row keeps the earlier moment it was answered.
        """
        with self._lock, self._conn:
            cursor = self._conn.execute(
                "UPDATE open_runs SET closed_at = ? WHERE run_id = ? AND closed_at IS NULL",
                (now or utc_now_iso(), run_id),
            )
            return cursor.rowcount == 1

    def open_runs(self) -> list[OpenRun]:
        """Return every run steward started and has heard nothing back about, oldest first."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM open_runs WHERE closed_at IS NULL ORDER BY started_at, rowid"
            ).fetchall()
        return [OpenRun.from_row(row) for row in rows]

    def record_watchdog_pass(self, *, interventions: int = 0, now: str | None = None) -> str:
        """Record that the watchdog made a pass, and return when. ``doctor`` reads this."""
        moment = now or utc_now_iso()
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO watchdog_passes (id, last_pass_at, passes, interventions) "
                "VALUES (1, ?, 1, ?) ON CONFLICT(id) DO UPDATE SET last_pass_at = excluded."
                "last_pass_at, passes = passes + 1, interventions = interventions + ?",
                (moment, interventions, interventions),
            )
        return moment

    def last_watchdog_pass(self) -> dict[str, Any] | None:
        """Return when the watchdog last swept, or ``None`` if it never has.

        ``None`` is the important answer: it means nothing is watching, which is exactly
        what ``steward doctor`` has to be able to say out loud.
        """
        with self._lock:
            row = self._conn.execute("SELECT * FROM watchdog_passes WHERE id = 1").fetchone()
        if row is None:
            return None
        return {
            "last_pass_at": row["last_pass_at"],
            "passes": row["passes"],
            "interventions": row["interventions"],
        }

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
