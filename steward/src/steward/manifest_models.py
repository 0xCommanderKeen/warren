"""Resident declaration models and values, independent of loading and fleet validation."""

import re
import zoneinfo
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Any, Literal, Self, get_args

from croniter import croniter
from pydantic import (
    UUID4,
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    RootModel,
    StringConstraints,
    ValidationInfo,
    field_validator,
    model_validator,
)

from steward.diagnostics import closest_match

MANIFEST_FILENAME = "manifest.yaml"

#: What a tree with nothing in it is called. A warning rather than an error here on
#: purpose — asking about an empty directory is a fair question — but ``steward validate``
#: promotes it when nobody named the tree, because that run is CI's merge gate and a gate
#: that passes having read no manifests is not a gate (steward #137).
NO_MANIFESTS_PROBLEM = "no resident manifests found"

DEFAULT_SOUL_FILENAME = "soul.md"
SCHEMA_VERSION = 0
VILLAGE_HOME_MIN = 0
VILLAGE_HOME_MAX = 7
VOICE_MAX_CHARS = 1200
DISCORD_CHANNEL_NAME_MAX_CHARS = 100
DISCORD_LISTEN_CHANNELS_MAX = 10
VOICE_HEADING = "## Voice"

#: The charter and the identity section are the two parts of a preamble that are **never**
#: truncated on their way into a prompt (:mod:`steward.prompt`): a hard rule cut in half is
#: worse than no hard rule, and half a name is not an identity. So the bound they get is a
#: refusal here, at authoring time, rather than a silent shortening at 3am — and with it the
#: total size of a preamble becomes something you can compute rather than hope about. Without
#: these numbers the section framed as the last word is also the one section able to crowd
#: out every bounded section above it (steward #147). Each is generous against what live
#: residents actually write: across ``residents/`` the longest mission is 359 characters,
#: the longest duty 90, the longest hard rule 92, the longest summary 72.
CHARTER_MISSION_MAX_CHARS = 2000

#: One duty, one hard rule, or one escalation trigger. A line, not an essay.
CHARTER_ENTRY_MAX_CHARS = 400

#: How many duties, rules, or escalation triggers one charter may list. A charter nobody can
#: hold in their head is not a charter, and the session has to hold it too.
CHARTER_ENTRIES_MAX = 20

#: The free-text escalation form, for a manifest that writes prose instead of an
#: :class:`Escalation` block.
ESCALATION_MAX_CHARS = 2000

#: ``how`` names a protocol or a channel — ``needs_human``, an address; ``note`` is the extra
#: guidance for whoever gets woken.
ESCALATION_HOW_MAX_CHARS = 200
ESCALATION_NOTE_MAX_CHARS = 1000

#: The identity section: a display name, a one-line role, and the one line burrow displays.
SOUL_NAME_MAX_CHARS = 80
SOUL_ROLE_MAX_CHARS = 200
SUMMARY_MAX_CHARS = 400

#: A routine's own prompt. Declared text like the charter, and it lands in a section of its
#: own *after* the charter — the last thing a session reads — so leaving it unbounded left
#: the one manifest field able to make the size of an assembled prompt uncomputable, and
#: the one able to draw a section rule in the best position for a forgery to be believed.
#: Bounded here for the same reason the charter is, and neutralized in :mod:`steward.prompt`
#: alongside it (steward #147).
ROUTINE_PROMPT_MAX_CHARS = 8000

#: The journal subdirectory, under ``memory.path``, a manifest gets when it names none.
DEFAULT_JOURNAL_DIR = "journal"

#: Keep the newest N entries. Counted, not aged: there is at most one entry per local
#: day, so 30 is "the last 30 days this resident actually wrote", and one that was quiet
#: for a month still has its last entry to wake up to. An age cut-off would delete it.
DEFAULT_KEEP_ENTRIES = 30
MAX_JOURNAL_KEEP = 3650

#: The one legal value of a routine's ``journal:`` flag: the run that ends the day.
CLOSE_OF_DAY = "close_of_day"

#: Keep an over-firing diagnostic compact once its exact count stops being useful.
MANY_FIRES_DISPLAY_THRESHOLD = 24

#: The word a manifest writes instead of a list when a resident's tools are not bounded.
#: Unlimited is *said* here rather than left as a silence, for the same reason ``budgets``
#: reports a limit of ``null`` rather than omitting the gauge: "which residents can reach
#: anything" is then one grep over the tree, not an audit of which key is absent from which
#: file. It is also the one word that makes ``tools`` safe to require of every manifest.
UNRESTRICTED_TOOLS = "unrestricted"

#: Named write doors a resident session may be granted. Closed here so a typo cannot
#: silently become a permission that looks declared but never opens anything.
SESSION_GRANT_SKILLS_WRITE = "skills.write"
SESSION_GRANT_RESIDENTS_DECLARE = "residents.declare"
SESSION_GRANT_RESIDENTS_DRY_RUN = "residents.dry_run"
#: Deliberately not implied by ``residents.dry_run`` (warren#446). A dry run reads a
#: rendered plan and costs nothing; a rehearsal runs a model turn and spends the caller's
#: budget, so a resident handed the cheap door must not inherit the expensive one.
SESSION_GRANT_RESIDENTS_REHEARSE = "residents.rehearse"
SESSION_GRANT_RESIDENTS_GRANT_SKILL = "residents.grant_skill"


class SessionGrant(StrEnum):
    """A write door an authenticated resident session may cross."""

    SKILLS_WRITE = SESSION_GRANT_SKILLS_WRITE
    RESIDENTS_DECLARE = SESSION_GRANT_RESIDENTS_DECLARE
    RESIDENTS_DRY_RUN = SESSION_GRANT_RESIDENTS_DRY_RUN
    RESIDENTS_REHEARSE = SESSION_GRANT_RESIDENTS_REHEARSE
    #: The one door that does not open on the grant alone (warren#437). A session holding
    #: it may ``PUT`` a declaration only while presenting an approval a human answered
    #: ``approve`` to, and only when the edit is the one that approval describes.
    RESIDENTS_GRANT_SKILL = SESSION_GRANT_RESIDENTS_GRANT_SKILL


#: A built-in tool name as ``claude --tools`` spells it — ``Read``, ``Glob``, ``WebFetch``.
#: Names only. ``--tools`` takes names *from the built-in set*, not the rule syntax that
#: ``--allowed-tools`` accepts, so ``Bash(git *)`` is not something that can be written
#: here. The pattern forbids a comma inside a value too, which turns the near miss
#: ``tools: Read,Glob`` — one string where a list was meant — into a diagnostic rather than
#: a single tool name that will never match anything.
TOOL_NAME_PATTERN = r"^[A-Za-z][A-Za-z0-9_]*$"

#: How the claude CLI spells a tool that came from an MCP server. Refused inside a bounded
#: list: see :func:`_check_tools_are_enforceable`.
MCP_TOOL_PREFIX = "mcp__"

