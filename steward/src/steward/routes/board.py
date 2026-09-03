"""Job-board HTTP routes."""

from typing import Annotated, Any

from fastapi import APIRouter, Request
from pydantic import Field

from steward.input_bounds import (
    DETAIL_MAX_CHARS,
    IDENTIFIER_MAX_CHARS,
    SKILLS_MAX_ITEMS,
    TITLE_MAX_CHARS,
)
from steward.routes.deps import Deps, _Body, _refuse
from steward.store import JOB_STATUSES


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


def router(deps: Deps) -> APIRouter:
    """Build the job-board router over one application collaborator graph."""
    routes = APIRouter()

    @routes.get("/jobs")
    def list_jobs(status: str | None = None) -> dict[str, Any]:
        """List the board, optionally narrowed with ``?status=open|claimed|done|failed``."""
        if status is not None and status not in JOB_STATUSES:
            _refuse(
                422,
                "unknown_status",
                f"status {status!r} is not a board status; use one of: "
                f"{', '.join(JOB_STATUSES)}, or leave it off for the whole board",
            )
        return {"jobs": [job.to_dict() for job in deps.db.jobs(status)]}

    @routes.post("/jobs", status_code=202)
    def post_job(body: JobPost, request: Request) -> dict[str, Any]:
        """Put a task on the board and announce it. No resident is prompted."""
        job = deps.tasks.post(
            title=body.title,
            detail=body.detail,
            required_skills=body.required_skills,
            posted_by=deps.acted_by(request),
        ).require()
        request_id = deps.accept(request, "posted", {"task_id": job.task_id})
        return {
            "request_id": request_id,
            "task_id": job.task_id,
            "status": "accepted",
            "message": (
                "queued on the board; a resident claims it on its own next wake-up, and "
                "task_claimed in burrow's log is the only proof that happened"
            ),
        }

    return routes
