"""Chat transports behavior through the public chat interface."""

from typing import Any

import pytest

from steward import chat as ch
from steward import notify as nf
from support.chat import (
    CONVERSATION,
    FAKE_BOT_TOKEN,
    FAKE_DISCORD_TOKEN,
    OPERATOR,
)
from support.chat_http import BotApi, DiscordApi, discord_message
from support.chat_http import bot_api as bot_api  # noqa: PLC0414 — pytest fixture discovery
from support.chat_http import discord_api as discord_api  # noqa: PLC0414 — pytest fixture discovery


def test_discord_opens_operator_dms_and_starts_after_the_latest_message(
    discord_api: DiscordApi,
):
    discord_api.queue("POST", "/users/@me/channels", (200, {"id": "900"}))
    discord_api.queue("GET", "/channels/900/messages?limit=1", (200, [discord_message("100")]))
    discord_api.queue(
        "GET", "/channels/900/messages?after=100&limit=50", (200, [discord_message()])
    )
    transport = ch.DiscordTransport(base_url=discord_api.url, operators=frozenset({"31337"}))

    assert transport.poll(FAKE_DISCORD_TOKEN, 0) == []
    [received] = transport.poll(FAKE_DISCORD_TOKEN, 0) or []

    assert (received.update_id, received.conversation, received.sender, received.text) == (
        101,
        "900",
        "discord:31337",
        "hello",
    )
    assert all(call[3] == f"Bot {FAKE_DISCORD_TOKEN}" for call in discord_api.calls)


def test_discord_listened_channel_starts_at_latest_and_only_yields_bot_mentions(
    discord_api: DiscordApi,
):
    discord_api.queue("GET", "/users/@me", (200, {"id": "42", "username": "Pip"}))
    discord_api.queue(
        "GET", "/guilds/home/channels", (200, [{"id": "700", "name": "household", "type": 0}])
    )
    discord_api.queue("GET", "/channels/700/messages?limit=1", (200, [discord_message("100")]))
    discord_api.queue(
        "GET",
        "/channels/700/messages?after=100&limit=50",
        (
            200,
            [
                discord_message("101", content="ordinary channel talk"),
                discord_message("102", content="<@42> hello", mentions=("42",)),
            ],
        ),
    )
    transport = ch.DiscordTransport(base_url=discord_api.url, guild="home")

    assert transport.listen(FAKE_DISCORD_TOKEN, ["household"])
    assert transport.poll(FAKE_DISCORD_TOKEN, 0) == []
    [received] = transport.poll(FAKE_DISCORD_TOKEN, 0) or []

    assert (received.update_id, received.conversation, received.private) == (102, "700", False)


def test_discord_discovers_the_bot_handle(discord_api: DiscordApi):
    discord_api.queue("GET", "/users/@me", (200, {"id": "42", "username": "Pip"}))

    transport = ch.DiscordTransport(base_url=discord_api.url)

    assert transport.identity(FAKE_DISCORD_TOKEN) == "@Pip"
    # Discord's edge answers Python's default agent with 403 before looking at the token
    # (measured from the burrow, 2026-09-04), so every call must wear the documented one.
    assert discord_api.user_agents == [nf.DISCORD_USER_AGENT]


def test_discord_resolves_text_channels_by_name(discord_api: DiscordApi):
    discord_api.queue(
        "GET",
        "/guilds/home/channels",
        (
            200,
            [
                {"id": "42", "name": "household", "type": 0},
                {"id": "43", "name": "voice", "type": 2},
            ],
        ),
    )
    transport = ch.DiscordTransport(base_url=discord_api.url)

    assert transport.channels(FAKE_DISCORD_TOKEN, "home") == {"household": "42"}
    assert discord_api.calls[0][3] == f"Bot {FAKE_DISCORD_TOKEN}"


def test_discord_refuses_ambiguous_channel_names(discord_api: DiscordApi):
    discord_api.queue(
        "GET",
        "/guilds/home/channels",
        (
            200,
            [
                {"id": "42", "name": "announcements", "type": 0},
                {"id": "43", "name": "announcements", "type": 0},
            ],
        ),
    )
    transport = ch.DiscordTransport(base_url=discord_api.url)

    assert transport.channels(FAKE_DISCORD_TOKEN, "home") is None


