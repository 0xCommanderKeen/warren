"""Writing the residents tree and the skills library, from a control plane rather than a terminal.

Everything steward reads about the fleet lives in two trees of files — ``residents/<id>/``
and ``skills/<name>/`` — and until now the only way to change one was a person with a
checkout, an editor, and a commit. That is a good rule and this module does not break it.
It moves the *typing* somewhere else while keeping every guarantee the rule was there to
buy:

**An invalid manifest is never written.** Not written and then rolled back — never written
at all. A candidate write is applied to a throwaway *copy* of the tree and validated there
with :func:`steward.manifest.validate_tree`, the same gate ``steward validate`` runs. Only
a copy that passes is applied to the real tree. This is stronger than the nursery's
write-then-``rmtree``-on-failure, and it has to be: the nursery creates a new directory
that can simply be deleted, while an edit that failed halfway would have destroyed a
resident that was working ten seconds ago.

Validating a *copy of the whole tree* rather than the one file is the other half of that.
``validate_manifest`` on a single path cannot see a duplicate ``uid`` (steward #112) or two
residents sharing a journal directory — those checks only exist at tree level. An API that
validated one file would be strictly weaker than CI, and "it passed the API and broke the
build" is exactly the failure this endpoint exists to prevent.

**Every accepted write is committed.** By steward, naming who asked, so the audit trail and
the rollback are the ones the team already knows how to use: ``git log``, ``git revert``.
The repo stays the source of truth rather than becoming a cache of whatever the last HTTP
call did.

**Only the files steward wrote.** Commits are staged by path, never ``git add -A``. A
checkout with somebody's half-finished afternoon in it is none of steward's business, and
a server that swept unrelated work into its own commit would be far worse than one that
refused. This is why the nursery's dirty-worktree refusal is deliberately *not* reused
here: that refusal protects a deploy's "one commit to revert" story on a developer's
machine, and applying it to a server would mean a stray editor swapfile could stop the
control plane from working at all.
"""

import hashlib
import shutil
import tempfile
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from steward.manifest import (
    DEFAULT_SOUL_FILENAME,
    MANIFEST_FILENAME,
    Diagnostic,
    ValidationResult,
    validate_tree,
)
from steward.nursery import CommitIdentity, NurseryError, commit_paths
from steward.runners import PipedRun, run_argv
from steward.skills import (
    SKILL_FILENAME,
    Skill,
    default_skills_dir,
    parse_skill,
)

__all__ = [
    "DEFAULT_IDENTITY",
    "AuthoringError",
    "CommitReport",
    "Declaration",
    "SkillDocument",
    "commit_write",
    "read_declaration",
    "read_skill_document",
    "repo_toplevel",
    "resolve_skills_dir",
    "revision_of",
    "write_declaration",
    "write_skill",
]


#: Who a commit made over the API is by, when the operator has not said otherwise.
#:
#: Deliberately not a person. ``STEWARD_TOKEN`` is a shared secret with no principal behind
#: it — steward genuinely does not know *which* human is holding it — and an author line
#: naming somebody would be a guess dressed up as an audit record. What steward can say
#: truthfully is "this arrived over the API", so that is what it says. An operator running a
#: single-person steward can configure a real identity, which is then exactly as trustworthy
#: as the token itself.
DEFAULT_IDENTITY = CommitIdentity(name="steward (api)", email="steward-api@localhost")

#: The commit subjects the write API produces. Prefixed like the nursery's so a
#: ``git log --oneline`` of the residents tree reads as one lifecycle regardless of whether
#: a change arrived from a terminal or a control panel.
UPDATE_SUBJECT = "chore(residents): update {id} via the API"
SKILL_CREATE_SUBJECT = "feat(skills): add {name} via the API"
SKILL_UPDATE_SUBJECT = "chore(skills): update {name} via the API"

#: The trailer that ties a commit back to the request log. ``git show`` names the request,
#: ``GET /requests/{id}`` names the method, the path, and the moment — so "who changed this
#: and when" is answerable from either end without steward inventing an identity it does
#: not have.
REQUEST_TRAILER = "Steward-Request-Id"


