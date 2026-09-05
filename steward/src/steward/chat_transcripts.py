"""Bounded conversation persistence in each resident's memory directory."""

import json
import logging
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from steward import events as ev
from steward.chat_config import ChatError
from steward.deploy import memory_host_dir
from steward.manifest import ResidentManifest
from steward.prompt import TRANSCRIPT_MAX_CHARS

log = logging.getLogger("steward.chat")

#: Where a resident's conversations live inside its memory directory.
CHAT_DIR = "chat"

#: How many turns of one conversation survive on disk. Ten exchanges: enough to read back
#: what happened this morning, and bounded by construction like the journal is.
TRANSCRIPT_KEEP_TURNS = 20

#: How many of those go into the prompt. Fewer than are kept, because the file is for a
#: person scrolling back and the window is paid for on every single message.
TRANSCRIPT_WINDOW_TURNS = 10
_UNSAFE_IN_NAME = re.compile(r"[^A-Za-z0-9_-]+")

#: How long a conversation's file name may be before steward shortens it. Telegram's own
#: ids are far shorter; the bound exists so nothing a transport invents can produce a name
#: a filesystem refuses.
SLUG_MAX_CHARS = 64


# --------------------------------------------------------------------------------------
# the rolling transcript
# --------------------------------------------------------------------------------------


def chat_complaint(manifest: ResidentManifest) -> str | None:
    """Return why this resident cannot keep a transcript, or ``None`` when it can.

    Pure, like :func:`steward.journal.journal_complaint`: it reads the declaration and never
    the filesystem, so the bridge can refuse at startup on a machine that has never seen the
    resident's volume rather than discovering it mid-conversation.
    """
    memory = manifest.memory
    if memory.kind == "file":
        return (
            "memory.kind is 'file', so there is nowhere to keep a conversation; a chat "
            "route needs memory.kind 'directory' or 'repo'"
        )
    if "://" in memory.path:
        return (
            f"memory.path {memory.path!r} is a remote reference; a transcript is read and "
            f"written as an ordinary file, so it needs a local directory"
        )
    return None


def resolve_chat_dir(manifest: ResidentManifest) -> Path:
    """Return the directory this resident's conversations live in.

    ``<memory>/chat``, on the *host* side of the mount for a container-placed resident —
    the same base :func:`steward.journal.resolve_journal_dir` resolves against, so a
    resident's memory has one location and steward writes to the side it can actually see
    (steward #58). The session inside the container reads the same files at
    ``<memory.path>/chat`` through the bind mount.
    """
    complaint = chat_complaint(manifest)
    if complaint is not None:
        raise ChatError(f"{manifest.id}: {complaint}")
    return memory_host_dir(manifest) / CHAT_DIR


def conversation_slug(conversation: str) -> str:
    """Return a file name for one conversation, from an id steward did not choose.

    Everything that is not a letter, a digit, ``_`` or ``-`` is folded away, so nothing a
    transport hands over can climb out of the chat directory or name a file the filesystem
    refuses. A Telegram chat id is an integer and survives this unchanged, negative sign
    included.
    """
    slug = _UNSAFE_IN_NAME.sub("_", conversation.strip())[:SLUG_MAX_CHARS].strip("_")
    return slug or "unknown"


@dataclass(frozen=True, slots=True)
class Turn:
    """One thing that was said, by one side, at one moment."""

    at: str
    speaker: str
    text: str

    def render(self) -> str:
        """Render this turn the way it appears in a prompt."""
        return f"{self.speaker}: {self.text}"


