"""The watchdog: keep unattended residents alive, and never let a lie stand in the log.

Steward #8's other half. A resident nobody is watching can fail in two directions, and
this module answers both:

**It can stop, quietly.** A container exits, the scheduler daemon is killed, a session's
process vanishes with its machine. The watchdog notices, restarts what it can restart,
and says out loud that it did — ``resident_restarted`` with the reason and the attempt
number, under the resident's own ``agent_id``, so the village shows a villager going down
and coming back rather than an unbroken one. A silent restart is a lie by omission.

**It can leave work that never happened looking like work.** A ``routine_started`` with no
closing event is a villager stuck mid-task forever. Past ``timeout + grace`` the watchdog
emits the ``routine_failed`` the dead session could not, with the honest error ``run never
reported back`` — once, ever, guarded by a row in the store.

## The supervisor seam, honestly scoped

Steward does not own containers yet; deployment is steward #4. So this module builds the
seam and one real implementation of each side of it:

:class:`LocalProbe`
    Watches *steward's own* stuck states, which is all steward can truthfully see today:
    a scheduler anchor that stopped advancing while occurrences went by, a lease still
    held past its expiry, a run that started and never reported back. It restarts
    nothing — there is no process it owns — and says so rather than pretending.
:class:`DockerSupervisor`
    ``docker inspect`` for liveness and ``docker restart`` to intervene, against the
    container a manifest names in ``deploy.container``. Fully exercised against a stub
    ``docker`` on PATH. It is wired and ready; real use arrives with #4, and until then a
    resident with no ``deploy.container`` is reported as *unsupervised*, not as healthy.

## Restarts are bounded, and then they stop

1 minute, 5 minutes, 25 minutes, and that is all: three attempts. A resident that is still
down after the third stops being restarted and becomes one ``needs_human`` carrying the
failure summary. Restarting forever would burn a machine to keep a promise nobody made,
and — worse — would fill the village with a heartbeat that means nothing.

``steward watchdog tick`` is one pass; ``steward watchdog run`` is that pass on a loop. A
pass probes every resident, sweeps the deadlines the board and approvals already know how
to sweep, buries unbracketed runs, and checks every resident's budget so a cap trips even
on a day nothing was scheduled.
"""

import json
import logging
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Protocol

from steward import approvals
from steward import events as ev
from steward.approvals import NeedsHuman
from steward.board import Dispatcher, DispatchRun
from steward.budgets import BudgetGuard
from steward.manifest import Resident, validate_path
from steward.runners import CommandRun, run_argv
from steward.scheduler import SchedulerState, default_state_path, next_fire_after
from steward.store import ApprovalRecord, JobRecord, Store

__all__ = [
    "BACKOFF_S",
    "DEFAULT_GRACE_S",
    "DEFAULT_INTERVAL_S",
    "MAX_ATTEMPTS",
    "NEVER_REPORTED_BACK",
    "RESTART_FAILED_ACTION",
    "DockerSupervisor",
    "Health",
    "LocalProbe",
    "ProcessSupervisor",
    "StaleRun",
    "Watchdog",
    "WatchdogPass",
    "scan_unbracketed",
]

log = logging.getLogger("steward.watchdog")

#: How long after a run's own timeout steward waits before calling it dead. Generous on
#: purpose: a session killed at its timeout still has to be reaped, its output read, and
#: its event emitted, and burying a run that was two seconds from reporting would be its
#: own kind of lie.
DEFAULT_GRACE_S = 120.0

#: The bounded restart schedule, in seconds, and the number of attempts it implies.
#: Widely spaced because the failures worth restarting through are the transient ones; a
#: resident that is still down twenty-five minutes later is not having a bad moment.
BACKOFF_S = (60.0, 300.0, 1500.0)
MAX_ATTEMPTS = len(BACKOFF_S)

#: How often ``steward watchdog run`` makes a pass.
DEFAULT_INTERVAL_S = 60.0

#: The error a run that vanished is closed with. Deliberately about steward's knowledge
#: rather than about the session: steward does not know that the run failed, only that it
#: never came back, and the payload says exactly that much.
NEVER_REPORTED_BACK = "run never reported back"

