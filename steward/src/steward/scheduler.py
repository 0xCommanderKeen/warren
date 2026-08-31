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

**Every real fire crosses the resident session lifecycle.** The scheduler owns occurrence
selection, overlap, durable anchors, and routine protocol events.  The shared
:mod:`steward.sessions` module owns admission, provision, context, prompt assembly, runner
invocation, completion-time accounting, and safe harvest.  The board still reaches the
scheduler through :class:`WakeHooks` only to dispatch after due routines have fired; its
decision and harvest implementation is passed through to the resident-session module.
A rehearsal touches no database, consumes no decision, materializes no skill, and launches
no runner.
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
from typing import Any, Protocol, cast
from zoneinfo import ZoneInfo

from croniter import croniter

from steward import events as ev
from steward import journal
from steward.deploy import placement_for
from steward.manifest import (
    Resident,
    ResidentManifest,
    Routine,
    active_residents,
    validate_path,
)
from steward.run_lifecycle import RunStore, RunTransitions, event_log_path, new_owner_token
from steward.runners import (
    RunResult,
    build_runner,
    check_runner,
)
from steward.runs import TRIGGER_MANUAL, TRIGGER_SCHEDULE, validate_kind_trigger
from steward.session_auth import new_session_credential
from steward.sessions import (
    Refusal,
    ResidentSessions,
    RoutineWake,
    RunGuard,
    RunnerFactory,
    workdir_refusal,
)
from steward.skills import (
    SkillLibrary,
    describe_missing,
    missing_skills,
)

