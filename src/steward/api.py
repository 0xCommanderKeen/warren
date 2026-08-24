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
"""

import logging
import os
import threading
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import Future, ThreadPoolExecutor
from concurrent.futures import wait as wait_for_futures
from dataclasses import dataclass, field
from hmac import compare_digest
from pathlib import Path
from typing import Any, Literal, NoReturn

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, ConfigDict, Field

from steward import events as ev
from steward.journal import journal_complaint, read_entries
from steward.manifest import Resident, ValidationResult, validate_path
from steward.nursery import CreatedResident, NewResident, NurseryError, declare_resident
from steward.runners import build_runner
from steward.scheduler import (
    TRIGGER_MANUAL,
    FireReport,
    RunnerFactory,
    ScheduledRoutine,
    Scheduler,
    SchedulerState,
    default_state_path,
)
from steward.skills import SkillLibrary, effective_skills, library_for
from steward.store import JOB_STATUSES, Store, default_db_path, new_id

__all__ = [
    "DEFAULT_HOST",
    "DEFAULT_PORT",
    "ApiConfig",
    "ApiError",
    "ManualRuns",
    "create_app",
    "run_server",
]

log = logging.getLogger("steward.api")

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8801

TOKEN_ENV = "STEWARD_TOKEN"  # noqa: S105 — an env var name, not a credential
CORS_ENV = "STEWARD_CORS_ORIGINS"
RESIDENTS_ENV = "STEWARD_RESIDENTS"

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
        )


def parse_origins(raw: str | None) -> tuple[str, ...]:
    """Split ``STEWARD_CORS_ORIGINS`` into origins. Unset means no origin is allowed."""
    return tuple(part.strip() for part in (raw or "").split(",") if part.strip())


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

    title: str = Field(min_length=1, description="One line naming the work.")
    detail: str = Field(default="", description="Everything the claimant needs to know.")
    required_skills: list[str] = Field(
        default_factory=list,
        description="Skills a resident must be granted before it may claim this.",
    )


class ApprovalDecision(_Body):
    """A human's answer to a gated action."""

    decision: Literal["approve", "deny", "edit"]
    edit: dict[str, Any] | None = Field(
        default=None, description="The modified detail, for decision=edit."
    )


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


