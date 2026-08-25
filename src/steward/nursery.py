"""The nursery: raising a resident, in the one place that will ever do it.

Steward #4 turns raising a resident into three stages, and this module is all three:

**declare**
    Write the soul and manifest into this repo, read them back through the ordinary
    validator, and — from the CLI — commit them. The repo is the source of truth, so the
    declaration exists in git *before* anything is built out of it, and a provision that
    fails leaves a commit somebody can re-run from.
**provision**
    Render the resident's compose fragment and runtime bundle, put it on the host through
    the transport seam (:mod:`steward.deploy`), and bring the container up.
**register**
    Verify the scheduler can actually take this resident — the same ``check()``
    ``steward doctor`` runs — and report when each routine fires next. There is no second
    registry: routines are read from the manifest, so "registered" means *the manifest is
    valid, the runner exists, and here is the next fire*, and nothing is written anywhere
    to make that true.

## Two callers, one difference

``steward new-resident`` commits; ``POST /residents`` does not, ever, even with
``deploy: true``. The server is not guaranteed to own the checkout it is reading — it may
be a tailnet process on a machine where nobody is watching git, and a commit made there is
a commit that surprises somebody. So the API writes the files, deploys if asked, and says
plainly in its response that the declaration is uncommitted and a human has to commit it.
Everything else about the two paths is the same code.

## Nothing is emitted on a resident's behalf

Not at declare, not at provision, not at retire. A villager appears in burrow when the
resident genuinely exists and emits its own first event, and it leaves when it stops
emitting. steward does not forge a ``session_ended`` for a resident it just retired, and
it does not emit a ``task_started`` to make a house look occupied before anybody lives in
it. This is the same rule the scheduler and the watchdog follow, and the nursery is where
it would be most tempting to break.

## --dry-run touches nothing

Not the repo, not the host, not the scheduler's state. A rehearsal prints the files it
would write, the compose fragment it would render, the exact argv it would run over the
transport, and the next fire of every routine — and makes zero transport calls, which
``tests/test_nursery.py`` asserts by handing it a transport that records everything and
then checking the recording is empty.
"""

import os
import re
import shutil
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from steward.deploy import (
    BUNDLE_NAMES,
    BURROW_URL_ENV,
    COMPOSE_FILENAME,
    DeployTarget,
    Transport,
    TransportError,
    bundle_changes,
    bundle_for,
    burrow_env,
    compose_argv,
    planned_env,
    render_argv,
    render_compose,
    target_for,
    transport_for,
)
from steward.manifest import (
    DEFAULT_JOURNAL_DIR,
    MANIFEST_FILENAME,
    AppGrant,
    Charter,
    Diagnostic,
    ManifestError,
    Memory,
    Resident,
    ResidentManifest,
    Route,
    Runner,
    SkillGrant,
    SoulDocument,
    SoulIdentity,
    closest_match,
    load_manifest,
    validate_manifest,
)
from steward.runners import CommandOutcome, PipedRun, run_argv
from steward.scheduler import (
    ScheduledRoutine,
    Scheduler,
    SchedulerState,
    default_state_path,
    next_fire_after,
)
from steward.skills import library_for

__all__ = [
    "CreatedResident",
    "DeclareStage",
    "NewResident",
    "NurseryError",
    "NurseryReport",
    "ProvisionStage",
    "RegisterStage",
    "RetireReport",
    "declare_resident",
    "raise_resident",
    "retire_resident",
]

#: How a runner kind becomes the ``<source>`` half of a burrow agent id, when the
#: caller does not name one. Anything else is steward's own doing, and says so.
AGENT_ID_SOURCES = {"claude": "claude-code", "codex": "codex"}

DEFAULT_MEMORY_ROOT = "/data/residents"

DEFAULT_SOUL_BODY = (
    "A new resident of the village. This soul body is a skeleton written by steward's "
    "nursery — replace it with who this resident actually is before deploying them."
)

DEFAULT_VOICE = (
    "Plain and specific. Says what happened and what it does not know, and never "
    "dresses an assumption up as a fact."
)


class NurseryError(Exception):
    """Raised when a resident cannot be declared. Carries the diagnostics, when there are any.

    ``reason`` is the machine-readable half, for the callers that have to answer in a code
    rather than a paragraph — the API turns it into the ``error`` field of its refusal. It
    is ``None`` for the ordinary "this could not be declared", and set only where a caller
    would otherwise have to match on prose to tell two refusals apart.
    """

    def __init__(
        self,
        message: str,
        diagnostics: tuple[Diagnostic, ...] = (),
        *,
        reason: str | None = None,
    ) -> None:
        """Explain the refusal, and name the fields that caused it when known."""
        self.diagnostics = diagnostics
        self.reason = reason
        super().__init__(message)


