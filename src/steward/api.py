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

The one exception is ``/ui``: the management console's own HTML, CSS, and JavaScript
are served unauthenticated, because the browser has to load the script *before* there
is anything to ask a human for a token with. Those three files contain no fleet data —
every byte the console displays it fetches from the endpoints above, with the token.
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
from datetime import UTC, datetime
from hmac import compare_digest
from pathlib import Path
from typing import Annotated, Any, Literal, NoReturn

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field, field_validator
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from steward import delegation as dg
from steward import events as ev
from steward.board import Dispatcher
from steward.budgets import PAUSED_ERROR, BudgetGuard, BudgetStatus
from steward.deploy import Transport
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
    NewResident,
    NurseryError,
    NurseryReport,
    raise_resident,
)
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
from steward.sessions import RunnerFactory
from steward.skills import SkillLibrary, effective_skills, library_for
from steward.store import (
    JOB_STATUSES,
    STATUS_OPEN,
    ApprovalRecord,
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
    "ResidentPost",
    "create_app",
    "default_ui_dir",
    "run_server",
]

#: How the API reaches the nursery. Injectable so a test can prove the endpoint and
#: ``steward new-resident`` run *the same* pipeline rather than two that agree by
#: convention — hand it a recorder and assert on what the route asked for.
type NurseryPipeline = Callable[..., NurseryReport]

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
UI_ENV = "STEWARD_UI"

#: The management console's mount point, and the file that must exist for a directory to
#: count as one. Serving a directory without it would hand the browser a 404 shaped like
#: a working console.
UI_MOUNT = "/ui"
UI_INDEX = "index.html"

#: How many rows ``GET /requests`` will hand back at most, and how many it hands back
#: when nobody asks. The console polls this to confirm what a 202 only accepted.
REQUESTS_DEFAULT_LIMIT = 50
REQUESTS_MAX_LIMIT = 500

POSTED_BY = "api"
DECIDED_BY = "api"

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
    #: The management console's static files. ``None`` looks for ``ui/`` in the checkout;
    #: a directory with no ``index.html`` in it is not served at all.
    ui_dir: Path | None = None
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
        ui_dir: Path | str | None = None,
    ) -> ApiConfig:
        """Read the token, the CORS origins, and the residents tree from the environment."""
        source = os.environ if env is None else env
        configured_residents = residents_dir or (source.get(RESIDENTS_ENV) or "").strip()
        configured_ui = ui_dir or (source.get(UI_ENV) or "").strip()
        return cls(
            residents_dir=Path(configured_residents) if configured_residents else Path("residents"),
            db_path=Path(db_path) if db_path is not None else None,
            token=source.get(TOKEN_ENV),
            allow_open=allow_open,
            cors_origins=parse_origins(source.get(CORS_ENV)),
            workdir=Path(workdir) if workdir is not None else None,
            skills_dir=Path(skills_dir) if skills_dir is not None else None,
            ui_dir=Path(configured_ui) if configured_ui else None,
        )


def parse_origins(raw: str | None) -> tuple[str, ...]:
    """Split ``STEWARD_CORS_ORIGINS`` into origins. Unset means no origin is allowed."""
    return tuple(part.strip() for part in (raw or "").split(",") if part.strip())


def default_ui_dir() -> Path | None:
    """Find the management console's static files, or ``None`` if this install has none.

    Looked for beside the package first, so a future wheel that ships the console wins,
    then at the root of the checkout, which is where ``ui/`` lives in this repo. A
    directory without an ``index.html`` does not count: mounting one would answer ``/ui``
    with a 404 shaped like a working console.
    """
    here = Path(__file__).resolve()
    for candidate in (here.parent / "ui", here.parents[2] / "ui"):
        if (candidate / UI_INDEX).is_file():
            return candidate
    return None


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


class AlreadyRunningError(Exception):
    """Raised when a routine is asked to run while its previous run is still going."""