#: A directory a session may reach beyond its own working directory.
#:
#: Absolute, because a relative path would resolve against the working directory — the one
#: place the resident can already write — so "which directory did that grant name" would
#: depend on where steward happened to be launched from rather than on what the manifest
#: says. The character class is ``memory.path``'s and is there for the same reason: this
#: value is interpolated into an argv, and for a provisioned resident into generated
#: compose YAML, so it has to be data and never markup (steward #61).
WORKSPACE_PATH_PATTERN = r"""^/[^\s'"`$;|&<>(){}\[\]!*?\\]*$"""

#: The permission mode that auto-approves every call a session makes. Named because a
#: manifest that bounds its tools and then declares this has drawn one boundary and dropped
#: the other, which :func:`_check_tools_are_enforceable` refuses.
BYPASS_PERMISSIONS = "bypassPermissions"

#: ``soul.file`` is joined onto the manifest's own directory at three places — the
#: validation read, the deploy bundle read, and the nursery's declare write — and
#: ``pathlib`` semantics mean an absolute value replaces the base entirely while ``..``
#: segments compose without normalisation. It was the one path component in a manifest
#: with no pattern and no validator, in a module whose stated posture is that a value is
#: data and never markup, so it was gated by review rather than by validation (steward
#: #149). This closes it the way the others are closed: **a name, not a path**. No
#: separator of either slash, no leading dot — which is what excludes ``..`` — and none of
#: the whitespace, quotes, or shell metacharacters the deploy patterns already refuse.
#: The archive entry name is the constant :data:`steward.deploy.SOUL_FILENAME`, so the
#: remote layout never depended on this value; only which local file is read did.
SOUL_FILE_PATTERN = r"^[A-Za-z0-9_][A-Za-z0-9_.-]*$"

SLUG_PATTERN = r"^[a-z0-9][a-z0-9-]*$"
ACCENT_PATTERN = r"^#[0-9a-fA-F]{6}$"
AGENT_ID_PATTERN = r"^[a-z0-9][a-z0-9._-]*:[A-Za-z0-9._:-]+$"

_CRON_FIELD = r"(?:\*|[0-9]+|[0-9]+-[0-9]+|\*/[0-9]+|[0-9]+/[0-9]+|[0-9]+-[0-9]+/[0-9]+)"
_CRON_LIST = rf"{_CRON_FIELD}(?:,{_CRON_FIELD})*"
CRON_PATTERN = re.compile(rf"^{_CRON_LIST}(?:\s+{_CRON_LIST}){{4}}$")

MAX_TIMEOUT_S = 24 * 60 * 60

DEFAULT_SCHEDULE_TZ = "UTC"

#: The route kind that names the job board as a channel work reaches a resident through.
JOB_BOARD_ROUTE_KIND = "job-board"

#: The route kind that makes a channel deliverable: steward may hand another resident's
#: delegated work into a route of this kind, and into no other. Naming it as a *kind*
#: rather than a flag keeps one vocabulary — ``routes`` is already this manifest's answer
#: to "how does work reach this resident", and a letter from a neighbour is work reaching
#: it. The route's ``id`` is what a delegating session names in its block.
DELEGATION_ROUTE_KIND = "delegation"

#: The route kind an operator's message arrives through (warren#108). The third kind
#: steward *delivers* into rather than merely describes, and the only one where the thing
#: arriving is a person rather than the fleet: the bridge maps the route's ``address``
#: reference to a bot token held in steward's own environment, fires a session with the
#: message, and sends the answer back. The address stays a reference — ``telegram:pip`` —
#: for the reason every reference field in this file is one: a manifest is git, and a bot
#: token in git is a bot anybody who clones the repo can speak as.
CHAT_ROUTE_KIND = "chat"

CHAT_TOKEN_ENV_PREFIX = "STEWARD_CHAT_TOKEN_"  # noqa: S105 — variable prefix, not a secret
_CHAT_ADDRESS = re.compile(r"^(?P<transport>[a-z][a-z0-9+.-]*):(?P<reference>\S.*)$")
_CHAT_TOKEN_ENV_UNSAFE = re.compile(r"[^A-Z0-9]+")


def chat_token_env_name(address: str) -> str | None:
    """Return the credential slot for a parseable chat address.

    This lives beside manifest validation so the collision check and runtime lookup cannot
    drift. Telegram retains its v0 reference-only name; every other transport is qualified.
    """
    match = _CHAT_ADDRESS.match(address.strip())
    if match is None:
        return None
    transport = match.group("transport")
    reference = match.group("reference")
    qualified = reference if transport == "telegram" else f"{transport}_{reference}"
    folded = _CHAT_TOKEN_ENV_UNSAFE.sub("_", qualified.upper()).strip("_")
    return f"{CHAT_TOKEN_ENV_PREFIX}{folded}"


#: Where a routine's final message may be sent (warren#385, warren#399). ``chat`` names
#: the route *kind* and remains the convenient spelling when exactly one active chat route
#: exists. A transport-qualified address names one declared route when there are several.
#: The pattern rejects misspellings before a paid-for digest can disappear into them; the
#: cross-field check below proves an address is active and belongs to this resident.
ROUTINE_DELIVER_PATTERN = r"^(?:chat|[a-z][a-z0-9+.-]*:\S+)$"
RoutineDeliverKind = Annotated[str, StringConstraints(pattern=ROUTINE_DELIVER_PATTERN)]
ROUTINE_DELIVER_CHAT = "chat"

#: A quiet word is one token: no whitespace, and short enough to be typed exactly. A
#: session says it *instead of* a message, so "did the resident say the quiet word" has to
#: be an exact comparison, and a phrase would make every near-miss a message on a phone.
QUIET_WORD_MAX_CHARS = 32
QUIET_WORD_PATTERN = re.compile(rf"^\S{{1,{QUIET_WORD_MAX_CHARS}}}$")

#: One transport name, as a manifest spells it. A closed set for the reason
#: :data:`PermissionMode` is one: a typo in a transport name would otherwise be a manifest
#: that reads as wired up and taps nobody, discovered on the night an approval knock does
#: not arrive. Telegram belongs here when it exists — as a second transport of *this*
#: declaration, not as an outbound growth of the chat bridge (warren#108).
NotificationTransport = Literal["ntfy", "discord"]

#: One fact a tap may be sent about, spelled as the chronicle event type it follows rather
#: than as a second vocabulary — so an ``on:`` list reads against ``docs/transitions.md``
#: directly. This module cannot import :mod:`steward.events` (that module imports *this*
#: one, for redaction), so the strings are repeated here and :mod:`steward.notify` refuses
#: to import if the two ever disagree.
NotificationKind = Literal["needs_human", "task_done"]

#: The same two vocabularies as tuples, for the diagnostics and the checks that have to
#: *name* the members. Derived rather than written twice: a hand-kept copy of a ``Literal``
#: is a copy that goes stale the first time somebody adds a transport.
NOTIFICATION_TRANSPORTS: tuple[NotificationTransport, ...] = get_args(NotificationTransport)
NOTIFICATION_KINDS: tuple[NotificationKind, ...] = get_args(NotificationKind)

