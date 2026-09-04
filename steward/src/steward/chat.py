"""The chat bridge: an operator says something, a resident answers, and that is all.

``routes: {kind: chat}`` has been descriptive since the manifest had routes at all — a
manifest could say "you can talk to me here" and nothing in steward listened. This module
is what makes it deliverable (warren#108): a daemon long-polls one bot per resident, and
every message from a named operator fires **one ordinary session** whose final message is
sent back as the reply.

**It only ever speaks when spoken to.** There is no outbound half here and there is not
going to be one: steward tapping a person on the shoulder — a ``needs_human`` at 2am, a
task that finished — is a *notification*, it goes through :mod:`steward.notify`, and it is
one-way by construction (warren#114). A bridge that grew unprompted messages would be two
channels wearing one name.

**One bot per resident, and no secret in the manifest.** The route's ``address`` is a
reference — ``telegram:pip`` — and the token BotFather issued lives in steward's own
environment under :data:`TOKEN_ENV_PREFIX` plus the reference (``STEWARD_CHAT_TOKEN_PIP``).
That is the same division every other credential in this system lives on: manifests are git,
and a manifest is exactly where a bot token must never be. It also means the mapping is
readable from the manifest alone — an operator who can see ``address: telegram:pip`` knows
which variable to set without consulting anything else.

**Only named operators are answered.** :data:`OPERATORS_ENV` is a comma-separated list of
Telegram user ids. A message from anybody else is dropped with **no reply at all** — an
answer, even a refusal, tells a scanner that the bot is live and that something is behind
it — and recorded as a :data:`steward.events.CHAT_MESSAGE_DROPPED` event, so the operator
can find out that somebody knocked. A message in a group chat is dropped the same way even
when an operator sent it: the reply would be readable by everyone else in that group, and a
resident's answers are not a broadcast.

    Chronicle accepts that type as of warren#276 and treats it as ambient evidence: the
    drop becomes a bounded ``diagnostics`` record naming the door and who knocked, and it
    never animates the resident's villager — a stranger's message is not a sign of life
    (``docs/chat.md``).

**A busy resident says so.** The bridge takes the same cross-process claim every other
firing process takes (:mod:`steward.claims`, warren#111). A refused claim means the
scheduler, a dispatch or another conversation has that resident right now — so the operator
is told, in the conversation, and the message is *not* queued. This is the API's 409 in
sentence form and it is the same judgement: a person asking for something now cannot be
given it later and told it was now, and a queue would turn "answer me" into a backlog of
sessions answering questions the operator has stopped caring about.

**The conversation is a window, not a history.** Each resident keeps a rolling transcript
per conversation in its own memory directory (:class:`Transcript`), the last few turns of
which are injected into the prompt as *context*, ahead of the charter and beneath it in
authority. It survives restarts because it is a file, it is inspectable for the same reason,
and it is bounded because a prompt paid for on every message must be.

**Every message is a run like any other.** Same admission, same budget, same runner seam,
same run registry row, same ``routine_started``/``routine_finished`` bracket in the village
— under the trigger ``chat``, and the ledger kind ``chat``, so what a resident spent
answering its operator is a number somebody can look up. Nothing here launches a process:
that is :mod:`steward.runners`, reached through :class:`steward.sessions.ResidentSessions`
exactly as the scheduler and the board reach it.
"""

import contextlib
import fcntl
import json
import logging
import os
import re
import time
import urllib.error
import urllib.request
import uuid
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any, Protocol

from steward import events as ev
from steward.claims import ClaimRefused, ResidentClaims
from steward.deploy import memory_host_dir
from steward.manifest import (
    CHAT_ROUTE_KIND,
    Resident,
    ResidentManifest,
    Route,
    active_residents,
    redact_secrets,
    validate_path,
)
from steward.prompt import TRANSCRIPT_MAX_CHARS
from steward.run_lifecycle import RunTransitions, event_log_path, new_owner_token
from steward.runners import RunResult, build_runner
from steward.runs import RUN_CHAT, TRIGGER_CHAT
from steward.scheduler import WakeHooks, default_state_path
from steward.session_auth import new_session_credential
from steward.sessions import (
    DEFAULT_CHAT_TIMEOUT_S,
    Admission,
    ChatWake,
    Refusal,
    ResidentSessions,
    RunGuard,
    RunnerFactory,
)
from steward.skills import SkillLibrary, library_for
from steward.store import Store

__all__ = [
    "API_URL_ENV",
    "CHAT_DIR",
    "DEFAULT_API_URL",
    "DEFAULT_CATCHUP_S",
    "KNOCK_DOORS_TRACKED",
    "OPERATORS_ENV",
    "POLL_TIMEOUT_ENV",
    "REPLY_MAX_CHARS",
    "SEND_TIMEOUT_S",
    "TOKEN_ENV_PREFIX",
    "TRANSCRIPT_KEEP_TURNS",
    "TRANSCRIPT_WINDOW_TURNS",
    "Address",
    "ChatBridge",
    "ChatError",
    "ChatOutcome",
    "ChatReport",
    "ChatRoute",
    "ChatStatus",
    "ChatTransport",
    "Drop",
    "KnockLimiter",
    "Message",
    "TelegramTransport",
    "Transcript",
    "Turn",
    "chat_complaint",
    "chat_routes",
    "describe_chat",
    "operators_from_env",
    "poll_timeout_from_env",
    "resolve_chat_dir",
    "token_env_name",
    "token_env_names",
    "tokens_from_env",
]

log = logging.getLogger("steward.chat")


def _utcnow() -> datetime:
    """Read the wall clock, in UTC. What a message's age is measured against."""
    return datetime.now(UTC)


#: The transport a ``telegram:…`` address names. One today, and the module is written so a
#: second is a class rather than a rewrite — but v0 ships one, because a chat kind nobody
#: has a bot for is a vocabulary rather than a feature.
TELEGRAM = "telegram"

#: Where Telegram's bot API lives. Overridable for exactly one reason: the test suite points
#: it at a local server, so nothing in this repo can reach the real API. Mirrors
#: :data:`steward.notify.NTFY_URL_ENV`, and for the same reason.
DEFAULT_API_URL = "https://api.telegram.org"
API_URL_ENV = "STEWARD_CHAT_API_URL"

#: What every per-bot token variable starts with. The rest is the address's reference,
#: upper-cased: ``telegram:pip`` is ``STEWARD_CHAT_TOKEN_PIP``. Keyed on the *reference*
#: rather than on the resident id, because the reference is what the manifest declares —
#: renaming a resident should not silently disconnect its bot, and reading the route should
#: be enough to know which variable to set.
TOKEN_ENV_PREFIX = "STEWARD_CHAT_TOKEN_"  # noqa: S105 — a variable-name prefix, not a secret

#: Who steward answers: a comma-separated list of Telegram user ids. Empty or unset means
#: nobody, which means the bridge refuses to start rather than answering the world.
OPERATORS_ENV = "STEWARD_CHAT_OPERATORS"

#: How long one ``getUpdates`` call may wait for a message before coming back empty. Long
#: polling is what keeps a bridge that answers instantly from costing a request a second.
POLL_TIMEOUT_ENV = "STEWARD_CHAT_POLL_TIMEOUT_S"
DEFAULT_POLL_TIMEOUT_S = 25.0

#: How much longer than the long poll steward waits on the socket before giving up. The
#: server holds the connection for the whole poll timeout by design, so a socket timeout at
#: or below it would turn every idle minute into a stream of invented failures.
HTTP_TIMEOUT_MARGIN_S = 10.0

