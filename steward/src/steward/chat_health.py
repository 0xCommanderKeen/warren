"""Chat route diagnostics shared by the bridge and the operator listing."""

import logging
import os
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from steward.chat_config import (
    DISCORD,
    OPERATORS_ENV,
    TELEGRAM,
    Address,
    _declared_chat_routes,
    _shared_bot_problems,
    chat_routes,
    operators_from_env,
    tokens_from_env,
)
from steward.chat_transcripts import chat_complaint
from steward.chat_transports import (
    ChatTransport,
    DiscordTransport,
    TelegramTransport,
)
from steward.manifest import Resident

log = logging.getLogger("steward.chat")


@dataclass(frozen=True, slots=True)
class RouteHealth:
    """Whether one route could carry a conversation when steward last asked, and when.

    The unit warren#456 broke the daemon's startup check into. A problem found here belongs
    to *this* route and says nothing about the one next door, so it closes one doorway
    instead of the whole burrow's — and because it is stamped with the moment it was
    measured, it can be asked again rather than believed for the life of the process.
    """

    problems: tuple[str, ...]
    checked_at: datetime

    @property
    def reachable(self) -> bool:
        """Report whether an operator could get an answer through this route right now."""
        return not self.problems


@dataclass(frozen=True, slots=True)
class ChatReport:
    """What one declared chat route is, and whether an operator could talk through it.

    A record rather than a dictionary, like :class:`steward.notify.NotificationReport`: the
    CLI renders it in two formats and a field renamed in one place should not be a
    ``KeyError`` discovered in the other.

    ``token_env`` is the *name* of the variable, and there is deliberately nowhere in this
    record for its value. Whether a token is set is the fact an operator needs; what it is
    belongs in the environment and nowhere a command could print it.
    """

    resident: str
    route: str
    address: str
    status: str
    token_env: str | None
    token_set: bool
    reachable: bool
    note: str | None
    bot: str | None = None
    shared: bool = False

    def to_dict(self) -> dict[str, Any]:
        """Render as the JSON object ``steward chat list --format json`` prints."""
        return {
            "resident": self.resident,
            "route": self.route,
            "address": self.address,
            "status": self.status,
            "token_env": self.token_env,
            "token_set": self.token_set,
            "reachable": self.reachable,
            "note": self.note,
            "bot": self.bot,
            "shared": self.shared,
        }


@dataclass(frozen=True, slots=True)
class RouteCheck:
    """What one look at a route found: its bot's name, if any, and what is wrong with it."""

    bot: str | None
    problems: tuple[str, ...]


def check_route(
    resident: Resident,
    address: Address,
    *,
    token: str | None,
    transport: ChatTransport | None,
    operators: frozenset[str],
) -> RouteCheck:
    """Return why this one route cannot carry a conversation, nearest thing to fix first.

    The single place the question is asked (warren#456). ``steward chat list`` renders the
    first problem as the route's note and the daemon shuts a route that has any, so a second
    copy of this cascade would drift the day one of them learned a new problem — and an
    operator reading a listing that disagrees with the log is worse off than one reading
    neither.

    Every problem here is a fact about *this* route and says nothing about the doorway next
    door, which is precisely why none of them may close it. They widen outwards: this bot,
    then this transport's operator list, then this resident's memory.
    """
    if transport is None:
        return RouteCheck(
            bot=None, problems=(f"steward cannot carry chat over {address.transport!r}",)
        )
    problems: list[str] = []
    bot = None
    if not token:
        problems.append(f"no token — set {address.token_env} to the token issued for {address}")
    elif address.transport == DISCORD:
        bot = _transport_identity(transport, token)
        if bot is None:
            problems.append(f"{address.token_env} could not identify a Discord bot")
    if not operators:
        problems.append(
            f"{OPERATORS_ENV} has no {address.transport!r} operators, so steward would "
            "answer nobody"
        )
    complaint = chat_complaint(resident.manifest)
    if complaint is not None:
        problems.append(complaint)
    return RouteCheck(bot=bot, problems=tuple(problems))


