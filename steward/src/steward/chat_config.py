"""Chat route declarations and environment configuration, independent of the wire."""

import logging
import os
import re
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass

from steward import secrets
from steward.manifest import (
    CHAT_ROUTE_KIND,
    CHAT_TOKEN_ENV_PREFIX,
    Resident,
    Route,
    active_residents,
    chat_token_env_name,
)

log = logging.getLogger("steward.chat")


#: The transport names used by route addresses and operator configuration.
TELEGRAM = "telegram"
DISCORD = "discord"

#: What every per-bot token variable starts with. Telegram appends the reference for v0
#: compatibility; other transports append transport then reference so credentials cannot
#: collide. Addresses, not resident ids, own the names: renaming a resident does not silently
#: disconnect its bot, and reading the route is enough to know which variable to set.
TOKEN_ENV_PREFIX = CHAT_TOKEN_ENV_PREFIX

#: Who steward answers: comma-separated ``<transport>:<user-id>`` identities. Bare ids
#: retain their original Telegram meaning. Empty means the bridge refuses to start rather
#: than answering the world.
OPERATORS_ENV = "STEWARD_CHAT_OPERATORS"

#: How long one ``getUpdates`` call may wait for a message before coming back empty. Long
#: polling is what keeps a bridge that answers instantly from costing a request a second.
POLL_TIMEOUT_ENV = "STEWARD_CHAT_POLL_TIMEOUT_S"
DEFAULT_POLL_TIMEOUT_S = 25.0

_ADDRESS = re.compile(r"^(?P<transport>[a-z][a-z0-9+.-]*):(?P<reference>\S.*)$")


class ChatError(Exception):
    """Raised when the bridge refuses to start — loudly, in daylight."""


# --------------------------------------------------------------------------------------
# where a bot's token comes from
# --------------------------------------------------------------------------------------


def token_env_name(reference: str, transport: str = TELEGRAM) -> str:
    """Return the environment variable holding the token for one chat address.

    Telegram keeps its v0 spelling: ``telegram:polica-librarian`` reads
    ``STEWARD_CHAT_TOKEN_POLICA_LIBRARIAN``. Other transports include their name —
    ``discord:pip`` reads ``STEWARD_CHAT_TOKEN_DISCORD_PIP`` — so two services using the
    same reference never share a credential slot. Everything is upper-cased and non-word
    runs fold to ``_`` because environment variable names cannot carry a hyphen.
    """
    resolved = chat_token_env_name(f"{transport}:{reference}")
    if resolved is None:  # pragma: no cover — constructed above in the accepted grammar
        raise ValueError(f"cannot derive a token variable for {transport}:{reference}")
    return resolved


def token_env_names(env: Mapping[str, str] | None = None) -> list[str]:
    """Return every configured bot-token slot name, including empty ones.

    Files and variables together (warren#462): a token that arrived as a file in the
    secrets directory occupies exactly the same named slot as one exported into the
    environment, so a listing that only read the environment would report a wired-up bot
    as unassigned.
    """
    return sorted(name for name in secrets.overlay(env) if name.startswith(TOKEN_ENV_PREFIX))


def tokens_from_env(env: Mapping[str, str] | None = None) -> dict[str, str]:
    """Return every bot token in the environment, keyed by the variable that held it.

    Keyed by variable name rather than address so lookup stays exact and no secret is copied
    into a report. :class:`Address` owns the transport-aware name derivation.

    The mapping is read through :func:`steward.secrets.overlay`, so a token written into the
    secrets directory answers here without ever having been exported — which is what lets a
    daemon that is already running pick one up (warren#462).
    """
    source = secrets.overlay(env)
    names = {name for name in source if name.startswith(TOKEN_ENV_PREFIX)}
    return {
        name: value.strip() for name, value in source.items() if name in names and value.strip()
    }


def operators_from_env(
    env: Mapping[str, str] | None = None, *, transport: str = TELEGRAM
) -> frozenset[str]:
    """Return the user ids steward will answer on one transport. Empty means nobody.

    Empty is a real answer and the safe one: a bridge with no operators answers no message
    from anyone, and refuses to start rather than run as an open door
    (:meth:`ChatBridge.require_ready`). A qualified entry such as ``discord:31337`` belongs
    only to that transport. An unqualified id keeps the original Telegram meaning so an
    existing ``STEWARD_CHAT_OPERATORS=4242`` deployment changes neither identity nor access.
    """
    source = os.environ if env is None else env
    raw = (source.get(OPERATORS_ENV) or "").strip()
    found: set[str] = set()
    for item in raw.split(","):
        entry = item.strip()
        if not entry:
            continue
        namespace, separator, user_id = entry.partition(":")
        if not separator:
            if transport == TELEGRAM:
                found.add(entry)
        elif namespace == transport and user_id:
            found.add(user_id)
    return frozenset(found)


def poll_timeout_from_env(env: Mapping[str, str] | None = None) -> float:
    """Return how long one ``getUpdates`` may wait, from the environment or the default."""
    source = os.environ if env is None else env
    raw = (source.get(POLL_TIMEOUT_ENV) or "").strip()
    if not raw:
        return DEFAULT_POLL_TIMEOUT_S
    try:
        seconds = float(raw)
    except ValueError:
        log.warning(
            "%s=%r is not a number of seconds; using %.0fs",
            POLL_TIMEOUT_ENV,
            raw,
            DEFAULT_POLL_TIMEOUT_S,
        )
        return DEFAULT_POLL_TIMEOUT_S
    return seconds if seconds >= 0 else DEFAULT_POLL_TIMEOUT_S