#: How long sending one reply may take. Five times the two seconds a notification tap gets
#: (:data:`steward.notify.NTFY_TIMEOUT_S`), because the two are paid for by different
#: people: a tap runs inside a durable transition and may not cost a run, while this runs
#: in a daemon whose entire job is this message, after a session that already took minutes.
#: Bounded all the same — a hung socket must not stop the daemon answering the next message.
SEND_TIMEOUT_S = 10.0

#: How long a bridge waits after a pass in which some bot could not be reached, so an API
#: that is down costs one request every few seconds rather than a spin.
UNREACHABLE_SLEEP_S = 5.0

#: How long a bridge waits after an idle pass. Small, because a long poll has already done
#: the waiting; it exists so a transport that answers instantly cannot spin.
IDLE_SLEEP_S = 1.0

#: How late a message may be and still be answered. The scheduler's number and the
#: scheduler's reason (:data:`steward.scheduler.DEFAULT_CATCHUP_S`): Telegram holds
#: undelivered updates for a day, so a bridge that was down all night would otherwise come
#: up and fire a session for every message that arrived while nobody was listening —
#: answering questions the operator asked, gave up on, and has since answered themselves.
#: Unlike a missed routine, this one is said out loud: a person sent the message, so they
#: are told it went unanswered rather than left watching a bot that never replies.
DEFAULT_CATCHUP_S = 300.0

#: How many (door, stranger) pairs the knock limiter counts at once. The limiter forgets a
#: pair as soon as its window closes, so this bound is only ever reached inside a single
#: pass — by a scanner rotating sender ids faster than steward can sweep. It exists because
#: a map an outsider fills is the same class of problem this whole limiter is about: the
#: fix must not become the leak (warren#278).
KNOCK_DOORS_TRACKED = 4096

#: Where a resident's conversations live inside its memory directory.
CHAT_DIR = "chat"

#: How many turns of one conversation survive on disk. Ten exchanges: enough to read back
#: what happened this morning, and bounded by construction like the journal is.
TRANSCRIPT_KEEP_TURNS = 20

#: How many of those go into the prompt. Fewer than are kept, because the file is for a
#: person scrolling back and the window is paid for on every single message.
TRANSCRIPT_WINDOW_TURNS = 10

#: How long one reply may be. Telegram refuses anything past 4096 characters, and a reply
#: that long is a report rather than an answer; the cap is applied *after* redaction, which
#: is the repo rule (steward #65) and matters most here — this text goes to a phone.
REPLY_MAX_CHARS = 3500

#: What steward calls the operator in a transcript. Not their user id: the file lives in the
#: resident's memory directory, is injected into prompts, and a numeric identity in it would
#: be a fact about a person that the conversation never needed.
OPERATOR_SPEAKER = "operator"

_ADDRESS = re.compile(r"^(?P<transport>[a-z][a-z0-9+.-]*):(?P<reference>\S.*)$")
_UNSAFE_IN_NAME = re.compile(r"[^A-Za-z0-9_-]+")
_ENV_NAME_UNSAFE = re.compile(r"[^A-Z0-9]+")

#: How long a conversation's file name may be before steward shortens it. Telegram's own
#: ids are far shorter; the bound exists so nothing a transport invents can produce a name
#: a filesystem refuses.
SLUG_MAX_CHARS = 64


class ChatError(Exception):
    """Raised when the bridge refuses to start — loudly, in daylight."""


# --------------------------------------------------------------------------------------
# where a bot's token comes from
# --------------------------------------------------------------------------------------


def token_env_name(reference: str) -> str:
    """Return the environment variable holding the token for one address reference.

    Upper-cased, with anything that is not a letter or a digit folded to ``_`` — so
    ``telegram:polica-librarian`` reads its token from
    ``STEWARD_CHAT_TOKEN_POLICA_LIBRARIAN``. The folding is deliberate rather than a
    restriction on references: a variable name is not allowed to contain a hyphen, and an
    operator should not have to know that to name their route.
    """
    folded = _ENV_NAME_UNSAFE.sub("_", reference.upper()).strip("_")
    return f"{TOKEN_ENV_PREFIX}{folded}"


def token_env_names(env: Mapping[str, str] | None = None) -> list[str]:
    """Return every configured bot-token variable name, including empty slots."""
    source = os.environ if env is None else env
    return sorted(name for name in source if name.startswith(TOKEN_ENV_PREFIX))


def tokens_from_env(env: Mapping[str, str] | None = None) -> dict[str, str]:
    """Return every bot token in the environment, keyed by the variable that held it.

    Keyed by variable name rather than by reference so the lookup is exact: two references
    that fold to one variable name are the same bot as far as this module is concerned, and
    :func:`describe_chat` is where that shows up as something an operator can read.
    """
    source = os.environ if env is None else env
    names = set(token_env_names(source))
    return {
        name: value.strip() for name, value in source.items() if name in names and value.strip()
    }


def operators_from_env(env: Mapping[str, str] | None = None) -> frozenset[str]:
    """Return the Telegram user ids steward will answer. Empty means nobody.

    Empty is a real answer and the safe one: a bridge with no operators answers no message
    from anyone, and refuses to start rather than run as an open door
    (:meth:`ChatBridge.require_ready`).
    """
    source = os.environ if env is None else env
    raw = (source.get(OPERATORS_ENV) or "").strip()
    return frozenset(part.strip() for part in raw.split(",") if part.strip())


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
        return token_env_name(self.reference)

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
    def name(self) -> str:
        """What to call this resident *to its operator*: the soul's name, not the id.

        The same judgement :func:`steward.notify.tap_for` makes about a knock on a phone —
        "Pip is busy right now" is a sentence and ``pip/chat: busy`` is a log line, and this
        text is read by a person in a chat window.
        """
        return self.resident.manifest.soul.name


def _declared_chat_routes(residents: Sequence[Resident]) -> Iterator[tuple[Resident, Route]]:
    """Yield every chat route of every active resident, in declared order."""
    for resident in active_residents(residents):
        for route in resident.manifest.routes:
            if route.kind == CHAT_ROUTE_KIND:
                yield resident, route


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
        }


def describe_chat(
    residents: Sequence[Resident], env: Mapping[str, str] | None = None
) -> list[ChatReport]:
    """Report every declared chat route and what still stands between it and a message.

    Deliberately wider than :func:`chat_routes`, exactly as
    :meth:`steward.notify.Notifier.describe` is wider than ``transport_for``: a ``pending``
    route is precisely the one an operator is in the middle of wiring up, and reporting
    nothing for it would make this command useless at the one moment it is needed.
    """
    tokens = tokens_from_env(env)
    reports: list[ChatReport] = []
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
        note = route.note
        if address.transport != TELEGRAM:
            note = f"transport {address.transport!r} is not one steward can carry chat over"
        elif not route.accepts_chat:
            note = note or f"declared and silent: status is {route.status!r}"
        elif not token_set:
            note = f"no token: set {address.token_env} to the token BotFather issued"
        reports.append(
            ChatReport(
                resident=resident.id,
                route=route.id,
                address=str(address),
                status=route.status,
                token_env=address.token_env,
                token_set=token_set,
                reachable=route.accepts_chat and token_set and address.transport == TELEGRAM,
                note=note,
            )
        )
    return reports


# --------------------------------------------------------------------------------------
# the rolling transcript
# --------------------------------------------------------------------------------------