#: The one kind whose enforceability depends on the rest of the manifest — a resident that
#: claims no board work and takes no letters closes no tasks. Named rather than spelled as a
#: string literal inside :func:`_check_notifications_are_deliverable`, so the check cannot
#: quietly stop matching a kind somebody renamed.
NOTIFY_TASK_DONE: NotificationKind = "task_done"

#: How long a claim is good for before the task returns to the board (30 minutes).
DEFAULT_BOARD_LEASE_S = 30 * 60

#: How long a board session may run before steward kills it (15 minutes).
DEFAULT_BOARD_TIMEOUT_S = 900

#: A wake-up is a wake-up, not a shift. More than this and the resident is a queue worker.
MAX_CLAIMS_PER_WAKE = 10

#: A container name is a docker identifier: letters, digits, and ``_.-`` after the first.
CONTAINER_PATTERN = r"^[a-zA-Z0-9][a-zA-Z0-9_.-]*$"

#: The four ``deploy`` patterns are narrow on purpose, and the reason is not tidiness.
#: Everything in that block is interpolated into an ``ssh`` command line, and ``ssh``
#: hands its arguments to a shell on the far side — so a host, user, or path carrying a
#: space, a quote, a ``;`` or a ``$(…)`` would be a manifest that runs arbitrary commands
#: on the NAS. steward never builds a shell string, but the remote end is a shell whether
#: steward likes it or not, and these patterns are where that fact is answered.
HOST_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9.-]*$"
SSH_USER_PATTERN = r"^[A-Za-z_][A-Za-z0-9._-]*$"
REMOTE_PATH_PATTERN = r"^[A-Za-z0-9~/][A-Za-z0-9_./-]*$"
MOUNT_HOST_PATH_PATTERN = r"""^(?:/|~/)[^\s'"`:$;|&<>(){}\[\]!*?\\]+$"""
MOUNT_CONTAINER_PATH_PATTERN = r"""^/[^\s'"`:$;|&<>(){}\[\]!*?\\]*$"""
IMAGE_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9_.:/@-]*$"

#: ``project`` becomes ``CHRONICLE_PROJECT`` in the resident's compose environment, so like
#: every value that lands in generated YAML it has to be data and not markup: a slug-ish
#: label with no whitespace, no ``:`` and no newline. Without this a project string could
#: reopen the compose document from inside a value (#61).
PROJECT_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._-]*$"

#: A directory ``memory.path`` is mounted into the container and written into its compose
#: file twice — as ``working_dir`` and as the source of a ``./memory:<path>`` volume — so a
#: value carrying whitespace, a quote, ``$(…)``, a brace, or (the #61 exploit) a newline is
#: a value trying to be markup rather than a reference. This is the same "the value is data,
#: never markup" boundary the deploy patterns draw, one step earlier: it forbids that whole
#: class of character while still allowing an ordinary POSIX path or a scheme reference
#: (``s3://…``) — the latter is a *directory* the schema accepts and :mod:`steward.journal`
#: refuses at schedule time, not something this pattern's job is to judge.
MEMORY_PATH_PATTERN = r"""^[^\s'"`$;|&<>(){}\[\]!*?\\]+$"""

#: Control characters — and the line breaks among them — never belong in a single-line
#: reference or label. A newline that survived validation is exactly how a value became a
#: second YAML key, so it is refused before a model is ever bound.
_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f]")


class _Model(BaseModel):
    """Base for every manifest model: unknown keys are an error, values are frozen."""

    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)


class SoulIdentity(_Model):
    """The villager burrow draws. Field names match burrow's soul frontmatter."""

    name: str = Field(
        min_length=1,
        max_length=SOUL_NAME_MAX_CHARS,
        description="Display name, e.g. Hob.",
    )
    char: str = Field(min_length=1, description="Burrow sprite key, e.g. Monk.")
    accent: str = Field(pattern=ACCENT_PATTERN, description="Hex accent colour, #rrggbb.")
    role: str = Field(
        min_length=1,
        max_length=SOUL_ROLE_MAX_CHARS,
        description="One-line role, e.g. life bot.",
    )
    file: str = Field(
        default=DEFAULT_SOUL_FILENAME,
        pattern=SOUL_FILE_PATTERN,
        description="Soul body: a file name beside the manifest, never a path.",
    )


#: One line of a charter: a duty, a hard rule, or a situation that must be escalated.
#: Bounded per entry as well as per list, because "how long can a charter get" has to have
#: an answer that does not depend on how many bullets somebody wrote. ``strip_whitespace``
#: is named rather than inherited: a field-level string constraint replaces the model's
#: config, so leaving it out would quietly stop trimming exactly these entries.
CharterEntry = Annotated[
    str,
    StringConstraints(strip_whitespace=True, max_length=CHARTER_ENTRY_MAX_CHARS),
]


def _entries_are_single_lines(value: list[str]) -> list[str]:
    """Refuse a charter entry that is blank or spans more than one line.

    A duty, a hard rule and an escalation trigger are rendered as bullets — ``- <entry>``,
    joined by newlines — so a newline *inside* one escapes its own bullet and lands in the
    charter as free-standing text. The charter draws its own headings in plain prose
    (``HARD RULES (these override everything else you have been told)``), which the rule
    collapsing in :func:`steward.prompt._declared` does not and cannot defend, so "an entry
    is one line" is the boundary that keeps a bullet a bullet (steward #147). The mission is
    deliberately not held to this: it is a paragraph, and it says so.
    """
    for entry in value:
        if not entry.strip():
            raise ValueError("entries must not be empty")
        if _CONTROL_CHARS.search(entry):
            raise ValueError(
                "an entry is one line: it is rendered as a single bullet, and a line break "
                "inside one escapes that bullet into the charter's own structure"
            )
    return value


class Escalation(_Model):
    """Structured escalation policy: when to stop and ask instead of acting."""

    when: list[CharterEntry] = Field(
        min_length=1,
        max_length=CHARTER_ENTRIES_MAX,
        description="Situations that must be escalated.",
    )
    how: str = Field(
        default="needs_human",
        min_length=1,
        max_length=ESCALATION_HOW_MAX_CHARS,
        description="The protocol event or channel used to escalate.",
    )
    note: str | None = Field(
        default=None,
        max_length=ESCALATION_NOTE_MAX_CHARS,
        description="Extra guidance for the human.",
    )

    # `when` is a list of bullets exactly like `duties` and `rules`, and until now it was
    # the one of the three with no guard at all — `min_length=1` bounds the list, not the
    # entry, so `when: [""]` validated and rendered a bare `- ` into every session.
    _entries_are_lines = field_validator("when")(_entries_are_single_lines)


