"""Resident manifests: the one place a resident's identity and obligations live.

A resident is declared by two files in ``residents/<id>/``:

``manifest.yaml``
    The structured declaration — soul identity, charter, and the five capability
    dimensions burrow renders (skills, memory, routes, app grants) plus the
    steward-side execution blocks (runner, routines).
``soul.md``
    The free-form soul body, markdown with frontmatter, compatible in spirit with
    burrow's ``villagers/*.md``.

Everything here is references and grants. Credentials live outside both repos, and a
manifest that carries credential-shaped keys or inline secrets fails validation rather
than being stored.
"""

import difflib
import posixpath
import re
import zoneinfo
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import StrEnum
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Any, Literal, Self

import yaml
from croniter import croniter
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    ValidationInfo,
    field_validator,
    model_validator,
)

__all__ = [
    "CLOSE_OF_DAY",
    "DEFAULT_BOARD_LEASE_S",
    "DEFAULT_BOARD_TIMEOUT_S",
    "DEFAULT_JOURNAL_DIR",
    "DEFAULT_KEEP_ENTRIES",
    "DELEGATION_ROUTE_KIND",
    "JOB_BOARD_ROUTE_KIND",
    "MANIFEST_FILENAME",
    "SCHEMA_VERSION",
    "VOICE_MAX_CHARS",
    "AppGrant",
    "Board",
    "Budgets",
    "Charter",
    "Delegation",
    "Deploy",
    "Diagnostic",
    "Escalation",
    "ManifestError",
    "Memory",
    "Resident",
    "ResidentManifest",
    "Route",
    "Routine",
    "Runner",
    "Severity",
    "SkillGrant",
    "SoulDocument",
    "SoulIdentity",
    "ValidationResult",
    "active_residents",
    "closest_match",
    "extract_voice",
    "load_manifest",
    "manifest_json_schema",
    "retired_complaint",
    "split_frontmatter",
    "validate_manifest",
    "validate_path",
    "validate_paths",
    "validate_tree",
]

if TYPE_CHECKING:  # pragma: no cover — the library reads this module, not the reverse
    from steward.skills import SkillLibrary

MANIFEST_FILENAME = "manifest.yaml"
DEFAULT_SOUL_FILENAME = "soul.md"
SCHEMA_VERSION = 0
VOICE_MAX_CHARS = 1200
VOICE_HEADING = "## Voice"

#: The journal subdirectory, under ``memory.path``, a manifest gets when it names none.
DEFAULT_JOURNAL_DIR = "journal"

#: Keep the newest N entries. Counted, not aged: there is at most one entry per local
#: day, so 30 is "the last 30 days this resident actually wrote", and one that was quiet
#: for a month still has its last entry to wake up to. An age cut-off would delete it.
DEFAULT_KEEP_ENTRIES = 30
MAX_JOURNAL_KEEP = 3650

#: The one legal value of a routine's ``journal:`` flag: the run that ends the day.
CLOSE_OF_DAY = "close_of_day"

#: Stop counting a closing routine's daily fires here; anything over one is already wrong.
MAX_CLOSER_FIRES = 24

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
IMAGE_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9_.:/@-]*$"

#: ``project`` becomes ``BURROW_PROJECT`` in the resident's compose environment, so like
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


# --------------------------------------------------------------------------------------
# diagnostics
# --------------------------------------------------------------------------------------


class Severity(StrEnum):
    """How badly a diagnostic hurts. Errors fail validation; warnings do not."""

    ERROR = "error"
    WARNING = "warning"


@dataclass(frozen=True, slots=True)
class Diagnostic:
    """One actionable complaint about one field of one file.

    Every diagnostic names the file, the field path, what is wrong, and what a valid
    value looks like — a bad manifest must never silently imply access it does not have.
    """

    file: Path
    field_path: str
    problem: str
    example: str
    severity: Severity = Severity.ERROR

    def render(self) -> str:
        """Return the human-readable, terminal-friendly form of this diagnostic."""
        return (
            f"{self.file}: {self.severity.value}: {self.field_path}\n"
            f"    problem: {self.problem}\n"
            f"    example: {self.example}"
        )

    def __str__(self) -> str:
        """Render the diagnostic for humans."""
        return self.render()


@dataclass(frozen=True, slots=True)
class ValidationResult:
    """The structured outcome of validating one or more manifests."""

    residents: tuple[Resident, ...] = ()
    diagnostics: tuple[Diagnostic, ...] = ()

    @property
    def errors(self) -> tuple[Diagnostic, ...]:
        """Diagnostics that fail validation."""
        return tuple(d for d in self.diagnostics if d.severity is Severity.ERROR)

    @property
    def warnings(self) -> tuple[Diagnostic, ...]:
        """Diagnostics worth printing that do not fail validation."""
        return tuple(d for d in self.diagnostics if d.severity is Severity.WARNING)

    @property
    def ok(self) -> bool:
        """True when nothing failed."""
        return not self.errors

    def merged_with(self, other: ValidationResult) -> ValidationResult:
        """Combine two results, preserving order."""
        return ValidationResult(
            residents=self.residents + other.residents,
            diagnostics=self.diagnostics + other.diagnostics,
        )


def closest_match(value: str, candidates: Iterable[str]) -> str | None:
    """Return the candidate a misspelling most likely meant, or ``None``.

    One near-miss finder, shared by every "you named something that does not exist"
    diagnostic — a typo should be answered with the fix, not with a list to read.
    """
    matches = difflib.get_close_matches(value, sorted(candidates), n=1)
    return matches[0] if matches else None


class ManifestError(Exception):
    """Raised by :func:`load_manifest` when a manifest does not validate."""

    def __init__(self, diagnostics: Sequence[Diagnostic]) -> None:
        """Carry the diagnostics that explain the refusal."""
        self.diagnostics = tuple(diagnostics)
        summary = "; ".join(f"{d.field_path}: {d.problem}" for d in self.diagnostics[:3])
        super().__init__(summary or "manifest is invalid")


