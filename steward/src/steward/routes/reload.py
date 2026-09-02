"""Fleet reload HTTP route."""

from collections.abc import Sequence
from typing import Any, NoReturn

from fastapi import APIRouter, HTTPException, Request

from steward import manifest as m
from steward.manifest import validate_path
from steward.routes.deps import Deps
from steward.scheduler import ScheduledRoutine
from steward.skills import library_for


def _refuse_reload(errors: Sequence[str]) -> NoReturn:
    """Refuse to swap in a tree that does not validate, and say why."""
    raise HTTPException(
        status_code=409,
        detail={
            "error": "tree_invalid",
            "message": (
                "the residents tree does not validate, so nothing was reloaded and this "
                "process is still running the last declarations that did; run "
                "`steward validate` for the field-by-field diagnostics"
            ),
            "errors": list(errors),
        },
    )


def router(deps: Deps) -> APIRouter:
    """Build the reload router over one application collaborator graph."""
    routes = APIRouter()

    @routes.post("/reload")
    def reload_fleet(request: Request) -> dict[str, Any]:
        """Re-read the residents tree and the skills library into this process.

        **This process**, and the distinction is the whole of the endpoint's honesty. The
        scheduler daemon is a *different process* — usually on the same burrow, started by
        ``steward serve`` — and no HTTP call can reach into it. It does not need one: it
        watches the trees itself and reloads on its next wake-up (:class:`TreeSource`),
        which is within a minute. What this endpoint fixes is the API's own long-lived
        collaborators, the run-now scheduler and the board dispatcher, which were assembled
        at startup and would otherwise fire a routine against the manifest that was on disk
        when the server booted.

        Read views need no reload at all — they re-read the tree on every request.
        """
        current = library_for(deps.residents_dir, deps.settings.skills_dir)
        result = validate_path(deps.residents_dir, deps.settings.skills_dir)
        errors = [diagnostic.render() for diagnostic in result.errors]
        if errors:
            _refuse_reload(errors)
        active = tuple(m.active_residents(result.residents))
        deps.runs.scheduler.set_library(current)
        deps.runs.scheduler.scheduled = [
            ScheduledRoutine(resident=resident, routine=routine)
            for resident in active
            for routine in resident.manifest.routines
            if routine.enabled
        ]
        deps.hooks.refresh(active, current)
        request.app.state.library = current
        request_id = deps.accept(request, "reloaded", {"residents": len(active)})
        return {
            "request_id": request_id,
            "status": "reloaded",
            "residents": len(active),
            "routines": len(deps.runs.scheduler.scheduled),
            "skills": [skill.name for skill in current],
            "errors": errors,
            "message": (
                "this API process re-read the tree; the scheduler daemon is a separate "
                "process and picks the same change up on its own next wake-up"
            ),
        }

    return routes
