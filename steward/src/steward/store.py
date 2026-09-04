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
  while burrow was unreachable (steward #39). It is also where a session's own scoped
  credential lives, as a digest: the run's lease is the credential's expiry, so
  :meth:`Store.session_principal` accepts one exactly while the watchdog could not yet
  bury the run (steward #41).
- ``resident_claims`` — one row per resident saying which process is currently running a
  session for it. The scheduler's overlap guard used to be an in-process lock, which the
  API, the board and a chat daemon could not see; this is the same guard in the one place
  every firing process can read (warren#111). :mod:`steward.claims` owns what it means.
"""

import json
import sqlite3
import threading
import uuid
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Self

from steward.claims import ResidentClaim
from steward.events import utc_now_iso
from steward.health import HealthJournal
from steward.letter_replies import ANSWER_BATCH_MAX_CHARS, bounded_message, render_answer
from steward.operator_auth import OperatorPrincipal
from steward.runs import (
    DELIVERY_STATUSES,
    RUN_CHAT,
    RUN_DELEGATED,
    RUN_KINDS,
    RUN_ROUTINE,
    RUN_TASK,
    RUN_TRIGGERS,
    DeliveryStatus,
    validate_kind_trigger,
)
from steward.runs import (
    TRIGGER_MANUAL as RUN_TRIGGER_MANUAL,
)
from steward.runs import (
    TRIGGER_SCHEDULE as RUN_TRIGGER_SCHEDULE,
)
from steward.scheduler import default_state_path
from steward.session_auth import SessionPrincipal, credential_digest

__all__ = [
    "APPROVAL_DECISIONS",
    "DECIDED_BY_EXPIRY",
    "DECIDED_BY_REPEAT",
    "JOB_STATUSES",
    "ORIGIN_UNATTRIBUTED",
    "RUN_CHAT",
    "RUN_DELEGATED",
    "RUN_KINDS",
    "RUN_ROUTINE",
    "RUN_TASK",
    "RUN_TRIGGERS",
    "RUN_TRIGGER_MANUAL",
    "RUN_TRIGGER_SCHEDULE",
    "STATUS_OPEN",
    "ApprovalRecord",
    "JobRecord",
    "LedgerEntry",
    "OpenRun",
    "OperatorRecord",
    "OriginSpend",
    "PauseRecord",
    "RequestRecord",
    "ResidentClaim",
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
    detail       TEXT NOT NULL DEFAULT '{}',
    approval_id  TEXT
);

CREATE TABLE IF NOT EXISTS approval_announcements (
    request_id    TEXT PRIMARY KEY REFERENCES approvals(request_id),
    claimed_by    TEXT,
    claimed_until TEXT,
    announced_at  TEXT,
    attempts      INTEGER NOT NULL DEFAULT 0,
    next_attempt_at TEXT,
    effects_at    TEXT,
    effects_claimed_by TEXT,
    effects_claimed_until TEXT,
    effects_attempts INTEGER NOT NULL DEFAULT 0,
    effects_next_attempt_at TEXT
);

CREATE TABLE IF NOT EXISTS run_ledger (
    entry_id      TEXT PRIMARY KEY,
    resident      TEXT NOT NULL,
    agent_id      TEXT NOT NULL,
    kind          TEXT NOT NULL,
    trigger       TEXT NOT NULL DEFAULT '',
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
    trigger    TEXT NOT NULL DEFAULT '',
    agent_id   TEXT NOT NULL,
    project    TEXT NOT NULL DEFAULT '',
    ref        TEXT NOT NULL DEFAULT '',
    timeout_s  REAL NOT NULL DEFAULT 0.0,
    started_at TEXT NOT NULL,
    heartbeat_at TEXT NOT NULL DEFAULT '',
    event_log_path TEXT NOT NULL DEFAULT '',
    evidence_version INTEGER NOT NULL DEFAULT 1,
    owner_token TEXT NOT NULL DEFAULT '',
    resident_id TEXT NOT NULL DEFAULT '',
    session_credential_sha256 TEXT NOT NULL DEFAULT '',
    terminal_event TEXT,
    terminal_event_id TEXT,
    terminal_claimed_at TEXT,
    terminal_published_at TEXT,
    closed_at  TEXT
);

CREATE INDEX IF NOT EXISTS open_runs_still_open
    ON open_runs (closed_at, started_at);

CREATE TABLE IF NOT EXISTS resident_claims (
    resident_id  TEXT PRIMARY KEY,
    token        TEXT NOT NULL,
    holder       TEXT NOT NULL DEFAULT '',
    kind         TEXT NOT NULL DEFAULT '',
    ref          TEXT NOT NULL DEFAULT '',
    run_id       TEXT NOT NULL DEFAULT '',
    claimed_at   TEXT NOT NULL,
    heartbeat_at TEXT NOT NULL,
    released_at  TEXT
);

CREATE TABLE IF NOT EXISTS operator_credentials (
    name        TEXT PRIMARY KEY,
    email       TEXT NOT NULL,
    digest      TEXT NOT NULL,
    note        TEXT NOT NULL DEFAULT '',
    issued_at   TEXT NOT NULL,
    revoked_at  TEXT
);

CREATE INDEX IF NOT EXISTS operator_credentials_live
    ON operator_credentials (digest, revoked_at);
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
        # A terminal delegated task is also the durable reply owed to its sender.
        "final_message": "TEXT NOT NULL DEFAULT ''",
        "reply_delivered_at": "TEXT",
    },
    "approvals": {
        "resident": "TEXT NOT NULL DEFAULT ''",
        "delivered_at": "TEXT",
    },
    "approval_announcements": {
        "attempts": "INTEGER NOT NULL DEFAULT 0",
        "next_attempt_at": "TEXT",
        "effects_at": "TEXT",
        "effects_claimed_by": "TEXT",
        "effects_claimed_until": "TEXT",
        "effects_attempts": "INTEGER NOT NULL DEFAULT 0",
        "effects_next_attempt_at": "TEXT",
    },
    "requests": {
        "approval_id": "TEXT",
    },
    "run_ledger": {
        # Denormalized from the task the run came off (steward #45). Rolling spend up by
        # joining ``ref`` to ``jobs.task_id`` guessed: a routine whose ref happens to
        # equal some task's id would have inherited that task's bill. The row says what
        # it descends from, so the ledger is self-describing and a join cannot misread it.
        "origin": "TEXT NOT NULL DEFAULT ''",
        "trigger": "TEXT NOT NULL DEFAULT ''",
    },
    "open_runs": {
        "trigger": "TEXT NOT NULL DEFAULT ''",
        # Who the session belongs to, and what it may prove that with (steward #41). The
        # resident id rather than only ``agent_id``, because ``agent_id`` is burrow's join
        # key and two manifests may declare the same one, while the sender of a delegated
        # letter has to resolve to exactly one manifest.
        "resident_id": "TEXT NOT NULL DEFAULT ''",
        "session_credential_sha256": "TEXT NOT NULL DEFAULT ''",
        "heartbeat_at": "TEXT NOT NULL DEFAULT ''",
        "event_log_path": "TEXT NOT NULL DEFAULT ''",
        "evidence_version": "INTEGER NOT NULL DEFAULT 0",
        "owner_token": "TEXT NOT NULL DEFAULT ''",
        "terminal_event": "TEXT",
        "terminal_event_id": "TEXT",
        "terminal_claimed_at": "TEXT",
        "terminal_published_at": "TEXT",
        # Where the final message went, for a routine that ``deliver:``s (warren#385).
        "delivery": "TEXT",
        "delivery_reason": "TEXT NOT NULL DEFAULT ''",
    },
}

#: Indexes over columns that arrived after the first schema, and so can only be created
#: once :meth:`Store._add_missing_columns` has added them. ``approvals_denials`` is what
#: keeps the repeat-deny guard (:mod:`steward.transitions.approval`) a lookup rather than a
#: table scan on every knock: the table has grown one row per ask since phase 3.
#: ``jobs_lineage`` is what :meth:`Store.lineage` walks down: without it, finding a task's
#: children is a scan of the whole board once per level of the tree.
_LATE_INDEXES = """
CREATE INDEX IF NOT EXISTS approvals_denials
    ON approvals (resident, action, decided_at);
CREATE INDEX IF NOT EXISTS requests_approval
    ON requests (approval_id, outcome);
CREATE INDEX IF NOT EXISTS jobs_lineage
    ON jobs (parent_task_id);
CREATE INDEX IF NOT EXISTS open_runs_session_credential
    ON open_runs (session_credential_sha256);
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


def _resident_claim(row: sqlite3.Row) -> ResidentClaim:
    """Read one claim row back. Column by column, like every other record here.

    :class:`~steward.claims.ResidentClaim` lives in :mod:`steward.claims` rather than beside
    the other records in this module, because the scheduler needs it and this module imports
    the scheduler. So it gets a function rather than a ``from_row`` classmethod — the reading
    is still explicit, which is what matters: a column added to the table later is a column
    this function does not pass, not a ``TypeError`` on the next read.
    """
    return ResidentClaim(
        resident_id=row["resident_id"],
        token=row["token"],
        holder=row["holder"],
        claimed_at=row["claimed_at"],
        heartbeat_at=row["heartbeat_at"],
        kind=row["kind"],
        ref=row["ref"],
        run_id=row["run_id"],
        released_at=row["released_at"],
    )


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
    final_message: str = ""
    reply_delivered_at: str | None = None

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
            final_message=row["final_message"],
            reply_delivered_at=row["reply_delivered_at"],
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
    trigger: str
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
            trigger=row["trigger"],
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
            "trigger": self.trigger,
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
class OperatorRecord:
    """One named operator credential, as the table holds it (warren#225).

    The plaintext is not here and never was: ``digest`` is all steward keeps, so this
    record can be listed, printed and logged freely. ``revoked_at`` rather than a deleted
    row, because "who could act as this fleet's operator, and until when" is a question an
    audit asks and a missing row cannot answer.
    """

    name: str
    email: str
    digest: str
    note: str
    issued_at: str
    revoked_at: str | None = None

    @property
    def live(self) -> bool:
        """Whether this credential is still one steward will accept."""
        return self.revoked_at is None

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> OperatorRecord:
        """Rebuild one operator credential from its database row."""
        return cls(
            name=row["name"],
            email=row["email"],
            digest=row["digest"],
            note=row["note"],
            issued_at=row["issued_at"],
            revoked_at=row["revoked_at"],
        )

    def to_dict(self) -> dict[str, Any]:
        """Return the JSON view. The digest is included; the plaintext does not exist."""
        return {
            "name": self.name,
            "email": self.email,
            "digest": self.digest,
            "note": self.note,
            "issued_at": self.issued_at,
            "revoked_at": self.revoked_at,
            "live": self.live,
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
    trigger: str
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
    #: Which resident's session this is. ``agent_id`` is burrow's join key and need not be
    #: unique across manifests; this is the id a delegated letter's sender resolves to.
    resident_id: str = ""
    terminal_event: str | None = None
    terminal_event_id: str | None = None
    terminal_claimed_at: str | None = None
    terminal_published_at: str | None = None
    closed_at: str | None = None
    #: What became of the final message, when the routine said where it goes: one of
    #: :data:`steward.runs.DELIVERY_STATUSES`, or ``None`` for a run that delivers nowhere.
    delivery: str | None = None
    delivery_reason: str = ""

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
            trigger=row["trigger"],
            agent_id=row["agent_id"],
            started_at=row["started_at"],
            project=row["project"],
            ref=row["ref"],
            timeout_s=row["timeout_s"],
            heartbeat_at=row["heartbeat_at"],
            event_log_path=row["event_log_path"],
            evidence_version=row["evidence_version"],
            owner_token=row["owner_token"],
            resident_id=row["resident_id"],
            terminal_event=row["terminal_event"],
            terminal_event_id=row["terminal_event_id"],
            terminal_claimed_at=row["terminal_claimed_at"],
            terminal_published_at=row["terminal_published_at"],
            closed_at=row["closed_at"],
            delivery=row["delivery"],
            delivery_reason=row["delivery_reason"] or "",
        )

    def to_dict(self) -> dict[str, Any]:
        """Return the JSON view of one open run."""
        return {
            "run_id": self.run_id,
            "kind": self.kind,
            "trigger": self.trigger,
            "agent_id": self.agent_id,
            "project": self.project,
            "ref": self.ref,
            "timeout_s": self.timeout_s,
            "started_at": self.started_at,
            "heartbeat_at": self.heartbeat_at,
            "event_log_path": self.event_log_path,
            "closed_at": self.closed_at,
            "delivery": self.delivery,
            "delivery_reason": self.delivery_reason,
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
        """Return the whole chain this task belongs to: its root and every descendant.

        Depth-first from the root, oldest sibling first, so rendering by
        :attr:`JobRecord.depth` reads as the tree it is.

        The chain is only ever *written* upwards — a row records the parent it came from —
        so the root is found by walking ``parent_task_id`` up from the named task. But the
        answer has to be the same whichever member of the chain is named, and it was not:
        walking up alone returned the root by itself, reporting "nothing was delegated" for
        work that had in fact fanned out. That is the id operators actually hold, because
        ``POST /delegate`` hands the root back, so the audit query was wrong on exactly its
        commonest input (steward #202). The walk up is therefore followed by a walk down
        over the same column.

        Both walks stop on an id already seen: a database somebody hand-edited into a loop
        is a corrupt database, not an infinite loop in a CLI. A task nobody delegated, and
        who delegated to nobody, is a chain of one — a real answer, not an error.
        """
        path = self.ancestry(task_id)
        if not path:
            return []
        root = path[0]
        chain: list[JobRecord] = []
        seen: set[str] = {root.task_id}
        stack: list[JobRecord] = [root]
        while stack:
            item = stack.pop()
            chain.append(item)
            children = [kid for kid in self._children(item.task_id) if kid.task_id not in seen]
            seen.update(kid.task_id for kid in children)
            stack.extend(reversed(children))
        return chain

    def ancestry(self, task_id: str) -> list[JobRecord]:
        """Return the path this task actually travelled: root first, ending at the task.

        The hops a piece of work has already been through, and deliberately *not*
        :meth:`lineage`. The delegation cycle guard asks which residents this task has
        already passed through, and branches delegated out of a shared parent are not on
        its path — answering that with the whole tree would refuse a manager's second
        letter to a worker its first letter had already reached (steward #202).

        A dangling parent — a row whose parent was deleted out from under it — ends the
        walk at the highest row that does exist, which is the most of the path there is
        left to tell. An empty list means the board has never heard of ``task_id``.
        """
        path: list[JobRecord] = []
        seen: set[str] = set()
        cursor: str | None = task_id
        while cursor is not None and cursor not in seen:
            seen.add(cursor)
            record = self.job(cursor)
            if record is None:
                break
            path.append(record)
            cursor = record.parent_task_id
        return list(reversed(path))

    def _children(self, task_id: str) -> list[JobRecord]:
        """Return the tasks delegated directly out of this one, oldest first.

        ``rowid`` breaks the tie rather than ``task_id``: two letters written in the same
        second share a ``created_at``, and ordering those by a random uuid would shuffle
        siblings around between one run of the audit query and the next.
        """
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM jobs WHERE parent_task_id = ? ORDER BY created_at, rowid",
                (task_id,),
            ).fetchall()
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
        final_message: str = "",
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
                "final_message = ?, "
                "finished_at = ?, lease_expires_at = NULL, run_id = NULL, owner_token = NULL "
                "WHERE task_id = ? AND status = ? AND claimant = ? "
                "AND (? IS NULL OR claimed_at = ?)",
                (
                    status,
                    outcome,
                    reason,
                    _dumps(list(artifacts)),
                    bounded_message(final_message),
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

    def claim_answered_letters(self, sender: str, now: str | None = None) -> list[JobRecord]:
        """Take terminal letters sent by one resident, marking their replies told once."""
        moment = now or utc_now_iso()
        claimed: list[JobRecord] = []
        rendered_chars = 0
        with self._lock, self._conn:
            rows = self._conn.execute(
                "SELECT * FROM jobs WHERE delegated_by = ? AND status IN (?, ?) "
                "AND reply_delivered_at IS NULL ORDER BY finished_at, rowid",
                (sender, STATUS_DONE, STATUS_FAILED),
            ).fetchall()
            for row in rows:
                rendered = render_answer(
                    title=row["title"],
                    receiver=row["assignee"] or "unknown receiver",
                    status=row["status"],
                    message=row["final_message"],
                )
                next_size = rendered_chars + (2 if claimed else 0) + len(rendered)
                if next_size > ANSWER_BATCH_MAX_CHARS:
                    break
                cursor = self._conn.execute(
                    "UPDATE jobs SET reply_delivered_at = ? "
                    "WHERE task_id = ? AND reply_delivered_at IS NULL",
                    (moment, row["task_id"]),
                )
                if cursor.rowcount == 1:
                    fresh = self._conn.execute(
                        "SELECT * FROM jobs WHERE task_id = ?", (row["task_id"],)
                    ).fetchone()
                    claimed.append(JobRecord.from_row(fresh))
                    rendered_chars = next_size
        return claimed

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
        final_message: str = "",
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
                    "final_message = ?, "
                    "finished_at = ?, lease_expires_at = NULL, run_id = NULL, owner_token = NULL "
                    "WHERE task_id = ? AND status = ? AND claimant = ? "
                    "AND (? IS NULL OR claimed_at = ?) AND run_id = ? AND owner_token = ?",
                    (
                        status,
                        outcome,
                        reason,
                        _dumps(list(artifacts)),
                        bounded_message(final_message),
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
                self._conn.execute(
                    "INSERT OR IGNORE INTO approval_announcements (request_id) VALUES (?)",
                    (request_id,),
                )
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

    def decide(  # noqa: PLR0913 — optional API ledger metadata joins the same transaction
        self,
        request_id: str,
        decision: str,
        *,
        decided_by: str = "api",
        edit: Mapping[str, Any] | None = None,
        now: str | None = None,
        request_log: tuple[str, str, str] | None = None,
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
            if recorded:
                self._conn.execute(
                    "INSERT OR IGNORE INTO approval_announcements (request_id) VALUES (?)",
                    (request_id,),
                )
            row = self._conn.execute(
                "SELECT * FROM approvals WHERE request_id = ?", (request_id,)
            ).fetchone()
            if request_log is not None and row["status"] == STATUS_RESOLVED:
                log_id, method, path = request_log
                self._conn.execute(
                    "INSERT OR IGNORE INTO requests (request_id, received_at, method, path, "
                    "outcome, detail, approval_id) VALUES (?, ?, ?, ?, "
                    "CASE WHEN EXISTS (SELECT 1 FROM approval_announcements "
                    "WHERE request_id = ? AND effects_at IS NOT NULL) THEN 'recorded' "
                    "ELSE 'recorded_announcement_pending' END, ?, ?)",
                    (
                        log_id,
                        moment,
                        method,
                        path,
                        request_id,
                        _dumps({"approval": request_id, "decision": row["decision"]}),
                        request_id,
                    ),
                )
        return ApprovalRecord.from_row(row), recorded

    def claim_approval_announcement(
        self, request_id: str | None = None, *, lease_s: float = 30
    ) -> tuple[ApprovalRecord, str] | None:
        """Lease one unresolved approval announcement for emission.

        The decision and queue row are committed atomically.  The lease prevents two
        processes from announcing concurrently, while its deadline makes a process death
        recoverable by the next API start or replay.
        """
        token = new_id()
        now = datetime.now(UTC)
        moment = utc_now_iso(now)
        until = utc_now_iso(now + timedelta(seconds=lease_s))
        with self._lock, self._conn:
            params: tuple[str, ...] = (token, until, moment, moment)
            if request_id is not None:
                params += (request_id,)
                requested = "AND request_id = ?"
            else:
                requested = ""
            claim_sql = (
                "UPDATE approval_announcements SET claimed_by = ?, claimed_until = ? "  # noqa: S608
                "WHERE request_id = (SELECT request_id FROM approval_announcements "
                "WHERE announced_at IS NULL AND (next_attempt_at IS NULL OR next_attempt_at <= ?) "
                "AND (claimed_until IS NULL OR claimed_until <= ?) "
                + requested
                + " ORDER BY rowid LIMIT 1) AND announced_at IS NULL RETURNING request_id"
            )
            claimed = self._conn.execute(claim_sql, params).fetchone()
            if claimed is None:
                return None
            row = self._conn.execute(
                "SELECT * FROM approvals WHERE request_id = ?", (claimed["request_id"],)
            ).fetchone()
        return ApprovalRecord.from_row(row), token

    def finish_approval_announcement(self, request_id: str, token: str, *, accepted: bool) -> bool:
        """Acknowledge a claimed announcement, or release it for immediate retry."""
        with self._lock, self._conn:
            if accepted:
                cursor = self._conn.execute(
                    "UPDATE approval_announcements SET announced_at = ?, claimed_by = NULL, "
                    "claimed_until = NULL WHERE request_id = ? AND claimed_by = ? "
                    "AND announced_at IS NULL",
                    (utc_now_iso(), request_id, token),
                )
            else:
                attempts = (
                    self._conn.execute(
                        "SELECT attempts FROM approval_announcements WHERE request_id = ?",
                        (request_id,),
                    ).fetchone()["attempts"]
                    + 1
                )
                delay = min(30.0, 0.1 * (2 ** min(attempts - 1, 8)))
                cursor = self._conn.execute(
                    "UPDATE approval_announcements SET claimed_by = NULL, claimed_until = NULL, "
                    "attempts = ?, next_attempt_at = ? "
                    "WHERE request_id = ? AND claimed_by = ? AND announced_at IS NULL",
                    (
                        attempts,
                        utc_now_iso(datetime.now(UTC) + timedelta(seconds=delay)),
                        request_id,
                        token,
                    ),
                )
        return cursor.rowcount == 1

    def next_approval_announcement_at(self) -> str | None:
        """Earliest retry or live lease deadline; ``None`` means no pending work."""
        with self._lock:
            row = self._conn.execute(
                "SELECT MIN(CASE WHEN claimed_until IS NOT NULL THEN claimed_until "
                "WHEN next_attempt_at IS NOT NULL THEN next_attempt_at ELSE ? END) AS due "
                "FROM approval_announcements WHERE announced_at IS NULL",
                (utc_now_iso(),),
            ).fetchone()
        return row["due"]

    def approval_announcement_state(self, request_id: str) -> str | None:
        """Return ``pending``, ``announced`` or ``complete`` for a queued decision."""
        with self._lock:
            row = self._conn.execute(
                "SELECT announced_at, effects_at FROM approval_announcements WHERE request_id = ?",
                (request_id,),
            ).fetchone()
        if row is None:
            return None
        if row["announced_at"] is None:
            return "pending"
        return "complete" if row["effects_at"] is not None else "announced"

    def claim_approval_effects(
        self, request_id: str | None = None, *, lease_s: float = 30
    ) -> tuple[ApprovalRecord, str] | None:
        """Lease one acknowledged decision's completion effects across processes."""
        token = new_id()
        now = datetime.now(UTC)
        moment = utc_now_iso(now)
        until = utc_now_iso(now + timedelta(seconds=lease_s))
        with self._lock, self._conn:
            requested = "AND request_id = ?" if request_id is not None else ""
            params: tuple[str, ...] = (token, until, moment, moment)
            if request_id is not None:
                params += (request_id,)
            claim_sql = (
                "UPDATE approval_announcements SET effects_claimed_by = ?, "  # noqa: S608
                "effects_claimed_until = ? WHERE request_id = (SELECT request_id FROM "
                "approval_announcements WHERE announced_at IS NOT NULL AND effects_at IS NULL "
                "AND (effects_next_attempt_at IS NULL OR effects_next_attempt_at <= ?) "
                "AND (effects_claimed_until IS NULL OR effects_claimed_until <= ?) "
                + requested
                + " ORDER BY rowid LIMIT 1) AND effects_at IS NULL RETURNING request_id"
            )
            claimed = self._conn.execute(claim_sql, params).fetchone()
            if claimed is None:
                return None
            row = self._conn.execute(
                "SELECT * FROM approvals WHERE request_id = ?", (claimed["request_id"],)
            ).fetchone()
        return ApprovalRecord.from_row(row), token

    def complete_approval_effects(
        self, record: ApprovalRecord, token: str
    ) -> tuple[bool, str | None]:
        """Apply completion and its marker in one transaction guarded by the effects lease.

        Budget approval atomically removes the pause and grants its window allowance.
        All other decisions have no completion mutation, but still atomically acquire the
        durable marker. A stale worker cannot complete a lease another process recovered.
        """
        moment = utc_now_iso()
        with self._lock, self._conn:
            owned = self._conn.execute(
                "SELECT 1 FROM approval_announcements WHERE request_id = ? "
                "AND announced_at IS NOT NULL AND effects_at IS NULL AND effects_claimed_by = ?",
                (record.request_id, token),
            ).fetchone()
            if owned is None:
                return False, None
            resumed: str | None = None
            if record.action == "budget_unpause" and record.decision == "approve":
                pause = self._conn.execute(
                    "SELECT * FROM budget_pauses WHERE request_id = ?", (record.request_id,)
                ).fetchone()
                if pause is not None:
                    resumed = pause["resident"]
                    if pause["window_end"]:
                        self._conn.execute(
                            "INSERT INTO budget_allowances "
                            "(resident, until, granted_by, reason, granted_at) "
                            "VALUES (?, ?, ?, ?, ?) "
                            "ON CONFLICT(resident) DO UPDATE SET until=excluded.until, "
                            "granted_by=excluded.granted_by, reason=excluded.reason, "
                            "granted_at=excluded.granted_at",
                            (
                                resumed,
                                pause["window_end"],
                                record.decided_by or "api",
                                pause["reason"],
                                moment,
                            ),
                        )
                    self._conn.execute(
                        "DELETE FROM budget_pauses WHERE resident = ? AND request_id = ?",
                        (resumed, record.request_id),
                    )
            cursor = self._conn.execute(
                "UPDATE approval_announcements SET effects_at = ?, effects_claimed_by = NULL, "
                "effects_claimed_until = NULL WHERE request_id = ? AND effects_at IS NULL "
                "AND effects_claimed_by = ?",
                (moment, record.request_id, token),
            )
            if cursor.rowcount == 1:
                self._conn.execute(
                    "UPDATE requests SET outcome = 'recorded' WHERE approval_id = ? "
                    "AND outcome = 'recorded_announcement_pending'",
                    (record.request_id,),
                )
        return cursor.rowcount == 1, resumed

    def release_approval_effects(self, request_id: str, token: str) -> bool:
        """Release a failed effects claim with bounded exponential backoff."""
        with self._lock, self._conn:
            row = self._conn.execute(
                "SELECT effects_attempts FROM approval_announcements WHERE request_id = ? "
                "AND effects_claimed_by = ? AND effects_at IS NULL",
                (request_id, token),
            ).fetchone()
            if row is None:
                return False
            attempts = row["effects_attempts"] + 1
            delay = min(30.0, 0.1 * (2 ** min(attempts - 1, 8)))
            cursor = self._conn.execute(
                "UPDATE approval_announcements SET effects_claimed_by=NULL, "
                "effects_claimed_until=NULL, effects_attempts=?, effects_next_attempt_at=? "
                "WHERE request_id=? AND effects_claimed_by=? AND effects_at IS NULL",
                (
                    attempts,
                    utc_now_iso(datetime.now(UTC) + timedelta(seconds=delay)),
                    request_id,
                    token,
                ),
            )
        return cursor.rowcount == 1

    def next_approval_work_at(self) -> str | None:
        """Earliest announcement/effects retry or live-lease deadline."""
        with self._lock:
            row = self._conn.execute(
                "SELECT MIN(due) AS due FROM ("
                "SELECT CASE WHEN claimed_until IS NOT NULL THEN claimed_until "
                "WHEN next_attempt_at IS NOT NULL THEN next_attempt_at ELSE ? END AS due "
                "FROM approval_announcements WHERE announced_at IS NULL UNION ALL "
                "SELECT CASE WHEN effects_claimed_until IS NOT NULL THEN effects_claimed_until "
                "WHEN effects_next_attempt_at IS NOT NULL THEN effects_next_attempt_at ELSE ? END "
                "FROM approval_announcements WHERE announced_at IS NOT NULL "
                "AND effects_at IS NULL)",
                (utc_now_iso(), utc_now_iso()),
            ).fetchone()
        return row["due"]

    # -- the run ledger ----------------------------------------------------------------

    def record_run(  # noqa: PLR0913 — one keyword per column of the entry
        self,
        *,
        resident: str,
        agent_id: str,
        kind: str,
        run_id: str,
        trigger: str = "",
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
        validate_kind_trigger(kind, trigger)
        entry = LedgerEntry(
            entry_id=new_id(),
            resident=resident,
            agent_id=agent_id,
            kind=kind,
            trigger=trigger,
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
                "INSERT INTO run_ledger (entry_id, resident, agent_id, kind, trigger, run_id, ref, "
                "origin, outcome, input_tokens, output_tokens, cost_usd, duration_s, "
                "usage_known, recorded_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    entry.entry_id,
                    entry.resident,
                    entry.agent_id,
                    entry.kind,
                    entry.trigger,
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
        trigger: str = "",
        project: str = "",
        ref: str = "",
        timeout_s: float = 0.0,
        event_log_path: str = "",
        owner_token: str = "",
        resident_id: str = "",
        session_credential: str = "",
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

        ``session_credential`` is the plaintext, and only its digest is written: a copy of
        this database must not yield live credentials. Hashing here rather than in the
        caller means the mint and the check share one definition of what is hashed.
        """
        validate_kind_trigger(kind, trigger)
        with self._lock, self._conn:
            opened_at = now or utc_now_iso()
            cursor = self._conn.execute(
                "INSERT INTO open_runs (run_id, kind, trigger, agent_id, project, ref, timeout_s, "
                "started_at, heartbeat_at, event_log_path, evidence_version, owner_token, "
                "resident_id, session_credential_sha256) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?) "
                "ON CONFLICT(run_id) DO NOTHING",
                (
                    run_id,
                    kind,
                    trigger,
                    agent_id,
                    project,
                    ref,
                    float(timeout_s),
                    opened_at,
                    opened_at,
                    event_log_path,
                    owner_token,
                    resident_id,
                    credential_digest(session_credential),
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
        resident_id: str = "",
        session_credential: str = "",
        now: str | None = None,
    ) -> bool:
        """Open and bind a task attempt in one transaction.

        The claim stamp fences the binding.  A swept/retried job cannot accidentally be
        attached to the late predecessor, and an inserted run is rolled back when the
        binding loses that race.

        ``session_credential`` is stored as a digest, exactly as in :meth:`open_run`, and
        rolled back with the row when the binding loses that race — a credential for a run
        that never opened would authenticate a session nobody is watching.
        """
        moment = now or utc_now_iso()
        try:
            with self._lock, self._conn:
                opened = self._conn.execute(
                    "INSERT INTO open_runs (run_id, kind, agent_id, project, ref, timeout_s, "
                    "started_at, heartbeat_at, event_log_path, evidence_version, owner_token, "
                    "resident_id, session_credential_sha256) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?) "
                    "ON CONFLICT(run_id) DO NOTHING",
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
                        resident_id,
                        credential_digest(session_credential),
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

    # -- the resident claim ----------------------------------------------------------------

    def claim_resident(  # noqa: PLR0913 — one parameter per fact the claim records
        self,
        resident_id: str,
        *,
        token: str,
        holder: str = "",
        kind: str = "",
        ref: str = "",
        run_id: str = "",
        stale_before: str,
        now: str | None = None,
    ) -> ResidentClaim | None:
        """Take this resident's one live-session claim, or return ``None``.

        What a claim *means* is :mod:`steward.claims`; this is the write that makes it true.
        One statement, so the check and the take cannot be separated by another process.
        The upsert's ``WHERE`` is the whole guard: a claim may be taken only when the last
        one was given back, or when its holder stopped saying it was alive before
        ``stale_before``. A live claim leaves the row exactly as it is — ``rowcount`` is 0,
        nothing is overwritten, and the caller reads back who holds it.

        The row is updated rather than replaced so the resident's ``PRIMARY KEY`` is what
        makes the claim exclusive; there is no window where two rows exist for one resident.
        """
        moment = now or utc_now_iso()
        with self._lock, self._conn:
            cursor = self._conn.execute(
                "INSERT INTO resident_claims (resident_id, token, holder, kind, ref, run_id, "
                "claimed_at, heartbeat_at, released_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL) "
                "ON CONFLICT(resident_id) DO UPDATE SET token = excluded.token, "
                "holder = excluded.holder, kind = excluded.kind, ref = excluded.ref, "
                "run_id = excluded.run_id, claimed_at = excluded.claimed_at, "
                "heartbeat_at = excluded.heartbeat_at, released_at = NULL "
                "WHERE resident_claims.released_at IS NOT NULL "
                "OR resident_claims.heartbeat_at <= ?",
                (resident_id, token, holder, kind, ref, run_id, moment, moment, stale_before),
            )
            if cursor.rowcount != 1:
                return None
            row = self._conn.execute(
                "SELECT * FROM resident_claims WHERE resident_id = ?", (resident_id,)
            ).fetchone()
        return _resident_claim(row)

    def renew_resident_claim(self, resident_id: str, *, token: str, now: str | None = None) -> bool:
        """Stamp a claim's heartbeat. ``False`` means this token no longer holds it.

        Fenced on the token, like every other write here: a holder that was declared dead
        and reclaimed must not be able to keep the *new* holder's claim alive under its own
        name, which is precisely what an unfenced ``UPDATE … WHERE resident_id`` would do.
        """
        with self._lock, self._conn:
            cursor = self._conn.execute(
                "UPDATE resident_claims SET heartbeat_at = ? WHERE resident_id = ? "
                "AND token = ? AND released_at IS NULL",
                (now or utc_now_iso(), resident_id, token),
            )
            return cursor.rowcount == 1

    def release_resident_claim(
        self, resident_id: str, *, token: str, now: str | None = None
    ) -> bool:
        """Give a claim back. ``False`` means somebody else already holds this resident."""
        with self._lock, self._conn:
            cursor = self._conn.execute(
                "UPDATE resident_claims SET released_at = ? WHERE resident_id = ? "
                "AND token = ? AND released_at IS NULL",
                (now or utc_now_iso(), resident_id, token),
            )
            return cursor.rowcount == 1

    def resident_claim(self, resident_id: str) -> ResidentClaim | None:
        """Return the last claim recorded for a resident, live or not.

        Whether it still *holds* is :meth:`steward.claims.ResidentClaim.live_at`'s question,
        and it needs a cutoff this layer has no business inventing. A released or stale row
        is kept and handed back because it is the answer to "what ran here last".
        """
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM resident_claims WHERE resident_id = ?", (resident_id,)
            ).fetchone()
        return _resident_claim(row) if row is not None else None

    def session_principal(self, credential: str, *, fresh_since: str) -> SessionPrincipal | None:
        """Return who a session credential is, or ``None`` if it is not a live one.

        The condition is the exact negation of the watchdog's burial condition — the run is
        open, no terminal fact has been chosen for it, and its heartbeat is *not* stale by
        ``fresh_since``, which is what :meth:`close_stale_run` and :meth:`claim_run_terminal`
        require in order to bury it. So a credential is accepted exactly while the watchdog
        could not yet call this run dead, and it stops the moment the session closes, times
        out, or the lease goes stale — whether or not a watchdog has actually swept.

        Not :meth:`renew_run`'s condition, which is a near miss worth naming: that one has no
        freshness clause at all (an owner whose heartbeat thread was starved for three
        minutes may still renew and carry on) and it does check ``owner_token``. The owner
        token fences steward's own writes about a run; it is not the session's to present,
        and a session that had it could renew its own credential. Burial is the right clock
        here because it is the one that answers "is anybody still entitled to act as this
        session" — and it is deny-by-default, which is the house rule (steward #41).

        Looked up by digest, so the plaintext is never compared against anything on disk.
        An empty credential is refused before the query: every row that never got one
        stores the empty digest, and matching those would make "no credential" a master
        key.
        """
        digest = credential_digest(credential)
        if not digest:
            return None
        with self._lock:
            row = self._conn.execute(
                "SELECT run_id, resident_id FROM open_runs "
                "WHERE session_credential_sha256 = ? AND closed_at IS NULL "
                "AND terminal_event IS NULL AND heartbeat_at > ?",
                (digest, fresh_since),
            ).fetchone()
        if row is None:
            return None
        return SessionPrincipal(run_id=row["run_id"], resident_id=row["resident_id"])

    # -- operator credentials (warren#225) ---------------------------------------------

    def mint_operator(
        self,
        *,
        name: str,
        email: str,
        credential: str,
        note: str = "",
        now: str | None = None,
    ) -> OperatorRecord:
        """Record a named operator credential. Raises :class:`ValueError` for a live name.

        Only the digest is stored — the caller has just generated the plaintext and is
        about to print it once, and this method is deliberately unable to hand it back.

        A name is refused rather than silently rotated. Re-minting in place would leave the
        person holding the old credential with no way to tell it had stopped working, and
        "revoke, then mint" says the same thing in two steps that each leave a stamp.
        """
        moment = now or utc_now_iso()
        record = OperatorRecord(
            name=name,
            email=email,
            digest=credential_digest(credential),
            note=note,
            issued_at=moment,
        )
        if not record.digest:
            raise ValueError("an operator credential cannot be empty")
        with self._lock, self._conn:
            existing = self._conn.execute(
                "SELECT revoked_at FROM operator_credentials WHERE name = ?", (name,)
            ).fetchone()
            if existing is not None and existing["revoked_at"] is None:
                raise ValueError(f"operator {name!r} already holds a live credential")
            # A revoked name may be minted again: the stamp on the old row is the audit
            # trail, and REPLACE keeps the primary key honest without a second table.
            self._conn.execute(
                "INSERT OR REPLACE INTO operator_credentials "
                "(name, email, digest, note, issued_at, revoked_at) VALUES (?, ?, ?, ?, ?, NULL)",
                (record.name, record.email, record.digest, record.note, record.issued_at),
            )
        return record

    def revoke_operator(self, name: str, *, now: str | None = None) -> OperatorRecord | None:
        """Stamp an operator's credential dead. ``None`` when there was no live one.

        Conditional on ``revoked_at IS NULL``, so the answer is about *this* call and a
        second revocation does not move the moment the first one recorded.
        """
        moment = now or utc_now_iso()
        with self._lock, self._conn:
            cursor = self._conn.execute(
                "UPDATE operator_credentials SET revoked_at = ? "
                "WHERE name = ? AND revoked_at IS NULL",
                (moment, name),
            )
            if cursor.rowcount != 1:
                return None
            row = self._conn.execute(
                "SELECT * FROM operator_credentials WHERE name = ?", (name,)
            ).fetchone()
        return OperatorRecord.from_row(row)

    def operators(self, *, live_only: bool = False) -> list[OperatorRecord]:
        """List operator credentials, oldest first. Revoked ones are included by default."""
        clause = " WHERE revoked_at IS NULL" if live_only else ""
        with self._lock:
            rows = self._conn.execute(
                f"SELECT * FROM operator_credentials{clause} ORDER BY issued_at, name"  # noqa: S608
            ).fetchall()
        return [OperatorRecord.from_row(row) for row in rows]

    def operator_principal(self, credential: str) -> OperatorPrincipal | None:
        """Return who an operator credential is, or ``None`` if it is not a live one.

        Looked up by digest, so the plaintext is never compared against anything on disk,
        and gated on ``revoked_at IS NULL``, which is the whole of what revocation means:
        there is no cache, no session, and nothing to expire. An empty credential is
        refused before the query — a row can never store the empty digest, but a query
        that would match one is a query worth not writing.
        """
        digest = credential_digest(credential)
        if not digest:
            return None
        with self._lock:
            row = self._conn.execute(
                "SELECT name, email FROM operator_credentials "
                "WHERE digest = ? AND revoked_at IS NULL",
                (digest,),
            ).fetchone()
        if row is None:
            return None
        return OperatorPrincipal(name=row["name"], email=row["email"])

    # -- what actually ran (warren#104) ------------------------------------------------

    def latest_routine_runs(self) -> dict[str, LedgerEntry]:
        """Return the newest recorded run of each routine, keyed ``<resident>/<routine>``.

        The answer to warren#104. The routine ledger used to report ``last_request`` and
        nothing else, which is the *API request log*: a run somebody asked for over HTTP
        leaves a row there, and a run the scheduler fired on its own schedule does not.
        An operator reading that panel concluded a healthy resident "only runs when I
        trigger it manually", which was false.

        This reads the run ledger instead, which every finished session writes to whatever
        started it, so the answer carries the trigger (``schedule`` or ``manual``) and the
        outcome. The two facts stay side by side rather than one replacing the other: they
        are different questions, and a request that was accepted and never ran is exactly
        the case where the difference matters.

        The bare-column-with-``MAX`` form is SQLite's documented idiom for "the whole row
        at the maximum", and ``recorded_at`` is a sortable protocol timestamp, so this is
        one indexed pass rather than a full ledger read and a fold in Python.
        """
        with self._lock:
            rows = self._conn.execute(
                "SELECT *, MAX(recorded_at) FROM run_ledger "
                "WHERE kind = ? AND ref <> '' GROUP BY resident, ref",
                (RUN_ROUTINE,),
            ).fetchall()
        return {f"{row['resident']}/{row['ref']}": LedgerEntry.from_row(row) for row in rows}

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

    def record_delivery(self, run_id: str, status: DeliveryStatus, reason: str = "") -> bool:
        """Write down what became of a run's final message. Returns whether a row took it.

        Unconditional on ``closed_at``: delivery happens after the terminal transition, so
        the row it lands on is normally already closed, and that is the row that should say
        where the message went. The membership check stays although the type is a
        ``Literal``: this is the database boundary, and a row must never hold a word the
        API would not know how to read back.
        """
        if status not in DELIVERY_STATUSES:
            raise ValueError(f"invalid delivery status: {status!r}")
        with self._lock, self._conn:
            cursor = self._conn.execute(
                "UPDATE open_runs SET delivery = ?, delivery_reason = ? WHERE run_id = ?",
                (status, reason, run_id),
            )
            return cursor.rowcount == 1

    def run_record(self, run_id: str) -> OpenRun | None:
        """Return one run's row, open or closed, or ``None`` when steward never opened it.

        The read seam for a run *by id* — what a test, and a future ``GET /runs/{id}``,
        asks; :meth:`open_runs` and :meth:`terminal_runs` answer the watchdog's questions.
        """
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM open_runs WHERE run_id = ?", (run_id,)
            ).fetchone()
        return OpenRun.from_row(row) if row is not None else None

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