# --------------------------------------------------------------------------------------
# credential rejection
# --------------------------------------------------------------------------------------

CREDENTIAL_KEY_PATTERN = re.compile(
    r"(?:^|[_.\- ])(?:"
    r"token|tokens|secret|secrets|password|passwd|passphrase|"
    r"api[_-]?key|apikey|access[_-]?key|private[_-]?key|signing[_-]?key|session[_-]?key|"
    r"client[_-]?secret|credential|credentials|bearer|authorization|cookie|"
    r"refresh[_-]?token|access[_-]?token|auth[_-]?token"
    r")(?:$|[_.\- ])",
    re.IGNORECASE,
)

SECRET_VALUE_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"), "an inline private key"),
    (re.compile(r"\bsk-[A-Za-z0-9_-]{16,}"), "an inline API key"),
    (re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}"), "an inline GitHub token"),
    (re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}"), "an inline GitHub token"),
    (re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}"), "an inline Slack token"),
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), "an inline AWS access key id"),
    (re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b"), "an inline Google API key"),
    (
        re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}"),
        "an inline JWT",
    ),
    (re.compile(r"://[^/\s:@]+:[^/\s@]{3,}@"), "an inline password in a URL"),
)

# A reference field holds a pointer (path, URL, scheme-prefixed handle). A long run of
# random-looking characters with no separators is a value pretending to be a reference.
_BLOB_CHARSET = re.compile(r"[A-Za-z0-9+_=-]{32,}")
_HEX_DIGEST = re.compile(r"[0-9a-fA-F]{32,}")


def _looks_like_opaque_blob(value: str) -> bool:
    """Report whether a reference-shaped field holds something that looks like a secret."""
    if not _BLOB_CHARSET.fullmatch(value):
        return False
    if _HEX_DIGEST.fullmatch(value):
        return True
    return (
        any(char.isdigit() for char in value)
        and any(char.isupper() for char in value)
        and any(char.islower() for char in value)
    )


REFERENCE_FIELDS = frozenset({"memory.path", "routes.address", "app_grants.status_ref"})

#: The exact field paths whose *names* look credential-shaped and are not. There is one,
#: and it earns its place: ``budgets.daily_tokens`` is how many tokens a resident may
#: spend in a day — an integer, and the whole point of the field is that a person reads it
#: next to ``daily_cost_usd``. The alternative was to name it something the scanner does
#: not recognise, which would have meant letting a regex choose the vocabulary of the
#: manifest. An exemption is a path, never a prefix and never a pattern, so nothing new
#: slips through by being spelled cleverly.
CREDENTIAL_NAME_EXEMPT = frozenset({"budgets.daily_tokens"})

CREDENTIAL_EXAMPLE = (
    "drop the field entirely — credentials live outside this repo; "
    "declare access instead: app_grants: [{id: gmail, name: Gmail, status: granted}]"
)


def _walk(node: object, path: str = "") -> Iterator[tuple[str, object]]:
    """Yield ``(dotted_path, value)`` for every node of a parsed YAML tree."""
    yield path, node
    if isinstance(node, Mapping):
        for key, value in node.items():
            child = f"{path}.{key}" if path else str(key)
            yield from _walk(value, child)
    elif isinstance(node, (list, tuple)):
        for index, value in enumerate(node):
            yield from _walk(value, f"{path}[{index}]")


def _strip_indices(path: str) -> str:
    return re.sub(r"\[\d+\]", "", path)


def scan_for_credentials(data: object, source: Path) -> list[Diagnostic]:
    """Reject credential-shaped keys and inline secrets anywhere in a parsed tree.

    Runs before schema validation so a secret is never bound into a model, let alone
    written back out.
    """
    diagnostics: list[Diagnostic] = []
    for path, value in _walk(data):
        if not path:
            continue
        normalized = _strip_indices(path)
        leaf = normalized.rsplit(".", 1)[-1]
        if normalized not in CREDENTIAL_NAME_EXEMPT and CREDENTIAL_KEY_PATTERN.search(leaf):
            diagnostics.append(
                Diagnostic(
                    file=source,
                    field_path=path,
                    problem=(
                        f"field name {leaf!r} is credential-shaped; manifests carry "
                        f"references and grants, never credentials"
                    ),
                    example=CREDENTIAL_EXAMPLE,
                )
            )
            continue
        if not isinstance(value, str):
            continue
        for pattern, description in SECRET_VALUE_PATTERNS:
            if pattern.search(value):
                diagnostics.append(
                    Diagnostic(
                        file=source,
                        field_path=path,
                        problem=f"value looks like {description}",
                        example=CREDENTIAL_EXAMPLE,
                    )
                )
                break
        else:
            if normalized in REFERENCE_FIELDS and _looks_like_opaque_blob(value):
                diagnostics.append(
                    Diagnostic(
                        file=source,
                        field_path=path,
                        problem=(
                            "value is an opaque blob where a reference is expected; "
                            "this field points at a location, it does not hold a value"
                        ),
                        example="/data/residents/life-agent/memory  (or https://…, op://…)",
                    )
                )
    return diagnostics


def scan_text_for_secrets(text: str, source: Path, field_path: str) -> list[Diagnostic]:
    """Reject inline secrets in a free-form document (soul body, skill file)."""
    diagnostics: list[Diagnostic] = []
    for pattern, description in SECRET_VALUE_PATTERNS:
        if pattern.search(text):
            diagnostics.append(
                Diagnostic(
                    file=source,
                    field_path=field_path,
                    problem=f"document contains {description}",
                    example=CREDENTIAL_EXAMPLE,
                )
            )
    return diagnostics


# --------------------------------------------------------------------------------------
# schema
# --------------------------------------------------------------------------------------


class _Model(BaseModel):
    """Base for every manifest model: unknown keys are an error, values are frozen."""

    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)


