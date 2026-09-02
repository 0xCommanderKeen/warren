"""The one lifecycle seam every real resident wake-up crosses.

Callers decide *why* a resident wakes and retain their domain conclusion: the scheduler
owns occurrences and routine events, while the board owns claims, leases, and task
events.  This module owns what must happen in the same safe order once either caller
wants a resident session: admit, provision, gather context, assemble, run, account, and
harvest.
"""

import logging
import os
import stat
import time
from collections.abc import Callable, Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol, runtime_checkable

from steward import events as ev
from steward import journal
from steward.deploy import memory_host_dir, placement_for
from steward.manifest import ManifestError, Resident, ResidentManifest, Routine
from steward.manifest import Runner as RunnerSpec
from steward.prompt import (
    assemble_chat_prompt,
    assemble_delegated_prompt,
    assemble_routine_prompt,
    assemble_task_prompt,
)
from steward.runners import (
    Outcome,
    Placement,
    Runner,
    RunRequest,
    RunResult,
    build_runner,
    skills_home,
)
from steward.runs import RUN_CHAT, RUN_DELEGATED, RUN_ROUTINE, RUN_TASK, TRIGGER_CHAT
from steward.session_auth import SESSION_TOKEN_ENV
from steward.skills import (
    Skill,
    SkillError,
    SkillLibrary,
    describe_missing,
    effective_skills,
    materialize,
    missing_skills,
)

__all__ = [
    "DEFAULT_CHAT_TIMEOUT_S",
    "Admission",
    "ChatWake",
    "DelegatedWake",
    "LegacySessionHooks",
    "Refusal",
    "ResidentSessions",
    "RoutineWake",
    "RunGuard",
    "RunnerFactory",
    "SessionHarvest",
    "SessionHooks",
    "SessionResult",
    "TaskWake",
    "declared_directory",
    "session_credential_env",
    "unprovisioned_reason",
    "workdir_refusal",
]

log = logging.getLogger("steward.sessions")

#: How a runner is built: from the manifest's runner block and its resolved placement.
#: Two arguments because they are two independent axes — *which brain* and *where it
#: runs* — and a factory that saw only the runner block could not place a session in the
#: resident's container (steward #58).
type RunnerFactory = Callable[[RunnerSpec, Placement], Runner]


def declared_directory(resident: Resident) -> Path | None:
    """Return the host-side directory this resident's session state lives in, or ``None``.

    For a local placement: the declared memory directory, when memory is a directory at
    all. For a container placement: the *host side* of the memory mount — the same files
    the session will see at the container's own mount point, and the side every
    control-plane touch (journal, skills materialization, the admission identity pin)
    must use. ``None`` means nothing is declared to pin and the session runs in the
    caller's fallback directory.
    """
    manifest = resident.manifest
    if manifest.runner.container_placed:
        return memory_host_dir(manifest)
    if manifest.memory.kind != "directory":
        return None
    return Path(manifest.memory.path).expanduser()


def unprovisioned_reason(resident: Resident) -> str:
    """Why a container-placed resident with no host-side memory directory may not run."""
    host = memory_host_dir(resident.manifest)
    return (
        f"this resident's sessions are placed in container "
        f"{resident.manifest.deploy.container!r}, but the host side of its memory mount "
        f"({str(host)!r}) is not a directory on this host; provision the resident "
        f"(`steward new-resident`) before its sessions have anywhere to happen"
    )


def session_credential_env(credential: str) -> dict[str, str]:
    """Name this run's own credential for the child, or say nothing at all.

    One spelling for every kind of wake-up, so a routine session and a board session cannot
    end up looking for their credential under different names. Absent rather than empty
    when there is none: a session that finds ``STEWARD_SESSION_TOKEN=""`` would present it
    and be told it is unauthorized, which reads like a revoked credential rather than like
    a run that was never registered.
    """
    return {SESSION_TOKEN_ENV: credential} if credential else {}


class RunGuard(Protocol):
    """Whether a resident may run, how long it gets, and what it spent."""

    def allow(self, manifest: ResidentManifest, now: datetime | None = None) -> str | None:
        """Return why this resident may not run, or ``None`` when admitted."""
        ...

    def timeout_for(self, manifest: ResidentManifest, declared_s: int) -> int:
        """Cap a wake-up's declared timeout by its resident budget."""
        ...

    def record(  # noqa: PLR0913 - the existing budget adapter interface
        self,
        manifest: ResidentManifest,
        *,
        result: RunResult,
        kind: str,
        run_id: str,
        trigger: str = "",
        ref: str,
        origin: str,
        now: datetime | None = None,
    ) -> object:
        """Record one completed session's spend."""
        ...