@dataclass(slots=True)
class ManualRuns:
    """Fires routines on request, through the scheduler's own fire path.

    Manual runs go through :meth:`steward.scheduler.Scheduler.fire` rather than a
    second implementation, so a run-now gets the same bracketing events, the same
    prompt assembly, the same timeout, and the same runner seam as a scheduled fire.
    Only the ``trigger`` differs — ``manual`` instead of ``schedule`` — so the ledger
    can tell work steward decided to do from work a human asked for (steward #23).

    The scheduler's other promise carries over too: one run per routine at a time. A
    second run-now while the first is still going is refused with a 409 rather than
    queued, because a queue would let the village show an hourly routine as a backlog.
    """

    scheduler: Scheduler
    store: Store
    max_workers: int = 4
    _pool: ThreadPoolExecutor = field(init=False)
    _inflight: set[str] = field(default_factory=set, init=False)
    _futures: list[Future[None]] = field(default_factory=list, init=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, init=False)

    def __post_init__(self) -> None:
        """Open the small pool manual runs execute on."""
        self._pool = ThreadPoolExecutor(max_workers=self.max_workers, thread_name_prefix="run-now")

    def submit(self, item: ScheduledRoutine, request_id: str) -> None:
        """Queue exactly one run of ``item``, or refuse because one is already going."""
        with self._lock:
            if item.key in self._inflight:
                raise AlreadyRunningError(item.key)
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
    :mod:`steward.prompt` injects. It is already parsed and in memory here, and a console
    that showed a resident's charter but not the style it writes in would be showing half
    of who it is. ``None`` means the soul declares no voice, which is a real answer.
    """
    manifest = resident.manifest
    resolved = effective_skills(manifest, library) if library is not None else ()
    return {
        "id": manifest.id,
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
        # Which brain, answerable without opening a file.
        "runner": {"kind": manifest.runner.kind, "model": manifest.runner.model},
        # Whether this resident takes work off the board, and on what terms.
        "board": manifest.board.model_dump(mode="json"),
        # And whether it may hand work to anybody else, and to whom.
        "delegation": manifest.delegation.model_dump(mode="json"),
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


def _refuse_if_retired(resident: Resident) -> None:
    """Refuse to give work to a retired resident, with the reason every path shares."""
    complaint = retired_complaint(resident)
    if complaint is not None:
        _refuse(409, "resident_retired", complaint)


def _find_resident(result: ValidationResult, resident_id: str, residents_dir: Path) -> Resident:
    for resident in result.residents:
        if resident.id == resident_id:
            return resident
    if (residents_dir / resident_id).is_dir():
        # The resident exists but did not validate. Saying "unknown" would send someone
        # looking for a missing directory instead of a broken manifest.
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


def _auth_dependency(token: str | None) -> Callable[[Request], None]:
    """Build the bearer-token gate every endpoint hangs off."""

    def require_token(request: Request) -> None:
        if not _authorized(request.scope.get("headers", []), token):
            raise HTTPException(
                status_code=401,
                detail={
                    "error": "unauthorized",
                    "message": f"this endpoint needs Authorization: Bearer <{TOKEN_ENV}>",
                },
                headers={"WWW-Authenticate": "Bearer"},
            )

    return require_token


def _authorized(headers: Sequence[tuple[bytes, bytes]], token: str | None) -> bool:
    """Apply the API's one bearer policy to raw ASGI headers.

    Exactly one Authorization field is accepted.  Rejecting duplicates avoids proxy and
    framework disagreement over first/last/comma-joined semantics.  All presented bearer
    tokens reach the same constant-time comparison used by the route dependency.
    """
    if token is None:
        return True
    values = [value for key, value in headers if key.lower() == b"authorization"]
    if len(values) != 1:
        return False
    scheme, separator, presented = values[0].partition(b" ")
    return (
        separator == b" "
        and scheme.lower() == b"bearer"
        and compare_digest(presented.strip(), token.encode("utf-8"))
    )


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
        if not is_decision or not _authorized(scope.get("headers", []), self.token):
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
    hooks = Dispatcher.from_path(
        residents_dir,
        db,
        emitter=sink,
        workdir=settings.workdir,
        runner_factory=runner_factory,
        library=library,
        guard=guard,
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
        ),
        store=db,
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
        dependencies=[Depends(_auth_dependency(token))],
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
        return {
            "residents": [
                {
                    **resident_view(resident, library),
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
        return resident_view(_find_resident(result, resident_id, residents_dir), library)

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
            **Never.** The server is not guaranteed to own the checkout it is reading —
            it may be a tailnet process on a machine where nobody is watching git — and a
            commit appearing there is a commit that surprises somebody. The response says
            so, so a panel can tell the human what is still theirs to do.
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
        request_id = accept(
            request, "deployed" if body.deploy else "declared", {"resident": body.id}
        )
        uncommitted = (
            "the declaration is written but NOT committed — steward does not commit from "
            "the server, because it may not own this checkout; commit "
            f"residents/{body.id}/ yourself"
        )
        if not body.deploy:
            deployed = "nothing is deployed and no routine is scheduled: this is a file for review"
        elif report.register is not None and not report.register.ok:
            # The container went up; the check that follows it did not pass. Saying only
            # the first half would be the console's one unforgivable sin, so say both and
            # let `register.problems` carry the detail.
            deployed = (
                "the container is up, but the schedule check did not pass — see "
                "register.problems; nothing fires until those are fixed"
            )
        else:
            deployed = (
                "the container is up and the schedule was checked; the resident appears in "
                "the village when it emits its own first event, and never before"
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
        }

    # -- skills ----------------------------------------------------------------------

    @app.get("/skills")
    def list_skills() -> dict[str, Any]:
        """List the skills library, and who holds each skill.

        Read-only, like the rest of the views: a skill is added by committing a
        ``SKILL.md`` and granted by committing a manifest, never over HTTP.
        """
        result = validate_path(residents_dir, settings.skills_dir)
        holders: dict[str, list[str]] = {skill.name: [] for skill in library}
        for resident in result.residents:
            for skill in effective_skills(resident.manifest, library):
                holders[skill.name].append(resident.id)
        return {
            "library": str(library.path) if library.path is not None else None,
            "skills": [{**skill.as_dict(), "holders": holders[skill.name]} for skill in library],
            "errors": [diagnostic.render() for diagnostic in library.diagnostics],
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
        resident_retired``, which is what the console reads to grey the button out.

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
        """
        result = validate_path(residents_dir, settings.skills_dir)
        state = SchedulerState.load(default_state_path())
        now = datetime.now(UTC)
        latest = latest_run_requests(db.requests())
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
        except AlreadyRunningError:
            db.set_request_outcome(request_id, "refused: already running")
            _refuse(
                409,
                "already_running",
                f"a run of {item.key} is still going; steward skips an overlapping fire "
                f"rather than queueing one, so ask again when it has finished",
            )
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
            posted_by=POSTED_BY,
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
        """
        result = validate_path(residents_dir, settings.skills_dir)
        sender = (
            _find_resident(result, body.sender, residents_dir) if body.sender is not None else None
        )
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
        return {"status": wanted, "approvals": [record.to_dict() for record in records]}

    @app.get("/approvals/{request_id}")
    def get_approval(request_id: str) -> dict[str, Any]:
        """Return one request with its decision, decider, and timestamps. The audit query."""
        record = db.approval(request_id)
        if record is None:
            _refuse(404, "unknown_approval", f"no approval request {request_id!r}")
        return record.to_dict()

    @app.post("/approvals/{request_id}")
    def decide_approval(
        request_id: str, body: ApprovalDecision, request: Request, response: Response
    ) -> dict[str, Any]:
        """Record a decision, once. A replay reads back what was already recorded."""
        ledger_id = new_id()
        moment = now()
        decided = approvals.decide(
            request_id,
            body.decision,
            decided_by=DECIDED_BY,
            edit=body.edit,
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
        ``failed`` when the run it stands for finishes. A console polls one of these
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

    # -- the management console ------------------------------------------------------

    ui_dir = settings.ui_dir if settings.ui_dir is not None else default_ui_dir()
    app.state.ui_dir = ui_dir if ui_dir is not None and (ui_dir / UI_INDEX).is_file() else None
    if app.state.ui_dir is not None:
        # Deliberately outside the token gate: a mount is a plain ASGI app, so the
        # dependency above does not reach it, and it must not — the browser has to load
        # the script before there is anything to ask a human for a token with. What is
        # served here is three static files with no fleet data in them; everything the
        # console displays it fetches from the endpoints above, with the token.
        app.mount(UI_MOUNT, StaticFiles(directory=app.state.ui_dir, html=True), name="ui")

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
