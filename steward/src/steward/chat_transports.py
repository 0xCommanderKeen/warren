"""Telegram and Discord HTTP adapters, with transport-owned discovery and cursors."""

import hashlib
import json
import logging
import math
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Protocol

from steward.chat_config import (
    DEFAULT_POLL_TIMEOUT_S,
    DISCORD,
    TELEGRAM,
    operators_from_env,
    poll_timeout_from_env,
)
from steward.manifest import redact_secrets
from steward.notify import DISCORD_USER_AGENT

log = logging.getLogger("steward.chat")

#: Where Telegram's bot API lives. Overridable for exactly one reason: the test suite points
#: it at a local server, so nothing in this repo can reach the real API. Mirrors
#: :data:`steward.notify.NTFY_URL_ENV`, and for the same reason.
DEFAULT_API_URL = "https://api.telegram.org"
API_URL_ENV = "STEWARD_CHAT_API_URL"
DISCORD_API_URL_ENV = "STEWARD_CHAT_DISCORD_API_URL"
DEFAULT_DISCORD_API_URL = "https://discord.com/api/v10"

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
DISCORD_REPLY_CHARS = 1900
DISCORD_PAGE_SIZE = 50
DISCORD_MEMBER_PAGE_SIZE = 1000
DISCORD_MEMBER_MAX_PAGES = 100
HTTP_TOO_MANY_REQUESTS = 429
MAX_RATE_LIMIT_SLEEP_S = 30.0


# --------------------------------------------------------------------------------------
# the wire
# --------------------------------------------------------------------------------------


class ConversationAccess(StrEnum):
    """Why a conversation is, or is not, allowed to cross the bridge."""

    PRIVATE = "private"
    PUBLIC = "public"
    ALLOWLISTED_PUBLIC = "allowlisted_public"


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
    #: Whether this is private, public, or public with manifest consent.
    access: ConversationAccess = ConversationAccess.PRIVATE
    #: Whether the transport says the sender is another bot. Bots never start residents.
    bot: bool = False
    #: Transport message id to attach a reply to, when the transport supports references.
    reply_to: str | None = None

    @property
    def private(self) -> bool:
        """Whether this is a one-to-one conversation."""
        return self.access == ConversationAccess.PRIVATE

    def age_s(self, now: datetime) -> float:
        """How long ago this was sent, in seconds."""
        return (now - self.at).total_seconds()