@dataclass(frozen=True, slots=True)
class Transcript:
    """One conversation, kept as a rolling file in the resident's own memory directory.

    JSON lines rather than the markdown the journal uses, and the difference is who writes
    them. A journal entry is a resident's own prose, written by the session, read by a
    person; a transcript is *steward's* record of an exchange, written a turn at a time and
    read back into a prompt, and a format that can hold any text without a parser having to
    guess where one turn ends is worth more here than prettiness. It still reads fine in a
    terminal, which is the property the issue actually asked for.

    Bounded by construction, like the journal: every append rotates the file down to
    :data:`TRANSCRIPT_KEEP_TURNS` turns, so nothing here grows without limit and no cron job
    has to remember to trim it.
    """

    manifest: ResidentManifest
    conversation: str
    keep: int = TRANSCRIPT_KEEP_TURNS

    @property
    def path(self) -> Path:
        """The file this conversation lives in."""
        return resolve_chat_dir(self.manifest) / f"{conversation_slug(self.conversation)}.jsonl"

    def turns(self) -> list[Turn]:
        """Return every surviving turn, oldest first. An unreadable line is skipped.

        Broad on purpose, the way :func:`steward.journal._parse_entry` is: this file is read
        at the top of a session, and a byte somebody corrupted must degrade to "less context"
        rather than to a resident that cannot be talked to at all.
        """
        try:
            raw = self.path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return []
        found: list[Turn] = []
        for line in raw.splitlines():
            if not line.strip():
                continue
            try:
                value = json.loads(line)
            except ValueError:
                continue
            if not isinstance(value, dict):
                continue
            speaker = str(value.get("speaker") or "")
            text = str(value.get("text") or "")
            if not speaker or not text:
                continue
            found.append(Turn(at=str(value.get("at") or ""), speaker=speaker, text=text))
        return found

    def window(self, turns: int = TRANSCRIPT_WINDOW_TURNS) -> list[Turn]:
        """Return the last few turns: what the next session is opened with."""
        return self.turns()[-turns:] if turns > 0 else []

    def render(self) -> str:
        """Render the window as the text injected into a prompt, oldest first.

        Bounded here as well as at injection, and from the *newest* end: a window cut from
        the front by the prompt's cap would drop the turn the operator just referred to and
        keep the one from an hour ago, which is the wrong half to lose.
        """
        rendered = [turn.render() for turn in self.window()]
        text = "\n".join(rendered)
        while len(text) > TRANSCRIPT_MAX_CHARS and rendered:
            rendered.pop(0)
            text = "\n".join(rendered)
        return text

    def append(self, speaker: str, text: str, *, now: datetime | None = None) -> None:
        """Record one turn and rotate. Never raises: a lost line is not a failed answer."""
        if not text.strip():
            return
        turn = Turn(at=ev.utc_now_iso(now), speaker=speaker, text=text.strip())
        try:
            existing = self.turns()
            path = self.path
            path.parent.mkdir(parents=True, exist_ok=True)
            kept = [*existing, turn][-self.keep :] if self.keep > 0 else []
            body = "".join(
                json.dumps(
                    {"at": item.at, "speaker": item.speaker, "text": item.text},
                    ensure_ascii=False,
                )
                + "\n"
                for item in kept
            )
            path.write_text(body, encoding="utf-8")
        except OSError as exc:
            log.warning(
                "%s: could not record a chat turn in %s: %s",
                self.manifest.id,
                self.conversation,
                exc,
            )


@dataclass(frozen=True, slots=True)
class ConversationSummary:
    """The list-view facts for one remembered conversation."""

    id: str
    last_turn_at: str
    turn_count: int


def conversation_summaries(manifest: ResidentManifest) -> list[ConversationSummary]:
    """Return readable conversation windows, newest conversation first."""
    directory = resolve_chat_dir(manifest)
    summaries: list[ConversationSummary] = []
    try:
        paths = directory.glob("*.jsonl") if directory.is_dir() else ()
        for path in paths:
            turns = Transcript(manifest, path.stem).turns()
            if turns:
                summaries.append(
                    ConversationSummary(
                        id=path.stem,
                        last_turn_at=turns[-1].at,
                        turn_count=len(turns),
                    )
                )
    except OSError:
        return []
    return sorted(
        summaries,
        key=lambda summary: (summary.last_turn_at, summary.id),
        reverse=True,
    )