@dataclass(frozen=True, slots=True)
class SessionHarvest:
    """Approval requests and handoffs safely retained from one completed session."""

    raised: tuple[object, ...] = ()
    handed_over: tuple[object, ...] = ()


@runtime_checkable
class SessionHooks(Protocol):
    """The existing durable decisions and output-harvest implementation."""

    def decisions_for(self, resident_id: str) -> str | None:
        """Claim and render decisions waiting for one resident."""
        ...

    def harvest_session(
        self,
        manifest: ResidentManifest,
        output: str,
        *,
        parent_task_id: str | None = None,
        now: datetime | None = None,
    ) -> SessionHarvest:
        """Persist approvals and handoffs found in completed output."""
        ...


class LegacySessionHooks(Protocol):
    """The pre-lifecycle hook shape retained for compatible scheduler construction."""

    def decisions_for(self, resident_id: str) -> str | None:
        """Claim and render decisions waiting for one resident."""
        ...

    def harvest(self, manifest: ResidentManifest, output: str) -> object:
        """Persist legacy scheduler output."""
        ...


@dataclass(frozen=True, slots=True)
class Refusal:
    """Why a resident was not admitted to a real session."""

    reason: str


@dataclass(slots=True)
class _DirectoryCapability:
    """One close-once descriptor pinning an admitted directory's lifetime."""

    descriptor: int

    def close(self) -> None:
        if self.descriptor >= 0:
            descriptor, self.descriptor = self.descriptor, -1
            with suppress(OSError):
                os.close(descriptor)

    def __del__(self) -> None:
        self.close()


@dataclass(frozen=True, slots=True)
class _DirectoryIdentity:
    """The POSIX identity of one consistently observed declared directory.

    Steward's supported deployment targets are POSIX hosts, where ``st_dev`` and
    ``st_ino`` identify an extant filesystem object.  ``file_type`` records the relevant
    part of ``st_mode`` so a non-directory can never inherit an admitted identity.
    """

    canonical: Path
    device: int
    inode: int
    file_type: int
    capability: _DirectoryCapability = field(repr=False, compare=False)

    @property
    def descriptor(self) -> int:
        return self.capability.descriptor


class _DirectoryChangedError(OSError):
    """The declared path changed during one supposedly consistent observation."""


@dataclass(frozen=True, slots=True)
class Admission:
    """A resident allowed to cross the session lifecycle seam."""

    resident: Resident
    workdir: Path
    admitted_at: datetime
    rehearsal: bool = False
    declared_workdir: Path | None = None
    declared_identity: _DirectoryIdentity | None = field(default=None, repr=False, compare=False)
    _resolve_timeout: Callable[[int], int] = field(
        default=lambda declared_s: declared_s, repr=False, compare=False
    )

    def timeout_for(self, declared_s: int) -> int:
        """Resolve one declared timeout once, then return that answer consistently."""
        return self._resolve_timeout(declared_s)

    def close(self) -> None:
        """Release the directory capability retained for this admission."""
        if self.declared_identity is not None:
            self.declared_identity.capability.close()


@dataclass(frozen=True, slots=True)
class RoutineWake:
    """The routine-specific facts the shared lifecycle needs."""

    routine: Routine
    run_id: str
    trigger: str = "schedule"
    #: This run's own scoped API credential, or ``""`` when the run is not in the registry
    #: (a rehearsal, or a steward with no store). Empty rather than absent because a
    #: credential no row backs would authenticate nothing, and a session is better told
    #: nothing than handed a dud (steward #41).
    session_credential: str = ""

    @property
    def timeout_s(self) -> int:
        """Return this routine's declared timeout."""
        return self.routine.timeout_s

    @property
    def kind(self) -> str:
        """Name the ledger kind for a routine."""
        return RUN_ROUTINE

    @property
    def ref(self) -> str:
        """Name the routine this session works."""
        return self.routine.id

    @property
    def harvest_parent_task_id(self) -> None:
        """A routine has no parent task."""
        return None

    def origin_for(self, resident: Resident) -> str:
        """Attribute a routine to its resident's own initiative."""
        return f"resident:{resident.id}"

    def environment(self, resident: Resident) -> Mapping[str, str]:
        """Build the environment facts a routine session inherits."""
        return {
            "CHRONICLE_AGENT_ID": resident.agent_id,
            "CHRONICLE_PROJECT": resident.project,
            # Both spellings: a session may be picked up by an emitter older than the
            # warren#216 rename, and an unrecognised identity files its events under a
            # different villager rather than failing.
            "BURROW_AGENT_ID": resident.agent_id,
            "BURROW_PROJECT": resident.project,
            "STEWARD_ROUTINE": self.routine.id,
            "STEWARD_RUN_ID": self.run_id,
            **session_credential_env(self.session_credential),
        }

    def pre_run_failure_duration(self, elapsed_s: float) -> float:
        """Preserve routine failures' elapsed duration accounting."""
        return elapsed_s


