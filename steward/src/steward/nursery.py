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

## Two doors onto the same three stages

``steward new-resident`` assembles a resident from flags, and refuses to converge those
flags onto a manifest a person has since edited — silently overwriting a soul somebody
wrote is not something a command line should be able to do. That refusal is right, and it
used to be a dead end: a manifest carrying a route, an app grant, or a ``runner.placement``
can never match a spec built from flags that do not exist, so the fleet's oldest residents
had no supported way onto the nursery path at all (warren#270).

:func:`provision_resident` is the other door. It skips declare entirely — the declaration
is already there, written by a person and committed — and runs provision and register
against ``residents/<id>/manifest.yaml`` as the source of truth. It is
``steward retire <id>``'s exact counterpart: same argument, same source of truth, opposite
direction. Nothing is written into the repo, so nothing is committed and there is no
dirty-worktree refusal to make; a declaration whose bytes are in no commit is named in a
warning instead, because provision does not own that commit and cannot make it.

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
from collections.abc import Callable, Mapping, Sequence
from contextlib import AbstractContextManager, nullcontext
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal
from uuid import UUID, uuid4

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from steward import events as ev
from steward.deploy import (
    BUNDLE_NAMES,
    CHRONICLE_URL_ENV,
    COMPOSE_FILENAME,
    DeployTarget,
    Transport,
    TransportError,
    bundle_changes,
    bundle_for,
    compose_argv,
    emitter_env,
    planned_env,
    render_argv,
    render_compose,
    target_for,
    transport_for,
)
from steward.manifest import (
    DEFAULT_JOURNAL_DIR,
    MANIFEST_FILENAME,
    VILLAGE_HOME_MAX,
    VILLAGE_HOME_MIN,
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
    SkillGrantInput,
    SoulDocument,
    SoulIdentity,
    ToolGrant,
    WorkspacePath,
    closest_match,
    load_manifest,
    validate_manifest,
    validate_path,
)
from steward.runners import CommandOutcome, PipedRun, run_argv
from steward.scheduler import (
    ScheduledRoutine,
    Scheduler,
    SchedulerState,
    default_state_path,
    next_fire_after,
)
from steward.skills import library_for, redundant_grants

__all__ = [
    "COMMIT_FAILED",
    "WORKTREE_REFUSED",
    "CommitIdentity",
    "CreatedResident",
    "DeclareStage",
    "NewResident",
    "NurseryError",
    "NurseryReport",
    "ProvisionStage",
    "RegisterStage",
    "RetireReport",
    "declare_resident",
    "provision_resident",
    "raise_resident",
    "retire_resident",
]

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
    # ``SkillGrantInput``, not ``SkillGrant``, for the same reason ``Resident.skills``
    # uses it: the bare-string spelling (`"daily-summary"`) is half of this field's
    # public grammar, and only the annotated input type puts it in the JSON Schema.
    # With the plain model the exported document said this had to be an object while
    # the API happily took a list of names — and townhall's nursery form sends names
    # (warren#321).
    skills: list[SkillGrantInput] = Field(default_factory=list, description="Granted capabilities.")
    memory: Memory | None = Field(default=None, description="Memory location; derived if absent.")
    routes: list[Route] = Field(default_factory=list, description="Declared inbound channels.")
    app_grants: list[AppGrant] = Field(default_factory=list, description="Declared app access.")
    tools: ToolGrant = Field(
        # An empty list, not ``unrestricted``. Every other capability dimension the nursery
        # defaults is defaulted to *nothing granted*, and tools is the dimension that rule
        # was written for: a resident declared without a word about its tools should arrive
        # able to touch nothing and be widened deliberately, in a diff somebody reads.
        default_factory=lambda: ToolGrant([]),
        description="Tools a session may reach; defaults to none until declared.",
    )
    workspace: list[WorkspacePath] = Field(
        default_factory=list,
        description="Absolute directories a session may reach beyond its working directory.",
    )
    runner: Runner = Field(default_factory=Runner, description="Which brain this resident runs on.")
    soul_body: str | None = Field(default=None, description="Opening paragraph of soul.md.")
    voice: str | None = Field(default=None, description="The soul's ## Voice section.")

    def resolved_agent_id(self, uid: UUID) -> str:
        """Return the explicit join key, or derive the permanent one from ``uid``."""
        return self.agent_id or f"resident:{uid}"

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
            "uid": str(self.resident.manifest.uid),
            "directory": str(self.directory),
            "manifest_path": str(self.manifest_path),
            "soul_path": str(self.soul_path),
            "agent_id": self.resident.manifest.agent_id,
            "project": self.resident.manifest.project,
        }