class NewResident(BaseModel):
    """What a caller must say to declare a resident. The API body and the CLI share it."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    id: str = Field(pattern=r"^[a-z0-9][a-z0-9-]*$", description="Directory under residents/.")
    name: str = Field(min_length=1, description="Display name, e.g. Hob.")
    char: str = Field(min_length=1, description="Burrow sprite key, e.g. Monk.")
    accent: str = Field(pattern=r"^#[0-9a-fA-F]{6}$", description="Hex accent colour.")
    role: str = Field(min_length=1, description="One-line role, e.g. life bot.")
    charter: Charter = Field(description="Mission, duties, hard rules, escalation policy.")

    agent_id: str | None = Field(default=None, description="Burrow identity; derived if absent.")
    project: str | None = Field(default=None, description="Project label, for a scoped soul.")
    summary: str | None = Field(default=None, description="One line burrow can display.")
    skills: list[SkillGrant] = Field(default_factory=list, description="Granted capabilities.")
    memory: Memory | None = Field(default=None, description="Memory location; derived if absent.")
    routes: list[Route] = Field(default_factory=list, description="Declared inbound channels.")
    app_grants: list[AppGrant] = Field(default_factory=list, description="Declared app access.")
    runner: Runner = Field(default_factory=Runner, description="Which brain this resident runs on.")
    soul_body: str | None = Field(default=None, description="Opening paragraph of soul.md.")
    voice: str | None = Field(default=None, description="The soul's ## Voice section.")

    def resolved_agent_id(self) -> str | None:
        """Return the burrow identity to declare, deriving one when only an id was given."""
        if self.agent_id or self.project:
            return self.agent_id
        source = AGENT_ID_SOURCES.get(self.runner.kind, "steward")
        return f"{source}:{self.id}"

    def resolved_memory(self) -> Memory:
        """Return the declared memory location, or the conventional one for this id."""
        if self.memory is not None:
            return self.memory
        return Memory(
            kind="directory",
            path=f"{DEFAULT_MEMORY_ROOT}/{self.id}/memory",
            journal=DEFAULT_JOURNAL_DIR,
        )


@dataclass(frozen=True, slots=True)
class CreatedResident:
    """What the declare stage actually wrote, and the validated resident it read back."""

    id: str
    directory: Path
    manifest_path: Path
    soul_path: Path
    resident: Resident

    def to_dict(self) -> dict[str, Any]:
        """Return the JSON view: the paths a human should go and review."""
        return {
            "id": self.id,
            "directory": str(self.directory),
            "manifest_path": str(self.manifest_path),
            "soul_path": str(self.soul_path),
            "agent_id": self.resident.manifest.agent_id,
            "project": self.resident.manifest.project,
        }


def _soul_document(spec: NewResident, agent_id: str | None) -> str:
    """Render ``soul.md``: frontmatter that agrees with the manifest, then a body."""
    frontmatter: dict[str, Any] = {}
    if agent_id:
        frontmatter["agent_id"] = agent_id
    if spec.project:
        frontmatter["project"] = spec.project
    frontmatter |= {
        "name": spec.name,
        "char": spec.char,
        "accent": spec.accent,
        "role": spec.role,
    }
    header = yaml.safe_dump(frontmatter, sort_keys=False, allow_unicode=True).strip()
    body = (spec.soul_body or DEFAULT_SOUL_BODY).strip()
    voice = (spec.voice or DEFAULT_VOICE).strip()
    return f"---\n{header}\n---\n{body}\n\n## Voice\n\n{voice}\n"


def _manifest_model(spec: NewResident) -> ResidentManifest:
    """Bind the request into a manifest model, so an invalid one never reaches disk."""
    try:
        return ResidentManifest(
            id=spec.id,
            agent_id=spec.resolved_agent_id(),
            project=spec.project,
            summary=spec.summary,
            soul=SoulIdentity(name=spec.name, char=spec.char, accent=spec.accent, role=spec.role),
            charter=spec.charter,
            skills=spec.skills,
            memory=spec.resolved_memory(),
            routes=spec.routes,
            app_grants=spec.app_grants,
            runner=spec.runner,
            routines=[],
        )
    except ValidationError as exc:
        raise NurseryError(
            f"cannot declare resident {spec.id!r}: {exc.errors()[0]['msg']}"
        ) from exc


def declare_resident(spec: NewResident, residents_dir: Path | str) -> CreatedResident:
    """Write a resident's manifest and soul body, and read them back through the validator.

    Refuses to touch an existing resident: converging an existing declaration is the
    nursery's job in steward #4, and quietly overwriting a soul someone wrote is not
    something an API call should be able to do by accident.
    """
    root = Path(residents_dir)
    directory = root / spec.id
    if directory.exists():
        raise NurseryError(f"resident {spec.id!r} already exists at {directory}")

    manifest = _manifest_model(spec)
    payload = manifest.model_dump(mode="json", exclude_none=True)
    # An ordinary resident declares no `deploy` block at all — docs/manifest.md says so, and
    # the dxp2800 defaults (image, `command: [sleep, infinity]`) fill it in. The bare model
    # default dumps as `deploy: {command: []}`, which reads as "this container runs nothing",
    # the opposite of the documented default. Drop it so the skeleton says what it means.
    if payload.get("deploy") == {"command": []}:
        del payload["deploy"]

    directory.mkdir(parents=True)
    manifest_path = directory / MANIFEST_FILENAME
    soul_path = directory / manifest.soul.file
    try:
        manifest_path.write_text(
            yaml.safe_dump(payload, sort_keys=False, allow_unicode=True), encoding="utf-8"
        )
        soul_path.write_text(_soul_document(spec, manifest.agent_id), encoding="utf-8")
        result = validate_manifest(manifest_path)
        if not result.ok or not result.residents:
            raise NurseryError(  # noqa: TRY301 — the cleanup below is the point of the try
                f"the declaration written for {spec.id!r} does not validate",
                result.errors,
            )
    except NurseryError, OSError:
        # A skeleton that does not validate is worse than no skeleton: it would break
        # `steward validate` for everyone until someone deleted it by hand.
        shutil.rmtree(directory, ignore_errors=True)
        raise

    return CreatedResident(
        id=spec.id,
        directory=directory,
        manifest_path=manifest_path,
        soul_path=soul_path,
        resident=result.residents[0],
    )


# --------------------------------------------------------------------------------------
# git — the repo is the source of truth, so the CLI path commits before it builds
# --------------------------------------------------------------------------------------

#: The commit subjects the nursery writes. Prefixed so `git log --oneline` reads as a
#: lifecycle rather than as noise from a robot.
DECLARE_SUBJECT = "feat(residents): declare {id}"
RETIRE_SUBJECT = "chore(residents): retire {id}"

#: How many dirty paths a refusal names before it starts counting instead.
DIRTY_SHOWN = 5


def _git(repo: Path, *args: str, git: PipedRun = run_argv) -> CommandOutcome:
    """Run one git command against a checkout. ``-C`` rather than a chdir, always."""
    return git(["git", "-C", str(repo), *args])


def worktree_complaint(repo: Path, *, git: PipedRun = run_argv) -> str | None:
    """Return why this worktree is not safe to commit into, or ``None``.

    The nursery commits a manifest and a soul by path, so an unrelated dirty file could
    not actually be swept into the commit — but the refusal is not about that. It is
    about the resident: a deploy that goes wrong should leave one commit to revert and one
    command to re-run, and a repo that already had half of somebody's afternoon in it when
    the nursery started is a repo where "revert the deploy" is no longer a simple sentence.
    ``--allow-dirty`` says out loud that you have read that and want it anyway.
    """
    outcome = _git(repo, "rev-parse", "--is-inside-work-tree", git=git)
    if not outcome.ok:
        return (
            f"{repo} is not a git checkout, so there is nothing to commit the declaration "
            f"into; point --repo at the steward repo or pass --no-commit"
        )
    status = _git(repo, "status", "--porcelain", git=git)
    if not status.ok:
        return f"git could not read the worktree at {repo}: {status.summary()}"
    dirty = [line for line in status.stdout.splitlines() if line.strip()]
    if not dirty:
        return None
    shown = ", ".join(line[3:] for line in dirty[:DIRTY_SHOWN])
    more = f" (+{len(dirty) - DIRTY_SHOWN} more)" if len(dirty) > DIRTY_SHOWN else ""
    return (
        f"the worktree at {repo} has uncommitted changes ({shown}{more}); commit or stash "
        f"them so a failed deploy leaves exactly one commit to revert, or pass "
        f"--allow-dirty to go ahead anyway"
    )


def commit_paths(
    repo: Path, paths: Sequence[Path], message: str, *, git: PipedRun = run_argv
) -> str | None:
    """Stage and commit exactly these paths. Returns the new commit, or ``None``.

    ``None`` means *nothing to commit*, and it is the ordinary answer on a converged
    re-run: the manifest on disk is already the manifest in git, so there is no second
    commit saying the same thing. That is what makes running ``steward new-resident``
    twice a no-op rather than a growing pile of empty commits.
    """
    relative = [str(path.resolve().relative_to(repo.resolve())) for path in paths]
    added = _git(repo, "add", "--", *relative, git=git)
    if not added.ok:
        raise NurseryError(f"git could not stage the declaration: {added.summary()}")
    staged = _git(repo, "diff", "--cached", "--quiet", "--", *relative, git=git)
    if staged.exit_status == 0:
        return None  # Already committed, byte for byte. Converged.
    committed = _git(repo, "commit", "-m", message, "--", *relative, git=git)
    if not committed.ok:
        raise NurseryError(f"git could not commit the declaration: {committed.summary()}")
    head = _git(repo, "rev-parse", "HEAD", git=git)
    return head.stdout.strip() or None


_RETIRED_LINE = re.compile(r"^retired:.*$", re.MULTILINE)

RETIRED_NOTE = """
# Retired by `steward retire`. The manifest and the soul stay here: retirement is a
# lifecycle state, not a deletion, and a village that forgot a resident ever existed
# would be a village that cannot answer what it used to do. A retired resident is
# excluded from the scheduler, the board, delegation, and run-now — and keeps
# validating, so `steward validate` still reads it. Set this to false and commit to
# bring it back; `steward new-resident` puts its container up again.
retired: true
"""


def set_retired(manifest_path: Path, *, retired: bool = True) -> bool:
    """Write ``retired:`` into a manifest by hand, and report whether anything changed.

    A text edit rather than a model round-trip, and deliberately: these files are written
    by people, carry comments explaining choices somebody thought about, and re-dumping
    them through pydantic would silently throw all of that away the first time a resident
    was retired. Retirement changes one line.
    """
    text = manifest_path.read_text(encoding="utf-8")
    replacement = f"retired: {'true' if retired else 'false'}"
    if _RETIRED_LINE.search(text):
        updated = _RETIRED_LINE.sub(replacement, text, count=1)
    elif retired:
        updated = text.rstrip("\n") + "\n" + RETIRED_NOTE
    else:
        updated = text.rstrip("\n") + f"\n\n{replacement}\n"
    if updated == text:
        return False
    manifest_path.write_text(updated, encoding="utf-8")
    return True


# --------------------------------------------------------------------------------------
# what each stage came to
# --------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class DeclareStage:
    """What the declare stage did to the repo. Every field is something that happened."""

    resident_id: str
    manifest_path: Path
    soul_path: Path
    #: True when this run wrote the declaration; False when it was already there and
    #: matched, which is the converged case rather than an error.
    written: bool
    commit: str | None = None
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        """Return the JSON view of the declare stage."""
        return {
            "resident": self.resident_id,
            "manifest_path": str(self.manifest_path),
            "soul_path": str(self.soul_path),
            "written": self.written,
            "commit": self.commit,
            "note": self.note,
        }


@dataclass(frozen=True, slots=True)
class ProvisionStage:
    """What the provision stage put on the host, and what it ran there."""

    target: DeployTarget
    files: tuple[str, ...]
    compose: str
    #: ``None`` means *not asked*: a dry run does not reach the host, so it cannot know
    #: whether the compose file there differs, and says that rather than guessing.
    compose_changed: bool | None
    #: The names of the variables written into the remote ``.env``. Names only, forever.
    env_keys: tuple[str, ...]
    commands: tuple[tuple[str, ...], ...]
    sent: bool = False

    @property
    def changed(self) -> bool:
        """True when this run actually altered the host."""
        return self.sent

    def to_dict(self) -> dict[str, Any]:
        """Return the JSON view. The compose text is included; the secrets are not."""
        return {
            "target": self.target.to_dict(),
            "files": list(self.files),
            "compose": self.compose,
            "compose_changed": self.compose_changed,
            "env_keys": list(self.env_keys),
            "commands": [render_argv(argv) for argv in self.commands],
            "sent": self.sent,
        }


@dataclass(frozen=True, slots=True)
class RegisterStage:
    """What the scheduler says about the resident it has just been handed.

    There is no registration to perform: a routine is scheduled because a manifest
    declares it, and the manifest is now in the tree. So this stage *verifies* — the same
    ``Scheduler.check()`` ``steward doctor`` runs — and reports the next fire of each
    routine, which is the only honest answer to "is it scheduled?".
    """

    problems: tuple[str, ...] = ()
    fires: tuple[tuple[str, str], ...] = ()

    @property
    def ok(self) -> bool:
        """True when nothing stands between this resident and its next fire."""
        return not self.problems

    def to_dict(self) -> dict[str, Any]:
        """Return the JSON view of the register stage."""
        return {
            "ok": self.ok,
            "problems": list(self.problems),
            "next_fires": [{"routine": routine, "at": at} for routine, at in self.fires],
        }


@dataclass(frozen=True, slots=True)
class NurseryReport:
    """One run of the whole pipeline, or one rehearsal of it."""

    resident_id: str
    declare: DeclareStage
    provision: ProvisionStage | None = None
    register: RegisterStage | None = None
    dry_run: bool = False
    warnings: tuple[str, ...] = ()

    @property
    def changed(self) -> bool:
        """True when this run changed the repo or the host. False means converged."""
        if self.dry_run:
            return False
        return self.declare.written or bool(self.provision and self.provision.changed)

    def to_dict(self) -> dict[str, Any]:
        """Return the JSON view of a whole run, for the API and ``--format json``."""
        return {
            "resident": self.resident_id,
            "dry_run": self.dry_run,
            "changed": self.changed,
            "declare": self.declare.to_dict(),
            "provision": self.provision.to_dict() if self.provision else None,
            "register": self.register.to_dict() if self.register else None,
            "warnings": list(self.warnings),
        }

    def render(self) -> list[str]:
        """Render the plan (or the result) as lines a human reads top to bottom."""
        head = "plan for" if self.dry_run else "raised"
        lines = [f"{head} {self.resident_id}", ""]
        lines += ["declare", f"  {self.declare.manifest_path}", f"  {self.declare.soul_path}"]
        if self.declare.commit:
            lines.append(f"  committed {self.declare.commit[:12]}")
        if self.declare.note:
            lines.append(f"  {self.declare.note}")
        if self.provision is not None:
            lines += ["", "provision", f"  {self.provision.target.describe()}"]
            lines += [f"  file {name}" for name in self.provision.files]
            lines.append(
                "  .env carries " + ", ".join(self.provision.env_keys) + " (values not shown)"
            )
            lines += [f"  $ {render_argv(argv)}" for argv in self.provision.commands]
            if self.provision.compose_changed is None:
                # A rehearsal has not asked the host anything, so there is no diff to show
                # — the whole fragment is printed instead, which is the thing a person
                # actually wants to read before letting this near a machine.
                lines.append("  compose diff not computed: a dry run does not reach the host")
                lines += ["", f"  {COMPOSE_FILENAME}", ""]
                lines += [f"    {line}" for line in self.provision.compose.splitlines()]
            else:
                lines.append(
                    "  compose "
                    + ("re-rendered" if self.provision.compose_changed else "unchanged")
                )
        if self.register is not None:
            lines += ["", "register"]
            lines += [f"  {problem}" for problem in self.register.problems]
            lines += [f"  {routine} fires next at {at}" for routine, at in self.register.fires]
            if not self.register.problems and not self.register.fires:
                lines.append("  no enabled routines; this resident fires nothing")
        lines += [f"warning: {warning}" for warning in self.warnings]
        return lines


@dataclass(frozen=True, slots=True)
class RetireReport:
    """What retiring a resident came to. Every field is something that happened."""

    resident_id: str
    manifest_path: Path
    #: True when this run wrote ``retired: true``; False when it already said so.
    marked: bool
    stopped: bool
    commands: tuple[tuple[str, ...], ...] = ()
    commit: str | None = None
    dry_run: bool = False
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        """Return the JSON view of a retirement."""
        return {
            "resident": self.resident_id,
            "manifest_path": str(self.manifest_path),
            "marked": self.marked,
            "stopped": self.stopped,
            "commands": [render_argv(argv) for argv in self.commands],
            "commit": self.commit,
            "dry_run": self.dry_run,
            "note": self.note,
        }

    def render(self) -> list[str]:
        """Render the retirement as lines a human reads top to bottom.

        The manifest mark comes first and the ``docker compose down`` after it, because
        that is the order retirement actually happens in — ``retired: true`` is what takes
        the resident out of the watchdog, so it has to land before the container goes away,
        or the watchdog would notice the container die and put it straight back. The plan a
        ``--dry-run`` prints reads in that same order (docs/manifest.md).
        """
        head = "plan to retire" if self.dry_run else "retired"
        lines = [f"{head} {self.resident_id}"]
        lines.append(f"  {self.manifest_path}: retired: true")
        lines += [f"  $ {render_argv(argv)}" for argv in self.commands]
        if self.commit:
            lines.append(f"  committed {self.commit[:12]}")
        if self.note:
            lines.append(f"  {self.note}")
        return lines


# --------------------------------------------------------------------------------------
# the pipeline
# --------------------------------------------------------------------------------------

#: The fields of a manifest a :class:`NewResident` can speak for. Convergence is decided
#: on these and only these, so a routine, a budget, or a ``deploy`` block somebody added
#: by hand after the skeleton was written does not read as "your inputs changed".
SPEC_FIELDS: tuple[str, ...] = (
    "id",
    "agent_id",
    "project",
    "summary",
    "soul",
    "charter",
    "skills",
    "memory",
    "routes",
    "app_grants",
    "runner",
)


def _pending_resident(spec: NewResident, residents_dir: Path) -> Resident:
    """Build the resident a rehearsal is about, without writing a byte of it.

    ``--dry-run`` has to render the compose fragment for a resident that does not exist
    yet, and rendering it from anything other than the real manifest model would make the
    plan a drawing of the plan.
    """
    manifest = _manifest_model(spec)
    directory = residents_dir / spec.id
    return Resident(
        path=directory / MANIFEST_FILENAME,
        manifest=manifest,
        soul=SoulDocument(
            path=directory / manifest.soul.file,
            body=(spec.soul_body or DEFAULT_SOUL_BODY),
            voice=(spec.voice or DEFAULT_VOICE),
        ),
    )


def _load_or_refuse(manifest_path: Path, skills_dir: Path | str | None) -> Resident:
    """Load a resident through the ordinary validator, or refuse with its diagnostics."""
    try:
        return load_manifest(manifest_path, skills_dir)
    except ManifestError as exc:
        raise NurseryError(
            f"{manifest_path} does not validate, so steward will not deploy from it: {exc}",
            exc.diagnostics,
        ) from exc


def _spec_differences(wanted: ResidentManifest, existing: ResidentManifest) -> list[str]:
    """Name the fields on which a re-run's inputs disagree with what is already declared."""
    left = wanted.model_dump(mode="json")
    right = existing.model_dump(mode="json")
    return [name for name in SPEC_FIELDS if left.get(name) != right.get(name)]


