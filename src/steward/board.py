"""The job board: pull-based dispatch, atomic claims, and leases that expire.

One place work can be dropped for the fleet, instead of prompting a particular resident.
A human posts a task through the API; nobody is told to do it. On its next wake-up, a
resident that has *declared* it takes board work looks at the queue, claims the oldest
open task whose required skills it holds, works it as an ordinary headless session, and
marks it done.

**Opting in is a declaration, not an inference.** ``board: {claim: true}`` in the
manifest, backed by an active ``job-board`` route (:class:`steward.manifest.Board`). A
resident with perfectly matching skills and no board block never claims anything —
silence is not consent, and the point of a manifest is that you can read what a resident
will do before it does it.

**Claiming is one conditional write.** ``UPDATE jobs SET status='claimed' … WHERE
task_id=? AND status='open'``. ``rowcount == 0`` means another resident got there first;
the loser moves to the next open task and emits nothing. Two residents waking in the
same millisecond can never both hold task 7, and burrow can never render two villagers
walking to the same notice.

**A claim is a lease, not a deed.** Default thirty minutes. :meth:`Dispatcher.dispatch`
sweeps expired leases before it claims anything: the task returns to ``open`` and
``task_failed`` is emitted with reason ``lease_expired``. Work that quietly vanished
back onto the board would be the board lying about what happened to it.

**Matching is against the effective skill set, not the granted one.** A task's
``required_skills`` is checked against :func:`claimable_skills` — the library's defaults
plus this manifest's own grants (:mod:`steward.skills`) — so a task tagged ``research`` is
claimable by a resident with no ``skills:`` block at all, because research is something
every resident holds. The claimant is then provisioned exactly as a routine session is:
same library, same prompt injection, same on-disk materialization for the runners that
read skills off disk, same refusal when a grant names nothing.

**A delegated item is a task addressed to one resident.** Steward #7 writes handoffs into
the same table with an ``assignee`` (:mod:`steward.delegation`), and this module works them
with the machinery already here: the same conditional claim, the same lease, the same
provisioned session, the same three closing events — only narrowed to the one resident the
letter names, and drained before the open board, because work addressed to you personally
comes ahead of work addressed to nobody. A resident drains its inbox whether or not it
claims board work — accepting delegated work is declared in ``routes``, not in ``board`` —
and stops draining it the moment that route stops being active.

**The village hears every transition.** ``task_posted`` (the API), then ``task_claimed``,
then exactly one of ``task_done`` / ``task_failed`` — the last three under the
*claimant's* agent id, so burrow walks the right villager. Everything the board shows is
reconstructible from those four events alone.
"""

import logging
from collections.abc import Callable, Mapping, Sequence
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from functools import cached_property
from pathlib import Path
from typing import cast

from steward import approvals as ap
from steward import delegation as dg
from steward import events as ev
from steward.manifest import (
    DEFAULT_BOARD_LEASE_S,
    DEFAULT_BOARD_TIMEOUT_S,
    Resident,
    ResidentManifest,
    active_residents,
    validate_path,
)
from steward.manifest import (
    Runner as RunnerSpec,
)
from steward.runners import (
    Outcome,
    Runner,
    RunRequest,
    RunResult,
    build_runner,
    check_runner,
)
from steward.sessions import (
    Admission,
    DelegatedWake,
    Refusal,
    ResidentSessions,
    RunGuard,
    RunnerFactory,
    SessionHarvest,
    TaskWake,
    workdir_refusal,
)
from steward.skills import (
    SkillLibrary,
    describe_missing,
    effective_names,
    library_for,
    missing_skills,
)
from steward.store import (
    RUN_DELEGATED,
    RUN_TASK,
    STATUS_DONE,
    STATUS_FAILED,
    STATUS_OPEN,
    ApprovalRecord,
    JobRecord,
    Store,
    new_id,
)
from steward.transitions.approval import ApprovalTransitions
from steward.transitions.task import LEASE_EXPIRED, TaskTransitions

__all__ = [
    "LEASE_EXPIRED",
    "BoardReport",
    "DispatchRun",
    "Dispatcher",
    "PlannedClaim",
    "board_preflight",
    "board_residents",
    "claimable_skills",
    "delegation_residents",
    "load_board_residents",
    "load_residents",
]

log = logging.getLogger("steward.board")


def _utcnow() -> datetime:
    """Read the wall clock, in UTC. The default source of each lease's own birth moment."""
    return datetime.now(UTC)


@dataclass(frozen=True, slots=True)
class _RegistryRun:
    """The board registry row paired with the runner currently executing it."""

    run_id: str
    task_id: str
    started_at: datetime


def _close_registry(store: Store, run: _RegistryRun, moment: datetime) -> None:
    """Answer one board-owned run row without interfering with session completion."""
    try:
        store.close_run(run.run_id, now=ev.utc_now_iso(moment))
    except Exception as exc:  # noqa: BLE001 — the registry must not take the board down
        log.warning("could not record that task %s reported back: %s", run.task_id, exc)


