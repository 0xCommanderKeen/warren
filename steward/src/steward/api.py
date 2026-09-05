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
from contextlib import AbstractAsyncContextManager, asynccontextmanager, suppress
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from hmac import compare_digest
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.security import HTTPBearer

from steward import authoring as au
from steward import events as ev
from steward.board import Dispatcher
from steward.budgets import BudgetGuard
from steward.chat import RoutineDelivery
from steward.claims import ONE_SESSION_PER_RESIDENT, ResidentClaims
from steward.deploy import Transport
from steward.manifest import SessionGrant, validate_manifest
from steward.nursery import (
    CommitIdentity,
    NurseryReport,
    RetireReport,
    provision_resident,
    raise_resident,
    retire_resident,
)
from steward.operator_auth import OperatorPrincipal
from steward.routes import approvals as approval_routes
from steward.routes import board as board_routes
from steward.routes import delegation as delegation_routes
from steward.routes import deps as route_deps
from steward.routes import org as org_routes
from steward.routes import reload as reload_routes
from steward.routes import requests as request_routes
from steward.routes import residents as resident_routes
from steward.routes import routines as routine_routes
from steward.routes import secrets as secret_routes
from steward.routes import skills as skill_routes
from steward.routes.auth import (
    _ApprovalBodyDepthMiddleware,
    _auth_dependency,
    session_of,  # noqa: F401 — compatibility re-export
)
from steward.routes.deps import Deps
from steward.routes.residents import (
    ResidentPost,
)
from steward.routes.routines import last_run_view
from steward.run_lifecycle import RUN_LEASE_GRACE_S
from steward.runners import build_runner
from steward.runs import AlreadyRunningError
from steward.scheduler import (
    TRIGGER_MANUAL,
    FireReport,
    ScheduledRoutine,
    Scheduler,
    SchedulerState,
    default_state_path,
)
from steward.session_auth import SessionPrincipal
from steward.sessions import RunnerFactory
from steward.skills import library_for
from steward.store import (
    ApprovalRecord,
    Store,
    default_db_path,
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
    "RetirePipeline",
    "create_app",
    "last_run_view",
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

#: And how it reaches the door back out again. Same seam, same reason: retirement is a
#: mark, a commit, a ``docker compose down`` and two removals in one order, and a route
#: with its own copy of that order would be a second place for it to be wrong.
type RetirePipeline = Callable[..., RetireReport]

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
PUSH_BRANCH_ENV = "STEWARD_PUSH_BRANCH"
PUSH_REMOTE_ENV = "STEWARD_PUSH_REMOTE"
DEFAULT_PUSH_REMOTE = "origin"

API_PRINCIPAL = route_deps.API_PRINCIPAL
WRITE_STATUS = route_deps.WRITE_STATUS
REQUESTS_DEFAULT_LIMIT = request_routes.REQUESTS_DEFAULT_LIMIT
REQUESTS_MAX_LIMIT = request_routes.REQUESTS_MAX_LIMIT
ACTED_BY_API = route_deps.ACTED_BY_API
POSTED_BY = ACTED_BY_API
DECIDED_BY = ACTED_BY_API
APPROVAL_STATUS_PENDING = approval_routes.APPROVAL_STATUS_PENDING
APPROVAL_STATUS_ALL = approval_routes.APPROVAL_STATUS_ALL
APPROVAL_STATUSES = approval_routes.APPROVAL_STATUSES

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
    #: Where every commit the write API makes is pushed afterwards (warren#351). ``None``
    #: pushes nowhere — a laptop's checkout, where the person pushes. A burrow sets it to
    #: its own branch so the history it is authoritative for exists somewhere that is not
    #: one disk on a NAS. The push is best effort and never fails a write.
    push: au.PushTarget | None = None
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
            push=parse_push(source.get(PUSH_BRANCH_ENV), source.get(PUSH_REMOTE_ENV)),
        )


#: The one path whose 422 may not carry the body that caused it (warren#462). FastAPI's
#: default handler reports a validation failure by echoing the offending ``input``, which is
#: right everywhere else in this API and is a disclosure here: the body of a secret write
#: *is* the credential, and a mistyped key or a wrong-typed value would otherwise put it in
#: a response — and from there into whatever logged the response. The field bounds moved
#: into ``steward.secrets.write_secret`` for the same reason; this closes the half that
#: never reaches a route at all.
SECRET_WRITE_PREFIX = "/secrets/"  # noqa: S105 — a URL prefix, not a credential


