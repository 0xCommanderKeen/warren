"""Parse and execute resident-authored Discord room posts after a session."""

import json
import logging
import os
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Protocol

from steward import approvals, prompt
from steward import events as ev
from steward.chat import Address, DiscordTransport, tokens_from_env
from steward.manifest import Resident, ResidentManifest, redact_secrets
from steward.store import Store
from steward.transitions.approval import ApprovalTransitions

GUILD_ENV = "STEWARD_CHAT_DISCORD_GUILD"
MAX_POSTS = 5
MAX_ACTIONS = 5
POST_MAX_CHARS = 1900
CHANNEL_NAME_MAX_CHARS = 100
TOPIC_MAX_CHARS = 1024
SNOWFLAKE_MAX_CHARS = 20
REJECTED_ACTION = "rejected_post"
UNREADABLE = "unreadable_block"
NOT_ALLOWED = "channel_not_allowed"
UNKNOWN_CHANNEL = "unknown_channel"
UNAVAILABLE = "transport_unavailable"
MISSING_SCOPE = "missing_scope"

ADMIN_VERBS = {
    "create_channel": "channels.manage",
    "set_topic": "channels.manage",
    "create_thread": "threads.manage",
    "archive_thread": "threads.manage",
    "pin": "messages.pin",
}

log = logging.getLogger("steward.discord_posts")

_BLOCK = re.compile(r"<discord\s+post(?P<attrs>[^>]*)>(?P<body>.*?)</discord>", re.DOTALL)
_ATTRIBUTE = re.compile(r'(?P<name>[a-z][a-z-]*)\s*=\s*"(?P<value>[^"]*)"')
_ADMIN_BLOCK = re.compile(
    r"<discord\s+(?P<verb>create_channel|set_topic|create_thread|archive_thread|pin)"
    r"(?P<attrs>[^>]*)>(?P<body>.*?)</discord>",
    re.DOTALL,
)


@dataclass(frozen=True, slots=True)
class Post:
    """One parsed Discord room-post request, valid or malformed."""

    raw: str
    channel: str = ""
    text: str = ""
    problem: str | None = None

    @property
    def ok(self) -> bool:
        """Say whether the action carries the required channel and JSON text."""
        return self.problem is None


@dataclass(frozen=True, slots=True)
class PostOutcome:
    """The transcript-safe result of attempting one post."""

    post: Post
    posted: bool = False
    reason: str | None = None
    message: str = ""

    def transcript_line(self) -> str:
        """Render the outcome without copying resident-authored message text."""
        state = "posted" if self.posted else f"refused: {self.reason}"
        return f"Discord post to #{self.post.channel or 'unknown'} — {state}"


@dataclass(frozen=True, slots=True)
class AdminAction:
    """One parsed Discord administration request."""

    raw: str
    verb: str
    attrs: Mapping[str, str] = field(default_factory=dict)
    body: Mapping[str, object] = field(default_factory=dict)
    problem: str | None = None


@dataclass(frozen=True, slots=True)
class AdminOutcome:
    """Transcript-safe outcome for one Discord administration request."""

    action: AdminAction
    posted: bool = False
    reason: str | None = None

    def transcript_line(self) -> str:
        """Render the verb outcome without resident-authored content."""
        state = "completed" if self.posted else f"refused: {self.reason}"
        return f"Discord {self.action.verb} — {state}"


def extract_posts(output: str) -> list[Post]:
    """Read Discord post blocks from an already isolated machine-action region."""
    found: list[Post] = []
    for match in _BLOCK.finditer(output or ""):
        raw = match.group(0)
        attrs = match.group("attrs")
        parsed_attrs = [
            (item.group("name"), item.group("value")) for item in _ATTRIBUTE.finditer(attrs)
        ]
        values = dict(parsed_attrs)
        leftover = _ATTRIBUTE.sub("", attrs).strip()
        if (
            leftover
            or len(parsed_attrs) != 1
            or set(values) != {"channel"}
            or not values.get("channel", "").strip()
        ):
            found.append(Post(raw=raw, problem='the block needs only channel="name"'))
            continue
        try:
            body = json.loads(match.group("body").strip())
        except ValueError as exc:
            found.append(
                Post(raw=raw, channel=values["channel"].strip(), problem=f"invalid JSON: {exc}")
            )
            continue
        if (
            not isinstance(body, Mapping)
            or set(body) != {"text"}
            or not isinstance(body.get("text"), str)
            or not body["text"].strip()
        ):
            found.append(
                Post(
                    raw=raw,
                    channel=values["channel"].strip(),
                    problem='the body must be {"text": "…"}',
                )
            )
            continue
        found.append(Post(raw=raw, channel=values["channel"].strip(), text=body["text"].strip()))
    consumed = _BLOCK.sub("", output or "")
    if "<discord post" in consumed:
        found.append(
            Post(
                raw=consumed[consumed.index("<discord post") :][:8000],
                problem="the post block is not closed",
            )
        )
    return found