def _declare(  # noqa: PLR0913 — every collaborator is keyword-only and injectable
    spec: NewResident,
    *,
    residents_dir: Path,
    repo: Path,
    skills_dir: Path | str | None,
    commit: bool,
    dry_run: bool,
    git: PipedRun,
) -> tuple[DeclareStage, Resident]:
    """Run the declare stage: write the two files, read them back, and commit them.

    A resident that is already declared and already matches is **converged**, not an
    error: the whole point of the pipeline being idempotent is that you can re-run
    ``steward new-resident`` after a failed deploy without first deleting anything.

    A resident that is already declared and *does not* match is refused, and this is the
    deliberate half. The skeleton the nursery writes is a starting point — a human then
    writes the soul body, tunes the charter, adds routines — and silently overwriting that
    from a command line full of flags would destroy work nobody asked it to destroy. The
    refusal names the fields that disagree and points at the file to edit.
    """
    directory = residents_dir / spec.id
    manifest_path = directory / MANIFEST_FILENAME
    wanted = _manifest_model(spec)

    if directory.exists():
        existing = _load_or_refuse(manifest_path, skills_dir)
        if existing.retired:
            raise NurseryError(
                f"resident {spec.id!r} is retired; set retired: false in {manifest_path} and "
                f"commit that decision before raising it again — un-retiring a resident is "
                f"something a person should have said out loud in git",
                reason="resident_retired",
            )
        differences = _spec_differences(wanted, existing.manifest)
        if differences:
            raise NurseryError(
                f"resident {spec.id!r} already exists at {directory} and its "
                f"{', '.join(differences)} do not match what you asked for; edit "
                f"{manifest_path} and commit, rather than having a command line overwrite "
                f"a soul somebody wrote"
            )
        stage = DeclareStage(
            resident_id=spec.id,
            manifest_path=manifest_path,
            soul_path=existing.directory / existing.manifest.soul.file,
            written=False,
            note="already declared and unchanged",
        )
        resident = existing
    elif dry_run:
        resident = _pending_resident(spec, residents_dir)
        stage = DeclareStage(
            resident_id=spec.id,
            manifest_path=manifest_path,
            soul_path=resident.soul.path,
            written=False,
            note="would be written; a dry run writes nothing",
        )
    else:
        created = declare_resident(spec, residents_dir)
        resident = created.resident
        stage = DeclareStage(
            resident_id=spec.id,
            manifest_path=created.manifest_path,
            soul_path=created.soul_path,
            written=True,
            note="declared and validated",
        )

    if commit and not dry_run:
        # Before anything is built out of it: the repo is the source of truth, so a
        # provision that fails leaves a commit to revert and a command to re-run.
        stage = replace(
            stage,
            commit=commit_paths(
                repo,
                [stage.manifest_path, stage.soul_path],
                DECLARE_SUBJECT.format(id=spec.id),
                git=git,
            ),
        )
    return stage, resident