@pytest.mark.parametrize(
    ("method", "path", "payload"),
    [
        ("POST", "/guilds/home/channels", {"name": "news", "type": 0}),
        ("PATCH", "/channels/42", {"topic": "Today"}),
        ("POST", "/channels/42/threads", {"name": "chores", "type": 11}),
        ("PATCH", "/channels/77", {"archived": True}),
        ("PUT", "/channels/42/pins/99", None),
    ],
)
def test_discord_admin_makes_exactly_one_authenticated_rest_call(
    discord_api: DiscordApi,
    method: str,
    path: str,
    payload: dict[str, object] | None,
):
    discord_api.queue(method, path, (200, {}))
    transport = ch.DiscordTransport(base_url=discord_api.url)

    assert transport.admin(FAKE_DISCORD_TOKEN, method, path, payload)
    assert discord_api.calls == [(method, path, payload, f"Bot {FAKE_DISCORD_TOKEN}")]


def test_discord_reads_channels_and_members_for_the_guild_mirror(discord_api: DiscordApi):
    discord_api.queue("GET", "/guilds/home/channels", (200, [{"id": "42"}]))
    discord_api.queue(
        "GET",
        "/guilds/home/members?limit=1000",
        (200, [{"user": {"id": "7"}, "joined_at": "2026-09-01T10:00:00Z"}]),
    )
    transport = ch.DiscordTransport(base_url=discord_api.url)

    assert transport.guild_snapshot(FAKE_DISCORD_TOKEN, "home") == {
        "channels": [{"id": "42"}],
        "members": [{"user": {"id": "7"}, "joined_at": "2026-09-01T10:00:00Z"}],
    }


def test_discord_guild_mirror_drains_every_member_page(discord_api: DiscordApi):
    first = [
        {"user": {"id": str(index)}, "joined_at": "2026-09-01T10:00:00Z"}
        for index in range(1, 1001)
    ]
    final = [{"user": {"id": "1001"}, "joined_at": "2026-09-02T10:00:00Z"}]
    discord_api.queue("GET", "/guilds/home/channels", (200, []))
    discord_api.queue("GET", "/guilds/home/members?limit=1000", (200, first))
    discord_api.queue("GET", "/guilds/home/members?limit=1000&after=1000", (200, final))
    transport = ch.DiscordTransport(base_url=discord_api.url)

    snapshot = transport.guild_snapshot(FAKE_DISCORD_TOKEN, "home")
    assert snapshot is not None
    members = snapshot["members"]
    assert isinstance(members, list)
    assert len(members) == 1001


def test_discord_resolves_only_threads_in_the_configured_guild(discord_api: DiscordApi):
    discord_api.queue(
        "GET",
        "/guilds/home/threads/active",
        (200, {"threads": [{"id": "77"}, {"id": "not-a-snowflake"}]}),
    )
    transport = ch.DiscordTransport(base_url=discord_api.url)

    assert transport.threads(FAKE_DISCORD_TOKEN, "home") == frozenset({"77"})


def test_discord_chunks_replies_below_its_limit(discord_api: DiscordApi):
    path = "/channels/900/messages"
    discord_api.queue("POST", path, (200, {"id": "1"}), (200, {"id": "2"}))
    transport = ch.DiscordTransport(base_url=discord_api.url)

    assert transport.send(FAKE_DISCORD_TOKEN, "900", "x" * 3500)

    payloads = [call[2] for call in discord_api.calls]
    assert [len(payload["content"]) for payload in payloads if payload] == [1900, 1600]


def test_discord_channel_reply_references_the_triggering_message(discord_api: DiscordApi):
    path = "/channels/700/messages"
    discord_api.queue("POST", path, (200, {"id": "103"}))
    transport = ch.DiscordTransport(base_url=discord_api.url)

    assert transport.send_reply(FAKE_DISCORD_TOKEN, "700", "hello", "102")

    assert discord_api.calls[-1][2] == {
        "content": "hello",
        "message_reference": {"message_id": "102", "channel_id": "700"},
    }


