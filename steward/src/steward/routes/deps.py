"""Collaborators shared by steward's route factories."""

from dataclasses import dataclass
from pathlib import Path
from typing import Any, NoReturn

from fastapi import HTTPException, Request
from pydantic import BaseModel, ConfigDict

from steward.operator_auth import OperatorPrincipal
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


def _refuse(status: int, error: str, message: str) -> NoReturn:
    """Fail immediately with the API's stable refusal shape."""
    raise HTTPException(status_code=status, detail={"error": error, "message": message})