def extract_admin_actions(output: str) -> list[AdminAction]:
    """Parse the fixed, deletion-free Discord administration vocabulary."""
    found: list[AdminAction] = []
    for match in _ADMIN_BLOCK.finditer(output or ""):
        raw = match.group(0)
        parsed = [
            (item.group("name"), item.group("value"))
            for item in _ATTRIBUTE.finditer(match.group("attrs"))
        ]
        attrs = dict(parsed)
        leftover = _ATTRIBUTE.sub("", match.group("attrs")).strip()
        try:
            body = json.loads(match.group("body").strip() or "{}")
        except ValueError:
            body = None
        problem = None
        if leftover or len(attrs) != len(parsed) or not isinstance(body, Mapping):
            problem = "invalid attributes or JSON object"
        found.append(
            AdminAction(
                raw=raw,
                verb=match.group("verb"),
                attrs=attrs,
                body=body if isinstance(body, Mapping) else {},
                problem=problem,
            )
        )
    consumed = _ADMIN_BLOCK.sub("", _BLOCK.sub("", output or ""))
    if "<discord" in consumed:
        raw = consumed[consumed.index("<discord") :][:8000]
        verb_match = re.match(r"<discord\s+([a-z_]+)", raw)
        found.append(
            AdminAction(
                raw=raw,
                verb=verb_match.group(1) if verb_match else "unknown",
                problem="unknown or unclosed Discord action block",
            )
        )
    return found


class RoomTransport(Protocol):
    """The narrow Discord capabilities the executor needs."""

    def channels(self, token: str, guild: str) -> Mapping[str, str] | None:
        """Resolve text-channel names to Discord channel identifiers."""
        ...

    def send(self, token: str, conversation: str, text: str) -> bool:
        """Send text to an already-resolved channel identifier."""
        ...

    def admin(
        self,
        token: str,
        method: str,
        path: str,
        payload: Mapping[str, object] | None = None,
    ) -> bool:
        """Perform one already-authorized Discord REST mutation."""
        ...

    def threads(self, token: str, guild: str) -> frozenset[str] | None:
        """Resolve active thread identifiers belonging to the configured guild."""
        ...