class AuthoringError(Exception):
    """Raised when a write cannot be accepted, carrying why in a form a UI can render.

    ``diagnostics`` is the validator's own structured complaints — file, field path,
    problem, example — rather than a rendered blob of prose, because the caller is a form
    with fields to highlight. ``reason`` is the machine-readable code the HTTP layer keys
    its status off.
    """

    def __init__(
        self,
        message: str,
        diagnostics: Sequence[Diagnostic] = (),
        *,
        reason: str = "write_refused",
    ) -> None:
        """Record the complaint, its structured diagnostics, and its reason code."""
        super().__init__(message)
        self.diagnostics = tuple(diagnostics)
        self.reason = reason


@dataclass(frozen=True, slots=True)
class CommitReport:
    """What git did about an accepted write, including when it did nothing.

    ``committed: false`` with ``sha: null`` is not a failure and not a silence — it is the
    converged answer, meaning the bytes on disk were already the bytes in git. A UI that
    treated it as an error would report a problem every time somebody saved a form without
    changing anything.
    """

    committed: bool
    sha: str | None
    message: str
    note: str

    def to_dict(self) -> dict[str, Any]:
        """Return the JSON view a response carries."""
        return {
            "committed": self.committed,
            "sha": self.sha,
            "message": self.message,
            "note": self.note,
        }


@dataclass(frozen=True, slots=True)
class Declaration:
    """The two files that declare one resident, as text.

    Both, together, and never one at a time: ``agent_id`` appears in the manifest *and* in
    the soul's frontmatter, and :func:`steward.manifest._check_soul_agreement` insists they
    match. Split into two endpoints, renaming a resident's agent id would be impossible —
    whichever file you wrote first would be refused for disagreeing with the other.
    """

    manifest_text: str
    soul_text: str | None = None

    @property
    def soul_filename(self) -> str:
        """Return the soul file this manifest names, without validating anything.

        Only used to decide *where* to put the soul in the candidate tree; the real answer
        comes from the model once the candidate validates. A manifest too broken to parse
        gets the default name, and then fails validation for the reason it is actually
        broken rather than for a missing file.
        """
        try:
            data = yaml.safe_load(self.manifest_text)
        except yaml.YAMLError:
            return DEFAULT_SOUL_FILENAME
        if not isinstance(data, Mapping):
            return DEFAULT_SOUL_FILENAME
        soul = data.get("soul")
        if isinstance(soul, Mapping) and isinstance(soul.get("file"), str):
            return soul["file"]
        return DEFAULT_SOUL_FILENAME


@dataclass(frozen=True, slots=True)
class SkillDocument:
    """One ``SKILL.md``, as the library stores it and as a form edits it."""

    name: str
    description: str
    body: str
    default: bool = False

    def text(self) -> str:
        """Render the document exactly as it will land on disk."""
        return Skill(
            name=self.name, description=self.description, body=self.body, default=self.default
        ).document()


# --------------------------------------------------------------------------------------
# reading — what a form loads before it edits
# --------------------------------------------------------------------------------------


def revision_of(*paths: Path) -> str:
    """Fingerprint the files a document is made of, for optimistic concurrency.

    Content rather than mtime: two stewards, a git pull, and a checkout restored from a
    backup all change mtimes without changing meaning, and a UI that refused a save because
    the clock moved would be a UI people learn to force past. A missing file hashes as
    absent, so "there was no soul and now there is" is a change like any other.
    """
    digest = hashlib.sha256()
    for path in paths:
        digest.update(path.name.encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes() if path.is_file() else b"")
        digest.update(b"\0")
    return f"sha256:{digest.hexdigest()}"


def declaration_paths(residents_dir: Path, resident_id: str, soul_filename: str) -> list[Path]:
    """Return the manifest and soul paths for one resident, in commit order."""
    directory = residents_dir / resident_id
    return [directory / MANIFEST_FILENAME, directory / soul_filename]


def read_declaration(residents_dir: Path, resident_id: str, soul_filename: str) -> Declaration:
    """Read one resident's two files back as text, byte for byte.

    Text rather than a re-serialised model, and that is the whole point: a manifest people
    edit carries comments explaining choices somebody thought about, and a round trip
    through pydantic and ``yaml.safe_dump`` would silently throw all of them away. A caller
    that wants structure parses this; a caller that wants to preserve the file writes it
    back unchanged apart from the line it meant to change.
    """
    manifest_path, soul_path = declaration_paths(residents_dir, resident_id, soul_filename)
    return Declaration(
        manifest_text=manifest_path.read_text(encoding="utf-8"),
        soul_text=soul_path.read_text(encoding="utf-8") if soul_path.is_file() else None,
    )


