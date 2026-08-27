"""The one lifecycle seam every real resident wake-up crosses.

Callers decide *why* a resident wakes and retain their domain conclusion: the scheduler
owns occurrences and routine events, while the board owns claims, leases, and task
events.  This module owns what must happen in the same safe order once either caller
wants a resident session: admit, provision, gather context, assemble, run, account, and
harvest.
"""

import logging
import stat
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from pathlib import Path
from typing import Protocol, runtime_checkable

from steward import journal
from steward.manifest import ManifestError, Resident, ResidentManifest, Routine
from steward.manifest import Runner as RunnerSpec
from steward.prompt import assemble_delegated_prompt, assemble_routine_prompt, assemble_task_prompt
from steward.runners import Outcome, Runner, RunRequest, RunResult, build_runner, skills_home
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
    "Admission",
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
    "workdir_refusal",
]

log = logging.getLogger("steward.sessions")

type RunnerFactory = Callable[[RunnerSpec], Runner]


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


@dataclass(frozen=True, slots=True)
class RoutineWake:
    """The routine-specific facts the shared lifecycle needs."""

    routine: Routine
    run_id: str

    @property
    def timeout_s(self) -> int:
        """Return this routine's declared timeout."""
        return self.routine.timeout_s

    @property
    def kind(self) -> str:
        """Name the ledger kind for a routine."""
        return "routine"

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
            "BURROW_AGENT_ID": resident.agent_id,
            "BURROW_PROJECT": resident.project,
            "STEWARD_ROUTINE": self.routine.id,
            "STEWARD_RUN_ID": self.run_id,
        }

    def pre_run_failure_duration(self, elapsed_s: float) -> float:
        """Preserve routine failures' elapsed duration accounting."""
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
        """Build the environment facts a task session inherits."""
        env = {
            "BURROW_AGENT_ID": resident.agent_id,
            "BURROW_PROJECT": resident.project,
            "STEWARD_TASK_ID": self.task_id,
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
        return "task"


@dataclass(frozen=True, slots=True, kw_only=True)
class DelegatedWake(_BoardWake):
    """The valid, complete facts carried by one delegated letter."""

    delegated_by: str | None
    route: str

    @property
    def kind(self) -> str:
        """Name the ledger kind for a delegated letter."""
        return "delegated"


type Wake = DelegatedWake | RoutineWake | TaskWake


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
    ) -> None:
        """Assemble the lifecycle over its stable collaborators."""
        self.workdir = Path(workdir) if workdir is not None else Path.cwd()
        self.runner_factory = runner_factory
        self.library = library if library is not None else SkillLibrary()
        self.guard = guard
        self.hooks = hooks
        self.residents = tuple(residents)

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
            return Admission(
                resident,
                resident.workdir(self.workdir),
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
        refusal = workdir_refusal(resident, self.workdir, self.library)
        if refusal is not None:
            return Refusal(refusal)
        resolved = self._resolve_admitted_workdir(resident)
        if isinstance(resolved, Refusal):
            return resolved
        workdir, identity = resolved
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
        """Resolve the chosen directory and remember when it was the declared memory."""
        workdir = resident.workdir(self.workdir)
        identity: _DirectoryIdentity | None = None
        try:
            memory = resident.manifest.memory
            if memory.kind == "directory":
                candidate = Path(memory.path).expanduser()
                try:
                    identity = self._observe_directory(candidate)
                except FileNotFoundError:
                    identity = None
                workdir = (
                    identity.canonical if identity is not None else resident.workdir(self.workdir)
                )
            workdir = workdir.resolve(strict=identity is not None)
        except _DirectoryChangedError:
            return Refusal(self._changed_workdir_reason(resident))
        except OSError:
            return Refusal(self._vanished_workdir_reason(resident))
        return workdir, identity

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
        canonical = candidate.resolve(strict=True)
        after = candidate.stat(follow_symlinks=False)
        before_identity = (before.st_dev, before.st_ino, stat.S_IFMT(before.st_mode))
        after_identity = (after.st_dev, after.st_ino, stat.S_IFMT(after.st_mode))
        if before_identity != after_identity or not stat.S_ISDIR(after.st_mode):
            raise _DirectoryChangedError("declared directory changed while it was inspected")
        return _DirectoryIdentity(canonical, *after_identity)

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
            skills = self._provision(resident, admission.workdir)
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
            runner = self.runner_factory(resident.manifest.runner)
            result = runner.run(
                RunRequest(
                    prompt=prompt,
                    workdir=admission.workdir,
                    timeout_s=timeout_s,
                    model=resident.manifest.runner.model,
                    env=wake.environment(resident),
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
        completed_at = admission.admitted_at + timedelta(seconds=result.duration_s)
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
        memory = resident.manifest.memory
        if memory.kind != "directory":
            return None
        if skills_home(resident.manifest.runner) is None or not self.library.configured:
            return None
        if admission.declared_identity is None:
            return None
        candidate = Path(memory.path).expanduser()
        try:
            observed = self._observe_directory(candidate)
        except _DirectoryChangedError:
            return Refusal(self._changed_workdir_reason(resident))
        except OSError:
            return Refusal(self._vanished_workdir_reason(resident))
        if observed is None:
            return Refusal(self._vanished_workdir_reason(resident))
        if observed != admission.declared_identity:
            return Refusal(self._changed_workdir_reason(resident))
        return None

    def _require_revalidated(self, admission: Admission) -> None:
        """Turn a late workdir refusal into the lifecycle's provision-failure path."""
        refusal = self.revalidate(admission)
        if refusal is not None:
            raise SkillError(refusal.reason)

    def _vanished_workdir_reason(self, resident: Resident) -> str:
        return (
            f"memory.path {resident.manifest.memory.path!r} is no longer the directory "
            "admitted for this session; steward refuses to fall back to the current "
            "working directory"
        )

    def _changed_workdir_reason(self, resident: Resident) -> str:
        return (
            f"memory.path {resident.manifest.memory.path!r} canonical path or filesystem "
            "identity changed after admission; steward refuses to provision or run in "
            "the replacement directory"
        )

    def _skills_for(self, resident: Resident) -> tuple[Skill, ...]:
        return effective_skills(resident.manifest, self.library)

    def _provision(self, resident: Resident, workdir: Path) -> tuple[Skill, ...]:
        missing = missing_skills(resident.manifest, self.library)
        if missing:
            raise SkillError(describe_missing(resident.id, missing, self.library))
        skills = self._skills_for(resident)
        subdir = skills_home(resident.manifest.runner)
        if subdir is not None and self.library.configured:
            result = materialize(skills, workdir, subdir)
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
