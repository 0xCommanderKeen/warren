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

import dataclasses
import json
import logging
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Protocol, cast

from steward import events as ev
from steward.approvals import NeedsHuman
from steward.board import Dispatcher, DispatchRun
from steward.budgets import BudgetGuard
from steward.manifest import Resident, active_residents, validate_path
from steward.run_lifecycle import RUN_LEASE_GRACE_S, RunTransitions
from steward.runners import CommandRun, run_argv
from steward.scheduler import SchedulerState, default_state_path, next_fire_after
from steward.store import (
    RUN_ROUTINE,
    ApprovalRecord,
    JobRecord,
    OpenRun,
    Store,
)
from steward.topology import Survey, survey
from steward.transitions.approval import ApprovalTransitions

__all__ = [
    "BACKOFF_S",
    "DEFAULT_GRACE_S",
    "DEFAULT_INTERVAL_S",
    "MAX_ATTEMPTS",
    "NEVER_REPORTED_BACK",
    "RESTART_FAILED_ACTION",
    "DockerSupervisor",
    "EventLogEvidenceError",
    "Health",
    "LocalProbe",
    "ProcessSupervisor",
    "StaleRun",
    "Watchdog",
    "WatchdogPass",
    "answered_runs",
    "scan_unbracketed",
]

log = logging.getLogger("steward.watchdog")

#: How long after a run's own timeout steward waits before calling it dead. Generous on
#: purpose: a session killed at its timeout still has to be reaped, its output read, and
#: its event emitted, and burying a run that was two seconds from reporting would be its
#: own kind of lie.
#:
#: It is the ownership lease, so it is named by the seam that owns it: a run whose heartbeat
#: is this stale is a run whose owner can no longer renew it, which is also when a session
#: credential minted for it stops working (steward #41).
DEFAULT_GRACE_S = RUN_LEASE_GRACE_S

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

#: The events that answer a run's bracket, for either kind of session. Each carries the
#: ``run_id`` of the session it closes: a routine's pair has always named its run, and a
#: task's close names the session that did the work as well as the task (steward #39).
#: A closing event that names no run — the lease sweep's ``task_failed``, or anything
#: written by a steward older than that change — answers nothing, which is the safe way
#: round: an unanswered row costs one quiet burial, a wrongly answered one costs the
#: outage this file exists to find.
_CLOSING_TYPES = frozenset(
    {
        ev.ROUTINE_FINISHED,
        ev.ROUTINE_FAILED,
        ev.TASK_DONE,
        ev.TASK_FAILED,
        ev.TASK_SESSION_FINISHED,
    }
)