def test_discord_delivery_resolves_an_operator_to_their_dm_channel(discord_api: DiscordApi):
    discord_api.queue("POST", "/users/@me/channels", (200, {"id": "900"}))
    discord_api.queue("GET", "/channels/900/messages?limit=1", (200, []))
    discord_api.queue("POST", "/channels/900/messages", (200, {"id": "1"}))
    transport = ch.DiscordTransport(base_url=discord_api.url, operators=frozenset({"31337"}))

    assert transport.send(FAKE_DISCORD_TOKEN, "31337", "scheduled hello")

    assert discord_api.calls[-1][1] == "/channels/900/messages"


def test_discord_honours_one_rate_limit_without_spinning(discord_api: DiscordApi):
    slept: list[float] = []
    path = "/channels/900/messages"
    discord_api.queue("POST", path, (429, {"retry_after": 0.25}), (200, {"id": "1"}))
    transport = ch.DiscordTransport(base_url=discord_api.url, sleep=slept.append)

    assert transport.send(FAKE_DISCORD_TOKEN, "900", "hello")
    assert slept == [0.25]
    assert len(discord_api.calls) == 2


@pytest.mark.parametrize("retry_after", [31, float("inf"), "not-a-number"])
def test_discord_refuses_an_unsafe_rate_limit_delay(discord_api: DiscordApi, retry_after: object):
    slept: list[float] = []
    path = "/channels/900/messages"
    discord_api.queue("POST", path, (429, {"retry_after": retry_after}))
    transport = ch.DiscordTransport(base_url=discord_api.url, sleep=slept.append)

    assert not transport.send(FAKE_DISCORD_TOKEN, "900", "hello")
    assert slept == []
    assert len(discord_api.calls) == 1


def test_discord_drains_more_than_one_page_without_skipping_unseen_messages(
    discord_api: DiscordApi,
):
    newest = [discord_message(str(number)) for number in range(150, 100, -1)]
    discord_api.queue("POST", "/users/@me/channels", (200, {"id": "900"}))
    discord_api.queue("GET", "/channels/900/messages?limit=1", (200, [discord_message("99")]))
    discord_api.queue("GET", "/channels/900/messages?after=99&limit=50", (200, newest))
    discord_api.queue(
        "GET",
        "/channels/900/messages?before=101&limit=50",
        (200, [discord_message("100")]),
    )
    transport = ch.DiscordTransport(base_url=discord_api.url, operators=frozenset({"31337"}))

    assert transport.poll(FAKE_DISCORD_TOKEN, 0) == []
    received = transport.poll(FAKE_DISCORD_TOKEN, 0) or []

    assert [
        message.update_id for message in sorted(received, key=lambda item: item.update_id)
    ] == list(range(100, 151))


def telegram_update(  # noqa: PLR0913 — one keyword per field of a real update
    *,
    update_id: int = 1,
    text: str | None = "are you alive?",
    sender: str = OPERATOR,
    conversation: str = CONVERSATION,
    chat_type: str = "private",
    date: object = 1_756_728_000,
) -> dict[str, Any]:
    """Build one update in the shape Telegram's ``getUpdates`` actually returns."""
    body: dict[str, Any] = {
        "message_id": 5,
        "date": date,
        "chat": {"id": int(conversation), "type": chat_type},
        "from": {"id": int(sender), "is_bot": False},
    }
    if text is not None:
        body["text"] = text
    return {"update_id": update_id, "message": body}


def test_a_poll_authenticates_as_the_bot_and_reads_its_messages_back(bot_api: BotApi):
    bot_api.replies["getUpdates"] = {"ok": True, "result": [telegram_update()]}
    transport = ch.TelegramTransport(base_url=bot_api.url, poll_timeout_s=0)

    [received] = transport.poll(FAKE_BOT_TOKEN, 12) or []

    assert bot_api.calls[0][0] == f"/bot{FAKE_BOT_TOKEN}/getUpdates"
    assert bot_api.method("getUpdates") == {
        "timeout": 0,
        "allowed_updates": ["message"],
        "offset": 12,
    }
    assert (received.update_id, received.conversation, received.sender) == (
        1,
        CONVERSATION,
        OPERATOR,
    )
    assert received.text == "are you alive?"
    assert received.private


def test_a_first_poll_asks_for_whatever_is_waiting(bot_api: BotApi):
    transport = ch.TelegramTransport(base_url=bot_api.url, poll_timeout_s=0)

    transport.poll(FAKE_BOT_TOKEN, 0)

    assert "offset" not in bot_api.method("getUpdates")


