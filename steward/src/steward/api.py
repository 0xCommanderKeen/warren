"""The steward HTTP API: the one write path into the fleet.

Everything a human can *do* to the fleet from burrow's viewer arrives here — run a
routine now, post a job, answer an approval, declare a resident. Burrow's server never
gets write access to agents; the browser calls steward directly, and burrow stays the
pure reader it claims to be.

**The contract is acknowledgement, not effect.** An accepted request returns a
``request_id`` and a word like *accepted*, *queued*, or *recorded* — never *done* or
*ran*. The effect is confirmed only when the corresponding protocol event lands in
burrow's log (``routine_started``, ``task_posted``, ``needs_human_resolved``), and the
UI treats that log as the sole source of truth. This is what makes it impossible for
the API to make the village claim something that never happened.

**Refusals are immediate and specific.** An unknown resident, an unknown routine, a
disabled routine, a run already in flight: each fails at once with its own reason
rather than queueing into silence. Nothing is written for a request that was refused.

**Posture.** Every endpoint — including the read-only views — requires
``Authorization: Bearer $STEWARD_TOKEN``, compared in constant time. An unset or
blank token means the server refuses to start unless ``--allow-open`` says out loud
that this is local development. The default bind is ``127.0.0.1``; in deployment
steward listens on the tailnet interface and is never exposed to the public internet.

**Every path here is authenticated, with no exceptions.** This server used to mount a
management console at ``/ui`` and serve its three static files unauthenticated, because a
browser has to load a script before there is anything to ask a human for a token with.
That console has been retired (warren#225): townhall is the fleet's one governance UI, it
is served from its own origin, and it reaches these routes through the shared nginx with an
operator credential. steward is a pure API again, and no byte it serves is unauthenticated.
"""

import asyncio
import logging
import os
import threading
from collections.abc import AsyncIterator, Callable, Mapping, Sequence
from concurrent.futures import Future, ThreadPoolExecutor
from concurrent.futures import wait as wait_for_futures
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from hmac import compare_digest
from pathlib import Path
from typing import Annotated, Any, Literal, NoReturn

import yaml
from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.security import HTTPBearer
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from steward import authoring as au
from steward import delegation as dg
from steward import events as ev
from steward import manifest as m
from steward.approvals import WithheldValueError, redact_decision, restore_withheld
from steward.board import Dispatcher
from steward.budgets import PAUSED_ERROR, BudgetGuard, BudgetStatus
from steward.claims import ONE_SESSION_PER_RESIDENT, ResidentClaims
from steward.deploy import Transport, TransportError
from steward.input_bounds import (
    APPROVAL_BODY_MAX_BYTES,
    DETAIL_MAX_CHARS,
    EDIT_MAX_DEPTH,
    IDENTIFIER_MAX_CHARS,
    SKILLS_MAX_ITEMS,
    TITLE_MAX_CHARS,
    validate_approval_edit,
    validate_json_container_depth,
)
from steward.journal import journal_complaint, read_entries
from steward.manifest import Resident, ValidationResult, retired_complaint, validate_path
from steward.nursery import (
    CommitIdentity,
    NewResident,
    NurseryError,
    NurseryReport,
    provision_resident,
    raise_resident,
)
from steward.operator_auth import OperatorPrincipal, looks_like_operator_credential
from steward.run_lifecycle import RUN_LEASE_GRACE_S
from steward.runners import build_runner
from steward.scheduler import (
    TRIGGER_MANUAL,
    FireReport,
    ScheduledRoutine,
    Scheduler,
    SchedulerState,
    default_state_path,
    scheduler_liveness,
)
from steward.session_auth import (
    SESSION_TOKEN_ENV,
    SessionPrincipal,
    looks_like_session_credential,
)
from steward.sessions import RunnerFactory
from steward.skills import SkillLibrary, effective_skills, library_for
from steward.store import (
    JOB_STATUSES,
    STATUS_OPEN,
    ApprovalRecord,
    LedgerEntry,
    RequestRecord,
    Store,
    default_db_path,
    new_id,
)
from steward.transitions.approval import ApprovalOutboxWorker, ApprovalTransitions
from steward.transitions.task import TaskTransitions

__all__ = [
    "DEFAULT_HOST",
    "DEFAULT_PORT",
    "ApiConfig",
    "ApiError",
    "ManualRuns",
    "NurseryPipeline",
    "ProvisionPipeline",
    "ResidentPost",
    "create_app",
    "run_server",
]

#: How the API reaches the nursery. Injectable so a test can prove the endpoint and
#: ``steward new-resident`` run *the same* pipeline rather than two that agree by
#: convention — hand it a recorder and assert on what the route asked for.
type NurseryPipeline = Callable[..., NurseryReport]

#: How the API reaches the *other* nursery door — provision from a declared manifest.
#: Injectable for the reason :data:`NurseryPipeline` is: a test proves the route and
#: ``steward provision`` run one pipeline rather than two that happen to agree.
type ProvisionPipeline = Callable[..., NurseryReport]

log = logging.getLogger("steward.api")

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8801

#: ``serve`` keeps approval deadlines real even when no scheduler, dispatcher, or
#: watchdog is resident. Kept here rather than in the transition: cadence is process
#: lifecycle policy, while ``ApprovalTransitions.expire`` remains the atomic domain act.
APPROVAL_EXPIRY_INTERVAL_S = 30.0

TOKEN_ENV = "STEWARD_TOKEN"  # noqa: S105 — an env var name, not a credential
CORS_ENV = "STEWARD_CORS_ORIGINS"
RESIDENTS_ENV = "STEWARD_RESIDENTS"
COMMIT_IDENTITY_ENV = "STEWARD_COMMIT_IDENTITY"
ALLOW_UNCOMMITTED_ENV = "STEWARD_ALLOW_UNCOMMITTED_WRITES"

#: How the write API describes the caller in a commit. Not a name, because there is not one
#: to know: the human token is a shared secret, so what steward can say truthfully is which
#: door the change came through. The request id in the same commit is what makes it
#: traceable to a moment and a path in the request log.
API_PRINCIPAL = "a holder of STEWARD_TOKEN, over the steward API"

#: How a refused write is answered. A manifest that does not validate is the caller's
#: mistake and unprocessable; a residents tree with no git behind it is the *server's*
#: configuration and not something the caller can fix by sending different bytes.
WRITE_STATUS: Mapping[str, int] = {
    "manifest_invalid": 422,
    "skill_invalid": 422,
    "unknown_skill": 404,
    "unknown_resident": 404,
    "resident_invalid": 409,
    "soul_file_changed": 409,
    "skill_exists": 409,
    "not_a_git_checkout": 409,
    "commit_failed": 409,
}

#: How many rows ``GET /requests`` will hand back at most, and how many it hands back
#: when nobody asks. A control panel polls this to confirm what a 202 only accepted.
REQUESTS_DEFAULT_LIMIT = 50
REQUESTS_MAX_LIMIT = 500

#: What steward records as the actor when the caller has no name: the master token, which
#: is a shared secret, or open mode, where there is no credential at all. A *named*
#: operator (warren#225) replaces this with their own name — see ``acted_by`` — because a
#: board and an approval ledger whose every row says ``api`` cannot answer "who did this".
ACTED_BY_API = "api"
POSTED_BY = ACTED_BY_API
DECIDED_BY = ACTED_BY_API

#: What ``GET /approvals?status=`` accepts. ``pending`` is the default, so a panel that
#: never passed the parameter sees exactly what it always saw.
APPROVAL_STATUS_PENDING = "pending"
APPROVAL_STATUS_ALL = "all"
APPROVAL_STATUSES = (APPROVAL_STATUS_PENDING, "resolved", APPROVAL_STATUS_ALL)

NO_TOKEN_MESSAGE = (
    f"{TOKEN_ENV} is unset or blank, and every endpoint of this API is a write path "
    f"into the fleet. Set {TOKEN_ENV} to a secret shared with burrow's viewer, or pass "
    f"--allow-open to say out loud that this is local development on a loopback bind."
)


class ApiError(Exception):
    """Raised when the API cannot be configured — before it ever binds a port."""


# --------------------------------------------------------------------------------------
# configuration
# --------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ApiConfig:
    """Everything the API needs to know that is not a collaborator."""

    residents_dir: Path = Path("residents")
    db_path: Path | None = None
    token: str | None = None
    allow_open: bool = False
    cors_origins: tuple[str, ...] = ()
    workdir: Path | None = None
    #: The skills library. ``None`` finds the one beside the residents tree.
    skills_dir: Path | None = None
    #: Who commits appear to be by. ``None`` uses steward's own generic API identity,
    #: because ``STEWARD_TOKEN`` is a shared secret with no person behind it. An operator
    #: running a single-person steward can name themselves here, which is exactly as
    #: trustworthy as the token — and is configuration rather than a claim in a request.
    commit_identity: CommitIdentity | None = None
    #: Accept writes into a residents tree that is not in a git checkout. Off, because a
    #: fleet whose declarations have no history is a thing to choose out loud rather than
    #: to discover on the day somebody needs to undo something.
    allow_uncommitted_writes: bool = False
    approval_poll_interval_s: float = 1.0
    approval_close_timeout_s: float = 5.0

    @classmethod
    def from_env(  # noqa: PLR0913 — one keyword per thing the environment can name
        cls,
        env: Mapping[str, str] | None = None,
        *,
        residents_dir: Path | str | None = None,
        db_path: Path | str | None = None,
        allow_open: bool = False,
        workdir: Path | str | None = None,
        skills_dir: Path | str | None = None,
    ) -> ApiConfig:
        """Read the token, the CORS origins, and the residents tree from the environment."""
        source = os.environ if env is None else env
        configured_residents = residents_dir or (source.get(RESIDENTS_ENV) or "").strip()
        return cls(
            residents_dir=Path(configured_residents) if configured_residents else Path("residents"),
            db_path=Path(db_path) if db_path is not None else None,
            token=source.get(TOKEN_ENV),
            allow_open=allow_open,
            cors_origins=parse_origins(source.get(CORS_ENV)),
            workdir=Path(workdir) if workdir is not None else None,
            skills_dir=Path(skills_dir) if skills_dir is not None else None,
            commit_identity=parse_identity(source.get(COMMIT_IDENTITY_ENV)),
            allow_uncommitted_writes=_flag(source.get(ALLOW_UNCOMMITTED_ENV)),
        )


def parse_origins(raw: str | None) -> tuple[str, ...]:
    """Split ``STEWARD_CORS_ORIGINS`` into origins. Unset means no origin is allowed."""
    return tuple(part.strip() for part in (raw or "").split(",") if part.strip())


def parse_identity(raw: str | None) -> CommitIdentity | None:
    """Read ``Name <email>`` from the environment, or ``None`` for steward's own identity.

    Deliberately forgiving in only one direction: anything that is not the standard git
    author spelling is ignored rather than half-parsed into a name that is really an
    address. A misconfigured identity should leave commits reading ``steward (api)``, which
    is true, rather than something that looks like a person and is not.
    """
    text = (raw or "").strip()
    if not text.endswith(">") or "<" not in text:
        return None
    name, _, address = text[:-1].partition("<")
    name, address = name.strip(), address.strip()
    if not name or not address:
        return None
    return CommitIdentity(name=name, email=address)


def _flag(raw: str | None) -> bool:
    """Read an environment switch. Only an explicit yes counts as one."""
    return (raw or "").strip().lower() in {"1", "true", "yes", "on"}


