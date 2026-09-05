"""Collaborators shared by steward's route factories."""

from collections.abc import Callable
from contextlib import AbstractContextManager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, NoReturn, NotRequired, Protocol, TypedDict

from fastapi import HTTPException, Request
from pydantic import BaseModel, ConfigDict

from steward import authoring as au
from steward.board import Dispatcher
from steward.budgets import BudgetGuard
from steward.claims import ResidentClaims
from steward.deploy import Transport
from steward.events import Emitter
from steward.manifest import Resident, ValidationResult
from steward.nursery import CommitIdentity, NewResident, NurseryReport, RetireReport
from steward.operator_auth import OperatorPrincipal
from steward.runners import build_runner
from steward.scheduler import ScheduledRoutine, Scheduler
from steward.session_auth import SessionPrincipal
from steward.sessions import RunnerFactory
from steward.store import Store, new_id
from steward.transitions.approval import ApprovalOutboxWorker, ApprovalTransitions
from steward.transitions.task import TaskTransitions

ACTED_BY_API = "api"
API_PRINCIPAL = "a holder of STEWARD_TOKEN, over the steward API"
DOCUMENT_MAX_CHARS = 200_000
WRITE_STATUS: dict[str, int] = {
    "manifest_invalid": 422,
    "skill_invalid": 422,
    "unknown_skill": 404,
    "unknown_resident": 404,
    "resident_invalid": 409,
    "resident_identity_changed": 409,
    "soul_file_changed": 409,
    "skill_exists": 409,
    "not_a_git_checkout": 409,
    "commit_failed": 409,
}