async def _validation_handler(request: Request, exc: Exception) -> JSONResponse:
    """Answer a body that did not validate — without quoting a credential back at anybody."""
    errors = exc.errors() if isinstance(exc, RequestValidationError) else []
    if not request.url.path.startswith(SECRET_WRITE_PREFIX):
        return JSONResponse(status_code=422, content={"detail": jsonable_encoder(errors)})
    return JSONResponse(
        status_code=422,
        content={
            "detail": {
                "error": "invalid_secret_body",
                "message": (
                    'the body of a secret write is exactly {"value": "<the credential>"}; '
                    "what was sent did not match, and steward will not quote it back. "
                    + "; ".join(sorted({str(error.get("msg", "")) for error in errors}))
                ).strip(),
            }
        },
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


def parse_push(branch: str | None, remote: str | None) -> au.PushTarget | None:
    """Read where commits go from the environment, or ``None`` for nowhere.

    The branch is the switch: a remote alone names nowhere to push to, and ``origin`` is
    what a checkout made by ``git clone`` calls its remote, so it is the default.
    """
    target_branch = (branch or "").strip()
    if not target_branch:
        return None
    return au.PushTarget(remote=(remote or "").strip() or DEFAULT_PUSH_REMOTE, branch=target_branch)


def _flag(raw: str | None) -> bool:
    """Read an environment switch. Only an explicit yes counts as one."""
    return (raw or "").strip().lower() in {"1", "true", "yes", "on"}


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
    if report.ok:
        return "ran", detail
    detail["error"] = report.terminal_error or (
        report.result.summary() if report.result is not None else "no result"
    )
    return "failed", detail


# --------------------------------------------------------------------------------------
# views
# --------------------------------------------------------------------------------------


# --------------------------------------------------------------------------------------
# the app
# --------------------------------------------------------------------------------------


def _principal_lookups(
    db: Store, now: Callable[[], datetime]
) -> tuple[Callable[[str], SessionPrincipal | None], Callable[[str], OperatorPrincipal | None]]:
    """Build the fail-closed credential lookups used by the auth gate."""

    def session_principal(credential: str) -> SessionPrincipal | None:
        fresh_since = ev.utc_now_iso(now() - timedelta(seconds=RUN_LEASE_GRACE_S))
        try:
            return db.session_principal(credential, fresh_since=fresh_since)
        except Exception:
            log.exception("could not check a presented session credential")
            return None

    def operator_principal(credential: str) -> OperatorPrincipal | None:
        try:
            return db.operator_principal(credential)
        except Exception:
            log.exception("could not check a presented operator credential")
            return None

    return session_principal, operator_principal


def _lifespan_for(
    outbox: ApprovalOutboxWorker,
    approvals: ApprovalTransitions,
    now: Callable[[], datetime],
    approval_expiry_interval_s: float,
) -> Callable[[FastAPI], AbstractAsyncContextManager[None]]:
    """Build the app lifespan that owns approval announcement and expiry workers."""

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        outbox.start()
        stopped = asyncio.Event()

        async def expire_approvals() -> None:
            while not stopped.is_set():
                try:
                    await asyncio.to_thread(approvals.expire, now())
                except Exception:
                    log.exception("approval expiry sweep failed; will retry")
                with suppress(TimeoutError):
                    await asyncio.wait_for(stopped.wait(), timeout=approval_expiry_interval_s)

        worker = asyncio.create_task(expire_approvals(), name="steward-approval-expiry")
        app.state.approval_expiry_task = worker
        try:
            yield
        finally:
            stopped.set()
            await worker
            app.state.approval_expiry_task = None
            outbox.close()

    return lifespan


def create_app(  # noqa: PLR0913 — injectable collaborators are the public test seams
    config: ApiConfig | None = None,
    *,
    store: Store | None = None,
    emitter: ev.Emitter | None = None,
    runner_factory: RunnerFactory = build_runner,
    nursery: NurseryPipeline = raise_resident,
    provisioner: ProvisionPipeline = provision_resident,
    retirer: RetirePipeline = retire_resident,
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
    library = library_for(residents_dir, settings.skills_dir)

    db = store if store is not None else Store(settings.db_path or default_db_path())
    sink: ev.Emitter = emitter if emitter is not None else ev.EventEmitter.from_env()

    tasks = TaskTransitions(store=db, emitter=sink)
    approvals = ApprovalTransitions(store=db, emitter=sink)
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
            # A run-now of a routine that says ``deliver:`` delivers exactly as a scheduled
            # fire would (warren#385); the API process holds the same tokens.
            deliverer=RoutineDelivery.from_env(),
        ),
        store=db,
        claims=claims,
    )

    session_principal, operator_principal = _principal_lookups(db, now)

    def session_grants(resident_id: str) -> tuple[SessionGrant, ...]:
        """Read the named resident's current declaration at the write door."""
        result = validate_manifest(
            residents_dir / resident_id / "manifest.yaml", settings.skills_dir
        )
        if not result.ok or not result.residents:
            return ()
        return tuple(result.residents[0].manifest.session_grants)

    lifespan = _lifespan_for(outbox, approvals, now, approval_expiry_interval_s)
    app = FastAPI(
        title="steward",
        summary="The token-gated write path into the agent fleet burrow watches.",
        version="0",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        dependencies=[
            Depends(HTTPBearer(auto_error=False)),
            Depends(
                _auth_dependency(
                    token,
                    session_principal,
                    operator_principal,
                    session_grants,
                    compare_digest,
                )
            ),
        ],
        responses={
            401: {
                "description": "No credential was presented, or steward refused the one that was."
            }
        },
        lifespan=lifespan,
    )
    app.add_exception_handler(RequestValidationError, _validation_handler)
    app.add_middleware(_ApprovalBodyDepthMiddleware, token=token, compare_token=compare_digest)
    if settings.cors_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=list(settings.cors_origins),
            allow_credentials=False,
            allow_methods=["GET", "POST", "PUT", "OPTIONS"],
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

    deps = Deps(
        settings=settings,
        db=db,
        sink=sink,
        residents_dir=residents_dir,
        now=now,
        nursery=nursery,
        provisioner=provisioner,
        retirer=retirer,
        transport=transport,
        tasks=tasks,
        approvals=approvals,
        guard=guard,
        outbox=outbox,
        runs=runs,
        hooks=hooks,
        claims=claims,
        runner_factory=runner_factory,
    )
    app.include_router(request_routes.router(deps))
    app.include_router(board_routes.router(deps))
    app.include_router(approval_routes.router(deps))
    app.include_router(reload_routes.router(deps))
    app.include_router(routine_routes.router(deps))
    app.include_router(delegation_routes.router(deps))
    app.include_router(org_routes.router(deps))

    app.include_router(secret_routes.router(deps))
    app.include_router(resident_routes.router(deps))
    app.include_router(skill_routes.router(deps))

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
