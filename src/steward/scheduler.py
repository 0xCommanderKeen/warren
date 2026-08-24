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
"""

import json
import logging
import os
import threading
import time
import uuid
from collections.abc import Callable, Iterator, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from croniter import croniter

from steward import events as ev
from steward.manifest import Resident, Routine, validate_path
from steward.manifest import Runner as RunnerSpec
from steward.prompt import assemble_routine_prompt
from steward.runners import Outcome, Runner, RunRequest, RunResult, build_runner, check_runner

__all__ = [
    "DEFAULT_CATCHUP_S",
    "DEFAULT_STATE_PATH",
    "FireReport",
    "ScheduledRoutine",
    "Scheduler",
    "SchedulerError",
    "SchedulerState",
    "latest_fire_at_or_before",
    "next_fire_after",
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

TRIGGER_SCHEDULE = "schedule"

#: How a manifest's runner declaration becomes a runner. Injectable for tests.
type RunnerFactory = Callable[[RunnerSpec], Runner]
STATE_VERSION = 0


class SchedulerError(Exception):
    """Raised when the scheduler refuses to start — loudly, in daylight."""


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
        manifest = self.resident.manifest
        return manifest.agent_id or f"steward:{manifest.id}"

    @property
    def project(self) -> str:
        """The burrow project label: the manifest's project, else the resident id."""
        return self.resident.manifest.project or self.resident.manifest.id

    def workdir(self, fallback: Path) -> Path:
        """Where the session runs.

        The resident's declared memory directory when it exists — the one place its
        charter says it may write — and otherwise the scheduler's working directory.
        Containers land in a later issue; until then this is the most honest location
        the manifest actually gives us.
        """
        memory = self.resident.manifest.memory
        if memory.kind == "directory":
            candidate = Path(memory.path).expanduser()
            if candidate.is_dir():
                return candidate
        return fallback

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

    @property
    def routine_id(self) -> str:
        """Name the routine this report is about."""
        return self.scheduled.routine.id


# --------------------------------------------------------------------------------------
# persisted state
# --------------------------------------------------------------------------------------


