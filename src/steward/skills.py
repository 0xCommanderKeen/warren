"""The skills library: named, reusable capabilities, shared by every resident.

A skill is a directory at the root of this repo::

    skills/<name>/SKILL.md

with YAML frontmatter (``name``, ``description``, and an optional ``defaults: true``)
and a markdown body of instructions written for the session that will read them. The
shape is deliberately the same as a Claude Code skill, so a skill steward injects into a
prompt is the same file a ``claude`` session can load from disk.

Two grants exist, and a resident's **effective set** is their sum:

**Defaults.** Every resident gets the skills marked ``defaults: true`` — the ones that
are part of being a resident at all: closing the day with a journal, writing an honest
summary, researching without inventing, escalating instead of guessing. Nothing has to
be granted for a resident to have them, and nothing can take them away.

**Grants.** Everything else is named in the resident's manifest under ``skills:``, and
must exist in the library: an unknown name fails validation with the closest match
named, rather than becoming a capability the resident believes it has.

The library is shared, so improving a skill improves every resident that holds it, and
adding one to a resident is edit-manifest → commit → next session has it. Skills carry
no credentials: a SKILL.md with a credential-shaped frontmatter key or an inline secret
fails validation exactly like a manifest would.
"""

import re
import shutil
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from steward.manifest import (
    Diagnostic,
    ResidentManifest,
    Severity,
    closest_match,
    scan_for_credentials,
    scan_text_for_secrets,
    split_frontmatter,
)

__all__ = [
    "BODY_MAX_CHARS",
    "DEFAULT_SKILLS_DIRNAME",
    "DESCRIPTION_MAX_CHARS",
    "SKILL_FILENAME",
    "Materialization",
    "Skill",
    "SkillError",
    "SkillLibrary",
    "default_skills",
    "default_skills_dir",
    "describe_missing",
    "effective_names",
    "effective_skills",
    "grant_diagnostics",
    "library_for",
    "load_library",
    "load_skill",
    "materialize",
    "missing_skills",
    "parse_skill",
]

SKILL_FILENAME = "SKILL.md"

#: Where the library lives relative to a residents tree: ``<residents_dir>/../skills``.
DEFAULT_SKILLS_DIRNAME = "skills"

#: A skill body is paid for on every session launch that holds it. Instructions, not a
#: manual: past this, the skill wants splitting into two.
BODY_MAX_CHARS = 8000

#: The description is a one-liner — it is what a listing shows and what a session reads
#: before deciding the skill applies.
DESCRIPTION_MAX_CHARS = 300

NAME_PATTERN = re.compile(r"^[a-z0-9][a-z0-9-]*$")

FRONTMATTER_KEYS = frozenset({"name", "description", "defaults"})

_FRONTMATTER_EXAMPLE = (
    "---\nname: write-journal\ndescription: Close the day by writing one honest entry.\n"
    "defaults: true\n---\n\nInstructions, in the second person."
)


class SkillError(Exception):
    """Raised when a session cannot be provisioned with the skills it was granted."""


# --------------------------------------------------------------------------------------
# the skill
# --------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Skill:
    """One parsed ``SKILL.md``: what it is called, what it is for, and how to do it."""

    name: str
    description: str
    body: str = ""
    #: True when this skill is part of the default set every resident holds.
    default: bool = False
    path: Path | None = None

    def document(self) -> str:
        """Render the canonical ``SKILL.md`` text, for materializing onto disk.

        Rendered rather than copied byte-for-byte so what a session loads from disk is
        exactly what steward parsed and validated — one representation, two readers.
        """
        frontmatter = [f"name: {self.name}", f"description: {self.description}"]
        if self.default:
            frontmatter.append("defaults: true")
        return "---\n" + "\n".join(frontmatter) + "\n---\n\n" + self.body.strip() + "\n"

    def render(self) -> str:
        """Render the skill for prompt injection: heading, description, then the body.

        The heading is an ``h1`` because skill bodies use ``##`` for their own sections;
        anything smaller would leave a body's headings outranking the skill they belong to.
        """
        return f"# {self.name} — {self.description}\n\n{self.body.strip()}"

    def as_dict(self) -> dict[str, Any]:
        """Render the skill as plain JSON-able data, for the API and the CLI."""
        return {
            "name": self.name,
            "description": self.description,
            "default": self.default,
            "path": str(self.path) if self.path is not None else None,
            "body_chars": len(self.body),
        }