#: The action the crash-loop knock is raised under.
RESTART_FAILED_ACTION = "resident_restart_failed"

_CLOSING_TYPES = frozenset({ev.ROUTINE_FINISHED, ev.ROUTINE_FAILED})


# --------------------------------------------------------------------------------------
# the seam
# --------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Health:
    """What one supervisor can say about one resident right now.

    Three states, not two, and the third is the point. ``known=False`` means *this
    supervisor cannot tell* — no container is declared, ``docker`` is not installed, the
    daemon is unreachable — and the watchdog treats that as a reason to say nothing rather
    than as a reason to restart. Collapsing "I cannot see it" into "it is down" is how a
    watchdog starts bouncing healthy processes.
    """

    resident_id: str
    alive: bool = True
    detail: str = ""
    known: bool = True
    supervisor: str = ""

    @property
    def down(self) -> bool:
        """True only when a supervisor that *can* see this resident says it is not up."""
        return self.known and not self.alive

    def to_dict(self) -> dict[str, Any]:
        """Return the JSON view of one health reading."""
        return {
            "resident": self.resident_id,
            "alive": self.alive,
            "known": self.known,
            "detail": self.detail,
            "supervisor": self.supervisor,
        }


class ProcessSupervisor(Protocol):
    """Something that can see whether a resident is up, and maybe put it back.

    Two methods, because those are the two things an intervention needs and nothing more.
    A supervisor that can only observe (:class:`LocalProbe`) returns ``False`` from
    :meth:`restart` rather than raising: "I noticed, and I cannot fix it" is a complete
    and useful answer, and it is the one that reaches a human.
    """

    kind: str

    def health(self, resident: Resident, now: datetime) -> Health:
        """Report whether this resident is up, or that this supervisor cannot tell."""
        ...

    def restart(self, resident: Resident) -> bool:
        """Try to bring this resident back. Returns whether the attempt succeeded."""
        ...


# --------------------------------------------------------------------------------------
# what steward can see about itself
# --------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class StaleRun:
    """A ``routine_started`` in the local log that no closing event ever answered."""

    run_id: str
    agent_id: str
    project: str
    routine: str
    started_at: datetime

    def age_s(self, now: datetime) -> float:
        """How long this run has been unaccounted for, in seconds."""
        return (now - self.started_at).total_seconds()


def _parse_ts(raw: object) -> datetime | None:
    if not isinstance(raw, str):
        return None
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def scan_unbracketed(
    path: Path,
    *,
    now: datetime,
    timeouts: Mapping[str, float] | None = None,
    grace_s: float = DEFAULT_GRACE_S,
    default_timeout_s: float = 900.0,
) -> list[StaleRun]:
    """Return runs the local event log shows starting and never finishing.

    Read from the fallback JSONL steward already writes when burrow cannot be reached
    (:func:`steward.events.default_fallback_path`), which is the only run history steward
    holds locally. That bounds what this can see, and the bound is worth stating plainly:
    a run whose ``routine_started`` was delivered to burrow and whose ``routine_finished``
    was too never appears here at all, and needs no burying. What lands here is precisely
    the interesting case — a village that was unreachable, or a steward that died with
    events still in its own file.

    ``timeouts`` maps ``<agent_id>/<routine>`` to that routine's declared timeout, so each
    run is judged against its own deadline plus the grace window. A run steward has no
    manifest for gets ``default_timeout_s``, because "I do not know your timeout" must not
    mean "you may hang forever".

    A malformed line is skipped, not raised on: this file is append-only from several
    processes, and a half-written last line is an ordinary thing to find.
    """
    started: dict[str, StaleRun] = {}
    closed: set[str] = set()
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        try:
            event = json.loads(stripped)
        except ValueError:
            continue
        if not isinstance(event, Mapping):
            continue
        payload = event.get("payload")
        payload = payload if isinstance(payload, Mapping) else {}
        run_id = payload.get("run_id")
        if not isinstance(run_id, str) or not run_id:
            continue
        kind = event.get("type")
        if kind in _CLOSING_TYPES:
            closed.add(run_id)
        elif kind == ev.ROUTINE_STARTED:
            moment = _parse_ts(event.get("ts"))
            if moment is None:
                continue
            started[run_id] = StaleRun(
                run_id=run_id,
                agent_id=str(event.get("agent_id") or ""),
                project=str(event.get("project") or ""),
                routine=str(payload.get("routine") or ""),
                started_at=moment,
            )

    deadlines = dict(timeouts or {})
    return [
        run
        for run_id, run in started.items()
        if run_id not in closed
        and run.age_s(now)
        > deadlines.get(f"{run.agent_id}/{run.routine}", default_timeout_s) + grace_s
    ]