def chat_complaint(manifest: ResidentManifest) -> str | None:
    """Return why this resident cannot keep a transcript, or ``None`` when it can.

    Pure, like :func:`steward.journal.journal_complaint`: it reads the declaration and never
    the filesystem, so the bridge can refuse at startup on a machine that has never seen the
    resident's volume rather than discovering it mid-conversation.
    """
    memory = manifest.memory
    if memory.kind == "file":
        return (
            "memory.kind is 'file', so there is nowhere to keep a conversation; a chat "
            "route needs memory.kind 'directory' or 'repo'"
        )
    if "://" in memory.path:
        return (
            f"memory.path {memory.path!r} is a remote reference; a transcript is read and "
            f"written as an ordinary file, so it needs a local directory"
        )
    return None


def resolve_chat_dir(manifest: ResidentManifest) -> Path:
    """Return the directory this resident's conversations live in.

    ``<memory>/chat``, on the *host* side of the mount for a container-placed resident —
    the same base :func:`steward.journal.resolve_journal_dir` resolves against, so a
    resident's memory has one location and steward writes to the side it can actually see
    (steward #58). The session inside the container reads the same files at
    ``<memory.path>/chat`` through the bind mount.
    """
    complaint = chat_complaint(manifest)
    if complaint is not None:
        raise ChatError(f"{manifest.id}: {complaint}")
    return memory_host_dir(manifest) / CHAT_DIR


def conversation_slug(conversation: str) -> str:
    """Return a file name for one conversation, from an id steward did not choose.

    Everything that is not a letter, a digit, ``_`` or ``-`` is folded away, so nothing a
    transport hands over can climb out of the chat directory or name a file the filesystem
    refuses. A Telegram chat id is an integer and survives this unchanged, negative sign
    included.
    """
    slug = _UNSAFE_IN_NAME.sub("_", conversation.strip())[:SLUG_MAX_CHARS].strip("_")
    return slug or "unknown"


@dataclass(frozen=True, slots=True)
class Turn:
    """One thing that was said, by one side, at one moment."""

    at: str
    speaker: str
    text: str

    def render(self) -> str:
        """Render this turn the way it appears in a prompt."""
        return f"{self.speaker}: {self.text}"


@dataclass(frozen=True, slots=True)
class Transcript:
    """One conversation, kept as a rolling file in the resident's own memory directory.

    JSON lines rather than the markdown the journal uses, and the difference is who writes
    them. A journal entry is a resident's own prose, written by the session, read by a
    person; a transcript is *steward's* record of an exchange, written a turn at a time and
    read back into a prompt, and a format that can hold any text without a parser having to
    guess where one turn ends is worth more here than prettiness. It still reads fine in a
    terminal, which is the property the issue actually asked for.

    Bounded by construction, like the journal: every append rotates the file down to
    :data:`TRANSCRIPT_KEEP_TURNS` turns, so nothing here grows without limit and no cron job
    has to remember to trim it.
    """

    manifest: ResidentManifest
    conversation: str
    keep: int = TRANSCRIPT_KEEP_TURNS

    @property
    def path(self) -> Path:
        """The file this conversation lives in."""
        return resolve_chat_dir(self.manifest) / f"{conversation_slug(self.conversation)}.jsonl"

    def turns(self) -> list[Turn]:
        """Return every surviving turn, oldest first. An unreadable line is skipped.

        Broad on purpose, the way :func:`steward.journal._parse_entry` is: this file is read
        at the top of a session, and a byte somebody corrupted must degrade to "less context"
        rather than to a resident that cannot be talked to at all.
        """
        try:
            raw = self.path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return []
        found: list[Turn] = []
        for line in raw.splitlines():
            if not line.strip():
                continue
            try:
                value = json.loads(line)
            except ValueError:
                continue
            if not isinstance(value, dict):
                continue
            speaker = str(value.get("speaker") or "")
            text = str(value.get("text") or "")
            if not speaker or not text:
                continue
            found.append(Turn(at=str(value.get("at") or ""), speaker=speaker, text=text))
        return found

    def window(self, turns: int = TRANSCRIPT_WINDOW_TURNS) -> list[Turn]:
        """Return the last few turns: what the next session is opened with."""
        return self.turns()[-turns:] if turns > 0 else []

    def render(self) -> str:
        """Render the window as the text injected into a prompt, oldest first.

        Bounded here as well as at injection, and from the *newest* end: a window cut from
        the front by the prompt's cap would drop the turn the operator just referred to and
        keep the one from an hour ago, which is the wrong half to lose.
        """
        rendered = [turn.render() for turn in self.window()]
        text = "\n".join(rendered)
        while len(text) > TRANSCRIPT_MAX_CHARS and rendered:
            rendered.pop(0)
            text = "\n".join(rendered)
        return text

    def append(self, speaker: str, text: str, *, now: datetime | None = None) -> None:
        """Record one turn and rotate. Never raises: a lost line is not a failed answer."""
        if not text.strip():
            return
        turn = Turn(at=ev.utc_now_iso(now), speaker=speaker, text=text.strip())
        try:
            existing = self.turns()
            path = self.path
            path.parent.mkdir(parents=True, exist_ok=True)
            kept = [*existing, turn][-self.keep :] if self.keep > 0 else []
            body = "".join(
                json.dumps(
                    {"at": item.at, "speaker": item.speaker, "text": item.text},
                    ensure_ascii=False,
                )
                + "\n"
                for item in kept
            )
            path.write_text(body, encoding="utf-8")
        except OSError as exc:
            log.warning(
                "%s: could not record a chat turn in %s: %s",
                self.manifest.id,
                self.conversation,
                exc,
            )


# --------------------------------------------------------------------------------------
# the wire
# --------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Message:
    """One inbound message, in the only shape this module cares about."""

    #: The transport's own cursor for this message. What the next poll acknowledges.
    update_id: int
    #: The conversation it belongs to: what a reply is addressed to, and what the
    #: transcript is filed under.
    conversation: str
    #: Who sent it, as the transport names them. Checked against the operator list.
    sender: str
    text: str
    at: datetime
    #: Whether this is a one-to-one conversation. A group is never answered, whoever spoke.
    private: bool = True

    def age_s(self, now: datetime) -> float:
        """How long ago this was sent, in seconds."""
        return (now - self.at).total_seconds()


class ChatTransport(Protocol):
    """Anything that can carry a conversation. Telegram is the first and only one today.

    Two methods and no state the bridge can see, so the whole of "how a message travels" is
    behind one seam: a test injects a fake, and a second transport is a class rather than a
    rewrite. Neither method raises — an unreachable API is a reported fact, not an exception
    that takes a daemon down.
    """

    @property
    def name(self) -> str:
        """The transport a route's address names to select this."""
        ...

    def poll(self, token: str, offset: int) -> list[Message] | None:
        """Return the messages waiting past ``offset``, or ``None`` if it is unreachable."""
        ...

    def send(self, token: str, conversation: str, text: str) -> bool:
        """Deliver one reply. Returns whether it landed; never raises."""
        ...