class _RegistryClosingRunner(Runner):
    """Close a board run at the existing runner seam, before shared bookkeeping."""

    def __init__(self, runner: Runner, store: Store, registry_run: _RegistryRun) -> None:
        super().__init__()
        self.runner = runner
        self.store = store
        self.registry_run = registry_run

    def run(self, request: RunRequest) -> RunResult:
        """Retain vanished-run semantics while closing every answered runner promptly."""
        try:
            result = self.runner.run(request)
        except Exception:
            _close_registry(self.store, self.registry_run, self.registry_run.started_at)
            raise
        _close_registry(
            self.store,
            self.registry_run,
            self.registry_run.started_at + timedelta(seconds=result.duration_s),
        )
        return result


class _RegistryClosingGuard:
    """Preserve board registry ordering when a session fails before a runner exists."""

    def __init__(
        self,
        guard: RunGuard | None,
        store: Store,
        registry_run: ContextVar[_RegistryRun | None],
    ) -> None:
        self.guard = guard
        self.store = store
        self.registry_run = registry_run

    def allow(self, manifest: ResidentManifest, now: datetime | None = None) -> str | None:
        """Delegate admission policy when the dispatcher has one."""
        return None if self.guard is None else self.guard.allow(manifest, now)

    def timeout_for(self, manifest: ResidentManifest, declared_s: int) -> int:
        """Delegate timeout policy without changing the unguarded default."""
        if self.guard is None:
            return declared_s
        return self.guard.timeout_for(manifest, declared_s)

    def record(  # noqa: PLR0913 - mirrors the established RunGuard contract
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
        """Close any pre-run failure before handing accounting to the real guard."""
        registry_run = self.registry_run.get()
        if registry_run is not None:
            _close_registry(self.store, registry_run, now or registry_run.started_at)
        if self.guard is None:
            return None
        return self.guard.record(
            manifest,
            result=result,
            kind=kind,
            run_id=run_id,
            ref=ref,
            origin=origin,
            now=now,
        )


def claimable_skills(manifest: ResidentManifest, library: SkillLibrary) -> frozenset[str]:
    """Return the skills this resident actually holds, for matching against a task.

    The **effective** set, not the granted one (:func:`steward.skills.effective_names`):
    the library's defaults plus this manifest's own grants. That distinction is the whole
    point of the library — a resident with no ``skills:`` block at all still holds
    ``research`` and ``write-journal``, so a task tagged with a default skill is claimable
    by anybody, exactly as a task tagged with nothing is.

    Matching stays a subset test on ids and nothing cleverer. A task tagged with a skill a
    resident lacks is never claimed by it; an untagged task is claimable by any
    board-enabled resident, because an empty set is a subset of everything.
    """
    return frozenset(effective_names(manifest, library))


def board_residents(residents: Sequence[Resident]) -> list[Resident]:
    """Return the residents whose manifests opt into board work, in declared order.

    A retired resident is not one of them, whatever its ``board`` block still says. The
    filter is applied here as well as at load time because a caller may hand the
    dispatcher an explicit list, and "does this resident take work" must have the same
    answer however the list was built.
    """
    return [resident for resident in active_residents(residents) if resident.manifest.board.claim]


def delegation_residents(residents: Sequence[Resident]) -> list[Resident]:
    """Return the residents with a door open to delegated work, in declared order.

    An *active* route of kind ``delegation`` — the same declaration steward checks before
    delivering anything (:mod:`steward.delegation`). A resident that has closed its door
    since a letter arrived stops taking new ones and the letter waits on the mat, visible
    in ``steward inbox``; steward does not push work through a channel a manifest now says
    is shut — and a resident that has been retired has closed every door it had.
    """
    return [resident for resident in active_residents(residents) if resident.delegation_routes]


def load_board_residents(
    residents_dir: Path | str, skills_dir: Path | str | None = None
) -> list[Resident]:
    """Collect every board-enabled resident under a residents tree.

    Invalid manifests never reach the board, for the same reason they never reach the
    scheduler: a resident steward cannot read is a resident steward will not run — and a
    resident granted a skill the library does not have is one steward cannot read.
    """
    return board_residents(load_residents(residents_dir, skills_dir))


def load_residents(
    residents_dir: Path | str, skills_dir: Path | str | None = None
) -> list[Resident]:
    """Collect every valid resident under a residents tree.

    Wider than :func:`load_board_residents` on purpose. Claiming from the board is one
    reason a dispatch touches a resident; a letter addressed to it is another, and a
    resident that accepts delegated work has said so in its ``routes`` without ever
    opting into the board. Both filters are applied where they belong — at dispatch —
    rather than by loading a narrower fleet than exists.

    Retirement is filtered at dispatch too, and for the same reason plus one more: a lease
    held by a resident that has since been retired still has to be swept, and naming its
    project in the ``task_failed`` needs the manifest this load would otherwise have
    dropped. What a retired resident cannot do is take *new* work, and that is decided by
    :func:`board_residents` and :func:`delegation_residents`.
    """
    return list(validate_path(residents_dir, skills_dir).residents)


# --------------------------------------------------------------------------------------
# before the first claim
# --------------------------------------------------------------------------------------


def board_preflight(
    residents: Sequence[Resident],
    library: SkillLibrary,
    workdir: Path | str | None = None,
) -> list[str]:
    """Return why these claimants could not work a notice. Empty means ready to claim.

    :meth:`steward.scheduler.Scheduler.check` asks the same questions of the same builders
    for the other kind of wake-up, and this exists beside it because that one cannot see
    these residents: it iterates the *scheduled* fleet, so a board-only claimant —
    ``board: {claim: true}`` with ``routines: []`` — is never pre-flighted at all. Its
    missing binary or unresolvable grant is then discovered at claim time, by the resident
    session lifecycle's provision stage, as a task the village watched a villager pick up
    and close failed a second later (steward #37). Asked here it is a complaint at a
    reasonable hour instead.

    All three questions are asked of every claimant, even though a caller that reached here
    through :func:`steward.manifest.validate_path` has already been told about the grant —
    validation resolves the same library and an unresolvable grant is an error there, so
    :command:`steward doctor` never gets this far. A caller that did not validate first (a
    dispatch-time dry run) has not, and half a pre-flight that depends on how you arrived is
    worse than one redundant line.

    The journal is deliberately not asked about: a board session reads one when there is
    one and works the task without complaint when there is not, so an unjournalable memory
    is the scheduler's refusal to make, not this one's.
    """
    fallback = Path(workdir) if workdir is not None else Path.cwd()
    problems: list[str] = []
    for resident in board_residents(residents):
        missing = missing_skills(resident.manifest, library)
        complaints = (
            check_runner(resident.manifest.runner),
            describe_missing(resident.id, missing, library) if missing else None,
            workdir_refusal(resident, fallback, library),
        )
        problems.extend(f"{resident.id}: board — {c}" for c in complaints if c)
    return problems


# --------------------------------------------------------------------------------------
# what a dispatch came to
# --------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class BoardReport:
    """One claimed task, worked to a conclusion. Every field is something that happened."""

    resident_id: str
    claimant: str
    task: JobRecord
    status: str
    result: RunResult | None = None
    reason: str | None = None
    artifacts: tuple[str, ...] = ()
    raised: tuple[ApprovalRecord, ...] = ()
    #: The handoffs this session wrote, delivered or refused (:mod:`steward.delegation`).
    handed_over: tuple[dg.Delivery, ...] = ()

    @property
    def done(self) -> bool:
        """True only when the session finished the task on its own terms."""
        return self.status == STATUS_DONE

    @property
    def delegated(self) -> bool:
        """True when this work was handed to this resident rather than claimed by it."""
        return self.task.delegated


@dataclass(frozen=True, slots=True)
class DispatchRun:
    """Everything one wake-up of the board did, including the housekeeping."""

    reopened: tuple[JobRecord, ...] = ()
    expired_approvals: tuple[ApprovalRecord, ...] = ()
    reports: tuple[BoardReport, ...] = ()
    #: What a ``dry_run`` dispatch *would* have claimed and worked, resident by resident,
    #: without claiming or working any of it (steward #88). Empty on a real dispatch, where
    #: the work is in ``reports`` because it actually happened.
    planned: tuple[PlannedClaim, ...] = ()

    def __bool__(self) -> bool:
        """Report whether this dispatch actually changed anything."""
        return bool(self.reopened or self.expired_approvals or self.reports or self.planned)


@dataclass(frozen=True, slots=True)
class PlannedClaim:
    """One task a ``dry_run`` dispatch would have claimed. A rehearsal, never a write."""

    resident_id: str
    claimant: str
    task: JobRecord
    #: ``delegated`` for a letter waiting in the inbox, ``board`` for an open notice.
    source: str

    def to_dict(self) -> dict[str, object]:
        """Return the JSON view a ``--dry-run`` CLI prints as "would claim"."""
        return {
            "resident": self.resident_id,
            "claimant": self.claimant,
            "task_id": self.task.task_id,
            "title": self.task.title,
            "source": self.source,
        }


# --------------------------------------------------------------------------------------
# the dispatcher
# --------------------------------------------------------------------------------------


@dataclass
class Dispatcher:
    """Claims and works board tasks for a set of residents. Never raises.

    Held by the scheduler (one call per tick) and by ``steward board dispatch`` (one call
    per invocation), so the board is swept on exactly the same rhythm routines fire on.
    A dispatch with nothing to do is cheap and silent.
    """

    residents: Sequence[Resident]
    store: Store
    emitter: ev.Emitter = field(default_factory=ev.NullEmitter)
    workdir: Path = field(default_factory=Path.cwd)
    runner_factory: RunnerFactory = build_runner
    #: The one library the fleet is matched and provisioned against, threaded in the same
    #: way :class:`steward.scheduler.Scheduler` threads it. An unconfigured library means
    #: no defaults, so matching falls back to exactly what each manifest grants.
    library: SkillLibrary = field(default_factory=SkillLibrary)
    #: The budget seam (:class:`steward.sessions.RunGuard`). A paused resident is skipped
    #: *before* it claims anything, not after: a claim it cannot work would hold a real
    #: task hostage for a full lease while the resident sat stopped.
    guard: RunGuard | None = None
    #: Do the housekeeping and stop: reopen dead leases, deny stale approvals, claim
    #: nothing. Deliberately *not* called a dry run, because it writes — the scheduler's
    #: ``--dry-run`` is a rehearsal that changes nothing at all, and one word should not
    #: mean two things.
    sweep_only: bool = False
    #: Resolve and report what *would* be claimed and worked, launching no session and
    #: writing nothing — no claim, no ledger, no events (steward #88). The board's honest
    #: rehearsal, mirroring the scheduler's ``--dry-run``: the first ``dispatch`` a new
    #: operator runs against shipped ``claude`` residents otherwise spends real money.
    #: Distinct from ``sweep_only``, which writes (it reopens dead leases).
    dry_run: bool = False
    #: The clock each lease's length is measured from. Read *per claim*, not once per
    #: dispatch, so the Nth claim of a slow ``max_claims_per_wake`` dispatch is born with a
    #: full lease rather than one already eaten into by the sessions before it (steward #73).
    clock: Callable[[], datetime] = _utcnow
    #: How deep a chain of delegated work may run before steward refuses (steward #7).
    max_delegation_depth: int = field(default_factory=dg.max_depth)
    #: How many delegated items one wake-up drains. A wake-up is a wake-up, not a shift:
    #: a resident that finds five letters answers one and finds four next time.
    max_delegations_per_wake: int = 1
    #: The terms delegated work runs on. Deliberately *not* read from the receiver's
    #: ``board`` block: accepting a letter is not opting into the board, and a resident
    #: that never claims has never been asked what a claim of its should cost. These are
    #: the same honest defaults the board ships with.
    delegation_lease_s: int = DEFAULT_BOARD_LEASE_S
    delegation_timeout_s: int = DEFAULT_BOARD_TIMEOUT_S
    sessions: ResidentSessions = field(init=False, repr=False)
    _registry_run: ContextVar[_RegistryRun | None] = field(
        default_factory=lambda: ContextVar("steward_board_registry_run", default=None),
        init=False,
        repr=False,
    )

    def __post_init__(self) -> None:
        """Build the shared lifecycle from the dispatcher's existing dependencies."""
        self.sessions = ResidentSessions(
            workdir=self.workdir,
            runner_factory=self._registry_runner,
            library=self.library,
            guard=_RegistryClosingGuard(self.guard, self.store, self._registry_run),
            hooks=self,
            residents=self.residents,
        )

    @classmethod
    def from_path(  # noqa: PLR0913 — every knob is keyword-only and independently useful
        cls,
        residents_dir: Path | str,
        store: Store,
        *,
        emitter: ev.Emitter | None = None,
        workdir: Path | None = None,
        runner_factory: RunnerFactory = build_runner,
        library: SkillLibrary | None = None,
        skills_dir: Path | str | None = None,
        guard: RunGuard | None = None,
        sweep_only: bool = False,
        dry_run: bool = False,
        max_delegation_depth: int | None = None,
    ) -> Dispatcher:
        """Build a dispatcher over the valid residents of a residents tree.

        The whole tree, not only the board-enabled part: claiming is one reason a resident
        is woken here and a delegated letter is another, and the second is declared in
        ``routes``, not in ``board``. Which residents claim is still decided by
        :func:`board_residents`, at dispatch.

        The library is resolved the same way every other entry point resolves it — an
        explicit one wins, otherwise the library beside the tree — so the board matches
        and provisions against the same skills the scheduler does.
        """
        resolved = library if library is not None else library_for(residents_dir, skills_dir)
        return cls(
            residents=load_residents(residents_dir, skills_dir),
            store=store,
            emitter=emitter or ev.EventEmitter.from_env(),
            workdir=workdir if workdir is not None else Path.cwd(),
            runner_factory=runner_factory,
            library=resolved,
            guard=guard,
            sweep_only=sweep_only,
            dry_run=dry_run,
            max_delegation_depth=(
                max_delegation_depth if max_delegation_depth is not None else dg.max_depth()
            ),
        )

    @cached_property
    def tasks(self) -> TaskTransitions:
        """The seam every board write crosses, paired with the fact that announces it.

        Built once, from this dispatcher's own store and emitter, so a dispatcher that was
        handed a mock emitter announces into that mock and nowhere else. Cached rather
        than rebuilt per access because a dataclass's fields are settled by the time the
        first access happens and nothing in steward swaps a dispatcher's emitter after
        construction — tests inject at construction too.
        """
        return TaskTransitions(store=self.store, emitter=self.emitter, project_of=self._project_of)

    @cached_property
    def approvals(self) -> ApprovalTransitions:
        """The seam a session's escalations and the deadline sweep both cross."""
        return ApprovalTransitions(store=self.store, emitter=self.emitter)

    @cached_property
    def delegator(self) -> dg.Delegator:
        """The arbiter this dispatch validates handoffs through.

        Built from the same residents, store, and emitter, so a handoff harvested out of a
        session is judged against exactly the fleet this dispatcher can see.
        """
        return dg.Delegator(
            residents=self.residents,
            store=self.store,
            emitter=self.emitter,
            max_depth=self.max_delegation_depth,
        )

    # -- housekeeping ----------------------------------------------------------------

    def expire_leases(self, now: datetime) -> list[JobRecord]:
        """Return every task whose lease ran out to the board, loudly.

        The reopened row is announced as ``task_failed`` with reason ``lease_expired``,
        under the agent id of the resident that dropped it. A task silently returning to
        ``open`` would let the village show unattended work as work in progress.

        The seam returns a transition per lease swept; a dispatch reports the rows.
        """
        return [swept.require() for swept in self.tasks.expire_leases(now=now)]

    def _project_of(self, claimant: str) -> str:
        """Name the burrow project a claimant belongs to, falling back to steward's own."""
        for resident in self.residents:
            if resident.agent_id == claimant:
                return resident.project
        return ev.API_PROJECT

    def decisions_for(self, resident_id: str) -> str | None:
        """Return this resident's undelivered approval decisions, and mark them told.

        Handed to the scheduler as a callable so a parked session's answer arrives on the
        resident's *next* wake-up whatever that wake-up is — a routine or a board task.

        The read and the mark are one atomic store transaction
        (:meth:`steward.store.Store.claim_undelivered_decisions`), so two wake-ups of the
        same resident at the same instant cannot both be handed the same decision — one
        gets it, the other opens without it, and it is delivered exactly once (steward #74).
        The preamble is rendered from what *this* call claimed.
        """
        records = self.store.claim_undelivered_decisions(resident_id)
        return ap.decisions_preamble(records)

    def harvest(self, manifest: ResidentManifest, output: str) -> list[ApprovalRecord]:
        """Turn what a finished session wrote into requests and handoffs.

        Part of :class:`steward.scheduler.WakeHooks`, unchanged in shape: a routine session
        asks for approval exactly the way a board session does, and both land in the same
        store. Delegation rides the same hook rather than adding a second one — a
        ``<delegate>`` block is another thing a session writes on its way out, and the
        scheduler should not have to learn a new question to keep working.

        A routine session has no task above it, so anything it hands over is attributed to
        the resident's own initiative rather than to a parent task.
        """
        harvested = self.harvest_session(manifest, output)
        return list(cast("tuple[ApprovalRecord, ...]", harvested.raised))

    def harvest_session(
        self,
        manifest: ResidentManifest,
        output: str,
        *,
        parent_task_id: str | None = None,
        now: datetime | None = None,
    ) -> SessionHarvest:
        """Safely persist everything one completed session handed back."""
        raised = tuple(self._harvest_approvals(manifest, output, now))
        handed = self.hand_over(manifest, output, parent_task_id=parent_task_id, now=now)
        return SessionHarvest(raised, handed)

    def hand_over(
        self,
        manifest: ResidentManifest,
        output: str,
        *,
        parent_task_id: str | None = None,
        now: datetime | None = None,
    ) -> tuple[dg.Delivery, ...]:
        """Deliver every ``<delegate>`` block this session wrote, or knock about the refusal.

        Never raises: a handoff steward cannot deliver is a knock at a human's door, and
        neither a refusal nor a broken database may turn a finished session into a failed
        one. The session's own work is done either way.
        """
        sender = next((r for r in self.residents if r.id == manifest.id), None)
        if sender is None or not output:
            return ()
        try:
            return tuple(
                self.delegator.harvest(sender, output, parent_task_id=parent_task_id, now=now)
            )
        except Exception as exc:  # noqa: BLE001 — a failed handoff is not a failed task
            log.warning("%s: could not record a delegation from this session: %s", sender.id, exc)
            return ()

    # -- claiming ---------------------------------------------------------------------

    def claim(self, resident: Resident, now: datetime) -> JobRecord | None:
        """Claim the oldest open task this resident is qualified for, or return ``None``.

        Returns ``None`` for all three honest reasons — nothing open, nothing matching,
        or another resident won the race — and does not distinguish them here, because
        from the caller's side they are the same fact: this resident has no work.
        """
        claimed = self.tasks.claim(
            claimant=resident.agent_id,
            project=resident.project,
            skills=claimable_skills(resident.manifest, self.library),
            now=now,
            lease_s=resident.manifest.board.lease_s,
        )
        job = claimed.record
        if job is None:
            return None
        log.info("%s claimed task %s (%s)", resident.id, job.task_id, job.title)
        return job

    def take_delivery(self, resident: Resident, now: datetime) -> JobRecord | None:
        """Pick up the oldest letter waiting in this resident's inbox, or return ``None``.

        The claim half of delegation, and deliberately the *same* claim: one conditional
        write, a lease rather than a deed, and ``task_claimed`` under the receiver's own
        agent id. A delegated item is a task addressed to one resident, so everything the
        board already knows how to do to a task applies to it unchanged — including the
        lease sweep that puts it back if the receiver dies mid-session.
        """
        taken = self.tasks.take_delivery(
            assignee=resident.id,
            claimant=resident.agent_id,
            project=resident.project,
            now=now,
            lease_s=self.delegation_lease_s,
        )
        job = taken.record
        if job is None:
            return None
        log.info(
            "%s picked up delegated task %s (%s) from %s",
            resident.id,
            job.task_id,
            job.title,
            job.delegated_by,
        )
        return job

    # -- working ----------------------------------------------------------------------

    def _harvest_approvals(
        self, manifest: ResidentManifest, output: str, now: datetime | None
    ) -> list[ApprovalRecord]:
        """Raise every approval a session asked for. Never raises: a task is still closed.

        Wrapped like its neighbours (steward #80): a hook that throws while reading a
        session's escalations must not leave a claimed task with only ``task_claimed``
        emitted and no close, showing the village work that ended minutes ago as still
        in progress.
        """
        if not output:
            return []
        try:
            return self.approvals.harvest(manifest=manifest, output=output, now=now)
        except Exception as exc:  # noqa: BLE001 — a failed escalation is not a failed task
            log.warning("%s: could not record an approval from this session: %s", manifest.id, exc)
            return []

    def work(
        self,
        resident: Resident,
        job: JobRecord,
        now: datetime | None = None,
        *,
        admission: Admission | None = None,
    ) -> BoardReport:
        """Run one claimed task to a conclusion and record it. Never raises."""
        moment = now or datetime.now(UTC)
        # A letter and a notice declare their own timeouts, and the budget caps whichever
        # one applies: ``max_run_seconds`` is a ceiling on any session, however it woke up.
        declared_s = (
            self.delegation_timeout_s if job.delegated else resident.manifest.board.timeout_s
        )
        admitted = admission or self.sessions.admit(resident, now=moment)
        if isinstance(admitted, Refusal):
            result = RunResult(outcome=Outcome.FAILED, error=admitted.reason)
            return self._record(
                resident,
                job,
                result,
                moment,
                (),
                run_id=new_id(),
            )
        # This session's own id, and not the task's: a task claimed, dropped on a dead
        # lease and claimed again is *two* sessions, and the registry has to be able to
        # hold both of them open at once (steward #39).
        run_id = new_id()
        if job.delegated:
            wake = DelegatedWake(
                task_id=job.task_id,
                title=job.title,
                detail=job.detail,
                timeout_s=declared_s,
                origin=job.origin or f"task:{job.task_id}",
                delegated_by=job.delegated_by,
                route=job.route or "delegation",
                parent_task_id=job.parent_task_id,
            )
        else:
            wake = TaskWake(
                task_id=job.task_id,
                title=job.title,
                detail=job.detail,
                required_skills=job.required_skills,
                timeout_s=declared_s,
                origin=job.origin or f"task:{job.task_id}",
                parent_task_id=job.parent_task_id,
            )
        # The deadline this session actually gets, read once: the run registry is judged
        # against it, and the runner is given it.
        timeout_s = admitted.timeout_for(declared_s)
        self._open_run(resident, job, run_id, timeout_s, moment)

        registry_run = _RegistryRun(run_id, job.task_id, moment)
        token = self._registry_run.set(registry_run)
        try:
            session = self.sessions.run(admitted, wake)
        finally:
            self._registry_run.reset(token)
        result = session.require_result()

        # The shared lifecycle contains and returns from every ordinary accounting or
        # harvest failure, so reaching here means this board-owned registry row can close
        # before the board records its task-specific conclusion. A process that vanishes
        # inside the lifecycle deliberately leaves the row open for the watchdog.
        _close_registry(self.store, registry_run, session.completed_at or moment)
        return self._record(
            resident,
            job,
            result,
            moment,
            cast("tuple[ApprovalRecord, ...]", session.raised),
            handed=cast("tuple[dg.Delivery, ...]", session.handed_over),
            run_id=run_id,
        )

    # -- the run registry ---------------------------------------------------------------

    def _open_run(
        self, resident: Resident, job: JobRecord, run_id: str, timeout_s: int, moment: datetime
    ) -> None:
        """Write this session into steward's run registry. Never raises.

        The scheduler's :meth:`steward.scheduler.Scheduler._open_run` for the other kind
        of wake-up, and the registry does not care which it was: a claimed task is a
        session, so it belongs in the one place that knows which sessions are open
        (steward #39). What happens to a stale row differs — a routine that vanished is
        buried with the ``routine_failed`` its session never sent, while a task that
        vanished is the lease sweep's to reopen — but "steward started this and heard
        nothing back" is one fact with one home.

        The row is keyed by ``run_id`` — this session — and carries the task as its
        ``ref``. Keying it by the task instead used to lose every retry: the board's
        ordinary flow is claim, die, expire the lease, re-claim, and a second row under
        the id of a task the first attempt already closed is a conflict the registry
        drops. The second session then vanished with nothing left open to find, which is
        precisely the death this registry exists to catch.
        """
        try:
            opened = self.store.open_run(
                run_id=run_id,
                kind=RUN_DELEGATED if job.delegated else RUN_TASK,
                agent_id=resident.agent_id,
                project=resident.project,
                ref=job.task_id,
                timeout_s=float(timeout_s),
                now=ev.utc_now_iso(moment),
            )
        except Exception as exc:  # noqa: BLE001 — an unwritable registry is not a failed task
            log.warning(
                "%s: could not record that task %s started: %s", resident.id, job.task_id, exc
            )
            return
        if not opened:  # pragma: no cover — a fresh id per session cannot collide
            # Said out loud rather than shrugged off: an ignored open means this session
            # is invisible to the watchdog, and silence would make that look like health.
            log.warning(
                "%s: run %s was already recorded, so task %s is not being watched",
                resident.id,
                run_id,
                job.task_id,
            )

    def _registry_runner(self, spec: RunnerSpec) -> Runner:
        """Wrap the real adapter when this context is working a watched board run."""
        runner = self.runner_factory(spec)
        registry_run = self._registry_run.get()
        if registry_run is None:
            return runner
        return _RegistryClosingRunner(runner, self.store, registry_run)

    def _record(  # noqa: PLR0913 — one parameter per fact the report is built from
        self,
        resident: Resident,
        job: JobRecord,
        result: RunResult,
        moment: datetime,
        raised: Sequence[ApprovalRecord],
        *,
        run_id: str,
        handed: Sequence[dg.Delivery] = (),
    ) -> BoardReport:
        """Close the task on the board and say so, naming the session that did the work.

        ``run_id`` rides along into the closing event because a task id is not a session:
        it is the id of this attempt's run registry row, and it is what lets the watchdog
        tell this close from the close of an attempt that came before (steward #39). It is
        required rather than optional so a future caller cannot quietly drop the name.
        """
        closed = self.tasks.finish(
            job,
            claimant=resident.agent_id,
            project=resident.project,
            result=result,
            run_id=run_id,
            now=moment,
        )
        if closed.superseded:
            # The lease died while the session ran and the task is somebody else's now.
            # The transition wrote nothing and said nothing, and it carries no record —
            # the conditional update matched no row — so the pre-close row this dispatch
            # is already holding is the only honest thing to report. Branching on the
            # *outcome* rather than on a missing record keeps that true if the seam ever
            # learns to read the winning row back: "no record" would then mean "it worked"
            # and a refused close would be reported as a finished task.
            log.warning(
                "%s finished task %s but no longer held the claim — the board keeps its own record",
                resident.id,
                job.task_id,
            )
            return BoardReport(
                resident_id=resident.id,
                claimant=resident.agent_id,
                task=job,
                status=STATUS_FAILED,
                result=result,
                reason=closed.reason,
                raised=tuple(raised),
                handed_over=tuple(handed),
            )
        recorded = closed.require()
        return BoardReport(
            resident_id=resident.id,
            claimant=resident.agent_id,
            task=recorded,
            status=recorded.status,
            result=result,
            reason=closed.reason or None,
            artifacts=result.artifacts,
            raised=tuple(raised),
            handed_over=tuple(handed),
        )

    # -- the entry point ---------------------------------------------------------------

    def dispatch(self, now: datetime | None = None) -> DispatchRun:
        """Sweep, then let every board-enabled resident claim and work. Never raises.

        Order matters, twice. Expired leases are reopened *first*, so a task somebody
        dropped is available to whoever is waking up right now rather than a tick later;
        expired approvals are denied in the same breath, because both are deadlines that
        only exist if something visits them.

        Then the inboxes, and only then the board. Work addressed to you personally comes
        ahead of work addressed to nobody: a neighbour who handed you something is waiting
        on it, while a notice on the board is waiting on the fleet. A resident drains its
        inbox whether or not it claims from the board at all — accepting a letter is
        declared in ``routes``, not in ``board`` — and only while that route is open.

        Who may work at all is settled *before* either loop, in one pass over every
        resident a source could reach (:meth:`_claim_admissions`). A resident out of money is
        out of money for every way work can reach it, and a letter is not a loophole: the
        item stays in the inbox, addressed and unread, for whoever unpauses the resident
        tomorrow. Asking once rather than once per source is what keeps one exhausted
        budget to one knock, whatever a resident has opted into.
        """
        moment = now or self.clock()
        if self.dry_run:
            return self._rehearse(moment)
        reopened = self.expire_leases(moment)
        expired_approvals = [swept.require() for swept in self.approvals.expire(moment)]

        if self.sweep_only:
            return DispatchRun(reopened=tuple(reopened), expired_approvals=tuple(expired_approvals))
        admissions, refusals = self._claim_admissions(moment)
        reports = self._drain(
            delegation_residents(self.residents),
            moment,
            refusals,
            admissions,
            pick=self.take_delivery,
            count_for=lambda _r: self.max_delegations_per_wake,
        )
        reports += self._drain(
            board_residents(self.residents),
            moment,
            refusals,
            admissions,
            pick=self.claim,
            count_for=lambda r: r.manifest.board.max_claims_per_wake,
        )
        return DispatchRun(
            reopened=tuple(reopened),
            expired_approvals=tuple(expired_approvals),
            reports=tuple(reports),
        )

    def _drain(  # noqa: PLR0913 - the two work-source strategies are explicit callables
        self,
        residents: Sequence[Resident],
        moment: datetime,
        refusals: Mapping[str, str],
        admissions: Mapping[str, Admission],
        *,
        pick: Callable[[Resident, datetime], JobRecord | None],
        count_for: Callable[[Resident], int],
    ) -> list[BoardReport]:
        """Let each un-refused resident claim and work up to its cap. Never raises.

        ``refusals`` is the map :meth:`_claim_admissions` already built for this dispatch;
        a resident named in it has been asked, told, and logged once, so the drain only
        has to skip it rather than ask again.

        ``pick`` is the claim — an inbox pickup or a board claim — and the clock is read
        *per claim*, not once, so a slow drain's Nth lease is measured from when it was
        actually handed out rather than from the top of the dispatch (steward #73).
        """
        reports: list[BoardReport] = []
        for resident in residents:
            if resident.id in refusals:
                continue
            for _ in range(count_for(resident)):
                job = pick(resident, self.clock())
                if job is None:
                    break
                reports.append(self.work(resident, job, moment, admission=admissions[resident.id]))
        return reports

    def _claimants(self) -> list[Resident]:
        """Return every resident a dispatch could hand work to, in declared order.

        The union of the two work sources, inbox first and board second, deduped by id —
        so a resident that has opted into both appears once. This is the list the refusal
        pass walks, and the reason it can be walked ahead of any particular source.
        """
        seen: set[str] = set()
        claimants: list[Resident] = []
        for resident in (*delegation_residents(self.residents), *board_residents(self.residents)):
            if resident.id in seen:
                continue
            seen.add(resident.id)
            claimants.append(resident)
        return claimants

    def _claim_admissions(self, moment: datetime) -> tuple[dict[str, Admission], dict[str, str]]:
        """Admit each possible claimant once, before either work source claims."""
        admissions: dict[str, Admission] = {}
        refusals: dict[str, str] = {}
        for resident in self._claimants():
            admission = self.sessions.admit(resident, now=moment)
            if isinstance(admission, Refusal):
                log.warning("%s: not working — %s", resident.id, admission.reason)
                refusals[resident.id] = admission.reason
            else:
                admissions[resident.id] = admission
        return admissions, refusals

    def _rehearse(self, moment: datetime) -> DispatchRun:
        """Resolve what a real dispatch *would* do, writing nothing (steward #88).

        The board's honest ``--dry-run``: it reports the leases it would reopen, the
        approvals it would deny, and the tasks each resident would claim — reading the
        store, launching no session, and touching neither the ledger nor the event stream.
        Budget and workdir are read the same read-only way a real dispatch checks them, but
        a rehearsal never *pauses* a resident, so an exhausted one is simply reported as
        claiming nothing. Claims are planned against a running set of already-spoken-for
        task ids, so the plan does not hand the same notice to two residents.

        The shape mirrors the real dispatch deliberately, down to the single refusal pass
        ahead of both sources: a rehearsal that decided who may work differently from the
        thing it rehearses would be a rehearsal of something else.
        """
        now_iso = ev.utc_now_iso(moment)
        reopened = tuple(
            job
            for job in self.store.jobs(status="claimed")
            if job.lease_expires_at is not None and job.lease_expires_at <= now_iso
        )
        expired_approvals = tuple(
            record
            for record in self.store.pending_approvals()
            if record.expires_at is not None and record.expires_at <= now_iso
        )
        spoken_for: set[str] = set()
        refusals = self._rehearsal_refusals()
        planned = self._plan_delegations(spoken_for, refusals) + self._plan_board_claims(
            spoken_for, refusals
        )
        return DispatchRun(
            reopened=reopened, expired_approvals=expired_approvals, planned=tuple(planned)
        )

    def _plan_delegations(
        self, spoken_for: set[str], refusals: Mapping[str, str]
    ) -> list[PlannedClaim]:
        """Plan the letters each delegation resident would pick up, up to its cap."""
        planned: list[PlannedClaim] = []
        for resident in delegation_residents(self.residents):
            if resident.id in refusals:
                continue
            taken = 0
            for job in self.store.inbox(resident.id, status=STATUS_OPEN):
                if taken >= self.max_delegations_per_wake:
                    break
                if job.task_id in spoken_for:
                    continue
                spoken_for.add(job.task_id)
                taken += 1
                planned.append(
                    PlannedClaim(resident.id, resident.agent_id, job, source="delegated")
                )
        return planned

    def _plan_board_claims(
        self, spoken_for: set[str], refusals: Mapping[str, str]
    ) -> list[PlannedClaim]:
        """Plan the notices each board resident would claim, honouring skills and its cap."""
        planned: list[PlannedClaim] = []
        for resident in board_residents(self.residents):
            if resident.id in refusals:
                continue
            claimed_here = 0
            held = claimable_skills(resident.manifest, self.library)
            for job in self.store.jobs(status=STATUS_OPEN):
                if claimed_here >= resident.manifest.board.max_claims_per_wake:
                    break
                if job.assignee is not None or job.task_id in spoken_for:
                    continue
                if not job.claimable_by <= held:
                    continue
                spoken_for.add(job.task_id)
                claimed_here += 1
                planned.append(PlannedClaim(resident.id, resident.agent_id, job, source="board"))
        return planned

    def _rehearsal_refusals(self) -> dict[str, str]:
        """Map id → why not for every claimant, the read-only twin of real admission.

        One pass over the same union, so a rehearsal answers "who may work" exactly once
        per resident too — and reports the same resident as idle for both of its sources.
        """
        return {
            resident.id: refusal
            for resident in self._claimants()
            if (refusal := self._rehearsal_refusal(resident)) is not None
        }

    def _rehearsal_refusal(self, resident: Resident) -> str | None:
        """Return why a rehearsal shows this resident claiming nothing, writing nothing.

        Read-only on purpose: a real refusal *pauses* an exhausted resident and knocks at a
        door, and a rehearsal must do neither. It reports an existing pause or a missing
        workdir, and leaves the trip-check to the real dispatch.
        """
        if self.store.budget_pause(resident.id) is not None:
            return "paused"
        return workdir_refusal(resident, self.workdir, self.library)
