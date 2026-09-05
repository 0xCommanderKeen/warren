"""Accepted requests and bounded authentication summaries over HTTP."""

from typing import Any

from fastapi import APIRouter

from steward.routes.deps import Deps, _refuse
from steward.routes.responses import RefusalResponse, RequestListResponse, RequestResponse

REQUESTS_DEFAULT_LIMIT = 50
REQUESTS_MAX_LIMIT = 500


def router(deps: Deps) -> APIRouter:
    """Build request-ledger routes over one application collaborator graph."""
    routes = APIRouter(responses={401: {"model": RefusalResponse}})

    @routes.get("/requests", response_model=RequestListResponse)
    def list_requests(
        limit: int = REQUESTS_DEFAULT_LIMIT, resident: str | None = None
    ) -> dict[str, Any]:
        """Return requests and AUTH failure summaries, newest first.

        The endpoint that makes "accepted" survivable as an answer. Every mutating call
        here returns a ``request_id`` and refuses to claim an effect; this is where the
        effect eventually shows up — ``queued`` becomes ``ran``, ``skipped: …``, or
        ``failed`` when the run it stands for finishes. A control panel polls one of these
        rather than deciding on its own that a 202 went well.
        """
        window = max(1, min(limit, REQUESTS_MAX_LIMIT))
        rows = deps.db.recent_requests(limit=window, resident=resident)
        return {"requests": [record.to_dict() for record in rows]}

    @routes.get(
        "/requests/{request_id}",
        response_model=RequestResponse,
        responses={404: {"model": RefusalResponse}},
    )
    def get_request(request_id: str) -> dict[str, Any]:
        """Return one request or AUTH summary. ``404`` for an id nobody logged."""
        record = deps.db.request(request_id)
        if record is None:
            _refuse(
                404,
                "unknown_request",
                f"no request {request_id!r}; refused calls have no individual receipt, "
                "but authentication failures appear in aggregate AUTH summaries",
            )
        return record.to_dict()

    return routes
