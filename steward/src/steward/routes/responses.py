"""Published responses for the run-now → request-ledger consumer path.

Only action-specific request detail remains open: it is shared by all write routes,
whose outcome payloads migrate separately. Named envelopes never accept unknown fields.
"""

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict


class _Response(BaseModel):
    model_config = ConfigDict(extra="forbid")


class RequestResponse(_Response):
    """One accepted request; outcome is an extensible, human-readable ledger value."""

    request_id: str
    received_at: str
    method: str
    path: str
    outcome: str
    detail: dict[str, Any]


class RequestListResponse(_Response):
    """The newest accepted requests, in display order."""

    requests: list[RequestResponse]


class RoutineRunReceipt(_Response):
    """An acknowledgement, never proof that the routine ran."""

    request_id: str
    status: Literal["accepted"]
    resident: str
    routine: str
    trigger: Literal["manual"]
    message: str


class RoutineLastRun(_Response):
    """What actually ran, independently of whether an HTTP request asked for it."""

    run_id: str
    trigger: str
    outcome: str
    recorded_at: str
    duration_s: float


class SchedulerLiveness(_Response):
    """A never-ticked scheduler is unknown, rather than dead."""

    last_tick: str | None
    stale_after_s: int
    alive: bool | None


class RoutineResponse(_Response):
    """One standing-work declaration and the evidence of its execution."""

    key: str
    resident: str
    resident_name: str
    accent: str
    routine: str
    schedule: str
    schedule_tz: str
    enabled: bool
    retired: bool
    requires: list[str]
    timeout_s: int
    journal: Literal["close_of_day"] | None
    anchor: str | None
    next_fire: str | None
    last_request: RequestResponse | None
    last_run: RoutineLastRun | None


class RoutineListResponse(_Response):
    """The fleet routine ledger, including manifest errors and scheduler health."""

    routines: list[RoutineResponse]
    state_path: str
    scheduler: SchedulerLiveness
    errors: list[str]


class RefusalDetail(_Response):
    """A machine-readable reason and its operator-facing explanation."""

    error: str
    message: str


class RefusalResponse(_Response):
    """The simple refusal used by the ledger and run-now routes (not every API error)."""

    detail: RefusalDetail
