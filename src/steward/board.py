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
from steward import events as ev
from steward import journal as journal_module
from steward.manifest import ManifestError, Resident, ResidentManifest, validate_path
from steward.prompt import assemble_task_prompt
from steward.runners import Outcome, RunRequest, RunResult, build_runner, skills_home
from steward.scheduler import RunnerFactory
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
from steward.store import STATUS_DONE, STATUS_FAILED, ApprovalRecord, JobRecord, Store

__all__ = [
    "LEASE_EXPIRED",
    "BoardReport",
    "DispatchRun",
    "Dispatcher",
    "board_residents",
    "claimable_skills",
    "load_board_residents",
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
    """Return the residents whose manifests opt into board work, in declared order."""
    return [resident for resident in residents if resident.manifest.board.claim]


def load_board_residents(
    residents_dir: Path | str, skills_dir: Path | str | None = None
) -> list[Resident]:
    """Collect every board-enabled resident under a residents tree.

    Invalid manifests never reach the board, for the same reason they never reach the
    scheduler: a resident steward cannot read is a resident steward will not run — and a
    resident granted a skill the library does not have is one steward cannot read.
    """
    result = validate_path(residents_dir, skills_dir)
    return board_residents(result.residents)


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

    @property
    def done(self) -> bool:
        """True only when the session finished the task on its own terms."""
        return self.status == STATUS_DONE


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
    #: Do the housekeeping and stop: reopen dead leases, deny stale approvals, claim
    #: nothing. Deliberately *not* called a dry run, because it writes — the scheduler's
    #: ``--dry-run`` is a rehearsal that changes nothing at all, and one word should not
    #: mean two things.
    sweep_only: bool = False

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
        sweep_only: bool = False,
    ) -> Dispatcher:
        """Build a dispatcher over the board-enabled residents of a residents tree.

        The library is resolved the same way every other entry point resolves it — an
        explicit one wins, otherwise the library beside the tree — so the board matches
        and provisions against the same skills the scheduler does.
        """
        resolved = library if library is not None else library_for(residents_dir, skills_dir)
        return cls(
            residents=load_board_residents(residents_dir, skills_dir),
            store=store,
            emitter=emitter or ev.EventEmitter.from_env(),
            workdir=workdir if workdir is not None else Path.cwd(),
            runner_factory=runner_factory,
            library=resolved,
            sweep_only=sweep_only,
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
        """Turn any ``<needs-human>`` block a finished session wrote into a request.

        Part of :class:`steward.scheduler.WakeHooks`: a routine session asks for approval
        exactly the way a board session does, and both land in the same store.
        """
        return approvals.harvest(self.store, self.emitter, manifest=manifest, output=output)

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
        self.emitter.emit(
            ev.task_claimed_event(
                task_id=job.task_id,
                title=job.title,
                claimant=resident.agent_id,
                project=resident.project,
            )
        )
        return job

    # -- working ----------------------------------------------------------------------

    def build_prompt(self, resident: Resident, job: JobRecord) -> str:
        """Assemble a board session's prompt, through the one prompt module."""
        return assemble_task_prompt(
            resident.manifest,
            task_id=job.task_id,
            title=job.title,
            detail=job.detail,
            required_skills=job.required_skills,
            soul_text=resident.soul.body,
            journal_entry=self._journal_for(resident),
            skills=self.skills_for(resident),
            decisions=self.decisions_for(resident.id),
        )

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
        board = resident.manifest.board

        try:
            self.provision(resident, workdir)
            runner = self.runner_factory(resident.manifest.runner)
            result = runner.run(
                RunRequest(
                    prompt=prompt,
                    workdir=workdir,
                    timeout_s=board.timeout_s,
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

        raised = approvals.harvest(
            self.store,
            self.emitter,
            manifest=resident.manifest,
            output=result.output,
            now=moment,
        )
        return self._record(resident, job, result, moment, raised)

    def _record(
        self,
        resident: Resident,
        job: JobRecord,
        result: RunResult,
        moment: datetime,
        raised: Sequence[ApprovalRecord],
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
            )

        if result.ok:
            self.emitter.emit(
                ev.task_done_event(
                    task_id=job.task_id,
                    title=job.title,
                    claimant=resident.agent_id,
                    project=resident.project,
                    artifacts=result.artifacts,
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
        )

    def _session_env(self, resident: Resident, job: JobRecord) -> dict[str, str]:
        """Build the env a board session inherits, so its own emitter reports truthfully."""
        return {
            "BURROW_AGENT_ID": resident.agent_id,
            "BURROW_PROJECT": resident.project,
            "STEWARD_TASK_ID": job.task_id,
        }

    # -- the entry point ---------------------------------------------------------------

    def dispatch(self, now: datetime | None = None) -> DispatchRun:
        """Sweep, then let every board-enabled resident claim and work. Never raises.

        Order matters. Expired leases are reopened *first*, so a task somebody dropped is
        available to whoever is waking up right now rather than a tick later. Expired
        approvals are denied in the same breath, because both are deadlines that only
        exist if something visits them.
        """
        moment = now or datetime.now(UTC)
        reopened = self.expire_leases(moment)
        expired_approvals = approvals.expire(self.store, self.emitter, moment)

        reports: list[BoardReport] = []
        if self.sweep_only:
            return DispatchRun(reopened=tuple(reopened), expired_approvals=tuple(expired_approvals))
        for resident in board_residents(self.residents):
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
