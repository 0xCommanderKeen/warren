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
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

from steward import approvals
from steward import delegation as dg
from steward import events as ev
from steward import journal as journal_module
from steward.manifest import (
    DEFAULT_BOARD_LEASE_S,
    DEFAULT_BOARD_TIMEOUT_S,
    ManifestError,
    Resident,
    ResidentManifest,
    active_residents,
    validate_path,
)
from steward.prompt import assemble_delegated_prompt, assemble_task_prompt
from steward.runners import Outcome, RunRequest, RunResult, build_runner, skills_home
from steward.scheduler import RunGuard, RunnerFactory
from steward.skills import (
    Skill,
    SkillError,
    SkillLibrary,
    describe_missing,
    effective_names,
    effective_skills,
    library_for,
    materialize,
    missing_skills,
)
from steward.store import (
    RUN_DELEGATED,
    RUN_TASK,
    STATUS_DONE,
    STATUS_FAILED,
    ApprovalRecord,
    JobRecord,
    Store,
)

__all__ = [
    "LEASE_EXPIRED",
    "BoardReport",
    "DispatchRun",
    "Dispatcher",
    "board_residents",
    "claimable_skills",
    "delegation_residents",
    "load_board_residents",
    "load_residents",
]

log = logging.getLogger("steward.board")