@dataclass
class TelegramTransport:
    """Telegram's bot API: long-polled ``getUpdates`` in, ``sendMessage`` out.

    Both are POSTs with JSON bodies rather than the query strings the API also accepts, for
    one reason worth stating: the token is in the URL path either way, and putting the
    *arguments* there as well would mean a message's text ends up in whatever logs a proxy or
    a library keeps. Nothing here ever logs a URL, for the same reason — the path carries the
    bot's credential, and a warning that leaked it would be worse than the failure it
    reported.

    No webhook, and that is a deployment property rather than a preference (warren#108): long
    polling means the NAS opens outbound connections and nothing on the internet needs a way
    in.
    """

    base_url: str = DEFAULT_API_URL
    poll_timeout_s: float = DEFAULT_POLL_TIMEOUT_S
    send_timeout_s: float = SEND_TIMEOUT_S

    @property
    def name(self) -> str:
        """The address transport that selects this."""
        return TELEGRAM

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> TelegramTransport:
        """Build a transport from the environment. No manifest ever carries any of this."""
        source = os.environ if env is None else env
        return cls(
            base_url=((source.get(API_URL_ENV) or "").strip() or DEFAULT_API_URL).rstrip("/"),
            poll_timeout_s=poll_timeout_from_env(source),
        )

    def poll(self, token: str, offset: int) -> list[Message] | None:
        """Long-poll one bot for what it has been sent since ``offset``.

        ``allowed_updates`` asks for messages and nothing else: v0 is text in and text out,
        and an edited message, a reaction or a button press arriving here would be a fact
        steward has no honest answer to.
        """
        payload: dict[str, Any] = {
            "timeout": int(self.poll_timeout_s),
            "allowed_updates": ["message"],
        }
        if offset:
            payload["offset"] = offset
        result = self._call(
            token,
            "getUpdates",
            payload,
            timeout_s=self.poll_timeout_s + HTTP_TIMEOUT_MARGIN_S,
        )
        if result is None:
            return None
        if not isinstance(result, list):
            log.warning("telegram answered getUpdates with something that is not a list")
            return None
        return [message for update in result if (message := _message_from(update)) is not None]

    def send(self, token: str, conversation: str, text: str) -> bool:
        """Send one reply into a conversation. Never raises, and never waits long."""
        if not text.strip():
            return False
        sent = self._call(
            token,
            "sendMessage",
            {"chat_id": conversation, "text": text},
            timeout_s=self.send_timeout_s,
        )
        return sent is not None

    def _call(
        self, token: str, method: str, payload: Mapping[str, Any], *, timeout_s: float
    ) -> Any | None:  # noqa: ANN401 — the API's own result shape differs per method
        """Call one bot API method and return its ``result``, or ``None`` on any failure."""
        url = f"{self.base_url}/bot{token}/{method}"
        if not url.startswith(("http://", "https://")):
            log.warning("the chat API target is not an http(s) URL; nothing can be delivered")
            return None
        request = urllib.request.Request(  # noqa: S310 — scheme checked just above
            url,
            data=json.dumps(dict(payload), ensure_ascii=False).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout_s) as response:  # noqa: S310
                body = json.loads(response.read().decode("utf-8"))
        except (OSError, urllib.error.URLError, ValueError) as exc:
            # Never the URL: it carries the bot's token. The method name and the error class
            # are what a person debugging this actually needs.
            log.warning("telegram %s failed — %s: %s", method, type(exc).__name__, exc)
            return None
        if not isinstance(body, dict) or not body.get("ok"):
            log.warning(
                "telegram refused %s: %s",
                method,
                redact_secrets(str(body.get("description", "no reason given")))
                if isinstance(body, dict)
                else "unreadable answer",
            )
            return None
        return body.get("result")


def _message_from(update: object) -> Message | None:
    """Read one Telegram update into a :class:`Message`, or ignore it.

    Ignored rather than refused: an update steward cannot read is one it has no answer for,
    and the poll's job is to hand over the messages it understood, not to fail on the ones it
    did not. A non-text message — a photo, a sticker, a voice note — is one of those.
    """
    if not isinstance(update, Mapping):
        return None
    update_id = update.get("update_id")
    message = update.get("message")
    if not isinstance(update_id, int) or not isinstance(message, Mapping):
        return None
    chat = message.get("chat")
    sender = message.get("from")
    text = message.get("text")
    date = message.get("date")
    if not isinstance(chat, Mapping) or not isinstance(sender, Mapping):
        return None
    if not isinstance(text, str) or not text.strip():
        return None
    conversation = chat.get("id")
    sender_id = sender.get("id")
    if conversation is None or sender_id is None:
        return None
    try:
        at = datetime.fromtimestamp(float(date), tz=UTC) if date is not None else datetime.now(UTC)
    except TypeError, ValueError, OSError, OverflowError:
        at = datetime.now(UTC)
    return Message(
        update_id=update_id,
        conversation=str(conversation),
        sender=str(sender_id),
        text=text,
        at=at,
        private=chat.get("type") == "private",
    )


# --------------------------------------------------------------------------------------
# what one message came to
# --------------------------------------------------------------------------------------


class ChatStatus(StrEnum):
    """What became of one message. Nothing here is a guess.

    A closed vocabulary rather than seven loose strings, for the reason
    :class:`steward.runners.Outcome` is one: three places read these — the outcome's own
    ``ran``, the CLI's colours, and every test — and a typo in any of them would be a branch
    that silently never fires.
    """

    #: A session answered it, whatever the session concluded.
    ANSWERED = "answered"
    #: A session ran and did not finish on its own terms. The operator was told which.
    FAILED = "failed"
    #: From somebody steward does not answer. **No reply was sent**, deliberately.
    DROPPED = "dropped"
    #: The resident is running something else right now, so the operator was told so.
    BUSY = "busy"
    #: The resident may not run at all — paused, or with nowhere to run.
    REFUSED = "refused"
    #: It arrived while nothing was listening and is too old to answer honestly.
    STALE = "stale"
    #: A bot steward could not reach at all this pass. Not about a message; about a doorway.
    UNREACHABLE = "unreachable"


@dataclass(frozen=True, slots=True)
class ChatOutcome:
    """What one message came to. Every field is something that happened."""

    resident_id: str
    route: str
    status: ChatStatus
    conversation: str = ""
    run_id: str = ""
    reason: str | None = None
    #: What was sent back, already redacted and bounded. Empty when nothing was.
    reply: str = ""

    @property
    def ran(self) -> bool:
        """True when a session actually opened for this message."""
        return self.status in (ChatStatus.ANSWERED, ChatStatus.FAILED)


# --------------------------------------------------------------------------------------
# how loudly a stranger may knock
# --------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Drop:
    """One record about knocks at one door, and how many of them it stands for."""

    route: ChatRoute
    sender: str
    reason: str
    #: How many *other* knocks this record stands for, beyond itself. Zero for an ordinary
    #: knock; the rest of a window's worth on the record that closes it.
    suppressed: int = 0


@dataclass(slots=True)
class _Window:
    """One stranger's knocking at one door for one reason, since the knock that was told."""

    route: ChatRoute
    sender: str
    reason: str
    opened_at: datetime
    swallowed: int = 0