def test_a_message_that_is_not_text_is_ignored_rather_than_refused(bot_api: BotApi):
    bot_api.replies["getUpdates"] = {
        "ok": True,
        "result": [telegram_update(update_id=1, text=None), telegram_update(update_id=2)],
    }
    transport = ch.TelegramTransport(base_url=bot_api.url, poll_timeout_s=0)

    received = transport.poll(FAKE_BOT_TOKEN, 0) or []

    assert [item.update_id for item in received] == [2]


def test_a_group_message_arrives_marked_as_one(bot_api: BotApi):
    bot_api.replies["getUpdates"] = {
        "ok": True,
        "result": [telegram_update(chat_type="supergroup", conversation="-100999")],
    }
    transport = ch.TelegramTransport(base_url=bot_api.url, poll_timeout_s=0)

    [received] = transport.poll(FAKE_BOT_TOKEN, 0) or []

    assert not received.private


def test_a_refused_call_is_a_reported_failure_rather_than_an_exception(bot_api: BotApi):
    bot_api.replies["getUpdates"] = {"ok": False, "description": "Unauthorized"}
    transport = ch.TelegramTransport(base_url=bot_api.url, poll_timeout_s=0)

    assert transport.poll(FAKE_BOT_TOKEN, 0) is None


def test_a_reply_is_posted_into_the_conversation(bot_api: BotApi):
    bot_api.replies["sendMessage"] = {"ok": True, "result": {"message_id": 6}}
    transport = ch.TelegramTransport(base_url=bot_api.url)

    assert transport.send(FAKE_BOT_TOKEN, CONVERSATION, "I am alive.")
    assert bot_api.method("sendMessage") == {"chat_id": CONVERSATION, "text": "I am alive."}


def test_an_empty_reply_is_never_sent(bot_api: BotApi):
    transport = ch.TelegramTransport(base_url=bot_api.url)

    assert not transport.send(FAKE_BOT_TOKEN, CONVERSATION, "   ")
    assert bot_api.calls == []


def test_an_unreachable_api_is_reported_rather_than_raised():
    transport = ch.TelegramTransport(base_url="http://127.0.0.1:1", poll_timeout_s=0)

    assert transport.poll(FAKE_BOT_TOKEN, 0) is None
    assert not transport.send(FAKE_BOT_TOKEN, CONVERSATION, "anybody there?")


def test_a_transport_built_in_this_suite_can_never_reach_the_real_api():
    assert ch.TelegramTransport.from_env().base_url == "http://127.0.0.1:1"


def test_an_update_steward_cannot_read_is_ignored_rather_than_refused(bot_api: BotApi):
    bot_api.replies["getUpdates"] = {
        "ok": True,
        "result": [
            "not an update",
            {"message": {"text": "no update id"}},
            {"update_id": 2, "message": "not an object"},
            {"update_id": 3, "message": {"text": "hi", "from": {"id": 1}}},
            {"update_id": 4, "message": {"text": "hi", "chat": {"id": 1}, "from": {}}},
            telegram_update(update_id=5),
        ],
    }
    transport = ch.TelegramTransport(base_url=bot_api.url, poll_timeout_s=0)

    received = transport.poll(FAKE_BOT_TOKEN, 0) or []

    assert [item.update_id for item in received] == [5]


def test_an_update_with_an_unreadable_date_is_still_a_message(bot_api: BotApi):
    bot_api.replies["getUpdates"] = {"ok": True, "result": [telegram_update(date="yesterday")]}
    transport = ch.TelegramTransport(base_url=bot_api.url, poll_timeout_s=0)

    [received] = transport.poll(FAKE_BOT_TOKEN, 0) or []

    assert received.text == "are you alive?"


def test_an_answer_that_is_not_a_list_of_updates_is_a_failure(bot_api: BotApi):
    bot_api.replies["getUpdates"] = {"ok": True, "result": {"not": "a list"}}
    transport = ch.TelegramTransport(base_url=bot_api.url, poll_timeout_s=0)

    assert transport.poll(FAKE_BOT_TOKEN, 0) is None


def test_a_chat_api_that_is_not_http_reaches_nothing():
    transport = ch.TelegramTransport(base_url="file:///etc", poll_timeout_s=0)

    assert transport.poll(FAKE_BOT_TOKEN, 0) is None