class SoulIdentity(_Model):
    """The villager burrow draws. Field names match burrow's soul frontmatter."""

    name: str = Field(min_length=1, description="Display name, e.g. Hob.")
    char: str = Field(min_length=1, description="Burrow sprite key, e.g. Monk.")
    accent: str = Field(pattern=ACCENT_PATTERN, description="Hex accent colour, #rrggbb.")
    role: str = Field(min_length=1, description="One-line role, e.g. life bot.")
    file: str = Field(
        default=DEFAULT_SOUL_FILENAME,
        description="Soul body, relative to the manifest directory.",
    )


class Escalation(_Model):
    """Structured escalation policy: when to stop and ask instead of acting."""

    when: list[str] = Field(min_length=1, description="Situations that must be escalated.")
    how: str = Field(
        default="needs_human",
        min_length=1,
        description="The protocol event or channel used to escalate.",
    )
    note: str | None = Field(default=None, description="Extra guidance for the human.")


class Charter(_Model):
    """What a resident is for. Injected into every headless session for this resident."""

    mission: str = Field(min_length=1, description="One paragraph of purpose.")
    duties: list[str] = Field(min_length=1, description="Standing responsibilities.")
    rules: list[str] = Field(min_length=1, description="Hard constraints.")
    escalation: str | Escalation = Field(description="When and how to raise needs_human.")

    @field_validator("duties", "rules")
    @classmethod
    def _no_blank_entries(cls, value: list[str]) -> list[str]:
        if any(not entry.strip() for entry in value):
            raise ValueError("entries must not be empty")
        return value


class SkillGrant(_Model):
    """A named, reusable capability granted to a resident.

    The id names a skill in the library (``skills/<id>/SKILL.md``), and a name the
    library does not have fails validation with the closest match named. The default
    skills every resident holds are not listed here: a grant is what this resident has
    *on top* of them.
    """

    id: str = Field(pattern=SLUG_PATTERN, description="Skill name in the library.")
    source: Literal["library", "local"] = Field(
        default="library",
        description="Where the skill body comes from.",
    )
    note: str | None = Field(default=None, description="Why this resident holds it.")

    @model_validator(mode="before")
    @classmethod
    def _accept_bare_string(cls, value: object) -> object:
        if isinstance(value, str):
            return {"id": value}
        return value


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

    @property
    def accepts_delegation(self) -> bool:
        """True when steward may deliver another resident's work into this route.

        Both halves are required and neither is inferred: the kind says this channel is
        *for* delegated work, and ``active`` says it is open today. A route somebody is
        still wiring up takes no letters.
        """
        return self.kind == DELEGATION_ROUTE_KIND and self.status == "active"


class AppGrant(_Model):
    """A declared grant to use an external application. Identifier and status only."""

    id: str = Field(pattern=SLUG_PATTERN)
    name: str = Field(min_length=1, description="Human label, e.g. Gmail.")
    status: Literal["granted", "pending", "revoked"]
    scopes: list[str] = Field(default_factory=list, description="Scope names, not values.")
    status_ref: str | None = Field(
        default=None,
        description="Where the grant is administered, e.g. a settings URL.",
    )


class Runner(_Model):
    """Which brain a resident runs on. Every session launch goes through this seam."""

    kind: Literal["claude", "codex", "command", "mock"] = "claude"
    model: str | None = Field(default=None, description="Model id passed to the CLI.")
    command: list[str] | None = Field(
        default=None,
        description="Argv template for kind=command; placeholders {prompt} and {workdir}.",
    )
    permission_mode: str | None = None

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
        description="Docker container name, e.g. steward-life-agent. Default: steward-<id>.",
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
        description="Compose directory on the host. Default: ~/docker/<container>.",
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
    prompt: str = Field(min_length=1, description="Prompt template for the session.")
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
    id: str = Field(pattern=SLUG_PATTERN, description="Directory name under residents/.")
    agent_id: str | None = Field(
        default=None,
        pattern=AGENT_ID_PATTERN,
        description="Stable burrow identity, <source>:<name>. Matched before project.",
    )
    project: str | None = Field(
        default=None,
        pattern=PROJECT_PATTERN,
        description="Project label for scoped residents.",
    )
    summary: str | None = Field(default=None, description="One line burrow can display.")
    retired: bool = Field(
        default=False,
        description="This resident has been retired. It keeps validating and stops working.",
    )

    soul: SoulIdentity
    charter: Charter
    skills: list[SkillGrant] = Field(description="Granted capabilities (may be empty).")
    memory: Memory
    routes: list[Route] = Field(description="Declared inbound channels (may be empty).")
    app_grants: list[AppGrant] = Field(description="Declared app access (may be empty).")

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


# --------------------------------------------------------------------------------------
# soul document
# --------------------------------------------------------------------------------------

_FRONTMATTER_RE = re.compile(
    r"\A---\r?\n(?P<frontmatter>.*?)\r?\n---\r?\n?(?P<body>.*)\Z", re.DOTALL
)


def split_frontmatter(text: str) -> tuple[str | None, str]:
    """Split a markdown document into its raw ``---`` frontmatter block and its body.

    One splitter for every document in this repo that carries frontmatter — souls here,
    skills in :mod:`steward.skills` — so "what counts as frontmatter" has one answer.
    A document without a block returns ``(None, text)``.
    """
    match = _FRONTMATTER_RE.match(text)
    if match is None:
        return None, text
    return match.group("frontmatter"), match.group("body")


@dataclass(frozen=True, slots=True)
class SoulDocument:
    """A parsed ``soul.md``: frontmatter, body, and the optional Voice section."""

    path: Path
    frontmatter: Mapping[str, Any] = field(default_factory=dict)
    body: str = ""
    voice: str | None = None


def extract_voice(body: str) -> str | None:
    """Return the text of the ``## Voice`` section, if the soul has one.

    The one definition of what a voice is: the validator caps it here, and
    :mod:`steward.prompt` injects exactly what this returns.
    """
    lines = body.splitlines()
    for index, line in enumerate(lines):
        if line.strip().lower() == VOICE_HEADING.lower():
            collected: list[str] = []
            for following in lines[index + 1 :]:
                if following.startswith("## "):
                    break
                collected.append(following)
            return "\n".join(collected).strip()
    return None