@dataclass
class Poster:
    """Allowlist, resolve, bound, post, announce, and knock for session control blocks."""

    residents: Sequence[Resident]
    store: Store
    emitter: ev.Emitter = field(default_factory=ev.NullEmitter)
    transport: RoomTransport | None = None
    tokens: Mapping[str, str] = field(default_factory=dict, repr=False)
    guild: str = ""
    _channels: dict[str, Mapping[str, str] | None] = field(default_factory=dict, init=False)
    _threads: dict[str, frozenset[str] | None] = field(default_factory=dict, init=False)

    @classmethod
    def from_env(
        cls,
        residents: Sequence[Resident],
        store: Store,
        emitter: ev.Emitter,
        *,
        transport: RoomTransport | None = None,
        env: Mapping[str, str] | None = None,
    ) -> Poster:
        """Build one daemon-lifetime executor from operator configuration."""
        source = os.environ if env is None else env
        poster = cls(
            residents=residents,
            store=store,
            emitter=emitter,
            transport=transport or DiscordTransport.from_env(source),
            tokens=tokens_from_env(source),
            guild=(source.get(GUILD_ENV) or "").strip(),
        )
        poster._resolve_channels()
        return poster

    def _resolve_channels(self) -> None:
        """Resolve each configured Discord bot's room directory once for this daemon."""
        if self.transport is None or not self.guild:
            return
        for resident in self.residents:
            scopes = {
                scope
                for grant in resident.manifest.app_grants
                if grant.id == "discord" and grant.status == "granted"
                for scope in grant.scopes
            }
            for route in resident.manifest.routes:
                address = Address.parse(route.address)
                if not route.accepts_chat or address is None or address.transport != "discord":
                    continue
                needs_channels = bool(
                    route.posts_to or scopes & {"channels.manage", "threads.manage", "messages.pin"}
                )
                if not needs_channels:
                    continue
                token = self.tokens.get(address.token_env)
                if not token:
                    self._channels[address.token_env] = None
                    continue
                try:
                    if address.token_env not in self._channels:
                        self._channels[address.token_env] = self.transport.channels(
                            token, self.guild
                        )
                    if "threads.manage" in scopes and address.token_env not in self._threads:
                        self._threads[address.token_env] = self.transport.threads(token, self.guild)
                except Exception as exc:  # noqa: BLE001 — configuration becomes a refusal
                    log.warning("Discord channel resolution failed: %s", exc)
                    self._channels[address.token_env] = None
                    self._threads[address.token_env] = None

    def refresh(self, residents: Sequence[Resident]) -> None:
        """Adopt refreshed manifests and resolve only newly introduced bot slots."""
        self.residents = tuple(residents)
        self._resolve_channels()

    def harvest(
        self, manifest: ResidentManifest, output: str, now: datetime | None = None
    ) -> tuple[PostOutcome | AdminOutcome, ...]:
        """Execute bounded post actions from one completed session."""
        resident = next((item for item in self.residents if item.id == manifest.id), None)
        if resident is None:
            return ()
        actions = prompt.harvestable(output)
        posts = extract_posts(actions)
        admins = extract_admin_actions(actions)
        outcomes: list[PostOutcome | AdminOutcome] = [
            self._execute(resident, post, now) for post in posts[:MAX_ACTIONS]
        ]
        remaining = max(0, MAX_ACTIONS - len(outcomes))
        outcomes.extend(self._execute_admin(resident, action, now) for action in admins[:remaining])
        if len(posts) + len(admins) > MAX_ACTIONS:
            if len(posts) > MAX_ACTIONS:
                outcomes.append(
                    self._refuse(resident, posts[MAX_ACTIONS], "post_limit_exceeded", now)
                )
            else:
                outcomes.append(
                    self._refuse_admin(
                        resident, admins[MAX_ACTIONS - len(posts)], "post_limit_exceeded", now
                    )
                )
        return tuple(outcomes)

    def _execute_admin(
        self, resident: Resident, action: AdminAction, now: datetime | None
    ) -> AdminOutcome:
        if action.problem or action.verb not in ADMIN_VERBS:
            return self._refuse_admin(resident, action, UNREADABLE, now)
        scope = ADMIN_VERBS[action.verb]
        granted = any(
            grant.id == "discord" and grant.status == "granted" and scope in grant.scopes
            for grant in resident.manifest.app_grants
        )
        if not granted:
            return self._refuse_admin(resident, action, MISSING_SCOPE, now, scope)
        route = next(
            (
                route
                for route in resident.manifest.routes
                if route.accepts_chat and route.address.startswith("discord:")
            ),
            None,
        )
        address = Address.parse(route.address) if route else None
        token = self.tokens.get(address.token_env) if address else None
        if not route or not address or not token or not self.guild or self.transport is None:
            return self._refuse_admin(resident, action, UNAVAILABLE, now)
        if address.token_env not in self._channels:
            self._channels[address.token_env] = self.transport.channels(token, self.guild)
        if action.verb == "archive_thread" and address.token_env not in self._threads:
            self._threads[address.token_env] = self.transport.threads(token, self.guild)
        call = self._admin_call(address.token_env, action)
        if call is None:
            return self._refuse_admin(resident, action, UNREADABLE, now)
        method, path, payload, channel = call
        if not self.transport.admin(token, method, path, payload):
            return self._refuse_admin(resident, action, UNAVAILABLE, now)
        self.emitter.emit(
            ev.discord_admin_event(
                event_type=f"discord_{action.verb.replace('create_', '')}_created"
                if action.verb in {"create_channel", "create_thread"}
                else {
                    "set_topic": "discord_topic_set",
                    "archive_thread": "discord_thread_archived",
                    "pin": "discord_message_pinned",
                }[action.verb],
                resident=resident,
                route=route.id,
                channel=channel,
            )
        )
        return AdminOutcome(action, posted=True)

    def _admin_call(
        self, token_env: str, action: AdminAction
    ) -> tuple[str, str, Mapping[str, object] | None, str] | None:
        channels = self._channels.get(token_env) or {}
        if action.verb == "create_channel" and set(action.body) == {"name"}:
            name = action.body.get("name")
            if isinstance(name, str) and 0 < len(name.strip()) <= CHANNEL_NAME_MAX_CHARS:
                return "POST", f"/guilds/{self.guild}/channels", {"name": name, "type": 0}, name
        if action.verb in {"set_topic", "create_thread"}:
            channel = action.attrs.get("channel", "")
            channel_id = channels.get(channel)
            key = "topic" if action.verb == "set_topic" else "name"
            value = action.body.get(key)
            limit = TOPIC_MAX_CHARS if action.verb == "set_topic" else CHANNEL_NAME_MAX_CHARS
            if (
                set(action.attrs) == {"channel"}
                and set(action.body) == {key}
                and channel_id
                and isinstance(value, str)
                and 0 < len(value.strip()) <= limit
            ):
                method = "PATCH" if action.verb == "set_topic" else "POST"
                path = f"/channels/{channel_id}" + (
                    "/threads" if action.verb == "create_thread" else ""
                )
                payload = {key: value} | ({"type": 11} if action.verb == "create_thread" else {})
                return method, path, payload, channel
        if action.verb == "archive_thread" and set(action.attrs) == {"thread"} and not action.body:
            thread = action.attrs["thread"]
            if (
                thread.isdigit()
                and len(thread) <= SNOWFLAKE_MAX_CHARS
                and thread in (self._threads.get(token_env) or frozenset())
            ):
                return "PATCH", f"/channels/{thread}", {"archived": True}, thread
        if action.verb == "pin" and set(action.attrs) == {"channel", "message"} and not action.body:
            channel, message = action.attrs["channel"], action.attrs["message"]
            channel_id = channels.get(channel)
            if channel_id and message.isdigit() and len(message) <= SNOWFLAKE_MAX_CHARS:
                return "PUT", f"/channels/{channel_id}/pins/{message}", None, channel
        return None

    def _refuse_admin(
        self,
        resident: Resident,
        action: AdminAction,
        reason: str,
        now: datetime | None,
        missing_scope: str = "",
    ) -> AdminOutcome:
        self.emitter.emit(
            ev.chat_post_refused_event(
                resident=resident, route="discord", channel=action.verb, reason=reason
            )
        )
        detail = {"reason": reason, "verb": action.verb}
        if missing_scope:
            detail["missing_scope"] = missing_scope
        ApprovalTransitions(store=self.store, emitter=self.emitter).knock(
            manifest=resident.manifest,
            request=approvals.NeedsHuman(
                raw=redact_secrets(action.raw)[:8000], action=REJECTED_ACTION, detail=detail
            ),
            message=(
                f"{resident.manifest.soul.name} tried Discord administration and steward refused"
            ),
            now=now or datetime.now(UTC),
        )
        return AdminOutcome(action, reason=reason)

    def _execute(self, resident: Resident, post: Post, now: datetime | None) -> PostOutcome:
        routes = [
            route
            for route in resident.manifest.routes
            if route.kind == "chat"
            and route.address.startswith("discord:")
            and route.accepts_chat
            and post.channel in route.posts_to
        ]
        if not post.ok:
            return self._refuse(resident, post, UNREADABLE, now)
        if not routes:
            return self._refuse(resident, post, NOT_ALLOWED, now)
        route = routes[0]
        address = Address.parse(route.address)
        if address is None:
            return self._refuse(resident, post, UNAVAILABLE, now, route.id)
        token = self.tokens.get(address.token_env)
        if not token or not self.guild or self.transport is None:
            return self._refuse(resident, post, UNAVAILABLE, now, route.id)
        key = address.token_env
        if key not in self._channels:
            self._channels[key] = self.transport.channels(token, self.guild)
        channel_id = (self._channels[key] or {}).get(post.channel)
        if not channel_id:
            return self._refuse(resident, post, UNKNOWN_CHANNEL, now, route.id)
        text = redact_secrets(post.text)[:POST_MAX_CHARS]
        if not self.transport.send(token, channel_id, text):
            return self._refuse(resident, post, UNAVAILABLE, now, route.id)
        self.emitter.emit(
            ev.chat_message_posted_event(
                resident=resident, route=route.id, channel=post.channel, length=len(text)
            )
        )
        return PostOutcome(post=post, posted=True, message="posted")

    def _refuse(
        self,
        resident: Resident,
        post: Post,
        reason: str,
        now: datetime | None,
        route: str = "discord",
    ) -> PostOutcome:
        self.emitter.emit(
            ev.chat_post_refused_event(
                resident=resident, route=route, channel=post.channel, reason=reason
            )
        )
        ApprovalTransitions(store=self.store, emitter=self.emitter).knock(
            manifest=resident.manifest,
            request=approvals.NeedsHuman(
                raw=redact_secrets(post.raw)[:8000],
                action=REJECTED_ACTION,
                detail={"reason": reason, "channel": post.channel, "problem": post.problem or ""},
            ),
            message=f"{resident.manifest.soul.name} tried to post to Discord and steward refused",
            now=now or datetime.now(UTC),
        )
        return PostOutcome(post=post, reason=reason, message=post.problem or reason)
