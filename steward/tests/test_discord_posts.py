from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from conftest import ResidentWriter, valid_manifest
from steward import discord_posts as dp
from steward import events as ev
from steward.manifest import Resident, load_manifest
from steward.store import Store


@dataclass
class Rooms:
    """Fake Discord room directory and delivery adapter."""

    resolved: dict[str, str] | None = field(default_factory=lambda: {"household": "42"})
    sent: list[tuple[str, str, str]] = field(default_factory=list)
    resolves: int = 0
    admin_calls: list[tuple[str, str, Mapping[str, object] | None]] = field(default_factory=list)

    def channels(self, token: str, guild: str):
        """Resolve the configured fake rooms once per executor."""
        del token, guild
        self.resolves += 1
        return self.resolved

    def send(self, token: str, conversation: str, text: str) -> bool:
        """Capture one attempted Discord post."""
        self.sent.append((token, conversation, text))
        return True

    def admin(
        self, token: str, method: str, path: str, payload: Mapping[str, object] | None = None
    ) -> bool:
        """Capture one attempted Discord administration call."""
        assert token == "secret"
        self.admin_calls.append((method, path, payload))
        return True

    def threads(self, token: str, guild: str) -> frozenset[str]:
        """Resolve one active thread inside the configured fake guild."""
        assert (token, guild) == ("secret", "guild")
        return frozenset({"77"})


def resident(
    write_resident: ResidentWriter, tmp_path: Path, scopes: list[str] | None = None
) -> Resident:
    data = valid_manifest()
    data["memory"] = {"kind": "directory", "path": str(tmp_path / "memory"), "journal": "journal"}
    data["routes"].append(
        {
            "id": "discord",
            "kind": "chat",
            "address": "discord:testy",
            "status": "active",
            "posts_to": ["household"],
        }
    )
    if scopes is not None:
        data["app_grants"] = [
            {"id": "discord", "name": "Discord", "status": "granted", "scopes": scopes}
        ]
    return load_manifest(write_resident(data))


def output(*blocks: str) -> str:
    return "===STEWARD-ACTIONS===\n" + "\n".join(blocks) + "\n===END-STEWARD-ACTIONS==="


def test_post_grammar_is_read_only_from_the_action_region():
    block = '<discord post channel="household">{"text":"hello"}</discord>'
    [post] = dp.extract_posts(block)
    assert (post.channel, post.text, post.ok) == ("household", "hello", True)


def test_post_grammar_rejects_duplicate_channel_attributes():
    [post] = dp.extract_posts(
        '<discord post channel="household" channel="other">{"text":"hello"}</discord>'
    )
    assert not post.ok


def test_poster_posts_allowed_bounded_redacted_text_and_emits_length_only(write_resident, tmp_path):
    person = resident(write_resident, tmp_path)
    rooms = Rooms()
    sink = ev.NullEmitter()
    with Store(tmp_path / "steward.db") as store:
        poster = dp.Poster(
            [person], store, sink, rooms, {"STEWARD_CHAT_TOKEN_DISCORD_TESTY": "secret"}, "guild"
        )
        [result] = poster.harvest(
            person.manifest,
            output(
                '<discord post channel="household">{"text":"CHRONICLE_TOKEN=super-secret '
                + "x" * 3000
                + '"}</discord>'
            ),
        )
    assert result.posted
    assert len(rooms.sent[0][2]) <= dp.POST_MAX_CHARS
    assert "super-secret" not in rooms.sent[0][2]
    assert sink.events[0].type == "chat_message_posted"
    assert "text" not in sink.events[0].payload


def test_refusal_emits_event_and_needs_human_without_posting(write_resident, tmp_path):
    person = resident(write_resident, tmp_path)
    rooms = Rooms()
    sink = ev.NullEmitter()
    with Store(tmp_path / "steward.db") as store:
        poster = dp.Poster(
            [person], store, sink, rooms, {"STEWARD_CHAT_TOKEN_DISCORD_TESTY": "secret"}, "guild"
        )
        [result] = poster.harvest(
            person.manifest, output('<discord post channel="elsewhere">{"text":"no"}</discord>')
        )
        assert store.pending_approvals()[0].action == "rejected_post"
    assert not result.posted
    assert rooms.sent == []
    assert [event.type for event in sink.events] == ["chat_post_refused", "needs_human"]


def test_at_most_five_posts_are_sent_per_session(write_resident, tmp_path):
    person = resident(write_resident, tmp_path)
    rooms = Rooms()
    with Store(tmp_path / "steward.db") as store:
        poster = dp.Poster(
            [person],
            store,
            ev.NullEmitter(),
            rooms,
            {"STEWARD_CHAT_TOKEN_DISCORD_TESTY": "secret"},
            "guild",
        )
        results = poster.harvest(
            person.manifest,
            output(*['<discord post channel="household">{"text":"hi"}</discord>'] * 6),
        )
    assert len(rooms.sent) == 5
    assert len(results) == 6
    assert results[-1].reason == "post_limit_exceeded"
    assert rooms.resolves == 1


