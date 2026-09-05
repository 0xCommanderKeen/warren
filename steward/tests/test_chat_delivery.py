"""Chat delivery behavior through the public chat interface."""

from pathlib import Path

import pytest

from conftest import ResidentWriter
from steward import chat as ch
from steward.manifest import (
    Resident,
    load_manifest,
)
from steward.runs import (
    DELIVERED,
    DELIVERY_FAILED,
)
from steward.scheduler import Delivery
from support.chat import (
    DIGEST,
    FAKE_BOT_TOKEN,
    FAKE_DISCORD_TOKEN,
    FakeTransport,
    chat_manifest,
)
from support.chat_http import DiscordApi
from support.chat_http import discord_api as discord_api  # noqa: PLC0414 — pytest fixture discovery


@pytest.fixture
def phone_resident(write_resident: ResidentWriter, tmp_path: Path) -> Resident:
    return load_manifest(write_resident(chat_manifest(tmp_path / "memory")))


def _delivery(
    transport: FakeTransport | None = None, env: dict[str, str] | None = None
) -> tuple[ch.RoutineDelivery, FakeTransport]:
    transport = transport or FakeTransport()
    source = {
        "STEWARD_CHAT_TOKEN_TESTY": FAKE_BOT_TOKEN,
        ch.OPERATORS_ENV: "4242, 99",
        **(env or {}),
    }
    return ch.RoutineDelivery.from_env(source, transport=transport), transport


def test_a_delivery_reaches_every_operators_private_conversation(phone_resident: Resident):
    delivery, transport = _delivery()
    outcome = delivery.deliver(phone_resident, DIGEST, "Two things happened today.")
    assert outcome == Delivery(status=DELIVERED)
    assert transport.sent == [
        (FAKE_BOT_TOKEN, "4242", "Two things happened today."),
        (FAKE_BOT_TOKEN, "99", "Two things happened today."),
    ]


def test_production_delivery_builds_discord_and_posts_to_the_operator_dm(
    discord_api: DiscordApi, write_resident: ResidentWriter, tmp_path: Path
):
    data = chat_manifest(tmp_path / "memory")
    data["routes"][-1]["address"] = "discord:testy"
    resident = load_manifest(write_resident(data))
    routine = DIGEST.model_copy(update={"deliver": "discord:testy"})
    discord_api.queue("POST", "/users/@me/channels", (200, {"id": "900"}))
    discord_api.queue("GET", "/channels/900/messages?limit=1", (200, []))
    discord_api.queue("POST", "/channels/900/messages", (200, {"id": "1"}))
    delivery = ch.RoutineDelivery.from_env(
        {
            "STEWARD_CHAT_TOKEN_DISCORD_TESTY": FAKE_DISCORD_TOKEN,
            ch.OPERATORS_ENV: "discord:31337",
            ch.DISCORD_API_URL_ENV: discord_api.url,
        }
    )

    outcome = delivery.deliver(resident, routine, "hello")

    assert outcome == Delivery(status=DELIVERED)
    assert discord_api.calls[-1][1] == "/channels/900/messages"


def test_the_original_routine_delivery_constructor_remains_usable(phone_resident: Resident):
    transport = FakeTransport()
    delivery = ch.RoutineDelivery(
        tokens={"STEWARD_CHAT_TOKEN_TESTY": FAKE_BOT_TOKEN},
        operators=frozenset({"4242"}),
        transport=transport,
    )

    assert delivery.deliver(phone_resident, DIGEST, "hello") == Delivery(status=DELIVERED)
    assert transport.sent == [(FAKE_BOT_TOKEN, "4242", "hello")]


def test_a_delivered_message_is_redacted_and_bounded(phone_resident: Resident):
    delivery, transport = _delivery()
    text = f"the token is {FAKE_BOT_TOKEN} and then " + "x" * (ch.REPLY_MAX_CHARS * 2)
    outcome = delivery.deliver(phone_resident, DIGEST, text)
    assert outcome.status == DELIVERED
    for _token, _conversation, sent in transport.sent:
        assert FAKE_BOT_TOKEN not in sent
        assert len(sent) <= ch.REPLY_MAX_CHARS


def test_no_operators_means_a_failed_delivery_and_no_send(phone_resident: Resident):
    delivery, transport = _delivery(env={ch.OPERATORS_ENV: ""})
    outcome = delivery.deliver(phone_resident, DIGEST, "hello")
    assert outcome.status == DELIVERY_FAILED
    assert ch.OPERATORS_ENV in outcome.reason
    assert transport.sent == []


def test_a_missing_token_is_a_failed_delivery_naming_the_variable(phone_resident: Resident):
    delivery, transport = _delivery(env={"STEWARD_CHAT_TOKEN_TESTY": ""})
    outcome = delivery.deliver(phone_resident, DIGEST, "hello")
    assert outcome.status == DELIVERY_FAILED
    assert "STEWARD_CHAT_TOKEN_TESTY" in outcome.reason
    assert FAKE_BOT_TOKEN not in outcome.reason
    assert transport.sent == []


def test_a_resident_with_no_active_chat_route_cannot_be_delivered_to(
    write_resident: ResidentWriter, tmp_path: Path
):
    data = chat_manifest(tmp_path / "memory")
    data["routes"][-1]["status"] = "pending"
    resident = load_manifest(write_resident(data))
    delivery, transport = _delivery()
    outcome = delivery.deliver(resident, DIGEST, "hello")
    assert outcome.status == DELIVERY_FAILED
    assert "no active chat route" in outcome.reason
    assert transport.sent == []


def test_a_transport_that_refuses_every_send_is_a_failed_delivery(phone_resident: Resident):
    class Refusing(FakeTransport):
        def send(self, token: str, conversation: str, text: str) -> bool:
            super().send(token, conversation, text)
            return False

    delivery, transport = _delivery(transport=Refusing())
    outcome = delivery.deliver(phone_resident, DIGEST, "hello")
    assert outcome.status == DELIVERY_FAILED
    assert "0 of 2" in outcome.reason
    assert len(transport.sent) == 2


def test_a_partly_delivered_message_is_delivered_and_says_who_was_missed(
    phone_resident: Resident,
):
    class Picky(FakeTransport):
        def send(self, token: str, conversation: str, text: str) -> bool:
            super().send(token, conversation, text)
            return conversation == "4242"

    delivery, _transport = _delivery(transport=Picky())
    outcome = delivery.deliver(phone_resident, DIGEST, "hello")
    assert outcome.status == DELIVERED
    assert "1 of 2" in outcome.reason


def test_a_transport_that_raises_is_a_failed_delivery_not_a_crash(phone_resident: Resident):
    class Exploding(FakeTransport):
        def send(self, token: str, conversation: str, text: str) -> bool:  # noqa: ARG002
            raise OSError("no route to host")

    delivery, _transport = _delivery(transport=Exploding())
    outcome = delivery.deliver(phone_resident, DIGEST, "hello")
    assert outcome.status == DELIVERY_FAILED
    assert "0 of 2" in outcome.reason


def test_an_address_on_another_transport_is_not_delivered_here(
    write_resident: ResidentWriter, tmp_path: Path
):
    data = chat_manifest(tmp_path / "memory")
    data["routes"][-1]["address"] = "discord:testy"
    resident = load_manifest(write_resident(data))
    delivery, transport = _delivery()
    outcome = delivery.deliver(resident, DIGEST, "hello")
    assert outcome.status == DELIVERY_FAILED
    assert "discord" in outcome.reason
    assert transport.sent == []