class _Body(BaseModel):
    """Base for request bodies: unknown keys are refused, not silently ignored."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class WriteSettings(TypedDict):
    """Authoring options shared by declaration and skill writes."""

    identity: CommitIdentity
    allow_uncommitted: bool
    push: au.PushTarget | None


class RetirementSettings(TypedDict):
    """Common arguments for retirement planning and execution."""

    residents_dir: Path
    skills_dir: Path | None
    transport: Transport | None
    identity: CommitIdentity
    resident_dirty_only: bool
    revision_of: Callable[[Path], str]
    emitter: Emitter


class NurseryOptions(TypedDict):
    """Options the declaration route supplies to its nursery."""

    residents_dir: Path
    skills_dir: Path | None
    transport: Transport | None
    provision: bool
    commit: bool


class ProvisionOptions(TypedDict):
    """Options the provision route supplies to its nursery."""

    residents_dir: Path
    skills_dir: Path | None
    transport: Transport | None
    dry_run: bool


class RetireOptions(RetirementSettings):
    """Retirement options, including guards used only for execution."""

    dry_run: bool
    expected_revision: NotRequired[str | None]
    durable_guard: NotRequired[AbstractContextManager[object] | None]


class RouteSettings(Protocol):
    """The configuration read by routes; frozen application settings also conform."""

    @property
    def skills_dir(self) -> Path | None:
        """The optional shared skills library."""
        ...

    @property
    def commit_identity(self) -> CommitIdentity | None:
        """The fallback author of API commits."""
        ...

    @property
    def allow_uncommitted_writes(self) -> bool:
        """Whether declaration writes require a checkout."""
        ...

    @property
    def push(self) -> au.PushTarget | None:
        """The optional destination for declaration commits."""
        ...


class NurseryPipeline(Protocol):
    """Declare and optionally provision a resident through the nursery."""

    def __call__(  # noqa: PLR0913 — independent nursery options
        self,
        spec: NewResident,
        *,
        residents_dir: Path,
        skills_dir: Path | None,
        transport: Transport | None,
        provision: bool,
        commit: bool,
    ) -> NurseryReport:
        """Run the nursery pipeline."""
        ...


class ProvisionPipeline(Protocol):
    """Provision an existing declaration."""

    def __call__(
        self,
        resident_id: str,
        *,
        residents_dir: Path,
        skills_dir: Path | None,
        transport: Transport | None,
        dry_run: bool,
    ) -> NurseryReport:
        """Run the nursery pipeline."""
        ...


class RetirePipeline(Protocol):
    """Plan or perform retirement against the guarded declaration."""

    def __call__(  # noqa: PLR0913 — independent nursery options
        self,
        resident_id: str,
        *,
        residents_dir: Path,
        skills_dir: Path | None,
        transport: Transport | None,
        identity: CommitIdentity,
        resident_dirty_only: bool,
        revision_of: Callable[[Path], str],
        emitter: Emitter,
        dry_run: bool,
        expected_revision: str | None = None,
        durable_guard: AbstractContextManager[object] | None = None,
    ) -> RetireReport:
        """Run the guarded retirement pipeline."""
        ...


class RoutineRuns(Protocol):
    """The run-now queue and the scheduler refreshed by the reload door."""

    scheduler: Scheduler

    def submit(self, item: ScheduledRoutine, request_id: str) -> None:
        """Queue a manual run or refuse an overlapping one."""
        ...


@dataclass(slots=True)
class Deps:
    """The single mutable collaborator graph every router closes over."""

    settings: RouteSettings
    db: Store
    sink: Emitter
    residents_dir: Path
    now: Callable[[], datetime]
    nursery: NurseryPipeline
    provisioner: ProvisionPipeline
    retirer: RetirePipeline
    transport: Transport | None
    tasks: TaskTransitions
    approvals: ApprovalTransitions
    guard: BudgetGuard
    outbox: ApprovalOutboxWorker
    runs: RoutineRuns
    hooks: Dispatcher
    claims: ResidentClaims
    #: How a session is turned into a process. Held here because the *rehearsal* door
    #: (warren#446) launches one directly rather than through the scheduler or the chat
    #: bridge, and a route that reached for :func:`steward.runners.build_runner` itself
    #: would be a runner nobody could inject a mock into.
    runner_factory: RunnerFactory = build_runner

    def accept(self, request: Request, outcome: str, detail: dict[str, Any] | None = None) -> str:
        """Log an accepted mutating request and return its trace id."""
        request_id = new_id()
        detail = dict(detail or {})
        slot = getattr(request.state, "master_token_slot", None)
        if slot in {"current", "previous"}:
            detail["master_token_slot"] = slot
        self.db.log_request(
            request_id=request_id,
            method=request.method,
            path=request.url.path,
            outcome=outcome,
            detail=detail,
        )
        return request_id

    def acted_by(self, request: Request) -> str:
        """Return the named operator, or the honest generic API actor."""
        operator = getattr(request.state, "operator", None)
        return operator.name if isinstance(operator, OperatorPrincipal) else ACTED_BY_API

    @staticmethod
    def refuse_write(exc: au.AuthoringError) -> NoReturn:
        """Turn an authoring refusal into its stable HTTP answer."""
        raise HTTPException(
            status_code=WRITE_STATUS.get(exc.reason, 409),
            detail={
                "error": exc.reason,
                "message": str(exc),
                "diagnostics": [au.diagnostic_as_dict(d) for d in exc.diagnostics],
            },
        )

    def write_settings(self, request: Request) -> WriteSettings:
        """Return the authoring knobs shared by resident and skill writes."""
        operator = getattr(request.state, "operator", None)
        session = getattr(request.state, "session", None)
        if isinstance(operator, OperatorPrincipal):
            identity = CommitIdentity(name=operator.name, email=operator.email)
        elif isinstance(session, SessionPrincipal):
            identity = CommitIdentity(
                name=f"{session.resident_id} (session)",
                email=f"{session.resident_id}-session@localhost",
            )
        else:
            identity = self.settings.commit_identity or au.DEFAULT_IDENTITY
        return {
            "identity": identity,
            "allow_uncommitted": self.settings.allow_uncommitted_writes,
            "push": self.settings.push,
        }

    @staticmethod
    def acting_principal(request: Request) -> str:
        """Describe this caller in an authoring commit trailer."""
        operator = getattr(request.state, "operator", None)
        if isinstance(operator, OperatorPrincipal):
            return operator.principal
        session = getattr(request.state, "session", None)
        if isinstance(session, SessionPrincipal):
            return (
                f"{session.resident_id}, over the steward API with a session credential "
                f"for run {session.run_id}"
            )
        return API_PRINCIPAL

    def find_resident(  # noqa: RET503 — every fallthrough raises through the refusal seam
        self, result: ValidationResult, resident_id: str
    ) -> Resident:
        """Resolve a resident by id or uid, preserving the API's refusal vocabulary."""
        for resident in result.residents:
            if resident.id == resident_id:
                return resident
        for resident in result.residents:
            if resident.uid == resident_id:
                return resident
        if (self.residents_dir / resident_id).is_dir():
            _refuse(
                409,
                "resident_invalid",
                f"resident {resident_id!r} exists but its manifest does not validate; "
                "run `steward validate` for the field-by-field diagnostics",
            )
        _refuse(
            404,
            "unknown_resident",
            f"no resident {resident_id!r} in {self.residents_dir}",
        )


def _refuse(status: int, error: str, message: str) -> NoReturn:
    """Fail immediately with the API's stable refusal shape."""
    raise HTTPException(status_code=status, detail={"error": error, "message": message})
