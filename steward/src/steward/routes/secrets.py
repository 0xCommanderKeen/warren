"""The credential write path: ``PUT /secrets/{name}``, and a listing that names no values.

Provisioning a bot used to have one step nobody could automate — paste the token into the
burrow's ``.env`` over ssh — and it is the step that keeps breaking (warren#391, and again
on 2026-09-04 while wiring Hob). A Claude session appending a credential to a shell heredoc
is refused by a classifier, correctly; the answer is not to talk it round but to give the
credential a door of its own.

This is that door, and it is deliberately one-way. There is a ``PUT`` and there is a
listing of *names*; there is no ``GET /secrets/{name}``, no value in the listing, no value
in the event, and no value in the request log. A secret that steward can be asked to read
back is a secret one compromised credential away from being read back, and every consumer
of these values is a process on this machine that can read the file directly.

Human callers only. The gate in :mod:`steward.routes.auth` refuses a session credential on
every write path it has not explicitly opened, and this one is never opened: a resident that
could set ``STEWARD_CHAT_TOKEN_DISCORD_PIP`` could take Pip's identity, which is exactly what
``app_grants`` exists to prevent.
"""

from typing import Any

from fastapi import APIRouter, Request
from pydantic import Field

from steward import chat as ch
from steward import events as ev
from steward import secrets as sec
from steward.manifest import validate_path
from steward.routes.deps import Deps, _Body, _refuse

#: Where a set secret was found. ``file`` is the secrets directory this endpoint writes to;
#: ``env`` is the burrow's ``.env``, still authoritative for anything no file overrides. The
#: distinction is the one an operator mid-migration actually needs: it says whether a
#: rotation through this endpoint would take effect, or be shadowed by a variable.
SOURCE_FILE = "file"
SOURCE_ENV = "env"


class SecretBody(_Body):
    """The one thing a caller sends, and the one thing steward never sends back.

    Deliberately *unconstrained* at the schema layer, which is the opposite of every other
    body in this API. Pydantic reports a length violation by echoing the offending
    ``input`` into the 422 response — which for this one field would put the credential in
    a response body, and from there into whatever logged it. The bounds still apply; they
    are applied by :func:`steward.secrets.write_secret`, which refuses by *naming the rule*
    and never quotes what it refused.
    """

    value: str = Field(description="The credential itself. Stored, never returned by any route.")


def router(deps: Deps) -> APIRouter:
    """Build the secrets routes over one application collaborator graph."""
    routes = APIRouter()

    def claims() -> dict[str, dict[str, str]]:
        """Map each declared chat route's credential slot to the route that wants it.

        The same walk ``steward chat list`` makes, over the tree as it is on disk right now
        rather than the copy this process booted with — a route declared a second ago
        through ``PUT /residents/{id}/declaration`` is precisely the one whose token is
        about to be pasted.
        """
        result = validate_path(deps.residents_dir, deps.settings.skills_dir)
        found: dict[str, dict[str, str]] = {}
        for route in ch.chat_routes(list(result.residents)):
            found.setdefault(
                route.address.token_env,
                {
                    "resident": route.resident.id,
                    "route": route.route_id,
                    "address": str(route.address),
                },
            )
        return found

    @routes.get("/secrets")
    def list_secrets() -> dict[str, Any]:
        """List every credential slot, whether it is filled, and which route wants it.

        Three sources of names, unioned: the slots declared chat routes ask for, the files
        in the secrets directory, and the ``STEWARD_CHAT_TOKEN_*`` variables still in this
        process's environment. A slot no route claims is listed all the same — that is what
        a half-finished provisioning looks like, and hiding it would hide the thing an
        operator came here to see.
        """
        directory = sec.secrets_dir()
        wanted = claims()
        on_disk = set(sec.secret_names(directory))
        in_env = {name for name in ch.token_env_names() if name not in on_disk}
        names = sorted(set(wanted) | on_disk | in_env)
        return {
            "directory": str(directory),
            "secrets": [
                {
                    "name": name,
                    "set": name in on_disk or name in in_env,
                    "source": (
                        SOURCE_FILE if name in on_disk else SOURCE_ENV if name in in_env else None
                    ),
                    "route": wanted.get(name),
                }
                for name in names
            ],
        }

    @routes.put("/secrets/{name}")
    def write_secret(name: str, body: SecretBody, request: Request) -> dict[str, Any]:
        """Store one credential in its own private file. Returns that it was stored.

        Everything a caller learns is that the write happened, and the request id it
        happened under. The name is echoed because the caller chose it; the value is not,
        because a response body is the easiest thing in this system to end up in a log.
        """
        try:
            sec.write_secret(name, body.value, directory=sec.secrets_dir())
        except sec.SecretError as error:
            _refuse(
                422,
                "invalid_secret_name" if not sec.valid_name(name) else "invalid_secret_value",
                str(error),
            )
        # The name, and only the name, in both places a person can read afterwards. The
        # request log gets no body: ``deps.accept`` writes whatever detail it is handed
        # straight into ``steward.db``, and the body is the credential.
        request_id = deps.accept(request, "secret_written", {"secret": name})
        deps.sink.emit(ev.secret_written_event(name=name))
        return {"request_id": request_id, "name": name, "set": True}

    return routes
