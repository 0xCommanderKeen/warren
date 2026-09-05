"""Chat transcripts behavior through the public chat interface."""

from pathlib import Path

import pytest

from steward import chat as ch
from support.chat import (
    CONVERSATION,
    NOW,
    chat_manifest,
    manifest_for,
)

# --------------------------------------------------------------------------------------
# what the session is told
# --------------------------------------------------------------------------------------


# --------------------------------------------------------------------------------------
# the transcript
# --------------------------------------------------------------------------------------


def test_a_transcript_lives_in_the_residents_own_memory_directory(tmp_path: Path):
    manifest = manifest_for(chat_manifest(tmp_path / "memory"))
    transcript = ch.Transcript(manifest, CONVERSATION)
    assert transcript.path == tmp_path / "memory" / "chat" / f"{CONVERSATION}.jsonl"


def test_a_transcript_survives_being_written_and_read_back(tmp_path: Path):
    manifest = manifest_for(chat_manifest(tmp_path / "memory"))
    transcript = ch.Transcript(manifest, CONVERSATION)
    transcript.append("operator", "are you alive?", now=NOW)
    transcript.append("test-agent", "I am.", now=NOW)
    assert ch.Transcript(manifest, CONVERSATION).render() == (
        "operator: are you alive?\ntest-agent: I am."
    )


def test_a_transcript_keeps_only_the_last_few_turns(tmp_path: Path):
    manifest = manifest_for(chat_manifest(tmp_path / "memory"))
    transcript = ch.Transcript(manifest, CONVERSATION, keep=3)
    for index in range(5):
        transcript.append("operator", f"turn {index}", now=NOW)
    assert [turn.text for turn in transcript.turns()] == ["turn 2", "turn 3", "turn 4"]


def test_an_unreadable_line_costs_context_and_never_a_conversation(tmp_path: Path):
    manifest = manifest_for(chat_manifest(tmp_path / "memory"))
    transcript = ch.Transcript(manifest, CONVERSATION)
    transcript.append("operator", "the real turn", now=NOW)
    with transcript.path.open("a", encoding="utf-8") as handle:
        handle.write("{not json at all\n")
    assert [turn.text for turn in transcript.turns()] == ["the real turn"]


def test_a_conversation_id_can_never_climb_out_of_the_chat_directory(tmp_path: Path):
    manifest = manifest_for(chat_manifest(tmp_path / "memory"))
    transcript = ch.Transcript(manifest, "../../etc/passwd")
    assert transcript.path.parent == tmp_path / "memory" / "chat"
    assert "/" not in transcript.path.name.removesuffix(".jsonl")


def test_a_negative_conversation_id_keeps_its_sign():
    assert ch.conversation_slug("-1001234") == "-1001234"


def test_a_resident_with_nowhere_to_keep_a_conversation_says_so(tmp_path: Path):
    declared = chat_manifest(tmp_path / "memory")
    declared["memory"] = {"kind": "file", "path": str(tmp_path / "memory.md")}
    manifest = manifest_for(declared)

    assert ch.chat_complaint(manifest) is not None
    with pytest.raises(ch.ChatError, match="nowhere to keep a conversation"):
        ch.resolve_chat_dir(manifest)


def test_a_remote_memory_is_no_place_for_a_transcript(tmp_path: Path):
    declared = chat_manifest(tmp_path / "memory")
    declared["memory"] = {"kind": "repo", "path": "s3://bucket/memory"}
    complaint = ch.chat_complaint(manifest_for(declared))

    assert complaint is not None
    assert "remote reference" in complaint


def test_an_empty_turn_is_never_recorded(tmp_path: Path):
    transcript = ch.Transcript(manifest_for(chat_manifest(tmp_path / "memory")), CONVERSATION)

    transcript.append("operator", "   ", now=NOW)

    assert transcript.turns() == []


def test_a_turn_missing_half_of_itself_is_not_a_turn(tmp_path: Path):
    transcript = ch.Transcript(manifest_for(chat_manifest(tmp_path / "memory")), CONVERSATION)
    transcript.append("operator", "the real turn", now=NOW)
    with transcript.path.open("a", encoding="utf-8") as handle:
        handle.write('["not an object"]\n')
        handle.write('{"speaker": "operator"}\n')
        handle.write('{"text": "said by nobody"}\n')

    assert [turn.text for turn in transcript.turns()] == ["the real turn"]


def test_a_window_is_trimmed_from_the_oldest_end(tmp_path: Path):
    transcript = ch.Transcript(manifest_for(chat_manifest(tmp_path / "memory")), CONVERSATION)
    transcript.append("operator", "the oldest thing", now=NOW)
    transcript.append("operator", "z" * (ch.TRANSCRIPT_MAX_CHARS - 20), now=NOW)

    rendered = transcript.render()

    assert "the oldest thing" not in rendered
    assert len(rendered) <= ch.TRANSCRIPT_MAX_CHARS