def parse_soul(text: str, source: Path) -> tuple[SoulDocument, list[Diagnostic]]:
    """Parse a soul document, returning it alongside any diagnostics."""
    raw_frontmatter, raw_body = split_frontmatter(text)
    if raw_frontmatter is None:
        return SoulDocument(path=source, body=text), [
            Diagnostic(
                file=source,
                field_path="frontmatter",
                problem="soul file has no --- frontmatter block",
                example="---\nname: Hob\nchar: Monk\naccent: '#a68a4f'\nrole: life bot\n---",
            )
        ]

    diagnostics: list[Diagnostic] = []
    try:
        loaded = yaml.safe_load(raw_frontmatter) or {}
    except yaml.YAMLError as exc:
        return SoulDocument(path=source, body=raw_body), [
            Diagnostic(
                file=source,
                field_path="frontmatter",
                problem=f"frontmatter is not valid YAML: {exc}",
                example="name: Hob",
            )
        ]

    if not isinstance(loaded, Mapping):
        return SoulDocument(path=source, body=raw_body), [
            Diagnostic(
                file=source,
                field_path="frontmatter",
                problem="frontmatter must be a mapping of keys to values",
                example="name: Hob",
            )
        ]

    body = raw_body
    voice = extract_voice(body)
    if voice is not None and len(voice) > VOICE_MAX_CHARS:
        diagnostics.append(
            Diagnostic(
                file=source,
                field_path=VOICE_HEADING,
                problem=(
                    f"voice section is {len(voice)} characters; the cap is {VOICE_MAX_CHARS} "
                    f"so it stays cheap to inject into every session"
                ),
                example=f"a voice section of at most {VOICE_MAX_CHARS} characters",
            )
        )
    diagnostics.extend(scan_text_for_secrets(text, source, "body"))
    return SoulDocument(path=source, frontmatter=dict(loaded), body=body, voice=voice), diagnostics


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
    def agent_id(self) -> str:
        """The burrow identity this resident's events are emitted under."""
        return self.manifest.agent_id or f"steward:{self.manifest.id}"

    @property
    def project(self) -> str:
        """The burrow project label: the manifest's project, else the resident id."""
        return self.manifest.project or self.manifest.id

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
    "to bring it back, and run `steward new-resident` to put its container back up."
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


# --------------------------------------------------------------------------------------
# diagnostics from pydantic
# --------------------------------------------------------------------------------------

FIELD_EXAMPLES: Mapping[str, str] = {
    "version": "version: 0",
    "id": "id: life-agent  (lowercase, dashes)",
    "agent_id": "agent_id: claude-code:life-agent",
    "project": "project: burrow",
    "summary": "summary: Keeps the household running.",
    "soul": "soul: {name: Hob, char: Monk, accent: '#a68a4f', role: life bot}",
    "soul.name": "name: Hob",
    "soul.char": "char: Monk",
    "soul.accent": "accent: '#a68a4f'",
    "soul.role": "role: life bot",
    "soul.file": "file: soul.md",
    "charter": (
        "charter: {mission: …, duties: [...], rules: [...], escalation: raise needs_human}"
    ),
    "charter.mission": "mission: Keep the household running day to day.",
    "charter.duties": "duties: ['Post a daily summary each morning']",
    "charter.rules": "rules: ['Never send email without explicit approval']",
    "charter.escalation": (
        "escalation: Raise needs_human before any irreversible action.  "
        "(or: {when: [...], how: needs_human})"
    ),
    "charter.escalation.when": "when: ['A message needs a reply I was not told to send']",
    "charter.escalation.how": "how: needs_human",
    "skills": "skills: [read-inbox, read-calendar]",
    "skills.id": "id: read-inbox  (a name in skills/)",
    "skills.source": "source: library",
    "memory": (
        "memory: {kind: directory, path: /data/residents/life-agent/memory, journal: journal}"
    ),
    "memory.kind": "kind: directory",
    "memory.path": "path: /data/residents/life-agent/memory",
    "memory.journal": "journal: journal  (a directory under path; one file per local day)",
    "memory.journal_keep": "journal_keep: 30  (entries kept, newest first)",
    "routes": "routes: [{id: cron, kind: cron, address: steward-scheduler, status: active}]",
    "routes.id": "id: inbox",
    "routes.kind": "kind: email  (delegation makes the route deliverable)",
    "routes.address": "address: mailbox:household  (a reference, not a credential)",
    "routes.status": "status: active",
    "app_grants": "app_grants: [{id: gmail, name: Gmail, status: granted}]",
    "app_grants.id": "id: gmail",
    "app_grants.name": "name: Gmail",
    "app_grants.status": "status: granted  (granted | pending | revoked)",
    "app_grants.scopes": "scopes: [gmail.readonly]",
    "app_grants.status_ref": "status_ref: https://myaccount.google.com/permissions",
    "runner": "runner: {kind: claude, model: claude-opus-5}",
    "runner.kind": "kind: claude  (claude | codex | command | mock)",
    "runner.model": "model: claude-opus-5",
    "runner.command": "command: ['my-agent', '--prompt', '{prompt}', '--cwd', '{workdir}']",
    "routines": (
        "routines: [{id: daily-summary, schedule: '0 7 * * *', prompt: …, timeout_s: 900}]"
    ),
    "routines.id": "id: daily-summary",
    "routines.schedule": "schedule: '0 7 * * *'",
    "routines.schedule_tz": "schedule_tz: Europe/Ljubljana  (IANA name; defaults to UTC)",
    "routines.prompt": "prompt: Write today's household summary.",
    "routines.requires": "requires: [read-inbox]  (a default skill, or one granted above)",
    "routines.timeout_s": "timeout_s: 900",
    "routines.enabled": "enabled: true",
    "routines.journal": "journal: close_of_day  (on exactly one routine, or omit it)",
    "delegation": "delegation: {send: true, to: [life-agent]}",
    "delegation.send": "send: true  (omit the block entirely to never delegate)",
    "delegation.to": "to: [life-agent]  (resident ids; omit it to allow any receiver)",
    "delegation.note": "note: Maren hands household errands to Hob.",
    "board": "board: {claim: true, max_claims_per_wake: 1, lease_s: 1800, timeout_s: 900}",
    "board.claim": "claim: true  (omit the block entirely to never claim)",
    "board.max_claims_per_wake": "max_claims_per_wake: 1",
    "board.lease_s": "lease_s: 1800  (must outlive timeout_s)",
    "board.timeout_s": "timeout_s: 900",
    "budgets": "budgets: {daily_cost_usd: 5.0, daily_tokens: 2000000, max_run_seconds: 900}",
    "budgets.daily_cost_usd": "daily_cost_usd: 5.0  (omit the field for no cap)",
    "budgets.daily_tokens": "daily_tokens: 2000000  (input + output, per local day)",
    "budgets.max_run_seconds": "max_run_seconds: 900  (one run, not a day)",
    "deploy": "deploy: {host: dxp2800, user: Miha, container: steward-life-agent}",
    "deploy.container": "container: steward-life-agent  (the docker name, or omit the block)",
    "deploy.host": "host: dxp2800  (a hostname, no spaces: it reaches a remote shell)",
    "deploy.user": "user: Miha  (the ssh user on that host)",
    "deploy.path": "path: ~/docker/steward-life-agent  (the compose directory on the host)",
    "deploy.image": "image: steward-resident:latest",
    "deploy.command": "command: [sleep, infinity]",
    "retired": "retired: false  (true retires the resident; the files stay in git)",
}

