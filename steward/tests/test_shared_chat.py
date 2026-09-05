"""One phone bot routes conversations without becoming a relaying resident."""

from collections.abc import Iterator
from dataclasses import replace
from datetime import timedelta
from pathlib import Path

import pytest
from click.testing import CliRunner
from pydantic import ValidationError

from conftest import VALID_SOUL, ResidentWriter
from steward import chat as ch
from steward.cli import main
from steward.manifest import Route, load_manifest
from steward.runners import Outcome, RunResult
from steward.runs import DELIVERED
from steward.store import Store
from test_chat import (
    DIGEST,
    FAKE_BOT_TOKEN,
    NOW,
    OPERATOR,
    FakeTransport,
    ScriptedRunner,
    chat_manifest,
    message,
)


@pytest.fixture
def shared_bridge(write_resident: ResidentWriter, tmp_path: Path) -> Iterator[ch.ChatBridge]:
    residents = []
    for number, (resident_id, name) in enumerate((("hob", "Hob"), ("hr", "Karen Smith"))):
        declared = chat_manifest(tmp_path / resident_id / "memory", id=resident_id, home=number)
        declared["uid"] = (
            "7e36d76a-1ad8-4d65-a619-8c6e7fb93ed9"
            if number == 0
            else "3a78217a-df03-4f3b-a46a-4c75b4ad929f"
        )
        declared["agent_id"] = f"claude-code:{resident_id}"
        declared["soul"]["name"] = name
        declared["routes"] = [
            {"id": "shared", "kind": "chat", "address": "telegram:warren", "shared": True}
        ]
        soul = VALID_SOUL.replace("Testy", name).replace("test-agent", resident_id)
        residents.append(load_manifest(write_resident(declared, soul=soul)))
    with Store(tmp_path / "steward.db") as store:
        yield ch.ChatBridge(
            routes=ch.chat_routes(residents),
            store=store,
            tokens={"STEWARD_CHAT_TOKEN_WARREN": FAKE_BOT_TOKEN},
            operators=frozenset({OPERATOR}),
            transport=FakeTransport(),
            runner_factory=lambda _spec, _placement: ScriptedRunner(),
            clock=lambda: NOW,
            state_path=tmp_path / "scheduler.json",
        )


def test_a_colon_inside_a_bare_followup_does_not_become_an_address(shared_bridge):
    transport = FakeTransport(
        [[message("@hob hello"), message("Here is context: yes", update_id=2)]]
    )
    shared_bridge.transports = {"telegram": transport}

    outcomes = shared_bridge.poll_once()

    assert [(outcome.resident_id, outcome.status) for outcome in outcomes] == [
        ("hob", ch.ChatStatus.ANSWERED),
        ("hob", ch.ChatStatus.ANSWERED),
    ]


def test_one_poll_routes_names_ids_and_followups_and_tags_each_answer(shared_bridge):
    transport = FakeTransport(
        [
            [
                message("kArEn SmItH: please write a skill", update_id=1),
                message("make it shorter", update_id=2),
                message("@HoB look at the workshop", update_id=3),
                message("@hr revise the skill", update_id=4),
            ],
            [],
        ]
    )
    runner = ScriptedRunner()
    bridge = replace(
        shared_bridge,
        transport=transport,
        transports={"telegram": transport},
        runner_factory=lambda _spec, _placement: runner,
    )

    outcomes = bridge.poll_once()
    bridge.poll_once()

    assert [outcome.resident_id for outcome in outcomes] == ["hr", "hr", "hob", "hr"]
    assert all(outcome.status == ch.ChatStatus.ANSWERED for outcome in outcomes)
    assert [text for _, _, text in transport.sent] == [
        "Karen Smith: I am alive.",
        "Karen Smith: I am alive.",
        "Hob: I am alive.",
        "Karen Smith: I am alive.",
    ]
    assert transport.polls == [(FAKE_BOT_TOKEN, 0), (FAKE_BOT_TOKEN, 5)]
    assert "please write a skill" in runner.requests[0].prompt
    assert "kArEn SmItH:" not in runner.requests[0].prompt
    assert "make it shorter" in runner.requests[1].prompt
    assert "make it shorter" not in runner.requests[2].prompt