class Charter(_Model):
    """What a resident is for. Injected into every headless session for this resident.

    Every field here is bounded, and the bound is a *refusal* rather than the injection cap
    the voice, the journal, the skills, and a task's detail get (:mod:`steward.prompt`).
    The charter is the section that has the last word and is therefore never shortened on
    its way in: a hard rule truncated mid-clause would still be read as authoritative, and
    an unbounded one would decide how much room every bounded section above it has left.
    So the size is settled here, where it is somebody's pull request rather than a session's
    surprise (steward #147).
    """

    mission: str = Field(
        min_length=1,
        max_length=CHARTER_MISSION_MAX_CHARS,
        description="One paragraph of purpose.",
    )
    duties: list[CharterEntry] = Field(
        min_length=1,
        max_length=CHARTER_ENTRIES_MAX,
        description="Standing responsibilities.",
    )
    rules: list[CharterEntry] = Field(
        min_length=1,
        max_length=CHARTER_ENTRIES_MAX,
        description="Hard constraints.",
    )
    escalation: (
        Annotated[str, StringConstraints(strip_whitespace=True, max_length=ESCALATION_MAX_CHARS)]
        | Escalation
    ) = Field(description="When and how to raise needs_human.")

    _no_blank_entries = field_validator("duties", "rules")(_entries_are_single_lines)


def _normalize_skill_grant(value: object) -> object:
    """Expand the documented bare-string spelling before binding a skill grant."""
    if isinstance(value, str):
        return {"id": value}
    return value


# A field-level string constraint overrides _Model's inherited stripping before the
# pattern runs. Skill IDs therefore keep the schema's exact, untrimmed contract.
SkillId = Annotated[
    str,
    StringConstraints(strip_whitespace=False, pattern=SLUG_PATTERN),
]


class SkillGrant(_Model):
    """A named, reusable capability granted to a resident.

    The id names a skill in the library (``skills/<id>/SKILL.md``), and a name the
    library does not have fails validation with the closest match named. The default
    skills every resident holds are not listed here: a grant is what this resident has
    *on top* of them.
    """

    id: SkillId = Field(description="Skill name in the library; surrounding whitespace is invalid.")
    source: Literal["library", "local"] = Field(
        default="library",
        description="Where the skill body comes from.",
    )
    note: str | None = Field(default=None, description="Why this resident holds it.")

    @model_validator(mode="before")
    @classmethod
    def _accept_bare_string(cls, value: object) -> object:
        return _normalize_skill_grant(value)


# The model validator above preserves ``SkillGrant.model_validate("name")`` as part of
# the public parsing API. The annotated input type makes that same pre-validation rule
# visible to Pydantic's schema generator while the stored/runtime type stays SkillGrant.
SkillGrantShorthand = SkillId
SkillGrantInput = Annotated[
    SkillGrant,
    BeforeValidator(
        _normalize_skill_grant,
        json_schema_input_type=SkillGrant | SkillGrantShorthand,
    ),
]


class Memory(_Model):
    """Where a resident's durable knowledge lives. A location, never its contents."""

    kind: Literal["directory", "file", "repo"] = "directory"
    path: str = Field(min_length=1, description="Reference to the memory location.")
    journal: str = Field(
        default=DEFAULT_JOURNAL_DIR,
        min_length=1,
        description="Journal directory relative to path; one entry per local day.",
    )
    journal_keep: int = Field(
        default=DEFAULT_KEEP_ENTRIES,
        ge=1,
        le=MAX_JOURNAL_KEEP,
        description="How many entries survive rotation, newest first.",
    )

    @field_validator("path")
    @classmethod
    def _path_is_a_reference_not_markup(cls, value: str, info: ValidationInfo) -> str:
        """Reject a memory path that is markup rather than a single-line reference.

        Two boundaries, both drawn here rather than discovered when the compose file is
        rendered (#61): no path of any kind may carry a control character or a line break,
        because a newline that survives validation becomes a second key in the generated
        YAML; and a *directory* path — the one kind that is mounted into the container and
        written into its compose file — must be a plain absolute or ``~``-rooted POSIX path,
        so a value can never reach that file as anything but a quoted scalar.
        """
        if _CONTROL_CHARS.search(value):
            raise ValueError(
                "path contains a control character or line break; a memory path is a "
                "single-line reference, and a newline here would become an extra key in "
                "the generated compose file"
            )
        if info.data.get("kind", "directory") == "directory" and not re.match(
            MEMORY_PATH_PATTERN, value
        ):
            raise ValueError(
                "a directory memory.path must carry no whitespace, quotes, or shell/YAML "
                "metacharacters ($ ; | & < > ( ) { } [ ] ! * ? \\), because it is mounted "
                "into the container and written into its compose file as data"
            )
        return value


class Route(_Model):
    """A declared inbound channel through which work can reach a resident.

    A route of kind ``delegation`` is the receiving half of steward #7: it declares that
    this resident accepts work handed to it by another resident, and steward will deliver
    into it. Every other kind is a channel steward only describes.
    """

    id: str = Field(pattern=SLUG_PATTERN)
    kind: Literal["email", "chat", "http", "webhook", "cron", "cli", "job-board", "delegation"]
    address: str = Field(min_length=1, description="Reference to the channel, not a secret.")
    status: Literal["active", "pending", "disabled"] = "active"
    note: str | None = None
    shared: bool = Field(
        default=False,
        description="Route a shared Telegram bot by resident id or display name.",
    )
    posts_to: list[str] = Field(
        default_factory=list,
        description="Discord channel names this resident may post to. Empty denies posting.",
    )
    listens_in: list[str] = Field(
        default_factory=list,
        max_length=DISCORD_LISTEN_CHANNELS_MAX,
        description=(
            "Discord channel names where this resident may answer mentions. Empty listens "
            "only in operator DMs."
        ),
    )

    @field_validator("posts_to", "listens_in")
    @classmethod
    def _clean_discord_channels(cls, value: list[str]) -> list[str]:
        cleaned = [name.strip() for name in value]
        if any(not name or len(name) > DISCORD_CHANNEL_NAME_MAX_CHARS for name in cleaned):
            raise ValueError(
                "Discord channel entries must be non-empty channel names of at most 100 chars"
            )
        if len(set(cleaned)) != len(cleaned):
            raise ValueError("Discord channel names must be unique within an allowlist")
        return cleaned

    @model_validator(mode="after")
    def _shared_belongs_to_telegram_chat(self) -> Self:
        if self.shared and not (
            self.kind == CHAT_ROUTE_KIND and self.address.startswith("telegram:")
        ):
            raise ValueError("shared is allowed only on a Telegram chat route")
        return self

    @model_validator(mode="after")
    def _channels_belong_to_discord_chat(self) -> Self:
        discord_chat = self.kind == CHAT_ROUTE_KIND and self.address.startswith("discord:")
        if self.posts_to and not discord_chat:
            raise ValueError("posts_to is allowed only on a Discord chat route")
        if self.listens_in and not discord_chat:
            raise ValueError("listens_in is allowed only on a Discord chat route")
        return self

    @property
    def accepts_delegation(self) -> bool:
        """True when steward may deliver another resident's work into this route.

        Both halves are required and neither is inferred: the kind says this channel is
        *for* delegated work, and ``active`` says it is open today. A route somebody is
        still wiring up takes no letters.
        """
        return self.kind == DELEGATION_ROUTE_KIND and self.status == "active"

    @property
    def accepts_chat(self) -> bool:
        """True when the chat bridge may carry an operator's messages into this route.

        The same two halves :attr:`accepts_delegation` requires, and for the same reason
        (warren#108): the kind says this channel is a conversation, and ``active`` says the
        operator has actually been to BotFather and put the token in steward's environment.
        A ``pending`` chat route is a declaration that the bot is not wired up yet — which
        is the state a manifest ships in, because the token cannot ship with it.
        """
        return self.kind == CHAT_ROUTE_KIND and self.status == "active"


