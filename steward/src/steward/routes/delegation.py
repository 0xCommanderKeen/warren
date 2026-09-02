"""Delegation inbox, handoff, and lineage HTTP routes."""

from typing import Any

from fastapi import APIRouter, Request
from pydantic import ConfigDict, Field

from steward import delegation as dg
from steward.input_bounds import DETAIL_MAX_CHARS, IDENTIFIER_MAX_CHARS, TITLE_MAX_CHARS
from steward.manifest import validate_path
from steward.routes.deps import Deps, _Body, _refuse
from steward.store import JOB_STATUSES, STATUS_OPEN

APPROVAL_STATUS_ALL = "all"
DELEGATION_STATUS = {
    dg.UNKNOWN_RECIPIENT: 404,
    dg.RETIRED_RECIPIENT: 404,
    dg.UNKNOWN_PARENT: 404,
}
DELEGATION_REFUSED_STATUS = 409


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


def router(deps: Deps) -> APIRouter:
    """Build the delegation router over one application collaborator graph."""
    routes = APIRouter()

    @routes.get("/residents/{resident_id}/inbox")
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
        result = validate_path(deps.residents_dir, deps.settings.skills_dir)
        resident = deps.find_resident(result, resident_id)
        items = deps.db.inbox(resident.id, None if wanted == APPROVAL_STATUS_ALL else wanted)
        return {
            "resident": resident.id,
            "status": wanted,
            "routes": [
                {"id": route.id, "status": route.status, "accepts": route.accepts_delegation}
                for route in resident.inbound_routes
            ],
            "pending": deps.db.inbox_count(resident.id),
            "inbox": [item.to_dict() for item in items],
        }

    @routes.post("/delegate", status_code=202)
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
        principal = deps.session_of(request)
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
        result = validate_path(deps.residents_dir, deps.settings.skills_dir)
        sender = deps.find_resident(result, sender_id) if sender_id is not None else None
        delegator = dg.Delegator(residents=result.residents, store=deps.db, emitter=deps.sink)
        handoff = dg.Handoff(
            raw="POST /delegate", to=body.to, route=body.route, title=body.title, detail=body.detail
        )
        try:
            task = delegator.delegate(
                sender=sender, handoff=handoff, parent_task_id=body.parent_task_id
            )
        except dg.DelegationError as exc:
            _refuse(
                DELEGATION_STATUS.get(exc.reason, DELEGATION_REFUSED_STATUS), exc.reason, str(exc)
            )
        request_id = deps.accept(
            request, "delegated", {"task_id": task.task_id, "to": task.assignee}
        )
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

    @routes.get("/tasks/{task_id}/lineage")
    def get_lineage(task_id: str) -> dict[str, Any]:
        """Return the whole chain this task belongs to, root first. The audit query.

        ``chain`` is the root and everything delegated out of it, depth-first, so the
        answer does not depend on which member of the chain was named (steward #202).
        ``origin`` and ``depth`` still describe the task that was asked about.
        """
        chain = deps.db.lineage(task_id)
        if not chain:
            _refuse(404, "unknown_task", f"no task {task_id!r}")
        asked = next((item for item in chain if item.task_id == task_id), chain[0])
        return {
            "task_id": task_id,
            "origin": asked.origin,
            "depth": asked.depth,
            "chain": [item.to_dict() for item in chain],
        }

    return routes