@dataclass
class LocalProbe:
    """Detects steward's own stuck states. Sees a lot; can restart nothing.

    Three signals, and each one is a specific thing that has actually gone wrong rather
    than a heuristic about health:

    - **A scheduler anchor that stopped advancing.** Every tick re-anchors the routines it
      visits, even the ones it decides not to back-fill, so an anchor with occurrences
      behind it means nothing has visited that routine — the daemon is not running.
    - **A lease held past its expiry.** The board sweeps these on every dispatch, so one
      that is still standing means the sweep is not happening.
    - **A run that started and never reported back**, past its own timeout plus grace.

    :meth:`restart` always returns ``False``, and honestly: the thing that is stuck is the
    steward process itself or something below it, and a process cannot restart itself into
    health. The watchdog turns that into a knock at the door, which is the correct
    intervention for "the daemon that was supposed to be doing this is gone".
    """

    store: Store
    state: SchedulerState = field(default_factory=lambda: SchedulerState.load(default_state_path()))
    fallback: Path = field(default_factory=ev.default_fallback_path)
    grace_s: float = DEFAULT_GRACE_S
    kind: str = "local"

    def health(self, resident: Resident, now: datetime) -> Health:
        """Report what is stuck about this resident, from steward's own state on disk.

        Finding nothing comes back as ``known=False``, not as ``alive=True``, and the
        distinction is the honest one: this probe can detect *stuckness*, and stuckness is
        the only thing it can detect. "None of steward's own state looks wrong" is a very
        different sentence from "the resident is running", and only a supervisor that can
        see the process — :class:`DockerSupervisor`, once steward #4 lands — gets to say
        the second one.
        """
        complaints = [
            *self._stale_anchors(resident, now),
            *self._dead_leases(resident, now),
            *self._stale_runs(resident, now),
        ]
        if not complaints:
            return Health(
                resident_id=resident.id,
                known=False,
                detail="steward's own state shows nothing stuck, which is not the same as up",
                supervisor=self.kind,
            )
        return Health(
            resident_id=resident.id,
            alive=False,
            detail="; ".join(complaints),
            supervisor=self.kind,
        )

    def restart(self, resident: Resident) -> bool:  # noqa: ARG002 — the honest answer is no
        """Return ``False``: this probe observes steward, it does not own a process."""
        return False

    def _stale_anchors(self, resident: Resident, now: datetime) -> list[str]:
        cutoff = now - timedelta(seconds=self.grace_s)
        complaints: list[str] = []
        for routine in resident.manifest.routines:
            if not routine.enabled:
                continue
            anchor = self.state.anchor(f"{resident.id}/{routine.id}")
            if anchor is None:
                continue  # Never seen by a scheduler; that is not the same as stuck.
            due = next_fire_after(routine, anchor)
            if due < cutoff:
                complaints.append(
                    f"routine {routine.id!r} was due at {due.isoformat()} and the scheduler "
                    f"has not visited it since"
                )
        return complaints

    def _dead_leases(self, resident: Resident, now: datetime) -> list[str]:
        moment = ev.utc_now_iso(now)
        held = [
            job
            for job in self.store.jobs("claimed")
            if job.claimant == resident.agent_id
            and job.lease_expires_at is not None
            and job.lease_expires_at <= moment
        ]
        return [
            f"task {job.task_id} is held past its lease ({job.lease_expires_at})" for job in held
        ]

    def _stale_runs(self, resident: Resident, now: datetime) -> list[str]:
        # A run steward has already buried is not still stuck: the fallback log is
        # append-only, so its ``routine_started`` line stays there forever, and reading it
        # as a fresh outage would leave a resident down for good over one old line.
        buried = self.store.closed_unbracketed_runs()
        return [
            f"run {run.run_id} of {run.routine!r} started {run.age_s(now):.0f}s ago and never "
            f"reported back"
            for run in scan_unbracketed(
                self.fallback, now=now, timeouts=timeouts_for([resident]), grace_s=self.grace_s
            )
            if run.agent_id == resident.agent_id and run.run_id not in buried
        ]


