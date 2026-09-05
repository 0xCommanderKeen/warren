"""The tracker projection and its separately attributed resident judgement."""

from datetime import timedelta
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import AwareDatetime

from steward.deploy import memory_host_dir
from steward.manifest import validate_path
from steward.queue_report import read_note
from steward.queue_responses import QueueResponse
from steward.routes.deps import Deps
from steward.work_queue import QueueUnavailableError


def router(deps: Deps) -> APIRouter:
    """Build the queue read around the application's cached tracker source."""
    routes = APIRouter()

    @routes.get("/queue", response_model=QueueResponse)
    def get_queue(since: AwareDatetime | None = None) -> dict[str, Any]:
        """Read tracker facts; failed or missing resident reports remain explicit."""
        source = deps.queue
        if source is None:
            raise HTTPException(
                503, detail={"message": "Set STEWARD_QUEUE_REPOSITORY to enable the work queue."}
            )
        residents = validate_path(deps.residents_dir, deps.settings.skills_dir)
        reporter = next(
            (r for r in residents.residents if r.id == deps.settings.queue_reporter), None
        )
        report: dict[str, Any] = {
            "note": None,
            "message": "The reporting resident is not declared.",
        }
        if reporter is not None:
            receipt = deps.db.latest_routine_runs().get(f"{reporter.id}/queue-review")
            report = read_note(memory_host_dir(reporter.manifest), receipt, source.repository)
        note = report.get("note")
        ranked = tuple(item["number"] for item in note["recommendations"]) if note else ()
        try:
            projection = source.read(since=since or deps.now() - timedelta(days=1), ranked=ranked)
        except QueueUnavailableError as exc:
            raise HTTPException(503, detail={"message": str(exc)}) from exc
        return {**projection, "reporter": deps.settings.queue_reporter, "report": report}

    return routes
