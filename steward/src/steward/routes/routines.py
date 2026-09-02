"""Routine ledger and run-now HTTP routes."""

from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Request

from steward.budgets import PAUSED_ERROR
from steward.manifest import Resident, retired_complaint, validate_path
from steward.routes.deps import Deps, _refuse
from steward.scheduler import (
    TRIGGER_MANUAL,
    ScheduledRoutine,
    SchedulerState,
    default_state_path,
    scheduler_liveness,
)
from steward.store import LedgerEntry, RequestRecord

ALREADY_RUNNING_ERROR = "already_running"


class AlreadyRunningError(Exception):
    """Raised when a resident is asked to run while one of its sessions is going."""

    def __init__(self, reason: str) -> None:
        """Carry the sentence this refusal is served with."""
        super().__init__(reason)
        self.reason = reason


def latest_run_requests(records: Sequence[RequestRecord]) -> dict[str, dict[str, Any]]:
    """Index the request log by routine key, keeping the newest entry for each."""
    latest: dict[str, dict[str, Any]] = {}
    for record in records:
        key = record.detail.get("routine")
        if isinstance(key, str):
            latest[key] = record.to_dict()
    return latest


def last_run_view(entry: LedgerEntry | None) -> dict[str, Any] | None:
    """Return the small "what actually ran" block a routine row carries, or ``None``."""
    if entry is None:
        return None
    return {
        "run_id": entry.run_id,
        "trigger": entry.trigger,
        "outcome": entry.outcome,
        "recorded_at": entry.recorded_at,
        "duration_s": round(entry.duration_s, 3),
    }


def _refuse_if_retired(resident: Resident) -> None:
    """Refuse to give work to a retired resident, with the shared reason."""
    complaint = retired_complaint(resident)
    if complaint is not None:
        _refuse(409, "resident_retired", complaint)


def router(deps: Deps) -> APIRouter:
    """Build the routines router over one application collaborator graph."""
    routes = APIRouter()

    @routes.get("/routines")
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
        resident_retired``, which is what townhall reads to grey the button out.

        ``anchor`` is the scheduler's own state file, read fresh on every request because
        the daemon is a different process — and it is called an anchor rather than a last
        run because that is what it is: the moment the next occurrence is computed from,
        which is the last fire *or* the moment steward first saw the routine. Calling it
        "last run" would let a routine that has never fired look like one that has.

        ``scheduler`` is the one thing here that *is* a heartbeat: when a scheduler process
        last woke up against that state file, how stale that may get before it stops
        meaning anything is up, and the verdict. ``alive: null`` — never ticked — is its
        own answer, distinct from a daemon that died. A ledger is still a declaration; this
        is what says whether the declarations have anything to fire them.

        ``last_run`` and ``last_request`` are two different facts and both are here
        (warren#104). ``last_request`` is the *API request log*: a run somebody asked for
        over HTTP. A scheduled fire is not an HTTP request, so it never appears there — and
        a panel that showed only that one concluded a perfectly healthy resident "only runs
        when I trigger it manually", which was false. ``last_run`` is the run ledger, which
        every finished session writes to whatever started it, so it carries the trigger
        (``schedule`` or ``manual``) and the outcome. Keeping both is the point: a request
        that was accepted and never ran is exactly the case where they disagree, and that
        disagreement is the diagnosis.
        """
        result = validate_path(deps.residents_dir, deps.settings.skills_dir)
        state = SchedulerState.load(default_state_path())
        now = datetime.now(UTC)
        latest = latest_run_requests(deps.db.requests())
        runs_by_key = deps.db.latest_routine_runs()
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
                        "last_run": last_run_view(runs_by_key.get(item.key)),
                    }
                )
        return {
            "routines": routines,
            "state_path": str(default_state_path()),
            "scheduler": scheduler_liveness(state, now),
            "errors": [diagnostic.render() for diagnostic in result.errors],
        }

    @routes.post("/residents/{resident_id}/routines/{routine_id}/run", status_code=202)
    def run_routine(resident_id: str, routine_id: str, request: Request) -> dict[str, Any]:
        """Ask for one run of one routine, right now, and acknowledge only that."""
        result = validate_path(deps.residents_dir, deps.settings.skills_dir)
        resident = deps.find_resident(result, resident_id)
        _refuse_if_retired(resident)
        routine = next((item for item in resident.manifest.routines if item.id == routine_id), None)
        if routine is None:
            known = ", ".join(item.id for item in resident.manifest.routines) or "none"
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
                "manifest rather than firing something the declaration says is off",
            )
        refusal = deps.guard.allow(resident.manifest)
        if refusal is not None:
            _refuse(409, PAUSED_ERROR, refusal)
        item = ScheduledRoutine(resident=resident, routine=routine)
        request_id = deps.accept(request, "queued", {"routine": item.key})
        try:
            deps.runs.submit(item, request_id)
        except AlreadyRunningError as exc:
            deps.db.set_request_outcome(request_id, "refused: already running")
            _refuse(409, ALREADY_RUNNING_ERROR, exc.reason)
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

    return routes
