"""The chat bridge: an operator says something, a resident answers, and that is all.

``routes: {kind: chat}`` has been descriptive since the manifest had routes at all — a
manifest could say "you can talk to me here" and nothing in steward listened. This module
is what makes it deliverable (warren#108): a daemon long-polls dedicated or shared bots, and
every message from a named operator fires **one ordinary session** whose final message is
sent back as the reply.

**It only ever speaks when spoken to.** There is no outbound half here and there is not
going to be one: steward tapping a person on the shoulder — a ``needs_human`` at 2am, a
task that finished — is a *notification*, it goes through :mod:`steward.notify`, and it is
one-way by construction (warren#114). A bridge that grew unprompted messages would be two
channels wearing one name.

**No secret in the manifest.** The route's ``address`` is a
reference — ``telegram:pip`` — and the token BotFather issued lives in steward's own
environment under :data:`TOKEN_ENV_PREFIX` plus the reference (``STEWARD_CHAT_TOKEN_PIP``).
That is the same division every other credential in this system lives on: manifests are git,
and a manifest is exactly where a bot token must never be. It also means the mapping is
readable from the manifest alone — an operator who can see ``address: telegram:pip`` knows
which variable to set without consulting anything else.

**Only named operators are answered.** :data:`OPERATORS_ENV` is a comma-separated list of
transport-qualified user ids; legacy bare ids belong to Telegram. A message from anybody
else is dropped with **no reply at all** — an
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
import hashlib
import logging
import os
import time
import uuid
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path

from steward import events as ev
from steward.chat_config import (
    DEFAULT_POLL_TIMEOUT_S as DEFAULT_POLL_TIMEOUT_S,  # noqa: PLC0414 — preserve public chat imports
)
from steward.chat_config import (
    DISCORD as DISCORD,  # noqa: PLC0414 — preserve public chat imports
)
from steward.chat_config import (
    OPERATORS_ENV,
    POLL_TIMEOUT_ENV,
    TOKEN_ENV_PREFIX,
    Address,
    ChatError,
    ChatRoute,
    _addressed_routes,
    _shared_bot_problems,
    chat_routes,
    operators_from_env,
    poll_timeout_from_env,
    token_env_name,
    token_env_names,
    tokens_from_env,
)
from steward.chat_config import (
    TELEGRAM as TELEGRAM,  # noqa: PLC0414 — preserve public chat imports
)
from steward.chat_health import (
    ChatReport,
    RouteCheck,
    RouteHealth,
    check_route,
    describe_chat,
)
from steward.chat_transcripts import (
    CHAT_DIR,
    TRANSCRIPT_KEEP_TURNS,
    TRANSCRIPT_WINDOW_TURNS,
    Transcript,
    Turn,
    chat_complaint,
    resolve_chat_dir,
)
from steward.chat_transcripts import (
    SLUG_MAX_CHARS as SLUG_MAX_CHARS,  # noqa: PLC0414 — preserve public chat imports
)
from steward.chat_transcripts import (
    ConversationSummary as ConversationSummary,  # noqa: PLC0414 — preserve public chat imports
)
from steward.chat_transcripts import (
    conversation_slug as conversation_slug,  # noqa: PLC0414 — preserve public chat imports
)
from steward.chat_transcripts import (
    conversation_summaries as conversation_summaries,  # noqa: PLC0414 — preserve public chat imports
)
from steward.chat_transports import (
    API_URL_ENV,
    DEFAULT_API_URL,
    DEFAULT_DISCORD_API_URL,
    DISCORD_API_URL_ENV,
    SEND_TIMEOUT_S,
    ChatTransport,
    ConversationAccess,
    DiscordTransport,
    Message,
    TelegramTransport,
)
from steward.chat_transports import (
    DISCORD_MEMBER_MAX_PAGES as DISCORD_MEMBER_MAX_PAGES,  # noqa: PLC0414 — preserve public chat imports
)
from steward.chat_transports import (
    DISCORD_MEMBER_PAGE_SIZE as DISCORD_MEMBER_PAGE_SIZE,  # noqa: PLC0414 — preserve public chat imports
)
from steward.chat_transports import (
    DISCORD_PAGE_SIZE as DISCORD_PAGE_SIZE,  # noqa: PLC0414 — preserve public chat imports
)
from steward.chat_transports import (
    DISCORD_REPLY_CHARS as DISCORD_REPLY_CHARS,  # noqa: PLC0414 — preserve public chat imports
)
from steward.chat_transports import (
    HTTP_TIMEOUT_MARGIN_S as HTTP_TIMEOUT_MARGIN_S,  # noqa: PLC0414 — preserve public chat imports
)
from steward.chat_transports import (
    HTTP_TOO_MANY_REQUESTS as HTTP_TOO_MANY_REQUESTS,  # noqa: PLC0414 — preserve public chat imports
)
from steward.chat_transports import (
    MAX_RATE_LIMIT_SLEEP_S as MAX_RATE_LIMIT_SLEEP_S,  # noqa: PLC0414 — preserve public chat imports
)
from steward.claims import ClaimRefused, ResidentClaims
from steward.manifest import (
    ROUTINE_DELIVER_CHAT,
    Resident,
    Routine,
    redact_secrets,
    validate_path,
)
from steward.prompt import TRANSCRIPT_MAX_CHARS as TRANSCRIPT_MAX_CHARS  # noqa: PLC0414
from steward.run_lifecycle import RunTransitions, event_log_path, new_owner_token
from steward.runners import RunResult, build_runner
from steward.runs import DELIVERED, DELIVERY_FAILED, RUN_CHAT, TRIGGER_CHAT
from steward.scheduler import Delivery, WakeHooks, default_state_path
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

log = logging.getLogger("steward.chat")

__all__ = [
    "API_URL_ENV",
    "CHAT_DIR",
    "DEFAULT_API_URL",
    "DEFAULT_CATCHUP_S",
    "DEFAULT_DISCORD_API_URL",
    "DISCORD_API_URL_ENV",
    "KNOCK_DOORS_TRACKED",
    "OPERATORS_ENV",
    "POLL_TIMEOUT_ENV",
    "REPLY_MAX_CHARS",
    "ROUTE_RECHECK_S",
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
    "ConversationAccess",
    "DiscordTransport",
    "Drop",
    "KnockLimiter",
    "Message",
    "RouteCheck",
    "RouteHealth",
    "RoutineDelivery",
    "TelegramTransport",
    "Transcript",
    "Turn",
    "chat_complaint",
    "chat_routes",
    "check_route",
    "describe_chat",
    "operators_from_env",
    "poll_timeout_from_env",
    "resolve_chat_dir",
    "token_env_name",
    "token_env_names",
    "tokens_from_env",
]


def _utcnow() -> datetime:
    """Read the wall clock, in UTC. What a message's age is measured against."""
    return datetime.now(UTC)


