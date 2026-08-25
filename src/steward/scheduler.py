"""The scheduler: residents act without being prompted, and only when they really do.

The scheduler reads validated manifests, computes each enabled routine's next fire in
that routine's own ``schedule_tz``, launches the session through the resident's runner
(:mod:`steward.runners` — the only thing here that starts a process), and brackets
every run with real burrow events.

Fire-time semantics are deliberately honest rather than clever:

**No back-fill.** A fire more than ``catchup_s`` late (default 5 minutes) is not run at
all; the routine is re-anchored to now and the next occurrence is scheduled normally.
A daemon that was down all morning does not run the 7am summary at noon and call it
the morning summary.

**One run per routine at a time.** An overlapping fire is skipped and logged, never
queued. A queue would turn "Hob is doing the hourly inbox read" into a backlog of
sessions claiming to be hourly.

**Restart changes nothing.** The last fire time of every routine is persisted (default
``.steward/state/scheduler.json``), so restarting neither re-fires what already ran nor
duplicates what is in flight. A brand-new routine is anchored at first sight and fires
at its next occurrence — never immediately, because "it is 09:04 and I have never run"
is not the same fact as "it is 07:00".

**Every run is bracketed.** ``routine_started`` before, then exactly one of
``routine_finished`` / ``routine_failed`` after — including when steward killed the
session at its timeout. A hung session must never look like work.

**Every session is provisioned with the skills its manifest grants.** At fire time the
resident's effective set — the library's defaults plus its own grants
(:mod:`steward.skills`) — is injected into the prompt, and for a runner that loads
skills off disk it is also written into the session's working directory. A granted
skill the library does not have fails the run before it starts; steward will not launch
a session that believes it has a capability nobody gave it.

**Every session opens with its journal, and one closes the day.** The latest surviving
entry goes into the preamble (:mod:`steward.journal`), and the single routine a manifest
flags ``journal: close_of_day`` gets the instruction to write the next one. After an
``ok`` close-of-day run, steward keeps whatever entry exists and rotates the rest. It
writes an entry itself only from an explicit ``<journal>`` block in the session's output,
and never over a file the session wrote for itself.

**Everything else that happens around a wake-up hangs off one hook.** The job board,
structured approvals, and delegation reach the scheduler through the structural
:class:`WakeHooks` protocol and nothing else: decisions in before a prompt is assembled,
escalations and handoffs out after a session ends, dispatch after the due routines have
fired. Budgets reach it the same way, through :class:`RunGuard`. A steward with no hooks
and no guard fires routines exactly as it did before any of them existed — unbounded,
which is what it was — and a rehearsal touches no database: a dry run that consumed a
real decision would silence the session that needed it, and a dry run that materialized
skills would write into a real workdir.
"""

import contextlib
import fcntl
import json
import logging
import os
import threading
import time
import uuid
from collections.abc import Callable, Iterator, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Protocol
from zoneinfo import ZoneInfo

from croniter import croniter

from steward import events as ev
from steward import journal
from steward.manifest import (
    ManifestError,
    Resident,
    ResidentManifest,
    Routine,
    active_residents,
    validate_path,
)
from steward.manifest import Runner as RunnerSpec
from steward.prompt import assemble_routine_prompt
from steward.runners import (
    Outcome,
    Runner,
    RunRequest,
    RunResult,
    build_runner,
    check_runner,
    skills_home,
)
from steward.skills import (
    Materialization,
    Skill,
    SkillError,
    SkillLibrary,
    describe_missing,
    effective_skills,
    materialize,
    missing_skills,
)

__all__ = [
    "DEFAULT_CATCHUP_S",
    "DEFAULT_STATE_PATH",
    "STALE_TICK_AFTER_S",
    "TRIGGER_MANUAL",
    "TRIGGER_SCHEDULE",
    "FireReport",
    "RunGuard",
    "ScheduledRoutine",
    "Scheduler",
    "SchedulerError",
    "SchedulerState",
    "WakeHooks",
    "default_state_path",
    "latest_fire_at_or_before",
    "load_scheduled",
    "next_fire_after",
    "scheduler_liveness",
    "workdir_refusal",
]

log = logging.getLogger("steward.scheduler")

DEFAULT_STATE_PATH = Path(".steward/state/scheduler.json")
STATE_ENV = "STEWARD_STATE"

#: How late a fire may be and still be honest work rather than a back-fill.
DEFAULT_CATCHUP_S = 300.0

#: The daemon never sleeps longer than this, so a clock jump or a slow run cannot
#: strand it past the next occurrence.
MAX_SLEEP_S = 60.0
MIN_SLEEP_S = 0.05

#: How long a state file may go untouched before "a scheduler is up" stops being a safe
#: reading of it. Not a new number: a living daemon stamps the file at least every
#: :data:`MAX_SLEEP_S`, and a fire is still honest work up to ``catchup_s`` late, so a
#: heartbeat older than the two together could not have fired anything on time anyway.
STALE_TICK_AFTER_S = MAX_SLEEP_S + DEFAULT_CATCHUP_S

#: Why a run happened. ``schedule`` is the clock coming round; ``manual`` is a human
#: asking for it now through the API. The ledger has to be able to tell them apart.
TRIGGER_SCHEDULE = "schedule"
TRIGGER_MANUAL = "manual"

#: How a manifest's runner declaration becomes a runner. Injectable for tests.
type RunnerFactory = Callable[[RunnerSpec], Runner]
STATE_VERSION = 0


class SchedulerError(Exception):
    """Raised when the scheduler refuses to start — loudly, in daylight."""