def _soul_document(spec: NewResident, uid: UUID, agent_id: str | None) -> str:
    """Render ``soul.md``: frontmatter that agrees with the manifest, then a body."""
    frontmatter: dict[str, Any] = {"uid": str(uid)}
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


def _next_home(residents_dir: Path) -> int:
    """Return the lowest plot not claimed by a valid resident declaration."""
    used = {resident.manifest.home for resident in validate_path(residents_dir).residents}
    try:
        return next(
            home for home in range(VILLAGE_HOME_MIN, VILLAGE_HOME_MAX + 1) if home not in used
        )
    except StopIteration as exc:
        raise NurseryError(
            "cannot declare a resident: all village homes 0 through 7 are claimed"
        ) from exc


def _manifest_model(spec: NewResident, *, home: int, uid: UUID | None = None) -> ResidentManifest:
    """Bind the request into a manifest model, so an invalid one never reaches disk."""
    uid = uuid4() if uid is None else uid
    try:
        return ResidentManifest(
            uid=uid,
            id=spec.id,
            home=home,
            agent_id=spec.resolved_agent_id(uid),
            project=spec.project,
            summary=spec.summary,
            soul=SoulIdentity(name=spec.name, char=spec.char, accent=spec.accent, role=spec.role),
            charter=spec.charter,
            skills=spec.skills,
            memory=spec.resolved_memory(),
            routes=spec.routes,
            app_grants=spec.app_grants,
            tools=spec.tools,
            workspace=spec.workspace,
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

    manifest = _manifest_model(spec, home=_next_home(root))
    payload = manifest.model_dump(mode="json", exclude_none=True)
    # An ordinary resident declares no `deploy` block at all — docs/manifest.md says so, and
    # the dxp2800 defaults (image, `command: [sleep, infinity]`) fill it in. The bare model
    # default dumps as `deploy: {command: [], mounts: []}`, which reads as "this container
    # runs nothing and mounts nothing", the opposite of the documented default. Drop it so
    # the skeleton says what it means.
    if payload.get("deploy") == {"command": [], "mounts": []}:
        del payload["deploy"]

    directory.mkdir(parents=True)
    manifest_path = directory / MANIFEST_FILENAME
    soul_path = directory / manifest.soul.file
    try:
        manifest_path.write_text(
            yaml.safe_dump(payload, sort_keys=False, allow_unicode=True), encoding="utf-8"
        )
        soul_path.write_text(
            _soul_document(spec, manifest.uid, manifest.agent_id), encoding="utf-8"
        )
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

#: The credential retirement deliberately leaves behind, said out loud rather than left as
#: a silence. ``claude/`` is bind-mounted to ``/root/.claude`` and holds whatever a
#: ``docker exec … claude`` login wrote; steward created the empty directory and never
#: wrote its contents, and a provision does not restore them — so deleting it would make
#: the documented way back (``retired: false``, re-provision) silently require a re-login.
#: An operator who wants it gone gets told it is there instead of discovering it later.
CLAUDE_LOGIN_REMAINS = (
    "left in place: claude/ still holds any login a `docker exec … claude` wrote — "
    "steward did not write it and a re-provision does not restore it, so removing it is "
    "yours to decide"
)

#: How many dirty paths a refusal names before it starts counting instead.
DIRTY_SHOWN = 5

#: Why a retirement's commit could not be made safely, for a caller that needs a code
#: rather than a paragraph. One name for both shapes :func:`worktree_complaint` reports —
#: a worktree carrying somebody else's half-finished afternoon, and a tree with no git
#: behind it at all — because the answer is the same either way: steward will not commit
#: here, and the message says which of the two it is.
WORKTREE_REFUSED = "worktree_refused"

#: Why a commit steward tried to make did not happen: git refused the stage or the commit
#: itself. Named because the answer a caller needs is *which side of the commit it stopped
#: on* — a retirement that fails here has already written ``retired: true`` to the file and
#: has nothing in git saying so, which is a different situation from one that never started.
#: The same name :data:`steward.api.WRITE_STATUS` already gives it, so one code means one
#: thing across every door.
COMMIT_FAILED = "commit_failed"


def _git(repo: Path, *args: str, git: PipedRun = run_argv) -> CommandOutcome:
    """Run one git command against a checkout. ``-C`` rather than a chdir, always."""
    return git(["git", "-C", str(repo), *args])


def _dirty_names(status: CommandOutcome) -> list[str]:
    """Return the path half of each ``git status --porcelain`` line, in git's own order.

    One place knows that porcelain puts two status letters and a space before the path, so
    the whole-worktree refusal and the one-resident warning cannot come to disagree about
    where a path starts.
    """
    return [line[3:] for line in status.stdout.splitlines() if line.strip()]


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
    dirty = _dirty_names(status)
    if not dirty:
        return None
    shown = ", ".join(dirty[:DIRTY_SHOWN])
    more = f" (+{len(dirty) - DIRTY_SHOWN} more)" if len(dirty) > DIRTY_SHOWN else ""
    return (
        f"the worktree at {repo} has uncommitted changes ({shown}{more}); commit or stash "
        f"them so a failed deploy leaves exactly one commit to revert, or pass "
        f"--allow-dirty to go ahead anyway"
    )


def path_complaint(repo: Path, path: Path, *, git: PipedRun = run_argv) -> str | None:
    """Name uncommitted bytes in the file retirement will commit, ignoring other work."""
    outcome = _git(repo, "rev-parse", "--is-inside-work-tree", git=git)
    if not outcome.ok:
        return (
            f"{repo} is not a git checkout, so there is nothing to commit the retirement "
            "into; point --repo at the steward repo or pass --no-commit"
        )
    try:
        relative = path.resolve().relative_to(repo.resolve())
    except ValueError:
        return f"{path} is outside the git checkout at {repo}, so steward cannot commit it"
    status = _git(repo, "status", "--porcelain", "--", str(relative), git=git)
    if not status.ok:
        return f"git could not inspect {path}: {status.summary()}"
    dirty = _dirty_names(status)
    if not dirty:
        return None
    return (
        f"{', '.join(dirty)} has uncommitted changes; commit or discard them before "
        "retiring this resident"
    )


@dataclass(frozen=True, slots=True)
class CommitIdentity:
    """Who a commit steward makes is authored and committed by.

    Both, not just the author. ``--author`` alone leaves the *committer* to whatever
    ``git config`` the process happens to see, and a server is exactly the place where that
    is unset — a steward on the NAS would stage a manifest perfectly and then fail at the
    commit with "Please tell me who you are". Passing the identity as command-line config
    makes the commit work on a machine with no git identity at all, which is the machine
    this is for.
    """

    name: str
    email: str

    @property
    def spec(self) -> str:
        """Return the ``Name <email>`` form git prints in a log."""
        return f"{self.name} <{self.email}>"

    def config_args(self) -> tuple[str, ...]:
        """Return the ``-c`` flags that make this identity both author and committer."""
        return ("-c", f"user.name={self.name}", "-c", f"user.email={self.email}")


def commit_paths(
    repo: Path,
    paths: Sequence[Path],
    message: str,
    *,
    identity: CommitIdentity | None = None,
    git: PipedRun = run_argv,
) -> str | None:
    """Stage and commit exactly these paths. Returns the new commit, or ``None``.

    ``None`` means *nothing to commit*, and it is the ordinary answer on a converged
    re-run: the manifest on disk is already the manifest in git, so there is no second
    commit saying the same thing. That is what makes running ``steward new-resident``
    twice a no-op rather than a growing pile of empty commits.

    ``identity`` names who the commit is by. ``None`` keeps the terminal's behaviour — the
    person's own git config, because ``steward new-resident`` is something a person ran —
    while the write API passes one, because there is nobody at the keyboard to inherit it
    from.

    Exactly these paths, always: a commit here can never pick up an unrelated file somebody
    was in the middle of editing, which is what makes committing from a long-running server
    safe at all.
    """
    relative = [str(path.resolve().relative_to(repo.resolve())) for path in paths]
    added = _git(repo, "add", "--", *relative, git=git)
    if not added.ok:
        raise NurseryError(
            f"git could not stage the declaration: {added.summary()}", reason=COMMIT_FAILED
        )
    staged = _git(repo, "diff", "--cached", "--quiet", "--", *relative, git=git)
    if staged.exit_status == 0:
        return None  # Already committed, byte for byte. Converged.
    config = identity.config_args() if identity is not None else ()
    committed = _git(repo, *config, "commit", "-m", message, "--", *relative, git=git)
    if not committed.ok:
        raise NurseryError(
            f"git could not commit the declaration: {committed.summary()}", reason=COMMIT_FAILED
        )
    head = _git(repo, "rev-parse", "HEAD", git=git)
    return head.stdout.strip() or None


_RETIRED_LINE = re.compile(r"^retired:.*$", re.MULTILINE)

RETIRED_NOTE = """
# Retired by `steward retire`. The manifest and the soul stay here: retirement is a
# lifecycle state, not a deletion, and a village that forgot a resident ever existed
# would be a village that cannot answer what it used to do. A retired resident is
# excluded from the scheduler, the board, delegation, and run-now — and keeps
# validating, so `steward validate` still reads it. Set this to false and commit to
# bring it back; `steward provision <id>` puts its container up again.
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
    #: Which door this run came through. The stages are the same either way — this only
    #: decides whether the report says *raised* or *provisioned*, and a command called
    #: `provision` that announced a resident "raised" would be describing a declare stage
    #: that did not happen.
    act: Literal["raise", "provision"] = "raise"

    @property
    def verb(self) -> str:
        """Return the past tense of what this run did, for the line a human reads first."""
        return "raised" if self.act == "raise" else "provisioned"

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
            "act": self.act,
            "declare": self.declare.to_dict(),
            "provision": self.provision.to_dict() if self.provision else None,
            "register": self.register.to_dict() if self.register else None,
            "warnings": list(self.warnings),
        }

    def render(self) -> list[str]:
        """Render the plan (or the result) as lines a human reads top to bottom."""
        head = "plan for" if self.dry_run else self.verb
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
    #: True when this run found a ``.env`` holding ``CHRONICLE_TOKEN`` on the host and removed
    #: it. **False when there was nothing to remove** — a host that never held a deployment,
    #: or one an earlier retirement already scrubbed — because "the token is gone" and "this
    #: run took it away" are different sentences and only the second is this field's
    #: (steward #157).
    scrubbed: bool = False
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
            "scrubbed": self.scrubbed,
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
        if self.commands:
            # Gated on having a host plan rather than on ``scrubbed``, so a ``--dry-run``
            # says it too. A rehearsal is exactly where an operator is deciding whether
            # this retirement needs a manual step, and it is the one run that never sets
            # ``scrubbed``.
            lines.append(f"  {CLAUDE_LOGIN_REMAINS}")
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
    manifest = _manifest_model(spec, home=_next_home(residents_dir))
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


def _load_or_refuse(
    manifest_path: Path, skills_dir: Path | str | None, *, reason: str | None = None
) -> Resident:
    """Load a resident through the ordinary validator, or refuse with its diagnostics.

    ``reason`` is opt-in and defaults to unset, because the callers that had this refusal
    before this parameter existed answer it in prose and would change their error codes if
    it started arriving named. The provision path asks for a name because its API twin maps
    the reason to a status and must not have to guess one.
    """
    try:
        return load_manifest(manifest_path, skills_dir)
    except ManifestError as exc:
        raise NurseryError(
            f"{manifest_path} does not validate, so steward will not deploy from it: {exc}",
            exc.diagnostics,
            reason=reason,
        ) from exc


def _declared_manifest(residents_dir: Path, resident_id: str) -> Path:
    """Return the path of a declared manifest, or refuse naming the id you probably meant.

    Shared by the two commands that take an id rather than a description —
    ``steward provision`` and ``steward retire`` — so a typo gets the same answer whichever
    direction you were going.
    """
    manifest_path = residents_dir / resident_id / MANIFEST_FILENAME
    if manifest_path.is_file():
        return manifest_path
    names = [path.name for path in residents_dir.iterdir() if path.is_dir()]
    known = ", ".join(sorted(names)) or "none"
    close = closest_match(resident_id, names)
    hint = f" — did you mean {close!r}?" if close else ""
    raise NurseryError(
        f"no resident {resident_id!r} under {residents_dir}{hint} (residents: {known})",
        reason="unknown_resident",
    )


def _refuse_retired_resident(resident: Resident) -> None:
    """Refuse to build a container for a resident the manifest says has stopped.

    Both doors, one refusal: coming back is a person's decision written into the file and
    committed, and neither a command line full of flags nor an id on its own is that
    person saying so.
    """
    if not resident.retired:
        return
    raise NurseryError(
        f"resident {resident.id!r} is retired; set retired: false in {resident.path} and "
        f"commit that decision before raising it again — un-retiring a resident is "
        f"something a person should have said out loud in git",
        reason="resident_retired",
    )


def _uncommitted_complaint(
    repo: Path, paths: Sequence[Path], *, git: PipedRun = run_argv
) -> str | None:
    """Name this resident's files that are in no commit, or ``None`` when they all are.

    A warning and never a refusal, because provision does not write the declaration and
    so cannot commit it either — refusing would leave the operator with a command that
    tells them to go and do something it will not do for them. What it can do is refuse to
    ship in silence: a container built from bytes that are in no commit is a container
    nobody can turn back into a diff.

    Scoped to this resident's own files, not the whole worktree: ``new-resident`` refuses
    on a dirty tree because its own commit is what a failed deploy leaves to revert, and
    there is no such commit here — somebody else's half-finished afternoon is none of
    provision's business. A tree with no git behind it says nothing at all: that is a
    deployment topology, not a mistake.
    """
    inside = _git(repo, "rev-parse", "--is-inside-work-tree", git=git)
    if not inside.ok:
        return None
    try:
        relative = [str(path.resolve().relative_to(repo.resolve())) for path in paths]
    except ValueError:
        # ``--repo`` names a checkout the residents tree is not inside. Not a crash and not
        # a silence: git genuinely cannot answer the question, and which checkout the
        # declaration belongs to is the thing the operator got wrong.
        return (
            f"{paths[0]} is not inside the checkout at {repo}, so steward cannot tell "
            f"whether this declaration is committed; point --repo at the checkout the "
            f"residents tree lives in"
        )
    status = _git(repo, "status", "--porcelain", "--", *relative, git=git)
    if not status.ok:
        return None
    dirty = sorted({name.strip() for name in _dirty_names(status)})
    if not dirty:
        return None
    return (
        f"{', '.join(dirty)} is not committed, so this container is built from bytes that "
        f"are in no commit; commit the declaration so the running resident can be turned "
        f"back into a diff"
    )


def _redundant_grants_warning(
    grants: Sequence[SkillGrant], residents_dir: Path, skills_dir: Path | str | None
) -> str | None:
    """Name the granted skills every resident already holds, or ``None``.

    Not a refusal: the effective set is the same either way, and the resident is perfectly
    valid. But a grant that adds nothing is a line somebody wrote believing it did
    something, and the moment it can be said usefully is the one where they are declaring
    or deploying it (warren#90).
    """
    already_held = redundant_grants(
        [grant.id for grant in grants], library_for(residents_dir, skills_dir)
    )
    if not already_held:
        return None
    return (
        f"already in the default set every resident holds, so granting them adds "
        f"nothing: {', '.join(already_held)}"
    )


def _no_village_warning(source: Mapping[str, str]) -> str | None:
    """Say that a real run would refuse for want of a village address, or ``None``.

    A rehearsal still prints the whole plan (#84); it just says out loud that the real run
    stops here, because a container with nowhere to emit is a resident that never appears
    in the village at all.
    """
    if (source.get(CHRONICLE_URL_ENV) or "").strip():
        return None
    return (
        f"{CHRONICLE_URL_ENV} is unset, so a real run would refuse to deploy a resident "
        f"with nowhere to emit; export {CHRONICLE_URL_ENV} before running this for real"
    )


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
    if directory.exists():
        existing = _load_or_refuse(manifest_path, skills_dir)
        wanted = _manifest_model(spec, home=existing.manifest.home, uid=existing.manifest.uid)
        _refuse_retired_resident(existing)
        differences = _spec_differences(wanted, existing.manifest)
        if differences:
            # Named, and then pointed somewhere. The refusal is right — a command line must
            # not overwrite a soul somebody wrote — but on its own it was a dead end for
            # every manifest carrying a field no flag can say (warren#270), so it names the
            # door that does work.
            raise NurseryError(
                f"resident {spec.id!r} already exists at {directory} and its "
                f"{', '.join(differences)} do not match what you asked for; edit "
                f"{manifest_path} and commit, rather than having a command line overwrite "
                f"a soul somebody wrote — then provisioning from the declaration itself "
                f"(`steward provision {spec.id}`, or POST /residents/{spec.id}/provision) "
                f"builds the manifest you wrote, exactly as it stands"
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
    # nothing, where `emitter_env` refuses without a village URL (#84). The real run below
    # still goes through `emitter_env`, so a deploy with nowhere to emit is still stopped.
    values = planned_env(env) if dry_run else emitter_env(env)
    compose = render_compose(resident, target, env)
    conveyance = transport if transport is not None else transport_for(target, env)
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

    files = bundle_for(resident, target, values, host_env=env)
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

    warnings += [
        warning
        for warning in (
            _redundant_grants_warning(spec.skills, root, skills_dir),
            _no_village_warning(source) if provision and dry_run else None,
        )
        if warning
    ]

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


def provision_resident(  # noqa: PLR0913 — every knob is keyword-only and independently useful
    resident_id: str,
    *,
    residents_dir: Path | str,
    repo: Path | str | None = None,
    transport: Transport | None = None,
    env: Mapping[str, str] | None = None,
    skills_dir: Path | str | None = None,
    dry_run: bool = False,
    git: PipedRun = run_argv,
    now: datetime | None = None,
) -> NurseryReport:
    """Build a resident from the manifest a person wrote, and check it is schedulable.

    The declare stage is *already done* — that is the whole difference. ``new-resident``
    assembles a manifest from flags and refuses to converge them onto a file somebody has
    since edited; this reads ``residents/<id>/manifest.yaml`` as the source of truth and
    runs the other two stages against it, so a route, an app grant or a
    ``runner.placement`` — none of which any flag can say — is deployed exactly as
    declared rather than being the reason a deploy is impossible (warren#270).

    It writes nothing into the repo, which is why there is no ``commit`` and no
    ``--allow-dirty`` here: there is no commit for a failed deploy to leave behind, so
    there is nothing for a dirty-worktree refusal to protect. A declaration whose bytes are
    in no commit is a *warning* naming the files, because that is the honest thing a
    command which does not own the commit can say.

    Returns the same :class:`NurseryReport` ``raise_resident`` returns, with the declare
    stage reporting what it found rather than what it wrote — so ``--dry-run``,
    ``--format json`` and every reader of the report see one shape from both doors.
    """
    root = Path(residents_dir)
    checkout = Path(repo) if repo is not None else root.parent
    moment = now or datetime.now(UTC)
    source = env if env is not None else os.environ

    manifest_path = _declared_manifest(root, resident_id)
    resident = _load_or_refuse(manifest_path, skills_dir, reason="declaration_invalid")
    _refuse_retired_resident(resident)
    soul_path = resident.directory / resident.manifest.soul.file

    warnings = [
        warning
        for warning in (
            _uncommitted_complaint(checkout, [manifest_path, soul_path], git=git),
            _redundant_grants_warning(resident.manifest.skills, root, skills_dir),
            _no_village_warning(source) if dry_run else None,
        )
        if warning
    ]

    stage = DeclareStage(
        resident_id=resident.id,
        manifest_path=manifest_path,
        soul_path=soul_path,
        written=False,
        note="already declared; provisioned from the manifest itself",
    )
    return NurseryReport(
        resident_id=resident.id,
        declare=stage,
        provision=_provision(resident, transport=transport, env=source, dry_run=dry_run),
        register=_register(resident, residents_dir=root, skills_dir=skills_dir, now=moment),
        dry_run=dry_run,
        warnings=tuple(warnings),
        act="provision",
    )


def scrubbed_paths(target: DeployTarget) -> tuple[str, ...]:
    """Name the two host files retirement removes, through the target's own properties.

    The rule behind the list: **steward removes on retire exactly what steward rewrites on
    provision** (steward #157). ``.env`` holds ``CHRONICLE_TOKEN``, and a village ingest token
    belonging to a resident that is no longer allowed to act had been sitting on the NAS
    indefinitely with nothing in the retire report mentioning it. The compose file goes with
    it, and not merely because it is inert without the ``.env``: ``CHRONICLE_URL`` is
    interpolated as ``${CHRONICLE_URL:?…}``, so a compose file left beside a removed ``.env``
    would make the *next* ``docker compose down`` fail on a variable rather than report an
    already-stopped container. Both are written again, byte for byte, by the next provision,
    so removing them costs the documented way back nothing.

    :attr:`~steward.deploy.DeployTarget.env_path` and ``.compose_path`` already answer
    "where does this land on the host", and ``compose_argv`` and the stop already address
    the compose file through them. Joining the same paths by hand here would leave the
    argv and the error message that names it free to drift apart from each other and from
    the deploy that wrote them.
    """
    return (target.env_path, target.compose_path)


def scrub_argv(target: DeployTarget) -> tuple[str, ...]:
    """Build the argv that removes a retired resident's generated files from the host.

    ``-f`` so a re-run over an already-scrubbed host succeeds instead of erroring on a
    file it deliberately removed last time. The paths come from ``deploy.path``, which
    :data:`steward.manifest.REMOTE_PATH_PATTERN` has already refused whitespace and shell
    metacharacters in — the same argument every other remote argv in this package rests on.
    """
    return ("rm", "-f", *scrubbed_paths(target))


def _scrub_host(
    resident_id: str, conveyance: Transport, target: DeployTarget, scrub: Sequence[str]
) -> bool:
    """Remove the token and the compose file, after the container is already down.

    **After**, not before: ``docker compose down`` reads the ``.env`` beside the compose
    file, and ``CHRONICLE_URL`` is interpolated as ``${CHRONICLE_URL:?…}``, so scrubbing first
    would make the stop fail on a missing variable.
    """
    left_behind = ", ".join(scrubbed_paths(target))
    # Asked *before* the removal, because ``rm -f`` cannot tell "removed it" from "there
    # was nothing here" — and a report that says `scrubbed` over a host that never held a
    # deployment is exactly the false assurance this whole change exists to remove. The
    # question is the token's file specifically: it is the one worth being sure about.
    held_a_token = conveyance.exists(target.env_path)
    try:
        outcome = conveyance.run(scrub)
    except TransportError as exc:
        # Wrapped for the same reason the stop wraps it: `steward retire` answers a
        # NurseryError with a message and a non-zero exit, and a raw TransportError from
        # a connection that died between the stop and the removal would be a traceback.
        raise NurseryError(
            f"{resident_id} is retired and its container is stopped, but "
            f"{target.user}@{target.host} could not be reached to remove its credentials: "
            f"{exc}; re-run `steward retire {resident_id}` once the host is back, or "
            f"remove {left_behind} by hand — the .env holds CHRONICLE_TOKEN"
        ) from exc
    if not outcome.ok:
        raise NurseryError(
            f"{resident_id} is retired and its container is stopped, but the credentials "
            f"could not be removed from {target.user}@{target.host}: {outcome.summary()}; "
            f"remove {left_behind} by hand — the .env holds CHRONICLE_TOKEN"
        )
    return held_a_token


def _stop_retired_container(
    resident_id: str, conveyance: Transport, target: DeployTarget, down: Sequence[str]
) -> tuple[bool, str]:
    """Bring a retired resident's container down, once the manifest already says retired.

    Split out of :func:`retire_resident` so the mark-then-stop order reads at a glance:
    by the time this runs the decision is committed, so every failure here says so and
    tells the operator to re-run once the host answers, rather than unwinding the mark.
    """
    try:
        # ``exists``, not ``read``: this only ever asked whether there is a deployment
        # here, and ``cat`` answers 1 both for a file that is missing and for one it may
        # not open. Reading the second as the first would report a resident retired with
        # its container still running, which is the bug this function is being fixed for
        # (steward #136).
        if not conveyance.exists(target.compose_path):
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


def retire_resident(  # noqa: C901, PLR0913 — staged lifecycle; collaborators are explicit
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
    identity: CommitIdentity | None = None,
    git: PipedRun = run_argv,
    resident_dirty_only: bool = False,
    expected_revision: str | None = None,
    revision_of: Callable[[Path], str] | None = None,
    durable_guard: AbstractContextManager[Any] | None = None,
    emitter: ev.Emitter | None = None,
) -> RetireReport:
    """Retire a resident: mark it retired in git, then stop and remove its container.

    **Marked before stopped**, and the order is the whole safety argument. ``retired:
    true`` is what takes this resident out of the scheduler, the board, delegation and
    run-now — and out of the watchdog, which would otherwise notice the container going
    away and dutifully restart it. Stopping first and marking second leaves a window in
    which steward is fighting itself.

    ``identity`` names who git records as having made the retirement commit. ``None`` keeps
    the terminal's behaviour — the person's own git config, because ``steward retire`` is
    something a person ran — while :data:`POST /residents/{id}/retire <steward.api>` passes
    one, because a server has nobody at the keyboard to inherit an identity from and would
    otherwise stage the mark perfectly and then fail on "Please tell me who you are".

    ``deploy=False`` marks and commits the manifest but reaches no host — the counterpart
    to ``new-resident``'s ``--no-deploy``, for the resident whose host is already gone (or
    was never steward's to stop). The container, if there is one, is left exactly as it is;
    the resident stops taking work the moment the mark is committed either way.

    Once the durable mark is committed, Steward emits ``resident_retired`` under the
    resident's declared identity before touching the host. This is Steward's own lifecycle
    fact, not a forged ``session_ended`` on the resident's behalf.
    """
    root = Path(residents_dir)
    checkout = Path(repo) if repo is not None else root.parent
    manifest_path = _declared_manifest(root, resident_id)
    if dry_run:
        resident = _load_or_refuse(manifest_path, skills_dir, reason="declaration_invalid")
        target = target_for(resident.manifest)
        conveyance = transport if transport is not None else transport_for(target)
        down = compose_argv(target, "down", "--remove-orphans")
        scrub = scrub_argv(target)
        plan = (conveyance.plan(down), conveyance.plan(scrub)) if deploy else ()
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

    with durable_guard if durable_guard is not None else nullcontext():
        # Re-read the bytes under the same checkout lock that protects revision, dirt,
        # mutation, and commit. Host and Chronicle I/O deliberately happen after release.
        resident = _load_or_refuse(manifest_path, skills_dir, reason="declaration_invalid")
        if expected_revision is not None and (
            revision_of is None or revision_of(manifest_path) != expected_revision
        ):
            raise NurseryError(
                "the resident declaration changed after rehearsal; rehearse the current plan",
                reason="stale_retirement_plan",
            )
        target = target_for(resident.manifest)
        conveyance = transport if transport is not None else transport_for(target)
        down = compose_argv(target, "down", "--remove-orphans")
        scrub = scrub_argv(target)
        plan = (conveyance.plan(down), conveyance.plan(scrub)) if deploy else ()
        if commit:
            complaint = (
                path_complaint(checkout, manifest_path, git=git)
                if resident_dirty_only
                else worktree_complaint(checkout, git=git)
            )
            if complaint and not allow_dirty:
                raise NurseryError(complaint, reason=WORKTREE_REFUSED)

        marked = set_retired(manifest_path)
        if marked:
            try:
                resident = _load_or_refuse(manifest_path, skills_dir)
            except NurseryError:
                set_retired(manifest_path, retired=False)
                raise

        try:
            sha = (
                commit_paths(
                    checkout,
                    [manifest_path],
                    RETIRE_SUBJECT.format(id=resident_id),
                    identity=identity,
                    git=git,
                )
                if commit
                else None
            )
        except NurseryError as exc:
            raise NurseryError(
                f"{exc}; {manifest_path} now says retired: true and nothing has committed it, "
                f"so {resident_id} has already stopped taking work with no history of the "
                "decision. Commit that file to finish the retirement, or set retired: false "
                "to undo it — the container was not touched either way",
                exc.diagnostics,
                reason=COMMIT_FAILED,
            ) from exc

    (emitter or ev.EventEmitter.from_env()).emit(ev.resident_retired_event(resident=resident))

    scrubbed = False
    if deploy:
        stopped, note = _stop_retired_container(resident_id, conveyance, target, down)
        # Runs whether or not there was a container to stop: "nothing here to stop" is
        # exactly the state a half-finished earlier retirement leaves behind, and the
        # ``.env`` is the thing worth being sure about. ``rm -f`` makes it a no-op when
        # the files are already gone.
        scrubbed = _scrub_host(resident_id, conveyance, target, scrub)
    else:
        stopped, note = (
            False,
            (
                "deploy skipped: the manifest is marked and the host was left untouched, "
                "so the .env holding CHRONICLE_TOKEN is still there"
            ),
        )

    return RetireReport(
        resident_id=resident_id,
        manifest_path=manifest_path,
        marked=marked,
        stopped=stopped,
        scrubbed=scrubbed,
        commands=plan,
        commit=sha,
        note=note or ("retired" if marked else "already retired; container reconciled"),
    )