@dataclass(frozen=True, slots=True)
class SkillLibrary:
    """Every skill steward could find, plus every complaint about the ones it could not.

    ``path`` is ``None`` when no library is configured at all — a residents tree with no
    ``skills/`` beside it. That is not an error: it is how a caller from before the
    library existed keeps working, and it means no grant is checked and no skill is
    injected. A *configured but broken* library is a different thing, and it complains.
    """

    path: Path | None = None
    skills: Mapping[str, Skill] = field(default_factory=dict)
    diagnostics: tuple[Diagnostic, ...] = ()

    @property
    def configured(self) -> bool:
        """True when a library directory was found and its contents count."""
        return self.path is not None

    @property
    def names(self) -> tuple[str, ...]:
        """Every skill name in the library, in stable (alphabetical) order."""
        return tuple(self.skills)

    @property
    def errors(self) -> tuple[Diagnostic, ...]:
        """The diagnostics that fail validation."""
        return tuple(d for d in self.diagnostics if d.severity is Severity.ERROR)

    def get(self, name: str) -> Skill | None:
        """Return one skill by name, or ``None``."""
        return self.skills.get(name)

    def __contains__(self, name: object) -> bool:
        """Report whether the library holds a skill by this name."""
        return name in self.skills

    def __iter__(self) -> Iterator[Skill]:
        """Iterate the skills in stable order."""
        return iter(self.skills.values())

    def __len__(self) -> int:
        """Count the skills that parsed."""
        return len(self.skills)


# --------------------------------------------------------------------------------------
# parsing
# --------------------------------------------------------------------------------------


def _complain(source: Path, field_path: str, problem: str, example: str) -> Diagnostic:
    return Diagnostic(file=source, field_path=field_path, problem=problem, example=example)


def _frontmatter(text: str, source: Path) -> tuple[Mapping[str, Any] | None, str, list[Diagnostic]]:
    raw, body = split_frontmatter(text)
    if raw is None:
        return (
            None,
            text,
            [
                _complain(
                    source,
                    "frontmatter",
                    f"{SKILL_FILENAME} has no --- frontmatter block, so it declares "
                    f"neither a name nor a description",
                    _FRONTMATTER_EXAMPLE,
                )
            ],
        )
    try:
        loaded = yaml.safe_load(raw) or {}
    except yaml.YAMLError as exc:
        return (
            None,
            body,
            [
                _complain(
                    source,
                    "frontmatter",
                    f"frontmatter is not valid YAML: {exc}",
                    _FRONTMATTER_EXAMPLE,
                )
            ],
        )
    if not isinstance(loaded, Mapping):
        return (
            None,
            body,
            [
                _complain(
                    source,
                    "frontmatter",
                    "frontmatter must be a mapping of keys to values",
                    _FRONTMATTER_EXAMPLE,
                )
            ],
        )
    return loaded, body, []


def parse_skill(  # noqa: C901 — one flat branch per field, each with its own diagnostic
    text: str, source: Path, expected_name: str | None = None
) -> tuple[Skill | None, list[Diagnostic]]:
    """Parse one ``SKILL.md``, returning the skill and every complaint about it.

    ``expected_name`` is the directory the file was found in. The frontmatter name must
    match it: the directory is what a manifest grants and what a session loads from
    disk, so a file that calls itself something else is a skill nobody can name.
    """
    frontmatter, body, diagnostics = _frontmatter(text, source)
    if frontmatter is None:
        return None, diagnostics

    # Credentials are rejected before anything is bound, exactly as in a manifest: a
    # skill is instructions, and instructions never need a secret in them.
    diagnostics.extend(scan_for_credentials(frontmatter, source))
    diagnostics.extend(scan_text_for_secrets(text, source, "body"))

    unknown = sorted(set(frontmatter) - FRONTMATTER_KEYS)
    if unknown:
        diagnostics.append(
            _complain(
                source,
                "frontmatter",
                f"unknown frontmatter key(s) {unknown}; a skill declares name, "
                f"description, and optionally defaults",
                _FRONTMATTER_EXAMPLE,
            )
        )

    name = str(frontmatter.get("name") or "").strip()
    if not name:
        diagnostics.append(
            _complain(
                source, "name", "required field is missing", f"name: {expected_name or 'research'}"
            )
        )
    elif not NAME_PATTERN.match(name):
        diagnostics.append(
            _complain(
                source,
                "name",
                f"name {name!r} is not a slug; it is a directory name and a grant id",
                "name: write-journal  (lowercase, dashes)",
            )
        )
    elif expected_name is not None and name != expected_name:
        diagnostics.append(
            _complain(
                source,
                "name",
                f"frontmatter says {name!r} but the skill lives in {expected_name!r}; "
                f"the directory is what a manifest grants, so the two must agree",
                f"name: {expected_name}",
            )
        )

    description = str(frontmatter.get("description") or "").strip()
    if not description:
        diagnostics.append(
            _complain(
                source,
                "description",
                "required field is missing; a skill nobody can describe in one line is "
                "one nobody can decide to grant",
                "description: How to research honestly: sources, uncertainty, citations.",
            )
        )
    elif len(description) > DESCRIPTION_MAX_CHARS:
        diagnostics.append(
            _complain(
                source,
                "description",
                f"description is {len(description)} characters; the cap is "
                f"{DESCRIPTION_MAX_CHARS} because it is a one-liner, not the skill",
                f"a description of at most {DESCRIPTION_MAX_CHARS} characters",
            )
        )

    stripped = body.strip()
    if not stripped:
        diagnostics.append(
            _complain(
                source,
                "body",
                "skill has no body; frontmatter names a capability, the body is the "
                "instructions that make it one",
                "a few dozen lines of direct, second-person guidance",
            )
        )
    elif len(stripped) > BODY_MAX_CHARS:
        diagnostics.append(
            _complain(
                source,
                "body",
                f"skill body is {len(stripped)} characters; the cap is {BODY_MAX_CHARS} "
                f"because every session that holds this skill pays for it — past this, "
                f"it wants splitting into two skills",
                f"a body of at most {BODY_MAX_CHARS} characters",
            )
        )

    defaults = frontmatter.get("defaults", False)
    if not isinstance(defaults, bool):
        diagnostics.append(
            _complain(
                source,
                "defaults",
                f"defaults is {defaults!r}; it is the flag that puts this skill in "
                f"every resident's set, so it is true or absent",
                "defaults: true",
            )
        )
        defaults = bool(defaults)

    if any(d.severity is Severity.ERROR for d in diagnostics):
        return None, diagnostics
    return (
        Skill(name=name, description=description, body=stripped, default=defaults, path=source),
        diagnostics,
    )


