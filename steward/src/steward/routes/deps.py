"""Collaborators shared by steward's route factories."""

from dataclasses import dataclass
from pathlib import Path
from typing import Any, NoReturn

from fastapi import HTTPException, Request
from pydantic import BaseModel, ConfigDict

from steward.manifest import Resident, ValidationResult
from steward.operator_auth import OperatorPrincipal
from steward.session_auth import SessionPrincipal
from steward.store import Store, new_id
from steward.transitions.approval import ApprovalOutboxWorker, ApprovalTransitions
from steward.transitions.task import TaskTransitions

ACTED_BY_API = "api"
DOCUMENT_MAX_CHARS = 200_000


class _Body(BaseModel):
    """Base for request bodies: unknown keys are refused, not silently ignored."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


@dataclass(slots=True)
class Deps:
    """The single mutable collaborator graph every router closes over."""

    settings: Any
    db: Store
    sink: Any
    residents_dir: Path
    now: Any
    nursery: Any
    provisioner: Any
    retirer: Any
    transport: Any
    tasks: TaskTransitions
    approvals: ApprovalTransitions
    guard: Any
    outbox: ApprovalOutboxWorker
    runs: Any
    hooks: Any
    claims: Any

    def accept(self, request: Request, outcome: str, detail: dict[str, Any] | None = None) -> str:
        """Log an accepted mutating request and return its trace id."""
        request_id = new_id()
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

    @staticmethod
    def session_of(request: Request) -> SessionPrincipal | None:
        """Return the resident session making this request, or ``None`` for a human."""
        principal = getattr(request.state, "session", None)
        return principal if isinstance(principal, SessionPrincipal) else None


def _refuse(status: int, error: str, message: str) -> NoReturn:
    """Fail immediately with the API's stable refusal shape."""
    raise HTTPException(status_code=status, detail={"error": error, "message": message})