def resident_view(resident: Resident, library: SkillLibrary | None = None) -> dict[str, Any]:
    """Return the JSON view of one validated manifest.

    Safe to serve wholesale: a manifest that contained a credential-shaped key or an
    inline secret would have failed validation and never become a ``Resident`` at all,
    so there is nothing here to redact.

    ``effective_skills`` is what a session for this resident is actually given — the
    library's defaults plus this manifest's grants — so the panel can show the set
    without re-deriving it from two places.
    """
    manifest = resident.manifest
    resolved = effective_skills(manifest, library) if library is not None else ()
    return {
        "id": manifest.id,
        "agent_id": manifest.agent_id,
        "project": manifest.project,
        "summary": manifest.summary,
        "path": str(resident.path),
        "soul": manifest.soul.model_dump(mode="json"),
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
        if token is None:
            return
        scheme, _, presented = request.headers.get("Authorization", "").partition(" ")
        if scheme.lower() != "bearer" or not compare_digest(
            presented.strip().encode("utf-8"), token.encode("utf-8")
        ):
            raise HTTPException(
                status_code=401,
                detail={
                    "error": "unauthorized",
                    "message": f"this endpoint needs Authorization: Bearer <{TOKEN_ENV}>",
                },
                headers={"WWW-Authenticate": "Bearer"},
            )

    return require_token


def create_app(  # noqa: C901, PLR0915 — the routes are flat; the length is the endpoint list
    config: ApiConfig | None = None,
    *,
    store: Store | None = None,
    emitter: ev.Emitter | None = None,
    runner_factory: RunnerFactory = build_runner,
) -> FastAPI:
    """Build the API. Raises :class:`ApiError` rather than serving without a token.

    Collaborators are injectable so tests exercise the real routes with a mock runner,
    a scratch database, and an emitter that writes to a file instead of a village.
    """
    settings = config if config is not None else ApiConfig.from_env()
    token = resolve_token(settings.token, allow_open=settings.allow_open)
    residents_dir = Path(settings.residents_dir)
    # Read once at startup, like the residents tree's souls: a skill edited on disk
    # lands on the next restart, and the library is one object the whole app shares.
    library = library_for(residents_dir, settings.skills_dir)

    db = store if store is not None else Store(settings.db_path or default_db_path())
    sink: ev.Emitter = emitter if emitter is not None else ev.EventEmitter.from_env()
    runs = ManualRuns(
        scheduler=Scheduler(
            [],
            emitter=sink,
            state=SchedulerState(path=default_state_path()),
            workdir=settings.workdir,
            runner_factory=runner_factory,
            library=library,
        ),
        store=db,
    )

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
    )
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
    app.state.residents_dir = residents_dir
    app.state.library = library
    app.state.open_mode = token is None

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
            "residents": [resident_view(resident, library) for resident in result.residents],
            "errors": [diagnostic.render() for diagnostic in result.errors],
        }

    @app.get("/residents/{resident_id}")
    def get_resident(resident_id: str) -> dict[str, Any]:
        """Return one validated manifest, runner included, so "which brain" is answerable."""
        result = validate_path(residents_dir, settings.skills_dir)
        return resident_view(_find_resident(result, resident_id, residents_dir), library)

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
    def create_resident(spec: NewResident, request: Request) -> dict[str, Any]:
        """Write a manifest skeleton and soul body for review. Deploys nothing."""
        try:
            created: CreatedResident = declare_resident(spec, residents_dir)
        except NurseryError as exc:
            status = 409 if (residents_dir / spec.id).exists() else 400
            _refuse(status, "resident_not_declared", str(exc))
        request_id = accept(request, "declared", {"resident": created.id})
        return {
            "request_id": request_id,
            "status": "accepted",
            "message": (
                "declaration written for review; nothing is deployed and no routine is "
                "scheduled until someone commits it and steward provisions the resident"
            ),
            **created.to_dict(),
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

    # -- run now ---------------------------------------------------------------------

    @app.post("/residents/{resident_id}/routines/{routine_id}/run", status_code=202)
    def run_routine(resident_id: str, routine_id: str, request: Request) -> dict[str, Any]:
        """Ask for one run of one routine, right now, and acknowledge only that."""
        result = validate_path(residents_dir, settings.skills_dir)
        resident = _find_resident(result, resident_id, residents_dir)
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
        job = db.post_job(
            title=body.title,
            detail=body.detail,
            required_skills=body.required_skills,
            posted_by=POSTED_BY,
        )
        sink.emit(
            ev.task_posted_event(
                task_id=job.task_id,
                title=job.title,
                required_skills=job.required_skills,
                posted_by=job.posted_by,
            )
        )
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

    # -- approvals -------------------------------------------------------------------

    @app.get("/approvals")
    def list_approvals(status: str | None = None) -> dict[str, Any]:
        """List gated actions. Pending by default; ``?status=resolved|all`` for the rest.

        The default stays ``pending`` so a panel that has always called this keeps seeing
        exactly what it saw before. ``all`` is the audit view: request and decision in one
        row, which is how "what did I approve, and when" gets answered.
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
        record, recorded = db.decide(
            request_id, body.decision, decided_by=DECIDED_BY, edit=body.edit
        )
        if record is None:
            _refuse(404, "unknown_approval", f"no approval request {request_id!r}")
        if not recorded:
            # The first decision won. A double-tapped notification changes nothing and
            # emits nothing — it is told what was recorded.
            response.status_code = 200
            return {
                "request_id": request_id,
                "status": "recorded",
                "decision": record.decision,
                "decided_by": record.decided_by,
                "decided_at": record.decided_at,
                "message": "this request was already decided; nothing changed",
            }
        sink.emit(
            ev.needs_human_resolved_event(
                request_id=record.request_id,
                decision=body.decision,
                action=record.action,
                agent_id=record.agent_id,
                project=record.project,
                decided_by=DECIDED_BY,
            )
        )
        accept(request, "recorded", {"approval": request_id, "decision": body.decision})
        response.status_code = 202
        return {
            "request_id": request_id,
            "status": "recorded",
            "decision": record.decision,
            "decided_by": record.decided_by,
            "decided_at": record.decided_at,
            "message": (
                "recorded; the resident acts on it when the blocked session reads it or "
                "the parked work resumes on its next wake-up"
            ),
        }

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