_UNION_TAGS = frozenset({"str", "int", "bool", "list[str]", "Escalation", "function-after[_"})


def _normalize_loc(loc: Sequence[object]) -> str:
    """Turn a pydantic error location into a dotted, index-free field path."""
    parts = [
        str(part)
        for part in loc
        if isinstance(part, str)
        and part not in _UNION_TAGS
        and not part.startswith(("function-", "constrained-"))
    ]
    return ".".join(parts)


def _render_loc(loc: Sequence[object]) -> str:
    """Turn a pydantic error location into a dotted path that keeps list indices."""
    rendered = ""
    for part in loc:
        if isinstance(part, int):
            rendered += f"[{part}]"
        elif part in _UNION_TAGS or str(part).startswith(("function-", "constrained-")):
            continue
        else:
            rendered += f".{part}" if rendered else str(part)
    return rendered or "<root>"


def _example_for(loc: Sequence[object]) -> str:
    normalized = _normalize_loc(loc)
    while normalized:
        if normalized in FIELD_EXAMPLES:
            return FIELD_EXAMPLES[normalized]
        normalized = normalized.rsplit(".", 1)[0] if "." in normalized else ""
    return "see docs/manifest.md for the field reference"


def _diagnostics_from_validation_error(error: ValidationError, source: Path) -> list[Diagnostic]:
    seen: set[tuple[str, str]] = set()
    diagnostics: list[Diagnostic] = []
    for raw in error.errors():
        loc = raw["loc"]
        path = _render_loc(loc)
        problem = raw["msg"]
        if raw["type"] == "missing":
            problem = "required field is missing"
        key = (path, problem)
        if key in seen:
            continue
        seen.add(key)
        diagnostics.append(
            Diagnostic(file=source, field_path=path, problem=problem, example=_example_for(loc))
        )
    return diagnostics


# --------------------------------------------------------------------------------------
# cross-field checks
# --------------------------------------------------------------------------------------


def _check_duplicate_ids(manifest: ResidentManifest, source: Path) -> list[Diagnostic]:
    diagnostics: list[Diagnostic] = []
    groups: Mapping[str, Sequence[Any]] = {
        "skills": manifest.skills,
        "routes": manifest.routes,
        "app_grants": manifest.app_grants,
        "routines": manifest.routines,
    }
    for name, entries in groups.items():
        seen: set[str] = set()
        for index, entry in enumerate(entries):
            if entry.id in seen:
                diagnostics.append(
                    Diagnostic(
                        file=source,
                        field_path=f"{name}[{index}].id",
                        problem=f"duplicate id {entry.id!r} in {name}",
                        example=f"one entry per id in {name}",
                    )
                )
            seen.add(entry.id)
    return diagnostics


def _check_routine_requirements(
    manifest: ResidentManifest, source: Path, effective: Sequence[str]
) -> list[Diagnostic]:
    """Check every ``requires`` against the resident's *effective* skills.

    Effective, not granted: the default skills every resident holds are part of the set
    a routine may require, so a manifest does not have to re-grant ``write-journal`` to
    be allowed to close its day with it.
    """
    available = set(effective)
    diagnostics: list[Diagnostic] = []
    for index, routine in enumerate(manifest.routines):
        for position, required in enumerate(routine.requires):
            if required in available:
                continue
            close = closest_match(required, available)
            hint = f"skills: [{close}]" if close else "grant it under skills: first"
            diagnostics.append(
                Diagnostic(
                    file=source,
                    field_path=f"routines[{index}].requires[{position}]",
                    problem=(
                        f"routine {routine.id!r} requires skill {required!r}, "
                        f"which this manifest does not grant"
                    ),
                    example=hint,
                )
            )
    return diagnostics


def _fires_per_day(routine: Routine) -> int:
    """Count a routine's occurrences in one ordinary local day, capped so it terminates."""
    zone = zoneinfo.ZoneInfo(routine.schedule_tz)
    start = datetime(2026, 6, 15, 0, 0, tzinfo=zone)  # mid-year: no DST seam either side
    end = start + timedelta(days=1)
    cursor = croniter(routine.schedule, start - timedelta(seconds=1))
    fires = 0
    while fires <= MAX_CLOSER_FIRES:
        if cursor.get_next(datetime) >= end:
            break
        fires += 1
    return fires