__all__ = [
    "DEFAULT_CATCHUP_S",
    "DEFAULT_STATE_PATH",
    "HEARTBEAT_EVERY_S",
    "STALE_TICK_AFTER_S",
    "TRIGGER_MANUAL",
    "TRIGGER_SCHEDULE",
    "FireReport",
    "RunGuard",
    "RunRegistry",
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

#: How often a scheduler stamps the heartbeat while it is heads-down in a run. The
#: daemon's own cadence, because that is the promise being kept: the file is touched at
#: least this often for as long as a scheduler is alive, whatever it is busy with.
HEARTBEAT_EVERY_S = MAX_SLEEP_S

#: How long a state file may go untouched before "a scheduler is up" stops being a safe
#: reading of it. Not a new number: a living scheduler stamps the file at least every
#: :data:`HEARTBEAT_EVERY_S`, and a fire is still honest work up to ``catchup_s`` late, so
#: a heartbeat older than the two together could not have fired anything on time anyway.
STALE_TICK_AFTER_S = HEARTBEAT_EVERY_S + DEFAULT_CATCHUP_S

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
        """Turn what a finished session wrote into requests and handoffs."""
        ...

    def dispatch(self, now: datetime) -> object:
        """Sweep deadlines and let board-enabled residents claim work."""
        ...


class RunRegistry(Protocol):
    """Where steward writes down that a session is open, and that it closed.

    A structural protocol for the third time, and for a duller reason than its
    neighbours: :class:`steward.store.Store` imports this module, so this module cannot
    import it back. What satisfies it is that ``Store``.

    The registry exists because the watchdog needs to know a run started without having
    to find the *event* that said so. Events go to burrow, and the copy steward keeps of
    them is a fallback log — written on the host that fired the run, and only useful for
    the runs burrow could not be told about at all. A run steward opened is steward's own
    fact, so steward records it (steward #39). A scheduler with no registry brackets its
    runs exactly as it did before, and the watchdog falls back to reading the log.
    """

    def open_run(  # noqa: PLR0913 — one parameter per fact about the session that opened
        self,
        *,
        run_id: str,
        kind: str,
        agent_id: str,
        trigger: str = "",
        project: str = "",
        ref: str = "",
        timeout_s: float = 0.0,
        event_log_path: str = "",
        owner_token: str = "",
        resident_id: str = "",
        session_credential: str = "",
        now: str | None = None,
    ) -> bool:
        """Record that a session has started, and return whether this call opened it."""
        ...

    def close_run(self, run_id: str, *, now: str | None = None) -> bool:
        """Record that a session reported back, and return whether this call closed it."""
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

    A live scheduler writes this from two threads — its own loop, and the heartbeat that
    keeps stamping while the loop is inside a 15-minute run — so the object serialises its
    own writes. Without that, a heartbeat saving while the loop anchors a routine iterates
    a dict that is being mutated underneath it.
    """

    path: Path
    anchors: dict[str, str] = field(default_factory=dict)
    #: When a scheduler last woke up against this file, in UTC. ``None`` is a real answer:
    #: nothing has ever ticked here, so nothing has ever fired from it.
    last_tick: str | None = None
    #: Serialises this state's two writers: the scheduler's loop and its heartbeat.
    _lock: threading.Lock = field(
        default_factory=threading.Lock, repr=False, compare=False, init=False
    )

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
        fresh = type(self).load(self.path).anchors
        with self._lock:
            self.anchors.update(fresh)

    def anchor(self, key: str) -> datetime | None:
        """Return the moment this routine's next occurrence is computed from, if known."""
        return _moment(self.anchors.get(key))

    def set_anchor(self, key: str, moment: datetime) -> None:
        """Record a new anchor, in UTC."""
        with self._lock:
            self.anchors[key] = moment.astimezone(UTC).isoformat()

    def last_tick_at(self) -> datetime | None:
        """Return when a scheduler last woke up here, or ``None`` if none ever has."""
        return _moment(self.last_tick)

    def record_tick(self, moment: datetime) -> None:
        """Record that a scheduler woke up now, in UTC. Persisted by the next ``save()``."""
        with self._lock:
            self.last_tick = moment.astimezone(UTC).isoformat()

    def save(self) -> None:
        """Write everything this process believes, atomically.

        The caller owns the anchors it is writing: :meth:`Scheduler.tick` saves inside the
        cross-process lock, having reloaded first, so the snapshot it persists is the
        newest one anybody has. A writer that cannot say that must use :meth:`stamp`.

        The in-process lock is held across the write, not just the snapshot: the loop and
        the heartbeat are two threads over one file, and two of their writes interleaving
        would rename half a file over the state — the corruption the rename exists to avoid.
        """
        with self._lock:
            self._write(self.anchors, self.last_tick)

    def stamp(self, moment: datetime) -> None:
        """Persist the heartbeat alone, leaving the anchors on disk exactly as they are.

        The heartbeat asserts one fact — a scheduler process is alive — and it is the only
        writer that fires on a timer rather than at a point the loop chose. It must
        therefore write *only* that fact. A heartbeat that saved this process's whole
        in-memory snapshot would, in the overlapping-cron deployment, rewrite anchors a
        second tick had just saved under the lock: the occurrence that tick anchored would
        look unfired to the next one and run twice, which is the exactly-once promise of
        steward #76 broken by the thing that was supposed to report on it.

        So the anchors are read back from disk and written straight through. Anything this
        process has anchored but not yet saved is not lost — the loop's own :meth:`save`
        follows within the same lock, and it is the loop's to persist, not the heartbeat's.
        """
        with self._lock:
            self.last_tick = moment.astimezone(UTC).isoformat()
            self._write(type(self).load(self.path).anchors, self.last_tick)

    def _write(self, anchors: dict[str, str], last_tick: str | None) -> None:
        """Write one snapshot atomically. Callers hold ``_lock``.

        The temp file is named for the writing process. Two schedulers over one state file
        are a documented deployment (external cron, overlapping ticks), and a shared temp
        name means one can rename the other's half-written file over the state, or delete
        it from under a rename that is already in flight.
        """
        payload: dict[str, Any] = {
            "version": STATE_VERSION,
            "routines": {key: {"anchor": value} for key, value in sorted(anchors.items())},
            "last_tick": last_tick,
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = temp_state_path(self.path)
        temporary.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        temporary.replace(self.path)


def temp_state_path(path: Path, pid: int | None = None) -> Path:
    """Return the temp file a given process writes ``path`` through.

    Named for the writer, so no two processes ever share one: see :meth:`SchedulerState._write`.
    """
    return path.with_suffix(f"{path.suffix}.{os.getpid() if pid is None else pid}.tmp")


def scheduler_liveness(state: SchedulerState, now: datetime | None = None) -> dict[str, Any]:
    """Say whether anything is still ticking this state file. The API and doctor share it.

    Three answers, and the third is the one worth having a shape for: ``alive`` is ``True``
    when the heartbeat is fresher than :data:`STALE_TICK_AFTER_S`, ``False`` when it is
    older — a daemon that died is not the same as one that never existed — and ``None``
    when nothing has ever ticked, which is what a fresh install honestly reports.

    The threshold only holds because the stamp is not the loop coming round: a scheduler
    keeps stamping every :data:`HEARTBEAT_EVERY_S` for as long as it is alive, including
    the fifteen minutes it spends inside one long run (see :meth:`Scheduler._beating`).
    Otherwise every daily summary would report the daemon that is running it as dead.
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


def _lock_holder(path: Path) -> str:
    """Name the PID recorded in a daemon lock file, for the refusal — or say nothing.

    Best effort by design: an unreadable or half-written lock file must still produce a
    clean refusal, just a less helpful one.
    """
    with contextlib.suppress(OSError, ValueError):
        return f" (pid {int(path.read_text(encoding='utf-8').strip())})"
    return ""


# --------------------------------------------------------------------------------------
# the scheduler
# --------------------------------------------------------------------------------------


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
        registry: RunRegistry | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        """Assemble a scheduler over an explicit list of routines."""
        self.scheduled = list(scheduled)
        self.dry_run = dry_run
        # No guard means no budget: unbounded, exactly as it was before steward #8.
        self.guard = guard
        # No registry means the watchdog is back to reading the fallback log for runs
        # that vanished — which is what it read before steward #39, and no worse.
        self.registry = registry
        self.clock = clock or (lambda: datetime.now(UTC))
        self.run_transitions = (
            RunTransitions(cast("RunStore", registry)) if registry is not None else None
        )
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
            (lambda spec, placement: build_runner(spec, placement, force_mock=True))
            if dry_run
            else runner_factory
        )
        self.sessions = ResidentSessions(
            workdir=self.workdir,
            runner_factory=self._runner_factory,
            library=self.library,
            guard=guard,
            hooks=hooks,
            clock=self.clock,
        )
        self._running: set[str] = set()
        self._lock = threading.Lock()
        # The in-process half of the cross-process state lock: which threads of this
        # process are inside it, and the fd carrying the flock while any of them are.
        self._state_gate = threading.Lock()
        self._state_holders = 0
        self._state_fd: int | None = None
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
                check_runner(item.resident.manifest.runner, placement_for(item.resident.manifest)),
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
        must not run. A stray temp from a write killed mid-flight is cleared, so a crash
        does not leave a booby-trap behind.
        """
        if self.dry_run:
            return
        path = self.state.path
        # Clear stray temps from writes killed mid-flight first, so a crash — including the
        # one a directory-shaped state itself causes — does not leave a booby-trap behind.
        # Only ones no live writer can own: this process's own, and the shared name steward
        # wrote through before temps were named per process. Deleting another scheduler's
        # temp would break the rename it is in the middle of, and swallow the anchor with it.
        strays = (temp_state_path(path), path.with_suffix(f"{path.suffix}.tmp"))
        for temporary in strays:
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
    def _state_lock(self, *, wait: bool = True) -> Iterator[bool]:
        """Hold an exclusive OS lock over one wake-up, so two of them never both fire.

        The documented ``scheduler tick`` deployment runs under external cron, so two ticks
        really can overlap — a slow run, a cron that fires while the last one is still
        going; and a cron tick can overlap a running daemon. Without a cross-process lock
        both see the same occurrence due and both fire it (steward #76, #25). ``flock`` on a
        sidecar ``.lock`` file serialises them: the second waiter blocks until the first has
        anchored, saved, and fired, then reloads the state the first one wrote and finds
        nothing due.

        Held around the *decision*, never around the work: the daemon releases it once the
        fires are submitted (:meth:`run`), because holding it across a 15-minute session
        would serialise every runner in the fleet across processes.

        Every write to the state file goes through here, the heartbeat's included: a write
        made outside the lock is a write that can land on top of what another process just
        saved. That is why the lock is re-entrant *per process* rather than per thread — the
        heartbeat stamps from its own thread while the loop thread is inside the lock, and
        the flock this process already holds is exactly the permission it needs. Yields
        whether the lock is held: with ``wait=False`` a lock another process owns is reported
        rather than queued behind, which is how the heartbeat declines to block — a stamp it
        cannot make right now is one the process holding the lock is making instead.

        ``fcntl.flock`` is advisory and unreliable over NFS. That is fine for the repo-local
        default ``.steward/state/`` and bites only an operator who points ``STEWARD_STATE``
        at a network mount, where two schedulers can still both believe they hold it.
        """
        if self.dry_run:
            # A rehearsal takes nothing and leaves no sidecar behind, like ``_save_state``.
            # Nothing writes under it either, so "held" is the honest answer to give a
            # caller that asked whether it may write — there is simply nothing to contend.
            yield True
            return
        acquired = self._acquire_state_lock(wait=wait)
        try:
            yield acquired
        finally:
            if acquired:
                self._release_state_lock()

    def _acquire_state_lock(self, *, wait: bool) -> bool:
        """Take the flock, or note that this process already has it. See :meth:`_state_lock`.

        The in-process gate is taken with the caller's own patience, not unconditionally.
        It is held across the blocking ``flock``, so the one thing that ever holds it for
        any length of time is another thread of this process waiting its turn behind
        another process — which, since the daemon takes the state lock once per wake-up
        (:meth:`run`), can be a whole cron fire long. A heartbeat that queued on the gate
        would go quiet for exactly that stretch, which is the stale heartbeat this all
        exists to prevent. It is in the same position it is in when the flock itself is
        taken — somebody else is about to stamp the file — so it declines the same way.
        """
        if not self._state_gate.acquire(blocking=wait):
            return False
        try:
            if self._state_holders:
                self._state_holders += 1
                return True
            lock_path = self.state.path.with_suffix(f"{self.state.path.suffix}.lock")
            fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o644)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX if wait else fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                os.close(fd)
                if wait:
                    raise  # a tick that cannot lock must not fire, not fire unlocked
                return False
            self._state_fd = fd
            self._state_holders = 1
            return True
        finally:
            self._state_gate.release()

    def _release_state_lock(self) -> None:
        """Drop one hold, releasing the flock when the last one goes."""
        with self._state_gate:
            self._state_holders -= 1
            if self._state_holders or self._state_fd is None:
                return
            fd, self._state_fd = self._state_fd, None
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)

    @contextlib.contextmanager
    def _daemon_lock(self) -> Iterator[None]:
        """Hold a lock for the daemon's whole lifetime, so a second daemon refuses to start.

        The per-wake-up :meth:`_state_lock` keeps two schedulers from firing one occurrence
        twice, but two daemons over one state file are still a mistake worth saying out
        loud: doubled sessions, doubled budgets, doubled board dispatch. A **non-blocking**
        ``LOCK_EX`` means the second ``steward scheduler run`` exits red immediately instead
        of sitting silent until the first one dies, and the PID written into the file lets
        the refusal name who is holding it.

        A *separate* sidecar from ``_state_lock`` on purpose: this one is held from start to
        stop, so sharing one file would make every cron ``tick`` block forever behind a
        running daemon rather than merely take its turn.

        Advisory, and unreliable over NFS — see :meth:`_state_lock`.
        """
        if self.dry_run:
            # A rehearsal is not a daemon; it must never refuse to start beside a real one.
            yield
            return
        lock_path = self.state.path.with_suffix(f"{self.state.path.suffix}.daemon.lock")
        fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o644)
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise SchedulerError(
                    f"another scheduler daemon is already running{_lock_holder(lock_path)} "
                    f"over {self.state.path} — two daemons over one state file double-fire. "
                    "Stop the running one, or point STEWARD_STATE somewhere else."
                ) from exc
            os.ftruncate(fd, 0)
            os.write(fd, f"{os.getpid()}\n".encode())
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

    def fire(
        self,
        item: ScheduledRoutine,
        *,
        trigger: str = TRIGGER_SCHEDULE,
        now: datetime | None = None,
    ) -> FireReport:
        """Run one routine end to end, bracketed by events.

        ``trigger`` is protocol data, not an arbitrary label; reject unknown values before
        opening a run or emitting an event that persistence could not represent.
        """
        validate_kind_trigger("routine", trigger)
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
        admission = self.sessions.admit(item.resident, now=moment, rehearsal=self.dry_run)
        if isinstance(admission, Refusal):
            log.warning("%s: %s", item.key, admission.reason)
            return FireReport(
                scheduled=item,
                run_id=run_id,
                fired=False,
                skipped_reason=admission.reason,
            )

        wake = RoutineWake(item.routine, run_id, trigger)
        if self.dry_run:
            try:
                session = self.sessions.run(admission, wake)
            finally:
                admission.close()
            return FireReport(
                scheduled=item,
                run_id=run_id,
                fired=False,
                prompt=session.prompt,
                skipped_reason="dry run",
            )

        context = ev.RunContext(
            agent_id=item.agent_id,
            project=item.project,
            routine=item.routine.id,
            run_id=run_id,
            cwd=str(admission.workdir),
        )
        # The deadline the session actually gets, read once: the registry is judged
        # against it, and the runner is given it.
        try:
            timeout_s = admission.timeout_for(item.routine.timeout_s)
        except Exception as exc:  # noqa: BLE001 - an unreadable budget refuses safely
            reason = f"budget unreadable: {type(exc).__name__}: {exc}"
            log.warning("%s: could not resolve the run timeout: %s", item.key, exc)
            admission.close()
            return FireReport(
                scheduled=item,
                run_id=run_id,
                fired=False,
                skipped_reason=reason,
            )
        owner_token = new_owner_token()
        # Minted beside the owner token and used in the opposite direction: that fences
        # steward's own writes about this run, this is what the run itself may present at
        # the API (steward #41). A rehearsal returned above, so nothing mints for one.
        credential = new_session_credential()
        # The event first, then the row. A crash between the two leaves a run steward
        # cannot find, which is where this stood before the registry existed; the other
        # order would leave a row the watchdog buries as ``routine_failed`` for a run the
        # village never saw start, which is a death it would have to invent a life for.
        self.emitter.emit(context.started(trigger))
        watched = self._open_run(item, run_id, timeout_s, trigger, moment, owner_token, credential)
        # Only a registered run carries the digest the credential is checked against, so a
        # run the registry did not take is told nothing rather than handed a dud.
        if watched:
            wake = replace(wake, session_credential=credential)

        ownership = (
            self.run_transitions.owned(run_id, owner_token)
            if watched and self.run_transitions is not None
            else contextlib.nullcontext()
        )
        try:
            with ownership:
                session = self.sessions.run(admission, wake)
                result = session.require_result()
                # Win the durable terminal transition before publishing it. If the watchdog
                # already won, a late success must not contradict its failure event.
                terminal = (
                    context.finished(
                        outcome=str(result.outcome),
                        artifacts=result.artifacts,
                        duration_s=result.duration_s,
                    )
                    if result.ok
                    else context.failed(
                        error=f"{result.outcome}: {result.summary()}",
                        duration_s=result.duration_s,
                    )
                )
                if not watched:
                    self.emitter.emit(terminal)
                elif self.run_transitions is not None:
                    self.run_transitions.session_claim(
                        run_id,
                        terminal,
                        owner_token=owner_token,
                        now=session.completed_at or moment,
                    )
                    self.run_transitions.publish_pending(
                        self.emitter, now=session.completed_at or moment
                    )
        finally:
            admission.close()
        return FireReport(
            scheduled=item,
            run_id=run_id,
            fired=True,
            result=result,
            prompt=session.prompt,
            journal_path=session.journal_path,
        )

    def _open_run(  # noqa: PLR0913, PLR0917 - one argument per persisted run fact
        self,
        item: ScheduledRoutine,
        run_id: str,
        timeout_s: int,
        trigger: str,
        moment: datetime,
        owner_token: str = "",
        session_credential: str = "",
    ) -> bool:
        """Write this run into the registry. Never raises: a lost row is not a lost run."""
        if self.registry is None:
            return False
        try:
            opened = self.registry.open_run(
                run_id=run_id,
                kind="routine",
                trigger=trigger,
                agent_id=item.agent_id,
                project=item.project,
                ref=item.routine.id,
                timeout_s=float(timeout_s),
                event_log_path=event_log_path(self.emitter),
                owner_token=owner_token,
                resident_id=item.resident.id,
                session_credential=session_credential,
                now=ev.utc_now_iso(moment),
            )
        except Exception as exc:  # noqa: BLE001 — an unwritable registry is not a failed routine
            log.warning("%s: could not record that this run started: %s", item.key, exc)
            return False
        if not opened:  # pragma: no cover — a fresh id per fire cannot collide
            # An ignored open means the watchdog cannot see this session at all, and a
            # run nobody is watching must not look like a run that is fine.
            log.warning(
                "%s: run %s was already recorded, so it is not being watched", item.key, run_id
            )
        return opened

    # -- liveness ----------------------------------------------------------------------

    @contextlib.contextmanager
    def _beating(self) -> Iterator[None]:
        """Keep the heartbeat stamped for as long as this scheduler is inside its work.

        The heartbeat cannot be the loop coming round, because coming round is exactly what
        a scheduler stops doing while it works: ``run`` joins each wake-up's fires, ``tick``
        fires inside its lock, and both then dispatch the board — sessions that the shipped
        manifests give 600 and 900 seconds. A stamp that only lands between wake-ups is
        therefore missing precisely when a routine is running, and every reader of it
        (``GET /routines``, ``steward doctor``, the console badge) would call a daemon that
        is mid-summary dead — false, and false at the moment somebody is checking on a run
        that looks stuck.

        So a small daemon thread stamps every :data:`HEARTBEAT_EVERY_S` on the wall clock —
        the wall clock, not an injected one, because the fact being asserted is "a scheduler
        process exists right now" and other processes read it against their own now.

        It writes nothing else, and it writes under the same cross-process lock every other
        writer takes. Both halves matter: the anchors are the loop's to own, so the stamp
        goes through :meth:`SchedulerState.stamp`, which leaves the ones on disk alone; and
        a stamp is still a write of the shared file, so it waits its turn — except that it
        does not wait, because a beat it cannot take the lock for is a beat the process
        holding the lock is making anyway. Skipping it is free; queueing on it would park
        the heartbeat behind another process's fifteen-minute run.

        What this asserts is the process, not its progress: a scheduler wedged with its
        heartbeat thread still alive reads as up. That is the right division of labour —
        "is anything attending these routines" is this file's question, and "is what it is
        attending them with actually getting anywhere" is the watchdog's, which is why the
        watchdog lives outside the daemon in the first place.

        A rehearsal does not beat. Nothing is firing on a dry run's account, and a state
        file it must not write cannot be stamped either.
        """
        if self.dry_run:
            yield
            return
        stop = threading.Event()

        def beat() -> None:
            while not stop.wait(HEARTBEAT_EVERY_S):
                try:
                    with self._state_lock(wait=False) as held:
                        if held:
                            self.state.stamp(datetime.now(UTC))
                except Exception as exc:  # noqa: BLE001 — a heartbeat must not kill the run
                    log.warning("could not stamp the scheduler heartbeat: %s", exc)

        thread = threading.Thread(target=beat, name="steward-heartbeat", daemon=True)
        thread.start()
        try:
            yield
        finally:
            stop.set()
            thread.join(timeout=HEARTBEAT_EVERY_S)

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
        # The heartbeat covers the fires and the board sweep, which is where a cron-driven
        # tick spends its minutes: this process really is here for all of them.
        with self._beating():
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

        Two cross-process locks, because the daemon shares its state file with whatever
        else an operator started (steward #25). The daemon-lifetime lock makes a *second*
        daemon refuse to start rather than quietly double every session; the per-wake-up
        state lock makes the daemon take its turn against a cron ``tick``, reloading the
        anchors that tick just wrote and anchoring its own before anything fires. The lock
        is released before ``future.result()`` — waiting on the sessions under it would
        serialise every runner in the fleet across processes.

        The whole loop runs inside :meth:`_beating`, so the heartbeat keeps landing while
        the daemon is heads-down in a fire or a board sweep rather than only between
        wake-ups — which is precisely the stretch the state lock is *not* held for. Both
        stamps are wanted: the loop's says the clock came round, the heartbeat's says the
        process is still here while it works. The heartbeat starts inside the daemon lock,
        so a second daemon that refuses to start never stamps the file it was refused.
        """
        self.require_ready()
        clock = now_fn or (lambda: datetime.now(UTC))
        reports: list[FireReport] = []
        ticks = 0
        with (
            self._daemon_lock(),
            self._beating(),
            ThreadPoolExecutor(max_workers=self.max_workers) as pool,
        ):
            while max_ticks is None or ticks < max_ticks:
                ticks += 1
                moment = clock()
                with self._state_lock():
                    self.state.reload()
                    due = self.due(moment)
                    for item in due:
                        self.state.set_anchor(item.key, moment)
                    # Every iteration, including the ones where nothing is due: an idle
                    # daemon is still a live one, and this stamp is what says the clock
                    # came round. Under the lock and after the reload, like the anchors —
                    # ``_save_state`` writes the whole snapshot, so it has to be the
                    # newest one anybody has.
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