# --------------------------------------------------------------------------------------
# what a manifest declared
# --------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Address:
    """A chat route's ``address``, split into the transport and the bot it names."""

    transport: str
    reference: str

    @classmethod
    def parse(cls, address: str) -> Address | None:
        """Split ``<transport>:<reference>``, or return ``None`` when it is not that.

        ``None`` rather than a raise: an address that does not parse is a manifest that
        validated (any non-empty string is a legal address) and cannot be delivered to, which
        is a line in :func:`describe_chat` and a skipped route — not an exception that takes
        the whole daemon down over one resident's typo.
        """
        match = _ADDRESS.match(address.strip())
        if match is None:
            return None
        return cls(transport=match.group("transport"), reference=match.group("reference").strip())

    @property
    def token_env(self) -> str:
        """Name the environment variable this bot's token is read from."""
        return token_env_name(self.reference, self.transport)

    def __str__(self) -> str:
        """Render the address the way the manifest spells it."""
        return f"{self.transport}:{self.reference}"


@dataclass(frozen=True, slots=True)
class ChatRoute:
    """One resident, one active chat route, and the bot that route names."""

    resident: Resident
    route: Route
    address: Address

    @property
    def key(self) -> str:
        """The stable name for this doorway: ``<resident id>/<route id>``, for a log line."""
        return f"{self.resident.id}/{self.route.id}"

    @property
    def route_id(self) -> str:
        """Name the route a message came through."""
        return self.route.id

    @property
    def poll_key(self) -> str:
        """Share one Telegram cursor between all routes to a shared bot."""
        return self.address.token_env if self.route.shared else self.key

    @property
    def name(self) -> str:
        """What to call this resident *to its operator*: the soul's name, not the id.

        The same judgement :func:`steward.notify.tap_for` makes about a knock on a phone —
        "Pip is busy right now" is a sentence and ``pip/chat: busy`` is a log line, and this
        text is read by a person in a chat window.
        """
        return self.resident.manifest.soul.name


def _addressed_routes(routes: Sequence[ChatRoute], text: str) -> tuple[list[ChatRoute], str, bool]:
    """Resolve an explicit prefix, preserving ambiguity instead of picking by order."""
    text = text.lstrip()
    if text.startswith("@"):
        candidates: list[tuple[int, ChatRoute, str]] = []
        for route in routes:
            for alias in {route.resident.id, route.name}:
                match = re.match(r"@" + re.escape(alias) + r"(?=:|\s|$):?\s*", text, re.IGNORECASE)
                if match:
                    candidates.append((len(alias), route, text[match.end() :]))
        if not candidates:
            return [], text, True
        longest = max(length for length, _, _ in candidates)
        matches = {route.key: route for length, route, _ in candidates if length == longest}
        remaining = next(rest for length, _, rest in candidates if length == longest)
        return list(matches.values()), remaining, True
    if ":" in text:
        alias, remaining = text.split(":", 1)
        matches = [
            route
            for route in routes
            if alias.strip().casefold() in {route.resident.id.casefold(), route.name.casefold()}
        ]
        if matches or re.fullmatch(r"[\w-]+", alias.strip()):
            return matches, remaining.lstrip(), True
    return [], text, False


def _declared_chat_routes(residents: Sequence[Resident]) -> Iterator[tuple[Resident, Route]]:
    """Yield every chat route of every active resident, in declared order."""
    for resident in active_residents(residents):
        for route in resident.manifest.routes:
            if route.kind == CHAT_ROUTE_KIND:
                yield resident, route


def _shared_bot_problems(
    routes: Sequence[ChatRoute], tokens: Mapping[str, str]
) -> dict[str, tuple[str, ...]]:
    """Refuse conflicting shared bot declarations before two pollers can steal updates."""
    groups: dict[str, list[ChatRoute]] = {}
    for route in routes:
        if route.address.transport == TELEGRAM:
            identity = tokens.get(route.address.token_env) or route.address.token_env
            groups.setdefault(identity, []).append(route)
    problems: dict[str, tuple[str, ...]] = {}
    for group in groups.values():
        if any(route.route.shared for route in group) and (
            not all(route.route.shared for route in group)
            or len({route.address.token_env for route in group}) > 1
        ):
            for route in group:
                problems[route.key] = (
                    (
                        "routes using a shared bot must all declare shared: true and the same "
                        "bot reference; use a different token for a dedicated bot"
                    ),
                )
    return problems


def chat_routes(residents: Sequence[Resident]) -> list[ChatRoute]:
    """Return every doorway an operator can actually reach, in declared order.

    Three things have to be true, and none of them is inferred: the route is
    ``kind: chat``, it is ``active`` (:attr:`steward.manifest.Route.accepts_chat`), and its
    address parses into a transport and a reference. A retired resident has closed every
    door it had, so :func:`steward.manifest.active_residents` filters it out first.

    Whether a *token* exists for the bot is deliberately not asked here: that is the
    environment's answer rather than the manifest's, it is what :func:`describe_chat` reports
    and what :meth:`ChatBridge.preflight` refuses on, and mixing the two would make "is this
    declared" unanswerable on a laptop that holds no tokens.
    """
    found: list[ChatRoute] = []
    for resident, route in _declared_chat_routes(residents):
        address = Address.parse(route.address)
        if not route.accepts_chat or address is None:
            continue
        found.append(ChatRoute(resident=resident, route=route, address=address))
    return found
