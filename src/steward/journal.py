"""The resident journal: durable memory, written by the resident, in its own hand.

A headless session wakes up amnesiac. The journal is the narrowest honest fix: at the
end of its day a resident writes a short entry — what it did, what is unfinished, what
it noticed — into the durable location **its own manifest declares**, and the next
session opens with that entry in its prompt. Continuity comes from an artifact the
resident really wrote. Steward never summarizes on a resident's behalf, and never
invents an entry: a day with no journal injects the previous surviving entry, or
nothing at all.

**Where entries live** is resolved strictly from ``memory`` — ``<memory.path>/<memory.journal>``,
one markdown file per local day, ``YYYY-MM-DD.md``. There is no fallback location. A
memory block steward cannot journal into is a startup error (:func:`journal_complaint`,
surfaced by ``steward doctor`` and by the scheduler before its first breath), not a
silent failure at midnight.

**Which day** is the day in the *routine's* ``schedule_tz``. A run at 00:30 in
Europe/Ljubljana belongs to that morning, not to the UTC day the NAS happens to be in.

**Who writes it.** The session does. Steward's job is three things and no more:

1. the instruction — :func:`close_of_day_instruction`, appended to the prompt of the
   one routine a manifest flags ``journal: close_of_day``;
2. reading it back — :func:`latest_entry` for injection, :func:`read_entries` for
   humans and for the API;
3. keeping it bounded — :func:`rotate`, run whenever a new entry could have appeared.

A headless run may have nowhere to write, so the instruction also documents a fallback:
an entry emitted between ``<journal>`` and ``</journal>`` markers in the session's final
output is persisted verbatim by :func:`persist_close_of_day`, attributed to the routine.
**A file the session wrote itself always wins** — the markers are a fallback, not a
shortcut, and steward would rather keep the resident's own file than its own copy.
"""

import datetime as dt
import re
from dataclasses import dataclass
from pathlib import Path
from zoneinfo import ZoneInfo

from steward.manifest import (
    CLOSE_OF_DAY,
    DEFAULT_JOURNAL_DIR,
    DEFAULT_KEEP_ENTRIES,
    Diagnostic,
    ManifestError,
    ResidentManifest,
    Routine,
)
from steward.prompt import JOURNAL_MAX_CHARS

__all__ = [
    "CLOSE_OF_DAY",
    "DEFAULT_JOURNAL_DIR",
    "DEFAULT_KEEP_ENTRIES",
    "ENTRY_SUFFIX",
    "JOURNAL_CLOSE",
    "JOURNAL_MAX_CHARS",
    "JOURNAL_OPEN",
    "CloseOfDay",
    "JournalEntry",
    "cap_entry",
    "close_of_day_instruction",
    "close_of_day_routine",
    "entry_header",
    "entry_path",
    "extract_block",
    "journal_complaint",
    "latest_entry",
    "latest_entry_text",
    "local_day",
    "persist_close_of_day",
    "read_entries",
    "resolve_journal_dir",
    "rotate",
    "write_entry",
]

ENTRY_SUFFIX = ".md"

#: The markers the close-of-day instruction documents, for a session with nowhere to write.
JOURNAL_OPEN = "<journal>"
JOURNAL_CLOSE = "</journal>"

_ENTRY_NAME = re.compile(r"^(?P<day>\d{4}-\d{2}-\d{2})\.md$")
_BLOCK = re.compile(
    re.escape(JOURNAL_OPEN) + r"(?P<body>.*?)" + re.escape(JOURNAL_CLOSE), re.DOTALL
)
_HEADER = re.compile(r"\A---\r?\n(?P<header>.*?)\r?\n---\r?\n?(?P<body>.*)\Z", re.DOTALL)
_HEADER_LINE = re.compile(r"^(?P<key>[a-z_]+):\s*(?P<value>.*)$")
_SCHEME = re.compile(r"^[a-z][a-z0-9+.-]*://")

_MEMORY_EXAMPLE = "memory: {kind: directory, path: /data/residents/<id>/memory, journal: journal}"