def _check_close_of_day(manifest: ResidentManifest, source: Path) -> list[Diagnostic]:
    """One entry per day means one closing routine, firing once.

    Both halves are checked here rather than left to discover themselves at midnight: a
    second closer would write a second entry over the first, and a closer on an hourly
    schedule would rewrite the day twenty-four times and call the last one the day.
    """
    closers = [
        (index, routine)
        for index, routine in enumerate(manifest.routines)
        if routine.journal == CLOSE_OF_DAY
    ]
    diagnostics = [
        Diagnostic(
            file=source,
            field_path=f"routines[{index}].journal",
            problem=(
                f"routine {routine.id!r} also closes the day; "
                f"{', '.join(repr(r.id) for _, r in closers)} all claim it, and a day "
                f"that ends more than once is not a day"
            ),
            example=f"journal: {CLOSE_OF_DAY} on exactly one routine",
        )
        for index, routine in closers[1:]
    ]
    for index, routine in closers:
        fires = _fires_per_day(routine)
        if fires == 1:
            continue
        many = f"{MAX_CLOSER_FIRES}+" if fires > MAX_CLOSER_FIRES else str(fires)
        diagnostics.append(
            Diagnostic(
                file=source,
                field_path=f"routines[{index}].schedule",
                problem=(
                    f"routine {routine.id!r} closes the day but fires {many} times a day "
                    f"in {routine.schedule_tz}; the journal is one entry per day, so the "
                    f"closing routine has to run once"
                ),
                example="schedule: '30 22 * * *'  (once, late)",
            )
        )
    return diagnostics


def _check_board_route(manifest: ResidentManifest, source: Path) -> list[Diagnostic]:
    """Require a declared job-board route from any resident that claims board work.

    ``routes`` is already this manifest's answer to "how does work reach this resident",
    and the board is a way work reaches it. Letting ``board.claim: true`` stand on its
    own would let a resident pull real work through a channel its own declaration never
    mentions — and burrow, which renders routes, would show a villager with no way in.
    The route must be ``active`` too: a channel somebody is still wiring up is not one
    tasks should be arriving through tonight.
    """
    if not manifest.board.claim:
        return []
    routes = [route for route in manifest.routes if route.kind == JOB_BOARD_ROUTE_KIND]
    example = (
        f"routes: [{{id: job-board, kind: {JOB_BOARD_ROUTE_KIND}, "
        f"address: steward:job-board, status: active}}]"
    )
    if not routes:
        return [
            Diagnostic(
                file=source,
                field_path="board.claim",
                problem=(
                    f"board.claim is true but no route of kind {JOB_BOARD_ROUTE_KIND!r} is "
                    f"declared; a resident cannot pull work through a channel its own "
                    f"manifest does not mention"
                ),
                example=example,
            )
        ]
    if any(route.status == "active" for route in routes):
        return []
    statuses = ", ".join(sorted({route.status for route in routes}))
    return [
        Diagnostic(
            file=source,
            field_path="board.claim",
            problem=(
                f"board.claim is true but every {JOB_BOARD_ROUTE_KIND!r} route is {statuses}; "
                f"claiming real work through a channel that is not open yet is a lie the "
                f"village would render"
            ),
            example=example,
        )
    ]


def _check_budget_runtime(manifest: ResidentManifest, source: Path) -> list[Diagnostic]:
    """Warn when a declared timeout is longer than the budget that will cut it short.

    A warning rather than an error, because the manifest is not *wrong*: steward enforces
    ``min(timeout_s, max_run_seconds)`` and the run really does get killed at the budget.
    But a routine declaring a fifteen-minute timeout under a five-minute budget will never
    once get fifteen minutes, and reading the two numbers side by side is the only moment
    anybody is going to notice that. Silence here would make the manifest a document that
    disagrees with itself.
    """
    cap = manifest.budgets.max_run_seconds
    if cap is None:
        return []
    example = f"timeout_s: {cap}  (at most budgets.max_run_seconds)"
    diagnostics = [
        Diagnostic(
            file=source,
            field_path=f"routines[{index}].timeout_s",
            problem=(
                f"routine {routine.id!r} declares timeout_s {routine.timeout_s} but "
                f"budgets.max_run_seconds is {cap}; steward runs it for {cap}s and the "
                f"declared timeout is never reached"
            ),
            example=example,
            severity=Severity.WARNING,
        )
        for index, routine in enumerate(manifest.routines)
        if routine.timeout_s > cap
    ]
    if manifest.board.claim and manifest.board.timeout_s > cap:
        diagnostics.append(
            Diagnostic(
                file=source,
                field_path="board.timeout_s",
                problem=(
                    f"board sessions declare timeout_s {manifest.board.timeout_s} but "
                    f"budgets.max_run_seconds is {cap}; a claimed task is killed at {cap}s"
                ),
                example=example,
                severity=Severity.WARNING,
            )
        )
    return diagnostics


def _check_delegation(manifest: ResidentManifest, source: Path) -> list[Diagnostic]:
    """Check that the delegation block says something, and does not say it about itself.

    Two ways to write a block that reads like a grant and is not one, both caught here
    rather than at the moment a session tries to hand work over and is refused:

    - **An allowlist with the switch off.** ``to:`` names recipients while ``send`` is
      false. Nothing is permitted, and a reader would swear otherwise.
    - **Naming yourself.** A resident cannot delegate to itself: that is not a handoff,
      it is the same session pretending to be two, and steward rejects it at enqueue. A
      manifest that declares it is declaring something that can never happen.
    """
    delegation = manifest.delegation
    diagnostics: list[Diagnostic] = []
    if delegation.to and not delegation.send:
        diagnostics.append(
            Diagnostic(
                file=source,
                field_path="delegation.send",
                problem=(
                    f"delegation.to names {sorted(delegation.to)} but delegation.send is "
                    f"false, so this resident may not delegate to anybody; an allowlist "
                    f"that grants nothing reads like a grant"
                ),
                example="delegation: {send: true, to: [life-agent]}",
            )
        )
    if manifest.id in delegation.to:
        diagnostics.append(
            Diagnostic(
                file=source,
                field_path="delegation.to",
                problem=(
                    f"{manifest.id!r} lists itself as a recipient; a resident handing work "
                    f"to itself is one session pretending to be two, and steward rejects it"
                ),
                example="to: [life-agent]  (somebody else)",
            )
        )
    return diagnostics