#: How long a bridge waits after a pass in which some bot could not be reached, so an API
#: that is down costs one request every few seconds rather than a spin.
UNREACHABLE_SLEEP_S = 5.0

#: How long a bridge waits after an idle pass. Small, because a long poll has already done
#: the waiting; it exists so a transport that answers instantly cannot spin.
IDLE_SLEEP_S = 1.0

#: How often a route that could not carry a conversation is asked again (warren#456). What
#: :func:`check_route` reads is not all fixed at startup: a bot re-enabled in Discord's
#: Developer Portal, permissions corrected on an application, an API that answered nothing
#: for a minute — each of those heals on the far side of the wire, and the identity call is
#: how steward finds out. A daemon that asked once would stay half-deaf until a person
#: happened to notice. What a recheck cannot heal is this process's *environment*: tokens
#: and the operator list are read at startup, so correcting one in the burrow's ``.env``
#: still needs the service recreated — a container's environment does not change under it.
ROUTE_RECHECK_S = 300.0

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

#: How long one reply may be. Telegram refuses anything past 4096 characters, and a reply
#: that long is a report rather than an answer; the cap is applied *after* redaction, which
#: is the repo rule (steward #65) and matters most here — this text goes to a phone.
REPLY_MAX_CHARS = 3500

#: What steward calls the operator in a transcript. Not their user id: the file lives in the
#: resident's memory directory, is injected into prompts, and a numeric identity in it would
#: be a fact about a person that the conversation never needed.
OPERATOR_SPEAKER = "operator"


def _post_outcome_line(outcome: object) -> str:
    """Render an optional shared-harvest outcome through its deliberately tiny seam."""
    render = getattr(outcome, "transcript_line", None)
    if not callable(render):
        return ""
    value = render()
    return value if isinstance(value, str) else ""


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