# --------------------------------------------------------------------------------------
# where the journal lives — from the manifest, and only from the manifest
# --------------------------------------------------------------------------------------


def journal_complaint(manifest: ResidentManifest) -> str | None:
    """Return why this resident cannot keep a journal, or ``None`` when it can.

    Pure: it reads the declaration, never the filesystem, so it is answerable at
    schedule time on a laptop that has never seen the resident's NAS volume. The
    scheduler calls it before it takes its first breath and ``steward doctor`` prints
    it, which is the whole point — a memory block with nowhere to put a journal must
    be a complaint in daylight, not a routine that quietly writes nothing at midnight.
    """
    memory = manifest.memory
    if memory.kind == "file":
        return (
            "memory.kind is 'file', so there is nowhere to keep one entry per day; "
            "journaling needs memory.kind 'directory' or 'repo'"
        )
    if _SCHEME.match(memory.path):
        return (
            f"memory.path {memory.path!r} is a remote reference; a journal is read and "
            f"written as ordinary files, so it needs a local directory"
        )
    reference = Path(memory.journal)
    if reference.is_absolute() or ".." in reference.parts:
        return (
            f"memory.journal is {memory.journal!r}; entries live inside memory.path, so "
            f"it can be neither absolute nor a path that climbs out with '..'"
        )
    return None


def resolve_journal_dir(manifest: ResidentManifest, *, source: Path | None = None) -> Path:
    """Return the directory this resident's daily entries live in.

    ``<memory.path>/<memory.journal>``, expanded, and nothing else. There is no default
    location and no hardcoded path: two residents with different memory references can
    never read each other's journals, because neither one knows a path it was not told.

    Raises :class:`~steward.manifest.ManifestError` — carrying a real diagnostic, with
    the file and the field named — when the memory block cannot hold a journal.
    """
    complaint = journal_complaint(manifest)
    if complaint is not None:
        raise ManifestError(
            [
                Diagnostic(
                    file=source if source is not None else Path(f"residents/{manifest.id}"),
                    field_path="memory",
                    problem=complaint,
                    example=_MEMORY_EXAMPLE,
                )
            ]
        )
    return (Path(manifest.memory.path).expanduser() / manifest.memory.journal).resolve()


def local_day(routine: Routine, moment: dt.datetime) -> dt.date:
    """Return the calendar day a run belongs to, read in the routine's own zone.

    "The last routine of the day" is a wall-clock fact where the household is. A
    23:55 Europe/Ljubljana run belongs to that evening and a 00:30 one to the new
    morning, whatever date UTC is on.
    """
    return moment.astimezone(ZoneInfo(routine.schedule_tz)).date()


def entry_path(manifest: ResidentManifest, day: dt.date, *, source: Path | None = None) -> Path:
    """Return the file one day's entry lives in: ``<journal dir>/YYYY-MM-DD.md``."""
    return resolve_journal_dir(manifest, source=source) / f"{day.isoformat()}{ENTRY_SUFFIX}"


# --------------------------------------------------------------------------------------
# entries
# --------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class JournalEntry:
    """One day's entry: when, who wrote it, and what it says.

    ``routine`` and ``resident`` are whatever the entry's own header claims, so each is
    ``None`` for an entry written without one. Steward reports what the file says rather
    than filling the gap in.
    """

    date: dt.date
    routine: str | None
    text: str
    path: Path
    #: The ``resident:`` header the entry declares, or ``None`` on a legacy/headerless
    #: entry. It is how a shared journal directory is kept from cross-feeding two
    #: residents (:func:`read_entries`), so it is reported as plainly as the routine is.
    resident: str | None = None

    def as_dict(self) -> dict[str, str | None]:
        """Render the entry as plain JSON-able data, for the API and the CLI."""
        return {
            "date": self.date.isoformat(),
            "routine": self.routine,
            "resident": self.resident,
            "text": self.text,
            "path": str(self.path),
        }