def _check_soul_agreement(
    manifest: ResidentManifest, soul: SoulDocument, source: Path
) -> list[Diagnostic]:
    """Check the soul frontmatter against the manifest, which is the source of truth."""
    diagnostics: list[Diagnostic] = []
    frontmatter = soul.frontmatter
    identity = {
        "name": manifest.soul.name,
        "char": manifest.soul.char,
        "accent": manifest.soul.accent,
        "role": manifest.soul.role,
        "agent_id": manifest.agent_id,
        "project": manifest.project,
    }
    for key, expected in identity.items():
        if key not in frontmatter:
            continue
        actual = str(frontmatter[key]).strip()
        if expected is None or actual.lower() != str(expected).lower():
            diagnostics.append(
                Diagnostic(
                    file=soul.path,
                    field_path=f"frontmatter.{key}",
                    problem=(
                        f"soul frontmatter says {actual!r} but {source.name} says "
                        f"{expected!r}; the manifest is the source of truth"
                    ),
                    example=f"{key}: {expected}" if expected is not None else f"remove {key}",
                )
            )
    return diagnostics


def _resolved_journal_dir(memory: Memory) -> str:
    """Return where a resident's entries land: ``memory.path`` joined with ``memory.journal``.

    Normalised so ``/a/b`` and ``/a//b/`` are one place, which is what "the same journal
    directory" has to mean for two manifests to be caught sharing one.
    """
    return posixpath.normpath(str(PurePosixPath(memory.path) / memory.journal))


def _check_shared_journal_dirs(residents: Sequence[Resident]) -> list[Diagnostic]:
    """Warn when two residents resolve to one journal directory (#77, the manifest side).

    A shared ``<memory.path>/<memory.journal>`` means one resident's close-of-day entry
    lands where another reads its own from, so the two cross-feed: alice would wake up to
    bob's private note. It is a **warning**, not an error — a tree can be arranged this way
    on purpose in a test or a migration — but it is never what a production village wants,
    and validation is the one moment anybody reads the two paths side by side. The
    read-time filtering that keeps entries apart even so lives in :mod:`steward.journal`.
    """
    by_dir: dict[str, list[Resident]] = {}
    for resident in residents:
        memory = resident.manifest.memory
        if memory.kind != "directory":
            continue
        by_dir.setdefault(_resolved_journal_dir(memory), []).append(resident)
    diagnostics: list[Diagnostic] = []
    for journal_dir, group in by_dir.items():
        if len(group) <= 1:
            continue
        ids = sorted(resident.id for resident in group)
        for resident in group:
            others = [rid for rid in ids if rid != resident.id]
            diagnostics.append(
                Diagnostic(
                    file=resident.path,
                    field_path="memory.path",
                    problem=(
                        f"resident {resident.id!r} resolves its journal directory to "
                        f"{journal_dir}, which {others} also use; a shared journal lets one "
                        f"resident read another's entries"
                    ),
                    example="give each resident its own memory.path",
                    severity=Severity.WARNING,
                )
            )
    return diagnostics


def _check_directory_name(manifest: ResidentManifest, source: Path) -> list[Diagnostic]:
    directory = source.parent.name
    if directory and manifest.id != directory:
        return [
            Diagnostic(
                file=source,
                field_path="id",
                problem=f"id {manifest.id!r} does not match directory {directory!r}",
                example=f"id: {directory}",
            )
        ]
    return []


# --------------------------------------------------------------------------------------
# loading
# --------------------------------------------------------------------------------------


def _read_yaml(path: Path) -> tuple[object, list[Diagnostic]]:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        return None, [
            Diagnostic(
                file=path,
                field_path="<file>",
                problem=f"cannot read manifest: {exc.strerror or exc}",
                example=f"a readable {MANIFEST_FILENAME}",
            )
        ]
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        return None, [
            Diagnostic(
                file=path,
                field_path="<file>",
                problem=f"manifest is not valid YAML: {exc}",
                example="version: 0\nid: life-agent\n…",
            )
        ]
    if data is None:
        return None, [
            Diagnostic(
                file=path,
                field_path="<file>",
                problem="manifest is empty",
                example="version: 0\nid: life-agent\n…",
            )
        ]
    if not isinstance(data, Mapping):
        return None, [
            Diagnostic(
                file=path,
                field_path="<root>",
                problem="manifest must be a mapping of fields at the top level",
                example="version: 0\nid: life-agent\n…",
            )
        ]
    return data, []


def _load_soul(manifest: ResidentManifest, source: Path) -> tuple[SoulDocument, list[Diagnostic]]:
    soul_path = source.parent / manifest.soul.file
    if not soul_path.is_file():
        return SoulDocument(path=soul_path), [
            Diagnostic(
                file=source,
                field_path="soul.file",
                problem=f"soul file {manifest.soul.file!r} does not exist next to the manifest",
                example=f"create {soul_path.name} with frontmatter and a short body",
            )
        ]
    return parse_soul(soul_path.read_text(encoding="utf-8"), soul_path)


def _library(residents_dir: Path, skills_dir: Path | str | None) -> SkillLibrary:
    """Load the skills library a residents tree is validated against.

    Imported here rather than at module scope because the dependency runs the other
    way: :mod:`steward.skills` is built out of this module's diagnostics and models,
    and this module only reaches for the library when somebody actually validates.
    ``skills_dir=None`` means "find the one beside the tree, if there is one", which is
    what keeps callers from before the library existed working unchanged.
    """
    from steward.skills import library_for  # noqa: PLC0415

    return library_for(residents_dir, skills_dir)


