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
import re
import zoneinfo
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any, Literal, Self

import yaml
from croniter import croniter
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

__all__ = [
    "MANIFEST_FILENAME",
    "SCHEMA_VERSION",
    "VOICE_MAX_CHARS",
    "AppGrant",
    "Charter",
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
    "extract_voice",
    "load_manifest",
    "manifest_json_schema",
    "validate_manifest",
    "validate_path",
    "validate_paths",
    "validate_tree",
]

MANIFEST_FILENAME = "manifest.yaml"
DEFAULT_SOUL_FILENAME = "soul.md"
SCHEMA_VERSION = 0
VOICE_MAX_CHARS = 1200
VOICE_HEADING = "## Voice"

SLUG_PATTERN = r"^[a-z0-9][a-z0-9-]*$"
ACCENT_PATTERN = r"^#[0-9a-fA-F]{6}$"
AGENT_ID_PATTERN = r"^[a-z0-9][a-z0-9._-]*:[A-Za-z0-9._:-]+$"

_CRON_FIELD = r"(?:\*|[0-9]+|[0-9]+-[0-9]+|\*/[0-9]+|[0-9]+/[0-9]+|[0-9]+-[0-9]+/[0-9]+)"
_CRON_LIST = rf"{_CRON_FIELD}(?:,{_CRON_FIELD})*"
CRON_PATTERN = re.compile(rf"^{_CRON_LIST}(?:\s+{_CRON_LIST}){{4}}$")

MAX_TIMEOUT_S = 24 * 60 * 60

DEFAULT_SCHEDULE_TZ = "UTC"


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
        leaf = _strip_indices(path).rsplit(".", 1)[-1]
        if CREDENTIAL_KEY_PATTERN.search(leaf):
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
            if _strip_indices(path) in REFERENCE_FIELDS and _looks_like_opaque_blob(value):
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

    The id references the skills library (``skills/<id>/SKILL.md``). Existence is
    enforced once the library lands; the shape is enforced now.
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
    journal: str | None = Field(
        default=None,
        description="Journal file relative to path, read at session start.",
    )


class Route(_Model):
    """A declared inbound channel through which work can reach a resident."""

    id: str = Field(pattern=SLUG_PATTERN)
    kind: Literal["email", "chat", "http", "webhook", "cron", "cli", "job-board"]
    address: str = Field(min_length=1, description="Reference to the channel, not a secret.")
    status: Literal["active", "pending", "disabled"] = "active"
    note: str | None = None


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
    project: str | None = Field(default=None, description="Project label for scoped residents.")
    summary: str | None = Field(default=None, description="One line burrow can display.")

    soul: SoulIdentity
    charter: Charter
    skills: list[SkillGrant] = Field(description="Granted capabilities (may be empty).")
    memory: Memory
    routes: list[Route] = Field(description="Declared inbound channels (may be empty).")
    app_grants: list[AppGrant] = Field(description="Declared app access (may be empty).")

    runner: Runner = Field(default_factory=Runner)
    routines: list[Routine] = Field(default_factory=list)

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
    match = _FRONTMATTER_RE.match(text)
    if match is None:
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
        loaded = yaml.safe_load(match.group("frontmatter")) or {}
    except yaml.YAMLError as exc:
        return SoulDocument(path=source, body=match.group("body")), [
            Diagnostic(
                file=source,
                field_path="frontmatter",
                problem=f"frontmatter is not valid YAML: {exc}",
                example="name: Hob",
            )
        ]

    if not isinstance(loaded, Mapping):
        return SoulDocument(path=source, body=match.group("body")), [
            Diagnostic(
                file=source,
                field_path="frontmatter",
                problem="frontmatter must be a mapping of keys to values",
                example="name: Hob",
            )
        ]

    body = match.group("body")
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
    "skills": "skills: [daily-summary, read-inbox]",
    "skills.id": "id: daily-summary  (a name in skills/)",
    "skills.source": "source: library",
    "memory": "memory: {kind: directory, path: /data/residents/life-agent/memory}",
    "memory.kind": "kind: directory",
    "memory.path": "path: /data/residents/life-agent/memory",
    "memory.journal": "journal: journal.md",
    "routes": "routes: [{id: cron, kind: cron, address: steward-scheduler, status: active}]",
    "routes.id": "id: inbox",
    "routes.kind": "kind: email",
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
    "routines.requires": "requires: [daily-summary]  (skills granted above)",
    "routines.timeout_s": "timeout_s: 900",
    "routines.enabled": "enabled: true",
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


def _check_routine_requirements(manifest: ResidentManifest, source: Path) -> list[Diagnostic]:
    granted = {skill.id for skill in manifest.skills}
    diagnostics: list[Diagnostic] = []
    for index, routine in enumerate(manifest.routines):
        for position, required in enumerate(routine.requires):
            if required in granted:
                continue
            close = difflib.get_close_matches(required, sorted(granted), n=1)
            hint = f"skills: [{close[0]}]" if close else "grant it under skills: first"
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


def validate_manifest(path: Path | str) -> ValidationResult:
    """Validate one manifest file and return structured diagnostics.

    This never raises for manifest problems: the caller decides what to do with the
    diagnostics. Use :func:`load_manifest` when you want the model or an exception.
    """
    source = Path(path)
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

    soul, soul_diagnostics = _load_soul(manifest, source)
    diagnostics.extend(soul_diagnostics)
    diagnostics.extend(_check_directory_name(manifest, source))
    diagnostics.extend(_check_duplicate_ids(manifest, source))
    diagnostics.extend(_check_routine_requirements(manifest, source))
    diagnostics.extend(_check_soul_agreement(manifest, soul, source))

    resident = Resident(path=source, manifest=manifest, soul=soul)
    residents = (resident,) if not any(d.severity is Severity.ERROR for d in diagnostics) else ()
    return ValidationResult(residents=residents, diagnostics=tuple(diagnostics))


def load_manifest(path: Path | str) -> Resident:
    """Load and validate one manifest, or raise :class:`ManifestError`.

    The single load-and-validate path shared by the scheduler, the API, and CI.
    """
    result = validate_manifest(path)
    if not result.ok or not result.residents:
        raise ManifestError(result.diagnostics)
    return result.residents[0]


def validate_tree(residents_dir: Path | str) -> ValidationResult:
    """Validate every ``<id>/manifest.yaml`` under a residents directory."""
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

    result = ValidationResult()
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
        result = result.merged_with(validate_manifest(manifest_path))

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


def validate_path(path: Path | str) -> ValidationResult:
    """Validate a manifest file, a resident directory, or a whole residents tree."""
    target = Path(path)
    if target.is_file():
        return validate_manifest(target)
    if (target / MANIFEST_FILENAME).is_file():
        return validate_manifest(target / MANIFEST_FILENAME)
    return validate_tree(target)


def validate_paths(paths: Iterable[Path | str]) -> ValidationResult:
    """Validate several targets, merging their results in order."""
    result = ValidationResult()
    for path in paths:
        result = result.merged_with(validate_path(path))
    return result


def manifest_json_schema() -> dict[str, Any]:
    """Return the manifest JSON Schema, so burrow can read manifests without translation."""
    schema = ResidentManifest.model_json_schema()
    schema["$id"] = "https://github.com/0xCommanderKeen/steward/schema/resident-manifest-v0.json"
    schema["title"] = "steward resident manifest v0"
    return schema