def test_address_alone_selects_without_a_run_and_survives_a_restart(shared_bridge):
    first = FakeTransport([[message("@Karen Smith")]])
    bridge = replace(shared_bridge, transports={"telegram": first})
    [selection] = bridge.poll_once()
    assert not selection.ran
    assert first.sent[0][2] == "Now talking to Karen Smith."

    second = FakeTransport([[message("continue", update_id=2)]])
    with Store(shared_bridge.store.path) as reopened:
        restarted = replace(shared_bridge, store=reopened, transports={"telegram": second})
        [followup] = restarted.poll_once()
    assert followup.resident_id == "hr"
    assert followup.status == ch.ChatStatus.ANSWERED


@pytest.mark.parametrize("text", ["hello", "@missing hello", "missing: hello", "@hobble hi"])
def test_no_recipient_or_unknown_address_starts_no_session(shared_bridge, text):
    transport = FakeTransport([[message(text)]])
    shared_bridge.transports = {"telegram": transport}
    [outcome] = shared_bridge.poll_once()
    assert not outcome.ran
    assert "Name one resident" in transport.sent[0][2]


def test_an_ambiguous_display_name_is_refused(shared_bridge):
    hob, karen = shared_bridge.routes
    ambiguous = replace(
        karen.resident,
        manifest=karen.resident.manifest.model_copy(
            update={"soul": karen.resident.manifest.soul.model_copy(update={"name": "Hob"})}
        ),
    )
    transport = FakeTransport([[message("@hob hello")]])
    bridge = replace(
        shared_bridge,
        routes=[hob, replace(karen, resident=ambiguous)],
        transports={"telegram": transport},
    )
    [outcome] = bridge.poll_once()
    assert not outcome.ran
    assert "Name one resident" in transport.sent[0][2]


@pytest.mark.parametrize(
    "bad_message",
    [
        message("@hr hi", sender="stranger"),
        message("@hr hi", private=False),
        message("@hr hi", bot=True),
        message("@hr hi", at=NOW - timedelta(hours=1)),
    ],
)
def test_unauthorized_or_stale_messages_cannot_change_the_recipient(shared_bridge, bad_message):
    transport = FakeTransport(
        [[message("@hob")], [bad_message], [message("continue", update_id=3)]]
    )
    shared_bridge.transports = {"telegram": transport}
    shared_bridge.poll_once()
    transport.sent.clear()
    [dropped] = shared_bridge.poll_once()
    assert not dropped.ran
    assert transport.sent == []
    [followup] = shared_bridge.poll_once()
    assert followup.resident_id == "hob"
    assert followup.status == ch.ChatStatus.ANSWERED


def test_conversations_and_bots_have_independent_recipients(shared_bridge):
    transport = FakeTransport([[message("@hr")], [message("continue", conversation="999")]])
    shared_bridge.transports = {"telegram": transport}
    shared_bridge.poll_once()
    [other_conversation] = shared_bridge.poll_once()
    assert not other_conversation.ran

    other_routes = [
        replace(
            route,
            address=ch.Address("telegram", "other"),
            route=route.route.model_copy(update={"address": "telegram:other"}),
        )
        for route in shared_bridge.routes
    ]
    other = FakeTransport([[message("continue")]])
    bridge = replace(
        shared_bridge,
        routes=other_routes,
        tokens={"STEWARD_CHAT_TOKEN_OTHER": "another-fake-token"},
        transports={"telegram": other},
    )
    [other_bot] = bridge.poll_once()
    assert not other_bot.ran


def test_a_removed_recipient_does_not_redirect_followups_to_a_neighbor(shared_bridge):
    transport = FakeTransport([[message("@hr")], [message("continue", update_id=2)]])
    shared_bridge.transports = {"telegram": transport}
    shared_bridge.poll_once()
    bridge = replace(shared_bridge, routes=shared_bridge.routes[:1])
    [outcome] = bridge.poll_once()
    assert not outcome.ran
    assert "Name one resident" in transport.sent[-1][2]