def load_skill(directory: Path) -> tuple[Skill | None, list[Diagnostic]]:
    """Load ``<directory>/SKILL.md``, named after the directory it lives in."""
    source = directory / SKILL_FILENAME
    try:
        text = source.read_text(encoding="utf-8")
    except OSError as exc:
        return None, [
            _complain(
                source,
                "<file>",
                f"cannot read skill: {exc.strerror or exc}",
                f"a readable {directory.name}/{SKILL_FILENAME}",
            )
        ]
    return parse_skill(text, source, directory.name)


def load_library(skills_dir: Path | str | None) -> SkillLibrary:
    """Load every ``<name>/SKILL.md`` under a skills directory.

    ``None`` — or a path that does not exist — is an unconfigured library, not an error:
    a residents tree with no ``skills/`` beside it validates exactly as it did before
    the library existed. Everything else is loaded, and what will not parse becomes a
    diagnostic rather than a silently missing capability.
    """
    if skills_dir is None:
        return SkillLibrary()
    root = Path(skills_dir)
    if not root.is_dir():
        return SkillLibrary()

    skills: dict[str, Skill] = {}
    diagnostics: list[Diagnostic] = []
    for directory in sorted(p for p in root.iterdir() if p.is_dir()):
        if not (directory / SKILL_FILENAME).is_file():
            diagnostics.append(
                _complain(
                    directory,
                    "<directory>",
                    f"skill directory has no {SKILL_FILENAME}",
                    f"{directory.name}/{SKILL_FILENAME}",
                )
            )
            continue
        skill, complaints = load_skill(directory)
        diagnostics.extend(complaints)
        if skill is not None:
            skills[skill.name] = skill
    return SkillLibrary(
        path=root,
        skills=dict(sorted(skills.items())),
        diagnostics=tuple(diagnostics),
    )


def library_for(residents_dir: Path | str, skills_dir: Path | str | None = None) -> SkillLibrary:
    """Turn a residents tree into the library it is validated and provisioned against.

    The one place that decision is made: an explicit ``skills_dir`` wins, otherwise the
    library beside the tree, otherwise no library at all.
    """
    return load_library(skills_dir if skills_dir is not None else default_skills_dir(residents_dir))


def default_skills_dir(residents_dir: Path | str) -> Path | None:
    """Return the library beside a residents tree — ``<residents_dir>/../skills``.

    ``None`` when there is none, which is what keeps every caller that predates the
    library working unchanged.
    """
    candidate = Path(residents_dir).resolve().parent / DEFAULT_SKILLS_DIRNAME
    return candidate if candidate.is_dir() else None


# --------------------------------------------------------------------------------------
# resolution
# --------------------------------------------------------------------------------------


def default_skills(library: SkillLibrary) -> tuple[Skill, ...]:
    """Return the skills every resident holds without granting them."""
    return tuple(skill for skill in library if skill.default)


