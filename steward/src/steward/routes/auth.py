"""Authentication and request-body guards for steward routes."""

from collections.abc import Callable, Collection, Sequence
from hmac import compare_digest

from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from steward.input_bounds import (
    APPROVAL_BODY_MAX_BYTES,
    EDIT_MAX_DEPTH,
    validate_json_container_depth,
)
from steward.manifest import SessionGrant
from steward.operator_auth import OperatorPrincipal, looks_like_operator_credential
from steward.routes.deps import _refuse
from steward.session_auth import (
    SESSION_TOKEN_ENV,
    SessionPrincipal,
    looks_like_session_credential,
)

TOKEN_ENV = "STEWARD_TOKEN"  # noqa: S105 — an env var name, not a credential

SESSION_SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})

#: The write paths a session credential may reach, exactly.
#:
#: An allowlist, so a route added later is refused until somebody decides otherwise —
#: the opposite way round from a denylist, where a new write path would be session-reachable
#: the moment it was merged and nobody would notice.
#:
#: It is a short list because the write surface a session actually wants is small. There is
#: no endpoint to *raise* an approval at all — the routes are ``GET /approvals``,
#: ``GET /approvals/{id}`` and the human-only ``POST /approvals/{id}`` — so raising stays on
#: the block and CLI path either way. This credential buys denial and identity, not reach.
SESSION_WRITE_PATHS = frozenset({"/delegate"})


#: Granted write paths, matched after the permanent session-safe allowlist above. A prefix
#: ends in ``/`` so ``/skills-not-really`` can never inherit the skill-library door.
def _session_grant_for(method: str, path: str) -> SessionGrant | None:
    """Name the grant for one exact method/route shape, or keep the door closed."""
    if method == "POST" and path == "/residents":
        return SessionGrant.RESIDENTS_DECLARE
    if method == "POST" and path.startswith("/residents/") and path.endswith("/provision"):
        resident_id = path.removeprefix("/residents/").removesuffix("/provision")
        if resident_id and "/" not in resident_id:
            return SessionGrant.RESIDENTS_DRY_RUN
    if method == "POST" and path == "/skills":
        return SessionGrant.SKILLS_WRITE
    if method == "PUT" and path.startswith("/skills/"):
        name = path.removeprefix("/skills/")
        if name and "/" not in name:
            return SessionGrant.SKILLS_WRITE
    return None


#: Why a particular refusal is the one it is. Generic prose would tell a session it may not
#: write; these say what the act *is*, which is the part worth knowing: these three are
#: human acts, and a session that could perform them would be answering its own knock,
#: declaring its own colleagues, or firing its own work.
#: **Most specific first**, and the first match wins: the routine-fire path is
#: ``/residents/{id}/routines/{id}/run``, so a ``/residents`` fragment ahead of
#: ``/routines/`` would tell a session it had tried to declare a resident.
_SESSION_REFUSALS: tuple[tuple[str, str], ...] = (
    (
        "/declaration",
        (
            "a resident's charter, skills and routines are written about it rather than by "
            "it; a session that could edit its own declaration would be choosing its own "
            "rules, which is the one thing the declaration exists to stop"
        ),
    ),
    (
        "/skills",
        (
            "a skill is a capability somebody granted; a session that could write one would "
            "be handing itself instructions nobody approved"
        ),
    ),
    (
        "/reload",
        (
            "when the fleet re-reads its declarations is an operator's decision, not "
            "something a running session arranges for itself"
        ),
    ),
    (
        "/approvals/",
        (
            "deciding an approval is the human end of the escalation boundary; a session "
            "that could decide would be answering its own knock"
        ),
    ),
    (
        "/routines/",
        (
            "firing a routine is a human act; a session's own work arrives through the "
            "board and its inbox"
        ),
    ),
    (
        "/provision",
        (
            "provisioning is starting a container on a machine over ssh; a session that "
            "could do it would be building its own colleagues, or itself again"
        ),
    ),
    (
        "/retire",
        (
            "retiring is ending a resident: a mark in git, a container stopped and a village "
            "token removed; a session that could do it would be deciding which of its "
            "colleagues carries on, or dismissing itself"
        ),
    ),
    (
        "/residents",
        ("declaring a resident is a human act; a session may not add to the fleet it is part of"),
    ),
)


def _session_refusal(path: str) -> str:
    """Name the act a session credential was refused, as specifically as steward can."""
    for fragment, reason in _SESSION_REFUSALS:
        if fragment in path:
            return reason
    return "this write path is not one a session credential reaches"


def _presented_bearer_ascii(headers: Sequence[tuple[bytes, bytes]]) -> str:
    """Return the presented bearer value as text, or ``""`` if it is not even ASCII.

    Every credential steward mints is ASCII by construction, so a value that does not
    decode is not one of them and never needs to reach a shape test.
    """
    try:
        return _presented_bearer(headers).decode("ascii")
    except UnicodeDecodeError:
        return ""