@dataclass
class SchedulerState:
    """The minimum steward must remember across restarts: when each routine last ran.

    ``anchor`` is the moment a routine's next occurrence is computed from — the last
    fire, or the moment the routine was first seen. Keeping it is what makes a restart
    truthful in both directions: nothing re-fires, and nothing is silently skipped
    because the process forgot it existed.
    """

    path: Path
    anchors: dict[str, str] = field(default_factory=dict)

    @classmethod
    def load(cls, path: Path | str) -> SchedulerState:
        """Read state from disk. A missing or unreadable file is an empty state."""
        target = Path(path)
        try:
            raw = json.loads(target.read_text(encoding="utf-8"))
        except OSError, ValueError:
            return cls(path=target)
        anchors = raw.get("routines") if isinstance(raw, dict) else None
        if not isinstance(anchors, dict):
            return cls(path=target)
        return cls(
            path=target,
            anchors={
                str(key): str(value.get("anchor"))
                for key, value in anchors.items()
                if isinstance(value, dict) and value.get("anchor")
            },
        )

    def anchor(self, key: str) -> datetime | None:
        """Return the moment this routine's next occurrence is computed from, if known."""
        raw = self.anchors.get(key)
        if not raw:
            return None
        try:
            parsed = datetime.fromisoformat(raw)
        except ValueError:
            return None
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)

    def set_anchor(self, key: str, moment: datetime) -> None:
        """Record a new anchor, in UTC."""
        self.anchors[key] = moment.astimezone(UTC).isoformat()

    def save(self) -> None:
        """Write the state atomically, so a kill mid-write cannot corrupt it."""
        payload = {
            "version": STATE_VERSION,
            "routines": {key: {"anchor": value} for key, value in sorted(self.anchors.items())},
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(f"{self.path.suffix}.tmp")
        temporary.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        temporary.replace(self.path)


def default_state_path(env: dict[str, str] | None = None) -> Path:
    """Return ``$STEWARD_STATE``, or ``.steward/state/scheduler.json`` under the cwd."""
    source = os.environ if env is None else env
    configured = (source.get(STATE_ENV) or "").strip()
    return Path(configured).expanduser() if configured else DEFAULT_STATE_PATH


# --------------------------------------------------------------------------------------
# the scheduler
# --------------------------------------------------------------------------------------


def load_scheduled(residents_dir: Path | str) -> list[ScheduledRoutine]:
    """Collect every enabled routine of every valid resident under a residents tree.

    Invalid manifests never reach the scheduler: :func:`validate_path` returns only
    residents that passed, and the diagnostics are the caller's to report.
    """
    result = validate_path(residents_dir)
    if not result.ok:
        raise SchedulerError(
            "cannot schedule from an invalid residents tree:\n"
            + "\n".join(d.render() for d in result.errors)
        )
    return [
        ScheduledRoutine(resident=resident, routine=routine)
        for resident in result.residents
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
    ) -> None:
        """Assemble a scheduler over an explicit list of routines."""
        self.scheduled = list(scheduled)
        self.dry_run = dry_run
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

    # -- startup validation ----------------------------------------------------------

    def check(self) -> list[str]:
        """Return why the declared runners cannot run. Empty means ready.

        Called before the daemon takes its first breath, so "the ``claude`` binary is
        not on PATH" is a startup error at a reasonable hour rather than a routine
        that silently never happened.
        """
        problems: list[str] = []
        seen: set[str] = set()
        for item in self.scheduled:
            if item.resident.id in seen:
                continue
            seen.add(item.resident.id)
            complaint = check_runner(item.resident.manifest.runner)
            if complaint:
                problems.append(f"{item.resident.path}: {complaint}")
        return problems

    def require_ready(self) -> None:
        """Raise :class:`SchedulerError` unless every declared runner can run."""
        if self.dry_run:
            return
        problems = self.check()
        if problems:
            raise SchedulerError("\n".join(problems))

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

    def build_prompt(self, item: ScheduledRoutine) -> str:
        """Assemble the full prompt for one routine, through the one prompt module."""
        return assemble_routine_prompt(
            item.resident.manifest,
            item.routine.prompt,
            soul_text=item.resident.soul.body,
            journal_entry=self.journal_for(item),
        )

    def journal_for(self, item: ScheduledRoutine) -> str | None:
        """Return the resident's latest journal entry.

        Always ``None`` today: journals land in steward #5, and until a session has
        actually written one there is nothing to inject. Steward never synthesizes an
        entry on a resident's behalf.
        """
        _ = item
        return None

    def fire(self, item: ScheduledRoutine, *, trigger: str = TRIGGER_SCHEDULE) -> FireReport:
        """Run one routine end to end, bracketed by events. Never raises."""
        run_id = str(uuid.uuid4())
        if not self._claim(item.key):
            log.warning(
                "%s: previous run is still going — skipping this fire rather than queueing a lie",
                item.key,
            )
            return FireReport(
                scheduled=item, run_id=run_id, fired=False, skipped_reason="already running"
            )
        try:
            return self._fire_claimed(item, run_id, trigger)
        finally:
            self._release(item.key)

    def _fire_claimed(self, item: ScheduledRoutine, run_id: str, trigger: str) -> FireReport:
        prompt = self.build_prompt(item)
        workdir = item.workdir(self.workdir)
        cwd = str(workdir)

        if self.dry_run:
            return FireReport(
                scheduled=item, run_id=run_id, fired=False, prompt=prompt, skipped_reason="dry run"
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
        try:
            runner: Runner = self._runner_factory(item.resident.manifest.runner)
            result = runner.run(
                RunRequest(
                    prompt=prompt,
                    workdir=workdir,
                    timeout_s=item.routine.timeout_s,
                    model=item.resident.manifest.runner.model,
                    env=self._session_env(item, run_id),
                )
            )
        except Exception as exc:  # noqa: BLE001 — a broken runner is a failed routine, not a crash
            duration = time.monotonic() - started
            result = RunResult(
                outcome=Outcome.FAILED, duration_s=duration, error=f"{type(exc).__name__}: {exc}"
            )

        if result.ok:
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
        return FireReport(scheduled=item, run_id=run_id, fired=True, result=result, prompt=prompt)

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
        """Fire everything due right now, then return. Useful under external cron."""
        moment = now or datetime.now(UTC)
        reports: list[FireReport] = []
        for item in self.due(moment):
            self.state.set_anchor(item.key, moment)
            reports.append(self.fire(item))
        self._save_state()
        return reports

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
                self._save_state()
                futures = [pool.submit(self.fire, item) for item in due]
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
