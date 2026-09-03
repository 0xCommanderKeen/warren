"""Approval-ledger HTTP routes."""

from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import Field, field_validator

from steward import events as ev
from steward.approvals import WithheldValueError, redact_decision, restore_withheld
from steward.input_bounds import validate_approval_edit
from steward.routes.deps import Deps, _Body, _refuse
from steward.store import new_id

APPROVAL_STATUS_PENDING = "pending"
APPROVAL_STATUS_ALL = "all"
APPROVAL_STATUSES = (APPROVAL_STATUS_PENDING, "resolved", APPROVAL_STATUS_ALL)


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


def router(deps: Deps) -> APIRouter:  # noqa: C901, PLR0915 — route factory is assembly
    """Build approval routes over one application collaborator graph."""
    routes = APIRouter()

    @routes.get("/approvals")
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
        records = deps.db.approvals(None if wanted == APPROVAL_STATUS_ALL else wanted)
        if wanted == APPROVAL_STATUS_PENDING:
            moment = ev.utc_now_iso(deps.now())
            records = [
                record
                for record in records
                if record.expires_at is None or record.expires_at > moment
            ]
        return {
            "status": wanted,
            "approvals": [redact_decision(record).to_dict() for record in records],
        }

    @routes.get("/approvals/{request_id}")
    def get_approval(request_id: str) -> dict[str, Any]:
        """Return one request with its decision, decider, and timestamps. The audit query."""
        record = deps.db.approval(request_id)
        if record is None:
            _refuse(404, "unknown_approval", f"no approval request {request_id!r}")
        return redact_decision(record).to_dict()

    @routes.post("/approvals/{request_id}")
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
        moment = deps.now()
        edit = body.edit
        stored = deps.db.approval(request_id)
        if stored is not None and edit is not None:
            try:
                edit = restore_withheld(edit, stored.detail)
            except WithheldValueError as exc:
                _refuse(422, "edit_withheld_value", str(exc))
        decided = deps.approvals.decide(
            request_id,
            body.decision,
            decided_by=deps.acted_by(request),
            edit=edit,
            now=moment,
            request_log=(ledger_id, request.method, request.url.path),
        )
        record = decided.record
        if record is None:
            _refuse(404, "unknown_approval", f"no approval request {request_id!r}")
        if decided.expired:
            deps.approvals.expire(moment)
            _refuse(
                409,
                "approval_expired",
                f"approval request {request_id!r} expired at {record.expires_at} and denies "
                "by default; it can no longer be decided",
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
        state = deps.db.approval_announcement_state(request_id)
        if decided.replayed and state != "pending":
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
        announced = state in {"announced", "complete"}
        outcome = "recorded" if announced else "recorded_announcement_pending"
        deps.outbox.notify()
        response.status_code = 202
        resumed = None
        if announced:
            claimed = deps.db.claim_approval_effects(request_id)
            if claimed is not None:
                effect_record, token = claimed
                completed, resumed = deps.db.complete_approval_effects(effect_record, token)
                if not completed:
                    deps.db.release_approval_effects(request_id, token)
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

    return routes