def _presented_session_credential(headers: Sequence[tuple[bytes, bytes]]) -> str:
    """Return the presented bearer value if it is *shaped* like a session credential.

    A cheap syntactic test that grants nothing: the API tries the human token first and
    only reaches for the run registry when what was presented could not be anything else.
    """
    presented = _presented_bearer_ascii(headers)
    return presented if looks_like_session_credential(presented) else ""


def _presented_operator_credential(headers: Sequence[tuple[bytes, bytes]]) -> str:
    """Return the presented bearer value if it is *shaped* like an operator credential.

    The same grant-nothing test as its session sibling, against the other prefix. The two
    prefixes are distinct precisely so this dispatch cannot be ambiguous.
    """
    presented = _presented_bearer_ascii(headers)
    return presented if looks_like_operator_credential(presented) else ""


type PrincipalLookup = Callable[[str], SessionPrincipal | None]
type OperatorLookup = Callable[[str], OperatorPrincipal | None]
type SessionGrantsLookup = Callable[[str], Collection[SessionGrant]]
type TokenComparator = Callable[[bytes, bytes], bool]


def _auth_dependency(
    token: str | None,
    principal_for: PrincipalLookup,
    operator_for: OperatorLookup,
    grants_for: SessionGrantsLookup,
    compare_token: TokenComparator = compare_digest,
) -> Callable[[Request], None]:
    """Build the gate every endpoint hangs off, and record who got through it.

    Three kinds of caller, tried in the order that keeps the cheapest check first and the
    database out of the common path:

    **The master token** (``STEWARD_TOKEN``), one constant-time compare, no principal —
    the CLI's and the environment's credential.

    **A named operator** (warren#225), looked up by digest against ``operator_credentials``.
    A *human* principal: it reaches exactly what the master token reaches, and the only
    difference is that steward can say who it was, which is the difference the audit trail
    lives on. This is what a browser is given, so the master token stops going into one.

    **A session** (steward #41), looked up against the live run registry, and then held to
    the reads-plus-``/delegate`` allowlist and its resident's named grants below. Unchanged
    by any of the above: that allowlist exists to keep *sessions* out of human acts, and an
    operator is a human.
    """

    def require_token(request: Request) -> None:
        headers = request.scope.get("headers", [])
        # Set before any branch: a route reading these must never see a principal left
        # over from the request before it.
        request.state.session = None
        request.state.operator = None
        if _authorized(headers, token, compare_token):
            return
        presented_operator = _presented_operator_credential(headers)
        operator = operator_for(presented_operator) if presented_operator else None
        if operator is not None:
            request.state.operator = operator
            return
        presented = _presented_session_credential(headers)
        principal = principal_for(presented) if presented else None
        if principal is None:
            raise HTTPException(
                status_code=401,
                detail={
                    "error": "unauthorized",
                    "message": (
                        f"this endpoint needs Authorization: Bearer <{TOKEN_ENV}>, an "
                        f"operator credential minted with `steward operator mint`, or the "
                        f"credential steward minted for a live run (${SESSION_TOKEN_ENV})"
                    ),
                },
                headers={"WWW-Authenticate": "Bearer"},
            )
        request.state.session = principal
        path = request.url.path.rstrip("/") or "/"
        required_grant = _session_grant_for(request.method, path)
        granted_path = required_grant is not None and required_grant in grants_for(
            principal.resident_id
        )
        if (
            request.method not in SESSION_SAFE_METHODS
            and path not in SESSION_WRITE_PATHS
            and not granted_path
        ):
            _refuse(
                403,
                "session_credential_forbidden",
                f"{principal.resident_id} presented the credential for run "
                f"{principal.run_id}, and {_session_refusal(path)}. Nothing was recorded.",
            )

    return require_token


def operator_of(request: Request) -> OperatorPrincipal | None:
    """Return the named operator who made this request, or ``None`` for anyone else.

    ``None`` covers the master token — a shared secret with no person behind it — and open
    mode, where there is no credential to name anybody by. Both are honest answers, and
    both make steward fall back to describing the *door* a change came through rather than
    inventing a person for it.

    Set by the gate, which runs before any route.
    """
    principal = getattr(request.state, "operator", None)
    return principal if isinstance(principal, OperatorPrincipal) else None