def latest_run_requests(records: Sequence[RequestRecord]) -> dict[str, dict[str, Any]]:
    """Index the request log by routine key, keeping the newest entry for each.

    This is how the routine ledger answers "and what became of the last run somebody
    asked for" without inventing a second ledger: the request log already records the
    outcome a manual fire came to, and the routine key is in its detail.
    """
    latest: dict[str, dict[str, Any]] = {}
    for record in records:  # oldest first, so the newest request is the last write
        key = record.detail.get("routine")
        if isinstance(key, str):
            latest[key] = record.to_dict()
    return latest


def last_run_view(entry: LedgerEntry | None) -> dict[str, Any] | None:
    """Return the small "what actually ran" block a routine row carries, or ``None``.

    Deliberately five fields rather than the whole ledger entry: a routine ledger answers
    *did this fire, how was it started, and how did it go*, and the money is
    ``GET /residents/{id}/budget``'s question. ``None`` means no run of this routine has
    ever finished — which is a real answer and not the same as one that failed.
    """
    if entry is None:
        return None
    return {
        "run_id": entry.run_id,
        "trigger": entry.trigger,
        "outcome": entry.outcome,
        "recorded_at": entry.recorded_at,
        "duration_s": round(entry.duration_s, 3),
    }


def resolve_token(token: str | None, *, allow_open: bool) -> str | None:
    """Return the token to require, or ``None`` in open mode. Raises otherwise.

    A blank or whitespace-only token counts as unset — a deployment that exported an
    empty variable has no auth at all, and the failure should be a refusal to start
    rather than an open door nobody notices.
    """
    cleaned = (token or "").strip() or None
    if cleaned is None and not allow_open:
        raise ApiError(NO_TOKEN_MESSAGE)
    return cleaned


# --------------------------------------------------------------------------------------
# request bodies
# --------------------------------------------------------------------------------------