def validate_manifest(path: Path | str, skills_dir: Path | str | None = None) -> ValidationResult:
    """Validate one manifest file and return structured diagnostics.

    This never raises for manifest problems: the caller decides what to do with the
    diagnostics. Use :func:`load_manifest` when you want the model or an exception.

    ``skills_dir`` names the skills library granted names are checked against. It
    defaults to ``<residents_dir>/../skills`` when that exists, and to no library at
    all when it does not — a tree with no library validates exactly as it did before
    the library landed.
    """
    source = Path(path)
    library = _library(source.parent.parent, skills_dir)
    result = _validate_manifest(source, library)
    if not library.diagnostics:
        return result
    return ValidationResult(diagnostics=library.diagnostics).merged_with(result)


def _validate_manifest(source: Path, library: SkillLibrary) -> ValidationResult:
    """Validate one manifest against an already-loaded library."""
    data, diagnostics = _read_yaml(source)
    if data is None:
        return ValidationResult(diagnostics=tuple(diagnostics))

    credential_diagnostics = scan_for_credentials(data, source)
    if credential_diagnostics:
        # Stop before binding a secret into a model.
        return ValidationResult(diagnostics=tuple(credential_diagnostics))

    try:
        manifest = ResidentManifest.model_validate(data)
    except ValidationError as exc:
        return ValidationResult(diagnostics=tuple(_diagnostics_from_validation_error(exc, source)))

    from steward.skills import effective_names, grant_diagnostics  # noqa: PLC0415

    soul, soul_diagnostics = _load_soul(manifest, source)
    diagnostics.extend(soul_diagnostics)
    diagnostics.extend(_check_directory_name(manifest, source))
    diagnostics.extend(_check_duplicate_ids(manifest, source))
    diagnostics.extend(grant_diagnostics(manifest, library, source))
    diagnostics.extend(
        _check_routine_requirements(manifest, source, effective_names(manifest, library))
    )
    diagnostics.extend(_check_close_of_day(manifest, source))
    diagnostics.extend(_check_board_route(manifest, source))
    diagnostics.extend(_check_budget_runtime(manifest, source))
    diagnostics.extend(_check_delegation(manifest, source))
    diagnostics.extend(_check_soul_agreement(manifest, soul, source))

    resident = Resident(path=source, manifest=manifest, soul=soul)
    residents = (resident,) if not any(d.severity is Severity.ERROR for d in diagnostics) else ()
    return ValidationResult(residents=residents, diagnostics=tuple(diagnostics))


def load_manifest(path: Path | str, skills_dir: Path | str | None = None) -> Resident:
    """Load and validate one manifest, or raise :class:`ManifestError`.

    The single load-and-validate path shared by the scheduler, the API, and CI.
    """
    result = validate_manifest(path, skills_dir)
    if not result.ok or not result.residents:
        raise ManifestError(result.diagnostics)
    return result.residents[0]


def validate_tree(
    residents_dir: Path | str, skills_dir: Path | str | None = None
) -> ValidationResult:
    """Validate every ``<id>/manifest.yaml`` under a residents directory.

    The skills library is loaded once for the whole tree, so a broken ``SKILL.md`` is
    one complaint rather than one per resident.
    """
    root = Path(residents_dir)
    if not root.is_dir():
        return ValidationResult(
            diagnostics=(
                Diagnostic(
                    file=root,
                    field_path="<path>",
                    problem="residents directory does not exist",
                    example="residents/<id>/manifest.yaml",
                ),
            )
        )

    library = _library(root, skills_dir)
    result = ValidationResult(diagnostics=library.diagnostics)
    found = False
    for directory in sorted(p for p in root.iterdir() if p.is_dir()):
        manifest_path = directory / MANIFEST_FILENAME
        if not manifest_path.is_file():
            result = result.merged_with(
                ValidationResult(
                    diagnostics=(
                        Diagnostic(
                            file=directory,
                            field_path="<directory>",
                            problem=f"resident directory has no {MANIFEST_FILENAME}",
                            example=f"{directory.name}/{MANIFEST_FILENAME}",
                        ),
                    )
                )
            )
            continue
        found = True
        result = result.merged_with(_validate_manifest(manifest_path, library))

    result = result.merged_with(
        ValidationResult(diagnostics=tuple(_check_shared_journal_dirs(result.residents)))
    )

    if not found and not result.diagnostics:
        result = ValidationResult(
            diagnostics=(
                Diagnostic(
                    file=root,
                    field_path="<path>",
                    problem="no resident manifests found",
                    example="residents/life-agent/manifest.yaml",
                    severity=Severity.WARNING,
                ),
            )
        )
    return result


def validate_path(path: Path | str, skills_dir: Path | str | None = None) -> ValidationResult:
    """Validate a manifest file, a resident directory, or a whole residents tree."""
    target = Path(path)
    if target.is_file():
        return validate_manifest(target, skills_dir)
    if (target / MANIFEST_FILENAME).is_file():
        return validate_manifest(target / MANIFEST_FILENAME, skills_dir)
    return validate_tree(target, skills_dir)


def validate_paths(
    paths: Iterable[Path | str], skills_dir: Path | str | None = None
) -> ValidationResult:
    """Validate several targets, merging their results in order."""
    result = ValidationResult()
    for path in paths:
        result = result.merged_with(validate_path(path, skills_dir))
    return result


def manifest_json_schema() -> dict[str, Any]:
    """Return the manifest JSON Schema, so burrow can read manifests without translation."""
    schema = ResidentManifest.model_json_schema()
    schema["$id"] = "https://github.com/0xCommanderKeen/steward/schema/resident-manifest-v0.json"
    schema["title"] = "steward resident manifest v0"
    return schema