def test_busy_resident_is_named_and_remains_the_followup_recipient(shared_bridge):
    transport = FakeTransport([[message("@hr hello")], [message("try again", update_id=2)]])
    shared_bridge.transports = {"telegram": transport}
    assert shared_bridge.claims is not None
    with shared_bridge.claims.hold("hr", kind="routine", ref="daily-digest"):
        [busy] = shared_bridge.poll_once()
    assert busy.status == ch.ChatStatus.BUSY
    assert busy.reply.startswith("Karen Smith:")
    assert "daily-digest" in busy.reply
    [followup] = shared_bridge.poll_once()
    assert followup.resident_id == "hr"
    assert followup.status == ch.ChatStatus.ANSWERED


def test_routine_delivery_tags_each_sender_without_changing_the_recipient(shared_bridge):
    transport = FakeTransport([[message("@hob")], [message("continue", update_id=2)]])
    shared_bridge.transports = {"telegram": transport}
    shared_bridge.poll_once()
    delivery = ch.RoutineDelivery(
        tokens=shared_bridge.tokens, operators=frozenset({OPERATOR}), transport=transport
    )
    for route in shared_bridge.routes:
        assert delivery.deliver(route.resident, DIGEST, "Today's digest").status == DELIVERED
    assert [text for _, _, text in transport.sent[-2:]] == [
        "Hob: Today's digest",
        "Karen Smith: Today's digest",
    ]
    [followup] = shared_bridge.poll_once()
    assert followup.resident_id == "hob"


@pytest.mark.parametrize(
    ("kind", "address"), [("chat", "discord:warren"), ("email", "telegram:warren")]
)
def test_shared_is_only_valid_for_telegram_chat(kind, address):
    with pytest.raises(ValidationError, match="shared is allowed only on a Telegram chat route"):
        Route(id="shared", kind=kind, address=address, shared=True)


@pytest.mark.usefixtures("shared_bridge")
def test_chat_list_shows_residents_on_the_shared_bot(monkeypatch, tmp_path):
    monkeypatch.setenv("STEWARD_CHAT_TOKEN_WARREN", FAKE_BOT_TOKEN)
    monkeypatch.setenv(ch.OPERATORS_ENV, OPERATOR)
    result = CliRunner().invoke(main, ["chat", "list", "--residents", str(tmp_path / "residents")])
    assert result.exit_code == 0, result.output
    assert "hob/shared: telegram:warren — reachable, shared" in result.output
    assert "hr/shared: telegram:warren — reachable, shared" in result.output
    assert FAKE_BOT_TOKEN not in result.output


def test_dedicated_bot_coexists_and_explicit_routine_delivery_chooses_shared(shared_bridge):
    hob, karen = shared_bridge.routes
    dedicated = Route(id="private", kind="chat", address="telegram:hob")
    resident = replace(
        hob.resident,
        manifest=hob.resident.manifest.model_copy(update={"routes": [hob.route, dedicated]}),
    )
    transport = FakeTransport(
        [[message("@hob shared-only conversation")], [message("ordinary message")]]
    )
    runner = ScriptedRunner()
    bridge = replace(
        shared_bridge,
        routes=ch.chat_routes([resident, karen.resident]),
        tokens={**shared_bridge.tokens, "STEWARD_CHAT_TOKEN_HOB": "dedicated-token"},
        transports={"telegram": transport},
        runner_factory=lambda _spec, _placement: runner,
    )
    outcomes = bridge.poll_once()
    assert [(outcome.resident_id, outcome.reply) for outcome in outcomes] == [
        ("hob", "Hob: I am alive."),
        ("hob", "I am alive."),
    ]
    assert "shared-only conversation" not in runner.requests[1].prompt
    delivery = ch.RoutineDelivery(
        tokens=bridge.tokens, operators=frozenset({OPERATOR}), transport=transport
    )
    routine = DIGEST.model_copy(update={"deliver": "telegram:warren"})
    assert delivery.deliver(resident, routine, "News").status == DELIVERED
    assert transport.sent[-1] == (FAKE_BOT_TOKEN, OPERATOR, "Hob: News")