def _provision(
    resident: Resident,
    *,
    transport: Transport | None,
    env: Mapping[str, str],
    dry_run: bool,
) -> ProvisionStage:
    """Run the provision stage: render, ship, and bring the container up.

    Idempotent by comparison, not by hope: the bundle is compared to what is already on
    the host file by file, and a bundle that matches is not sent. ``up -d`` is issued
    either way, because it is the only thing here that can converge a container that is
    *down* — reconciling, which is what a second run is for, is not the same as doing
    nothing.
    """
    target = target_for(resident.manifest)
    # A rehearsal reaches no host, so it must not require the emitter environment a real
    # deploy does: `planned_env` names whatever village variables are set and refuses
    # nothing, where `burrow_env` refuses without BURROW_URL (#84). The real run below still
    # goes through `burrow_env`, so a deploy with nowhere to emit is still stopped.
    values = planned_env(env) if dry_run else burrow_env(env)
    compose = render_compose(resident, target)
    conveyance = transport if transport is not None else transport_for(target)
    up = compose_argv(target, "up", "-d")
    plan = (
        conveyance.plan(["mkdir", "-p", target.path]),
        conveyance.plan(["tar", "-xf", "-", "-C", target.path]),
        conveyance.plan(up),
    )

    if dry_run:
        return ProvisionStage(
            target=target,
            files=BUNDLE_NAMES,
            compose=compose,
            compose_changed=None,
            env_keys=tuple(sorted(values)),
            commands=plan,
        )

    files = bundle_for(resident, target, values)
    try:
        changed = bundle_changes(conveyance, files, target.path)
        if changed:
            sent = conveyance.send(files, target.path)
            if not sent.ok:
                raise NurseryError(
                    f"could not put {resident.id}'s bundle on {target.host}: {sent.summary()}; "
                    f"the declaration is committed, so fixing the host and re-running this "
                    f"command picks up exactly where it stopped"
                )
        outcome = conveyance.run(up)
        if not outcome.ok:
            raise NurseryError(f"docker compose up failed on {target.host}: {outcome.summary()}")
    except TransportError as exc:
        raise NurseryError(
            f"cannot reach {target.user}@{target.host}: {exc}; the declaration is committed "
            f"and nothing was changed on the host, so re-run this command once the host is "
            f"back"
        ) from exc

    return ProvisionStage(
        target=target,
        files=tuple(sorted(files)),
        compose=compose,
        compose_changed=COMPOSE_FILENAME in changed,
        env_keys=tuple(sorted(values)),
        commands=plan,
        sent=bool(changed),
    )