#: How long a chat session gets before steward kills it, when no budget says otherwise.
#: Short by the standards of this codebase — the shipped routines are given 600 and 900
#: seconds — because the whole difference of this channel is that a person is sitting
#: there waiting for the answer. A resident's ``budgets.max_run_seconds`` still caps it
#: like any other session; nothing here can widen a declared bound.
DEFAULT_CHAT_TIMEOUT_S = 300


@dataclass(frozen=True, slots=True, kw_only=True)
class ChatWake:
    """One message from the operator, and everything the lifecycle needs to answer it.

    A wake-up like the other three (warren#108), and the one that is not declared anywhere:
    a routine is in the manifest, a task is on the board, a letter is in the inbox, and a
    chat message simply arrived. What it carries beyond the message is the *window* — the
    last few turns of this conversation, read from the resident's own memory directory by
    :class:`steward.chat.Transcript` — because a headless session that woke up amnesiac
    would otherwise ask the operator to repeat themselves on every single turn.
    """

    conversation: str
    route: str
    message: str
    transcript: str = ""
    run_id: str
    timeout_s: int = DEFAULT_CHAT_TIMEOUT_S
    #: This session's own scoped API credential, minted per message like a routine's is
    #: per fire: two turns of one conversation are two sessions, and the second must not be
    #: able to present the first's (steward #41).
    session_credential: str = ""

    @property
    def kind(self) -> str:
        """Name the ledger kind for a chat session."""
        return RUN_CHAT

    @property
    def trigger(self) -> str:
        """Name what started it. There is only ever one answer, and it is not a schedule."""
        return TRIGGER_CHAT

    @property
    def ref(self) -> str:
        """Name the conversation this session is a turn of."""
        return self.conversation

    @property
    def harvest_parent_task_id(self) -> None:
        """A chat session has no parent task."""
        return None

    def origin_for(self, resident: Resident) -> str:
        """Attribute a chat session to the person who asked for it.

        ``human:chat`` rather than ``resident:<id>``, in the vocabulary
        :func:`steward.delegation.origin_for` already documents: a routine is a resident
        acting on its own initiative and this is a person at the door, so the spend rolls up
        to the person. The channel names the door, the way ``human:api`` does; *which*
        operator typed it is deliberately not in the ledger, which is a record of what steward
        spent rather than of who said what.
        """
        del resident
        return "human:chat"

    def environment(self, resident: Resident) -> Mapping[str, str]:
        """Build the environment facts a chat session inherits.

        Conspicuously absent: the bot token. The bridge holds it, the session never needs
        it, and a session that had it could speak as the resident in a conversation steward
        never brokered — which is the one thing a chat bridge must not make possible.
        """
        return {
            "CHRONICLE_AGENT_ID": resident.agent_id,
            "CHRONICLE_PROJECT": resident.project,
            # Both spellings: a session may be picked up by an emitter older than the
            # warren#216 rename, and an unrecognised identity files its events under a
            # different villager rather than failing.
            "BURROW_AGENT_ID": resident.agent_id,
            "BURROW_PROJECT": resident.project,
            "STEWARD_CHAT_ROUTE": self.route,
            "STEWARD_RUN_ID": self.run_id,
            **session_credential_env(self.session_credential),
        }

    def pre_run_failure_duration(self, elapsed_s: float) -> float:
        """Report what a chat session that failed before its runner actually took."""
        return elapsed_s