class WakeHooks(Protocol):
    """What else happens around a wake-up, besides the routine that was due.

    A structural protocol rather than an import, because the thing that satisfies it —
    :class:`steward.board.Dispatcher` — sits *above* the scheduler and needs the
    scheduler's own types. Three questions, asked at three moments:

    - before a session is assembled, "does this resident have decisions waiting?";
    - after a session ends, "did it ask for anything?";
    - after the due routines have fired, "is there board work to pick up?".

    The scheduler runs perfectly well with no hooks at all: a steward without a job board
    and without approvals fires routines exactly as it did before either existed.
    """

    def decisions_for(self, resident_id: str) -> str | None:
        """Return decisions to tell this resident about, and mark them told."""
        ...

    def harvest(self, manifest: ResidentManifest, output: str) -> object:
        """Turn what a finished session wrote into requests and handoffs.

        Approvals it raised (:mod:`steward.approvals`) and work it handed to another
        resident (:mod:`steward.delegation`) both leave a session the same way — as a
        block in its final output — so both are read here rather than through a second
        question the scheduler would have to learn to ask.
        """
        ...

    def dispatch(self, now: datetime) -> object:
        """Sweep deadlines and let board-enabled residents claim work."""
        ...


class RunGuard(Protocol):
    """Whether a resident may run at all, how long it gets, and what the run cost.

    The budget seam (steward #8), and a structural protocol for the same reason
    :class:`WakeHooks` is one: :class:`steward.budgets.BudgetGuard` needs the scheduler's
    types, so the dependency runs the other way and the scheduler never imports it. A
    steward with no guard fires routines exactly as it did before budgets existed —
    unbounded, which is what it was.

    Three moments, and the order matters. :meth:`allow` is asked *before* the prompt is
    assembled, because assembling one delivers a resident's pending decisions and a
    refused fire must not eat an answer the next real session needs to hear.
    """

    def allow(self, manifest: ResidentManifest, now: datetime | None = None) -> str | None:
        """Return why this resident may not run at ``now``, or ``None`` when it may."""
        ...

    def timeout_for(self, manifest: ResidentManifest, declared_s: int) -> int:
        """Return the timeout this run actually gets, capped by the manifest's budget."""
        ...

    def record(  # noqa: PLR0913 — the run, named by its kind, its id, and what it cost
        self,
        manifest: ResidentManifest,
        *,
        result: RunResult,
        kind: str,
        run_id: str,
        ref: str,
        now: datetime | None = None,
    ) -> object:
        """Append what one finished session cost to the durable ledger."""
        ...


# --------------------------------------------------------------------------------------
# cron
# --------------------------------------------------------------------------------------


def _wall_minute(moment: datetime) -> str:
    """Render a moment as its local wall clock: what a cron line actually names."""
    return moment.strftime("%Y-%m-%d %H:%M")


def next_fire_after(routine: Routine, after: datetime) -> datetime:
    """Return the first occurrence of ``routine`` strictly after ``after``.

    The cron expression is read in the routine's ``schedule_tz``, so "07:00" means
    07:00 where the household is, not where the NAS thinks it is. The returned moment
    is timezone-aware in that same zone; compare it against an aware ``now``.

    DST is resolved on the wall clock, because that is what the manifest wrote down:

    - **Spring forward.** A time that does not exist that morning lands on the next
      minute that does, so a 02:30 routine runs at 03:00 and the day is not skipped.
    - **Autumn fall back.** The repeated hour is the *same* wall-clock slot twice, so
      the routine fires on the first pass only. Running it again an hour later would
      be a second run the schedule never asked for.
    """
    zone = ZoneInfo(routine.schedule_tz)
    local = after.astimezone(zone)
    cursor = croniter(routine.schedule, local)
    candidate = cursor.get_next(datetime)
    while _wall_minute(candidate) <= _wall_minute(local):
        candidate = cursor.get_next(datetime)
    return candidate


def latest_fire_at_or_before(routine: Routine, moment: datetime) -> datetime:
    """Return the most recent occurrence of ``routine`` at or before ``moment``.

    This is what "is this routine due *right now*" is asked against, so a fire that
    just came round is honest work while an older one is a missed schedule.
    """
    zone = ZoneInfo(routine.schedule_tz)
    local = moment.astimezone(zone)
    return croniter(routine.schedule, local + timedelta(seconds=1)).get_prev(datetime)


# --------------------------------------------------------------------------------------
# what is scheduled
# --------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ScheduledRoutine:
    """One routine of one resident, with everything needed to fire it."""

    resident: Resident
    routine: Routine

    @property
    def key(self) -> str:
        """The stable state key: ``<resident id>/<routine id>``."""
        return f"{self.resident.id}/{self.routine.id}"

    @property
    def agent_id(self) -> str:
        """The burrow identity events are emitted under."""
        return self.resident.agent_id

    @property
    def project(self) -> str:
        """The burrow project label: the manifest's project, else the resident id."""
        return self.resident.project

    def workdir(self, fallback: Path) -> Path:
        """Where the session runs — the resident's own declared location, or the fallback."""
        return self.resident.workdir(fallback)

    def next_fire_after(self, after: datetime) -> datetime:
        """Return the routine's next occurrence after a moment."""
        return next_fire_after(self.routine, after)


@dataclass(frozen=True, slots=True)
class FireReport:
    """What one fire decision came to. Every field is something that happened."""

    scheduled: ScheduledRoutine
    run_id: str
    fired: bool
    skipped_reason: str | None = None
    result: RunResult | None = None
    prompt: str = ""
    #: Where the day's journal entry ended up, when this was the closing routine and an
    #: entry exists. ``None`` means no entry — which is a real answer, not a failure.
    journal_path: Path | None = None

    @property
    def routine_id(self) -> str:
        """Name the routine this report is about."""
        return self.scheduled.routine.id