def session_of(request: Request) -> SessionPrincipal | None:
    """Return the resident whose session made this request, or ``None`` for a human.

    The distinction is the whole of steward #41. A **session** presents the credential
    minted for its own run, which *is* a principal: it names a resident, dies with the run,
    and reaches only what a session legitimately needs. Every other caller is a human —
    the master ``STEWARD_TOKEN``, or a named operator credential (see :func:`operator_of`,
    warren#225) — and reaches everything.

    ``None`` also covers open mode (``--allow-open``), where there is no token to compare
    and so no caller steward can tell apart. That is not a gap this function can close: a
    session running against an open steward can reach any route with no header at all.

    Set by the gate, which runs before any route.
    """
    principal = getattr(request.state, "session", None)
    return principal if isinstance(principal, SessionPrincipal) else None


def _presented_bearer(headers: Sequence[tuple[bytes, bytes]]) -> bytes:
    """Return the single presented bearer value, or ``b""``.

    Exactly one Authorization field is accepted.  Rejecting duplicates avoids proxy and
    framework disagreement over first/last/comma-joined semantics.

    One parse for both credential kinds, and bytes rather than ``str`` on purpose: the
    human token is compared byte for byte, and decoding first would let an
    invalid-UTF-8 header be lossily normalised into a comparison it should have failed
    (steward #41).
    """
    values = [value for key, value in headers if key.lower() == b"authorization"]
    if len(values) != 1:
        return b""
    scheme, separator, presented = values[0].partition(b" ")
    if separator != b" " or scheme.lower() != b"bearer":
        return b""
    return presented.strip()


def _authorized(
    headers: Sequence[tuple[bytes, bytes]],
    token: str | None,
    compare_token: TokenComparator = compare_digest,
) -> bool:
    """Apply the API's human-token policy to raw ASGI headers.

    All presented bearer tokens reach the same constant-time comparison used by the route
    dependency.  ``token is None`` is open mode, where there is nothing to compare.
    """
    if token is None:
        return True
    presented = _presented_bearer(headers)
    return bool(presented) and compare_token(presented, token.encode("utf-8"))


class _ApprovalBodyDepthMiddleware:
    """Bound approval JSON while receiving, before recursive materialisation."""

    def __init__(
        self,
        app: ASGIApp,
        *,
        token: str | None,
        compare_token: TokenComparator = compare_digest,
    ) -> None:
        self.app = app
        self.token = token
        self.compare_token = compare_token

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:  # noqa: C901
        path = scope.get("path", "")
        is_decision = (
            scope.get("type") == "http"
            and scope.get("method") == "POST"
            and path.startswith("/approvals/")
            and "/" not in path.removeprefix("/approvals/")
        )
        if not is_decision:
            await self.app(scope, receive, send)
            return
        # A session credential is *authenticated* and then refused by the route policy, so
        # it reaches body parsing the way the human token does. Bound it here on shape
        # alone — no database lookup in the middleware — or the depth guard would hold for
        # one credential kind and not the other. An operator credential is on the same
        # footing and for the same reason: it decides approvals, so it is the credential
        # most likely to be carrying one of these bodies (warren#225).
        headers = scope.get("headers", [])
        if not (
            _authorized(headers, self.token, self.compare_token)
            or _presented_operator_credential(headers)
            or _presented_session_credential(headers)
        ):
            await self.app(scope, receive, send)
            return

        body = bytearray()
        complete = False
        terminal: Message | None = None
        saw_request = False
        while True:
            message = await receive()
            if message["type"] != "http.request":
                terminal = message
                break
            saw_request = True
            chunk = message.get("body", b"")
            if len(body) + len(chunk) > APPROVAL_BODY_MAX_BYTES:
                response = JSONResponse(
                    status_code=413,
                    content={
                        "detail": {
                            "error": "approval_body_too_large",
                            "message": (
                                "approval request body exceeds the "
                                f"{APPROVAL_BODY_MAX_BYTES} byte wire limit"
                            ),
                        }
                    },
                )
                await response(scope, receive, send)
                return
            body.extend(chunk)
            if not message.get("more_body", False):
                complete = True
                break
        try:
            # The request object is level one; an eight-level edit is therefore level nine.
            if complete:
                validate_json_container_depth(body, EDIT_MAX_DEPTH + 1)
        except ValueError as error:
            response = JSONResponse(
                status_code=422,
                content={
                    "detail": [
                        {
                            "type": "value_error",
                            "loc": ["body", "edit"],
                            "msg": f"Value error, {error}",
                            "input": None,
                        }
                    ]
                },
            )
            await response(scope, receive, send)
            return

        replayed_body = False
        replayed_terminal = False

        async def replay() -> Message:
            nonlocal replayed_body, replayed_terminal
            if saw_request and not replayed_body:
                replayed_body = True
                return {
                    "type": "http.request",
                    "body": bytes(body),
                    "more_body": not complete,
                }
            if terminal is not None and not replayed_terminal:
                replayed_terminal = True
                return terminal
            return await receive()

        await self.app(scope, replay, send)