@dataclass(frozen=True, slots=True)
class BridgeSources:
    """Everything a running bridge can legitimately re-read without being restarted.

    The unit warren#462 needed. Two of the four things a bridge is assembled from live
    *outside* this process and change under it: the residents checkout is a git tree the API
    commits into, and the secrets directory is a mount an operator writes a token onto. The
    other two — the transports it can speak and the operator list — are fixed at startup by
    construction, because they come from the container's environment and a container's
    environment does not change under it.

    Kept as one record rather than four re-read calls so that a reload is all-or-nothing: a
    tree that stopped validating leaves the bridge on the fleet it already had, rather than
    on new tokens for routes it no longer knows about.
    """

    routes: tuple[ChatRoute, ...]
    tokens: Mapping[str, str]
    library: SkillLibrary


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
    operators_by_transport: Mapping[str, frozenset[str]] = field(default_factory=dict)
    transports: Mapping[str, ChatTransport] = field(default_factory=dict)
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
    #: How long a route's health is believed before it is measured again.
    recheck_s: float = ROUTE_RECHECK_S
    state_path: Path | None = None
    #: How this bridge re-reads the tree and the secrets directory it was assembled from,
    #: or ``None`` for a bridge built by hand from explicit collaborators (every test that
    #: injects its own routes and tokens, and :meth:`RoutineDelivery.from_env`\ 's siblings).
    #: :meth:`from_path` supplies it, because that is the constructor that knows *where* the
    #: bridge came from — a bridge handed a list of routes has no tree to go back to.
    reload_source: Callable[[], BridgeSources] | None = field(default=None, repr=False)
    sessions: ResidentSessions = field(init=False, repr=False)
    run_transitions: RunTransitions = field(init=False, repr=False)
    #: What stops a stranger deciding how much of the village an operator can see.
    knocks: KnockLimiter = field(init=False, repr=False)
    _offsets: dict[str, int] = field(default_factory=dict, init=False, repr=False)
    #: One :class:`RouteHealth` per carried route, keyed by :attr:`ChatRoute.key`.
    _health: dict[str, RouteHealth] = field(default_factory=dict, init=False, repr=False)
    #: When the tree and the secrets directory were last re-read. ``None`` means "not since
    #: this object was built", so the first health refresh re-reads before it measures —
    #: which is what makes a daemon that was started before the token was written come up
    #: with the route open rather than shut for one whole recheck interval.
    _reloaded_at: datetime | None = field(default=None, init=False, repr=False)

    _listener_tokens: dict[str, set[str]] = field(default_factory=dict, init=False, repr=False)
    _listened_channels: dict[str, frozenset[str]] = field(
        default_factory=dict, init=False, repr=False
    )

    def __post_init__(self) -> None:
        """Build the shared lifecycle from the bridge's existing dependencies."""
        if not self.transports:
            self.transports = {self.transport.name: self.transport}
        if not self.operators_by_transport:
            self.operators_by_transport = {self.transport.name: self.operators}
        self._register_listeners()
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

    def _register_listeners(self) -> None:
        """Tell each transport which rooms this bridge listens in, with which token.

        Not a query: :meth:`DiscordTransport.listen` *stores* the bot id and the resolved
        channel ids, and a route whose channels were never resolved polls nowhere. It runs
        at construction and again on every reload (warren#462), because the whole point of a
        reload is the case where neither the route nor its token existed at construction —
        a Discord bot declared with ``listens_in`` and tokened through ``PUT /secrets/{name}``
        would otherwise pass its identity check, report itself reachable, and hear nothing.
        """
        self._listened_channels.clear()
        for name, transport in self.transports.items():
            listen = getattr(transport, "listen", None)
            if not callable(listen):
                continue
            routes = [route for route in self.deliverable() if route.address.transport == name]
            desired: dict[str, set[str]] = {}
            for route in routes:
                token = self.tokens[route.address.token_env]
                desired.setdefault(token, set()).update(route.route.listens_in)
            for token in self._listener_tokens.get(name, set()) - desired.keys():
                listen(token, [])
            self._listener_tokens[name] = set(desired)
            for token, names in desired.items():
                if not listen(token, sorted(names)):
                    log.warning("%s: Discord listen channels could not be resolved", name)
            channels = getattr(transport, "listening_channels", None)
            if callable(channels):
                for route in routes:
                    token = self.tokens[route.address.token_env]
                    self._listened_channels[route.key] = channels(token, route.route.listens_in)

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
        transports: Mapping[str, ChatTransport] | None = None,
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
        # The raw mapping, not the secrets overlay: what the transports and the operator
        # list read from it are *settings* — an API base URL, a guild id, who steward
        # answers — and none of them is a credential, so none of them belongs in a directory
        # of credentials. The tokens are the exception and they read through
        # :func:`tokens_from_env`, freshly, on every call — which is what lets ``reload``
        # below pick up a file written after this process started (warren#462). Pre-mixing
        # the two here would freeze the tokens at construction and undo that.
        source = os.environ if env is None else env
        if transport is not None and transports is not None:
            raise TypeError("pass transport or transports, not both")
        available = transports or (
            {transport.name: transport}
            if transport is not None
            else {
                TELEGRAM: TelegramTransport.from_env(source),
                DISCORD: DiscordTransport.from_env(source),
            }
        )

        def reread() -> BridgeSources:
            """Re-read the tree and the secrets directory. Raises on an invalid tree."""
            fresh = validate_path(residents_dir, skills_dir)
            if not fresh.ok:
                raise ChatError(
                    "the residents tree does not validate:\n"
                    + "\n".join(d.render() for d in fresh.errors)
                )
            return BridgeSources(
                routes=tuple(chat_routes(list(fresh.residents))),
                tokens=tokens_from_env(source),
                library=library_for(residents_dir, skills_dir),
            )

        return cls(
            routes=chat_routes(list(result.residents)),
            store=store,
            tokens=tokens_from_env(source),
            operators=operators_from_env(source),
            transport=available.get(TELEGRAM, next(iter(available.values()))),
            operators_by_transport={
                name: operators_from_env(source, transport=name) for name in available
            },
            transports=available,
            emitter=emitter or ev.EventEmitter.from_env(),
            workdir=workdir if workdir is not None else Path.cwd(),
            library=library if library is not None else library_for(residents_dir, skills_dir),
            guard=guard,
            hooks=hooks,
            claims=claims,
            state_path=state_path,
            catchup_s=catchup_s,
            reload_source=reread,
        )

    # -- startup ---------------------------------------------------------------------

    def token_for(self, route: ChatRoute) -> str | None:
        """Return the bot token for one route, or ``None`` when nobody has set it."""
        return self.tokens.get(route.address.token_env)

    def deliverable(self) -> list[ChatRoute]:
        """Return the routes somebody has at least handed a token: declared, and configured.

        The question ``steward chat run`` asks before it starts — a burrow where nobody has
        set a single token is an idle daemon rather than an error. Whether a route can
        actually carry a message is a further question, and a live one: :meth:`reachable`.
        """
        return [
            route
            for route in self.routes
            if self.token_for(route) and route.address.transport in self.transports
        ]

    def carried(self) -> list[ChatRoute]:
        """Return the declared routes whose transport this process can actually speak."""
        return [route for route in self.routes if route.address.transport in self.transports]

    def reachable(self, now: datetime | None = None) -> list[ChatRoute]:
        """Return the routes an operator could get an answer through, re-asking if stale."""
        self._refresh_health(now if now is not None else self.clock())
        return [route for route in self.carried() if self._health[route.key].reachable]

    def reload(self) -> bool:
        """Re-read the residents tree and the secrets directory. Return whether it happened.

        The "chat reload" of warren#462, and the reason wiring a bot no longer ends in
        ``docker compose up -d --force-recreate chat``. A recreate was never about the code:
        it was the only way to change what the process had *read*. Two of those reads can be
        redone in place — the tree, because the API commits into a checkout this container
        mounts, and the tokens, because they are now files on a mount rather than lines in
        an environment — so a token pasted into ``PUT /secrets/{name}`` reaches "reachable,
        bot @Name" on the next recheck instead of on the next deploy.

        ``False`` means there was nothing to re-read: a bridge assembled from explicit
        routes and tokens (every hand-built one, and every test) has no tree to go back to,
        and inventing one for it would make those bridges depend on the machine they run on.

        Never raises. A tree that stopped validating mid-edit leaves this bridge on the
        fleet it already had, exactly as ``POST /reload`` refuses rather than swapping in a
        broken one — the alternative is one bad commit closing every door in the burrow.
        """
        if self.reload_source is None:
            return False
        try:
            fresh = self.reload_source()
        except Exception as exc:  # noqa: BLE001 — a bad tree must not take the daemon down
            log.warning("chat reload found nothing usable, keeping what was loaded: %s", exc)
            return False
        self.routes = fresh.routes
        self.tokens = fresh.tokens
        self.library = fresh.library
        # ``ResidentSessions`` is configuration around a fixed lifecycle rather than a thing
        # holding per-run state, so the fleet it admits is updated in place: rebuilding it
        # would hand the next session a different object for no reason.
        self.sessions.residents = tuple(route.resident for route in fresh.routes)
        self.sessions.library = fresh.library
        self._register_listeners()
        live = {route.key for route in self.routes}
        for key in [key for key in self._health if key not in live]:
            # A door that is no longer declared has no health to report. Left behind, it
            # would keep ``run`` awake believing something reachable still exists.
            del self._health[key]
        live_polls = {route.poll_key for route in self.routes}
        for key in [key for key in self._offsets if key not in live_polls]:
            # And its poll offset goes with it, so a route that is declared, retired and
            # declared again does not resume from a cursor belonging to the last time.
            del self._offsets[key]
        return True

    def _refresh_health(self, now: datetime) -> list[ChatOutcome]:
        """Re-ask every stale route whether it can carry a conversation; report what changed.

        Only *changes* are reported, and that is the whole reason this returns anything: a
        route that has been shut since Tuesday belongs in ``steward chat list``, not in a
        line of the daemon's log every second. A route that comes back says so too, because
        the operator who fixed it is owed the news as much as the one who broke it.

        The reload rides on this same timer (warren#462) rather than on one of its own: what
        a reload changes is precisely what a health check measures, so re-reading on any
        other cadence would leave the daemon holding a token it had not yet asked about.
        """
        if self._reloaded_at is None or (now - self._reloaded_at).total_seconds() >= self.recheck_s:
            self._reloaded_at = now
            self.reload()
        changed: list[ChatOutcome] = []
        conflicts = _shared_bot_problems(self.routes, self.tokens)
        for route in self.carried():
            previous = self._health.get(route.key)
            if (
                previous is not None
                and (now - previous.checked_at).total_seconds() < self.recheck_s
            ):
                continue
            problems = check_route(
                route.resident,
                route.address,
                token=self.token_for(route),
                transport=self.transports.get(route.address.transport),
                operators=self._operators_for(route.address.transport),
            ).problems + conflicts.get(route.key, ())
            self._health[route.key] = RouteHealth(problems=problems, checked_at=now)
            if previous is not None and previous.problems == problems:
                continue
            if problems:
                reason = "; ".join(problems)
                log.warning("%s: not reachable — %s", route.key, reason)
                changed.append(
                    ChatOutcome(
                        resident_id=route.resident.id,
                        route=route.route_id,
                        status=ChatStatus.UNREACHABLE,
                        reason=reason,
                    )
                )
            elif previous is not None:
                log.info("%s: reachable again", route.key)
        return changed

    def blocking_problems(self) -> list[str]:
        """Return why no conversation could happen here at all. Empty means some could.

        The one class that stays fatal (warren#456), and both members are facts about the
        *fleet* rather than about a route: nothing declares a chat route, or nothing declares
        one this build can carry. Neither heals without a manifest or an image change, so a
        daemon that kept polling on them would be polling nothing, for ever.
        """
        if not self.routes:
            return [
                (
                    "no resident declares an active chat route, so there is nothing to poll; "
                    "add routes: [{kind: chat, address: telegram:<bot>, status: active}] to a "
                    "manifest"
                )
            ]
        if not self.carried():
            names = sorted({route.address.transport for route in self.routes})
            return [
                (
                    f"this bridge carries {', '.join(sorted(self.transports))!r}, but no "
                    "active route uses it; "
                    f"declared transports: {', '.join(names)}"
                )
            ]
        return []

    def preflight(self) -> list[str]:
        """Return everything standing between this bridge and a conversation. Empty is ready.

        The chat half of :meth:`steward.scheduler.Scheduler.check`, and asked for the same
        reason: a bot with no token, a resident with nowhere to keep a transcript, or an
        operator list nobody filled in should be a complaint at a reasonable hour rather than
        a message that silently goes unanswered at midnight.

        It reports two classes together and they are acted on apart (warren#456):
        :meth:`blocking_problems` refuses to start the process, while a per-route problem
        only shuts its own route and is asked again every :data:`ROUTE_RECHECK_S` seconds.
        """
        problems = self.blocking_problems()
        self._refresh_health(self.clock())
        problems.extend(
            f"{route.key}: {problem}"
            for route in self.carried()
            for problem in self._health[route.key].problems
        )
        return problems

    def require_ready(self) -> None:
        """Raise :class:`ChatError` unless some conversation could happen through this process.

        Deliberately narrower than :meth:`preflight` (warren#456). It used to raise on
        everything preflight found, so one resident's unusable Discord token exited the
        daemon — and because there is one process for every route, a healthy Telegram bot on
        another resident went dark with it and stayed dark through nine restarts. A problem
        that belongs to one route now stays there.
        """
        problems = self.blocking_problems()
        if problems:
            raise ChatError("\n".join(problems))

    # -- one pass --------------------------------------------------------------------

    def poll_once(self, now: datetime | None = None) -> list[ChatOutcome]:
        """Ask every reachable bot what it was sent, and answer it. Never raises."""
        moment = now or self.clock()
        refresh = getattr(self.hooks, "refresh_discord_mirrors", None)
        if callable(refresh):
            try:
                refresh(moment)
            except Exception as exc:  # noqa: BLE001 — mirror context cannot stop chat
                log.warning("Discord guild mirror refresh failed: %s", exc)
        outcomes: list[ChatOutcome] = self._refresh_health(moment)
        polled: set[str] = set()
        for route in self.carried():
            if self._health[route.key].reachable and route.poll_key not in polled:
                polled.add(route.poll_key)
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
        if token is None:  # pragma: no cover — reachable() already filtered these out
            return []
        try:
            messages = self.transports[route.address.transport].poll(
                token, self._offsets.get(route.poll_key, 0)
            )
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
            self._offsets[route.poll_key] = message.update_id + 1
            try:
                recipient = self._public_recipient(route, message)
                outcomes.append(
                    self._handle_shared(recipient, message, now)
                    if recipient.route.shared
                    else self._handle(recipient, message, now)
                )
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

    def _public_recipient(self, polled: ChatRoute, message: Message) -> ChatRoute:
        """Route a shared Discord poll to a current, reachable listener of this room."""
        if (
            polled.address.transport != DISCORD
            or message.access != ConversationAccess.ALLOWLISTED_PUBLIC
        ):
            return polled
        token = self.token_for(polled)
        return next(
            (
                route
                for route in self.routes
                if route.address.transport == DISCORD
                and self.token_for(route) == token
                and message.conversation in self._listened_channels.get(route.key, frozenset())
                and self._health[route.key].reachable
            ),
            polled,
        )

    def _handle_shared(self, route: ChatRoute, message: Message, now: datetime) -> ChatOutcome:
        """Authenticate before resolving or changing a shared conversation's recipient."""
        refusal = self._unanswerable(route, message, now)
        if refusal is not None:
            return refusal
        routes = [
            candidate
            for candidate in self.routes
            if candidate.route.shared and candidate.poll_key == route.poll_key
        ]
        matches, text, addressed = _addressed_routes(routes, message.text)
        if not addressed:
            current = self.store.chat_recipient(route.poll_key, message.conversation)
            matches = [candidate for candidate in routes if candidate.resident.uid == current]
        if len(matches) != 1:
            return self._routing_reply(
                route,
                message,
                "Name one resident with 'id: message' or '@id message'. "
                "Available: " + ", ".join(candidate.resident.id for candidate in routes),
            )
        [selected] = matches
        if addressed:
            self.store.select_chat_recipient(
                route.poll_key, message.conversation, selected.resident.uid
            )
        health = self._health[selected.key]
        if not health.reachable:
            return self._refuse(
                selected,
                message,
                ChatStatus.UNREACHABLE,
                reason="; ".join(health.problems),
                reply="Cannot answer right now: " + "; ".join(health.problems),
            )
        if not text.strip():
            return self._routing_reply(selected, message, f"Now talking to {selected.name}.")
        return self._handle(selected, replace(message, text=text), now)

    def _routing_reply(self, route: ChatRoute, message: Message, text: str) -> ChatOutcome:
        """Explain a routing decision without attributing the daemon's words to a resident."""
        return ChatOutcome(
            resident_id=route.resident.id,
            route=route.route_id,
            conversation=message.conversation,
            status=ChatStatus.REFUSED,
            reason="shared bot routing",
            reply=self._reply(route, message, text, tag=False),
        )

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
        if message.bot:
            return self._drop(route, message, "a bot", now)
        operators = self._operators_for(route.address.transport)
        allowed = (
            {f"{DISCORD}:{operator}" for operator in operators}
            if route.address.transport == DISCORD
            else operators
        )
        if message.sender not in allowed:
            return self._drop(route, message, "not an operator", now)
        if message.access != ConversationAccess.PRIVATE and not (
            route.address.transport == DISCORD
            and message.conversation in self._listened_channels.get(route.key, frozenset())
            and message.access == ConversationAccess.ALLOWLISTED_PUBLIC
        ):
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
            reply=self._reply(route, message, reply),
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
        conversation = message.conversation
        if route.route.shared:
            # Telegram uses the same private chat id across bots. Keep shared bot
            # windows separate while leaving existing dedicated transcript paths intact.
            bot = hashlib.sha256(route.poll_key.encode()).hexdigest()[:16]
            conversation = f"shared-{bot}-{conversation}"
        transcript = Transcript(resident.manifest, conversation)
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
        reply = self._reply(route, message, self._answer_text(route, result))
        post_outcomes = "\n".join(
            line for outcome in session.posts if (line := _post_outcome_line(outcome))
        )
        transcript_text = reply if not post_outcomes else f"{reply}\n\n{post_outcomes}"
        transcript.append(resident.id, transcript_text, now=session.completed_at or now)
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

    def _reply(self, route: ChatRoute, message: Message, text: str, *, tag: bool = True) -> str:
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
        if message.access != ConversationAccess.PRIVATE and message.conversation not in (
            self._listened_channels.get(route.key, frozenset())
        ):
            return ""
        sent = _bounded(f"{route.name}: {text}" if tag and route.route.shared else text)
        token = self.token_for(route)
        if token is None:  # pragma: no cover — nothing reaches here without a token
            return sent
        try:
            transport = self.transports[route.address.transport]
            referenced = getattr(transport, "send_reply", None)
            delivered = (
                referenced(token, message.conversation, sent, message.reply_to)
                if message.reply_to and callable(referenced)
                else transport.send(token, message.conversation, sent)
            )
        except Exception as exc:  # noqa: BLE001 — a transport that raises is still just a failure
            log.warning("%s: chat transport raised while replying: %s", route.key, exc)
            return sent
        if not delivered:
            log.warning("%s: the reply did not reach the conversation", route.key)
        return sent

    def _operators_for(self, transport: str) -> frozenset[str]:
        """Return the allowlist belonging to one route's transport."""
        scoped = self.operators_by_transport.get(transport)
        if scoped is not None:
            return scoped
        return self.operators if transport == self.transport.name else frozenset()

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
        # Only the problems that make this *process* pointless (warren#456). A route that
        # cannot carry a message is reported by the first pass and skipped by every one
        # after it, until a recheck finds it healthy again.
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
                if not any(health.reachable for health in self._health.values()):
                    # Every door is shut, each for a reason that is re-asked on a timer.
                    # Waiting for that timer is the whole of the work left, so wait for it
                    # rather than spinning through empty passes a second apart. Read off the
                    # health the pass above just measured, so the loop never asks the clock
                    # a second time and never re-checks a route outside a pass.
                    sleep(self.recheck_s)
                elif any(outcome.status is ChatStatus.UNREACHABLE for outcome in pass_outcomes):
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


