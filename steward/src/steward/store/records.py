"""The durable facts the store records and hands back."""

import json
import sqlite3
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from steward.claims import ResidentClaim
from steward.state_paths import default_state_path

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


#: Every status a task on the board can be in. The board reports these and no others.
JOB_STATUSES = (STATUS_OPEN, STATUS_CLAIMED, STATUS_DONE, STATUS_FAILED)

#: Where spend lands when no task — and so no delegation origin — stands behind the run.
#: A resident's own routines are the ordinary case, and they are named rather than
#: dropped: money steward cannot attribute is still money somebody spent.
ORIGIN_UNATTRIBUTED = "unattributed"

DB_FILENAME = "steward.db"


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
    #: When this decision was spent on the write it authorised, and by which request
    #: (warren#437). ``None`` is the ordinary case: most approvals authorise an action a
    #: resident performs itself, and steward never hears about it. A non-null value is the
    #: record that steward's own write door opened once against this decision and will not
    #: open against it again.
    consumed_at: str | None = None
    consumed_by: str = ""

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
            consumed_at=row["consumed_at"],
            consumed_by=row["consumed_by"] or "",
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
            "consumed_at": self.consumed_at,
            "consumed_by": self.consumed_by or None,
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
