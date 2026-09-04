from dataclasses import dataclass, field
from pathlib import Path

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

    def channels(self, token: str, guild: str):
        """Resolve the configured fake rooms once per executor."""
        del token, guild
        self.resolves += 1
        return self.resolved

    def send(self, token: str, conversation: str, text: str) -> bool:
        """Capture one attempted Discord post."""
        self.sent.append((token, conversation, text))
        return True


def resident(write_resident: ResidentWriter, tmp_path: Path) -> Resident:
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