def describe_chat(
    residents: Sequence[Resident],
    env: Mapping[str, str] | None = None,
    *,
    transports: Mapping[str, ChatTransport] | None = None,
) -> list[ChatReport]:
    """Report every declared chat route and what still stands between it and a message.

    Deliberately wider than :func:`chat_routes`, exactly as
    :meth:`steward.notify.Notifier.describe` is wider than ``transport_for``: a ``pending``
    route is precisely the one an operator is in the middle of wiring up, and reporting
    nothing for it would make this command useless at the one moment it is needed.
    """
    tokens = tokens_from_env(env)
    source = os.environ if env is None else env
    available = transports or {
        TELEGRAM: TelegramTransport.from_env(source),
        DISCORD: DiscordTransport.from_env(source),
    }
    reports: list[ChatReport] = []
    conflicts = _shared_bot_problems(chat_routes(residents), tokens)
    for resident, route in _declared_chat_routes(residents):
        address = Address.parse(route.address)
        if address is None:
            reports.append(
                ChatReport(
                    resident=resident.id,
                    route=route.id,
                    address=route.address,
                    status=route.status,
                    token_env=None,
                    token_set=False,
                    reachable=False,
                    note=(
                        "address is not <transport>:<reference> — steward cannot tell which "
                        "bot this names"
                    ),
                )
            )
            continue
        token_set = address.token_env in tokens
        transport = available.get(address.transport)
        # A silent route is not checked any further: nothing polls a `pending` doorway, so
        # naming what its token would need to be is answering a question nobody asked.
        # Otherwise this listing and the daemon shut a route on exactly the same facts —
        # one :func:`check_route`, two readers. Room posting is deliberately not one of
        # those facts: an unknown channel name breaks ``posts_to``/``listens_in`` and
        # nothing else, so it is reported (`room`) without closing the door.
        check = (
            RouteCheck(
                bot=None,
                problems=(route.note or f"declared and silent: status is {route.status!r}",),
            )
            if transport is not None and not route.accepts_chat
            else check_route(
                resident,
                address,
                token=tokens.get(address.token_env),
                transport=transport,
                operators=operators_from_env(source, transport=address.transport),
            )
        )
        rooms = [*route.posts_to, *route.listens_in]
        problems = [*check.problems, *conflicts.get(f"{resident.id}/{route.id}", ())]
        room = (
            _room_complaint(transport, tokens[address.token_env], rooms, source)
            if transport is not None and check.bot is not None and rooms
            else None
        )
        reports.append(
            ChatReport(
                resident=resident.id,
                route=route.id,
                address=str(address),
                status=route.status,
                token_env=address.token_env,
                token_set=token_set,
                reachable=not problems,
                note=problems[0] if problems else (room or route.note),
                bot=check.bot,
                shared=route.shared,
            )
        )
    return reports


def _transport_identity(transport: ChatTransport, token: str) -> str | None:
    """Use an adapter's optional diagnostic capability without widening the wire seam."""
    identity = getattr(transport, "identity", None)
    if not callable(identity):
        return None
    try:
        value = identity(token)
    except Exception as exc:  # noqa: BLE001 — a diagnostic must not take down the CLI
        log.warning("%s identity check failed: %s", transport.name, exc)
        return None
    return value if isinstance(value, str) and value else None


def _room_complaint(
    transport: ChatTransport,
    token: str,
    channel_names: Sequence[str],
    env: Mapping[str, str],
) -> str | None:
    """Return what is wrong with this route's Discord rooms, or ``None``.

    Reported but never a closure (warren#456): an unknown name in ``posts_to`` or
    ``listens_in`` breaks that channel and leaves the conversation itself working, so it is
    a note beside a reachable route rather than one of :func:`check_route`'s problems.
    """
    guild = (env.get("STEWARD_CHAT_DISCORD_GUILD") or "").strip()
    if not guild:
        return "room posting is configured but STEWARD_CHAT_DISCORD_GUILD is not set"
    channels = _transport_channels(transport, token, guild)
    if channels is None:
        return "the configured Discord guild's channels could not be resolved"
    unknown = [name for name in channel_names if name not in channels]
    if unknown:
        return "unknown Discord channel name(s): " + ", ".join(unknown)
    return None


def _transport_channels(
    transport: ChatTransport, token: str, guild: str
) -> Mapping[str, str] | None:
    """Use Discord's optional room lookup without widening the two-method chat seam."""
    channels = getattr(transport, "channels", None)
    if not callable(channels) or not guild:
        return None
    try:
        value = channels(token, guild)
    except Exception as exc:  # noqa: BLE001 — a diagnostic must not take down the CLI
        log.warning("%s channel lookup failed: %s", transport.name, exc)
        return None
    return value if isinstance(value, Mapping) else None