class ChatTransport(Protocol):
    """Anything that can carry a conversation. Telegram and Discord REST ship today.

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
        """Return a complete batch, or ``None`` without consuming unseen messages.

        Telegram uses the bridge's next-update ``offset``. Discord owns per-channel
        cursors and commits them only after every channel in this poll succeeds.
        Startup seeds Discord cursors at the latest messages, skipping old history.
        """
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
        access=(
            ConversationAccess.PRIVATE
            if chat.get("type") == "private"
            else ConversationAccess.PUBLIC
        ),
    )


@dataclass
class _DiscordState:
    """Per-bot DM discovery and cursors, keyed without retaining its credential."""

    channels: dict[str, str] = field(default_factory=dict)
    listened_channels: set[str] = field(default_factory=set)
    channel_names: dict[str, str] = field(default_factory=dict)
    cursors: dict[str, int] = field(default_factory=dict)
    bot_id: str = ""
    started: bool = False


@dataclass
class DiscordTransport:
    """Discord bot DMs and allowlisted channel mentions, polled over REST."""

    base_url: str = DEFAULT_DISCORD_API_URL
    operators: frozenset[str] = frozenset()
    guild: str = ""
    timeout_s: float = SEND_TIMEOUT_S
    sleep: Callable[[float], None] = time.sleep
    _states: dict[str, _DiscordState] = field(default_factory=dict, init=False, repr=False)

    @property
    def name(self) -> str:
        """The address transport that selects Discord REST."""
        return DISCORD

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> DiscordTransport:
        """Build the Discord adapter from process configuration."""
        source = os.environ if env is None else env
        return cls(
            base_url=(
                (source.get(DISCORD_API_URL_ENV) or "").strip() or DEFAULT_DISCORD_API_URL
            ).rstrip("/"),
            operators=operators_from_env(source, transport=DISCORD),
            guild=(source.get("STEWARD_CHAT_DISCORD_GUILD") or "").strip(),
        )

    def listen(self, token: str, names: Sequence[str]) -> bool:
        """Replace this bot's listening set; failed discovery revokes the old set.

        Empty names revoke all rooms. Newly added rooms start at their latest message
        when already polling. The bridge must replace the set on reload, including
        clearing tokens no longer declared, then read ``listening_channels`` to route
        only the rooms that this adapter actually registered.
        """
        state = self._state(token)
        previous = state.listened_channels
        state.listened_channels = set()
        state.channel_names = {}
        if not names:
            return True
        if not self.guild:
            return False
        identity = self._request(token, "GET", "/users/@me")
        bot_id = identity.get("id") if isinstance(identity, Mapping) else None
        channels = self.channels(token, self.guild)
        if not isinstance(bot_id, str) or not bot_id or channels is None:
            return False
        resolved = [channels.get(name) for name in names]
        if any(channel is None for channel in resolved):
            return False
        state.bot_id = bot_id
        state.channel_names = {name: channels[name] for name in names}
        desired = set(state.channel_names.values())
        for channel in desired - previous:
            if state.started and not self._seed_channel_cursor(token, channel):
                state.channel_names = {}
                return False
        state.listened_channels = desired
        return True

    def listening_channels(self, token: str, names: Sequence[str]) -> frozenset[str]:
        """Return currently registered channel ids belonging to these declared names."""
        state = self._state(token)
        return frozenset(state.channel_names[name] for name in names if name in state.channel_names)

    def identity(self, token: str) -> str | None:
        """Authenticate the token and return its visible bot username."""
        value = self._request(token, "GET", "/users/@me")
        if not isinstance(value, Mapping):
            return None
        username = value.get("username")
        return f"@{username}" if isinstance(username, str) and username else None

    def channels(self, token: str, guild: str) -> Mapping[str, str] | None:
        """Resolve one guild's text channel names to ids without exposing ids in manifests."""
        value = self._request(token, "GET", f"/guilds/{guild}/channels")
        if not isinstance(value, list):
            return None
        pairs = [
            (str(item["name"]), str(item["id"]))
            for item in value
            if isinstance(item, Mapping)
            and isinstance(item.get("name"), str)
            and str(item.get("id", "")).isdigit()
            and item.get("type") == 0
        ]
        if len({name for name, _channel_id in pairs}) != len(pairs):
            return None
        return dict(pairs)

    def admin(
        self,
        token: str,
        method: str,
        path: str,
        payload: Mapping[str, object] | None = None,
    ) -> bool:
        """Perform one scoped Discord write assembled by the policy-owning Poster."""
        return self._request(token, method, path, payload) is not None

    def guild_snapshot(self, token: str, guild: str) -> Mapping[str, object] | None:
        """Read the guild context used by scoped resident mirror files."""
        channels = self._request(token, "GET", f"/guilds/{guild}/channels")
        if not isinstance(channels, list):
            return None
        members: list[object] = []
        after = ""
        for _page in range(DISCORD_MEMBER_MAX_PAGES):
            suffix = f"&after={after}" if after else ""
            page = self._request(
                token,
                "GET",
                f"/guilds/{guild}/members?limit={DISCORD_MEMBER_PAGE_SIZE}{suffix}",
            )
            if not isinstance(page, list):
                return None
            members.extend(page)
            if len(page) < DISCORD_MEMBER_PAGE_SIZE:
                break
            last = page[-1]
            user = last.get("user") if isinstance(last, Mapping) else None
            snowflake = user.get("id") if isinstance(user, Mapping) else None
            if not isinstance(snowflake, str) or not snowflake.isdigit() or snowflake == after:
                return None
            after = snowflake
        else:
            return None
        return {"channels": channels, "members": members}

    def threads(self, token: str, guild: str) -> frozenset[str] | None:
        """Resolve active thread ids inside one configured guild."""
        value = self._request(token, "GET", f"/guilds/{guild}/threads/active")
        if not isinstance(value, Mapping) or not isinstance(value.get("threads"), list):
            return None
        return frozenset(
            str(item["id"])
            for item in value["threads"]
            if isinstance(item, Mapping) and str(item.get("id", "")).isdigit()
        )

    def poll(self, token: str, offset: int) -> list[Message] | None:
        """Open operator DMs once, then fetch each channel after its cursor."""
        del offset  # Discord cursors are per DM channel, not per bot.
        state = self._state(token)
        if not state.started:
            if not self._ensure_started(token):
                return None
            return []
        found: list[Message] = []
        cursors = state.cursors.copy()
        dm_channels = set(state.channels.values())
        for channel in (*state.channels.values(), *sorted(state.listened_channels)):
            messages = self._messages_after(token, channel, cursors.get(channel, 0))
            if messages is None:
                return None
            for raw in messages:
                raw_id = (
                    int(raw["id"])
                    if isinstance(raw, Mapping) and str(raw.get("id", "")).isdigit()
                    else 0
                )
                cursors[channel] = max(cursors.get(channel, 0), raw_id)
                message = self._message(
                    raw,
                    channel,
                    private=channel in dm_channels,
                    bot_id=state.bot_id,
                )
                if message is not None:
                    found.append(message)
        # A failed later channel returns no messages: keep every cursor retryable until
        # the complete batch can be handed to the bridge.
        state.cursors = cursors
        return found

    def send(self, token: str, conversation: str, text: str) -> bool:
        """Send a bounded sequence of Discord-sized message chunks."""
        return self.send_reply(token, conversation, text, None)

    def send_reply(self, token: str, conversation: str, text: str, reply_to: str | None) -> bool:
        """Send Discord-sized chunks, optionally attached to one triggering message."""
        if not text.strip():
            return False
        target = conversation
        if conversation in self.operators:
            if not self._ensure_started(token):
                return False
            target = self._state(token).channels.get(conversation, "")
            if not target:
                return False
        chunks = [
            text[start : start + DISCORD_REPLY_CHARS]
            for start in range(0, len(text), DISCORD_REPLY_CHARS)
        ]
        for chunk in chunks:
            payload: dict[str, object] = {"content": chunk}
            if reply_to:
                payload["message_reference"] = {
                    "message_id": reply_to,
                    "channel_id": target,
                }
            if self._request(token, "POST", f"/channels/{target}/messages", payload) is None:
                return False
        return True

    def _open_channels(self, token: str) -> bool:
        state = self._state(token)
        for operator in sorted(self.operators):
            value = self._request(token, "POST", "/users/@me/channels", {"recipient_id": operator})
            if not isinstance(value, Mapping) or not isinstance(value.get("id"), str):
                return False
            channel = value["id"]
            state.channels[operator] = channel
            if not self._seed_channel_cursor(token, channel):
                return False
        for channel in sorted(state.listened_channels):
            if not self._seed_channel_cursor(token, channel):
                return False
        return True

    def _seed_channel_cursor(self, token: str, channel: str) -> bool:
        """Start one channel after its newest message, never in its history."""
        latest = self._request(token, "GET", f"/channels/{channel}/messages?limit=1")
        if not isinstance(latest, list):
            return False
        ids = [
            int(item["id"])
            for item in latest
            if isinstance(item, Mapping) and str(item.get("id", "")).isdigit()
        ]
        self._state(token).cursors[channel] = max(ids, default=0)
        return True

    def _ensure_started(self, token: str) -> bool:
        state = self._state(token)
        if state.started:
            return True
        if not self._open_channels(token):
            return False
        state.started = True
        return True

    def _state(self, token: str) -> _DiscordState:
        key = hashlib.sha256(token.encode()).hexdigest()
        return self._states.setdefault(key, _DiscordState())

    def _messages_after(self, token: str, channel: str, cursor: int) -> list[object] | None:
        """Drain every unseen page without advancing past messages Discord omitted."""
        query = urllib.parse.urlencode({"after": str(cursor), "limit": str(DISCORD_PAGE_SIZE)})
        value = self._request(token, "GET", f"/channels/{channel}/messages?{query}")
        if not isinstance(value, list):
            return None
        found: list[object] = list(value)
        page = value
        while len(page) == DISCORD_PAGE_SIZE:
            ids = [
                int(item["id"])
                for item in page
                if isinstance(item, Mapping) and str(item.get("id", "")).isdigit()
            ]
            if not ids or min(ids) <= cursor:
                break
            query = urllib.parse.urlencode(
                {"before": str(min(ids)), "limit": str(DISCORD_PAGE_SIZE)}
            )
            older = self._request(token, "GET", f"/channels/{channel}/messages?{query}")
            if not isinstance(older, list):
                return None
            page = [
                item
                for item in older
                if isinstance(item, Mapping)
                and str(item.get("id", "")).isdigit()
                and int(item["id"]) > cursor
            ]
            found.extend(page)
        return found

    @staticmethod
    def _message(
        raw: object, channel: str, *, private: bool = True, bot_id: str = ""
    ) -> Message | None:
        if not isinstance(raw, Mapping):
            return None
        author = raw.get("author")
        content = raw.get("content")
        snowflake = str(raw.get("id") or "")
        if (
            not isinstance(author, Mapping)
            or not isinstance(content, str)
            or not content.strip()
            or not snowflake.isdigit()
        ):
            return None
        sender = str(author.get("id") or "")
        if not sender:
            return None
        if not private:
            mentions = raw.get("mentions")
            if not isinstance(mentions, list) or not any(
                isinstance(mention, Mapping) and str(mention.get("id") or "") == bot_id
                for mention in mentions
            ):
                return None
        timestamp = raw.get("timestamp")
        try:
            at = datetime.fromisoformat(str(timestamp))
        except ValueError:
            at = datetime.now(UTC)
        return Message(
            update_id=int(snowflake),
            conversation=channel,
            sender=f"{DISCORD}:{sender}",
            text=content,
            at=at,
            access=(
                ConversationAccess.PRIVATE if private else ConversationAccess.ALLOWLISTED_PUBLIC
            ),
            bot=bool(author.get("bot")),
            reply_to=snowflake,
        )

    def _request(
        self, token: str, method: str, path: str, payload: Mapping[str, Any] | None = None
    ) -> object | None:
        url = f"{self.base_url}{path}"
        if not url.startswith(("http://", "https://")):
            return None
        data = None if payload is None else json.dumps(dict(payload)).encode()
        request = urllib.request.Request(  # noqa: S310 — scheme checked just above
            url,
            data=data,
            headers={
                "Authorization": f"Bot {token}",
                "Content-Type": "application/json",
                "User-Agent": DISCORD_USER_AGENT,
            },
            method=method,
        )
        for attempt in range(2):
            try:
                with urllib.request.urlopen(request, timeout=self.timeout_s) as response:  # noqa: S310
                    body = response.read().decode()
                    return json.loads(body) if body else {}
            except urllib.error.HTTPError as exc:
                if exc.code == HTTP_TOO_MANY_REQUESTS and attempt == 0:
                    try:
                        body = json.loads(exc.read().decode())
                        delay = float(body.get("retry_after", 0))
                    except ValueError, TypeError, AttributeError:
                        return None
                    if not math.isfinite(delay) or not 0 <= delay <= MAX_RATE_LIMIT_SLEEP_S:
                        log.warning("discord rate limit delay is unsafe; deferring this route")
                        return None
                    self.sleep(delay)
                    continue
                log.warning(
                    "discord %s %s failed — HTTP %s", method, path.split("?", 1)[0], exc.code
                )
                return None
            except (OSError, urllib.error.URLError, ValueError) as exc:
                log.warning("discord %s failed — %s: %s", method, type(exc).__name__, exc)
                return None
        return None