def _register(
    resident: Resident,
    *,
    residents_dir: Path,
    skills_dir: Path | str | None,
    now: datetime,
) -> RegisterStage:
    """Run the register stage: check the resident is runnable, and say when it next fires.

    Nothing is written. Routines are read off the manifest by the scheduler on every tick,
    so there is no registry to add a row to — and inventing one would give steward a second
    place where "what is scheduled" is answered, which is one more than the truth survives.
    What this stage buys is the *check*: the runner's binary exists, the journal location is
    writable, every granted skill resolves. That is the same ``check()`` ``steward doctor``
    runs, and running it now means a missing ``claude`` is a message on the deploy rather
    than a routine that silently never happens at 7am.
    """
    scheduled = [
        ScheduledRoutine(resident=resident, routine=routine)
        for routine in resident.manifest.routines
        if routine.enabled
    ]
    engine = Scheduler(
        scheduled,
        state=SchedulerState(path=default_state_path()),
        library=library_for(residents_dir, skills_dir),
        # A rehearsal of the scheduler, never a run of it: no emitter, no real brain, and
        # crucially no `state.save()` anywhere on this path.
        dry_run=True,
    )
    return RegisterStage(
        problems=tuple(engine.check()),
        fires=tuple(
            (item.routine.id, next_fire_after(item.routine, now).isoformat()) for item in scheduled
        ),
    )