def timeouts_for(residents: Iterable[Resident]) -> dict[str, float]:
    """Map ``<agent_id>/<routine>`` to that routine's declared timeout, for the scan."""
    return {
        f"{resident.agent_id}/{routine.id}": float(routine.timeout_s)
        for resident in residents
        for routine in resident.manifest.routines
    }


# --------------------------------------------------------------------------------------
# containers
# --------------------------------------------------------------------------------------


@dataclass
class DockerSupervisor:
    """Liveness and restarts against the ``docker`` CLI. Ready for steward #4.

    ``docker inspect --format {{.State.Running}} <name>`` answers liveness in one word;
    ``docker restart <name>`` is the intervention. Both go through
    :func:`steward.runners.run_argv`, because steward starts processes in exactly one file
    and that rule is worth keeping for the processes that are not brains either.

    A resident whose manifest names no ``deploy.container`` gets ``known=False``, and so
    does a ``docker`` that is missing or refuses to answer. Neither is reported as healthy
    and neither triggers a restart: this supervisor speaks only about what it can see.
    """

    binary: str = "docker"
    command: CommandRun = run_argv
    kind: str = "docker"

    def container(self, resident: Resident) -> str | None:
        """Return the container this resident's manifest names, or ``None``."""
        return resident.manifest.deploy.container

    def health(self, resident: Resident, now: datetime) -> Health:  # noqa: ARG002 — docker knows
        """Ask docker whether this resident's container is running."""
        name = self.container(resident)
        if not name:
            return Health(
                resident_id=resident.id,
                known=False,
                detail="no deploy.container declared, so no container supervises this resident",
                supervisor=self.kind,
            )
        outcome = self.command([self.binary, "inspect", "--format", "{{.State.Running}}", name])
        if not outcome.ok:
            return Health(
                resident_id=resident.id,
                known=False,
                detail=f"docker could not answer for {name!r}: {outcome.summary()}",
                supervisor=self.kind,
            )
        running = outcome.stdout.strip().lower() == "true"
        return Health(
            resident_id=resident.id,
            alive=running,
            detail=f"container {name} is {'running' if running else 'not running'}",
            supervisor=self.kind,
        )

    def restart(self, resident: Resident) -> bool:
        """Restart this resident's container. Returns whether docker said it worked."""
        name = self.container(resident)
        if not name:
            return False
        outcome = self.command([self.binary, "restart", name])
        if not outcome.ok:
            log.warning("%s: docker restart %s failed: %s", resident.id, name, outcome.summary())
        return outcome.ok


# --------------------------------------------------------------------------------------
# what one pass came to
# --------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class WatchdogPass:
    """Everything one pass observed and did. Every field is something that happened."""

    at: datetime
    health: tuple[Health, ...] = ()
    restarted: tuple[Health, ...] = ()
    gave_up: tuple[Health, ...] = ()
    buried: tuple[StaleRun, ...] = ()
    reopened: tuple[JobRecord, ...] = ()
    expired_approvals: tuple[ApprovalRecord, ...] = ()
    paused: tuple[str, ...] = ()

    @property
    def interventions(self) -> int:
        """How many times this pass changed something rather than only looking."""
        return (
            len(self.restarted)
            + len(self.gave_up)
            + len(self.buried)
            + len(self.reopened)
            + len(self.expired_approvals)
            + len(self.paused)
        )

    def __bool__(self) -> bool:
        """Report whether this pass did anything at all."""
        return self.interventions > 0

    def to_dict(self) -> dict[str, Any]:
        """Return the JSON view of one pass."""
        return {
            "at": ev.utc_now_iso(self.at),
            "health": [reading.to_dict() for reading in self.health],
            "restarted": [reading.to_dict() for reading in self.restarted],
            "gave_up": [reading.to_dict() for reading in self.gave_up],
            "buried": [run.run_id for run in self.buried],
            "reopened": [job.task_id for job in self.reopened],
            "expired_approvals": [record.request_id for record in self.expired_approvals],
            "paused": list(self.paused),
            "interventions": self.interventions,
        }