class KnockLimiter:
    """One event per stranger per door per window, and a count of what that stood for.

    A drop is the one event in this system an *outsider* causes: every other line in the
    log is there because the fleet did something. Nothing bounded it (warren#278), and the
    channels it lands in are small — chronicle keeps the newest 200 diagnostics and the
    newest events per agent — so a scanner that finds a resident's bot and sends a few
    hundred messages could push that resident's own tools, tasks and knocks out of the
    village, and push the projection's own complaints out of the page an operator reads.
    Not data loss, but an outsider deciding what an operator can see, which is precisely
    what the projection is otherwise careful about.

    **The count is the point.** Swallowing the storm outright would trade one problem for
    the opposite one: a flood is *more* interesting than a single knock, and a limiter that
    turned two hundred messages into one indistinguishable record would hide the very thing
    worth noticing. So a suppressed knock is counted, and the count rides out on the record
    that closes its window — which is why :meth:`sweep` exists rather than only
    :meth:`admit`: a storm that stops must still be reported, and waiting for the next
    knock to carry it could wait for ever.

    The window is the bridge's catch-up window (:data:`DEFAULT_CATCHUP_S`) for the reason
    the catch-up window is that number: it is how long steward already considers "now" in a
    conversation.
    """

    def __init__(self, window_s: float, doors: int = KNOCK_DOORS_TRACKED) -> None:
        """Count knocks over ``window_s`` seconds, across at most ``doors`` windows at once."""
        self.window_s = window_s
        self.doors = doors
        self._windows: dict[tuple[str, str, str], _Window] = {}
        self._evicted: list[Drop] = []

    def __len__(self) -> int:
        """How many (door, stranger, reason) windows are open right now."""
        return len(self._windows)

    def admit(self, route: ChatRoute, sender: str, reason: str, now: datetime) -> int | None:
        """Return what to record for this knock, or ``None`` to record nothing.

        The number is ``suppressed``: how many knocks the event this admits stands for
        beyond itself. It is zero for an ordinary knock and non-zero only when a window ran
        out without anybody sweeping it, so that reopening it cannot look innocent.

        The key is one field wider than warren#278 asked for — the *reason* joins the door
        and the sender — because there are exactly two of them (a stranger, and a group
        chat), one person can manage both, and townhall folds knocks by reason. Counting
        them together would put a group chat's tally on the line that says "not an
        operator". Two records per stranger per window instead of one is not a volume this
        limiter exists to stop.
        """
        key = (route.key, sender, reason)
        window = self._windows.get(key)
        if window is not None and not self._closed(window, now):
            window.swallowed += 1
            return None
        if window is None:
            self._make_room()
        self._windows[key] = _Window(route=route, sender=sender, reason=reason, opened_at=now)
        return 0 if window is None else window.swallowed

    def sweep(self, now: datetime) -> list[Drop]:
        """Close every window that has run out, and report the ones that swallowed anything.

        Called once per pass, which is what keeps two promises at once: a storm becomes
        visible within a window of ending rather than whenever somebody knocks next, and a
        window that stopped is forgotten rather than counted for ever. Windows the door
        bound forced out come back here too, so no count is ever lost to a log line.
        """
        drops, self._evicted = self._evicted, []
        for key, window in list(self._windows.items()):
            if not self._closed(window, now):
                continue
            del self._windows[key]
            drops.extend(self._closing(window))
        return drops

    def _closing(self, window: _Window) -> list[Drop]:
        """Return what one window that is going away still owes the village."""
        if not window.swallowed:
            return []
        # The record *is* one of the knocks it stands for — the last one — which is why it
        # carries that window's reason and stands for one fewer than were swallowed.
        return [
            Drop(
                route=window.route,
                sender=window.sender,
                reason=window.reason,
                suppressed=window.swallowed - 1,
            )
        ]

    def _closed(self, window: _Window, now: datetime) -> bool:
        return (now - window.opened_at).total_seconds() >= self.window_s

    def _make_room(self) -> None:
        """Forget the oldest window when a pass brings more strangers than the bound allows.

        Only reachable inside one pass — :meth:`sweep` drops every closed window at the end
        of each one — and only by somebody rotating sender ids, whose every new id is
        recorded as its own knock anyway. What it had swallowed is handed to the next sweep
        rather than dropped: a bound meant to stop a flood becoming a silence must not be
        the thing that makes one.
        """
        if len(self._windows) < self.doors:
            return
        oldest = min(self._windows, key=lambda key: self._windows[key].opened_at)
        forgotten = self._windows.pop(oldest)
        self._evicted.extend(self._closing(forgotten))
        log.warning(
            "%s: the knock limiter is already counting %d windows, so %s is forgotten; "
            "somebody is rotating sender ids",
            oldest[0],
            self.doors,
            forgotten.sender,
        )


# --------------------------------------------------------------------------------------
# the bridge
# --------------------------------------------------------------------------------------


