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
POST_MAX_CHARS = 1900
REJECTED_ACTION = "rejected_post"
UNREADABLE = "unreadable_block"
NOT_ALLOWED = "channel_not_allowed"
UNKNOWN_CHANNEL = "unknown_channel"
UNAVAILABLE = "transport_unavailable"

log = logging.getLogger("steward.discord_posts")

_BLOCK = re.compile(r"<discord\s+post(?P<attrs>[^>]*)>(?P<body>.*?)</discord>", re.DOTALL)
_ATTRIBUTE = re.compile(r'(?P<name>[a-z][a-z-]*)\s*=\s*"(?P<value>[^"]*)"')


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


class RoomTransport(Protocol):
    """The narrow Discord capabilities the executor needs."""

    def channels(self, token: str, guild: str) -> Mapping[str, str] | None:
        """Resolve text-channel names to Discord channel identifiers."""
        ...

    def send(self, token: str, conversation: str, text: str) -> bool:
        """Send text to an already-resolved channel identifier."""
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
            for route in resident.manifest.routes:
                address = Address.parse(route.address)
                if not route.accepts_chat or not route.posts_to or address is None:
                    continue
                if address.transport != "discord" or address.token_env in self._channels:
                    continue
                token = self.tokens.get(address.token_env)
                if not token:
                    self._channels[address.token_env] = None
                    continue
                try:
                    self._channels[address.token_env] = self.transport.channels(token, self.guild)
                except Exception as exc:  # noqa: BLE001 — configuration becomes a refusal
                    log.warning("Discord channel resolution failed: %s", exc)
                    self._channels[address.token_env] = None

    def refresh(self, residents: Sequence[Resident]) -> None:
        """Adopt refreshed manifests and resolve only newly introduced bot slots."""
        self.residents = tuple(residents)
        self._resolve_channels()

    def harvest(
        self, manifest: ResidentManifest, output: str, now: datetime | None = None
    ) -> tuple[PostOutcome, ...]:
        """Execute bounded post actions from one completed session."""
        resident = next((item for item in self.residents if item.id == manifest.id), None)
        if resident is None:
            return ()
        posts = extract_posts(prompt.harvestable(output))
        outcomes = [self._execute(resident, post, now) for post in posts[:MAX_POSTS]]
        if len(posts) > MAX_POSTS:
            outcomes.append(self._refuse(resident, posts[MAX_POSTS], "post_limit_exceeded", now))
        return tuple(outcomes)

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