@dataclass(frozen=True, slots=True, kw_only=True)
class _BoardWake:
    """Facts and behaviour common to board notices and delegated letters."""

    task_id: str
    title: str
    detail: str
    timeout_s: int
    origin: str
    #: This session's own id, minted per attempt, and not the task's. A task claimed,
    #: dropped on a dead lease and claimed again is *two* sessions spending money twice,
    #: and the ledger has to be able to tell them apart — the run registry already can
    #: (steward #39). The task travels on ``ref`` instead, which is what the spend-by-
    #: origin join reads (steward #124).
    run_id: str
    parent_task_id: str | None = None
    #: This attempt's own scoped API credential. Per attempt, not per task: two claims of
    #: one task are two sessions, and the second must not be able to present the first's.
    session_credential: str = ""

    @property
    def trigger(self) -> str:
        """Name what started it: nothing did. A task is claimed rather than triggered."""
        return ""

    @property
    def ref(self) -> str:
        """Name the task this session works."""
        return self.task_id

    @property
    def harvest_parent_task_id(self) -> str:
        """Attribute handoffs created by this session to this task."""
        return self.task_id

    def origin_for(self, resident: Resident) -> str:
        """Return the task's already-resolved accountable origin."""
        del resident
        return self.origin

    def environment(self, resident: Resident) -> Mapping[str, str]:
        """Build the environment facts a task session inherits.

        ``STEWARD_RUN_ID`` names this *attempt*, like a routine wake's does: it is what
        keys a container-placed session's pid file, so a timed-out board session is as
        killable inside its container as a routine one (steward #58).
        """
        env = {
            "CHRONICLE_AGENT_ID": resident.agent_id,
            "CHRONICLE_PROJECT": resident.project,
            # Both spellings: a session may be picked up by an emitter older than the
            # warren#216 rename, and an unrecognised identity files its events under a
            # different villager rather than failing.
            "BURROW_AGENT_ID": resident.agent_id,
            "BURROW_PROJECT": resident.project,
            "STEWARD_TASK_ID": self.task_id,
            "STEWARD_RUN_ID": self.run_id,
            **session_credential_env(self.session_credential),
        }
        if self.parent_task_id:
            env["STEWARD_PARENT_TASK_ID"] = self.parent_task_id
        return env

    def pre_run_failure_duration(self, elapsed_s: float) -> float:
        """Preserve board failures' historical zero-duration result."""
        del elapsed_s
        return 0.0


@dataclass(frozen=True, slots=True, kw_only=True)
class TaskWake(_BoardWake):
    """The complete facts carried by one claimed board notice."""

    required_skills: tuple[str, ...]

    @property
    def kind(self) -> str:
        """Name the ledger kind for a board task."""
        return RUN_TASK


@dataclass(frozen=True, slots=True, kw_only=True)
class DelegatedWake(_BoardWake):
    """The valid, complete facts carried by one delegated letter."""

    delegated_by: str | None
    route: str

    @property
    def kind(self) -> str:
        """Name the ledger kind for a delegated letter."""
        return RUN_DELEGATED


type Wake = ChatWake | DelegatedWake | RoutineWake | TaskWake


@dataclass(frozen=True, slots=True)
class SessionResult:
    """The common conclusion callers translate into their domain report."""

    prompt: str
    result: RunResult | None
    timeout_s: int
    completed_at: datetime | None
    journal_path: Path | None = None
    raised: tuple[object, ...] = ()
    handed_over: tuple[object, ...] = ()

    def require_result(self) -> RunResult:
        """Return a real run result, rejecting use on a rehearsal conclusion."""
        if self.result is None:
            raise ValueError("a rehearsal has no runner result")
        return self.result


def workdir_refusal(resident: Resident, fallback: Path, library: SkillLibrary) -> str | None:
    """Return why materializing skills in this resident's fallback would be unsafe."""
    if resident.manifest.runner.container_placed:
        # A container-placed resident has no fallback to be unsafe *in*: its sessions
        # happen inside the container, and the host side of its memory mount is the only
        # place steward may journal and materialize. Missing means unprovisioned, and
        # that is a refusal in daylight, not a session in the wrong directory.
        if declared := declared_directory(resident):
            return None if declared.is_dir() else unprovisioned_reason(resident)
        return None  # pragma: no cover — container placement always declares a directory
    memory = resident.manifest.memory
    if memory.kind != "directory":
        return None
    if Path(memory.path).expanduser().is_dir():
        return None
    if skills_home(resident.manifest.runner) is None or not library.configured:
        return None
    try:
        effective = resident.workdir(fallback).resolve()
        cwd = Path.cwd().resolve()
    except OSError:  # pragma: no cover - an unresolvable cwd is its own refusal
        effective = cwd = Path()
    if effective != cwd:
        return None
    return (
        f"memory.path {memory.path!r} is not a directory on this host, so this resident "
        "would run in the current working directory — steward refuses that fallback rather "
        "than materialize skills into, and delete files from, a directory its charter never "
        "named"
    )