class Notifications(_Model):
    """Where this resident's outbound taps go — the one-way twin of :class:`Route`.

    ``routes`` answers *how work reaches this resident*: every kind in it is a doorway
    something arrives through, and two of them are doorways steward itself delivers into.
    This block answers the opposite question, and the opposite direction is exactly why it
    is a dimension of its own rather than a ninth route kind. A **notification** is steward
    tapping a *person* on the shoulder about a resident — a ``needs_human`` at 2am, a task
    that finished — and nothing listens for a reply: no session fires, no answer comes back,
    and the tap is not a channel anything can arrive through. Chat (warren#108) stays what
    it is, a two-way conversation where an operator speaks and a session answers; the two
    would be one type only if "a message went somewhere" were the whole of what a channel
    means, and it is not.

    Silence is not consent, exactly as it is for :class:`Board` and :class:`Delegation`: a
    manifest with no ``notifications`` block taps nobody, however loudly its resident knocks.

    Declaring a ``transport`` is the whole opt-in, and there is deliberately **no address
    field**. An ntfy topic is derived from the resident's ``uid``
    (:func:`steward.notify.ntfy_topic`), so it is unguessable in ntfy's public namespace, it
    cannot be typed wrong, and it cannot drift from the resident it belongs to. Nothing
    secret is declared here either, for the reason nothing secret is declared anywhere in a
    manifest: the ntfy server and its optional token are read from steward's own environment,
    and what a manifest says is *that* this resident taps — never how to authenticate as one.
    """

    transport: NotificationTransport | None = Field(
        default=None,
        description="Which transport carries the taps. Absent means this resident taps nobody.",
    )
    on: tuple[NotificationKind, ...] = Field(
        default=("needs_human",),
        description="Which facts are tapped. Defaults to the knock a human has to answer.",
    )
    status: Literal["active", "pending", "disabled"] = Field(
        default="active",
        description="Only 'active' sends; 'pending' and 'disabled' are declared and silent.",
    )
    note: str | None = Field(
        default=None,
        description="Who this taps, in words — 'Miha's phone'. A label, never an address.",
    )

    @property
    def enabled(self) -> bool:
        """True when steward should actually send this resident's taps.

        Both halves are required and neither is inferred, like
        :attr:`Route.accepts_delegation`: a transport says taps have somewhere to go, and
        ``active`` says the operator has actually subscribed. A topic nobody is listening to
        is a knock into an empty room, and a manifest that is still being wired up should be
        able to say so.
        """
        return self.transport is not None and self.status == "active"

    @field_validator("transport", mode="before")
    @classmethod
    def _transport_is_one_steward_has(cls, value: object) -> object:
        """Refuse a transport name steward has no way to deliver through.

        The ``Literal`` alone would refuse it too, with ``input should be 'ntfy'``. This
        exists for the message: it names the whole known set and offers the closest match,
        which is the diagnostic style every other named-vocabulary field in this file gets.
        """
        if not isinstance(value, str):
            return value
        name = value.strip()
        if name in NOTIFICATION_TRANSPORTS:
            return name
        known = ", ".join(NOTIFICATION_TRANSPORTS)
        suggestion = closest_match(name, NOTIFICATION_TRANSPORTS)
        hint = f"; did you mean {suggestion!r}?" if suggestion else ""
        raise ValueError(
            f"{name!r} is not a transport steward can deliver a notification through "
            f"(known: {known}){hint}"
        )

    @model_validator(mode="after")
    def _a_declared_transport_taps_something(self) -> Self:
        """Refuse a declaration that cannot ever send: a transport with nothing to send."""
        if self.transport is None:
            return self
        if not self.on:
            raise ValueError(
                "a declared transport with an empty 'on' taps nobody about anything; "
                f"name the facts to tap ({', '.join(NOTIFICATION_KINDS)}) or drop the block"
            )
        repeated = sorted({kind for kind in self.on if self.on.count(kind) > 1})
        if repeated:
            raise ValueError(f"duplicate notification kind(s) {repeated}; name each one once")
        return self


class AppGrant(_Model):
    """A declared app grant; Discord scopes are the first enforced capabilities."""

    id: str = Field(pattern=SLUG_PATTERN)
    name: str = Field(min_length=1, description="Human label, e.g. Gmail.")
    status: Literal["granted", "pending", "revoked"]
    scopes: list[str] = Field(default_factory=list, description="Scope names, not values.")
    status_ref: str | None = Field(
        default=None,
        description="Where the grant is administered, e.g. a settings URL.",
    )

    @model_validator(mode="after")
    def _only_discord_has_known_scopes(self) -> Self:
        if not self.scopes:
            return self
        if self.id != "discord":
            raise ValueError("scopes are only enforced for id 'discord'; omit them for other apps")
        known = frozenset({"channels.manage", "threads.manage", "messages.pin", "members.read"})
        unknown = sorted(set(self.scopes) - known)
        if unknown:
            raise ValueError(f"unknown Discord scope(s): {', '.join(unknown)}")
        if len(set(self.scopes)) != len(self.scopes):
            raise ValueError("Discord scopes must be unique")
        return self


#: One directory in a ``workspace`` grant. Not stripped, like a tool name and a skill id:
#: the contract is the exact string, and a path that only resolves after a trim is a path
#: somebody typed wrong.
WorkspacePath = Annotated[
    str,
    StringConstraints(strip_whitespace=False, pattern=WORKSPACE_PATH_PATTERN),
]

#: A single tool name inside a declared list. Whitespace is not stripped, for the same
#: reason a skill id is not: the schema's contract is the exact string, and a name that
#: only matches after a trim is a name somebody typed wrong.
ToolName = Annotated[
    str,
    StringConstraints(strip_whitespace=False, pattern=TOOL_NAME_PATTERN),
]