def read_skill_document(skills_dir: Path, name: str) -> tuple[SkillDocument, str]:
    """Read one skill and its revision, or raise :class:`AuthoringError` if it is unreadable."""
    path = skills_dir / name / SKILL_FILENAME
    if not path.is_file():
        raise AuthoringError(f"no skill {name!r} in {skills_dir}", reason="unknown_skill")
    skill, diagnostics = parse_skill(path.read_text(encoding="utf-8"), path, name)
    if skill is None:
        raise AuthoringError(
            f"skill {name!r} exists but does not parse; fix it in the checkout",
            diagnostics,
            reason="skill_invalid",
        )
    return (
        SkillDocument(
            name=skill.name,
            description=skill.description,
            body=skill.body,
            default=skill.default,
        ),
        revision_of(path),
    )


def resolve_skills_dir(residents_dir: Path, skills_dir: Path | str | None) -> Path | None:
    """Answer "which library is this tree validated against" once, explicitly.

    Every validation in this module names its library rather than letting it be inferred,
    because the candidate tree lives in a temp directory and
    :func:`steward.skills.default_skills_dir` looks *beside* the tree it is given. Left to
    infer, a candidate residents tree would be validated against no library at all, every
    skill grant in the fleet would become a diagnostic, and a perfectly good edit would be
    refused with a page of complaints about skills that exist.
    """
    if skills_dir is not None:
        return Path(skills_dir)
    return default_skills_dir(residents_dir)


# --------------------------------------------------------------------------------------
# the gate — validate a copy, apply only what passed
# --------------------------------------------------------------------------------------


@contextmanager
def candidate_tree(source: Path) -> Iterator[Path]:
    """Copy a tree somewhere disposable so a write can be tried before it is meant.

    ``symlinks=True`` copies links as links rather than following them: a residents tree is
    not supposed to contain any, and resolving one here would quietly pull whatever it
    pointed at into the copy steward is about to validate and trust.
    """
    with tempfile.TemporaryDirectory(prefix="steward-authoring-") as scratch:
        target = Path(scratch) / source.name
        shutil.copytree(source, target, symlinks=True)
        yield target


def relocate(diagnostics: Sequence[Diagnostic], frm: Path, to: Path) -> tuple[Diagnostic, ...]:
    """Re-point diagnostics from the candidate copy at the real tree.

    Without this every complaint a rejected write produced would name a temp directory that
    stopped existing before the response was serialised — telling a person to go and fix a
    file at a path that never existed for them.
    """
    relocated = []
    for diagnostic in diagnostics:
        try:
            moved = to / diagnostic.file.relative_to(frm)
        except ValueError:
            relocated.append(diagnostic)
            continue
        relocated.append(
            Diagnostic(
                file=moved,
                field_path=diagnostic.field_path,
                problem=diagnostic.problem,
                example=diagnostic.example,
                severity=diagnostic.severity,
            )
        )
    return tuple(relocated)


def diagnostic_as_dict(diagnostic: Diagnostic) -> dict[str, str]:
    """Return one diagnostic as the structured object a form highlights a field with.

    The rest of the API serves ``diagnostic.render()`` — three lines of terminal prose,
    right for a console and useless to a UI that wants to put a red border round
    ``charter.mission``. The fields were always there; this is the write path admitting it.
    """
    return {
        "file": str(diagnostic.file),
        "field": diagnostic.field_path,
        "problem": diagnostic.problem,
        "example": diagnostic.example,
        "severity": diagnostic.severity.value,
    }


def _refuse_on_errors(result: ValidationResult, what: str, reason: str) -> None:
    """Turn a failed validation into the refusal the caller sees. Nothing has been written."""
    if result.ok:
        return
    first = result.errors[0]
    raise AuthoringError(
        f"{what} does not validate: {first.field_path}: {first.problem}",
        result.errors,
        reason=reason,
    )