class ResidentSessions:
    """Admit and run resident sessions through one fixed lifecycle implementation."""

    def __init__(  # noqa: PLR0913 - each injected dependency is independently useful
        self,
        *,
        workdir: Path | None = None,
        runner_factory: RunnerFactory = build_runner,
        library: SkillLibrary | None = None,
        guard: RunGuard | None = None,
        hooks: SessionHooks | LegacySessionHooks | None = None,
        residents: Sequence[Resident] = (),
        clock: Callable[[], datetime] | None = None,
        on_completed: Callable[[datetime], None] | None = None,
        emitter: ev.Emitter | None = None,
    ) -> None:
        """Assemble the lifecycle over its stable collaborators."""
        self.workdir = Path(workdir) if workdir is not None else Path.cwd()
        self.runner_factory = runner_factory
        self.library = library if library is not None else SkillLibrary()
        self.guard = guard
        self.hooks = hooks
        self.residents = tuple(residents)
        self.clock = clock or (lambda: datetime.now(UTC))
        self.on_completed = on_completed
        self.emitter = emitter or ev.NullEmitter()

    def admit(
        self, resident: Resident, *, now: datetime, rehearsal: bool = False
    ) -> Admission | Refusal:
        """Decide whether a resident may run before a caller commits to launching work."""
        timeout_cache: dict[int, int] = {}

        def resolve_timeout(declared_s: int) -> int:
            if declared_s not in timeout_cache:
                timeout_cache[declared_s] = (
                    declared_s
                    if self.guard is None
                    else self.guard.timeout_for(resident.manifest, declared_s)
                )
            return timeout_cache[declared_s]

        if rehearsal:
            declared = declared_directory(resident)
            return Admission(
                resident,
                declared if declared is not None and declared.is_dir() else self.workdir,
                now,
                rehearsal=True,
                _resolve_timeout=resolve_timeout,
            )
        if self.guard is not None:
            try:
                refusal = self.guard.allow(resident.manifest, now)
            except Exception as exc:  # noqa: BLE001 - an unreadable budget refuses safely
                log.warning("%s: could not read the budget: %s", resident.id, exc)
                refusal = f"budget unreadable: {type(exc).__name__}: {exc}"
            if refusal is not None:
                return Refusal(refusal)
        resolved = self._resolve_admitted_workdir(resident)
        if isinstance(resolved, Refusal):
            return resolved
        workdir, identity = resolved
        refusal = workdir_refusal(resident, self.workdir, self.library)
        if refusal is not None:
            if identity is not None:
                identity.capability.close()
            return Refusal(refusal)
        return Admission(
            resident,
            workdir,
            now,
            declared_workdir=identity.canonical if identity is not None else None,
            declared_identity=identity,
            _resolve_timeout=resolve_timeout,
        )

    def _resolve_admitted_workdir(
        self, resident: Resident
    ) -> tuple[Path, _DirectoryIdentity | None] | Refusal:
        """Resolve the chosen host directory and remember when it was the declared one.

        The declared directory is placement-aware (:func:`declared_directory`): the
        memory directory itself for a local placement, the host side of the memory mount
        for a container one. A locally placed resident whose declared directory is
        absent falls back to the caller's working directory, exactly as it always has; a
        container-placed one is refused instead — its fallback would be a directory the
        container cannot see, so skills and journal would land where no session looks.
        """
        declared = declared_directory(resident)
        identity: _DirectoryIdentity | None = None
        try:
            if declared is not None:
                try:
                    identity = self._observe_directory(declared)
                except FileNotFoundError:
                    if resident.manifest.runner.container_placed:
                        return Refusal(unprovisioned_reason(resident))
                    identity = None
                else:
                    if identity is None:
                        return Refusal(self._vanished_workdir_reason(resident))
            workdir = self._fallback_workdir(resident) if identity is None else identity.canonical
            workdir = workdir.resolve(strict=identity is not None)
        except _DirectoryChangedError:
            return Refusal(self._changed_workdir_reason(resident))
        except OSError:
            return Refusal(self._vanished_workdir_reason(resident))
        return workdir, identity

    def _fallback_workdir(self, resident: Resident) -> Path:
        """Where a session runs when nothing declared is pinned: the resident's own say.

        Local placement only — a container placement either pinned its host directory or
        was refused above, so this never relocates one.
        """
        return resident.workdir(self.workdir)

    @staticmethod
    def _observe_directory(candidate: Path) -> _DirectoryIdentity | None:
        """Observe path, resolution, and identity without following a final symlink.

        The two no-follow stats bracket ``resolve``.  Any replacement while those calls
        are in flight is rejected instead of combining a canonical path from one object
        with identity from another.
        """
        before = candidate.stat(follow_symlinks=False)
        if not stat.S_ISDIR(before.st_mode):
            return None
        descriptor = os.open(candidate, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            pinned = os.fstat(descriptor)
            canonical = candidate.resolve(strict=True)
            after = candidate.stat(follow_symlinks=False)
            before_identity = (before.st_dev, before.st_ino, stat.S_IFMT(before.st_mode))
            pinned_identity = (pinned.st_dev, pinned.st_ino, stat.S_IFMT(pinned.st_mode))
            after_identity = (after.st_dev, after.st_ino, stat.S_IFMT(after.st_mode))
            changed = (
                before_identity != pinned_identity
                or pinned_identity != after_identity
                or not stat.S_ISDIR(after.st_mode)
            )
        except BaseException:
            os.close(descriptor)
            raise
        if changed:
            os.close(descriptor)
            raise _DirectoryChangedError("declared directory changed while it was inspected")
        return _DirectoryIdentity(canonical, *after_identity, _DirectoryCapability(descriptor))

    def run(
        self,
        admission: Admission,
        wake: Wake,
    ) -> SessionResult:
        """Run an admitted wake-up through the fixed lifecycle sequence. Never raises."""
        resident = admission.resident
        timeout_s = admission.timeout_for(wake.timeout_s)
        if admission.rehearsal:
            journal_entry = self._journal_for(resident)
            prompt = self._prompt(
                resident,
                wake,
                self._skills_for(resident),
                admission.admitted_at,
                journal_entry=journal_entry,
                decisions=None,
            )
            return SessionResult(prompt, None, timeout_s, None)
        started = time.monotonic()
        prompt = ""
        try:
            self._require_revalidated(admission)
            skills = self._provision(admission)
            journal_entry = self._journal_for(resident)
            decisions = self._decisions_for(resident)
            prompt = self._prompt(
                resident,
                wake,
                skills,
                admission.admitted_at,
                journal_entry=journal_entry,
                decisions=decisions,
            )
            runner = self.runner_factory(resident.manifest.runner, placement_for(resident.manifest))
            # This is a launch fact, not an admission attempt. Publish only after every
            # pre-run safety boundary succeeded and immediately before the runner starts.
            self.emitter.emit(ev.resident_declared_event(resident=resident))
            result = runner.run(
                RunRequest(
                    prompt=prompt,
                    workdir=admission.workdir,
                    timeout_s=timeout_s,
                    tools=resident.manifest.tools,
                    workspace=tuple(resident.manifest.workspace),
                    model=resident.manifest.runner.model,
                    env=wake.environment(resident),
                    workdir_fd=(
                        admission.declared_identity.descriptor
                        if admission.declared_identity is not None
                        else None
                    ),
                )
            )
        except SkillError as exc:
            log.error("%s: %s", resident.id, exc)  # noqa: TRY400
            result = RunResult(
                outcome=Outcome.FAILED,
                duration_s=wake.pre_run_failure_duration(time.monotonic() - started),
                error=str(exc),
            )
        except Exception as exc:  # noqa: BLE001 - a broken runner is a failed session
            result = RunResult(
                outcome=Outcome.FAILED,
                duration_s=wake.pre_run_failure_duration(time.monotonic() - started),
                error=f"{type(exc).__name__}: {exc}",
            )
        # Runner duration is a measurement of the adapter's work, not a timestamp.
        # Queueing, provisioning, exception handling, and adapter overhead all belong to
        # the wall-clock interval, so read completion once after the runner answers and
        # carry that one fact through every downstream close.
        completed_at = self.clock()
        if self.on_completed is not None:
            self.on_completed(completed_at)
        self._account(resident, wake, result, completed_at)
        harvested = self._harvest(
            resident,
            result.output,
            parent_task_id=wake.harvest_parent_task_id,
            now=admission.admitted_at if wake.harvest_parent_task_id is not None else None,
        )
        journal_path, result = self._close_routine(resident, wake, result, admission.admitted_at)
        return SessionResult(
            prompt,
            result,
            timeout_s,
            completed_at,
            journal_path,
            harvested.raised,
            harvested.handed_over,
        )

    def revalidate(self, admission: Admission) -> Refusal | None:
        """Refuse when an admitted resident's declared workdir is no longer stable.

        Admission and provisioning are separate operations.  A mounted memory directory
        can disappear, stop being a directory, or be replaced by a symlink between them;
        in all three cases resolving the resident again would silently select the process
        cwd.  Keep the exact directory admitted earlier and require it to remain the same
        real directory immediately before anything may materialize there.
        """
        resident = admission.resident
        candidate = declared_directory(resident)
        if candidate is None:
            return None
        if admission.declared_identity is None:
            return None
        try:
            observed = self._observe_directory(candidate)
        except _DirectoryChangedError:
            return Refusal(self._changed_workdir_reason(resident))
        except OSError:
            return Refusal(self._vanished_workdir_reason(resident))
        if observed is None:
            return Refusal(self._vanished_workdir_reason(resident))
        try:
            if observed != admission.declared_identity:
                return Refusal(self._changed_workdir_reason(resident))
            return None
        finally:
            observed.capability.close()

    def _require_revalidated(self, admission: Admission) -> None:
        """Turn a late workdir refusal into the lifecycle's provision-failure path."""
        refusal = self.revalidate(admission)
        if refusal is not None:
            raise SkillError(refusal.reason)

    @staticmethod
    def _declared_reference(resident: Resident) -> str:
        """Name the directory a workdir refusal is about, on the side steward touches."""
        manifest = resident.manifest
        if manifest.runner.container_placed:
            return f"the memory mount's host side {str(memory_host_dir(manifest))!r}"
        return f"memory.path {manifest.memory.path!r}"

    def _vanished_workdir_reason(self, resident: Resident) -> str:
        return (
            f"{self._declared_reference(resident)} is no longer the directory "
            "admitted for this session; steward refuses to fall back to the current "
            "working directory"
        )

    def _changed_workdir_reason(self, resident: Resident) -> str:
        return (
            f"{self._declared_reference(resident)} canonical path or filesystem "
            "identity changed after admission; steward refuses to provision or run in "
            "the replacement directory"
        )

    def _skills_for(self, resident: Resident) -> tuple[Skill, ...]:
        return effective_skills(resident.manifest, self.library)

    def _provision(self, admission: Admission) -> tuple[Skill, ...]:
        resident = admission.resident
        missing = missing_skills(resident.manifest, self.library)
        if missing:
            raise SkillError(describe_missing(resident.id, missing, self.library))
        skills = self._skills_for(resident)
        subdir = skills_home(resident.manifest.runner)
        if subdir is not None and self.library.configured:
            try:
                if admission.declared_identity is not None:
                    workdir_fd = admission.declared_identity.descriptor
                else:
                    workdir_fd = None
                result = materialize(skills, admission.workdir, subdir, workdir_fd=workdir_fd)
            except OSError as exc:
                raise SkillError(self._vanished_workdir_reason(resident)) from exc
            log.debug("%s: skills %s", resident.id, result.summary())
        return skills

    def _prompt(  # noqa: PLR0913 - one value per assembled lifecycle context section
        self,
        resident: Resident,
        wake: Wake,
        skills: tuple[Skill, ...],
        moment: datetime,
        *,
        journal_entry: str | None,
        decisions: str | None,
    ) -> str:
        if isinstance(wake, ChatWake):
            return assemble_chat_prompt(
                resident.manifest,
                wake.message,
                route=wake.route,
                transcript=wake.transcript,
                soul_text=resident.soul.body,
                journal_entry=journal_entry,
                skills=skills,
                decisions=decisions,
            )
        if isinstance(wake, DelegatedWake):
            return assemble_delegated_prompt(
                resident.manifest,
                task_id=wake.task_id,
                title=wake.title,
                detail=wake.detail,
                sender=self._sender_label(wake.delegated_by),
                route=wake.route,
                parent_task_id=wake.parent_task_id,
                soul_text=resident.soul.body,
                journal_entry=journal_entry,
                skills=skills,
                decisions=decisions,
            )
        if isinstance(wake, TaskWake):
            return assemble_task_prompt(
                resident.manifest,
                task_id=wake.task_id,
                title=wake.title,
                detail=wake.detail,
                required_skills=wake.required_skills,
                soul_text=resident.soul.body,
                journal_entry=journal_entry,
                skills=skills,
                decisions=decisions,
            )
        return assemble_routine_prompt(
            resident.manifest,
            wake.routine.prompt,
            soul_text=resident.soul.body,
            journal_entry=journal_entry,
            skills=skills,
            decisions=decisions,
            closing=self._closing_for(resident, wake, moment),
        )

    def _sender_label(self, sender_id: str | None) -> str:
        if not sender_id:
            return "another resident"
        for resident in self.residents:
            if resident.id == sender_id:
                return f"{resident.manifest.soul.name} ({resident.id})"
        return sender_id

    def _journal_for(self, resident: Resident) -> str | None:
        return self._journal_call(
            resident, lambda: journal.latest_entry(resident.manifest, source=resident.path)
        )

    def _decisions_for(self, resident: Resident) -> str | None:
        if self.hooks is None:
            return None
        try:
            return self.hooks.decisions_for(resident.id)
        except Exception as exc:  # noqa: BLE001 - a broken inbox is absent context
            log.warning("%s: could not read pending decisions: %s", resident.id, exc)
            return None

    def _closing_for(self, resident: Resident, wake: RoutineWake, moment: datetime) -> str | None:
        if wake.routine.journal != journal.CLOSE_OF_DAY:
            return None
        day = journal.local_day(wake.routine, moment)
        return self._journal_call(
            resident,
            lambda: journal.close_of_day_instruction(
                resident.manifest, day, wake.routine.id, source=resident.path
            ),
        )

    def _journal_call[T](self, resident: Resident, read: Callable[[], T]) -> T | None:
        try:
            return read()
        except ManifestError as exc:
            log.warning("%s: no journal — %s", resident.id, exc)
        except Exception as exc:  # noqa: BLE001 - a broken journal is absent context
            log.warning("%s: could not reach the journal: %s", resident.id, exc)
        return None

    def _account(
        self, resident: Resident, wake: Wake, result: RunResult, completed_at: datetime
    ) -> None:
        if self.guard is None:
            return
        try:
            self.guard.record(
                resident.manifest,
                result=result,
                kind=wake.kind,
                run_id=wake.run_id,
                trigger=wake.trigger,
                ref=wake.ref,
                origin=wake.origin_for(resident),
                now=completed_at,
            )
        except Exception as exc:  # noqa: BLE001 - accounting cannot reopen a completed run
            log.warning("%s: could not record what this run cost: %s", resident.id, exc)

    def _harvest(
        self,
        resident: Resident,
        output: str,
        *,
        parent_task_id: str | None = None,
        now: datetime | None = None,
    ) -> SessionHarvest:
        if self.hooks is None or not output:
            return SessionHarvest()
        try:
            if isinstance(self.hooks, SessionHooks):
                return self.hooks.harvest_session(
                    resident.manifest,
                    output,
                    parent_task_id=parent_task_id,
                    now=now,
                )
            raised = self.hooks.harvest(resident.manifest, output)
            return SessionHarvest(tuple(raised) if isinstance(raised, (list, tuple)) else ())
        except Exception as exc:  # noqa: BLE001 - harvest cannot reopen a completed session
            log.warning("%s: could not harvest this session: %s", resident.id, exc)
            return SessionHarvest()

    def _close_routine(
        self, resident: Resident, wake: Wake, result: RunResult, moment: datetime
    ) -> tuple[Path | None, RunResult]:
        if (
            not isinstance(wake, RoutineWake)
            or not result.ok
            or wake.routine.journal != journal.CLOSE_OF_DAY
        ):
            return None, result
        day = journal.local_day(wake.routine, moment)
        closed = self._journal_call(
            resident,
            lambda: journal.persist_close_of_day(
                resident.manifest,
                day,
                wake.routine.id,
                result.output,
                source=resident.path,
            ),
        )
        if closed is None:
            return None, result
        if closed.persisted and closed.path is not None:
            result = replace(result, artifacts=(*result.artifacts, str(closed.path)))
        return closed.path, result