class ToolGrant(RootModel[Literal["unrestricted"] | tuple[ToolName, ...]]):
    """Which tools a session may reach: an exact list of names, or ``unrestricted``.

    The capability dimension that used to be prose. Skills are granted and pruned, app
    access is declared, budgets are capped and enforced — tools were said in a charter
    (*"never send email without explicit approval"*) and a charter is not a boundary. This
    is the declaration steward compiles into the argv that actually removes them.

    Two spellings, and the word is not sugar for an absent key:

    ``tools: unrestricted``
        This resident reaches whatever its brain has. Byte for byte the argv steward built
        before this field existed — the behaviour did not change, it merely became legible.
    ``tools: [Read, Glob, Grep]``
        This resident reaches these and nothing else. An empty list is a real declaration
        too: a session that can think and reply and touch nothing.

    Ask :attr:`bound` rather than reading :attr:`root`, because ``None`` (not bounded) and
    ``()`` (bounded to nothing) are different answers and both of them happen.

    The list branch is a **tuple**, not a list, and that is not a style preference. Every
    other model here is ``frozen``, and over a ``list`` root ``frozen`` half-works: pydantic
    stops attribute assignment while ``grant.root.append("Bash")`` quietly widens a bound
    somebody already validated, and the model is unhashable on one branch and hashable on
    the other. A boundary that can be edited after it was checked is not one.
    """

    model_config = ConfigDict(frozen=True)

    @property
    def unrestricted(self) -> bool:
        """True when this manifest declines to bound the resident's tools."""
        return self.root == UNRESTRICTED_TOOLS

    @property
    def bound(self) -> tuple[ToolName, ...] | None:
        """The exact names a session may reach, or ``None`` when it is not bounded.

        The ``None`` matters: an empty tuple is a resident declared with no tools at all,
        which is a legal and occasionally useful thing to be, and a caller that read it as
        "no bound declared" would hand that resident everything.
        """
        root = self.root
        # The same question :attr:`unrestricted` asks — the string branch of the root union
        # *is* the word — spelled so the type checker follows the narrowing into the return.
        return None if isinstance(root, str) else root

    def describe(self) -> str:
        """One line for a report: the word, or the names in the order they were declared."""
        bound = self.bound
        if bound is None:
            return UNRESTRICTED_TOOLS
        return ", ".join(bound) or "no tools"


#: The permission modes ``claude --permission-mode`` accepts (CLI 2.1.247, ``--help``).
#: This was ``str | None`` and reached the flag unchecked, which made a typo not a failed
#: validation but a session that died at its next fire with a commander error — at 7am,
#: in a log nobody was reading. It is a closed set in the CLI, so it is one here.
PermissionMode = Literal["acceptEdits", "auto", "bypassPermissions", "manual", "dontAsk", "plan"]


class Runner(_Model):
    """Which brain a resident runs on. Every session launch goes through this seam."""

    kind: Literal["claude", "codex", "command", "mock"] = "claude"
    #: Where the session's process runs — an independent axis from ``kind``, which answers
    #: *which brain*. Explicit, never inferred from ``deploy.container`` being present: a
    #: resident can have a container for supervision while its sessions run on the control
    #: plane, which is precisely the state of every resident deployed before steward #58,
    #: and inferring would have silently relocated all of their execution.
    placement: Literal["local", "container"] = Field(
        default="local",
        description="Where a session runs: on the control plane, or inside deploy.container.",
    )
    model: str | None = Field(default=None, description="Model id passed to the CLI.")
    command: list[str] | None = Field(
        default=None,
        description="Argv template for kind=command; placeholders {prompt} and {workdir}.",
    )
    permission_mode: PermissionMode | None = Field(
        default=None,
        description="Permission mode passed to the CLI; one of the modes it accepts.",
    )

    @property
    def container_placed(self) -> bool:
        """True when this resident's sessions run inside its container.

        The one spelling of the question: every branch that must pick a side of the
        memory mount (steward #58) asks this rather than comparing the literal, so
        "which placements exist" has a single home.
        """
        return self.placement == "container"

    @model_validator(mode="after")
    def _check_command_template(self) -> Self:
        placeholders = {
            name for part in (self.command or []) for name in re.findall(r"\{([a-z_]+)\}", part)
        }
        if self.kind == "command":
            if not self.command:
                raise ValueError("runner kind 'command' requires a command template")
            if "prompt" not in placeholders:
                raise ValueError("command template must contain the {prompt} placeholder")
        elif self.command:
            raise ValueError(f"runner kind {self.kind!r} does not take a command template")
        unknown = placeholders - {"prompt", "workdir"}
        if unknown:
            raise ValueError(
                f"unknown placeholder(s) {sorted(unknown)}; only {{prompt}} and {{workdir}}"
            )
        return self


class Board(_Model):
    """Whether this resident takes work off the job board, and on what terms.

    Opting in is one boolean, and it defaults to *off*. A resident that has never heard
    of the board never claims from it, however well its skills happen to match, because
    silence in a manifest is not consent — the whole point of a declaration is that you
    can read what a resident will do before it does it.

    The rest is the terms of the claim, all with honest defaults: one task per wake-up,
    a thirty-minute lease, and a fifteen-minute session. ``lease_s`` should comfortably
    exceed ``timeout_s``; a lease that dies while the session is still running hands the
    same task to somebody else, which is the one thing claiming exists to prevent.
    """

    claim: bool = Field(
        default=False,
        description="Whether this resident may claim open tasks from the board.",
    )
    max_claims_per_wake: int = Field(
        default=1,
        ge=1,
        le=MAX_CLAIMS_PER_WAKE,
        description="How many tasks one wake-up may claim and work.",
    )
    lease_s: int = Field(
        default=DEFAULT_BOARD_LEASE_S,
        gt=0,
        le=MAX_TIMEOUT_S,
        description="How long a claim holds before the task returns to the board.",
    )
    timeout_s: int = Field(
        default=DEFAULT_BOARD_TIMEOUT_S,
        gt=0,
        le=MAX_TIMEOUT_S,
        description="Kill a board session after this long.",
    )

    @model_validator(mode="after")
    def _lease_outlives_the_session(self) -> Self:
        if self.claim and self.lease_s <= self.timeout_s:
            raise ValueError(
                f"lease_s ({self.lease_s}) must outlive timeout_s ({self.timeout_s}); "
                f"a lease that expires mid-session hands the same task to somebody else"
            )
        return self


class Budgets(_Model):
    """What one resident may spend in a day, and how long one run of it may take.

    Every field is optional, and an absent field means *unlimited* — but unlimited is
    said out loud rather than assumed: ``GET /residents/{id}/budget`` reports a limit of
    ``null`` and ``steward budget show`` prints ``no limit``, so "Hob has no cap" is an
    answer somebody read rather than a silence somebody hoped about.

    The two daily caps are counted in the resident's own primary time zone (see
    :mod:`steward.budgets`), because "a day" is a wall-clock fact where the household is.
    ``max_run_seconds`` is not daily at all: it caps a *single* run, and steward enforces
    it as ``min(routine timeout, max_run_seconds)`` so a manifest cannot declare a routine
    that outlives the budget the same manifest declares.
    """

    daily_cost_usd: float | None = Field(
        default=None,
        gt=0,
        description="Money this resident may spend per local day. Absent means unlimited.",
    )
    daily_tokens: int | None = Field(
        default=None,
        gt=0,
        description="Input plus output tokens per local day. Absent means unlimited.",
    )
    max_run_seconds: int | None = Field(
        default=None,
        gt=0,
        le=MAX_TIMEOUT_S,
        description="Hard cap on one run, applied as min(timeout_s, this).",
    )

    @property
    def declared(self) -> bool:
        """True when this manifest actually declares a budget of any kind."""
        return any(
            value is not None
            for value in (self.daily_cost_usd, self.daily_tokens, self.max_run_seconds)
        )


class Mount(_Model):
    """One extra bind mount made available inside a resident container."""

    host: str = Field(
        pattern=MOUNT_HOST_PATH_PATTERN,
        description="Absolute or ~-relative path on the burrow host.",
    )
    container: str = Field(
        pattern=MOUNT_CONTAINER_PATH_PATTERN,
        description="Absolute path inside the container.",
    )
    mode: Literal["rw", "ro"]