# --------------------------------------------------------------------------------------
# the watchdog
# --------------------------------------------------------------------------------------


class Watchdog:
    """One pass: probe, sweep, bury, and check every budget. Never raises."""

    def __init__(  # noqa: PLR0913 — every collaborator is keyword-only and injectable
        self,
        residents: Sequence[Resident],
        store: Store,
        *,
        emitter: ev.Emitter | None = None,
        supervisors: Sequence[ProcessSupervisor] | None = None,
        guard: BudgetGuard | None = None,
        sweeper: Dispatcher | None = None,
        fallback: Path | None = None,
        state: SchedulerState | None = None,
        grace_s: float = DEFAULT_GRACE_S,
        backoff_s: Sequence[float] = BACKOFF_S,
    ) -> None:
        """Assemble a watchdog over a set of residents and a durable store."""
        self.residents = list(residents)
        self.store = store
        self.emitter: ev.Emitter = emitter if emitter is not None else ev.NullEmitter()
        self.grace_s = grace_s
        self.backoff_s = tuple(backoff_s)
        self.fallback = fallback if fallback is not None else ev.default_fallback_path()
        self.state = state if state is not None else SchedulerState.load(default_state_path())
        self.guard = guard if guard is not None else BudgetGuard(store, self.emitter)
        self.supervisors: tuple[ProcessSupervisor, ...] = (
            tuple(supervisors)
            if supervisors is not None
            else (
                LocalProbe(store=store, state=self.state, fallback=self.fallback, grace_s=grace_s),
                DockerSupervisor(),
            )
        )
        # The board already knows how to reopen a dead lease and deny a stale approval,
        # loudly and with the right events. The watchdog visits those deadlines rather
        # than reimplementing them, because two sweeps with two ideas of "expired" is one
        # more than the truth can survive.
        self.sweeper = (
            sweeper
            if sweeper is not None
            else Dispatcher(
                residents=self.residents, store=store, emitter=self.emitter, sweep_only=True
            )
        )

    @classmethod
    def from_path(  # noqa: PLR0913 — every knob is keyword-only and independently useful
        cls,
        residents_dir: Path | str,
        store: Store,
        *,
        emitter: ev.Emitter | None = None,
        skills_dir: Path | str | None = None,
        supervisors: Sequence[ProcessSupervisor] | None = None,
        state: SchedulerState | None = None,
        grace_s: float = DEFAULT_GRACE_S,
    ) -> Watchdog:
        """Build a watchdog over every valid resident of a residents tree.

        Invalid manifests are left out for the usual reason: a resident steward cannot
        read is a resident steward cannot honestly speak for, and restarting a container
        named in a manifest that does not validate would be acting on a declaration
        nobody has checked.
        """
        sink = emitter if emitter is not None else ev.EventEmitter.from_env()
        return cls(
            residents=list(validate_path(residents_dir, skills_dir).residents),
            store=store,
            emitter=sink,
            supervisors=supervisors,
            state=state,
            grace_s=grace_s,
        )

    # -- probing -----------------------------------------------------------------------

    def probe(self, resident: Resident, now: datetime) -> Health:
        """Ask every supervisor about this resident and return the worst real answer.

        "Worst real answer" is the whole rule: the first supervisor that *can* see the
        resident and says it is down wins, because one supervisor seeing a dead container
        is not cancelled out by another seeing a live scheduler anchor. When nobody can
        see it, the reading says so and no intervention follows.
        """
        readings = []
        for supervisor in self.supervisors:
            try:
                readings.append(supervisor.health(resident, now))
            except Exception as exc:  # noqa: BLE001 — a broken supervisor is not an outage
                log.warning("%s: supervisor %s failed: %s", resident.id, supervisor.kind, exc)
        down = next((reading for reading in readings if reading.down), None)
        if down is not None:
            return down
        alive = next((reading for reading in readings if reading.known), None)
        if alive is not None:
            return alive
        detail = "; ".join(reading.detail for reading in readings if reading.detail)
        return Health(resident_id=resident.id, known=False, detail=detail or "nothing can see it")

    def _supervisor_for(self, health: Health) -> ProcessSupervisor | None:
        """Return the supervisor whose reading this is — the only one that may act on it."""
        return next((s for s in self.supervisors if s.kind == health.supervisor), None)

    def intervene(self, resident: Resident, health: Health, now: datetime) -> str:
        """Restart, wait out a backoff, or give up and knock. Returns what happened.

        The backoff is read from the store rather than from memory, so it survives a
        watchdog that is itself restarted: three attempts means three, not three per
        process.

        Each entry of :data:`BACKOFF_S` is the wait *after* an attempt, including the last
        one. So a resident that keeps dying is restarted immediately, again a minute
        later, again five minutes after that — and the final twenty-five minutes is the
        window in which it may still come back on its own before steward stops trying and
        wakes somebody. Restarting is cheap; waking a person at 2am is not, and the order
        of those two costs is what the schedule encodes.
        """
        record = self.store.watchdog_attempt(resident.id)
        if record.gave_up:
            return "already asked for a human"
        if record.next_attempt_at and ev.utc_now_iso(now) < record.next_attempt_at:
            return f"waiting until {record.next_attempt_at}"
        if record.attempts >= len(self.backoff_s):
            self._give_up(resident, health, now, attempts=record.attempts)
            return "gave up"

        attempt = record.attempts + 1
        supervisor = self._supervisor_for(health)
        restarted = supervisor is not None and supervisor.restart(resident)
        if not restarted:
            # Nothing here can put this resident back — a local probe has no process to
            # own, or docker refused. Counting failed attempts against the same budget
            # would be a slow way of saying "ask a human", so it is said now.
            self._give_up(resident, health, now, attempts=record.attempts)
            return "gave up"

        self.store.record_watchdog_attempt(
            resident.id,
            reason=health.detail,
            next_attempt_at=ev.utc_now_iso(
                now + timedelta(seconds=self.backoff_s[min(attempt, len(self.backoff_s)) - 1])
            ),
            now=ev.utc_now_iso(now),
        )
        log.warning(
            "%s: restarted (attempt %d of %d) — %s",
            resident.id,
            attempt,
            len(self.backoff_s),
            health.detail,
        )
        self.emitter.emit(
            ev.resident_restarted_event(
                agent_id=resident.agent_id,
                project=resident.project,
                reason=health.detail or "resident was not running",
                attempt=attempt,
                supervisor=health.supervisor,
            )
        )
        return f"restarted (attempt {attempt})"

    def _give_up(self, resident: Resident, health: Health, now: datetime, *, attempts: int) -> None:
        """Stop restarting and knock at the door, once, with the failure summary."""
        tried = (
            f"is still down after {attempts} restart attempt(s)"
            if attempts
            else "is down and nothing here can restart it"
        )
        summary = (
            f"{resident.manifest.soul.name} {tried}: "
            f"{health.detail or 'no supervisor could bring it back'}"
        )
        if not self.store.give_up_on(resident.id, reason=summary, now=ev.utc_now_iso(now)):
            return  # Already asked. One knock per crash loop, not one per pass.
        log.error("%s: %s", resident.id, summary)
        approvals.raise_request(
            self.store,
            self.emitter,
            manifest=resident.manifest,
            request=NeedsHuman(
                raw=summary,
                action=RESTART_FAILED_ACTION,
                detail={
                    "resident": resident.id,
                    "reason": health.detail,
                    "supervisor": health.supervisor,
                    "attempts": attempts,
                    "max_attempts": len(self.backoff_s),
                },
                options=("approve", "deny"),
                # Like a budget pause: denying this by default would resolve the one
                # request that can tell steward to try again, and change nothing else.
                expires_in_s=None,
            ),
            message=summary,
            now=now,
        )

    # -- burying runs that vanished ----------------------------------------------------

    def bury_stale_runs(self, now: datetime) -> list[StaleRun]:
        """Close every unbracketed run with the ``routine_failed`` its session never sent.

        The store's primary key on ``run_id`` is what makes this exactly-once: a run that
        vanished is buried on the first pass that notices, and every pass after that reads
        the row and stays silent. The village must never show eternal work, and it must
        never show one death twice either.
        """
        buried: list[StaleRun] = []
        for run in scan_unbracketed(
            self.fallback,
            now=now,
            timeouts=timeouts_for(self.residents),
            grace_s=self.grace_s,
        ):
            if not self.store.close_unbracketed_run(
                run_id=run.run_id,
                agent_id=run.agent_id,
                routine=run.routine,
                started_at=ev.utc_now_iso(run.started_at),
                now=ev.utc_now_iso(now),
            ):
                continue
            log.warning(
                "%s: run %s of %r started %.0fs ago and never reported back — closing it",
                run.agent_id,
                run.run_id,
                run.routine,
                run.age_s(now),
            )
            self.emitter.emit(
                ev.routine_failed_event(
                    agent_id=run.agent_id,
                    project=run.project or run.agent_id,
                    routine=run.routine,
                    run_id=run.run_id,
                    error=NEVER_REPORTED_BACK,
                )
            )
            buried.append(run)
        return buried

    # -- budgets -----------------------------------------------------------------------

    def check_budgets(self, now: datetime) -> list[str]:
        """Pause every resident that has spent its day, and knock once for each.

        Run here as well as at fire time so a cap trips on a resident that has stopped
        firing for any reason — the daemon is down, the day's routines have all run — and
        the fuel gauge in burrow's fleet view is right without waiting for a wake-up that
        may never come.
        """
        paused: list[str] = []
        for resident in self.residents:
            already = self.store.budget_pause(resident.id) is not None
            try:
                refusal = self.guard.allow(resident.manifest, now)
            except Exception as exc:  # noqa: BLE001 — a broken budget is not a broken pass
                log.warning("%s: could not check the budget: %s", resident.id, exc)
                continue
            if refusal is not None and not already:
                paused.append(resident.id)
        return paused

    # -- the two entry points ----------------------------------------------------------

    def tick(self, now: datetime | None = None) -> WatchdogPass:
        """Make one pass: probe, sweep deadlines, bury stale runs, check budgets."""
        moment = now or datetime.now(UTC)
        readings: list[Health] = []
        restarted: list[Health] = []
        gave_up: list[Health] = []

        for resident in self.residents:
            health = self.probe(resident, moment)
            readings.append(health)
            if not health.down:
                self.store.clear_watchdog_attempts(resident.id)
                continue
            outcome = self.intervene(resident, health, moment)
            if outcome.startswith("restarted"):
                restarted.append(health)
            elif outcome == "gave up":
                gave_up.append(health)

        swept = self._sweep(moment)
        report = WatchdogPass(
            at=moment,
            health=tuple(readings),
            restarted=tuple(restarted),
            gave_up=tuple(gave_up),
            buried=tuple(self.bury_stale_runs(moment)),
            reopened=tuple(swept.reopened),
            expired_approvals=tuple(swept.expired_approvals),
            paused=tuple(self.check_budgets(moment)),
        )
        self.store.record_watchdog_pass(
            interventions=report.interventions, now=ev.utc_now_iso(moment)
        )
        return report

    def _sweep(self, moment: datetime) -> DispatchRun:
        """Visit the board's and approvals' deadlines. A broken sweep is not a dead pass."""
        try:
            return self.sweeper.dispatch(moment)
        except Exception as exc:  # noqa: BLE001 — the watchdog outlives its collaborators
            log.warning("watchdog could not sweep the deadlines: %s", exc)
            return DispatchRun()

    def run(
        self,
        *,
        interval_s: float = DEFAULT_INTERVAL_S,
        max_passes: int | None = None,
        sleep: Callable[[float], object] = time.sleep,
        now_fn: Callable[[], datetime] | None = None,
    ) -> list[WatchdogPass]:
        """Pass, sleep, repeat. ``max_passes`` bounds the loop for tests."""
        clock = now_fn or (lambda: datetime.now(UTC))
        passes: list[WatchdogPass] = []
        count = 0
        while max_passes is None or count < max_passes:
            count += 1
            passes.append(self.tick(clock()))
            if max_passes is None or count < max_passes:
                sleep(interval_s)
        return passes