#: The reason a ``task_failed`` carries when nobody finished what they claimed.
LEASE_EXPIRED = "lease_expired"


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

    def __bool__(self) -> bool:
        """Report whether this dispatch actually changed anything."""
        return bool(self.reopened or self.expired_approvals or self.reports)


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
    #: The budget seam (:class:`steward.scheduler.RunGuard`). A paused resident is skipped
    #: *before* it claims anything, not after: a claim it cannot work would hold a real
    #: task hostage for a full lease while the resident sat stopped.
    guard: RunGuard | None = None
    #: Do the housekeeping and stop: reopen dead leases, deny stale approvals, claim
    #: nothing. Deliberately *not* called a dry run, because it writes — the scheduler's
    #: ``--dry-run`` is a rehearsal that changes nothing at all, and one word should not
    #: mean two things.
    sweep_only: bool = False
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
            max_delegation_depth=(
                max_delegation_depth if max_delegation_depth is not None else dg.max_depth()
            ),
        )

    @property
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
        """
        expired = self.store.expire_leases(ev.utc_now_iso(now))
        for job in expired:
            claimant = job.claimant or ev.API_AGENT_ID
            log.warning(
                "task %s (%s): lease held by %s expired at %s — back on the board",
                job.task_id,
                job.title,
                claimant,
                job.lease_expires_at,
            )
            self.emitter.emit(
                ev.task_failed_event(
                    task_id=job.task_id,
                    title=job.title,
                    claimant=claimant,
                    project=self._project_of(claimant),
                    reason=LEASE_EXPIRED,
                    parent_task_id=job.parent_task_id,
                )
            )
        return expired

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
        """
        text, _records = approvals.deliver_decisions(self.store, resident_id)
        return text

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
        raised = approvals.harvest(self.store, self.emitter, manifest=manifest, output=output)
        self.hand_over(manifest, output)
        return raised

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
        board = resident.manifest.board
        lease_until = ev.utc_now_iso(now + timedelta(seconds=board.lease_s))
        job = self.store.claim_next_job(
            claimant=resident.agent_id,
            skills=claimable_skills(resident.manifest, self.library),
            lease_expires_at=lease_until,
            now=ev.utc_now_iso(now),
        )
        if job is None:
            return None
        log.info("%s claimed task %s (%s)", resident.id, job.task_id, job.title)
        self._announce_claim(resident, job)
        return job

    def take_delivery(self, resident: Resident, now: datetime) -> JobRecord | None:
        """Pick up the oldest letter waiting in this resident's inbox, or return ``None``.

        The claim half of delegation, and deliberately the *same* claim: one conditional
        write, a lease rather than a deed, and ``task_claimed`` under the receiver's own
        agent id. A delegated item is a task addressed to one resident, so everything the
        board already knows how to do to a task applies to it unchanged — including the
        lease sweep that puts it back if the receiver dies mid-session.
        """
        job = self.store.claim_next_delegated(
            assignee=resident.id,
            claimant=resident.agent_id,
            lease_expires_at=ev.utc_now_iso(now + timedelta(seconds=self.delegation_lease_s)),
            now=ev.utc_now_iso(now),
        )
        if job is None:
            return None
        log.info(
            "%s picked up delegated task %s (%s) from %s",
            resident.id,
            job.task_id,
            job.title,
            job.delegated_by,
        )
        self._announce_claim(resident, job)
        return job

    def _announce_claim(self, resident: Resident, job: JobRecord) -> None:
        """Tell the village who holds this work now, delegated or claimed."""
        self.emitter.emit(
            ev.task_claimed_event(
                task_id=job.task_id,
                title=job.title,
                claimant=resident.agent_id,
                project=resident.project,
                parent_task_id=job.parent_task_id,
            )
        )

    # -- working ----------------------------------------------------------------------

    def build_prompt(self, resident: Resident, job: JobRecord) -> str:
        """Assemble a session's prompt for this task, through the one prompt module.

        Both kinds of task go through the same preamble and differ only in their last
        section: a notice off the board says so, and a letter names who sent it and which
        route it arrived through.
        """
        journal_entry = self._journal_for(resident)
        skills = self.skills_for(resident)
        decisions = self.decisions_for(resident.id)
        if job.delegated:
            return assemble_delegated_prompt(
                resident.manifest,
                task_id=job.task_id,
                title=job.title,
                detail=job.detail,
                sender=self._sender_label(job.delegated_by),
                route=job.route or "delegation",
                parent_task_id=job.parent_task_id,
                soul_text=resident.soul.body,
                journal_entry=journal_entry,
                skills=skills,
                decisions=decisions,
            )
        return assemble_task_prompt(
            resident.manifest,
            task_id=job.task_id,
            title=job.title,
            detail=job.detail,
            required_skills=job.required_skills,
            soul_text=resident.soul.body,
            journal_entry=journal_entry,
            skills=skills,
            decisions=decisions,
        )

    def _sender_label(self, sender_id: str | None) -> str:
        """Name the sender the way the receiving session will read it."""
        if not sender_id:
            return "another resident"
        for resident in self.residents:
            if resident.id == sender_id:
                return f"{resident.manifest.soul.name} ({resident.id})"
        return sender_id

    def skills_for(self, resident: Resident) -> tuple[Skill, ...]:
        """Resolve this resident's effective skill set, exactly as the scheduler does.

        The same library, the same resolution: a resident is told the same skills whether
        it woke up for a routine of its own or claimed a notice off the board.
        """
        return effective_skills(resident.manifest, self.library)

    def provision(self, resident: Resident, workdir: Path) -> None:
        """Put this resident's skills where a board session can reach them, or refuse.

        The scheduler's :meth:`steward.scheduler.Scheduler.provision` for the other kind
        of wake-up, and it refuses on the same terms: a granted skill the library does not
        have raises :class:`steward.skills.SkillError` before the session starts, which
        the caller records as a failed task rather than a session that believes it has a
        capability nobody gave it.
        """
        missing = missing_skills(resident.manifest, self.library)
        if missing:
            raise SkillError(describe_missing(resident.id, missing, self.library))
        subdir = skills_home(resident.manifest.runner)
        if subdir is None or not self.library.configured:
            return
        result = materialize(self.skills_for(resident), workdir, subdir)
        log.debug("%s: skills %s", resident.id, result.summary())

    def _journal_for(self, resident: Resident) -> str | None:
        try:
            return journal_module.latest_entry(resident.manifest, source=resident.path)
        except ManifestError as exc:
            log.warning("%s: no journal — %s", resident.id, exc)
        except OSError as exc:
            log.warning("%s: could not reach the journal: %s", resident.id, exc)
        return None

    def work(self, resident: Resident, job: JobRecord, now: datetime | None = None) -> BoardReport:
        """Run one claimed task to a conclusion and record it. Never raises."""
        moment = now or datetime.now(UTC)
        prompt = self.build_prompt(resident, job)
        workdir = resident.workdir(self.workdir)
        # A letter and a notice declare their own timeouts, and the budget caps whichever
        # one applies: ``max_run_seconds`` is a ceiling on any session, however it woke up.
        declared_s = (
            self.delegation_timeout_s if job.delegated else resident.manifest.board.timeout_s
        )

        try:
            self.provision(resident, workdir)
            runner = self.runner_factory(resident.manifest.runner)
            result = runner.run(
                RunRequest(
                    prompt=prompt,
                    workdir=workdir,
                    timeout_s=self._timeout_for(resident, declared_s),
                    model=resident.manifest.runner.model,
                    env=self._session_env(resident, job),
                )
            )
        except SkillError as exc:
            # steward refused to start, so the claim is closed as failed rather than left
            # to rot until its lease expires: this task never had a chance of being done.
            log.error("%s: %s", resident.id, exc)  # noqa: TRY400 — a refusal is not a traceback
            result = RunResult(outcome=Outcome.FAILED, error=str(exc))
        except Exception as exc:  # noqa: BLE001 — a broken runner is a failed task, not a crash
            result = RunResult(outcome=Outcome.FAILED, error=f"{type(exc).__name__}: {exc}")

        self._ledger(resident, job, result, moment)
        raised = approvals.harvest(
            self.store,
            self.emitter,
            manifest=resident.manifest,
            output=result.output,
            now=moment,
        )
        # This task is the parent of anything the session handed on, which is what makes
        # the chain — and the budget it rolls up to — traceable past the first hop.
        handed = self.hand_over(
            resident.manifest, result.output, parent_task_id=job.task_id, now=moment
        )
        return self._record(resident, job, result, moment, raised, handed=handed)

    # -- the budget seam ---------------------------------------------------------------

    def budget_refusal(self, resident: Resident, now: datetime | None = None) -> str | None:
        """Return why this resident may not pick anything up right now, or ``None``.

        A board session is a session: it spends the same money out of the same daily cap
        as a routine does, so a resident paused by its budget does not claim, exactly as
        it does not fire. A *delegated* session is a session too — the neighbour who sent
        the letter does not get to spend money this resident no longer has — so the inbox
        is gated by this same question.

        A resident that both receives letters and claims notices is therefore asked twice
        in one dispatch, and that is safe rather than merely tolerable: the pause is a
        conditional insert, so the second ask reads back the pause the first one wrote and
        nobody's door is knocked on twice for one exhausted budget.
        """
        if self.guard is None:
            return None
        try:
            return self.guard.allow(resident.manifest, now)
        except Exception as exc:  # noqa: BLE001 — an unreadable budget claims nothing
            log.warning(
                "%s: could not read the budget, so nothing is claimed: %s", resident.id, exc
            )
            return f"budget unreadable: {type(exc).__name__}: {exc}"

    def _timeout_for(self, resident: Resident, declared_s: int) -> int:
        """Return the board session's effective timeout, capped by the manifest's budget."""
        if self.guard is None:
            return declared_s
        return self.guard.timeout_for(resident.manifest, declared_s)

    def _ledger(
        self, resident: Resident, job: JobRecord, result: RunResult, moment: datetime
    ) -> None:
        """Record what a claimed task cost. Never raises: the task still happened.

        A letter is ledgered as ``delegated`` and a notice as ``task``, against the
        resident that *did* the work rather than the one that asked for it. Somebody
        else's request still spends your day, and the cap that stops you is yours.
        """
        if self.guard is None:
            return
        kind = RUN_DELEGATED if job.delegated else RUN_TASK
        try:
            self.guard.record(
                resident.manifest,
                result=result,
                kind=kind,
                run_id=job.task_id,
                ref=job.task_id,
                now=moment,
            )
        except Exception as exc:  # noqa: BLE001 — the ledger must not take the board down
            log.warning("%s: could not record what task %s cost: %s", resident.id, job.task_id, exc)

    def _record(  # noqa: PLR0913 — one parameter per fact the report is built from
        self,
        resident: Resident,
        job: JobRecord,
        result: RunResult,
        moment: datetime,
        raised: Sequence[ApprovalRecord],
        *,
        handed: Sequence[dg.Delivery] = (),
    ) -> BoardReport:
        status = STATUS_DONE if result.ok else STATUS_FAILED
        reason = None if result.ok else f"{result.outcome}: {result.summary()}"
        closed = self.store.finish_job(
            job.task_id,
            status=status,
            claimant=resident.agent_id,
            outcome=str(result.outcome),
            reason=reason,
            artifacts=result.artifacts,
            now=ev.utc_now_iso(moment),
        )
        if closed is None:
            # The lease died while the session ran and the task is somebody else's now.
            # Reporting it done would overwrite a claim this resident no longer holds.
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
                reason="lease lost while the session was running",
                raised=tuple(raised),
                handed_over=tuple(handed),
            )

        if result.ok:
            self.emitter.emit(
                ev.task_done_event(
                    task_id=job.task_id,
                    title=job.title,
                    claimant=resident.agent_id,
                    project=resident.project,
                    artifacts=result.artifacts,
                    parent_task_id=job.parent_task_id,
                )
            )
        else:
            self.emitter.emit(
                ev.task_failed_event(
                    task_id=job.task_id,
                    title=job.title,
                    claimant=resident.agent_id,
                    project=resident.project,
                    reason=reason or str(result.outcome),
                    parent_task_id=job.parent_task_id,
                )
            )
        return BoardReport(
            resident_id=resident.id,
            claimant=resident.agent_id,
            task=closed,
            status=status,
            result=result,
            reason=reason,
            artifacts=result.artifacts,
            raised=tuple(raised),
            handed_over=tuple(handed),
        )

    def _session_env(self, resident: Resident, job: JobRecord) -> dict[str, str]:
        """Build the env a board session inherits, so its own emitter reports truthfully."""
        env = {
            "BURROW_AGENT_ID": resident.agent_id,
            "BURROW_PROJECT": resident.project,
            "STEWARD_TASK_ID": job.task_id,
        }
        if job.parent_task_id:
            # So a session that emits for itself can name the chain it is part of.
            env["STEWARD_PARENT_TASK_ID"] = job.parent_task_id
        return env

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

        Both loops ask the budget first. A resident out of money is out of money for every
        way work can reach it, and a letter is not a loophole: the item stays in the inbox,
        addressed and unread, for whoever unpauses the resident tomorrow.
        """
        moment = now or datetime.now(UTC)
        reopened = self.expire_leases(moment)
        expired_approvals = approvals.expire(self.store, self.emitter, moment)

        reports: list[BoardReport] = []
        if self.sweep_only:
            return DispatchRun(reopened=tuple(reopened), expired_approvals=tuple(expired_approvals))
        for resident in delegation_residents(self.residents):
            refusal = self.budget_refusal(resident, moment)
            if refusal is not None:
                log.warning("%s: not draining the inbox — %s", resident.id, refusal)
                continue
            for _ in range(self.max_delegations_per_wake):
                job = self.take_delivery(resident, moment)
                if job is None:
                    break
                reports.append(self.work(resident, job, moment))
        for resident in board_residents(self.residents):
            refusal = self.budget_refusal(resident, moment)
            if refusal is not None:
                log.warning("%s: not claiming — %s", resident.id, refusal)
                continue
            for _ in range(resident.manifest.board.max_claims_per_wake):
                job = self.claim(resident, moment)
                if job is None:
                    break
                reports.append(self.work(resident, job, moment))
        return DispatchRun(
            reopened=tuple(reopened),
            expired_approvals=tuple(expired_approvals),
            reports=tuple(reports),
        )