def entry_header(manifest: ResidentManifest, day: dt.date, routine_id: str) -> str:
    """Return the small header every entry opens with: resident, date, writing routine."""
    return "\n".join(
        [
            "---",
            f"resident: {manifest.id}",
            f"date: {day.isoformat()}",
            f"routine: {routine_id}",
            "---",
        ]
    )


def _parse_entry(path: Path, day: dt.date) -> JournalEntry | None:
    """Read one entry file. A header is parsed if present and never required.

    The journal is text a *model* wrote, so a byte in it that is not valid UTF-8 is a
    self-inflicted wound and must degrade to "unreadable, skipped" rather than bricking
    the resident. The read replaces undecodable bytes instead of raising, and the guard
    is deliberately broad — any failure to read one entry is one skipped entry, never a
    dangling ``routine_started`` upstream (#75).
    """
    try:
        raw = path.read_text(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001 — a bad entry is skipped, never a raise that bricks a read (#75)
        return None
    routine: str | None = None
    resident: str | None = None
    body = raw
    match = _HEADER.match(raw)
    if match is not None:
        body = match.group("body")
        for line in match.group("header").splitlines():
            fields = _HEADER_LINE.match(line.strip())
            if fields is None:
                continue
            if fields.group("key") == "routine":
                routine = fields.group("value").strip() or None
            elif fields.group("key") == "resident":
                resident = fields.group("value").strip() or None
    body = body.strip()
    if not body:
        return None
    return JournalEntry(date=day, routine=routine, text=body, path=path, resident=resident)


def _belongs(entry: JournalEntry, manifest: ResidentManifest) -> bool:
    """Report whether this entry is this resident's to read or rotate.

    Ownership is lenient by design (#77): an entry whose header names a *different*
    resident is somebody else's — two manifests sharing a ``memory.path`` must never
    cross-feed — but an entry with no ``resident:`` header is legacy or hand-written and
    is treated as this resident's, because refusing every headerless entry would silently
    drop the very entries steward has always read (see ``test_an_entry_written_without_a_
    header_still_reads``).
    """
    return entry.resident is None or entry.resident == manifest.id


def _entry_files(directory: Path) -> list[tuple[dt.date, Path]]:
    """Return every ``YYYY-MM-DD.md`` in a journal directory, newest first.

    Anything else in the directory is somebody else's file and is left alone — steward
    only ever rotates what it can name.
    """
    try:
        candidates = list(directory.iterdir())
    except OSError:
        return []
    found: list[tuple[dt.date, Path]] = []
    for path in candidates:
        match = _ENTRY_NAME.match(path.name)
        if match is None or not path.is_file():
            continue
        try:
            day = dt.date.fromisoformat(match.group("day"))
        except ValueError:
            continue
        found.append((day, path))
    return sorted(found, key=lambda pair: pair[0], reverse=True)


def read_entries(
    manifest: ResidentManifest,
    limit: int = DEFAULT_KEEP_ENTRIES,
    *,
    source: Path | None = None,
) -> list[JournalEntry]:
    """Return this resident's entries, newest first, at most ``limit`` of them.

    The read path for everything outside a session: the CLI, and the token-gated
    read-only API endpoint burrow's house panel will call. An empty journal is an
    empty list — a resident that has never written renders as one that has never
    written.
    """
    if limit <= 0:
        return []
    directory = resolve_journal_dir(manifest, source=source)
    entries: list[JournalEntry] = []
    for day, path in _entry_files(directory):
        entry = _parse_entry(path, day)
        if entry is None or not _belongs(entry, manifest):
            continue
        entries.append(entry)
        if len(entries) >= limit:
            break
    return entries


def latest_entry_text(
    manifest: ResidentManifest,
    *,
    source: Path | None = None,
) -> str | None:
    """Return the most recent *surviving* entry, rendered whole and uncapped.

    "Surviving" is the load-bearing word. If last night's session died before it
    journaled, this returns the night before's entry; if the resident has never
    written one, it returns ``None``. Steward never fills the gap in.

    Split out of :func:`latest_entry` so a caller that has to **transform** the entry can
    do that to the whole text and cap afterwards (steward #209). A cap applied first can
    destroy the very shape a detector matches on: ``redact_secrets`` finds a PEM block by
    its ``BEGIN`` *and* ``END`` markers, so a block straddling the cut loses its ``END``,
    stops matching, and leaves live key bytes standing next to the ``[redacted:secret]``
    that replaced its lone ``BEGIN`` — which reads as though the scrub worked. The events
    egress has held redact-then-bound since steward #65; this is what lets the journal
    egress hold it too.
    """
    entries = read_entries(manifest, 1, source=source)
    if not entries:
        return None
    entry = entries[0]
    dateline = f"{entry.date.isoformat()}"
    if entry.routine:
        dateline += f" — written at the close of {entry.routine}"
    return f"{dateline}\n\n{entry.text}"


def cap_entry(rendered: str, cap_chars: int = JOURNAL_MAX_CHARS) -> str:
    """Cut a rendered entry down to the injection cap, saying so when it cuts.

    The cap is :data:`~steward.prompt.JOURNAL_MAX_CHARS` — a journal is a note to
    tomorrow, not a transcript, and it is paid for on every single session launch.
    """
    if len(rendered) <= cap_chars:
        return rendered
    return rendered[:cap_chars].rstrip() + "\n\n[truncated at the injection cap]"


def latest_entry(
    manifest: ResidentManifest,
    cap_chars: int = JOURNAL_MAX_CHARS,
    *,
    source: Path | None = None,
) -> str | None:
    """Return the most recent surviving entry, truncated at the injection cap.

    What every session-launch path injects, and it is unchanged: :func:`latest_entry_text`
    read whole, then :func:`cap_entry` applied. A caller that has to redact reaches for
    those two directly and puts its own step between them — never here, because a preamble
    hands a resident back its own writing and scrubbing that would misquote it.
    """
    rendered = latest_entry_text(manifest, source=source)
    if rendered is None:
        return None
    return cap_entry(rendered, cap_chars)


def write_entry(
    manifest: ResidentManifest,
    day: dt.date,
    routine_id: str,
    text: str,
    *,
    source: Path | None = None,
) -> Path:
    """Write one day's entry, header first, and return where it landed.

    Only ever used for the ``<journal>`` fallback: when the session writes its own
    file, steward keeps that file and does not rewrite it.
    """
    path = entry_path(manifest, day, source=source)
    path.parent.mkdir(parents=True, exist_ok=True)
    header = entry_header(manifest, day, routine_id)
    path.write_text(f"{header}\n\n{text.strip()}\n", encoding="utf-8")
    return path


def rotate(
    manifest: ResidentManifest,
    keep: int | None = None,
    *,
    source: Path | None = None,
) -> tuple[Path, ...]:
    """Delete all but the newest ``keep`` entries and return what was removed.

    Enforced whenever a new entry could have appeared, so the directory is bounded by
    construction rather than by a cron job somebody remembers to write. This mirrors
    burrow's log-rotation stance: the live window is small, nothing is unbounded.
    """
    limit = keep if keep is not None else manifest.memory.journal_keep
    directory = resolve_journal_dir(manifest, source=source)

    # Rotate over entries that actually parse and belong to this resident (#78). A file a
    # died session left empty or garbage is not an entry, so it must not count toward the
    # bound and evict a real one — and, being unreadable, it is left in place rather than
    # deleted, mirroring "steward only ever removes what it can read". An entry another
    # resident wrote into a shared directory is likewise never this resident's to rotate.
    mine: list[Path] = []
    for day, path in _entry_files(directory):
        entry = _parse_entry(path, day)
        if entry is None or not _belongs(entry, manifest):
            continue
        mine.append(path)

    removed: list[Path] = []
    for path in mine[limit:]:
        try:
            path.unlink()
        except OSError:  # pragma: no cover — a read-only journal dir is not a run failure
            continue
        removed.append(path)
    return tuple(removed)


# --------------------------------------------------------------------------------------
# close of day
# --------------------------------------------------------------------------------------


def close_of_day_routine(manifest: ResidentManifest) -> Routine | None:
    """Return the one routine this manifest flags ``journal: close_of_day``, if any.

    The contract is an explicit flag rather than "whichever routine steward computes
    fires last". A resident should be able to read its own manifest and know which run
    ends its day; a rule that depends on cron arithmetic, time zones, and which
    routines happen to be enabled today is not something anyone can read.
    """
    for routine in manifest.routines:
        if routine.enabled and routine.journal == CLOSE_OF_DAY:
            return routine
    return None


def close_of_day_instruction(
    manifest: ResidentManifest,
    day: dt.date,
    routine_id: str,
    *,
    source: Path | None = None,
) -> str:
    """Return the "close the day" instruction appended to the closing routine's prompt.

    It names the exact file, the exact header, and the fallback markers, because a
    session that has to guess any of the three writes its journal somewhere nobody
    reads it from.
    """
    path = entry_path(manifest, day, source=source)
    header = entry_header(manifest, day, routine_id)
    indented = "\n".join(f"    {line}" for line in header.splitlines())
    opening = (
        "This is the last routine of your day. Before you finish, write your journal "
        "entry for this day to:"
    )
    how = (
        "Open the file with exactly this header, then write freely, in your own voice: "
        "what you did, what is still unfinished, what you noticed and want to remember. "
        "A few short paragraphs. You are writing to tomorrow's session, which will be "
        "you, waking up with no memory of today."
    )
    fallback = (
        f"If you cannot write that file, put the same entry in your final message between "
        f"{JOURNAL_OPEN} and {JOURNAL_CLOSE} markers and steward will save it for you. The "
        f"file you write yourself is the one that counts; the markers are a fallback, not a "
        f"shortcut. Write one entry, not both."
    )
    return "\n".join([opening, "", f"    {path}", "", how, "", indented, "", fallback])


def extract_block(output: str) -> str | None:
    """Return the text between ``<journal>`` and ``</journal>``, if the output has it.

    The last block wins: a session that quotes the instruction back before writing its
    real entry should have its real entry kept. Empty markers are nothing, not an entry.
    """
    matches = list(_BLOCK.finditer(output or ""))
    if not matches:
        return None
    body = matches[-1].group("body").strip()
    return body or None


@dataclass(frozen=True, slots=True)
class CloseOfDay:
    """What closing the day actually came to. Every field is something that happened."""

    #: The entry that exists now, or ``None`` when neither the session nor its output
    #: produced one. Steward does not write a placeholder.
    path: Path | None = None
    #: True only when *steward* wrote the file from a ``<journal>`` block. False when
    #: the session wrote its own file — which is the outcome we would rather have.
    persisted: bool = False
    rotated: tuple[Path, ...] = ()


def persist_close_of_day(
    manifest: ResidentManifest,
    day: dt.date,
    routine_id: str,
    output: str,
    *,
    source: Path | None = None,
) -> CloseOfDay:
    """Make sure the day's entry exists if the session gave us one, then rotate.

    Precedence is the point: a file the session wrote itself is left exactly as it is,
    even when the output also carries a ``<journal>`` block. Steward's copy is the
    fallback for a session that had nowhere to write, never a replacement for the
    resident's own hand.
    """
    path = entry_path(manifest, day, source=source)
    persisted = False
    try:
        written_by_session = path.is_file() and bool(
            path.read_text(encoding="utf-8", errors="replace").strip()
        )
    except Exception:  # noqa: BLE001 — closing the day must never raise past this (#75)
        # A file we cannot even read is not one the session usefully wrote: fall through
        # to the block fallback rather than letting a dangling routine_started stand.
        written_by_session = False

    if written_by_session:
        existing: Path | None = path
    else:
        block = extract_block(output)
        if block is None:
            existing = None
        else:
            existing = write_entry(manifest, day, routine_id, block, source=source)
            persisted = True

    return CloseOfDay(path=existing, persisted=persisted, rotated=rotate(manifest, source=source))