class Deploy(_Model):
    """Where this resident runs: the address the nursery ships it to and the watchdog probes.

    Every field is optional and every one has a documented default for the dxp2800 layout
    (see :mod:`steward.deploy`), so an ordinary resident declares nothing here and still
    deploys. What the block is for is the resident that does *not* live where everything
    else lives — a second NAS, a different compose root, a container someone named by hand
    before steward existed.

    An absent ``deploy`` block is still what the watchdog calls **unsupervised**: the
    default container name is what the nursery *would* create, and a resident nobody has
    provisioned has no container under that name to speak for.

    The patterns on ``host``, ``user``, ``path`` and ``image`` are a safety boundary, not
    a style guide — see :data:`HOST_PATTERN`.
    """

    container: str | None = Field(
        default=None,
        pattern=CONTAINER_PATTERN,
        description="Docker container name, e.g. steward-hob. Default: steward-<id>.",
    )
    host: str | None = Field(
        default=None,
        pattern=HOST_PATTERN,
        description="Host the container runs on. Default: dxp2800.",
    )
    user: str | None = Field(
        default=None,
        pattern=SSH_USER_PATTERN,
        description="SSH user steward reaches that host as. Default: Miha.",
    )
    path: str | None = Field(
        default=None,
        pattern=REMOTE_PATH_PATTERN,
        description="Compose directory on the host. Default: ~/docker/warren/residents/<id>.",
    )
    image: str | None = Field(
        default=None,
        pattern=IMAGE_PATTERN,
        description="Container image. Default: steward-resident:latest.",
    )
    command: list[str] = Field(
        default_factory=list,
        description="argv the container runs. Default: ['sleep', 'infinity'].",
    )
    mounts: list[Mount] = Field(
        default_factory=list,
        description="Extra bind mounts made available inside the resident container.",
    )
    tz: str | None = Field(
        default=None,
        description=(
            "IANA zone the container's clock reads in (TZ). Default: the routines' "
            "schedule_tz when they all agree; required when they disagree."
        ),
    )

    @field_validator("tz")
    @classmethod
    def _check_tz(cls, value: str | None) -> str | None:
        if value is None:
            return None
        stripped = value.strip()
        try:
            zoneinfo.ZoneInfo(stripped)
        except zoneinfo.ZoneInfoNotFoundError, ValueError, ModuleNotFoundError:
            raise ValueError(
                f"deploy.tz {stripped!r} is not an IANA time zone; "
                f"the container's clock has to be written down to mean anything"
            ) from None
        return stripped


class Delegation(_Model):
    """Whether this resident may hand work to another resident, and to whom.

    The sending half of steward #7, and it is one boolean that defaults to *off*, for the
    same reason ``board.claim`` does: silence in a declaration is not consent. A resident
    that has never been told it may delegate never delegates, however sensible the handoff
    would have been, because the point of a manifest is that you can read what a resident
    will do before it does it.

    ``to`` is an optional allowlist of resident ids. Empty means "any resident whose own
    manifest declares a route that accepts the work" — the receiver's declaration is
    always the second half of the answer, and neither half can be waived by the other.
    """

    send: bool = Field(
        default=False,
        description="Whether this resident may delegate work to another resident.",
    )
    to: list[str] = Field(
        default_factory=list,
        description="Resident ids this one may delegate to. Empty means any receiver.",
    )
    note: str | None = Field(default=None, description="Why this resident hands work over.")

    @field_validator("to")
    @classmethod
    def _recipients_are_resident_ids(cls, value: list[str]) -> list[str]:
        bad = [entry for entry in value if not re.match(SLUG_PATTERN, entry.strip())]
        if bad:
            raise ValueError(f"recipient(s) {bad} are not resident ids (lowercase, dashes)")
        return [entry.strip() for entry in value]

    def may_send_to(self, resident_id: str) -> bool:
        """Report whether this manifest permits delegating to a named resident."""
        return self.send and (not self.to or resident_id in self.to)


class Routine(_Model):
    """Standing work a resident performs without being prompted."""

    id: str = Field(pattern=SLUG_PATTERN)
    schedule: str = Field(description="Five-field cron expression, read in schedule_tz.")
    schedule_tz: str = Field(
        default=DEFAULT_SCHEDULE_TZ,
        description="IANA zone the schedule is read in, e.g. Europe/Ljubljana.",
    )
    prompt: str = Field(
        min_length=1,
        max_length=ROUTINE_PROMPT_MAX_CHARS,
        description="Prompt template for the session.",
    )
    requires: list[str] = Field(
        default_factory=list,
        description="Skills that must be granted for this routine to run.",
    )
    timeout_s: int = Field(gt=0, le=MAX_TIMEOUT_S, description="Kill the run after this long.")
    enabled: bool = True
    journal: Literal["close_of_day"] | None = Field(
        default=None,
        description="Flag the one routine that ends the resident's day and journals.",
    )
    deliver: RoutineDeliverKind | None = Field(
        default=None,
        description=(
            "Where the final message of a finished run goes. 'chat' selects the sole "
            "active chat route; a <transport>:<reference> value selects that exact route."
        ),
    )
    quiet_word: str | None = Field(
        default=None,
        description=(
            "The one reply that means 'say nothing': a delivered run whose whole output "
            "is this word sends nothing. One short token, matched exactly."
        ),
    )

    @field_validator("quiet_word")
    @classmethod
    def _check_quiet_word(cls, value: str | None, info: ValidationInfo) -> str | None:
        if value is None:
            return None
        if not QUIET_WORD_PATTERN.match(value):
            raise ValueError(
                f"quiet_word must be one short token: no whitespace, at most "
                f"{QUIET_WORD_MAX_CHARS} characters"
            )
        # ``deliver`` is declared above this field, so it has already been read — unless
        # it failed, in which case it is absent from ``data`` and has its own diagnostic,
        # and a second one here would only point away from it.
        if "deliver" in info.data and info.data["deliver"] is None:
            raise ValueError(
                "quiet_word means nothing without deliver: a run nobody hears cannot be quiet"
            )
        return value

    @field_validator("schedule")
    @classmethod
    def _check_cron(cls, value: str) -> str:
        stripped = value.strip()
        if not CRON_PATTERN.match(stripped):
            raise ValueError("schedule must be a five-field cron expression")
        if not croniter.is_valid(stripped):
            raise ValueError("schedule is a five-field cron expression with out-of-range values")
        return stripped

    @field_validator("schedule_tz")
    @classmethod
    def _check_timezone(cls, value: str) -> str:
        stripped = value.strip()
        try:
            zoneinfo.ZoneInfo(stripped)
        except zoneinfo.ZoneInfoNotFoundError, ValueError, ModuleNotFoundError:
            raise ValueError(
                f"schedule_tz {stripped!r} is not an IANA time zone; "
                f"'resident-local time' has to be written down to mean anything"
            ) from None
        return stripped