# --------------------------------------------------------------------------------------
# routines[].deliver: chat — a routine's final message, sent to the operators
# --------------------------------------------------------------------------------------


@dataclass(frozen=True)
class RoutineDelivery:
    """Carries a finished routine's final message to each operator (warren#385).

    The outbound half the bridge deliberately did not have, and kept as narrow as it can
    be: no polling, no session, no transcript — one text, sent once into each operator's
    private conversation with the resident's own bot, through the same redact-then-bound
    egress every chat reply takes (:func:`_bounded`). It satisfies
    :class:`steward.scheduler.Deliverer`, and the scheduler is the only caller: *whether*
    anything is sent (the routine said ``deliver:``, the run finished, the text is not the
    quiet word) is decided there, before this is asked.

    ``deliver: chat`` names the route *kind* and resolves only when there is exactly one
    active chat route. ``deliver: discord:hob`` names one route address. In either form the
    address chooses a :class:`ChatTransport`; Discord remains an adapter rather than a
    branch in delivery.
    """

    tokens: Mapping[str, str] = field(default_factory=dict, repr=False)
    operators: frozenset[str] = frozenset()
    transport: ChatTransport = field(default_factory=TelegramTransport.from_env)
    operators_by_transport: Mapping[str, frozenset[str]] = field(default_factory=dict)
    transports: Mapping[str, ChatTransport] = field(default_factory=dict)

    @classmethod
    def from_env(
        cls,
        env: Mapping[str, str] | None = None,
        *,
        transport: ChatTransport | None = None,
        transports: Mapping[str, ChatTransport] | None = None,
    ) -> RoutineDelivery:
        """Read tokens and transport-scoped operators the way the bridge reads them.

        ``transport`` keeps the test/integration seam from warren#385. New callers may
        provide ``transports``; supplying both is an error rather than an order-dependent
        override.
        """
        if transport is not None and transports is not None:
            raise ValueError("provide transport or transports, not both")
        source = os.environ if env is None else env
        if transports is not None:
            available = dict(transports)
        elif transport is not None:
            available = {transport.name: transport}
        else:
            available = {
                TELEGRAM: TelegramTransport.from_env(source),
                DISCORD: DiscordTransport.from_env(source),
            }
        return cls(
            tokens=tokens_from_env(source),
            operators=operators_from_env(source),
            transport=available.get(TELEGRAM, next(iter(available.values()))),
            operators_by_transport={
                name: operators_from_env(source, transport=name) for name in available
            },
            transports=available,
        )

    def deliver(self, resident: Resident, routine: Routine, text: str) -> Delivery:
        """Send ``text`` to every operator through the resident's chat route. Never raises.

        Every refusal names what is missing in steward's own words — the variable, the
        route, the transport — and never the text itself: the reason lands on the run row,
        and the run row is not where a digest belongs.
        """
        routes = chat_routes([resident])
        if not routes:
            return Delivery(
                status=DELIVERY_FAILED,
                reason=f"resident {resident.id!r} has no active chat route to deliver into",
            )
        route, refusal = _delivery_route(resident, routine, routes)
        if route is None:
            return Delivery(status=DELIVERY_FAILED, reason=refusal)
        transport = self._transport_for(route.address.transport)
        if transport is None:
            return Delivery(
                status=DELIVERY_FAILED,
                reason=(
                    f"route {route.key} is on transport {route.address.transport!r}, and "
                    "no configured chat transport can deliver there"
                ),
            )
        token = self.tokens.get(route.address.token_env)
        if not token:
            return Delivery(
                status=DELIVERY_FAILED,
                reason=f"{route.address.token_env} is not set, so nothing can speak as the bot",
            )
        operators = self._operators_for(route.address.transport)
        if not operators:
            return Delivery(
                status=DELIVERY_FAILED,
                reason=(
                    f"{OPERATORS_ENV} has no {route.address.transport} operators; "
                    "there is nobody to deliver to"
                ),
            )
        sent = _bounded(f"{route.name}: {text}" if route.route.shared else text)
        reached = 0
        for operator in sorted(operators):
            try:
                landed = transport.send(token, operator, sent)
            except Exception as exc:  # noqa: BLE001 — a transport that raises is still just a failure
                log.warning("%s: chat transport raised while delivering: %s", route.key, exc)
                landed = False
            reached += bool(landed)
        total = len(operators)
        if reached == 0:
            return Delivery(
                status=DELIVERY_FAILED,
                reason=f"routine {routine.id!r}: reached 0 of {total} operators",
            )
        if reached < total:
            return Delivery(status=DELIVERED, reason=f"reached {reached} of {total} operators")
        return Delivery(status=DELIVERED)

    def _transport_for(self, name: str) -> ChatTransport | None:
        """Resolve a transport while preserving the original direct-constructor seam."""
        available = self.transports or {self.transport.name: self.transport}
        return available.get(name)

    def _operators_for(self, transport: str) -> frozenset[str]:
        """Resolve scoped operators, falling back only for the legacy transport."""
        scoped = self.operators_by_transport.get(transport)
        if scoped is not None:
            return scoped
        return self.operators if transport == self.transport.name else frozenset()


def _delivery_route(
    resident: Resident, routine: Routine, routes: Sequence[ChatRoute]
) -> tuple[ChatRoute | None, str]:
    """Resolve one routine's validated target, defensively, at the delivery boundary."""
    if routine.deliver == ROUTINE_DELIVER_CHAT:
        if len(routes) == 1:
            return routes[0], ""
        return None, (
            f"resident {resident.id!r} has {len(routes)} active chat routes; "
            "bare deliver: chat needs exactly one"
        )
    route = next(
        (candidate for candidate in routes if str(candidate.address) == routine.deliver), None
    )
    if route is not None:
        return route, ""
    return None, f"resident {resident.id!r} has no active chat route at {routine.deliver!r}"


def _bounded(text: str) -> str:
    """Scrub a reply of secrets and *then* bound it. Never the other way round."""
    scrubbed = redact_secrets(text)
    if len(scrubbed) <= REPLY_MAX_CHARS:
        return scrubbed
    return scrubbed[: REPLY_MAX_CHARS - 1].rstrip() + "…"