def raise_resident(  # noqa: PLR0913 — every knob is keyword-only and independently useful
    spec: NewResident,
    *,
    residents_dir: Path | str,
    repo: Path | str | None = None,
    transport: Transport | None = None,
    env: Mapping[str, str] | None = None,
    skills_dir: Path | str | None = None,
    provision: bool = True,
    commit: bool = True,
    allow_dirty: bool = False,
    dry_run: bool = False,
    git: PipedRun = run_argv,
    now: datetime | None = None,
) -> NurseryReport:
    """Raise a resident: declare it, provision it, and check it is schedulable.

    The one pipeline. ``steward new-resident`` calls it with ``commit=True``;
    ``POST /residents`` calls it with ``commit=False`` and ``provision`` set from the
    request's ``deploy`` flag. Neither has its own copy of any of this.

    ``transport=None`` means "build the ssh transport this resident's manifest addresses",
    which is what both real callers want; tests hand it a
    :class:`steward.deploy.LocalTransport` and the pipeline never knows the difference.
    """
    root = Path(residents_dir)
    checkout = Path(repo) if repo is not None else root.parent
    moment = now or datetime.now(UTC)
    source = env if env is not None else os.environ
    warnings: list[str] = []

    if commit:
        complaint = worktree_complaint(checkout, git=git)
        if complaint and dry_run:
            # A rehearsal on a dirty tree is exactly when you want a rehearsal, so it is
            # printed as a warning rather than refused. The real run still refuses.
            warnings.append(f"{complaint} (a real run would refuse)")
        elif complaint and not allow_dirty:
            raise NurseryError(complaint)
        elif complaint:
            warnings.append(complaint)

    if provision and dry_run and not (source.get(BURROW_URL_ENV) or "").strip():
        # The rehearsal still prints the whole plan (#84); it just says out loud that the
        # real run would refuse until the village address is exported.
        warnings.append(
            f"{BURROW_URL_ENV} is unset, so a real run would refuse to deploy a resident "
            f"with nowhere to emit; export {BURROW_URL_ENV} before running this for real"
        )

    stage, resident = _declare(
        spec,
        residents_dir=root,
        repo=checkout,
        skills_dir=skills_dir,
        commit=commit,
        dry_run=dry_run,
        git=git,
    )
    provisioned = (
        _provision(resident, transport=transport, env=source, dry_run=dry_run)
        if provision
        else None
    )
    registered = _register(resident, residents_dir=root, skills_dir=skills_dir, now=moment)
    return NurseryReport(
        resident_id=spec.id,
        declare=stage,
        provision=provisioned,
        register=registered,
        dry_run=dry_run,
        warnings=tuple(warnings),
    )