def test_from_env_resolves_rooms_at_startup_only_once(write_resident, tmp_path):
    person = resident(write_resident, tmp_path)
    rooms = Rooms()
    with Store(tmp_path / "steward.db") as store:
        poster = dp.Poster.from_env(
            [person],
            store,
            ev.NullEmitter(),
            transport=rooms,
            env={
                "STEWARD_CHAT_TOKEN_DISCORD_TESTY": "secret",
                dp.GUILD_ENV: "guild",
            },
        )
        assert rooms.resolves == 1
        poster.harvest(
            person.manifest,
            output('<discord post channel="household">{"text":"hi"}</discord>'),
        )
    assert rooms.resolves == 1


def test_admin_action_without_scope_knocks_and_makes_no_discord_call(write_resident, tmp_path):
    person = resident(write_resident, tmp_path, [])
    rooms = Rooms()
    sink = ev.NullEmitter()
    with Store(tmp_path / "steward.db") as store:
        poster = dp.Poster(
            [person], store, sink, rooms, {"STEWARD_CHAT_TOKEN_DISCORD_TESTY": "secret"}, "guild"
        )
        [result] = poster.harvest(
            person.manifest,
            output('<discord create_channel>{"name":"announcements"}</discord>'),
        )
        [knock] = store.pending_approvals()
    assert result.reason == "missing_scope"
    assert knock.action == "rejected_post"
    assert knock.detail["missing_scope"] == "channels.manage"
    assert rooms.admin_calls == []


def test_admin_refuses_unbounded_text_before_discord(write_resident, tmp_path):
    person = resident(write_resident, tmp_path, ["channels.manage"])
    rooms = Rooms()
    with Store(tmp_path / "steward.db") as store:
        poster = dp.Poster(
            [person],
            store,
            ev.NullEmitter(),
            rooms,
            {"STEWARD_CHAT_TOKEN_DISCORD_TESTY": "secret"},
            "guild",
        )
        [result] = poster.harvest(
            person.manifest,
            output(f'<discord create_channel>{{"name":"{"x" * 101}"}}</discord>'),
        )
    assert result.reason == dp.UNREADABLE
    assert rooms.admin_calls == []


def test_archive_refuses_a_thread_outside_the_configured_guild(write_resident, tmp_path):
    person = resident(write_resident, tmp_path, ["threads.manage"])
    rooms = Rooms()
    with Store(tmp_path / "steward.db") as store:
        poster = dp.Poster(
            [person],
            store,
            ev.NullEmitter(),
            rooms,
            {"STEWARD_CHAT_TOKEN_DISCORD_TESTY": "secret"},
            "guild",
        )
        [result] = poster.harvest(
            person.manifest, output('<discord archive_thread thread="88">{}</discord>')
        )
    assert result.reason == dp.UNREADABLE
    assert rooms.admin_calls == []


def test_no_discord_admin_verb_can_delete():
    assert all("delete" not in verb for verb in dp.ADMIN_VERBS)
    [action] = dp.extract_admin_actions('<discord delete_channel>{"name":"household"}</discord>')
    assert action.problem is not None


@pytest.mark.parametrize(
    ("scope", "block", "expected_call", "event_type"),
    [
        (
            "channels.manage",
            '<discord create_channel>{"name":"announcements"}</discord>',
            ("POST", "/guilds/guild/channels", {"name": "announcements", "type": 0}),
            "discord_channel_created",
        ),
        (
            "channels.manage",
            '<discord set_topic channel="household">{"topic":"Today"}</discord>',
            ("PATCH", "/channels/42", {"topic": "Today"}),
            "discord_topic_set",
        ),
        (
            "threads.manage",
            '<discord create_thread channel="household">{"name":"chores"}</discord>',
            ("POST", "/channels/42/threads", {"name": "chores", "type": 11}),
            "discord_thread_created",
        ),
        (
            "threads.manage",
            '<discord archive_thread thread="77">{}</discord>',
            ("PATCH", "/channels/77", {"archived": True}),
            "discord_thread_archived",
        ),
        (
            "messages.pin",
            '<discord pin channel="household" message="99">{}</discord>',
            ("PUT", "/channels/42/pins/99", None),
            "discord_message_pinned",
        ),
    ],
)
def test_each_scoped_admin_verb_makes_one_rest_call_and_event(  # noqa: PLR0913, PLR0917
    write_resident,
    tmp_path,
    scope: str,
    block: str,
    expected_call: tuple[str, str, dict[str, object] | None],
    event_type: str,
):
    person = resident(write_resident, tmp_path, [scope])
    rooms = Rooms()
    sink = ev.NullEmitter()
    with Store(tmp_path / "steward.db") as store:
        poster = dp.Poster(
            [person], store, sink, rooms, {"STEWARD_CHAT_TOKEN_DISCORD_TESTY": "secret"}, "guild"
        )
        [result] = poster.harvest(person.manifest, output(block))
    assert result.posted
    assert rooms.admin_calls == [expected_call]
    assert [event.type for event in sink.events] == [event_type]
    assert block not in result.transcript_line()