class EventLogEvidenceError(RuntimeError):
    """The complete local close evidence cannot be read safely."""


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

    A third method is *optional*, read through :func:`_owns` the way ``state`` already is:
    ``owns(resident) -> bool``, meaning "there is a process here that is mine to put
    back". It is what tells a resident nothing supervises from one every supervisor
    happens to be failing to restart, and :meth:`Watchdog.tick` needs that distinction to
    keep a give-up receipt from becoming permanent (steward #130). A supervisor that does
    not answer is taken to own one, which is the reading that changes nothing.
    """

    kind: str

    def health(self, resident: Resident, now: datetime) -> Health:
        """Report whether this resident is up, or that this supervisor cannot tell."""
        ...

    def restart(self, resident: Resident) -> bool:
        """Try to bring this resident back. Returns whether the attempt succeeded."""
        ...


def _owns(supervisor: ProcessSupervisor, resident: Resident) -> bool:
    """Ask a supervisor whether it owns a process for this resident, kindly.

    Optional capability, read the way ``state`` already is: a supervisor that does not
    implement ``owns`` is taken at its word as owning one. That default is deliberate —
    it is the answer under which nothing about the give-up path changes.
    """
    owns = getattr(supervisor, "owns", None)
    return True if owns is None else bool(owns(resident))


# --------------------------------------------------------------------------------------
# what steward can see about itself
# --------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class StaleRun:
    """A run steward started and that nothing ever answered — past its own deadline."""

    #: The session, not the thing it was doing: one routine fired twice, or one task
    #: claimed twice, is two runs under two ids.
    run_id: str
    agent_id: str
    project: str
    #: What the session was about — the routine it fired, or, for a ``task`` /
    #: ``delegated`` run, the id of the task it claimed (:attr:`steward.store.OpenRun.ref`).
    routine: str
    started_at: datetime
    #: What kind of session this was, from :data:`steward.store.RUN_KINDS`. A run found
    #: only in the event log is always a routine: nothing else emits ``routine_started``.
    kind: str = RUN_ROUTINE
    #: The deadline this run was actually given, when steward wrote it down. ``0`` means
    #: it did not, and the scan falls back to the declared timeout.
    timeout_s: float = 0.0
    heartbeat_at: datetime | None = None
    event_log_path: str = ""
    evidence_version: int = 0
    registered: bool = False

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


def scan_unbracketed(  # noqa: PLR0913 — every knob is keyword-only and independently useful
    path: Path,
    *,
    now: datetime,
    registry: Store | None = None,
    timeouts: Mapping[str, float] | None = None,
    grace_s: float = DEFAULT_GRACE_S,
    default_timeout_s: float = 900.0,
) -> list[StaleRun]:
    """Return runs that started, are past their own deadline, and never reported back.

    Two sources, and the first one is the source of truth. ``registry`` is steward's own
    :class:`steward.store.Store`, which holds a row per session opened where the opening
    event is emitted and closed where the closing one is (steward #39). That row exists
    whatever happened to the events, which is the whole point: a run whose whole bracket
    reached burrow used to be invisible here, so a session that died *after* its
    ``routine_started`` was delivered was never buried at all.

    ``path`` is the fallback JSONL steward writes when burrow cannot be reached, and it
    stays in the scan as a complement rather than as the answer. It still catches the one
    thing the registry cannot: a run opened by a steward that never wrote to this
    database — an older deployment, a different state directory — whose events reached
    nowhere. Where both know a run, the registry wins, because it knows the deadline the
    session was actually given.

    A *closing* event naming the run wins over both, for either kind of session: steward
    that emitted a finish and then died before answering the row did hear back, and burying
    that run would be inventing a death the log itself disproves. It has to name the run
    and not merely the task it was doing, because this log is append-only and a task id
    outlives its attempts — see :class:`_LogScan`. A run the log answers is not returned
    here and is not left open either: :func:`answered_runs` is where its row gets closed.

    ``timeouts`` maps ``<agent_id>/<routine>`` to that routine's declared timeout, so each
    run is judged against its own deadline plus the grace window. A run steward has no
    manifest for gets ``default_timeout_s``, because "I do not know your timeout" must not
    mean "you may hang forever". A registry row that carries its own timeout is judged
    against that instead: it is the deadline the session was actually given.
    """
    registered = _from_registry(registry)
    started = {run.run_id: run for run in registered}
    scans: dict[str, _LogScan] = {}
    unsafe: set[str] = set()
    for run in registered:
        if not run.event_log_path and run.evidence_version == 0:
            log.warning(
                "refusing to bury run %s: no recorded event log path; "
                "migrate or close this legacy row",
                run.run_id,
            )
            unsafe.add(run.run_id)
            continue
        current = run
        if not current.event_log_path:
            current = dataclasses.replace(current, event_log_path=str(path.resolve()))
            started[current.run_id] = current
        try:
            if current.event_log_path not in scans:
                scans[current.event_log_path] = _from_log(
                    Path(current.event_log_path), required=True
                )
        except EventLogEvidenceError as exc:
            log.warning(
                "refusing to bury runs recorded against %s: %s", current.event_log_path, exc
            )
            unsafe.add(current.run_id)
    try:
        logged = _from_log(path, required=False)
    except EventLogEvidenceError as exc:
        # This default path is evidence only for legacy/unregistered runs. Corruption
        # there cannot veto independent registered runs tied to other per-run paths.
        if not registered:
            raise
        log.warning("could not scan legacy/unregistered runs in %s: %s", path, exc)
        logged = _LogScan(started={}, closed_runs={})
    # The registry wins where both know a run; a closing event in the log wins over both,
    # because a run whose finish steward actually emitted did report back, however the
    # row it should have answered ended up.
    for run_id, run in logged.started.items():
        started.setdefault(run_id, run)

    deadlines = dict(timeouts or {})
    return [
        run
        for run in started.values()
        if run.run_id not in unsafe
        and not (scans[run.event_log_path].answers(run) if run.registered else logged.answers(run))
        and run.age_s(now) > _deadline_s(run, deadlines, default_timeout_s) + grace_s
    ]


def answered_runs(_path: Path, registry: Store | None = None) -> list[StaleRun]:
    """Return the open registry rows the fallback log already holds a closing event for.

    The other half of :func:`scan_unbracketed`'s answer, and a separate question because
    it has nothing to do with deadlines: a row whose finish is in the log reported back,
    whether it is a minute or a week old. Steward died between emitting that finish and
    writing the close — or the write threw — and nobody else will ever come back for the
    row: the scan filters it out before the burial path can reach it, so left alone it is
    re-read and re-filtered on every pass for ever, and ``open_runs`` — the watchdog's new
    source of truth — slowly fills with sessions that are long over.
    """
    rows = _from_registry(registry)
    scans: dict[str, _LogScan] = {}
    answered = []
    for run in rows:
        if not run.event_log_path and run.evidence_version == 0:
            log.warning(
                "refusing to close run %s: no recorded event log path; migrate this legacy row",
                run.run_id,
            )
            continue
        current = run
        if not current.event_log_path:
            current = dataclasses.replace(current, event_log_path=str(_path.resolve()))
        try:
            if current.event_log_path not in scans:
                scans[current.event_log_path] = _from_log(
                    Path(current.event_log_path), required=True
                )
            scan = scans[current.event_log_path]
        except EventLogEvidenceError as exc:
            log.warning(
                "refusing to close runs recorded against %s: %s", current.event_log_path, exc
            )
            continue
        if scan.answers(current):
            answered.append(current)
    return answered


@dataclass(frozen=True, slots=True)
class _LogScan:
    """What the fallback log says started, and what it says has already reported back.

    One kind of answer, joined on one name: a run id. Both kinds of session are closed by
    an event that carries the ``run_id`` of the session it closes, so a row is answered
    when and only when the log holds *that session's* finish.

    Joining on the task id instead is the thing this deliberately does not do, and the
    reason is that a task id is not a session. The board's ordinary flow is claim, die,
    expire the lease, re-claim, and every attempt carries the same task id, so one close
    in this append-only log would answer every attempt that ever shares it. Narrowing that
    to closes at or after a row's ``started_at`` is not enough either: a dispatch pass
    expires the dead predecessor's lease and re-claims the task in the same breath, so the
    predecessor's ``task_failed`` is stamped *after* the retry's row opens and answers it.
    The retry then ran unwatched — the exact death the registry exists to catch (#39).
    """

    started: dict[str, StaleRun]
    closed_runs: dict[str, set[str]]

    def answers(self, run: StaleRun) -> bool:
        """Say whether the log holds a closing event for *this session*."""
        expected = (
            {ev.ROUTINE_FINISHED, ev.ROUTINE_FAILED}
            if run.kind == RUN_ROUTINE
            else {ev.TASK_DONE, ev.TASK_FAILED}
        )
        return bool(self.closed_runs.get(run.run_id, set()) & expected)


def _from_log(  # noqa: C901, PLR0912
    path: Path, *, required: bool = False
) -> _LogScan:
    """Return what the fallback log says started, and what it says closed.

    A half-written final line is ignored because appenders can be interrupted. Any
    earlier malformed line refuses the scan: corruption is not evidence of no close.
    """
    scan = _LogScan(started={}, closed_runs={})
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        if required:
            raise EventLogEvidenceError(f"event log does not exist: {path}") from None
        return scan
    except OSError as exc:
        raise EventLogEvidenceError(f"could not read event log {path}: {exc}") from exc
    lines = text.splitlines()
    torn_final = bool(text) and not text.endswith("\n")
    for index, line in enumerate(lines):
        if not line.strip():
            continue
        try:
            decoded = json.loads(line.strip())
        except ValueError:
            if index == len(lines) - 1 and torn_final:
                continue
            raise EventLogEvidenceError(
                f"event log {path} has an unreadable line {index + 1}"
            ) from None
        if not isinstance(decoded, Mapping):
            raise EventLogEvidenceError(f"event log {path} has a non-event line {index + 1}")
        event = decoded
        problems = ev.validate_event(event)
        if event.get("source") != ev.EVENT_SOURCE:
            problems += (f"source must be {ev.EVENT_SOURCE!r}",)
        if event.get("type") not in ev.EVENT_TYPES:
            problems += (f"unknown event type {event.get('type')!r}",)
        if problems:
            raise EventLogEvidenceError(
                f"event log {path} has an invalid event on line {index + 1}: " + "; ".join(problems)
            )
        payload = cast("Mapping[str, Any]", event["payload"])
        kind = event.get("type")
        if kind in _CLOSING_TYPES:
            run_id = payload.get("run_id")
            if run_id is not None:
                subject = "task_id" if kind in {ev.TASK_DONE, ev.TASK_FAILED} else "routine"
                if (
                    not isinstance(run_id, str)
                    or not run_id
                    or not isinstance(payload.get(subject), str)
                    or not payload.get(subject)
                ):
                    raise EventLogEvidenceError(
                        f"event log {path} has invalid {kind} payload on line {index + 1}"
                    )
                scan.closed_runs.setdefault(run_id, set()).add(cast("str", kind))
            continue
        if kind != ev.ROUTINE_STARTED:
            continue
        moment = _parse_ts(event.get("ts"))
        run_id = payload.get("run_id")
        if moment is not None and isinstance(run_id, str) and run_id:
            scan.started[run_id] = StaleRun(
                run_id=run_id,
                agent_id=str(event.get("agent_id") or ""),
                project=str(event.get("project") or ""),
                routine=str(payload.get("routine") or ""),
                started_at=moment,
            )
    return scan


def _add(ids: set[str], value: object) -> None:
    """Note one id an event named, ignoring an event that named nothing readable."""
    if isinstance(value, str) and value:
        ids.add(value)


def _parse_line(line: str) -> Mapping[str, Any] | None:
    """Return one event off the fallback log, or ``None`` for anything unreadable."""
    stripped = line.strip()
    if not stripped:
        return None
    try:
        event = json.loads(stripped)
    except ValueError:
        return None
    return event if isinstance(event, Mapping) else None


def _deadline_s(run: StaleRun, timeouts: Mapping[str, float], default_s: float) -> float:
    """Return the timeout this run is judged against, best knowledge first.

    The registry's own number wins where there is one: it is the timeout the session was
    actually given, budget cap included, where the manifest only says what was declared.
    """
    if run.timeout_s > 0:
        return run.timeout_s
    return timeouts.get(f"{run.agent_id}/{run.routine}", default_s)


def _from_registry(registry: Store | None) -> list[StaleRun]:
    """Return every open registry run, refusing to turn a read failure into silence."""
    if registry is None:
        return []
    try:
        rows: list[OpenRun] = registry.open_runs()
    except Exception as exc:
        raise EventLogEvidenceError(f"could not read the run registry: {exc}") from exc
    runs = []
    for row in rows:
        moment = _parse_ts(row.started_at)
        if moment is None:  # pragma: no cover — only a hand-edited database gets here
            continue
        runs.append(
            StaleRun(
                run_id=row.run_id,
                agent_id=row.agent_id,
                project=row.project,
                routine=row.ref,
                started_at=moment,
                kind=row.kind,
                timeout_s=row.timeout_s,
                heartbeat_at=_parse_ts(row.heartbeat_at),
                event_log_path=row.event_log_path,
                evidence_version=row.evidence_version,
                registered=True,
            )
        )
    return runs


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

    def owns(self, resident: Resident) -> bool:  # noqa: ARG002 — never, for any resident
        """Return ``False``: there is no process here that is this probe's to put back."""
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
                self.fallback,
                now=now,
                registry=self.store,
                timeouts=timeouts_for([resident]),
                grace_s=self.grace_s,
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

    def owns(self, resident: Resident) -> bool:
        """Return whether this resident declares a container for docker to supervise.

        Not "is it running" — that is :meth:`health`. This is the prior question of
        whether there is anything here to run at all, and it is the one that decides
        whether steward giving up on this resident means anything (steward #130).
        """
        return self.container(resident) is not None

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
        #: The seam a give-up knocks through, built once from this watchdog's own store
        #: and emitter, like every other owner of a transitions seam.
        self.approvals = ApprovalTransitions(store=store, emitter=self.emitter)
        self.runs = RunTransitions(store)
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

        Retired residents are left out for a sharper one. ``steward retire`` marks the
        manifest and *then* stops the container, so a watchdog that still probed retired
        residents would find one down and put it straight back up — steward undoing
        steward, on a schedule, forever.
        """
        sink = emitter if emitter is not None else ev.EventEmitter.from_env()
        return cls(
            residents=active_residents(validate_path(residents_dir, skills_dir).residents),
            store=store,
            emitter=sink,
            supervisors=supervisors,
            state=state,
            grace_s=grace_s,
        )

    def topology(self) -> Survey:
        """Report whether this process's docker holds the containers it supervises (#59).

        Asked once, by whoever starts the daemon, rather than on every pass: it shells out
        to ``docker info``, and the answer is a property of where this process is running,
        which does not change under it. A pass keeps its own per-resident
        :meth:`DockerSupervisor.health` probe — this is the prior question of whether
        those probes are being asked of the right machine at all.

        The watchdog does not refuse to start on a bad answer. Two thirds of a pass —
        burying runs that never reported back, sweeping expired leases and approvals,
        checking budgets — need no docker at all, and stopping those because a container
        is on another host would turn one gap into three.
        """
        return survey(self.residents)

    # -- probing -----------------------------------------------------------------------

    def _reload_state(self) -> None:
        """Re-read scheduler state from disk before probing, so a live fleet looks live.

        ``run`` loops ``tick`` forever while the scheduler — a *different* process —
        advances anchors on disk. Loading the state once at construction froze that view,
        so after ``grace_s`` every routine looked un-visited and the :class:`LocalProbe`
        called a healthy fleet stuck, waking a person about nothing. Reloading here keeps
        the probe reading the same anchors the scheduler is actually writing.

        The anchors are refreshed in place so every :class:`LocalProbe` already holding
        this state object — including the one the watchdog wires by default — sees the
        fresh view without being rebuilt.
        """
        for target in (self.state, *(getattr(s, "state", None) for s in self.supervisors)):
            if isinstance(target, SchedulerState):
                target.anchors = SchedulerState.load(target.path).anchors

    def _read_all(self, resident: Resident, now: datetime) -> list[Health]:
        """Ask every supervisor about this resident. A broken one is skipped, not fatal."""
        readings: list[Health] = []
        for supervisor in self.supervisors:
            try:
                readings.append(supervisor.health(resident, now))
            except Exception as exc:  # noqa: BLE001 — a broken supervisor is not an outage
                log.warning("%s: supervisor %s failed: %s", resident.id, supervisor.kind, exc)
        return readings

    def _representative(self, resident: Resident, readings: Sequence[Health]) -> Health:
        """Reduce several readings to the worst real answer, for reporting and give-up.

        "Worst real answer" is the whole rule: the first supervisor that *can* see the
        resident and says it is down wins, because one supervisor seeing a dead container
        is not cancelled out by another seeing a live scheduler anchor. When nobody can
        see it, the reading says so and no intervention follows.
        """
        down = next((reading for reading in readings if reading.down), None)
        if down is not None:
            return down
        alive = next((reading for reading in readings if reading.known), None)
        if alive is not None:
            return alive
        detail = "; ".join(reading.detail for reading in readings if reading.detail)
        return Health(resident_id=resident.id, known=False, detail=detail or "nothing can see it")

    def probe(self, resident: Resident, now: datetime) -> Health:
        """Ask every supervisor about this resident and return the worst real answer."""
        return self._representative(resident, self._read_all(resident, now))

    def _supervisor_for(self, health: Health) -> ProcessSupervisor | None:
        """Return the supervisor whose reading this is — the only one that may act on it."""
        return next((s for s in self.supervisors if s.kind == health.supervisor), None)

    def _supervised(self, resident: Resident) -> bool:
        """Report whether any supervisor owns a process it could put this resident back as.

        The question a give-up receipt implicitly claims an answer to. Giving up means
        having tried, and of the shipped residents only ``life-agent`` declares a
        ``deploy.container``; ``pip`` and ``burrow-builder`` have nothing that could ever
        have tried (steward #130).
        """
        return any(_owns(supervisor, resident) for supervisor in self.supervisors)

    def intervene(
        self, resident: Resident, down: Sequence[Health], now: datetime
    ) -> tuple[str, Health | None]:
        """Restart, wait out a backoff, or give up and knock. Returns what happened.

        The backoff is read from the store rather than from memory, so it survives a
        watchdog that is itself restarted: three attempts means three, not three per
        process.

        ``down`` is every reading that reported this resident down, worst first. *Each* of
        the supervisors behind those readings is given a chance to restart before steward
        gives up, because a :class:`LocalProbe` complaint must not mask a dead container a
        :class:`DockerSupervisor` could actually put back: the first supervisor to succeed
        is the one credited, and only if none can do steward reach for a human.

        Each entry of :data:`BACKOFF_S` is the wait *after* an attempt, including the last
        one. So a resident that keeps dying is restarted immediately, again a minute
        later, again five minutes after that — and the final twenty-five minutes is the
        window in which it may still come back on its own before steward stops trying and
        wakes somebody. Restarting is cheap; waking a person at 2am is not, and the order
        of those two costs is what the schedule encodes.
        """
        worst = down[0]
        record = self.store.watchdog_attempt(resident.id)
        if record.gave_up:
            return "already asked for a human", None
        if record.next_attempt_at and ev.utc_now_iso(now) < record.next_attempt_at:
            return f"waiting until {record.next_attempt_at}", None
        if record.attempts >= len(self.backoff_s):
            self._give_up(resident, worst, now, attempts=record.attempts)
            return "gave up", None

        attempt = record.attempts + 1
        acted = self._restart_any(resident, down)
        if acted is None:
            # Nothing here can put this resident back — a local probe has no process to
            # own, and docker refused or has nothing to restart. Counting failed attempts
            # against the same budget would be a slow way of saying "ask a human", so it is
            # said now.
            self._give_up(resident, worst, now, attempts=record.attempts)
            return "gave up", None

        self.store.record_watchdog_attempt(
            resident.id,
            reason=acted.detail,
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
            acted.detail,
        )
        self.emitter.emit(
            ev.resident_restarted_event(
                agent_id=resident.agent_id,
                project=resident.project,
                reason=acted.detail or "resident was not running",
                attempt=attempt,
                supervisor=acted.supervisor,
            )
        )
        return f"restarted (attempt {attempt})", acted

    def _restart_any(self, resident: Resident, down: Sequence[Health]) -> Health | None:
        """Let each down-reporting supervisor try, and return the reading that succeeded."""
        for reading in down:
            supervisor = self._supervisor_for(reading)
            if supervisor is not None and supervisor.restart(resident):
                return reading
        return None

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
        # A knock rather than a raise: this is steward's news about a resident, not the
        # resident asking for anything, so the repeat-deny guard stays off. ``give_up_on``
        # above already makes this one knock per crash loop, and a resident that fell over
        # again a day after a denied restart is new news rather than a repeat.
        self.approvals.knock(
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

    def bury_stale_runs(self, now: datetime) -> list[StaleRun]:  # noqa: C901
        """Close every unbracketed run with the ``routine_failed`` its session never sent.

        The store's primary key on ``run_id`` is what makes this exactly-once: a run that
        vanished is buried on the first pass that notices, and every pass after that reads
        the row and stays silent. The village must never show eternal work, and it must
        never show one death twice either.

        A *task* that vanished is closed in the registry and announced by nobody here. The
        board already owns that death: its lease sweep — which this same pass runs — puts
        the task back on the board and says ``task_failed`` with ``lease_expired``, and a
        second closing event from the watchdog would be the same task dying twice.

        Every row this pass reaches ends up closed, mourned or not: a run the log has
        already answered is not a death, but leaving its row open would keep it in
        ``open_runs`` for ever, so it is closed quietly instead.
        """
        try:
            self._close_answered(now)
            stale = scan_unbracketed(
                self.fallback,
                now=now,
                registry=self.store,
                timeouts=timeouts_for(self.residents),
                grace_s=self.grace_s,
            )
        except EventLogEvidenceError as exc:
            log.warning("refusing to bury runs without trustworthy close evidence: %s", exc)
            return []
        buried: list[StaleRun] = []
        if hasattr(self.runs, "publish_pending"):
            self.runs.publish_pending(self.emitter, now=now)
        for run in stale:
            # A task death belongs to the lease sweep. If that sweep has not produced a
            # run-specific terminal fact, leave the run pending; a generic routine death
            # would be the wrong protocol and finalizing it without a sink would lose it.
            if run.registered and run.kind != RUN_ROUTINE:
                continue
            terminal = ev.routine_failed_event(
                agent_id=run.agent_id,
                project=run.project or run.agent_id,
                routine=run.routine,
                run_id=run.run_id,
                error=NEVER_REPORTED_BACK,
            )
            if run.registered and hasattr(self.runs, "watchdog_claim"):
                registry_lost = not self.runs.watchdog_claim(
                    run.run_id, terminal, now=now, grace_s=self.grace_s
                )
            elif run.registered:
                registry_lost = not self.runs.watchdog_close(
                    run.run_id, now=now, grace_s=self.grace_s
                )
            else:
                registry_lost = False
            if registry_lost:
                continue
            if not run.registered and not self.store.close_unbracketed_run(
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
            if run.kind == RUN_ROUTINE:
                if run.registered:
                    self.runs.publish_pending(self.emitter, now=now)
                else:
                    self.emitter.emit(terminal)
            buried.append(run)
        return buried

    def _close_answered(self, now: datetime) -> None:
        """Close the rows the fallback log has already answered. Nothing is emitted.

        The finish these sessions reported is in the log, so it either reached burrow or
        will on the next flush; the only thing missing is the row, and this writes it.
        Quietly on purpose — a village event here would announce a run that ended
        normally, hours after it did.
        """
        for run in answered_runs(self.fallback, self.store):
            if self.store.close_run(run.run_id, now=ev.utc_now_iso(now)):
                log.info(
                    "%s: run %s of %r was answered by the event log — closing its row",
                    run.agent_id,
                    run.run_id,
                    run.routine,
                )

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
        self._reload_state()
        readings: list[Health] = []
        restarted: list[Health] = []
        gave_up: list[Health] = []

        for resident in self.residents:
            all_readings = self._read_all(resident, moment)
            health = self._representative(resident, all_readings)
            readings.append(health)
            if health.known and health.alive:
                # Genuinely recovered — and only genuinely recovered — forgets the restart
                # history, including the give-up receipt. An "I cannot tell" reading is not
                # a recovery: clearing on it would reset the budget every flap and wipe the
                # receipt, so a crash loop would never hit the cap and would knock again on
                # every pass. So an unknown reading clears nothing.
                self.store.clear_watchdog_attempts(resident.id)
                continue
            if not health.down:
                # Nobody can see it right now. Say nothing, change nothing — unless
                # nobody ever *could*. A resident no supervisor owns a process for can
                # never produce the ``known and alive`` reading above: ``LocalProbe``
                # reports stuckness or ``known=False`` and never life, and
                # ``DockerSupervisor`` returns ``known=False`` for a manifest that names
                # no container. So a give-up receipt written about such a resident was
                # permanent — ``intervene`` answered "already asked for a human" on every
                # pass afterwards, for ever, with no CLI or endpoint that could undo it
                # (steward #130).
                #
                # Nothing tried to restart it, so there is nothing to keep having given up
                # on. "Steward's own state has stopped complaining" is the only recovery
                # signal such a resident has, so it is the one that clears the receipt.
                # The flap this branch guards against elsewhere cannot happen here: an
                # unowned resident's unknown is structural and constant, not intermittent.
                if not self._supervised(resident):
                    self.store.clear_watchdog_attempts(resident.id)
                continue
            outcome, acted = self.intervene(resident, [r for r in all_readings if r.down], moment)
            if outcome.startswith("restarted") and acted is not None:
                restarted.append(acted)
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