def effective_skills(manifest: ResidentManifest, library: SkillLibrary) -> tuple[Skill, ...]:
    """Return this resident's whole skill set: defaults first, then its own grants.

    Deduplicated and stable: a manifest that also grants a default skill gets it once,
    in the defaults' position, so two residents with the same set are told the same
    thing in the same order. Grants the library does not have are left out here and
    reported by :func:`missing_skills`; nothing is silently substituted.
    """
    resolved: dict[str, Skill] = {skill.name: skill for skill in default_skills(library)}
    for grant in manifest.skills:
        skill = library.get(grant.id)
        if skill is not None and skill.name not in resolved:
            resolved[skill.name] = skill
    return tuple(resolved.values())


def effective_names(manifest: ResidentManifest, library: SkillLibrary) -> tuple[str, ...]:
    """Return the names of the effective set, including grants missing from the library.

    This is what ``routines[].requires`` is checked against. A grant that names no
    library skill is already its own diagnostic, and counting it here keeps one mistake
    from being reported twice under two different fields.
    """
    names = [skill.name for skill in default_skills(library)]
    names.extend(grant.id for grant in manifest.skills if grant.id not in names)
    return tuple(names)


def missing_skills(manifest: ResidentManifest, library: SkillLibrary) -> tuple[str, ...]:
    """Return the granted skills the library does not have, in manifest order."""
    if not library.configured:
        return ()
    return tuple(grant.id for grant in manifest.skills if grant.id not in library)


def grant_diagnostics(
    manifest: ResidentManifest, library: SkillLibrary, source: Path
) -> list[Diagnostic]:
    """Complain about every granted skill that names nothing in the library."""
    diagnostics: list[Diagnostic] = []
    for index, grant in enumerate(manifest.skills):
        if not library.configured or grant.id in library:
            continue
        close = closest_match(grant.id, library.names)
        known = ", ".join(library.names) or "none"
        diagnostics.append(
            _complain(
                source,
                f"skills[{index}].id",
                f"skill {grant.id!r} is not in the skills library at {library.path}; "
                f"a grant that names nothing is a capability this resident does not have",
                f"id: {close}"
                if close
                else f"one of: {known}  (or add {grant.id}/{SKILL_FILENAME})",
            )
        )
    return diagnostics


# --------------------------------------------------------------------------------------
# materializing onto disk
# --------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Materialization:
    """What writing a session's skills to disk actually came to."""

    root: Path
    written: tuple[str, ...] = ()
    unchanged: tuple[str, ...] = ()
    removed: tuple[str, ...] = ()

    def summary(self) -> str:
        """Describe the result in one line, for logs."""
        return (
            f"{len(self.written)} written, {len(self.unchanged)} unchanged, "
            f"{len(self.removed)} removed in {self.root}"
        )


def materialize(skills: Sequence[Skill], workdir: Path | str, subdir: str) -> Materialization:
    """Write a session's skills into ``<workdir>/<subdir>``, and own that directory.

    Write-if-changed, so a session that runs hourly does not rewrite eight files an
    hour, and — the load-bearing half — **anything in there that is not in this set is
    removed**. A skill taken out of a manifest is gone from the next session rather than
    surviving on disk as a capability nobody granted. Steward owns this directory; put
    nothing else in it.
    """
    root = Path(workdir) / subdir
    granted = {skill.name: skill for skill in skills}
    written: list[str] = []
    unchanged: list[str] = []
    removed: list[str] = []

    try:
        existing = sorted(root.iterdir()) if root.is_dir() else []
    except OSError as exc:  # pragma: no cover — an unreadable workdir fails on write below
        raise SkillError(f"cannot read the skills directory {root}: {exc}") from exc

    for entry in existing:
        if entry.name in granted and entry.is_dir():
            continue
        try:
            shutil.rmtree(entry) if entry.is_dir() else entry.unlink()
        except OSError as exc:
            raise SkillError(
                f"cannot remove {entry} from steward's skills directory: {exc}"
            ) from exc
        removed.append(entry.name)

    for name, skill in granted.items():
        target = root / name / SKILL_FILENAME
        document = skill.document()
        try:
            if target.is_file() and target.read_text(encoding="utf-8") == document:
                unchanged.append(name)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(document, encoding="utf-8")
        except OSError as exc:
            raise SkillError(f"cannot write skill {name!r} to {target}: {exc}") from exc
        written.append(name)

    return Materialization(
        root=root,
        written=tuple(written),
        unchanged=tuple(unchanged),
        removed=tuple(removed),
    )


def describe_missing(resident_id: str, missing: Iterable[str], library: SkillLibrary) -> str:
    """Explain a granted-but-missing skill in one line, for a pre-run failure."""
    names = list(missing)
    known = ", ".join(library.names) or "none"
    return (
        f"{resident_id} is granted {', '.join(repr(name) for name in names)}, which the "
        f"skills library at {library.path} does not have (it holds: {known}); steward "
        f"will not launch a session that believes it has a capability it was never given"
    )