# --------------------------------------------------------------------------------------
# persisted state
# --------------------------------------------------------------------------------------


def _moment(raw: str | None) -> datetime | None:
    """Read a stored ISO timestamp back. Unparseable is ``None``; naive is read as UTC."""
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


@dataclass
class SchedulerState:
    """The minimum steward must remember across restarts: when each routine last ran.

    ``anchor`` is the moment a routine's next occurrence is computed from — the last
    fire, or the moment the routine was first seen. Keeping it is what makes a restart
    truthful in both directions: nothing re-fires, and nothing is silently skipped
    because the process forgot it existed.

    ``last_tick`` is the one liveness fact in the file: when a scheduler process last woke
    up here. Anchors say what already ran; only this says whether anything is still around
    to run the next occurrence — which is what turns a ledger of promises into a report.
    """

    path: Path
    anchors: dict[str, str] = field(default_factory=dict)
    #: When a scheduler last woke up against this file, in UTC. ``None`` is a real answer:
    #: nothing has ever ticked here, so nothing has ever fired from it.
    last_tick: str | None = None

    @classmethod
    def load(cls, path: Path | str) -> SchedulerState:
        """Read state from disk. A missing or unreadable file is an empty state.

        A file written before ``last_tick`` existed simply has never ticked as far as this
        reader is concerned — old files stay readable, and old readers ignore the new key.
        """
        target = Path(path)
        try:
            raw = json.loads(target.read_text(encoding="utf-8"))
        except OSError, ValueError:
            return cls(path=target)
        if not isinstance(raw, dict):
            return cls(path=target)
        tick = raw.get("last_tick")
        last_tick = tick if isinstance(tick, str) and tick else None
        anchors = raw.get("routines")
        if not isinstance(anchors, dict):
            return cls(path=target, last_tick=last_tick)
        return cls(
            path=target,
            anchors={
                str(key): str(value.get("anchor"))
                for key, value in anchors.items()
                if isinstance(value, dict) and value.get("anchor")
            },
            last_tick=last_tick,
        )

    def reload(self) -> None:
        """Re-read the anchors from disk, picking up what another process just wrote.

        The other half of "fires exactly once across two tick processes" (steward #76):
        holding the lock keeps two ticks from writing at once, but a tick that computed
        *due* against a stale in-memory anchor would still re-fire what the other one
        already ran. Reloading under the lock, before ``due`` is computed, is what makes
        the second tick see the first one's anchor and find nothing due.
        """
        self.anchors.update(type(self).load(self.path).anchors)

    def anchor(self, key: str) -> datetime | None:
        """Return the moment this routine's next occurrence is computed from, if known."""
        return _moment(self.anchors.get(key))

    def set_anchor(self, key: str, moment: datetime) -> None:
        """Record a new anchor, in UTC."""
        self.anchors[key] = moment.astimezone(UTC).isoformat()

    def last_tick_at(self) -> datetime | None:
        """Return when a scheduler last woke up here, or ``None`` if none ever has."""
        return _moment(self.last_tick)

    def record_tick(self, moment: datetime) -> None:
        """Record that a scheduler woke up now, in UTC. Persisted by the next ``save()``."""
        self.last_tick = moment.astimezone(UTC).isoformat()

    def save(self) -> None:
        """Write the state atomically, so a kill mid-write cannot corrupt it."""
        payload: dict[str, Any] = {
            "version": STATE_VERSION,
            "routines": {key: {"anchor": value} for key, value in sorted(self.anchors.items())},
            "last_tick": self.last_tick,
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(f"{self.path.suffix}.tmp")
        temporary.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        temporary.replace(self.path)


def scheduler_liveness(state: SchedulerState, now: datetime | None = None) -> dict[str, Any]:
    """Say whether anything is still ticking this state file. The API and doctor share it.

    Three answers, and the third is the one worth having a shape for: ``alive`` is ``True``
    when the heartbeat is fresher than :data:`STALE_TICK_AFTER_S`, ``False`` when it is
    older — a daemon that died is not the same as one that never existed — and ``None``
    when nothing has ever ticked, which is what a fresh install honestly reports.
    """
    last = state.last_tick_at()
    moment = now or datetime.now(UTC)
    return {
        "last_tick": last.isoformat() if last is not None else None,
        "stale_after_s": STALE_TICK_AFTER_S,
        "alive": None if last is None else (moment - last).total_seconds() <= STALE_TICK_AFTER_S,
    }


def default_state_path(env: dict[str, str] | None = None) -> Path:
    """Return ``$STEWARD_STATE``, or ``.steward/state/scheduler.json`` under the cwd."""
    source = os.environ if env is None else env
    configured = (source.get(STATE_ENV) or "").strip()
    return Path(configured).expanduser() if configured else DEFAULT_STATE_PATH


# --------------------------------------------------------------------------------------
# the scheduler
# --------------------------------------------------------------------------------------


def workdir_refusal(resident: Resident, fallback: Path, library: SkillLibrary) -> str | None:
    """Return why this resident has nowhere safe to run, or ``None`` when it does.

    A resident whose ``memory: {kind: directory}`` names a path that is not a directory on
    this host has *no* declared working directory: :meth:`Resident.workdir` falls back to
    the caller's. The destructive case steward #64 is about is precise, and this refuses
    exactly it — nothing wider, so a legitimately-chosen workdir keeps working:

    - the memory directory is **absent**, so the run would fall back; **and**
    - the fallback is the process's **own current working directory** — "NEVER a cwd
      fallback"; an explicitly chosen workdir is the operator's decision, not a silent
      default; **and**
    - the session would **materialize skills** into it — a skills-loading runner
      (:func:`steward.runners.skills_home`) plus a configured library — which is what makes
      :meth:`Scheduler.provision` / :meth:`Dispatcher.provision` prune everything under
      ``<cwd>/.claude/skills`` the resident was not granted, wiping a checkout's own skills.

    A runner that writes no skills, or an unconfigured library, deletes nothing, so it is
    left to run; ``file`` and ``repo`` memories declare no working directory and are left
    alone too.
    """
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
    except OSError:  # pragma: no cover — an unresolvable cwd is its own refusal
        effective = cwd = Path()
    if effective != cwd:
        return None
    return (
        f"memory.path {memory.path!r} is not a directory on this host, so this resident "
        "would run in the current working directory — steward refuses that fallback rather "
        "than materialize skills into, and delete files from, a directory its charter never "
        "named"
    )


def load_scheduled(
    residents_dir: Path | str, skills_dir: Path | str | None = None
) -> list[ScheduledRoutine]:
    """Collect every enabled routine of every valid resident under a residents tree.

    Invalid manifests never reach the scheduler: :func:`validate_path` returns only
    residents that passed, and the diagnostics are the caller's to report. A granted
    skill that names nothing in the library is one of those diagnostics, so nothing
    that could not be provisioned is scheduled in the first place.

    Retired residents are left out too, and for a plainer reason: a manifest that says
    ``retired: true`` has said this resident does no more work. Its routines are still
    declared, still valid, and still readable in git — they simply never fire, which is
    what retirement means. Un-retire it and commit, and they fire again.
    """
    result = validate_path(residents_dir, skills_dir)
    if not result.ok:
        raise SchedulerError(
            "cannot schedule from an invalid residents tree:\n"
            + "\n".join(d.render() for d in result.errors)
        )
    return [
        ScheduledRoutine(resident=resident, routine=routine)
        for resident in active_residents(result.residents)
        for routine in resident.manifest.routines
        if routine.enabled
    ]


class Scheduler:
    """Fires due routines, once each, and tells the village the truth about it."""

    def __init__(  # noqa: PLR0913 — every knob is keyword-only and independently useful
        self,
        scheduled: Sequence[ScheduledRoutine],
        *,
        emitter: ev.Emitter | None = None,
        state: SchedulerState | None = None,
        workdir: Path | None = None,
        catchup_s: float = DEFAULT_CATCHUP_S,
        dry_run: bool = False,
        max_workers: int = 4,
        runner_factory: RunnerFactory = build_runner,
        library: SkillLibrary | None = None,
        hooks: WakeHooks | None = None,
        guard: RunGuard | None = None,
    ) -> None:
        """Assemble a scheduler over an explicit list of routines."""
        self.scheduled = list(scheduled)
        self.dry_run = dry_run
        # No guard means no budget: unbounded, exactly as it was before steward #8.
        self.guard = guard
        # One library for the fleet: improving a skill improves every resident holding
        # it. An unconfigured library means no skill is injected and none is written.
        self.library = library if library is not None else SkillLibrary()
        self.hooks = hooks
        self.emitter: ev.Emitter = emitter or (
            ev.NullEmitter() if dry_run else ev.EventEmitter.from_env()
        )
        self.state = state or SchedulerState.load(default_state_path())
        self.workdir = Path(workdir) if workdir is not None else Path.cwd()
        self.catchup_s = catchup_s
        self.max_workers = max_workers
        # A rehearsal must not be able to reach a real brain, whatever the manifest says.
        self._runner_factory = (
            (lambda spec: build_runner(spec, force_mock=True)) if dry_run else runner_factory
        )
        self._running: set[str] = set()
        self._lock = threading.Lock()
        # One lock per resident, held across allow → run → record so two due routines of
        # the same resident cannot both pass one pre-ledger budget read (steward #68).
        self._resident_locks: dict[str, threading.Lock] = {}

    # -- startup validation ----------------------------------------------------------

    def check(self) -> list[str]:
        """Return why the declared runners cannot run. Empty means ready.

        Called before the daemon takes its first breath, so "the ``claude`` binary is
        not on PATH" is a startup error at a reasonable hour rather than a routine
        that silently never happened.

        The resident's journal location is checked here too, and for the same reason:
        every session opens with its journal, so a memory block steward cannot journal
        into is a complaint in daylight rather than a midnight write into nowhere.
        """
        problems: list[str] = []
        seen: set[str] = set()
        for item in self.scheduled:
            if item.resident.id in seen:
                continue
            seen.add(item.resident.id)
            missing = missing_skills(item.resident.manifest, self.library)
            complaints = (
                check_runner(item.resident.manifest.runner),
                journal.journal_complaint(item.resident.manifest),
                describe_missing(item.resident.id, missing, self.library) if missing else None,
                workdir_refusal(item.resident, self.workdir, self.library),
            )
            problems.extend(f"{item.resident.path}: {c}" for c in complaints if c)
        return problems

    def require_ready(self) -> None:
        """Raise :class:`SchedulerError` unless every declared runner can run."""
        if self.dry_run:
            return
        self._ensure_state_ready()
        problems = self.check()
        if problems:
            raise SchedulerError("\n".join(problems))

    def _ensure_state_ready(self) -> None:
        """Make the state file persistable, loudly, before anything relies on it.

        A ``STEWARD_STATE`` that names a directory (or is otherwise unwritable) used to be a
        warning that steward swallowed on its way to exiting 0 — a scheduler that cannot
        remember its anchor, which re-fires or re-anchors forever, pretending everything is
        fine (steward #85). It is fatal here instead: a scheduler that cannot persist state
        must not run. A stray ``state.tmp`` from a write killed mid-flight is cleared, so a
        crash does not leave a booby-trap behind.
        """
        if self.dry_run:
            return
        path = self.state.path
        # Clear a stray temp from a write killed mid-flight first, so a crash — including the
        # one a directory-shaped state itself causes — does not leave a booby-trap behind.
        temporary = path.with_suffix(f"{path.suffix}.tmp")
        with contextlib.suppress(OSError):
            temporary.unlink(missing_ok=True)
        if path.is_dir():
            raise SchedulerError(
                f"STEWARD_STATE names a directory, not a file: {path} — steward cannot "
                "persist its scheduler anchors there, and a scheduler that cannot remember "
                "what it already fired re-fires forever. Point STEWARD_STATE at a file."
            )
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise SchedulerError(
                f"cannot create the directory for STEWARD_STATE {path}: {exc}"
            ) from exc

    @contextlib.contextmanager
    def _state_lock(self) -> Iterator[None]:
        """Hold an exclusive OS lock over the whole tick, so two ticks never both fire.

        The documented ``scheduler tick`` deployment runs under external cron, so two ticks
        really can overlap — a slow run, a cron that fires while the last one is still
        going. Without a cross-process lock both see the same occurrence due and both fire
        it (steward #76). ``flock`` on a sidecar ``.lock`` file serialises them: the second
        tick blocks until the first has anchored, saved, and fired, then reloads the state
        the first one wrote and finds nothing due.
        """
        lock_path = self.state.path.with_suffix(f"{self.state.path.suffix}.lock")
        fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o644)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)

    def _resident_lock(self, resident_id: str) -> threading.Lock:
        """Return the lock held across one resident's allow → run → record."""
        with self._lock:
            lock = self._resident_locks.get(resident_id)
            if lock is None:
                lock = threading.Lock()
                self._resident_locks[resident_id] = lock
            return lock

    # -- due calculation -------------------------------------------------------------

    def _anchor_for(self, item: ScheduledRoutine, now: datetime) -> datetime:
        anchor = self.state.anchor(item.key)
        if anchor is None:
            # First sight: anchor at now, so a routine never fires just for existing.
            self.state.set_anchor(item.key, now)
            return now
        return anchor

    def due(self, now: datetime) -> list[ScheduledRoutine]:
        """Return what should fire at ``now``, re-anchoring anything that is too late.

        A routine is due when an occurrence has passed since its anchor *and* the most
        recent occurrence is fresh — within ``catchup_s``. Anything older is a schedule
        steward slept through: it is dropped, the routine is re-anchored to now, and
        the next occurrence happens on time. Whatever was missed stays missed, because
        running the 7am summary at noon and calling it the morning is a lie.
        """
        ready: list[ScheduledRoutine] = []
        for item in self.scheduled:
            anchor = self._anchor_for(item, now)
            if item.next_fire_after(anchor) > now:
                continue
            latest = latest_fire_at_or_before(item.routine, now)
            lateness = (now - latest).total_seconds()
            if lateness > self.catchup_s:
                log.info(
                    "%s: the last occurrence was %.0fs ago — steward does not back-fill "
                    "missed schedules, so this one is skipped",
                    item.key,
                    lateness,
                )
                self.state.set_anchor(item.key, now)
                continue
            ready.append(item)
        return ready

    def next_due_at(self, now: datetime) -> datetime | None:
        """Return the earliest next occurrence across all routines, if there is one."""
        moments = [item.next_fire_after(self._anchor_for(item, now)) for item in self.scheduled]
        return min(moments) if moments else None

    # -- firing ----------------------------------------------------------------------

    def _claim(self, key: str) -> bool:
        with self._lock:
            if key in self._running:
                return False
            self._running.add(key)
            return True

    def _release(self, key: str) -> None:
        with self._lock:
            self._running.discard(key)

    def build_prompt(self, item: ScheduledRoutine, now: datetime | None = None) -> str:
        """Assemble the full prompt for one routine, through the one prompt module."""
        return assemble_routine_prompt(
            item.resident.manifest,
            item.routine.prompt,
            soul_text=item.resident.soul.body,
            journal_entry=self.journal_for(item),
            skills=self.skills_for(item),
            decisions=self.decisions_for(item),
            closing=self.closing_for(item, now or datetime.now(UTC)),
        )

    def skills_for(self, item: ScheduledRoutine) -> tuple[Skill, ...]:
        """Resolve this resident's effective skill set: defaults plus its own grants.

        Resolved at fire time from the library on disk, so improving a skill improves
        the next session of every resident that holds it, and a skill removed from a
        manifest is simply absent from the next run.
        """
        return effective_skills(item.resident.manifest, self.library)

    def provision(self, item: ScheduledRoutine, workdir: Path) -> Materialization | None:
        """Put this resident's skills where the session can reach them. Or refuse.

        Two things happen here, and which ones apply is the runner's business:

        - **Every** runner kind gets the skills in its prompt, assembled above. That is
          the copy steward can honestly say the session was told.
        - A runner that loads skills off disk (``claude``) also gets them written into
          ``<workdir>/<skills_dir>``, write-if-changed, with anything no longer granted
          removed. Steward owns that directory.

        A granted skill the library does not have is a refusal, not a shrug: the run
        fails before it starts rather than launching a session that believes it has a
        capability nobody gave it.
        """
        missing = missing_skills(item.resident.manifest, self.library)
        if missing:
            raise SkillError(describe_missing(item.resident.id, missing, self.library))
        subdir = skills_home(item.resident.manifest.runner)
        if subdir is None or not self.library.configured:
            return None
        result = materialize(self.skills_for(item), workdir, subdir)
        log.debug("%s: skills %s", item.key, result.summary())
        return result

    def decisions_for(self, item: ScheduledRoutine) -> str | None:
        """Return the answers this resident has not been told about yet, or ``None``.

        This is the resident-inbox half of steward #10: a session that parked on an
        approval finishes its turn, and the decision reaches it here, at the top of its
        next wake-up. With no hooks wired there is nothing to deliver and the preamble is
        byte-identical to one assembled before approvals existed.

        A dry run gets nothing, and the reason matters: delivery is a *write*. A rehearsal
        that consumed a real decision would mean the resident's next real session never
        heard the answer — a rehearsal is not work, and it must not be able to eat one.

        A hook that raises degrades to no decisions and a log line rather than taking the
        whole fire down with zero events, exactly as the harvest hook already does
        (steward #80): a session that opens without a decision it was owed is a bug to fix,
        but a routine that never runs — and never brackets — because delivery threw is worse.
        """
        if self.hooks is None or self.dry_run:
            return None
        try:
            return self.hooks.decisions_for(item.resident.id)
        except Exception as exc:  # noqa: BLE001 — a broken hook is a missing decision, not a crash
            log.warning("%s: could not read pending decisions: %s", item.key, exc)
            return None

    def journal_for(self, item: ScheduledRoutine) -> str | None:
        """Return the resident's latest surviving journal entry, or ``None``.

        Read through :mod:`steward.journal`, so the location comes from this resident's
        own manifest and from nowhere else. A resident that has never journaled, or
        whose last session died before it wrote, gets the previous surviving entry or
        nothing — steward never synthesizes one.

        A journal that cannot be read *right now* degrades to no journal and a log line
        rather than a failed routine. The declaration itself is checked in
        :meth:`check`, which is where a broken one is supposed to be loud.
        """
        return self._journal_call(item, lambda: journal.latest_entry(item.resident.manifest))

    def closing_for(self, item: ScheduledRoutine, now: datetime) -> str | None:
        """Return the close-of-day instruction, on the flagged routine and nowhere else.

        The contract is the explicit ``journal: close_of_day`` flag in the manifest, not
        an inference about which routine happens to fire last today. The day the entry
        belongs to is read in this routine's own ``schedule_tz``.
        """
        if item.routine.journal != journal.CLOSE_OF_DAY:
            return None
        day = journal.local_day(item.routine, now)
        return self._journal_call(
            item,
            lambda: journal.close_of_day_instruction(
                item.resident.manifest, day, item.routine.id, source=item.resident.path
            ),
        )

    def _journal_call[T](self, item: ScheduledRoutine, read: Callable[[], T]) -> T | None:
        try:
            return read()
        except ManifestError as exc:
            log.warning("%s: no journal — %s", item.key, exc)
        except Exception as exc:  # noqa: BLE001 — a bad byte in a journal is not a failed routine
            # Widened past OSError on purpose: a session-written journal with one undecodable
            # byte raises UnicodeDecodeError (a ValueError), and letting that escape used to
            # brick the resident or leave a dangling routine_started (steward #75). The
            # declaration itself is checked in check(), where a broken one is meant to be loud.
            log.warning("%s: could not reach the journal: %s", item.key, exc)
        return None

    def fire(
        self,
        item: ScheduledRoutine,
        *,
        trigger: str = TRIGGER_SCHEDULE,
        now: datetime | None = None,
    ) -> FireReport:
        """Run one routine end to end, bracketed by events. Never raises."""
        run_id = str(uuid.uuid4())
        moment = now or datetime.now(UTC)
        if not self._claim(item.key):
            log.warning(
                "%s: previous run is still going — skipping this fire rather than queueing a lie",
                item.key,
            )
            return FireReport(
                scheduled=item, run_id=run_id, fired=False, skipped_reason="already running"
            )
        try:
            return self._fire_claimed(item, run_id, trigger, moment)
        finally:
            self._release(item.key)

    def _fire_claimed(
        self, item: ScheduledRoutine, run_id: str, trigger: str, moment: datetime
    ) -> FireReport:
        # Hold the resident's lock across allow → run → record, so two of its due routines
        # firing at once cannot both read the budget before either has ledgered and both
        # sail past an exhausted cap (steward #68). No guard means no ledger and nothing
        # to serialise, and the run stays as concurrent as it ever was.
        guard_lock = self._resident_lock(item.resident.id) if self.guard is not None else None
        with guard_lock or contextlib.nullcontext():
            return self._fire_body(item, run_id, trigger, moment)

    def _fire_body(
        self, item: ScheduledRoutine, run_id: str, trigger: str, moment: datetime
    ) -> FireReport:
        refusal = self._budget_refusal(item, moment)
        if refusal is not None:
            return FireReport(scheduled=item, run_id=run_id, fired=False, skipped_reason=refusal)

        # Refuse before the prompt is assembled — assembling delivers pending decisions —
        # so a resident with nowhere to run never has an answer consumed by a fire that was
        # never going to happen, and steward never falls back to the cwd (steward #64).
        if not self.dry_run:
            nowhere = workdir_refusal(item.resident, self.workdir, self.library)
            if nowhere is not None:
                log.error("%s: %s", item.key, nowhere)
                return FireReport(
                    scheduled=item, run_id=run_id, fired=False, skipped_reason=nowhere
                )

        workdir = item.workdir(self.workdir)
        cwd = str(workdir)

        if self.dry_run:
            return FireReport(
                scheduled=item,
                run_id=run_id,
                fired=False,
                prompt=self.build_prompt(item, moment),
                skipped_reason="dry run",
            )

        context = ev.RunContext(
            agent_id=item.agent_id,
            project=item.project,
            routine=item.routine.id,
            run_id=run_id,
            cwd=cwd,
        )
        self.emitter.emit(context.started(trigger))

        started = time.monotonic()
        prompt = ""
        try:
            # Provision *before* the prompt is assembled: assembling it delivers the
            # resident's pending decisions, and a session refused for a missing skill must
            # not eat an answer the next real session still needs to hear (steward #74).
            self.provision(item, workdir)
            prompt = self.build_prompt(item, moment)
            runner: Runner = self._runner_factory(item.resident.manifest.runner)
            result = runner.run(
                RunRequest(
                    prompt=prompt,
                    workdir=workdir,
                    timeout_s=self._timeout_for(item),
                    model=item.resident.manifest.runner.model,
                    env=self._session_env(item, run_id),
                )
            )
        except SkillError as exc:
            # A session that cannot be given what its manifest grants does not run. The
            # bracket still closes: routine_started, then routine_failed saying why.

            # steward refused to start is noise in the log.
            log.error("%s: %s", item.key, exc)  # noqa: TRY400
            result = RunResult(
                outcome=Outcome.FAILED, duration_s=time.monotonic() - started, error=str(exc)
            )
        except Exception as exc:  # noqa: BLE001 — a broken runner is a failed routine, not a crash
            duration = time.monotonic() - started
            result = RunResult(
                outcome=Outcome.FAILED, duration_s=duration, error=f"{type(exc).__name__}: {exc}"
            )

        # Stamp the ledger at *completion*, not at fire time: a run that started at 23:50
        # and crossed midnight belongs to the day it finished in, or a once-daily routine's
        # spend lands in a window its own next fire never re-reads and the cap never trips
        # (steward #68).
        self._ledger(item, run_id, result, moment + timedelta(seconds=result.duration_s))
        self._harvest(item, result.output)

        journal_path: Path | None = None
        if result.ok:
            closed = self.close_the_day(item, moment, result.output)
            journal_path = closed.path
            if closed.persisted and journal_path is not None:
                # Steward wrote this file, so steward can name it. Everything else in
                # artifacts is the session's own claim, reported by its own emitter.
                result = replace(result, artifacts=(*result.artifacts, str(journal_path)))
            self.emitter.emit(
                context.finished(
                    outcome=str(result.outcome),
                    artifacts=result.artifacts,
                    duration_s=result.duration_s,
                )
            )
        else:
            self.emitter.emit(
                context.failed(
                    error=f"{result.outcome}: {result.summary()}",
                    duration_s=result.duration_s,
                )
            )
        return FireReport(
            scheduled=item,
            run_id=run_id,
            fired=True,
            result=result,
            prompt=prompt,
            journal_path=journal_path,
        )

    def close_the_day(
        self, item: ScheduledRoutine, moment: datetime, output: str
    ) -> journal.CloseOfDay:
        """Keep whatever entry the closing routine produced, then rotate. Never raises.

        The session's own file wins; a ``<journal>`` block in its output is the fallback
        for a run that had nowhere to write. Only an ``ok`` run closes a day — a session
        killed at its timeout did not finish its day, and dating a half-written note
        would make tomorrow believe a day happened that did not.
        """
        if item.routine.journal != journal.CLOSE_OF_DAY:
            return journal.CloseOfDay()
        day = journal.local_day(item.routine, moment)
        closed = self._journal_call(
            item,
            lambda: journal.persist_close_of_day(
                item.resident.manifest,
                day,
                item.routine.id,
                output,
                source=item.resident.path,
            ),
        )
        return closed if closed is not None else journal.CloseOfDay()

    def _budget_refusal(self, item: ScheduledRoutine, moment: datetime) -> str | None:
        """Ask the budget whether this resident may run at all, before anything is spent.

        Asked before the prompt is assembled, because assembling one *delivers* the
        resident's pending approval decisions, and a fire refused after that would have
        eaten an answer the next real session needed to hear. A guard that raises is a
        guard steward refuses to trust into silence: an unreadable budget stops the run
        rather than waving it through.
        """
        if self.guard is None:
            return None
        try:
            refusal = self.guard.allow(item.resident.manifest, moment)
        except Exception as exc:  # noqa: BLE001 — an unreadable budget is a refusal, not a crash
            log.warning("%s: could not read the budget, so nothing fires: %s", item.key, exc)
            return f"budget unreadable: {type(exc).__name__}: {exc}"
        if refusal is not None:
            log.warning("%s: %s", item.key, refusal)
        return refusal

    def _timeout_for(self, item: ScheduledRoutine) -> int:
        """Return this run's effective timeout: the declared one, capped by the budget."""
        if self.guard is None:
            return item.routine.timeout_s
        return self.guard.timeout_for(item.resident.manifest, item.routine.timeout_s)

    def _ledger(
        self, item: ScheduledRoutine, run_id: str, result: RunResult, moment: datetime
    ) -> None:
        """Record what this run cost. Never raises: a lost row is not a failed routine."""
        if self.guard is None:
            return
        try:
            self.guard.record(
                item.resident.manifest,
                result=result,
                kind="routine",
                run_id=run_id,
                ref=item.routine.id,
                now=moment,
            )
        except Exception as exc:  # noqa: BLE001 — the ledger must not take a routine down
            log.warning("%s: could not record what this run cost: %s", item.key, exc)

    def _harvest(self, item: ScheduledRoutine, output: str) -> None:
        """Turn anything the session asked for into a real approval request.

        A run that timed out is harvested too: a session killed halfway through may still
        have written its escalation, and throwing that away would leave a resident having
        asked a question nobody was told about.
        """
        if self.hooks is None or not output:
            return
        try:
            self.hooks.harvest(item.resident.manifest, output)
        except Exception as exc:  # noqa: BLE001 — a failed escalation is not a failed routine
            log.warning("%s: could not record an approval from this session: %s", item.key, exc)

    def _session_env(self, item: ScheduledRoutine, run_id: str) -> dict[str, str]:
        """Build the env a session inherits, so its own emitter reports as this resident."""
        return {
            "BURROW_AGENT_ID": item.agent_id,
            "BURROW_PROJECT": item.project,
            "STEWARD_ROUTINE": item.routine.id,
            "STEWARD_RUN_ID": run_id,
        }

    # -- the two entry points --------------------------------------------------------

    def tick(self, now: datetime | None = None) -> list[FireReport]:
        """Fire everything due right now, then sweep the board. Useful under external cron.

        The whole "fire this occurrence exactly once" promise is held here, across both a
        crash and a second concurrent tick (steward #76, #85):

        - an unpersistable ``STEWARD_STATE`` is fatal *before* anything fires, not a warning
          on the way to a silent exit 0;
        - the tick takes an exclusive OS lock for its whole duration, reloads the anchors
          another process may have just written, then anchors and **saves** the due set
          *before* firing any of it. A crash mid-run therefore does not re-fire on restart
          (the anchor is already on disk), and a second tick that was waiting on the lock
          reloads that saved state and finds nothing due.

        The board sweep runs after the lock is released: its claims are atomic in their own
        right and do not need the scheduler's state lock, and holding it across board
        sessions would serialise dispatch across processes for no gain.
        """
        moment = now or datetime.now(UTC)
        if self.dry_run:
            # A rehearsal locks nothing and persists nothing; it just reports what would go.
            reports = [self.fire(item, now=moment) for item in self.due(moment)]
            self._dispatch(moment)
            return reports
        self._ensure_state_ready()
        with self._state_lock():
            self.state.reload()
            due = self.due(moment)
            for item in due:
                self.state.set_anchor(item.key, moment)
            self.state.record_tick(moment)
            self._save_state()
            reports = [self.fire(item, now=moment) for item in due]
        self._dispatch(moment)
        return reports

    def _dispatch(self, moment: datetime) -> None:
        """Run the board and the deadline sweeps for this wake-up. Never raises.

        After the due routines, not before: standing work a resident declared for itself
        comes ahead of work somebody dropped on a board. A board that cannot be reached
        is a warning, not a failed tick — the routines that already fired really did fire.
        """
        if self.hooks is None or self.dry_run:
            return
        try:
            self.hooks.dispatch(moment)
        except Exception as exc:  # noqa: BLE001 — the board must not take the scheduler down
            log.warning("board dispatch failed: %s", exc)

    def run(
        self,
        *,
        max_ticks: int | None = None,
        sleep: Callable[[float], object] = time.sleep,
        now_fn: Callable[[], datetime] | None = None,
    ) -> list[FireReport]:
        """Sleep to the next due routine, fire it, repeat.

        Long routines run on a small thread pool so a 15-minute daily summary does not
        hold up an hourly inbox read; the per-routine overlap guard is what makes that
        safe. ``max_ticks`` bounds the loop for tests.
        """
        self.require_ready()
        clock = now_fn or (lambda: datetime.now(UTC))
        reports: list[FireReport] = []
        ticks = 0
        with ThreadPoolExecutor(max_workers=self.max_workers) as pool:
            while max_ticks is None or ticks < max_ticks:
                ticks += 1
                moment = clock()
                due = self.due(moment)
                for item in due:
                    self.state.set_anchor(item.key, moment)
                # Every iteration, including the ones where nothing is due: an idle daemon
                # is still a live one, and this stamp is the only thing that says so.
                self.state.record_tick(moment)
                self._save_state()
                futures = [pool.submit(self.fire, item, now=moment) for item in due]
                self._dispatch(moment)
                if not due:
                    sleep(self._sleep_for(moment))
                reports.extend(future.result() for future in futures)
        return reports

    def _sleep_for(self, now: datetime) -> float:
        upcoming = self.next_due_at(now)
        if upcoming is None:
            return MAX_SLEEP_S
        return max(MIN_SLEEP_S, min(MAX_SLEEP_S, (upcoming - now).total_seconds()))

    def _save_state(self) -> None:
        if self.dry_run:
            # A rehearsal changes nothing, including what steward believes already ran.
            return
        try:
            self.state.save()
        except OSError as exc:  # pragma: no cover — an unwritable state dir is rare
            log.warning("could not persist scheduler state to %s: %s", self.state.path, exc)

    # -- reporting -------------------------------------------------------------------

    def upcoming(self, now: datetime | None = None) -> Iterator[tuple[ScheduledRoutine, datetime]]:
        """Yield every routine with its next fire, for ``steward doctor``."""
        moment = now or datetime.now(UTC)
        for item in self.scheduled:
            yield item, item.next_fire_after(self._anchor_for(item, moment))