def _stop_retired_container(
    resident_id: str, conveyance: Transport, target: DeployTarget, down: Sequence[str]
) -> tuple[bool, str]:
    """Bring a retired resident's container down, once the manifest already says retired.

    Split out of :func:`retire_resident` so the mark-then-stop order reads at a glance:
    by the time this runs the decision is committed, so every failure here says so and
    tells the operator to re-run once the host answers, rather than unwinding the mark.
    """
    try:
        if conveyance.read(target.compose_path) is None:
            return False, f"nothing at {target.path} on {target.host} to stop"
        outcome = conveyance.run(down)
        if not outcome.ok:
            raise NurseryError(
                f"the manifest now says {resident_id} is retired, and that is committed, but "
                f"docker compose down failed on {target.host}: {outcome.summary()}; re-run "
                f"`steward retire {resident_id}` once the host answers"
            )
    except TransportError as exc:
        raise NurseryError(
            f"the manifest now says {resident_id} is retired, and that is committed, but "
            f"{target.user}@{target.host} could not be reached: {exc}; re-run "
            f"`steward retire {resident_id}` once the host is back"
        ) from exc
    return True, ""


def retire_resident(  # noqa: PLR0913 — every knob is keyword-only and independently useful
    resident_id: str,
    *,
    residents_dir: Path | str,
    repo: Path | str | None = None,
    transport: Transport | None = None,
    skills_dir: Path | str | None = None,
    commit: bool = True,
    deploy: bool = True,
    allow_dirty: bool = False,
    dry_run: bool = False,
    git: PipedRun = run_argv,
) -> RetireReport:
    """Retire a resident: mark it retired in git, then stop and remove its container.

    **Marked before stopped**, and the order is the whole safety argument. ``retired:
    true`` is what takes this resident out of the scheduler, the board, delegation and
    run-now — and out of the watchdog, which would otherwise notice the container going
    away and dutifully restart it. Stopping first and marking second leaves a window in
    which steward is fighting itself.

    ``deploy=False`` marks and commits the manifest but reaches no host — the counterpart
    to ``new-resident``'s ``--no-deploy``, for the resident whose host is already gone (or
    was never steward's to stop). The container, if there is one, is left exactly as it is;
    the resident stops taking work the moment the mark is committed either way.

    Nothing is emitted. A retired resident leaves the village the honest way: it stops
    emitting, and burrow's existing projection rules do the rest. Forging a
    ``session_ended`` on its behalf would be steward putting words in a dead resident's
    mouth, which is precisely the thing the village exists not to do.
    """
    root = Path(residents_dir)
    checkout = Path(repo) if repo is not None else root.parent
    manifest_path = root / resident_id / MANIFEST_FILENAME
    if not manifest_path.is_file():
        known = ", ".join(sorted(p.name for p in root.iterdir() if p.is_dir())) or "none"
        close = closest_match(resident_id, [p.name for p in root.iterdir() if p.is_dir()])
        hint = f" — did you mean {close!r}?" if close else ""
        raise NurseryError(f"no resident {resident_id!r} under {root}{hint} (residents: {known})")

    resident = _load_or_refuse(manifest_path, skills_dir)
    target = target_for(resident.manifest)
    conveyance = transport if transport is not None else transport_for(target)
    down = compose_argv(target, "down", "--remove-orphans")
    plan = (conveyance.plan(down),) if deploy else ()

    if dry_run:
        if not deploy:
            note = "a dry run stops nothing and commits nothing; --no-deploy reaches no host"
        elif resident.retired:
            note = "already retired; a real run would only reconcile the container"
        else:
            note = "a dry run stops nothing and commits nothing"
        return RetireReport(
            resident_id=resident_id,
            manifest_path=manifest_path,
            marked=not resident.retired,
            stopped=False,
            commands=plan,
            dry_run=True,
            note=note,
        )

    if commit:
        complaint = worktree_complaint(checkout, git=git)
        if complaint and not allow_dirty:
            raise NurseryError(complaint)

    marked = set_retired(manifest_path)
    if marked:
        # Read it back through the ordinary validator, exactly as declare does: a manifest
        # steward edited and broke would be a resident nobody could load again.
        try:
            _load_or_refuse(manifest_path, skills_dir)
        except NurseryError:
            set_retired(manifest_path, retired=False)
            raise

    sha = (
        commit_paths(
            checkout,
            [manifest_path],
            RETIRE_SUBJECT.format(id=resident_id),
            git=git,
        )
        if commit
        else None
    )

    if deploy:
        stopped, note = _stop_retired_container(resident_id, conveyance, target, down)
    else:
        stopped, note = (
            False,
            "deploy skipped: the manifest is marked and the host was left untouched",
        )

    return RetireReport(
        resident_id=resident_id,
        manifest_path=manifest_path,
        marked=marked,
        stopped=stopped,
        commands=plan,
        commit=sha,
        note=note or ("retired" if marked else "already retired; container reconciled"),
    )