def _write_declaration_into(directory: Path, declaration: Declaration) -> None:
    """Write one resident's files into a directory, creating it if it is new."""
    directory.mkdir(parents=True, exist_ok=True)
    (directory / MANIFEST_FILENAME).write_text(declaration.manifest_text, encoding="utf-8")
    if declaration.soul_text is not None:
        (directory / declaration.soul_filename).write_text(declaration.soul_text, encoding="utf-8")


def validate_declaration(
    residents_dir: Path,
    resident_id: str,
    declaration: Declaration,
    skills_dir: Path | None,
) -> ValidationResult:
    """Validate a proposed declaration against a copy of the tree it would join.

    Raises :class:`AuthoringError` if it does not pass. Returns the result of the *whole
    tree* with the write applied, so a caller can also see that its edit left everybody
    else valid — which is the failure mode a single-file check cannot catch.
    """
    with candidate_tree(residents_dir) as candidate:
        _write_declaration_into(candidate / resident_id, declaration)
        result = validate_tree(candidate, skills_dir)
        relocated = ValidationResult(
            residents=result.residents,
            diagnostics=relocate(result.diagnostics, candidate, residents_dir),
        )
    _refuse_on_errors(relocated, f"the declaration for {resident_id!r}", "manifest_invalid")
    return relocated


def validate_skill(
    residents_dir: Path,
    skills_dir: Path,
    document: SkillDocument,
) -> ValidationResult:
    """Validate a proposed skill, then validate the fleet that would be reading it.

    Two gates, because a skill is not only a file. :func:`steward.skills.parse_skill` is the
    one the library itself applies — frontmatter, the name matching its directory, the
    description and body caps, and the credential and secret scans every steward document
    goes through. Then the whole residents tree is validated against a library holding the
    candidate, because ``defaults: true`` hands this skill to every resident in the fleet
    at once and a body that broke somebody's grant should be refused here rather than
    discovered at the next wake-up.
    """
    text = document.text()
    source = skills_dir / document.name / SKILL_FILENAME
    # Parse what will actually be written rather than what was asked for. `Skill.document`
    # builds frontmatter with f-strings, so a description carrying a colon or a newline
    # renders into YAML that means something else — a round trip is the only honest check
    # that the bytes landing on disk say what the request said.
    skill, diagnostics = parse_skill(text, source, document.name)
    if skill is None:
        raise AuthoringError(
            f"the skill {document.name!r} does not validate: {diagnostics[0].problem}",
            diagnostics,
            reason="skill_invalid",
        )
    with candidate_tree(skills_dir) as candidate:
        directory = candidate / document.name
        directory.mkdir(parents=True, exist_ok=True)
        (directory / SKILL_FILENAME).write_text(text, encoding="utf-8")
        result = validate_tree(residents_dir, candidate)
        relocated = ValidationResult(
            residents=result.residents,
            diagnostics=relocate(result.diagnostics, candidate, skills_dir),
        )
    _refuse_on_errors(relocated, f"the fleet reading skill {document.name!r}", "skill_invalid")
    return relocated


# --------------------------------------------------------------------------------------
# git — the audit trail and the undo button, for free
# --------------------------------------------------------------------------------------


def repo_toplevel(path: Path, *, git: PipedRun = run_argv) -> Path | None:
    """Return the checkout ``path`` is inside, or ``None`` if it is not in one.

    ``--show-toplevel`` rather than ``--is-inside-work-tree`` because the answer is needed
    as well as the fact: commits stage paths relative to the repository root, and the
    residents tree is usually a directory *within* the checkout rather than the checkout.
    """
    if not path.is_dir():
        return None
    outcome = git(["git", "-C", str(path), "rev-parse", "--show-toplevel"])
    if not outcome.ok:
        return None
    top = outcome.stdout.strip()
    return Path(top) if top else None


def commit_message(subject: str, request_id: str, principal: str) -> str:
    """Compose the commit body: what happened, who asked, and which request it was."""
    return f"{subject}\n\nWritten over the steward API by {principal}.\n{REQUEST_TRAILER}: {request_id}\n"