@pytest.mark.parametrize("shared", [True, False])
def test_conflicting_bot_references_or_modes_close_routes_without_polling(shared_bridge, shared):
    hob, karen = shared_bridge.routes
    resident = replace(
        karen.resident,
        manifest=karen.resident.manifest.model_copy(
            update={
                "routes": [Route(id="other", kind="chat", address="telegram:other", shared=shared)]
            }
        ),
    )
    transport = FakeTransport([[message("@hob hello")]])
    tokens = {**shared_bridge.tokens, "STEWARD_CHAT_TOKEN_OTHER": FAKE_BOT_TOKEN}
    bridge = replace(
        shared_bridge,
        routes=ch.chat_routes([hob.resident, resident]),
        tokens=tokens,
        transports={"telegram": transport},
    )
    outcomes = bridge.poll_once()
    assert all(outcome.status == ch.ChatStatus.UNREACHABLE for outcome in outcomes)
    assert transport.polls == []
    reports = ch.describe_chat(
        [hob.resident, resident],
        env={**tokens, ch.OPERATORS_ENV: OPERATOR},
        transports={"telegram": transport},
    )
    assert all(not report.reachable for report in reports)
    assert all("shared bot" in (report.note or "") for report in reports)
    assert all(FAKE_BOT_TOKEN not in (report.note or "") for report in reports)


def test_unhealthy_selected_resident_is_refused_without_closing_neighbors(shared_bridge):
    hob, karen = shared_bridge.routes
    resident = replace(
        karen.resident,
        manifest=karen.resident.manifest.model_copy(
            update={"memory": karen.resident.manifest.memory.model_copy(update={"kind": "file"})}
        ),
    )
    transport = FakeTransport([[message("@hr hello"), message("@hob hello", update_id=2)]])
    bridge = replace(
        shared_bridge,
        routes=[hob, replace(karen, resident=resident)],
        transports={"telegram": transport},
    )
    outcomes = bridge.poll_once()
    replies = [outcome for outcome in outcomes if outcome.conversation]
    assert replies[0].status == ch.ChatStatus.UNREACHABLE
    assert replies[0].reply.startswith("Karen Smith:")
    assert replies[1].status == ch.ChatStatus.ANSWERED
    assert replies[1].resident_id == "hob"


def test_reload_preserves_the_shared_cursor_when_the_first_resident_leaves(shared_bridge):
    transport = FakeTransport(
        [[message("@hr hello", update_id=8)], [message("continue", update_id=9)]]
    )
    shared_bridge.transports = {"telegram": transport}
    shared_bridge.poll_once()
    shared_bridge.reload_source = lambda: ch.BridgeSources(
        routes=tuple(shared_bridge.routes[1:]),
        tokens=shared_bridge.tokens,
        library=shared_bridge.library,
    )
    assert shared_bridge.reload()
    [followup] = shared_bridge.poll_once()
    assert followup.resident_id == "hr"
    assert followup.status == ch.ChatStatus.ANSWERED
    assert transport.polls == [(FAKE_BOT_TOKEN, 0), (FAKE_BOT_TOKEN, 9)]


def test_shared_egress_still_redacts_and_bounds_the_entire_tagged_reply(shared_bridge):
    transport = FakeTransport([[message("@hr hello")]])
    runner = ScriptedRunner(RunResult(outcome=Outcome.OK, output=FAKE_BOT_TOKEN + "x" * 6000))
    bridge = replace(
        shared_bridge,
        transports={"telegram": transport},
        runner_factory=lambda _spec, _placement: runner,
    )
    [outcome] = bridge.poll_once()
    assert outcome.reply.startswith("Karen Smith:")
    assert FAKE_BOT_TOKEN not in outcome.reply
    assert len(outcome.reply) <= ch.REPLY_MAX_CHARS