@dataclass
class ChatBridge:
    """Polls each resident's bot, answers its operator, and records what happened.

    Assembled from the same collaborators every other firing surface uses — the shared
    session lifecycle, the budget guard, the run registry, the cross-process claim — because
    a chat session must be an ordinary session in every respect except how it was asked for.
    """

    routes: Sequence[ChatRoute]
    store: Store
    tokens: Mapping[str, str] = field(default_factory=dict, repr=False)
    operators: frozenset[str] = frozenset()
    transport: ChatTransport = field(default_factory=TelegramTransport.from_env)
    emitter: ev.Emitter = field(default_factory=ev.NullEmitter)
    workdir: Path = field(default_factory=Path.cwd)
    library: SkillLibrary = field(default_factory=SkillLibrary)
    runner_factory: RunnerFactory = build_runner
    guard: RunGuard | None = None
    #: The board, reached for exactly two things: the decisions a resident is owed at the
    #: top of a session, and the dispatch sweep after one. Both are the scheduler's hooks,
    #: unchanged — a delegation written mid-conversation is handed over before the operator
    #: has finished reading the reply, instead of on the next hourly tick.
    hooks: WakeHooks | None = None
    claims: ResidentClaims | None = None
    clock: Callable[[], datetime] = _utcnow
    #: How long a chat session gets before the budget has its say.
    timeout_s: int = DEFAULT_CHAT_TIMEOUT_S
    catchup_s: float = DEFAULT_CATCHUP_S
    idle_sleep_s: float = IDLE_SLEEP_S
    state_path: Path | None = None
    sessions: ResidentSessions = field(init=False, repr=False)
    run_transitions: RunTransitions = field(init=False, repr=False)
    #: What stops a stranger deciding how much of the village an operator can see.
    knocks: KnockLimiter = field(init=False, repr=False)
    _offsets: dict[str, int] = field(default_factory=dict, init=False, repr=False)

    def __post_init__(self) -> None:
        """Build the shared lifecycle from the bridge's existing dependencies."""
        if self.claims is None:
            self.claims = ResidentClaims(self.store)
        self.knocks = KnockLimiter(self.catchup_s)
        self.sessions = ResidentSessions(
            workdir=self.workdir,
            runner_factory=self.runner_factory,
            library=self.library,
            guard=self.guard,
            hooks=self.hooks,
            residents=[route.resident for route in self.routes],
            clock=self.clock,
            emitter=self.emitter,
        )
        self.run_transitions = RunTransitions(self.store)

    @classmethod
    def from_path(  # noqa: PLR0913 — every knob is keyword-only and independently useful
        cls,
        residents_dir: Path | str,
        store: Store,
        *,
        env: Mapping[str, str] | None = None,
        emitter: ev.Emitter | None = None,
        workdir: Path | None = None,
        library: SkillLibrary | None = None,
        skills_dir: Path | str | None = None,
        guard: RunGuard | None = None,
        hooks: WakeHooks | None = None,
        claims: ResidentClaims | None = None,
        transport: ChatTransport | None = None,
        state_path: Path | None = None,
        catchup_s: float = DEFAULT_CATCHUP_S,
    ) -> ChatBridge:
        """Build a bridge over the reachable chat routes of a residents tree.

        An invalid tree never reaches the bridge, for the reason it never reaches the
        scheduler or the board: a resident steward cannot read is a resident steward will
        not run — and it certainly will not let a person talk to one.
        """
        result = validate_path(residents_dir, skills_dir)
        if not result.ok:
            raise ChatError(
                "cannot open a chat bridge over an invalid residents tree:\n"
                + "\n".join(d.render() for d in result.errors)
            )
        source = os.environ if env is None else env
        return cls(
            routes=chat_routes(list(result.residents)),
            store=store,
            tokens=tokens_from_env(source),
            operators=operators_from_env(source),
            transport=transport or TelegramTransport.from_env(source),
            emitter=emitter or ev.EventEmitter.from_env(),
            workdir=workdir if workdir is not None else Path.cwd(),
            library=library if library is not None else library_for(residents_dir, skills_dir),
            guard=guard,
            hooks=hooks,
            claims=claims,
            state_path=state_path,
            catchup_s=catchup_s,
        )

    # -- startup ---------------------------------------------------------------------

    def token_for(self, route: ChatRoute) -> str | None:
        """Return the bot token for one route, or ``None`` when nobody has set it."""
        return self.tokens.get(route.address.token_env)

    def deliverable(self) -> list[ChatRoute]:
        """Return the routes this process can actually poll: declared, and with a token."""
        return [
            route
            for route in self.routes
            if self.token_for(route) and route.address.transport == self.transport.name
        ]

    def preflight(self) -> list[str]:
        """Return why this bridge cannot carry a conversation. Empty means ready.

        The chat half of :meth:`steward.scheduler.Scheduler.check`, and asked for the same
        reason: a bot with no token, a resident with nowhere to keep a transcript, or an
        operator list nobody filled in should be a complaint at a reasonable hour rather than
        a message that silently goes unanswered at midnight.
        """
        problems: list[str] = []
        if not self.operators:
            problems.append(
                f"{OPERATORS_ENV} is empty, so steward would answer nobody; set it to the "
                "comma-separated Telegram user ids that may talk to this fleet"
            )
        for route in self.routes:
            if route.address.transport != self.transport.name:
                problems.append(
                    f"{route.key}: address {route.address} names transport "
                    f"{route.address.transport!r}, and this bridge carries "
                    f"{self.transport.name!r}"
                )
                continue
            if not self.token_for(route):
                problems.append(
                    f"{route.key}: no token — set {route.address.token_env} to the token "
                    f"BotFather issued for {route.address}"
                )
            complaint = chat_complaint(route.resident.manifest)
            if complaint is not None:
                problems.append(f"{route.key}: {complaint}")
        if not self.routes:
            problems.append(
                "no resident declares an active chat route, so there is nothing to poll; "
                "add routes: [{kind: chat, address: telegram:<bot>, status: active}] to a "
                "manifest"
            )
        return problems

    def require_ready(self) -> None:
        """Raise :class:`ChatError` unless at least one conversation could actually happen."""
        problems = self.preflight()
        if problems:
            raise ChatError("\n".join(problems))

    # -- one pass --------------------------------------------------------------------

    def poll_once(self, now: datetime | None = None) -> list[ChatOutcome]:
        """Ask every reachable bot what it was sent, and answer it. Never raises."""
        moment = now or self.clock()
        outcomes: list[ChatOutcome] = []
        for route in self.deliverable():
            outcomes.extend(self._poll_route(route, moment))
        self._report_storms(moment)
        return outcomes

    def _report_storms(self, now: datetime) -> None:
        """Put the storms whose windows just closed in the village, one record each.

        The other half of :class:`KnockLimiter`, and the reason it is not merely a filter:
        a scanner that sends two hundred messages and stops would otherwise leave one
        ordinary-looking knock in the log. Run at the end of every pass, including the idle
        ones — a long poll that came back empty is exactly when a window that closed
        mid-storm needs saying.
        """
        for drop in self.knocks.sweep(now):
            log.warning(
                "%s: %s knocked %d more time(s) inside %.0fs and got the same silence; "
                "recording the storm as one event",
                drop.route.key,
                drop.sender,
                drop.suppressed + 1,
                self.knocks.window_s,
            )
            self._record_drop(drop)

    def _poll_route(self, route: ChatRoute, now: datetime) -> list[ChatOutcome]:
        """Poll one bot and answer everything it hands over, oldest first."""
        token = self.token_for(route)
        if token is None:  # pragma: no cover — deliverable() already filtered these out
            return []
        try:
            messages = self.transport.poll(token, self._offsets.get(route.key, 0))
        except Exception as exc:  # noqa: BLE001 — the protocol says it does not; belt and braces
            # :class:`ChatTransport` promises never to raise, and the shipped one keeps that
            # promise. This is here because ``poll_once`` promises the same thing to a daemon
            # loop, and that promise must not rest on every future transport author
            # remembering — the position :meth:`steward.notify.Notifier.send` takes for
            # exactly the same reason.
            log.warning("%s: chat transport raised while polling: %s", route.key, exc)
            messages = None
        if messages is None:
            return [
                ChatOutcome(
                    resident_id=route.resident.id,
                    route=route.route_id,
                    status=ChatStatus.UNREACHABLE,
                    reason=f"could not reach {route.address}",
                )
            ]
        outcomes: list[ChatOutcome] = []
        for message in sorted(messages, key=lambda item: item.update_id):
            # Advanced *before* the message is handled, so one steward cannot answer keeps
            # this conversation moving instead of wedging the daemon on it for ever. Nothing
            # durable rests on it: the cursor lives in this process, and Telegram redelivers
            # whatever was never acknowledged when a new one starts.
            self._offsets[route.key] = message.update_id + 1
            try:
                outcomes.append(self._handle(route, message, now))
            except Exception as exc:  # noqa: BLE001 — one bad message must not stop the fleet
                log.warning("%s: could not answer a message: %s", route.key, exc)
                outcomes.append(
                    ChatOutcome(
                        resident_id=route.resident.id,
                        route=route.route_id,
                        conversation=message.conversation,
                        status=ChatStatus.FAILED,
                        reason=f"{type(exc).__name__}: {exc}",
                    )
                )
        return outcomes

    def _handle(self, route: ChatRoute, message: Message, now: datetime) -> ChatOutcome:
        """Decide what one message deserves, and give it that."""
        refusal = self._unanswerable(route, message, now)
        if refusal is not None:
            return refusal
        outcome = self._answer(route, message, now)
        if outcome.ran:
            # After the claim is released rather than inside it: a sweep that ran while this
            # resident was still claimed would skip the very resident whose session just
            # wrote the handoff, which is the latency this call exists to remove.
            self._dispatch(now)
        return outcome

    def _unanswerable(
        self, route: ChatRoute, message: Message, now: datetime
    ) -> ChatOutcome | None:
        """Return why this message gets no session, or ``None`` when it gets one.

        The two silences here are the deliberate ones. A stranger and a group chat are both
        dropped *without a reply* — an answer of any kind, refusal included, tells whoever is
        probing that the bot is live and that something is behind it — and both are recorded
        as events, because the operator has to be able to see that somebody found their
        resident. A message that is merely too old is answered with a line, because the person
        who sent it is the operator and silence would leave them watching a bot that looks
        broken.
        """
        if message.sender not in self.operators:
            return self._drop(route, message, "not an operator", now)
        if not message.private:
            return self._drop(route, message, "not a private conversation", now)
        if message.age_s(now) > self.catchup_s:
            # The scheduler's judgement about a missed occurrence, applied to a missed
            # message: dropped, logged, and **not** answered. Telegram holds undelivered
            # updates for a day, so a bridge that was down all night comes up holding every
            # message that arrived while nobody was listening — and firing a session for
            # each would spend real money answering questions the operator gave up on and
            # has since answered themselves. Silence rather than a line, because the same
            # restart hands over *many* of these at once, and a bot that says "I was not
            # running" twenty times in a row is worse than one that says nothing: it is the
            # unprompted outbound storm this bridge exists not to be. Sending it again is
            # the operator's move, and it is one message.
            log.warning(
                "%s: dropped a message from %.0fs ago without replying — older than the "
                "%.0fs catch-up window; steward does not back-fill",
                route.key,
                message.age_s(now),
                self.catchup_s,
            )
            return ChatOutcome(
                resident_id=route.resident.id,
                route=route.route_id,
                conversation=message.conversation,
                status=ChatStatus.STALE,
                reason="older than the catch-up window",
            )
        return None

    def _refuse(
        self,
        route: ChatRoute,
        message: Message,
        status: ChatStatus,
        *,
        reason: str,
        reply: str,
    ) -> ChatOutcome:
        """Tell the operator why nothing is going to happen, and report the same thing.

        The one shape every *spoken* "not this time" takes — busy, paused, a budget steward
        could not read. Gathered here because they are one act said three ways, and a fourth
        reason should be a call rather than another copy of five lines.

        Deliberately not every refusal: a stranger, a group chat and a message older than the
        catch-up window are answered with **silence**, and each has its own path for its own
        reason. Being able to see, here, that everything reaching this method does reply is
        the point of keeping them apart.
        """
        return ChatOutcome(
            resident_id=route.resident.id,
            route=route.route_id,
            conversation=message.conversation,
            status=status,
            reason=reason,
            reply=self._reply(route, message.conversation, reply),
        )

    def _drop(self, route: ChatRoute, message: Message, reason: str, now: datetime) -> ChatOutcome:
        """Say nothing back, and say so in the village — once per stranger per window.

        Every message that reaches here is dropped; what the window bounds is how often
        that becomes an *event*. The two are deliberately different questions: refusing the
        message is this fleet's own decision and costs nothing, while recording it spends
        room in channels the operator reads and an outsider chooses when to fill
        (warren#278). Never raises.
        """
        suppressed = self.knocks.admit(route, message.sender, reason, now)
        if suppressed is None:
            log.debug(
                "%s: dropped another message from %s without replying — %s",
                route.key,
                message.sender,
                reason,
            )
            return self._dropped(route, message, reason)
        log.warning(
            "%s: dropped a message from %s without replying — %s",
            route.key,
            message.sender,
            reason,
        )
        self._record_drop(
            Drop(route=route, sender=message.sender, reason=reason, suppressed=suppressed)
        )
        return self._dropped(route, message, reason)

    def _dropped(self, route: ChatRoute, message: Message, reason: str) -> ChatOutcome:
        """Report the silence a message got, whether or not it became an event."""
        return ChatOutcome(
            resident_id=route.resident.id,
            route=route.route_id,
            conversation=message.conversation,
            status=ChatStatus.DROPPED,
            reason=reason,
        )

    def _record_drop(self, drop: Drop) -> None:
        """Put one knock in the village. Never raises: a village that is down is not news."""
        route = drop.route
        try:
            self.emitter.emit(
                ev.chat_message_dropped_event(
                    agent_id=route.resident.agent_id,
                    project=route.resident.project,
                    route=route.route.id,
                    address=str(route.address),
                    sender=drop.sender,
                    reason=drop.reason,
                    suppressed=drop.suppressed,
                )
            )
        except Exception as exc:  # noqa: BLE001 — an unreachable village is not a failure here
            log.warning("%s: could not record a dropped message: %s", route.key, exc)

    def _answer(self, route: ChatRoute, message: Message, now: datetime) -> ChatOutcome:
        """Hold the resident's claim and answer one message, or say why it could not."""
        resident = route.resident
        run_id = str(uuid.uuid4())
        claimed = (
            self.claims.hold(resident.id, kind=RUN_CHAT, ref=message.conversation, run_id=run_id)
            if self.claims is not None
            else contextlib.nullcontext(None)
        )
        with claimed as claim:
            if isinstance(claim, ClaimRefused):
                # The API's 409 in sentence form (warren#111): refused, never queued. A
                # person asking for an answer *now* cannot be handed one later and told it
                # was now, and a queue of chat sessions would answer questions the operator
                # stopped caring about an hour ago.
                log.info("%s: %s", route.key, claim.reason)
                return self._refuse(
                    route,
                    message,
                    ChatStatus.BUSY,
                    reason=claim.reason,
                    reply=f"{route.name} is busy right now — {claim.reason}. "
                    f"Send that again in a minute.",
                )
            return self._answer_held(route, message, run_id, now)

    def _answer_held(
        self, route: ChatRoute, message: Message, run_id: str, now: datetime
    ) -> ChatOutcome:
        """Run one session for one message, under this resident's claim."""
        resident = route.resident
        admission = self.sessions.admit(resident, now=now)
        if isinstance(admission, Refusal):
            # Exactly what a scheduled fire does with a paused resident — nothing starts and
            # the village hears nothing — plus the one thing a fire has no one to do it for:
            # the person who asked is told why, because they are standing at the door.
            log.warning("%s: %s", route.key, admission.reason)
            return self._refuse(
                route,
                message,
                ChatStatus.REFUSED,
                reason=admission.reason,
                reply=f"{route.name} cannot answer right now: {admission.reason}",
            )
        transcript = Transcript(resident.manifest, message.conversation)
        try:
            return self._run_session(route, message, run_id, now, admission, transcript)
        finally:
            admission.close()

    def _run_session(  # noqa: PLR0913, PLR0917 — one argument per thing the session needs
        self,
        route: ChatRoute,
        message: Message,
        run_id: str,
        now: datetime,
        admission: Admission,
        transcript: Transcript,
    ) -> ChatOutcome:
        """Bracket, run and record one chat session, then send its answer back."""
        resident = route.resident
        window = self._window(transcript)
        try:
            timeout_s = admission.timeout_for(self.timeout_s)
        except Exception as exc:  # noqa: BLE001 — an unreadable budget refuses safely
            reason = f"budget unreadable: {type(exc).__name__}: {exc}"
            log.warning("%s: could not resolve the run timeout: %s", route.key, exc)
            return self._refuse(
                route,
                message,
                ChatStatus.REFUSED,
                reason=reason,
                reply=f"{route.name} cannot answer right now: {reason}",
            )
        # Written down before the session runs, so what the operator said survives a run that
        # dies — and so the next turn's window holds this message whatever happened to it.
        transcript.append(OPERATOR_SPEAKER, message.text, now=now)
        context = ev.RunContext(
            agent_id=resident.agent_id,
            project=resident.project,
            routine=route.route_id,
            run_id=run_id,
            cwd=str(admission.workdir),
        )
        owner_token = new_owner_token()
        credential = new_session_credential()
        # The event first, then the row: the same order and the same reason as a scheduled
        # fire (:meth:`steward.scheduler.Scheduler._fire_body`).
        self.emitter.emit(context.started(TRIGGER_CHAT))
        watched = self._open_run(route, run_id, message, timeout_s, now, owner_token, credential)
        wake = ChatWake(
            conversation=message.conversation,
            route=route.route_id,
            message=message.text,
            transcript=window,
            run_id=run_id,
            timeout_s=self.timeout_s,
            session_credential=credential if watched else "",
        )
        ownership = (
            self.run_transitions.owned(run_id, owner_token) if watched else contextlib.nullcontext()
        )
        with ownership:
            session = self.sessions.run(admission, wake)
            result = session.require_result()
            terminal = (
                context.finished(
                    outcome=str(result.outcome),
                    artifacts=result.artifacts,
                    duration_s=result.duration_s,
                )
                if result.ok
                else context.failed(
                    error=f"{result.outcome}: {result.summary()}", duration_s=result.duration_s
                )
            )
            if not watched:
                self.emitter.emit(terminal)
            else:
                self.run_transitions.session_claim(
                    run_id, terminal, owner_token=owner_token, now=session.completed_at or now
                )
                self.run_transitions.publish_pending(self.emitter, now=session.completed_at or now)
        reply = self._reply(route, message.conversation, self._answer_text(route, result))
        transcript.append(resident.id, reply, now=session.completed_at or now)
        return ChatOutcome(
            resident_id=resident.id,
            route=route.route_id,
            conversation=message.conversation,
            status=ChatStatus.ANSWERED if result.ok else ChatStatus.FAILED,
            run_id=run_id,
            reason=None if result.ok else f"{result.outcome}: {result.summary()}",
            reply=reply,
        )

    def _window(self, transcript: Transcript) -> str:
        """Read the last few turns of a conversation. A broken file is simply no context."""
        try:
            return transcript.render()
        except Exception as exc:  # noqa: BLE001 — a lost transcript is not a lost session
            log.warning("%s: could not read the transcript: %s", transcript.manifest.id, exc)
            return ""

    def _answer_text(self, route: ChatRoute, result: RunResult) -> str:
        """Turn what the session produced into the sentence the operator reads.

        A failed session answers with :meth:`steward.runners.RunResult.summary`, which is
        steward's own words and deliberately not the child's stdout: a session that crashed
        while printing a key must not have that key forwarded to the conversation as its
        "answer". A session that finished and said nothing is reported as exactly that,
        rather than answered with something steward made up on its behalf.

        Scrubbing and bounding are **not** done here. They are :meth:`_reply`'s, once, for
        every outbound string — see there.
        """
        if not result.ok:
            return f"{route.name} could not answer: {result.summary()}"
        return (result.output or "").strip() or f"{route.name} finished without saying anything."

    def _reply(self, route: ChatRoute, conversation: str, text: str) -> str:
        """Send one reply and return what was actually sent. Never raises.

        **The one egress**, and the one place redaction happens — redact, *then* bound
        (steward #65), never the reverse. It is here rather than at each caller for the
        reason :func:`steward.notify.tap_for` gives for doing it in one place: a refusal
        added tomorrow cannot forget the rule, and cannot get the order backwards. That
        matters more on this path than anywhere else in steward, because these strings are
        delivered to a phone — and it is not only the *session's* answer that needs it. An
        admission refusal carries whatever a broken budget read threw, which is steward's
        own diagnostic and is exactly the kind of sentence a connection string ends up in.

        Returns the sent text rather than whether it landed, because that is what a caller
        does with it: writes it into the transcript, and reports it. Whether it *arrived* is
        the transport's business and a log line — a reply the operator did not get is not a
        session that failed, and there is nobody to tell about it anyway.
        """
        sent = _bounded(text)
        token = self.token_for(route)
        if token is None:  # pragma: no cover — nothing reaches here without a token
            return sent
        try:
            delivered = self.transport.send(token, conversation, sent)
        except Exception as exc:  # noqa: BLE001 — a transport that raises is still just a failure
            log.warning("%s: chat transport raised while replying: %s", route.key, exc)
            return sent
        if not delivered:
            log.warning("%s: the reply did not reach the conversation", route.key)
        return sent

    def _open_run(  # noqa: PLR0913, PLR0917 — one argument per persisted run fact
        self,
        route: ChatRoute,
        run_id: str,
        message: Message,
        timeout_s: int,
        now: datetime,
        owner_token: str,
        session_credential: str,
    ) -> bool:
        """Write this session into the run registry. Never raises: a lost row is not a run."""
        try:
            opened = self.store.open_run(
                run_id=run_id,
                kind=RUN_CHAT,
                trigger=TRIGGER_CHAT,
                agent_id=route.resident.agent_id,
                project=route.resident.project,
                ref=message.conversation,
                timeout_s=float(timeout_s),
                event_log_path=event_log_path(self.emitter),
                owner_token=owner_token,
                resident_id=route.resident.id,
                session_credential=session_credential,
                now=ev.utc_now_iso(now),
            )
        except Exception as exc:  # noqa: BLE001 — an unwritable registry is not a failed answer
            log.warning("%s: could not record that this session started: %s", route.key, exc)
            return False
        if not opened:  # pragma: no cover — a fresh id per message cannot collide
            log.warning("%s: run %s was already recorded, so it is not watched", route.key, run_id)
        return opened

    def _dispatch(self, now: datetime) -> None:
        """Sweep the board after a conversation. Never raises.

        The same call the scheduler makes after its fires, for the reason warren#108 asked
        for it: a resident that hands work to a neighbour mid-conversation should have handed
        it over by the time the operator reads the reply, not on the next hourly tick.
        """
        if self.hooks is None:
            return
        try:
            self.hooks.dispatch(now)
        except Exception as exc:  # noqa: BLE001 — the board must not take the bridge down
            log.warning("board dispatch failed: %s", exc)

    # -- the daemon --------------------------------------------------------------------

    def run(
        self,
        *,
        max_polls: int | None = None,
        sleep: Callable[[float], object] = time.sleep,
    ) -> list[ChatOutcome]:
        """Poll, answer, repeat. ``max_polls`` bounds the loop for tests.

        One cross-process lock for the daemon's whole life, like
        :meth:`steward.scheduler.Scheduler._daemon_lock` and for a sharper reason: two
        pollers on one bot do not merely double the work, they *steal from each other*.
        Telegram hands each update to whichever ``getUpdates`` asked first and refuses the
        second with a conflict, so a second daemon would answer half the operator's messages
        and drop the rest on the floor.

        Every message is said out loud as it happens rather than only in the returned list,
        because the returned list is the *bounded* run's aggregate and an unbounded daemon
        never reaches it: a run that only reported at exit would run for weeks saying nothing.
        That is also why nothing accumulates unless the loop is bounded — a list holding one
        record per message answered since Tuesday is a leak, not a report.
        """
        self.require_ready()
        outcomes: list[ChatOutcome] = []
        polls = 0
        with self._daemon_lock():
            while max_polls is None or polls < max_polls:
                polls += 1
                pass_outcomes = self.poll_once()
                for outcome in pass_outcomes:
                    log.info(
                        "%s/%s: %s%s",
                        outcome.resident_id,
                        outcome.route,
                        outcome.status,
                        f" — {redact_secrets(outcome.reason)}" if outcome.reason else "",
                    )
                if max_polls is not None:
                    outcomes.extend(pass_outcomes)
                if any(outcome.status is ChatStatus.UNREACHABLE for outcome in pass_outcomes):
                    sleep(UNREACHABLE_SLEEP_S)
                elif not pass_outcomes:
                    sleep(self.idle_sleep_s)
        return outcomes

    @contextlib.contextmanager
    def _daemon_lock(self) -> Iterator[None]:
        """Hold a lock for this daemon's lifetime, so a second one refuses to start.

        Its own sidecar in the shared state directory rather than the scheduler's, because
        these are two daemons that are *meant* to run side by side: sharing a lock file would
        make the chat bridge and the scheduler refuse each other, which is the opposite of the
        deployment this exists for.

        Advisory, and unreliable over NFS — the same caveat the scheduler's carries.
        """
        base = self.state_path if self.state_path is not None else default_state_path()
        lock_path = Path(base).parent / "chat.lock"
        try:
            lock_path.parent.mkdir(parents=True, exist_ok=True)
            descriptor = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o644)
        except OSError as exc:
            raise ChatError(f"cannot open the chat daemon lock at {lock_path}: {exc}") from exc
        try:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise ChatError(
                    f"another chat bridge is already running over {lock_path} — two pollers "
                    "on one bot steal each other's messages. Stop the running one first."
                ) from exc
            os.ftruncate(descriptor, 0)
            os.write(descriptor, f"{os.getpid()}\n".encode())
            yield
        finally:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)


def _bounded(text: str) -> str:
    """Scrub a reply of secrets and *then* bound it. Never the other way round."""
    scrubbed = redact_secrets(text)
    if len(scrubbed) <= REPLY_MAX_CHARS:
        return scrubbed
    return scrubbed[: REPLY_MAX_CHARS - 1].rstrip() + "…"