class ResidentManifest(_Model):
    """The versioned declaration that makes a villager a resident."""

    version: Literal[0] = Field(default=SCHEMA_VERSION, description="Manifest schema version.")
    uid: UUID4 = Field(description="Durable random identity, minted once and never renamed.")
    id: str = Field(pattern=SLUG_PATTERN, description="Directory name under residents/.")
    home: int = Field(
        ge=VILLAGE_HOME_MIN,
        le=VILLAGE_HOME_MAX,
        description="Stable Chronicle village plot, from 0 through 7.",
    )
    agent_id: str | None = Field(
        default=None,
        pattern=AGENT_ID_PATTERN,
        description="Chronicle event join key; resident:<uid> for Steward Residents.",
    )
    project: str | None = Field(
        default=None,
        pattern=PROJECT_PATTERN,
        description="Project label for scoped residents.",
    )
    summary: str | None = Field(
        default=None,
        max_length=SUMMARY_MAX_CHARS,
        description="One line burrow can display.",
    )
    retired: bool = Field(
        default=False,
        description="This resident has been retired. It keeps validating and stops working.",
    )

    soul: SoulIdentity
    charter: Charter
    skills: list[SkillGrantInput] = Field(description="Granted capabilities (may be empty).")
    session_grants: list[SessionGrant] = Field(
        default_factory=list,
        description="Named steward API write doors this resident's sessions may cross.",
    )
    memory: Memory
    routes: list[Route] = Field(description="Declared inbound channels (may be empty).")
    app_grants: list[AppGrant] = Field(description="Declared app access (may be empty).")
    tools: ToolGrant = Field(
        description="Tool dimension: the names a session may reach, or 'unrestricted'.",
    )
    workspace: list[WorkspacePath] = Field(
        default_factory=list,
        description="Absolute directories a session may reach beyond its working directory.",
    )

    runner: Runner = Field(default_factory=Runner)
    routines: list[Routine] = Field(default_factory=list)
    board: Board = Field(
        default_factory=Board,
        description="Job board participation. Absent means this resident never claims.",
    )
    budgets: Budgets = Field(
        default_factory=Budgets,
        description="Daily spend caps and the per-run time cap. Absent means unlimited.",
    )
    delegation: Delegation = Field(
        default_factory=Delegation,
        description="Handing work to other residents. Absent means this one never does.",
    )
    notifications: Notifications = Field(
        default_factory=Notifications,
        description="Outbound taps to a human. Absent means steward taps nobody about this one.",
    )
    deploy: Deploy = Field(
        default_factory=Deploy,
        description="Where this resident runs, for the watchdog. Absent means unsupervised.",
    )

    @model_validator(mode="after")
    def _check_identity(self) -> Self:
        if not self.agent_id and not self.project:
            raise ValueError(
                "a resident needs agent_id (exact identity) or project (project-scoped soul)"
            )
        return self

    @property
    def chronicle_agent_id(self) -> str:
        """The chronicle identity this resident's events are filed under.

        The declared ``agent_id`` when there is one, and a legacy ``steward:<id>`` when
        the resident is project-scoped instead. Spelled once, here, because a resident
        whose events arrive under two identities is two villagers as far as chronicle is
        concerned — and nothing fails loudly when that happens, it just silently splits
        one resident's history in half.
        """
        return self.agent_id or f"steward:{self.id}"

    @property
    def chronicle_project(self) -> str:
        """The chronicle project label: the declared project, else the resident id."""
        return self.project or self.id


@dataclass(frozen=True, slots=True)
class SoulDocument:
    """A parsed ``soul.md``: frontmatter, body, and the optional Voice section."""

    path: Path
    frontmatter: Mapping[str, Any] = field(default_factory=dict)
    body: str = ""
    voice: str | None = None


@dataclass(frozen=True, slots=True)
class Resident:
    """A validated resident: its manifest, its soul document, and where they live."""

    path: Path
    manifest: ResidentManifest
    soul: SoulDocument

    @property
    def directory(self) -> Path:
        """The resident's directory."""
        return self.path.parent

    @property
    def id(self) -> str:
        """The resident id."""
        return self.manifest.id

    @property
    def uid(self) -> str:
        """The resident's durable identity, as the string every wire format carries it as."""
        return str(self.manifest.uid)

    @property
    def agent_id(self) -> str:
        """The burrow identity this resident's events are emitted under."""
        return self.manifest.chronicle_agent_id

    @property
    def project(self) -> str:
        """The burrow project label: the manifest's project, else the resident id."""
        return self.manifest.chronicle_project

    @property
    def retired(self) -> bool:
        """True when this resident's own manifest says it has been retired."""
        return self.manifest.retired

    def route(self, route_id: str) -> Route | None:
        """Return the declared route with this id, or ``None`` if it declares none."""
        return next((route for route in self.manifest.routes if route.id == route_id), None)

    @property
    def delegation_routes(self) -> tuple[str, ...]:
        """The ids of the routes work may be delegated into, in declared order."""
        return tuple(route.id for route in self.manifest.routes if route.accepts_delegation)

    @property
    def inbound_routes(self) -> tuple[Route, ...]:
        """Every declared route of kind ``delegation``, open or shut, in declared order.

        ``delegation_routes`` answers "where may steward deliver today"; this answers "what
        doors does this resident claim to have at all". A report built on the first cannot
        see a route somebody flipped to ``pending`` or ``disabled`` — it shows no route,
        says nothing, and the letters already delivered pile up behind it (#46).
        """
        return tuple(r for r in self.manifest.routes if r.kind == DELEGATION_ROUTE_KIND)

    def workdir(self, fallback: Path) -> Path:
        """Where a session for this resident runs.

        The declared memory directory when it exists — the one place its charter says it
        may write — and otherwise the caller's working directory. Both session types
        (scheduled routines and claimed board tasks) resolve it here, so a resident does
        not land somewhere different depending on why it woke up.
        """
        memory = self.manifest.memory
        if memory.kind == "directory":
            candidate = Path(memory.path).expanduser()
            if candidate.is_dir():
                return candidate
        return fallback


#: What every refusal says about a retired resident, so the reason reads the same whether
#: it came back from the scheduler, the board, a delegation, or the API.
RETIRED_REASON = (
    "resident {id!r} is retired: its manifest declares retired: true, so it takes no "
    "routines, no board tasks, no letters, and no run-now. Set retired: false and commit "
    "to bring it back, and run `steward provision {id}` to put its container back up."
)


def retired_complaint(resident: Resident) -> str | None:
    """Return why this resident may not be given work, or ``None`` when it may."""
    return RETIRED_REASON.format(id=resident.id) if resident.retired else None


def active_residents(residents: Iterable[Resident]) -> list[Resident]:
    """Return the residents that have not been retired, in the order they came in.

    The one filter. Retirement is a lifecycle state rather than a deletion — the manifest
    and the soul stay in git, the resident keeps validating, and ``steward validate``
    still reads it — so *every* path that hands out work has to consult it, and doing that
    in one function is what keeps "retired" from meaning four slightly different things.
    """
    return [resident for resident in residents if not resident.retired]