def commit_write(  # noqa: PLR0913 — one parameter per fact the commit records
    residents_dir: Path,
    paths: Sequence[Path],
    subject: str,
    *,
    request_id: str,
    principal: str,
    identity: CommitIdentity = DEFAULT_IDENTITY,
    allow_uncommitted: bool = False,
    git: PipedRun = run_argv,
) -> CommitReport:
    """Commit exactly these paths, or say clearly why nothing was committed.

    The refusal when there is no checkout is deliberate and is the answer to "what if the
    residents tree is not a git repo". Writing anyway would leave a fleet whose declarations
    have no history, no author, and no way back — quietly, and only discovered on the day
    somebody needs to undo something. An operator who genuinely wants that
    (a container with the tree on a volume, no git installed) sets ``allow_uncommitted``,
    which says it out loud in configuration and in every response.
    """
    repo = repo_toplevel(residents_dir, git=git)
    if repo is None:
        if not allow_uncommitted:
            raise AuthoringError(
                f"{residents_dir} is not inside a git checkout, so this write could not be "
                f"committed and steward will not write history it cannot record; point "
                f"STEWARD_RESIDENTS at the tree inside your checkout, or set "
                f"STEWARD_ALLOW_UNCOMMITTED_WRITES=1 to accept a fleet with no audit trail",
                reason="not_a_git_checkout",
            )
        return CommitReport(
            committed=False,
            sha=None,
            message="",
            note=(
                "written but NOT committed: this residents tree is not inside a git "
                "checkout and steward was configured to accept that, so there is no audit "
                "trail and no way to revert this change"
            ),
        )
    message = commit_message(subject, request_id, principal)
    try:
        sha = commit_paths(repo, list(paths), message, identity=identity, git=git)
    except NurseryError as exc:
        raise AuthoringError(str(exc), reason="commit_failed") from exc
    if sha is None:
        return CommitReport(
            committed=False,
            sha=None,
            message=message,
            note="nothing to commit: what is on disk was already what is in git",
        )
    return CommitReport(
        committed=True, sha=sha, message=message, note=f"committed as {sha[:12]} by {principal}"
    )


# --------------------------------------------------------------------------------------
# the write paths themselves
# --------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class WriteResult:
    """One accepted write: what landed, where, and what git made of it."""

    paths: tuple[Path, ...]
    revision: str
    commit: CommitReport
    validation: ValidationResult


def write_declaration(  # noqa: PLR0913 — one parameter per fact about the write
    residents_dir: Path,
    resident_id: str,
    declaration: Declaration,
    *,
    request_id: str,
    principal: str,
    skills_dir: Path | None = None,
    subject: str | None = None,
    identity: CommitIdentity = DEFAULT_IDENTITY,
    allow_uncommitted: bool = False,
    git: PipedRun = run_argv,
) -> WriteResult:
    """Validate, write, and commit one resident's declaration. Refusals write nothing."""
    result = validate_declaration(residents_dir, resident_id, declaration, skills_dir)
    _write_declaration_into(residents_dir / resident_id, declaration)
    paths = declaration_paths(residents_dir, resident_id, declaration.soul_filename)
    written = tuple(path for path in paths if path.is_file())
    commit = commit_write(
        residents_dir,
        written,
        subject if subject is not None else UPDATE_SUBJECT.format(id=resident_id),
        request_id=request_id,
        principal=principal,
        identity=identity,
        allow_uncommitted=allow_uncommitted,
        git=git,
    )
    return WriteResult(
        paths=written, revision=revision_of(*written), commit=commit, validation=result
    )


def write_skill(  # noqa: PLR0913 — one parameter per fact about the write
    residents_dir: Path,
    skills_dir: Path,
    document: SkillDocument,
    *,
    request_id: str,
    principal: str,
    created: bool,
    identity: CommitIdentity = DEFAULT_IDENTITY,
    allow_uncommitted: bool = False,
    git: PipedRun = run_argv,
) -> WriteResult:
    """Validate, write, and commit one skill. Refusals write nothing."""
    result = validate_skill(residents_dir, skills_dir, document)
    directory = skills_dir / document.name
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / SKILL_FILENAME
    path.write_text(document.text(), encoding="utf-8")
    subject = (SKILL_CREATE_SUBJECT if created else SKILL_UPDATE_SUBJECT).format(name=document.name)
    commit = commit_write(
        residents_dir,
        [path],
        subject,
        request_id=request_id,
        principal=principal,
        identity=identity,
        allow_uncommitted=allow_uncommitted,
        git=git,
    )
    return WriteResult(paths=(path,), revision=revision_of(path), commit=commit, validation=result)