class _Body(BaseModel):
    """Base for request bodies: unknown keys are a 422, not a silently ignored field."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class JobPost(_Body):
    """A task a human wants the fleet to pick up."""

    title: str = Field(
        min_length=1, max_length=TITLE_MAX_CHARS, description="One line naming the work."
    )
    detail: str = Field(
        default="",
        max_length=DETAIL_MAX_CHARS,
        description="Everything the claimant needs to know.",
    )
    required_skills: list[Annotated[str, Field(min_length=1, max_length=IDENTIFIER_MAX_CHARS)]] = (
        Field(
            default_factory=list,
            max_length=SKILLS_MAX_ITEMS,
            description="Skills a resident must be granted before it may claim this.",
        )
    )


class ResidentPost(NewResident):
    """A resident to declare, and whether to actually build it.

    Everything a :class:`~steward.nursery.NewResident` says, plus one flag. ``deploy``
    defaults to **false**, which keeps ``POST /residents`` exactly what it has always
    been: two files written for review, no container, no schedule, no event. Asking for
    ``deploy: true`` is asking steward to reach a machine over ssh and start something
    there, and that is not a thing a request should be able to do by leaving a field out.
    """

    deploy: bool = Field(
        default=False,
        description="Provision the container and check the schedule, not just declare.",
    )


class ProvisionPost(_Body):
    """Whether to build the declared resident, or only rehearse building it.

    There is nothing else to say: the manifest is the request. Everything ``new-resident``
    takes in flags this endpoint reads off ``residents/<id>/manifest.yaml``, which is the
    whole point of the door (warren#270).
    """

    dry_run: bool = Field(
        default=False,
        description="Print the plan and reach no host. Nothing is sent, run, or written.",
    )


#: How much manifest, soul, or skill text one request may carry. Generous next to the
#: caps validation applies to individual fields, because it is not trying to be those caps
#: — it is the bound that stops a write path from having to materialise something
#: unreasonable before the real limits get a chance to speak.
DOCUMENT_MAX_CHARS = 200_000


class DeclarationPut(_Body):
    """A resident's declaration, as a form edits it.

    ``manifest`` and ``text`` are two spellings of one thing and exactly one may be given.
    ``manifest`` is the mapping a form builds from its fields, which steward serialises —
    convenient, and it rewrites the file, so comments in it do not survive. ``text`` is the
    YAML itself, written byte for byte, which is how a caller keeps the comments a person
    wrote. Neither is more validated than the other.
    """

    manifest: dict[str, Any] | None = Field(
        default=None, description="The manifest as data. Steward serialises it to YAML."
    )
    text: str | None = Field(
        default=None,
        max_length=DOCUMENT_MAX_CHARS,
        description="The manifest as YAML, written exactly as given. Preserves comments.",
    )
    soul: str | None = Field(
        default=None,
        max_length=DOCUMENT_MAX_CHARS,
        description="The soul document. Omit it to leave the soul untouched.",
    )
    revision: str | None = Field(
        default=None,
        max_length=IDENTIFIER_MAX_CHARS,
        description="The revision this edit was made against. Omit it to overwrite blindly.",
    )

    @model_validator(mode="after")
    def _one_spelling(self) -> DeclarationPut:
        """Insist on exactly one manifest spelling, so neither can silently win."""
        if (self.manifest is None) == (self.text is None):
            raise ValueError("give exactly one of `manifest` (a mapping) or `text` (YAML)")
        return self


class SkillBody(_Body):
    """One skill, as the library stores it and a form edits it.

    ``defaults`` is the field to look at twice: it is not a property of this skill so much
    as a grant to the entire fleet, since a default skill is held by every resident without
    any manifest saying so.
    """

    description: str = Field(
        min_length=1,
        max_length=DOCUMENT_MAX_CHARS,
        description="One line saying what this skill is for.",
    )
    body: str = Field(
        min_length=1, max_length=DOCUMENT_MAX_CHARS, description="The instructions themselves."
    )
    defaults: bool = Field(
        default=False, description="Give this skill to every resident, granted or not."
    )
    revision: str | None = Field(
        default=None,
        max_length=IDENTIFIER_MAX_CHARS,
        description="The revision this edit was made against. Omit it to overwrite blindly.",
    )


class SkillPost(SkillBody):
    """A skill to add to the library, which names itself."""

    name: str = Field(
        min_length=1,
        max_length=IDENTIFIER_MAX_CHARS,
        description="The skill's slug; it becomes the directory name.",
    )


class ApprovalDecision(_Body):
    """A human's answer to a gated action."""

    decision: Literal["approve", "deny", "edit"]
    edit: dict[str, Any] | None = Field(
        default=None, description="The modified detail, for decision=edit."
    )

    @field_validator("edit")
    @classmethod
    def _bounded_edit(cls, value: dict[str, Any] | None) -> dict[str, Any] | None:
        validate_approval_edit(value)
        return value


class HandoffPost(_Body):
    """Work handed to one named resident, through a route that resident declares."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True, populate_by_name=True)

    to: str = Field(
        min_length=1,
        max_length=IDENTIFIER_MAX_CHARS,
        description="The resident id receiving the work.",
    )
    route: str = Field(
        min_length=1,
        max_length=IDENTIFIER_MAX_CHARS,
        description="A delegation route that resident declares.",
    )
    title: str = Field(
        min_length=1, max_length=TITLE_MAX_CHARS, description="One line naming the work."
    )
    detail: str = Field(
        default="",
        max_length=DETAIL_MAX_CHARS,
        description="Everything the receiver needs to know.",
    )
    sender: str | None = Field(
        default=None,
        alias="from",
        max_length=IDENTIFIER_MAX_CHARS,
        description="The resident handing the work over. Omit it when a person is.",
    )
    parent_task_id: str | None = Field(
        default=None,
        max_length=IDENTIFIER_MAX_CHARS,
        description="The task this work descends from, for lineage and attribution.",
    )


#: What a refusal costs over HTTP. A recipient steward has never heard of is a 404 like
#: any other unknown resident, and a retired one is a 404 too — from the sender's side there
#: is nobody at that address any more, even though the reason code now says which (steward
#: #W21). Everything else is a conflict between the request and what the two manifests
#: actually declare, which is a 409 and not a malformed request.
DELEGATION_STATUS: Mapping[str, int] = {
    dg.UNKNOWN_RECIPIENT: 404,
    dg.RETIRED_RECIPIENT: 404,
    dg.UNKNOWN_PARENT: 404,
}
DELEGATION_REFUSED_STATUS = 409


# --------------------------------------------------------------------------------------
# run-now
# --------------------------------------------------------------------------------------


#: The error code a run-now is refused with when the resident is already busy — whether
#: this process is running it or another one is (warren#111). One code, because from the
#: caller's side it is one fact: ask again when the session that is going has finished.
ALREADY_RUNNING_ERROR = "already_running"


class AlreadyRunningError(Exception):
    """Raised when a resident is asked to run while a session of its own is still going.

    ``reason`` is the sentence the refusal is served with, because the two ways this can
    happen are worth telling apart: this process already has that routine in flight, or
    another process — the scheduler daemon, a dispatch, a chat daemon — is running the
    resident right now (warren#111). Same 409 and the same code either way; a caller that
    only needs to know when to ask again reads either sentence the same way.
    """

    def __init__(self, reason: str) -> None:
        """Carry the sentence this refusal is served with."""
        super().__init__(reason)
        self.reason = reason


@dataclass(slots=True)
class ManualRuns:
    """Fires routines on request, through the scheduler's own fire path.

    Manual runs go through :meth:`steward.scheduler.Scheduler.fire` rather than a
    second implementation, so a run-now gets the same bracketing events, the same
    prompt assembly, the same timeout, and the same runner seam as a scheduled fire.
    Only the ``trigger`` differs — ``manual`` instead of ``schedule`` — so the ledger
    can tell work steward decided to do from work a human asked for (steward #23).

    The scheduler's other promise carries over too, now in both halves: one run per routine
    at a time here, and one session per resident across every process
    (:mod:`steward.claims`). A second run-now while the first is still going is refused with
    a 409 rather than queued, because a queue would let the village show an hourly routine as
    a backlog.

    Worth saying plainly, because it differs from the scheduler: a run-now for routine *B*
    while routine *A* of the same resident is going is refused too, not serialised. The
    scheduler serialises those, and should — it is executing a schedule, and both occurrences
    are work it already decided to do. A human at the door is asking for a session *now*, and
    "now" is the one thing steward cannot give while the resident is busy. Telling them so is
    the honest answer; holding the request open until the resident frees up is the queue this
    whole rule exists to refuse.
    """

    scheduler: Scheduler
    store: Store
    #: The cross-process claim this surface reads before accepting. Defaults to the one the
    #: scheduler it fires through will actually take, so the read and the take can never
    #: disagree about which claim is being talked about.
    claims: ResidentClaims | None = None
    max_workers: int = 4
    _pool: ThreadPoolExecutor = field(init=False)
    _inflight: set[str] = field(default_factory=set, init=False)
    _futures: list[Future[None]] = field(default_factory=list, init=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, init=False)

    def __post_init__(self) -> None:
        """Open the small pool manual runs execute on, and settle which claim to read."""
        if self.claims is None:
            self.claims = self.scheduler.claims
        self._pool = ThreadPoolExecutor(max_workers=self.max_workers, thread_name_prefix="run-now")

    def submit(self, item: ScheduledRoutine, request_id: str) -> None:
        """Queue exactly one run of ``item``, or refuse because one is already going.

        The cross-process claim is *read* here and taken in the fire path, which is the
        same division ``guard.allow`` already lives on: this read is what turns a genuine
        overlap into a 409 a human sees, and the claim the fire actually takes is what
        settles a race two reads could both pass. So a refusal here is never wrong, and a
        pass here is not yet a promise.
        """
        holder = self.claims.holder(item.resident.id) if self.claims is not None else None
        if holder is not None:
            raise AlreadyRunningError(
                f"{item.resident.id} is already running — {holder.describe()}; "
                f"{ONE_SESSION_PER_RESIDENT}, so ask again when it has finished"
            )
        with self._lock:
            if item.key in self._inflight:
                raise AlreadyRunningError(
                    f"a run of {item.key} is still going; steward skips an overlapping fire "
                    "rather than queueing one, so ask again when it has finished"
                )
            self._inflight.add(item.key)
            future = self._pool.submit(self._fire, item, request_id)
            self._futures.append(future)

    def _fire(self, item: ScheduledRoutine, request_id: str) -> None:
        try:
            report = self.scheduler.fire(item, trigger=TRIGGER_MANUAL)
            outcome, detail = _fire_outcome(report)
            self.store.set_request_outcome(request_id, outcome, {"routine": item.key, **detail})
        except Exception as exc:  # noqa: BLE001 — a failed run is a logged fact, not a crash
            log.warning("%s: manual run failed: %s", item.key, exc)
            self.store.set_request_outcome(
                request_id, "failed", {"routine": item.key, "error": f"{type(exc).__name__}: {exc}"}
            )
        finally:
            with self._lock:
                self._inflight.discard(item.key)

    def wait(self, timeout: float = 10.0) -> None:
        """Block until every submitted run has finished. For tests and shutdown."""
        with self._lock:
            pending = list(self._futures)
        wait_for_futures(pending, timeout=timeout)

    def shutdown(self) -> None:
        """Stop accepting runs. In-flight sessions are left to finish on their own."""
        self._pool.shutdown(wait=False)


def _fire_outcome(report: FireReport) -> tuple[str, dict[str, Any]]:
    """Describe how a fire ended, for the request log. The one place the API says "ran"."""
    detail: dict[str, Any] = {"run_id": report.run_id}
    if not report.fired:
        return f"skipped: {report.skipped_reason}", detail
    if report.result is not None and report.result.ok:
        return "ran", detail
    detail["error"] = report.result.summary() if report.result is not None else "no result"
    return "failed", detail


# --------------------------------------------------------------------------------------
# views
# --------------------------------------------------------------------------------------


def budget_summary(status: BudgetStatus) -> dict[str, Any]:
    """Return the small budget block the list view carries on every resident.

    Deliberately smaller than :meth:`BudgetStatus.to_dict` — a fleet list wants a fuel
    gauge and a stopped flag, not a full ledger window — but never *quieter*: a resident
    with no declared cap reports ``declared: false`` and a ``summary`` of ``no limit``,
    because a panel that simply omits the gauge would let unlimited read as unknown.
    """
    return {
        "declared": status.declared,
        "paused": status.paused,
        "summary": status.summary(),
        "spent_usd": round(status.spend.cost_usd, 6),
        "tokens": status.spend.tokens,
        "runs": status.spend.runs,
        "budgets": [gauge.to_dict() for gauge in status.gauges],
        "window": status.window.to_dict(),
    }


def resident_view(resident: Resident, library: SkillLibrary | None = None) -> dict[str, Any]:
    """Return the JSON view of one validated manifest.

    Safe to serve wholesale: a manifest that contained a credential-shaped key or an
    inline secret would have failed validation and never become a ``Resident`` at all,
    so there is nothing here to redact.

    ``effective_skills`` is what a session for this resident is actually given — the
    library's defaults plus this manifest's grants — so the panel can show the set
    without re-deriving it from two places.

    ``voice`` is the soul's own ``## Voice`` section, exactly the text
    :mod:`steward.prompt` injects. It is already parsed and in memory here, and a panel
    that showed a resident's charter but not the style it writes in would be showing half
    of who it is. ``None`` means the soul declares no voice, which is a real answer.
    """
    manifest = resident.manifest
    resolved = effective_skills(manifest, library) if library is not None else ()
    return {
        "id": manifest.id,
        "uid": str(manifest.uid),
        "home": manifest.home,
        "agent_id": manifest.agent_id,
        "project": manifest.project,
        "summary": manifest.summary,
        # Retirement is a lifecycle state, so a retired resident is *listed* rather than
        # hidden — a fleet view that quietly dropped it would be a fleet view that cannot
        # answer what used to run here.
        "retired": manifest.retired,
        "path": str(resident.path),
        "soul": manifest.soul.model_dump(mode="json"),
        "voice": resident.soul.voice,
        "charter": manifest.charter.model_dump(mode="json"),
        "skills": [skill.model_dump(mode="json") for skill in manifest.skills],
        # What the session is actually given: the library's defaults plus those grants.
        "effective_skills": [skill.name for skill in resolved],
        "memory": manifest.memory.model_dump(mode="json"),
        "routes": [route.model_dump(mode="json") for route in manifest.routes],
        "app_grants": [grant.model_dump(mode="json") for grant in manifest.app_grants],
        # Which tools a session may reach: the names, or the word "unrestricted". Here
        # rather than folded into "runner" because it is a capability dimension like the
        # four above it, and because "which residents are unbounded" should be one read.
        "tools": manifest.tools.model_dump(mode="json"),
        # And where those tools may act: the directories opened to a session beyond the
        # working directory it is confined to. Empty is the common, and the safe, answer.
        "workspace": list(manifest.workspace),
        # Which brain, answerable without opening a file.
        "runner": {"kind": manifest.runner.kind, "model": manifest.runner.model},
        # Whether this resident takes work off the board, and on what terms.
        "board": manifest.board.model_dump(mode="json"),
        # And whether it may hand work to anybody else, and to whom.
        "delegation": manifest.delegation.model_dump(mode="json"),
        # Whether steward taps a human about this resident, and about what (warren#114).
        # The *declaration* only: the derived ntfy topic is deliberately not here and not
        # anywhere else a browser can reach, because on ntfy the topic is the capability —
        # `steward notify list`, at a terminal, is the one place it is printed.
        "notifications": manifest.notifications.model_dump(mode="json"),
        "routines": [
            {
                "id": routine.id,
                "schedule": routine.schedule,
                "schedule_tz": routine.schedule_tz,
                "requires": list(routine.requires),
                "timeout_s": routine.timeout_s,
                "enabled": routine.enabled,
            }
            for routine in manifest.routines
        ],
    }


def _refuse(status: int, error: str, message: str) -> NoReturn:
    """Fail a request immediately, with a reason a UI can key on and a human can read."""
    raise HTTPException(status_code=status, detail={"error": error, "message": message})


#: How a refused provision is answered. A reason the nursery named maps to the status that
#: reason means; anything it did not name is the host having answered and said no, which is
#: not something the caller can fix by sending different bytes — the same reasoning
#: :data:`WRITE_STATUS` applies to a tree with no git behind it.
PROVISION_STATUS: Mapping[str, int] = {
    "unknown_resident": 404,
    "resident_retired": 409,
    "declaration_invalid": 409,
}
PROVISION_FAILED = "provision_failed"
PROVISION_REFUSED = "provision_refused"


def _deployed_message(report: NurseryReport) -> str:
    """Say what a finished provision came to — **both** halves of it.

    The container going up and the schedule check passing are two facts, and a report that
    said only the first would be a control panel's one unforgivable sin. Shared by both
    doors onto the nursery so they cannot come to describe the same outcome differently.
    """
    if report.register is not None and not report.register.ok:
        return (
            "the container is up, but the schedule check did not pass — see "
            "register.problems; nothing fires until those are fixed"
        )
    return (
        "the container is up and the schedule was checked; the resident appears in the "
        "village when it emits its own first event, and never before"
    )


def _provision_message(report: NurseryReport) -> str:
    """Say what ``POST /residents/{id}/provision`` came to, rehearsals included.

    Convergence is said as well as the outcome, never instead of it. A second run that sent
    nothing and *also* cannot schedule is two facts, and picking one of them to print would
    be the same half-truth :func:`_deployed_message` exists to prevent — so the converged
    sentence prefixes that one rather than replacing it.
    """
    if report.dry_run:
        return (
            "nothing was sent, run, or written: this is the plan, and `commands` is the "
            "exact argv a real run would issue"
        )
    if report.changed:
        return _deployed_message(report)
    return (
        f"converged: the host already had this bundle, so nothing was sent. "
        f"{_deployed_message(report)}"
    )


def _refuse_reload(errors: Sequence[str]) -> NoReturn:
    """Refuse to swap in a tree that does not validate, and say which part does not."""
    raise HTTPException(
        status_code=409,
        detail={
            "error": "tree_invalid",
            "message": (
                "the residents tree does not validate, so nothing was reloaded and this "
                "process is still running the last declarations that did; run "
                "`steward validate` for the field-by-field diagnostics"
            ),
            "errors": list(errors),
        },
    )


def _refuse_if_retired(resident: Resident) -> None:
    """Refuse to give work to a retired resident, with the reason every path shares."""
    complaint = retired_complaint(resident)
    if complaint is not None:
        _refuse(409, "resident_retired", complaint)


def _find_resident(result: ValidationResult, resident_id: str, residents_dir: Path) -> Resident:
    for resident in result.residents:
        if resident.id == resident_id:
            return resident
    # A uid also names a resident here. An id is a directory name: retire `pip` and raise a
    # new `pip` next month and the name has moved, while the uid (#112) never does — so a
    # link, a bookmark or a UI route that must still mean *this* resident a year from
    # now can carry the uid instead. Ids are matched first and exhaustively, so no caller
    # that works today can change meaning: a uid only ever resolves what an id did not.
    for resident in result.residents:
        if resident.uid == resident_id:
            return resident
    if (residents_dir / resident_id).is_dir():
        # The resident exists but did not validate. Saying "unknown" would send someone
        # looking for a missing directory instead of a broken manifest. This branch can
        # only ever fire for an id: a manifest that does not validate was never parsed,
        # so its uid is not a fact steward holds, and a uid for a broken resident falls
        # through to the 404 below. That is the honest answer available.
        _refuse(
            409,
            "resident_invalid",
            f"resident {resident_id!r} exists but its manifest does not validate; "
            f"run `steward validate` for the field-by-field diagnostics",
        )
    _refuse(404, "unknown_resident", f"no resident {resident_id!r} in {residents_dir}")


# --------------------------------------------------------------------------------------
# the app
# --------------------------------------------------------------------------------------


#: Methods a session credential may use on any route. Reads only.
SESSION_SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})

#: The write paths a session credential may reach, exactly.
#:
#: An allowlist, so a route added later is refused until somebody decides otherwise —
#: the opposite way round from a denylist, where a new write path would be session-reachable
#: the moment it was merged and nobody would notice.
#:
#: It is a short list because the write surface a session actually wants is small. There is
#: no endpoint to *raise* an approval at all — the routes are ``GET /approvals``,
#: ``GET /approvals/{id}`` and the human-only ``POST /approvals/{id}`` — so raising stays on
#: the block and CLI path either way. This credential buys denial and identity, not reach.
SESSION_WRITE_PATHS = frozenset({"/delegate"})

#: Why a particular refusal is the one it is. Generic prose would tell a session it may not
#: write; these say what the act *is*, which is the part worth knowing: these three are
#: human acts, and a session that could perform them would be answering its own knock,
#: declaring its own colleagues, or firing its own work.
#: **Most specific first**, and the first match wins: the routine-fire path is
#: ``/residents/{id}/routines/{id}/run``, so a ``/residents`` fragment ahead of
#: ``/routines/`` would tell a session it had tried to declare a resident.
_SESSION_REFUSALS: tuple[tuple[str, str], ...] = (
    (
        "/declaration",
        (
            "a resident's charter, skills and routines are written about it rather than by "
            "it; a session that could edit its own declaration would be choosing its own "
            "rules, which is the one thing the declaration exists to stop"
        ),
    ),
    (
        "/skills",
        (
            "a skill is a capability somebody granted; a session that could write one would "
            "be handing itself instructions nobody approved"
        ),
    ),
    (
        "/reload",
        (
            "when the fleet re-reads its declarations is an operator's decision, not "
            "something a running session arranges for itself"
        ),
    ),
    (
        "/approvals/",
        (
            "deciding an approval is the human end of the escalation boundary; a session "
            "that could decide would be answering its own knock"
        ),
    ),
    (
        "/routines/",
        (
            "firing a routine is a human act; a session's own work arrives through the "
            "board and its inbox"
        ),
    ),
    (
        "/provision",
        (
            "provisioning is starting a container on a machine over ssh; a session that "
            "could do it would be building its own colleagues, or itself again"
        ),
    ),
    (
        "/residents",
        ("declaring a resident is a human act; a session may not add to the fleet it is part of"),
    ),
)


def _session_refusal(path: str) -> str:
    """Name the act a session credential was refused, as specifically as steward can."""
    for fragment, reason in _SESSION_REFUSALS:
        if fragment in path:
            return reason
    return "this write path is not one a session credential reaches"


def _presented_bearer_ascii(headers: Sequence[tuple[bytes, bytes]]) -> str:
    """Return the presented bearer value as text, or ``""`` if it is not even ASCII.

    Every credential steward mints is ASCII by construction, so a value that does not
    decode is not one of them and never needs to reach a shape test.
    """
    try:
        return _presented_bearer(headers).decode("ascii")
    except UnicodeDecodeError:
        return ""


def _presented_session_credential(headers: Sequence[tuple[bytes, bytes]]) -> str:
    """Return the presented bearer value if it is *shaped* like a session credential.

    A cheap syntactic test that grants nothing: the API tries the human token first and
    only reaches for the run registry when what was presented could not be anything else.
    """
    presented = _presented_bearer_ascii(headers)
    return presented if looks_like_session_credential(presented) else ""


def _presented_operator_credential(headers: Sequence[tuple[bytes, bytes]]) -> str:
    """Return the presented bearer value if it is *shaped* like an operator credential.

    The same grant-nothing test as its session sibling, against the other prefix. The two
    prefixes are distinct precisely so this dispatch cannot be ambiguous.
    """
    presented = _presented_bearer_ascii(headers)
    return presented if looks_like_operator_credential(presented) else ""


type PrincipalLookup = Callable[[str], SessionPrincipal | None]
type OperatorLookup = Callable[[str], OperatorPrincipal | None]


def _auth_dependency(
    token: str | None, principal_for: PrincipalLookup, operator_for: OperatorLookup
) -> Callable[[Request], None]:
    """Build the gate every endpoint hangs off, and record who got through it.

    Three kinds of caller, tried in the order that keeps the cheapest check first and the
    database out of the common path:

    **The master token** (``STEWARD_TOKEN``), one constant-time compare, no principal —
    the CLI's and the environment's credential.

    **A named operator** (warren#225), looked up by digest against ``operator_credentials``.
    A *human* principal: it reaches exactly what the master token reaches, and the only
    difference is that steward can say who it was, which is the difference the audit trail
    lives on. This is what a browser is given, so the master token stops going into one.

    **A session** (steward #41), looked up against the live run registry, and then held to
    the reads-plus-``/delegate`` allowlist below. Unchanged by any of the above: that
    allowlist exists to keep *sessions* out of human acts, and an operator is a human.
    """

    def require_token(request: Request) -> None:
        headers = request.scope.get("headers", [])
        # Set before any branch: a route reading these must never see a principal left
        # over from the request before it.
        request.state.session = None
        request.state.operator = None
        if _authorized(headers, token):
            return
        presented_operator = _presented_operator_credential(headers)
        operator = operator_for(presented_operator) if presented_operator else None
        if operator is not None:
            request.state.operator = operator
            return
        presented = _presented_session_credential(headers)
        principal = principal_for(presented) if presented else None
        if principal is None:
            raise HTTPException(
                status_code=401,
                detail={
                    "error": "unauthorized",
                    "message": (
                        f"this endpoint needs Authorization: Bearer <{TOKEN_ENV}>, an "
                        f"operator credential minted with `steward operator mint`, or the "
                        f"credential steward minted for a live run (${SESSION_TOKEN_ENV})"
                    ),
                },
                headers={"WWW-Authenticate": "Bearer"},
            )
        request.state.session = principal
        path = request.url.path.rstrip("/") or "/"
        if request.method not in SESSION_SAFE_METHODS and path not in SESSION_WRITE_PATHS:
            _refuse(
                403,
                "session_credential_forbidden",
                f"{principal.resident_id} presented the credential for run "
                f"{principal.run_id}, and {_session_refusal(path)}. Nothing was recorded.",
            )

    return require_token


def operator_of(request: Request) -> OperatorPrincipal | None:
    """Return the named operator who made this request, or ``None`` for anyone else.

    ``None`` covers the master token — a shared secret with no person behind it — and open
    mode, where there is no credential to name anybody by. Both are honest answers, and
    both make steward fall back to describing the *door* a change came through rather than
    inventing a person for it.

    Set by the gate, which runs before any route.
    """
    principal = getattr(request.state, "operator", None)
    return principal if isinstance(principal, OperatorPrincipal) else None


def session_of(request: Request) -> SessionPrincipal | None:
    """Return the resident whose session made this request, or ``None`` for a human.

    The distinction is the whole of steward #41. A **session** presents the credential
    minted for its own run, which *is* a principal: it names a resident, dies with the run,
    and reaches only what a session legitimately needs. Every other caller is a human —
    the master ``STEWARD_TOKEN``, or a named operator credential (see :func:`operator_of`,
    warren#225) — and reaches everything.

    ``None`` also covers open mode (``--allow-open``), where there is no token to compare
    and so no caller steward can tell apart. That is not a gap this function can close: a
    session running against an open steward can reach any route with no header at all.

    Set by the gate, which runs before any route.
    """
    principal = getattr(request.state, "session", None)
    return principal if isinstance(principal, SessionPrincipal) else None


def _presented_bearer(headers: Sequence[tuple[bytes, bytes]]) -> bytes:
    """Return the single presented bearer value, or ``b""``.

    Exactly one Authorization field is accepted.  Rejecting duplicates avoids proxy and
    framework disagreement over first/last/comma-joined semantics.

    One parse for both credential kinds, and bytes rather than ``str`` on purpose: the
    human token is compared byte for byte, and decoding first would let an
    invalid-UTF-8 header be lossily normalised into a comparison it should have failed
    (steward #41).
    """
    values = [value for key, value in headers if key.lower() == b"authorization"]
    if len(values) != 1:
        return b""
    scheme, separator, presented = values[0].partition(b" ")
    if separator != b" " or scheme.lower() != b"bearer":
        return b""
    return presented.strip()


def _authorized(headers: Sequence[tuple[bytes, bytes]], token: str | None) -> bool:
    """Apply the API's human-token policy to raw ASGI headers.

    All presented bearer tokens reach the same constant-time comparison used by the route
    dependency.  ``token is None`` is open mode, where there is nothing to compare.
    """
    if token is None:
        return True
    presented = _presented_bearer(headers)
    return bool(presented) and compare_digest(presented, token.encode("utf-8"))


class _ApprovalBodyDepthMiddleware:
    """Bound approval JSON while receiving, before recursive materialisation."""

    def __init__(self, app: ASGIApp, *, token: str | None) -> None:
        self.app = app
        self.token = token

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:  # noqa: C901
        path = scope.get("path", "")
        is_decision = (
            scope.get("type") == "http"
            and scope.get("method") == "POST"
            and path.startswith("/approvals/")
            and "/" not in path.removeprefix("/approvals/")
        )
        if not is_decision:
            await self.app(scope, receive, send)
            return
        # A session credential is *authenticated* and then refused by the route policy, so
        # it reaches body parsing the way the human token does. Bound it here on shape
        # alone — no database lookup in the middleware — or the depth guard would hold for
        # one credential kind and not the other. An operator credential is on the same
        # footing and for the same reason: it decides approvals, so it is the credential
        # most likely to be carrying one of these bodies (warren#225).
        headers = scope.get("headers", [])
        if not (
            _authorized(headers, self.token)
            or _presented_operator_credential(headers)
            or _presented_session_credential(headers)
        ):
            await self.app(scope, receive, send)
            return

        body = bytearray()
        complete = False
        terminal: Message | None = None
        saw_request = False
        while True:
            message = await receive()
            if message["type"] != "http.request":
                terminal = message
                break
            saw_request = True
            chunk = message.get("body", b"")
            if len(body) + len(chunk) > APPROVAL_BODY_MAX_BYTES:
                response = JSONResponse(
                    status_code=413,
                    content={
                        "detail": {
                            "error": "approval_body_too_large",
                            "message": (
                                "approval request body exceeds the "
                                f"{APPROVAL_BODY_MAX_BYTES} byte wire limit"
                            ),
                        }
                    },
                )
                await response(scope, receive, send)
                return
            body.extend(chunk)
            if not message.get("more_body", False):
                complete = True
                break
        try:
            # The request object is level one; an eight-level edit is therefore level nine.
            if complete:
                validate_json_container_depth(body, EDIT_MAX_DEPTH + 1)
        except ValueError as error:
            response = JSONResponse(
                status_code=422,
                content={
                    "detail": [
                        {
                            "type": "value_error",
                            "loc": ["body", "edit"],
                            "msg": f"Value error, {error}",
                            "input": None,
                        }
                    ]
                },
            )
            await response(scope, receive, send)
            return

        replayed_body = False
        replayed_terminal = False

        async def replay() -> Message:
            nonlocal replayed_body, replayed_terminal
            if saw_request and not replayed_body:
                replayed_body = True
                return {
                    "type": "http.request",
                    "body": bytes(body),
                    "more_body": not complete,
                }
            if terminal is not None and not replayed_terminal:
                replayed_terminal = True
                return terminal
            return await receive()

        await self.app(scope, replay, send)


def create_app(  # noqa: C901, PLR0913, PLR0915 — flat routes; every collaborator is a seam
    config: ApiConfig | None = None,
    *,
    store: Store | None = None,
    emitter: ev.Emitter | None = None,
    runner_factory: RunnerFactory = build_runner,
    nursery: NurseryPipeline = raise_resident,
    provisioner: ProvisionPipeline = provision_resident,
    transport: Transport | None = None,
    approval_expiry_interval_s: float = APPROVAL_EXPIRY_INTERVAL_S,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> FastAPI:
    """Build the API. Raises :class:`ApiError` rather than serving without a token.

    Collaborators are injectable so tests exercise the real routes with a mock runner,
    a scratch database, and an emitter that writes to a file instead of a village.

    ``nursery`` and ``transport`` are the deploy path's two seams. ``transport=None``
    means each provision builds the ssh transport its own manifest addresses, which is
    what a real steward wants; a test hands over a
    :class:`steward.deploy.LocalTransport` and the endpoint deploys to a directory.
    """
    settings = config if config is not None else ApiConfig.from_env()
    token = resolve_token(settings.token, allow_open=settings.allow_open)
    residents_dir = Path(settings.residents_dir)
    # Read once at startup, like the residents tree's souls: a skill edited on disk
    # lands on the next restart, and the library is one object the whole app shares.
    library = library_for(residents_dir, settings.skills_dir)

    db = store if store is not None else Store(settings.db_path or default_db_path())
    sink: ev.Emitter = emitter if emitter is not None else ev.EventEmitter.from_env()

    # The named transitions this API's two mutating domain acts cross. Every durable
    # change and the burrow fact that says it happened are paired in there; what stays
    # here is translation — status codes, the request log, and the words a caller reads.
    #
    # Resolved once, unlike the per-access properties the board, the guard and the
    # delegator expose, because this whole function resolves its collaborators once: the
    # library, the store, the emitter and the guard below are all read at startup and
    # closed over by every route. A route that rebuilt its seam per request would be
    # reading the same two objects it is handed here anyway.
    def session_principal(credential: str) -> SessionPrincipal | None:
        """Resolve a presented session credential against the live run registry.

        The freshness bound is the run's ownership lease, not a window of this endpoint's
        own invention: a credential is accepted exactly while the watchdog could not yet
        bury the run (steward #41). A registry that cannot be read refuses rather than
        admits — an unreadable database is not a reason to let somebody in.
        """
        fresh_since = ev.utc_now_iso(now() - timedelta(seconds=RUN_LEASE_GRACE_S))
        try:
            return db.session_principal(credential, fresh_since=fresh_since)
        except Exception:
            log.exception("could not check a presented session credential")
            return None

    def operator_principal(credential: str) -> OperatorPrincipal | None:
        """Resolve a presented operator credential against the credentials table.

        No freshness clause and no lease: an operator credential lives until somebody
        revokes it, which is the whole of what makes it revocable. A table that cannot be
        read refuses rather than admits, for the same reason the session lookup does — an
        unreadable database is not a reason to let somebody in.
        """
        try:
            return db.operator_principal(credential)
        except Exception:
            log.exception("could not check a presented operator credential")
            return None

    tasks = TaskTransitions(store=db, emitter=sink)
    approvals = ApprovalTransitions(store=db, emitter=sink)
    # One guard for the whole app: the run-now path refuses through it before it accepts
    # anything, and the scheduler behind that path ledgers through the same object.
    guard = BudgetGuard(db, sink)

    def complete_approval(record: ApprovalRecord, token: str) -> bool:
        """Atomically apply idempotent post-announcement effects and their marker."""
        completed, _resumed = db.complete_approval_effects(record, token)
        return completed

    outbox = ApprovalOutboxWorker(
        approvals,
        complete_approval,
        poll_interval=settings.approval_poll_interval_s,
        close_timeout=settings.approval_close_timeout_s,
    )
    # The same WakeHooks the scheduler daemon runs with, so a manual fire is a fire in every
    # respect (steward #W1): a run-now session's <needs-human>/<delegate> blocks are
    # harvested and its pending decisions are delivered into its preamble, exactly as they
    # are on a scheduled fire. Without these a run-now silently dropped both while still
    # reporting "ran" — and this is burrow's primary write path.
    # The one-session-per-resident claim (:mod:`steward.claims`, warren#111). The API is the
    # *other* process the scheduler daemon has never been able to see, so this is the surface
    # the issue is named after. One object for this process, handed to everything here that
    # could ever open a session, so the two can never hold different ideas of who is busy.
    claims = ResidentClaims(db)
    hooks = Dispatcher.from_path(
        residents_dir,
        db,
        emitter=sink,
        workdir=settings.workdir,
        runner_factory=runner_factory,
        library=library,
        guard=guard,
        claims=claims,
    )
    runs = ManualRuns(
        scheduler=Scheduler(
            [],
            emitter=sink,
            state=SchedulerState(path=default_state_path()),
            workdir=settings.workdir,
            runner_factory=runner_factory,
            library=library,
            guard=guard,
            hooks=hooks,
            registry=db,
            claims=claims,
        ),
        store=db,
        claims=claims,
    )

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        """Own approval announcement and expiry workers for the app's lifetime."""
        outbox.start()
        stopped = asyncio.Event()

        async def expire_approvals() -> None:
            while not stopped.is_set():
                try:
                    # Store access and event delivery are synchronous. Keep them off the
                    # server loop; awaiting each pass also guarantees this worker never
                    # overlaps itself when a slow emitter outlives the interval.
                    await asyncio.to_thread(approvals.expire, now())
                except Exception:
                    log.exception("approval expiry sweep failed; will retry")
                with suppress(TimeoutError):
                    await asyncio.wait_for(stopped.wait(), timeout=approval_expiry_interval_s)

        worker = asyncio.create_task(expire_approvals(), name="steward-approval-expiry")
        _app.state.approval_expiry_task = worker
        try:
            yield
        finally:
            stopped.set()
            await worker
            _app.state.approval_expiry_task = None
            outbox.close()

    app = FastAPI(
        title="steward",
        summary="The token-gated write path into the agent fleet burrow watches.",
        version="0",
        # Every route here is a write path, so nothing is served unauthenticated —
        # including the schema. docs/api.md is the documentation.
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        # Two dependencies, and only the second one decides anything. `HTTPBearer` is
        # declared with `auto_error=False`, so it accepts and refuses nothing at all: it
        # exists to put `securitySchemes` and a per-operation `security` into the exported
        # document (warren#321), which is the machine-readable half of docs/api.md and
        # would otherwise describe a completely unauthenticated API. A client generated
        # from a document without it sends no Authorization header and gets a blanket 401.
        dependencies=[
            Depends(HTTPBearer(auto_error=False)),
            Depends(_auth_dependency(token, session_principal, operator_principal)),
        ],
        # Declared once here rather than on twenty-five routes: every route in this API is
        # token-gated, so every route answers this.
        responses={
            401: {
                "description": "No credential was presented, or steward refused the one that was."
            }
        },
        lifespan=lifespan,
    )
    app.add_middleware(_ApprovalBodyDepthMiddleware, token=token)
    if settings.cors_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=list(settings.cors_origins),
            allow_credentials=False,
            allow_methods=["GET", "POST", "OPTIONS"],
            allow_headers=["Authorization", "Content-Type"],
        )
    app.state.store = db
    app.state.runs = runs
    app.state.emitter = sink
    app.state.guard = guard
    app.state.residents_dir = residents_dir
    app.state.library = library
    app.state.open_mode = token is None
    app.state.approval_outbox = outbox

    def accept(request: Request, outcome: str, detail: Mapping[str, Any] | None = None) -> str:
        """Log an accepted mutating request and return the id it is traceable by."""
        request_id = new_id()
        db.log_request(
            request_id=request_id,
            method=request.method,
            path=request.url.path,
            outcome=outcome,
            detail=detail,
        )
        return request_id

    # -- residents -------------------------------------------------------------------

    @app.get("/residents")
    def list_residents() -> dict[str, Any]:
        """List the validated residents, and name the manifests that did not validate."""
        result = validate_path(residents_dir, settings.skills_dir)
        current = library_for(residents_dir, settings.skills_dir)
        return {
            "residents": [
                {
                    **resident_view(resident, current),
                    # The fuel gauge burrow's fleet-ops view draws, on the one call that
                    # already lists everybody. A stopped resident should not need a
                    # second round trip to look stopped.
                    "budget": budget_summary(guard.status(resident.manifest)),
                }
                for resident in result.residents
            ],
            "errors": [diagnostic.render() for diagnostic in result.errors],
        }

    @app.get("/residents/{resident_id}")
    def get_resident(resident_id: str) -> dict[str, Any]:
        """Return one validated manifest, runner included, so "which brain" is answerable."""
        result = validate_path(residents_dir, settings.skills_dir)
        current = library_for(residents_dir, settings.skills_dir)
        return resident_view(_find_resident(result, resident_id, residents_dir), current)

    @app.get("/residents/{resident_id}/budget")
    def get_resident_budget(resident_id: str) -> dict[str, Any]:
        """Return spent-against-limit for each budget, the window, and the pause state.

        The read burrow's fleet-ops view (burrow #40) draws fuel gauges from. Everything
        in it is a sum over rows steward wrote when runs finished, inside a window
        computed from the calendar at the moment of this request — so a steward that
        restarted an hour ago answers exactly what one that has been up all day answers.
        """
        result = validate_path(residents_dir, settings.skills_dir)
        resident = _find_resident(result, resident_id, residents_dir)
        return guard.status(resident.manifest).to_dict()

    @app.get("/residents/{resident_id}/journal")
    def get_resident_journal(resident_id: str, limit: int = 14) -> dict[str, Any]:
        """Return the resident's journal, newest first; an empty journal is an empty list."""
        result = validate_path(residents_dir, settings.skills_dir)
        resident = _find_resident(result, resident_id, residents_dir)
        complaint = journal_complaint(resident.manifest)
        if complaint is not None:
            _refuse(409, "journal_unreadable", complaint)
        entries = read_entries(resident.manifest, max(0, min(limit, 100)))
        return {"resident": resident.id, "entries": [entry.as_dict() for entry in entries]}

    @app.post("/residents", status_code=201)
    def create_resident(body: ResidentPost, request: Request) -> dict[str, Any]:
        """Declare a resident, and — only when asked — provision and check it.

        The same :func:`steward.nursery.raise_resident` pipeline ``steward new-resident``
        runs, with two settings the API always makes for itself:

        ``commit=False``
            The *nursery* does not commit here, because its commit is bound up with its own
            dirty-worktree refusal, which is right for a terminal and wrong for a server.
            The declaration is committed all the same — by :mod:`steward.authoring`, after
            the pipeline returns, staging only the two files that were written.

            This reverses the endpoint's original stance, deliberately (steward #214). It
            used to commit nothing on the grounds that the server may not own its checkout,
            which left the fleet's newest declarations as the only ones with no history and
            no author. The honest version of that worry is a *configured* one:
            ``STEWARD_ALLOW_UNCOMMITTED_WRITES`` accepts a tree with no git behind it, and
            a tree that has git gets the audit trail everything else gets.
        ``provision=body.deploy``
            Default false, so the endpoint's old behaviour is its default behaviour: files
            for review and nothing else.
        """
        try:
            report = nursery(
                body,
                residents_dir=residents_dir,
                skills_dir=settings.skills_dir,
                transport=transport,
                provision=body.deploy,
                commit=False,
            )
        except NurseryError as exc:
            status = 409 if (residents_dir / body.id).exists() else 400
            _refuse(status, exc.reason or "resident_not_declared", str(exc))
        except TransportError as exc:
            # `deploy: true` and there was nobody to ask — in practice a steward whose own
            # environment has no `CHRONICLE_URL` to give the container, since `emitter_env`
            # refuses before a transport is reached and every later one is already wrapped
            # as a `NurseryError`. This was an unhandled 500: a control panel got a
            # traceback where it needed a sentence (warren#270).
            #
            # The declare stage has already written its two files by now and nothing has
            # committed them, so the refusal says what the next move is. That is a promise
            # the pipeline actually keeps — declaring is idempotent, so the same body
            # converges on the skeleton rather than colliding with it — and a test holds it
            # to that rather than taking the sentence's word for it.
            _refuse(
                409,
                PROVISION_REFUSED,
                f"{exc}; nothing was deployed and this request committed nothing — post "
                f"the same body again once that is fixed and it will pick up where it "
                f"stopped rather than collide",
            )
        request_id = accept(
            request, "deployed" if body.deploy else "declared", {"resident": body.id}
        )
        written = [
            path
            for path in (report.declare.manifest_path, report.declare.soul_path)
            if path.is_file()
        ]
        try:
            commit = au.commit_write(
                residents_dir,
                written,
                au.DECLARE_SUBJECT.format(id=body.id),
                request_id=request_id,
                principal=acting_principal(request),
                **write_settings(request),
            )
        except au.AuthoringError as exc:
            db.set_request_outcome(request_id, f"refused: {exc.reason}")
            refuse_write(exc)
        uncommitted = commit.note
        deployed = (
            _deployed_message(report)
            if body.deploy
            else "nothing is deployed and no routine is scheduled: this is a file for review"
        )
        return {
            "request_id": request_id,
            "status": "accepted",
            "message": f"{deployed}. {uncommitted}",
            # The four keys this endpoint has always returned, kept at the top level so
            # the deploy flag is additive for anything already reading the response.
            "id": body.id,
            "directory": str(report.declare.manifest_path.parent),
            "manifest_path": str(report.declare.manifest_path),
            "soul_path": str(report.declare.soul_path),
            **report.to_dict(),
            # Last, and deliberately after the report: `declare.commit` is the *nursery's*
            # commit, which is always null here because the API asks it not to commit.
            # This is the one that happened.
            "commit": commit.to_dict(),
        }

    # -- writing declarations and skills (steward #214) -------------------------------

    def refuse_write(exc: au.AuthoringError) -> NoReturn:
        """Turn an authoring refusal into its HTTP answer, diagnostics and all.

        Structured rather than rendered, unlike the read views: the caller is a form with
        fields to highlight, and three lines of terminal prose cannot tell it which one.
        """
        raise HTTPException(
            status_code=WRITE_STATUS.get(exc.reason, 409),
            detail={
                "error": exc.reason,
                "message": str(exc),
                "diagnostics": [au.diagnostic_as_dict(d) for d in exc.diagnostics],
            },
        )

    def write_settings(request: Request) -> dict[str, Any]:
        """Return the two knobs every write shares, and who git records as the author.

        Configuration decides the author until a *named* caller turns up. An operator
        credential is one (warren#225): it was minted for a person, so their writes are
        committed by them rather than by the generic ``steward (api)`` that is all a shared
        secret can honestly be signed with. ``STEWARD_COMMIT_IDENTITY`` remains what the
        master token and open mode commit as — a single-operator install naming itself.
        """
        operator = operator_of(request)
        identity = (
            CommitIdentity(name=operator.name, email=operator.email)
            if operator is not None
            else settings.commit_identity or au.DEFAULT_IDENTITY
        )
        return {"identity": identity, "allow_uncommitted": settings.allow_uncommitted_writes}

    def acting_principal(request: Request) -> str:
        """How this caller is described in a commit trailer.

        :data:`API_PRINCIPAL` names the door because a shared secret is all there is to
        name; an operator credential names the person, which is the point of having one.
        """
        operator = operator_of(request)
        return operator.principal if operator is not None else API_PRINCIPAL

    def acted_by(request: Request) -> str:
        """Who steward's own records say did this — an operator's name, else ``api``.

        The same substitution as the commit author, in the two places steward stores a
        "who" of its own rather than handing one to git: the board's ``posted_by`` and an
        approval's ``decided_by``. An audit view whose every row says ``api`` is an audit
        view that cannot answer the only question it is for.
        """
        operator = operator_of(request)
        return operator.name if operator is not None else ACTED_BY_API

    @app.post("/residents/{resident_id}/provision")
    def provision_declared_resident(
        resident_id: str, request: Request, body: ProvisionPost | None = None
    ) -> dict[str, Any]:
        """Build a resident from the manifest already in the tree, and check its schedule.

        The other door onto the nursery (warren#270). ``POST /residents`` assembles a
        declaration from a request body and refuses to converge it onto a manifest somebody
        has since edited — which left every resident carrying a route, an app grant or a
        ``runner.placement`` with no way onto the nursery path at all, because no body can
        express those fields. This one reads ``residents/<id>/manifest.yaml`` as the source
        of truth and runs provision and register against it.

        **200, not 202.** The container is up and the schedule has been checked by the time
        this answers — there is nothing left to acknowledge later, and saying `accepted`
        about work that already finished would be the one dishonesty the request log exists
        to prevent.

        Nothing is written into the checkout, so unlike every other write here there is no
        commit: the declaration being provisioned was committed by whoever wrote it, and a
        declaration whose bytes are in no commit comes back in ``warnings`` rather than as a
        refusal this endpoint has no way to resolve.
        """
        asked = body or ProvisionPost()
        try:
            report = provisioner(
                resident_id,
                residents_dir=residents_dir,
                skills_dir=settings.skills_dir,
                transport=transport,
                dry_run=asked.dry_run,
            )
        except NurseryError as exc:
            # Keyed on the nursery's own ``reason``, never on the prose or on a second look
            # at the filesystem: "there is no such resident" and "its declaration does not
            # validate" are different answers and only the pipeline that looked knows which.
            # An unnamed one is the host having answered and refused — a bundle that would
            # not land, a `docker compose up` that failed — and it says that rather than
            # borrowing a name for something it is not.
            reason = exc.reason or PROVISION_FAILED
            _refuse(PROVISION_STATUS.get(reason, 409), reason, str(exc))
        except TransportError as exc:
            # Both halves of "there was nobody to ask": a host that did not answer, and a
            # steward with no village address to give the container. One refusal, because
            # the exception's own message already says which, and a traceback would say
            # neither (steward #90).
            _refuse(409, PROVISION_REFUSED, str(exc))
        request_id = accept(
            request,
            "rehearsed" if asked.dry_run else "provisioned",
            {"resident": report.resident_id},
        )
        return {
            "request_id": request_id,
            "message": _provision_message(report),
            **report.to_dict(),
        }

    @app.get("/residents/{resident_id}/declaration")
    def get_declaration(resident_id: str) -> dict[str, Any]:
        """Return the two files that declare this resident, as text and as data.

        The editable source, not the projection :func:`resident_view` serves. Both are
        useful and they are not the same thing: the view is assembled from a validated
        model and is what a fleet list draws, while this is what is actually in git —
        comments, field order, and all — which is the only thing you can sensibly write
        back. ``PUT`` takes exactly this shape.
        """
        result = validate_path(residents_dir, settings.skills_dir)
        resident = _find_resident(result, resident_id, residents_dir)
        soul_file = resident.manifest.soul.file
        declaration = au.read_declaration(residents_dir, resident.id, soul_file)
        return {
            "id": resident.id,
            "uid": str(resident.manifest.uid),
            "manifest": yaml.safe_load(declaration.manifest_text),
            "text": declaration.manifest_text,
            "soul": declaration.soul_text,
            "soul_file": soul_file,
            "revision": au.revision_of(
                *au.declaration_paths(residents_dir, resident.id, soul_file)
            ),
            "paths": [str(p) for p in au.declaration_paths(residents_dir, resident.id, soul_file)],
        }

    @app.put("/residents/{resident_id}/declaration")
    def put_declaration(resident_id: str, body: DeclarationPut, request: Request) -> dict[str, Any]:
        """Replace a resident's declaration, if it validates, and commit it.

        **Human callers only**, and this is the sharpest instance of that rule in the whole
        API: a resident that could rewrite its own charter would be choosing the rules it is
        held to.

        A full replacement rather than a patch. Merging a partial edit into a manifest means
        steward deciding what a missing key meant — cleared, or untouched? — and the
        declaration is the wrong file to be clever with. Read it, change it, write it back.
        The ``revision`` from the ``GET`` is how two editors find out about each other.

        Nothing is written unless the whole tree still validates with this change applied,
        including the checks that only exist across residents. A refusal has written
        nothing, committed nothing, and left the resident exactly as it was.
        """
        manifest_text = (
            body.text
            if body.text is not None
            else yaml.safe_dump(body.manifest, sort_keys=False, allow_unicode=True)
        )
        declaration = au.Declaration(manifest_text=manifest_text, soul_text=body.soul)
        request_id = accept(request, "written", {"resident": resident_id})
        try:
            written = au.write_declaration(
                residents_dir,
                resident_id,
                declaration,
                request_id=request_id,
                principal=acting_principal(request),
                skills_dir=settings.skills_dir,
                expected_revision=body.revision,
                **write_settings(request),
            )
        except au.AuthoringError as exc:
            db.set_request_outcome(request_id, f"refused: {exc.reason}")
            refuse_write(exc)
        return {
            "request_id": request_id,
            "status": "accepted",
            "id": written.paths[0].parent.name,
            "revision": written.revision,
            "paths": [str(p) for p in written.paths],
            "commit": written.commit.to_dict(),
            "warnings": [au.diagnostic_as_dict(d) for d in written.validation.warnings],
            "message": (
                f"written and validated; {written.commit.note}. The scheduler picks this up "
                f"on its next wake-up, or immediately via POST /reload"
            ),
        }

    # -- skills ----------------------------------------------------------------------

    def skills_view(current: SkillLibrary) -> dict[str, Any]:
        """Render the library and who holds each skill."""
        result = validate_path(residents_dir, settings.skills_dir)
        holders: dict[str, list[str]] = {skill.name: [] for skill in current}
        for resident in result.residents:
            for skill in effective_skills(resident.manifest, current):
                holders[skill.name].append(resident.id)
        return {
            "library": str(current.path) if current.path is not None else None,
            "skills": [{**skill.as_dict(), "holders": holders[skill.name]} for skill in current],
            "errors": [diagnostic.render() for diagnostic in current.diagnostics],
        }

    @app.get("/skills")
    def list_skills() -> dict[str, Any]:
        """List the skills library, and who holds each skill.

        Read from disk per request rather than from the copy this app started with. That
        was always the honest thing to serve and is now the necessary one: since a skill
        can be written over HTTP (steward #214), a listing built from a startup snapshot
        would not contain the skill the caller just created. It costs nothing extra —
        ``validate_path`` on the line below already re-reads the same library.
        """
        return skills_view(library_for(residents_dir, settings.skills_dir))

    @app.get("/skills/{name}")
    def get_skill(name: str) -> dict[str, Any]:
        """Return one skill's frontmatter and body, with the revision to edit against."""
        root = au.resolve_skills_dir(residents_dir, settings.skills_dir)
        if root is None:
            _refuse(404, "unknown_skill", f"there is no skills library beside {residents_dir}")
        try:
            document, revision = au.read_skill_document(root, name)
        except au.AuthoringError as exc:
            refuse_write(exc)
        return {
            "name": document.name,
            "description": document.description,
            "body": document.body,
            "defaults": document.default,
            "revision": revision,
            "path": str(root / name / "SKILL.md"),
        }

    def write_one_skill(
        document: au.SkillDocument,
        request: Request,
        *,
        created: bool,
        expected_revision: str | None = None,
    ) -> dict[str, Any]:
        """Validate, write and commit one skill — the shared half of POST and PUT."""
        root = au.resolve_skills_dir(residents_dir, settings.skills_dir)
        if root is None:
            # No library yet. The default location beside the tree is where one belongs,
            # and it is created only once the write has actually been accepted.
            root = Path(residents_dir).resolve().parent / "skills"
        request_id = accept(request, "written", {"skill": document.name})
        try:
            written = au.write_skill(
                residents_dir,
                root,
                document,
                request_id=request_id,
                principal=acting_principal(request),
                created=created,
                expected_revision=expected_revision,
                **write_settings(request),
            )
        except au.AuthoringError as exc:
            db.set_request_outcome(request_id, f"refused: {exc.reason}")
            refuse_write(exc)
        return {
            "request_id": request_id,
            "status": "accepted",
            "name": document.name,
            "revision": written.revision,
            "paths": [str(p) for p in written.paths],
            "commit": written.commit.to_dict(),
            "warnings": [au.diagnostic_as_dict(d) for d in written.validation.warnings],
            "message": (
                f"written and validated against the fleet; {written.commit.note}. Sessions "
                f"opened from now on are provisioned with it"
            ),
        }

    @app.post("/skills", status_code=201)
    def create_skill(body: SkillPost, request: Request) -> dict[str, Any]:
        """Add a skill to the library.

        **Human callers only.** Refuses an existing name rather than overwriting it: a
        ``POST`` that quietly replaced somebody's skill would make "add" and "rewrite" the
        same button.

        ``defaults: true`` deserves a second look before sending. A default skill is held
        by every resident in the fleet without any manifest granting it, so this one flag
        changes what every session is given.
        """
        return write_one_skill(
            au.SkillDocument(
                name=body.name,
                description=body.description,
                body=body.body,
                default=body.defaults,
            ),
            request,
            created=True,
        )

    @app.put("/skills/{name}")
    def update_skill(name: str, body: SkillBody, request: Request) -> dict[str, Any]:
        """Replace one skill in the library, if it still validates for the whole fleet.

        **Human callers only**, like every write here.
        """
        return write_one_skill(
            au.SkillDocument(
                name=name, description=body.description, body=body.body, default=body.defaults
            ),
            request,
            created=False,
            expected_revision=body.revision,
        )

    # -- reload ------------------------------------------------------------------------

    @app.post("/reload")
    def reload_fleet(request: Request) -> dict[str, Any]:
        """Re-read the residents tree and the skills library into this process.

        **This process**, and the distinction is the whole of the endpoint's honesty. The
        scheduler daemon is a *different process* — usually on the same burrow, started by
        ``steward serve`` — and no HTTP call can reach into it. It does not need one: it
        watches the trees itself and reloads on its next wake-up (:class:`TreeSource`),
        which is within a minute. What this endpoint fixes is the API's own long-lived
        collaborators, the run-now scheduler and the board dispatcher, which were assembled
        at startup and would otherwise fire a routine against the manifest that was on disk
        when the server booted.

        Read views need no reload at all — they re-read the tree on every request.
        """
        current = library_for(residents_dir, settings.skills_dir)
        result = validate_path(residents_dir, settings.skills_dir)
        errors = [diagnostic.render() for diagnostic in result.errors]
        if errors:
            # The same judgement the daemon makes (:meth:`Scheduler.reload_if_changed`): a
            # tree that stopped validating does not stop the fleet. Swapping in what did
            # parse would quietly retire every resident whose manifest is mid-edit, so the
            # previous snapshot stands and the reason is returned rather than swallowed.
            _refuse_reload(errors)
        active = tuple(m.active_residents(result.residents))
        runs.scheduler.set_library(current)
        runs.scheduler.scheduled = [
            ScheduledRoutine(resident=resident, routine=routine)
            for resident in active
            for routine in resident.manifest.routines
            if routine.enabled
        ]
        hooks.refresh(active, current)
        app.state.library = current
        request_id = accept(request, "reloaded", {"residents": len(active)})
        return {
            "request_id": request_id,
            "status": "reloaded",
            "residents": len(active),
            "routines": len(runs.scheduler.scheduled),
            "skills": [skill.name for skill in current],
            "errors": errors,
            "message": (
                "this API process re-read the tree; the scheduler daemon is a separate "
                "process and picks the same change up on its own next wake-up"
            ),
        }

    # -- routines --------------------------------------------------------------------

    @app.get("/routines")
    def list_routines() -> dict[str, Any]:
        """Every routine of every valid resident: the fleet-wide standing-work ledger.

        Assembled from three things steward already knows and nothing it does not. The
        schedule and the switch come from the manifest. ``next_fire`` is computed from the
        cron expression in the routine's own zone, and is ``null`` for a disabled routine
        because a routine that is off has no next occurrence to promise.

        A **retired** resident's routines are listed — they are still declared, and a
        ledger that hid them could not answer what used to run here — and carry
        ``retired: true`` with ``next_fire: null`` for the same reason a disabled routine
        does: :func:`steward.scheduler.load_scheduled` leaves retired residents out, so
        there is no next occurrence to promise. Run-now refuses them with ``409
        resident_retired``, which is what townhall reads to grey the button out.

        ``anchor`` is the scheduler's own state file, read fresh on every request because
        the daemon is a different process — and it is called an anchor rather than a last
        run because that is what it is: the moment the next occurrence is computed from,
        which is the last fire *or* the moment steward first saw the routine. Calling it
        "last run" would let a routine that has never fired look like one that has.

        ``scheduler`` is the one thing here that *is* a heartbeat: when a scheduler process
        last woke up against that state file, how stale that may get before it stops
        meaning anything is up, and the verdict. ``alive: null`` — never ticked — is its
        own answer, distinct from a daemon that died. A ledger is still a declaration; this
        is what says whether the declarations have anything to fire them.

        ``last_run`` and ``last_request`` are two different facts and both are here
        (warren#104). ``last_request`` is the *API request log*: a run somebody asked for
        over HTTP. A scheduled fire is not an HTTP request, so it never appears there — and
        a panel that showed only that one concluded a perfectly healthy resident "only runs
        when I trigger it manually", which was false. ``last_run`` is the run ledger, which
        every finished session writes to whatever started it, so it carries the trigger
        (``schedule`` or ``manual``) and the outcome. Keeping both is the point: a request
        that was accepted and never ran is exactly the case where they disagree, and that
        disagreement is the diagnosis.
        """
        result = validate_path(residents_dir, settings.skills_dir)
        state = SchedulerState.load(default_state_path())
        now = datetime.now(UTC)
        latest = latest_run_requests(db.requests())
        runs_by_key = db.latest_routine_runs()
        routines = []
        for resident in result.residents:
            for routine in resident.manifest.routines:
                item = ScheduledRoutine(resident=resident, routine=routine)
                anchor = state.anchor(item.key)
                routines.append(
                    {
                        "key": item.key,
                        "resident": resident.id,
                        "resident_name": resident.manifest.soul.name,
                        "accent": resident.manifest.soul.accent,
                        "routine": routine.id,
                        "schedule": routine.schedule,
                        "schedule_tz": routine.schedule_tz,
                        "enabled": routine.enabled,
                        "retired": resident.retired,
                        "requires": list(routine.requires),
                        "timeout_s": routine.timeout_s,
                        "journal": routine.journal,
                        "anchor": anchor.isoformat() if anchor is not None else None,
                        "next_fire": item.next_fire_after(now).isoformat()
                        if routine.enabled and not resident.retired
                        else None,
                        "last_request": latest.get(item.key),
                        "last_run": last_run_view(runs_by_key.get(item.key)),
                    }
                )
        return {
            "routines": routines,
            "state_path": str(default_state_path()),
            "scheduler": scheduler_liveness(state, now),
            "errors": [diagnostic.render() for diagnostic in result.errors],
        }

    # -- run now ---------------------------------------------------------------------

    @app.post("/residents/{resident_id}/routines/{routine_id}/run", status_code=202)
    def run_routine(resident_id: str, routine_id: str, request: Request) -> dict[str, Any]:
        """Ask for one run of one routine, right now, and acknowledge only that."""
        result = validate_path(residents_dir, settings.skills_dir)
        resident = _find_resident(result, resident_id, residents_dir)
        _refuse_if_retired(resident)
        routine = next((r for r in resident.manifest.routines if r.id == routine_id), None)
        if routine is None:
            known = ", ".join(r.id for r in resident.manifest.routines) or "none"
            _refuse(
                404,
                "unknown_routine",
                f"resident {resident_id!r} declares no routine {routine_id!r} "
                f"(declared routines: {known})",
            )
        if not routine.enabled:
            _refuse(
                409,
                "routine_disabled",
                f"routine {routine_id!r} is disabled in {resident.path}; enable it in the "
                f"manifest rather than firing something the declaration says is off",
            )
        refusal = guard.allow(resident.manifest)
        if refusal is not None:
            # Refused before anything is written, like every other refusal here. A human
            # asking for a run now is not a way around a budget the same human set — the
            # message names the number and how to lift it.
            _refuse(409, PAUSED_ERROR, refusal)
        item = ScheduledRoutine(resident=resident, routine=routine)
        request_id = accept(request, "queued", {"routine": item.key})
        try:
            runs.submit(item, request_id)
        except AlreadyRunningError as exc:
            # Two overlaps, one refusal: this process already has that routine in flight, or
            # another process is running the resident and said so in the shared claim
            # (warren#111). The scheduler daemon's in-process lock was invisible here until
            # that claim became durable, which is the whole of the issue. Recorded in the
            # request log either way, so a run somebody asked for and did not get is a fact
            # rather than a silence.
            db.set_request_outcome(request_id, "refused: already running")
            _refuse(409, ALREADY_RUNNING_ERROR, exc.reason)
        return {
            "request_id": request_id,
            "status": "accepted",
            "resident": resident_id,
            "routine": routine_id,
            "trigger": TRIGGER_MANUAL,
            "message": (
                "queued one run; it has happened when routine_started and then "
                "routine_finished or routine_failed appear in burrow's log"
            ),
        }

    # -- job board -------------------------------------------------------------------

    @app.get("/jobs")
    def list_jobs(status: str | None = None) -> dict[str, Any]:
        """List the board, optionally narrowed with ``?status=open|claimed|done|failed``."""
        if status is not None and status not in JOB_STATUSES:
            _refuse(
                422,
                "unknown_status",
                f"status {status!r} is not a board status; use one of: "
                f"{', '.join(JOB_STATUSES)}, or leave it off for the whole board",
            )
        return {"jobs": [job.to_dict() for job in db.jobs(status)]}

    @app.post("/jobs", status_code=202)
    def post_job(body: JobPost, request: Request) -> dict[str, Any]:
        """Put a task on the board and announce it. No resident is prompted."""
        job = tasks.post(
            title=body.title,
            detail=body.detail,
            required_skills=body.required_skills,
            posted_by=acted_by(request),
        ).require()
        request_id = accept(request, "posted", {"task_id": job.task_id})
        return {
            "request_id": request_id,
            "task_id": job.task_id,
            "status": "accepted",
            "message": (
                "queued on the board; a resident claims it on its own next wake-up, and "
                "task_claimed in burrow's log is the only proof that happened"
            ),
        }

    # -- delegation ------------------------------------------------------------------

    @app.get("/residents/{resident_id}/inbox")
    def get_inbox(resident_id: str, status: str | None = None) -> dict[str, Any]:
        """List the work delegated to this resident. Pending by default.

        Pending means *open*: handed over and not yet picked up. ``?status=`` narrows to
        any board status, and ``all`` is everything ever addressed to this resident, which
        is the audit view — who sent it, through which route, and what became of it.

        ``routes`` names every declared delegation route *with its status*, not only the
        ones open today, and ``pending`` is the open count whatever ``?status=`` asked
        for: a caller has to be able to see letters stacked behind a route somebody shut
        (#46), which a list of accepting routes alone cannot show.
        """
        wanted = status or STATUS_OPEN
        if wanted not in (*JOB_STATUSES, APPROVAL_STATUS_ALL):
            _refuse(
                422,
                "unknown_status",
                f"status {status!r} is not an inbox status; use one of: "
                f"{', '.join(JOB_STATUSES)}, all",
            )
        result = validate_path(residents_dir, settings.skills_dir)
        resident = _find_resident(result, resident_id, residents_dir)
        items = db.inbox(resident.id, None if wanted == APPROVAL_STATUS_ALL else wanted)
        return {
            "resident": resident.id,
            "status": wanted,
            "routes": [
                {"id": route.id, "status": route.status, "accepts": route.accepts_delegation}
                for route in resident.inbound_routes
            ],
            "pending": db.inbox_count(resident.id),
            "inbox": [item.to_dict() for item in items],
        }

    @app.post("/delegate", status_code=202)
    def delegate(body: HandoffPost, request: Request) -> dict[str, Any]:
        """Hand work to one resident, if both manifests and the guardrails agree.

        The human path into steward #7; a session uses the ``<delegate>`` block or
        ``steward delegate``, neither of which needs this token. ``from`` names the
        resident handing the work over and its manifest is checked exactly as it would be
        for a block — a person must not be able to make a resident do what its own
        declaration forbids. Omitting ``from`` means the person is the sender, and then
        the receiver's route is the whole of the agreement.

        **A session credential is the sender**, and the body cannot say otherwise
        (steward #41). This route used to read the sender from the request body with
        nothing binding the caller to the resident it named, so a session holding the API
        token could sign as any resident — or omit ``from`` and be read as "a person
        asked" — which skips the sender-charter half of the agreement by design. With the
        sender derived from the credential, both halves of #7's both-manifests-must-agree
        rule hold for a session too.

        The *chain* needs nothing further here, and that is worth saying out loud so nobody
        adds it: ``Delegator._resolve_parent`` already refuses to trust a supplied
        ``parent_task_id`` once it knows who the sender is (steward #67). It derives the
        parent from the tasks that sender is actually holding, and honours a supplied id
        only when it is one of them. Binding the sender is therefore the whole fix — a
        second derivation here would only turn a swept task row into a 404 where #67
        correctly falls back to the chain the sender is really in.
        """
        principal = session_of(request)
        if principal is not None:
            if body.sender is not None and body.sender != principal.resident_id:
                _refuse(
                    403,
                    "sender_not_the_caller",
                    f"this credential belongs to {principal.resident_id!r}, which cannot "
                    f"hand work over as {body.sender!r}; omit `from` and steward fills it in",
                )
            sender_id: str | None = principal.resident_id
        else:
            sender_id = body.sender
        result = validate_path(residents_dir, settings.skills_dir)
        sender = _find_resident(result, sender_id, residents_dir) if sender_id is not None else None
        delegator = dg.Delegator(residents=result.residents, store=db, emitter=sink)
        handoff = dg.Handoff(
            raw="POST /delegate",
            to=body.to,
            route=body.route,
            title=body.title,
            detail=body.detail,
        )
        try:
            task = delegator.delegate(
                sender=sender, handoff=handoff, parent_task_id=body.parent_task_id
            )
        except dg.DelegationError as exc:
            # Refused: nothing was written and nothing was emitted. The reason is the
            # error code, so a session or a panel can key on it without reading prose.
            _refuse(
                DELEGATION_STATUS.get(exc.reason, DELEGATION_REFUSED_STATUS), exc.reason, str(exc)
            )
        request_id = accept(request, "delegated", {"task_id": task.task_id, "to": task.assignee})
        return {
            "request_id": request_id,
            "task_id": task.task_id,
            "status": "accepted",
            "to": task.assignee,
            "route": task.route,
            "depth": task.depth,
            "parent_task_id": task.parent_task_id,
            "origin": task.origin,
            "message": (
                "delivered into the receiver's inbox; it is worked on that resident's own "
                "next wake-up, and task_claimed in burrow's log is the only proof of that"
            ),
        }

    @app.get("/tasks/{task_id}/lineage")
    def get_lineage(task_id: str) -> dict[str, Any]:
        """Return the whole chain this task belongs to, root first. The audit query.

        ``chain`` is the root and everything delegated out of it, depth-first, so the
        answer does not depend on which member of the chain was named (steward #202).
        ``origin`` and ``depth`` still describe the task that was asked about.
        """
        chain = db.lineage(task_id)
        if not chain:
            _refuse(404, "unknown_task", f"no task {task_id!r}")
        asked = next((item for item in chain if item.task_id == task_id), chain[0])
        return {
            "task_id": task_id,
            "origin": asked.origin,
            "depth": asked.depth,
            "chain": [item.to_dict() for item in chain],
        }

    # -- approvals -------------------------------------------------------------------

    @app.get("/approvals")
    def list_approvals(status: str | None = None) -> dict[str, Any]:
        """List gated actions. Pending by default; ``?status=resolved|all`` for the rest.

        The default stays ``pending`` so a panel that has always called this keeps seeing
        exactly what it saw before. ``all`` is the audit view: request and decision in one
        row, which is how "what did I approve, and when" gets answered.

        This route is a pure ledger read. The app lifespan owns deadline expiry, so an
        API-only steward still resolves overdue requests without polling causing writes.
        """
        wanted = status or APPROVAL_STATUS_PENDING
        if wanted not in APPROVAL_STATUSES:
            _refuse(
                422,
                "unknown_status",
                f"status {status!r} is not an approval status; use one of: "
                f"{', '.join(APPROVAL_STATUSES)}",
            )
        records = db.approvals(None if wanted == APPROVAL_STATUS_ALL else wanted)
        if wanted == APPROVAL_STATUS_PENDING:
            moment = ev.utc_now_iso(now())
            records = [r for r in records if r.expires_at is None or r.expires_at > moment]
        return {
            "status": wanted,
            "approvals": [redact_decision(record).to_dict() for record in records],
        }

    @app.get("/approvals/{request_id}")
    def get_approval(request_id: str) -> dict[str, Any]:
        """Return one request with its decision, decider, and timestamps. The audit query."""
        record = db.approval(request_id)
        if record is None:
            _refuse(404, "unknown_approval", f"no approval request {request_id!r}")
        return redact_decision(record).to_dict()

    @app.post("/approvals/{request_id}")
    def decide_approval(
        request_id: str, body: ApprovalDecision, request: Request, response: Response
    ) -> dict[str, Any]:
        """Record a decision, once. A replay reads back what was already recorded.

        The edit is un-redacted before it is recorded, by the same route that redacted the
        copy the decider read (steward #144). Restoring here rather than deeper down is
        deliberate: an edit only carries a marker because *this* route withheld the value,
        so whoever withheld it owes the restore, and a decider that was never served a
        scrubbed detail has nothing to put back.
        """
        ledger_id = new_id()
        moment = now()
        edit = body.edit
        stored = db.approval(request_id)
        if stored is not None and edit is not None:
            try:
                edit = restore_withheld(edit, stored.detail)
            except WithheldValueError as exc:
                _refuse(422, "edit_withheld_value", str(exc))
        decided = approvals.decide(
            request_id,
            body.decision,
            decided_by=acted_by(request),
            edit=edit,
            now=moment,
            request_log=(ledger_id, request.method, request.url.path),
        )
        record = decided.record
        if record is None:
            _refuse(404, "unknown_approval", f"no approval request {request_id!r}")
        if decided.expired:
            # Deny-by-default has the last word — a click a minute past the deadline must
            # not slip an action through ahead of the sweep, which denies it and closes the
            # loop in the log (steward #66). Do that sweep here too: an API-only steward
            # must not leave the refused request pending forever.
            approvals.expire(moment)
            _refuse(
                409,
                "approval_expired",
                f"approval request {request_id!r} expired at {record.expires_at} and denies "
                f"by default; it can no longer be decided",
            )
        if decided.refused:
            raise HTTPException(
                status_code=409,
                detail={
                    "error": "approval_decision_not_offered",
                    "message": f"decision {body.decision!r} was not offered for this approval; "
                    f"use one of: {', '.join(record.options)}",
                    "offered": list(record.options),
                },
            )
        state = db.approval_announcement_state(request_id)
        if decided.replayed and state != "pending":
            # The first decision won. A double-tapped notification changes nothing and
            # emitted nothing — it is told what was recorded.
            response.status_code = 200
            return {
                "request_id": ledger_id,
                "approval_request_id": request_id,
                "status": "recorded",
                "decision": record.decision,
                "decided_by": record.decided_by,
                "decided_at": record.decided_at,
                "message": "this request was already decided; nothing changed",
            }
        # A replay that recovered an abandoned announcement must also finish the
        # idempotent workflow below (notably budget unpause). The decision did not change,
        # but this request completed work the dead first process did not.
        announced = state in {"announced", "complete"}
        outcome = "recorded" if announced else "recorded_announcement_pending"
        outbox.notify()
        response.status_code = 202
        resumed = None
        if announced:
            claimed = db.claim_approval_effects(request_id)
            if claimed is not None:
                effect_record, token = claimed
                completed, resumed = db.complete_approval_effects(effect_record, token)
                if not completed:
                    db.release_approval_effects(request_id, token)
        return {
            "request_id": ledger_id,
            "approval_request_id": request_id,
            "status": outcome,
            "decision": record.decision,
            "decided_by": record.decided_by,
            "decided_at": record.decided_at,
            "resumed": resumed,
            "message": (
                f"recorded; {resumed} is no longer paused and fires on its next schedule"
                if resumed
                else (
                    "recorded; the resident acts on it when the blocked session reads it "
                    "or the parked work resumes on its next wake-up"
                    if announced
                    else "recorded; announcement pending and completion side effects are deferred"
                )
            ),
        }

    # -- the request log -------------------------------------------------------------

    @app.get("/requests")
    def list_requests(limit: int = REQUESTS_DEFAULT_LIMIT) -> dict[str, Any]:
        """Return accepted requests and what became of them, newest first.

        The endpoint that makes "accepted" survivable as an answer. Every mutating call
        here returns a ``request_id`` and refuses to claim an effect; this is where the
        effect eventually shows up — ``queued`` becomes ``ran``, ``skipped: …``, or
        ``failed`` when the run it stands for finishes. A control panel polls one of these
        rather than deciding on its own that a 202 went well.
        """
        window = max(1, min(limit, REQUESTS_MAX_LIMIT))
        rows = db.requests()[-window:]
        return {"requests": [record.to_dict() for record in reversed(rows)]}

    @app.get("/requests/{request_id}")
    def get_request(request_id: str) -> dict[str, Any]:
        """Return one accepted request and its outcome. ``404`` for an id nobody logged."""
        record = db.request(request_id)
        if record is None:
            _refuse(
                404,
                "unknown_request",
                f"no request {request_id!r}; only accepted mutating requests are logged, "
                f"so a refused one has no id to look up",
            )
        return record.to_dict()

    return app


def run_server(
    app: FastAPI,
    *,
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
    log_level: str = "info",
) -> None:  # pragma: no cover — the blocking call itself is not unit-testable
    """Serve the app. The default bind is loopback; deployment binds the tailnet address."""
    import uvicorn  # noqa: PLC0415 — importing a server at call time keeps the CLI fast

    uvicorn.run(app, host=host, port=port, log_level=log_level)


def origins_summary(origins: Sequence[str]) -> str:
    """Describe the configured CORS origins for a startup line."""
    return ", ".join(origins) if origins else "none (same-origin only)"
