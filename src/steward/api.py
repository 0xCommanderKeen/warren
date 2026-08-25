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

import logging
import os
import threading
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import Future, ThreadPoolExecutor
from concurrent.futures import wait as wait_for_futures
from dataclasses import dataclass, field
from datetime import UTC, datetime
from hmac import compare_digest
from pathlib import Path
from typing import Any, Literal, NoReturn

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field

from steward import delegation as dg
from steward import events as ev
from steward.board import Dispatcher
from steward.budgets import BUDGET_ACTION, PAUSED_ERROR, BudgetGuard, BudgetStatus
from steward.deploy import Transport
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
    RunnerFactory,
    ScheduledRoutine,
    Scheduler,
    SchedulerState,
    default_state_path,
)
from steward.skills import SkillLibrary, effective_skills, library_for
from steward.store import (
    JOB_STATUSES,
    STATUS_OPEN,
    RequestRecord,
    Store,
    default_db_path,
    new_id,
)

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

    title: str = Field(min_length=1, description="One line naming the work.")
    detail: str = Field(default="", description="Everything the claimant needs to know.")
    required_skills: list[str] = Field(
        default_factory=list,
        description="Skills a resident must be granted before it may claim this.",
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


class HandoffPost(_Body):
    """Work handed to one named resident, through a route that resident declares."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True, populate_by_name=True)

    to: str = Field(min_length=1, description="The resident id receiving the work.")
    route: str = Field(min_length=1, description="A delegation route that resident declares.")
    title: str = Field(min_length=1, description="One line naming the work.")
    detail: str = Field(default="", description="Everything the receiver needs to know.")
    sender: str | None = Field(
        default=None,
        alias="from",
        description="The resident handing the work over. Omit it when a person is.",
    )
    parent_task_id: str | None = Field(
        default=None,
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
        "uid": str(manifest.uid),
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


def create_app(  # noqa: C901, PLR0913, PLR0915 — flat routes; every collaborator is a seam
    config: ApiConfig | None = None,
    *,
    store: Store | None = None,
    emitter: ev.Emitter | None = None,
    runner_factory: RunnerFactory = build_runner,
    nursery: NurseryPipeline = raise_resident,
    transport: Transport | None = None,
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
    # One guard for the whole app: the run-now path refuses through it before it accepts
    # anything, and the scheduler behind that path ledgers through the same object.
    guard = BudgetGuard(db, sink)
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
    app.state.guard = guard
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

        None of this means anything is firing. Routines fire when
        ``steward scheduler run`` is up; a ledger is a declaration, not a heartbeat.
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
        """Return the whole chain this task belongs to, root first. The audit query."""
        chain = db.lineage(task_id)
        if not chain:
            _refuse(404, "unknown_task", f"no task {task_id!r}")
        return {
            "task_id": task_id,
            "origin": chain[-1].origin,
            "depth": chain[-1].depth,
            "chain": [item.to_dict() for item in chain],
        }

    # -- approvals -------------------------------------------------------------------

    @app.get("/approvals")
    def list_approvals(status: str | None = None) -> dict[str, Any]:
        """List gated actions. Pending by default; ``?status=resolved|all`` for the rest.

        The default stays ``pending`` so a panel that has always called this keeps seeing
        exactly what it saw before. ``all`` is the audit view: request and decision in one
        row, which is how "what did I approve, and when" gets answered.

        A request past its ``expires_at`` but not yet swept is **not** pending here (steward
        #66): it denies by default, and a panel that listed it as still answerable would let
        a human click *approve* on something the deny-by-default sweep is about to close. It
        reappears under ``resolved`` once :func:`steward.approvals.expire` records the deny.
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
            now = ev.utc_now_iso()
            records = [r for r in records if r.expires_at is None or r.expires_at > now]
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
        if not recorded and record.pending:
            # Not a replay: the request was still pending, so decide() refused it because it
            # had already expired (steward #66). Deny-by-default has the last word — a click
            # a minute past the deadline must not slip an action through ahead of the sweep,
            # which denies it and closes the loop in the log. Distinct from an already-decided
            # request, which comes back resolved and reads as a replay below.
            _refuse(
                409,
                "approval_expired",
                f"approval request {request_id!r} expired at {record.expires_at} and denies "
                f"by default; it can no longer be decided",
            )
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
        resumed = _resume_if_budget(record.action, body.decision, request_id)
        return {
            "request_id": request_id,
            "status": "recorded",
            "decision": record.decision,
            "decided_by": record.decided_by,
            "decided_at": record.decided_at,
            "resumed": resumed,
            "message": (
                f"recorded; {resumed} is no longer paused and fires on its next schedule"
                if resumed
                else "recorded; the resident acts on it when the blocked session reads it "
                "or the parked work resumes on its next wake-up"
            ),
        }

    def _resume_if_budget(action: str, decision: str, request_id: str) -> str | None:
        """Lift a budget pause when the human approved lifting it. Returns who resumed.

        The unpause path the issue asks for, and it is the *same* approval machinery every
        other gated action uses: a budget pause raises an ordinary ``needs_human``, and
        answering it ``approve`` here is what resumes the resident. ``deny`` is a real
        answer too — it leaves the resident paused, which is what the human just said.

        ``decide=False`` because the decision has already been recorded and
        ``needs_human_resolved`` already emitted, three lines up. Recording it twice would
        put two answers in the log for one question.
        """
        if action != BUDGET_ACTION or decision != "approve":
            return None
        pause = db.pause_for_request(request_id)
        if pause is None:
            return None
        guard.resume(pause.resident, decided_by=DECIDED_BY, decide=False)
        return pause.resident

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
