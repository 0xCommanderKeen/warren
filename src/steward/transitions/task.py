"""Task transitions: the board's durable changes, each with the fact that says it happened.

Five acts, and each pairs one write with one burrow event:

- :meth:`post` — a new open row, announced as ``task_posted``
- :meth:`claim` — ``open`` → ``claimed`` with a lease, announced as ``task_claimed``
- :meth:`take_delivery` — the same, narrowed to the one resident a letter is addressed to,
  and announced as the same ``task_claimed``
- :meth:`finish` — ``claimed`` → ``done`` or ``failed``, announced as ``task_done`` or
  ``task_failed``
- :meth:`expire_leases` — ``claimed`` → ``open``, mourned as ``task_failed`` with the
  reason ``lease_expired``

Four of the five are conditional writes that can lose, and losing is not an error: another
resident claimed the notice first, two wake-ups of one resident raced for the same letter,
or a lease died while a session was still running and the task belongs to somebody else
now. In every one of those cases this module writes nothing and says nothing. A
``task_done`` for a claim the sender no longer holds would overwrite somebody else's work
in the village even though the row itself refused it, which is precisely the disagreement
between row and log these transitions exist to prevent.
"""

import logging
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from steward import events as ev
from steward.runners import RunResult
from steward.store import STATUS_DONE, STATUS_FAILED, JobRecord, Store
from steward.transitions.outcome import APPLIED, Transition, applied, refused, superseded

__all__ = ["LEASE_EXPIRED", "LEASE_LOST", "NOTHING_TO_CLAIM", "TaskTransitions"]

log = logging.getLogger("steward.transitions.task")

#: The reason a ``task_failed`` carries when nobody finished what they claimed.
LEASE_EXPIRED = "lease_expired"

#: Why a close was refused: the lease died mid-session and the task is somebody else's.
LEASE_LOST = "lease lost while the session was running"

#: Why a claim came back empty. Deliberately one reason for all three honest causes —
#: nothing open, nothing matching, every race lost — because from the caller's side they
#: are the same fact: this resident has no work.
NOTHING_TO_CLAIM = "no task available for this claimant"


def _steward_project(_agent_id: str) -> str:
    """Attribute a claim to steward's own project when no fleet lookup was supplied."""
    return ev.API_PROJECT


@dataclass(frozen=True, slots=True)
class TaskTransitions:
    """The board's durable changes and their protocol facts, coordinated in one place.

    ``project_of`` maps a burrow agent id to the project its events are filed under. Only
    the lease sweep needs it — every other act is told the project by the caller that
    already resolved the resident — and it exists because a swept lease has to be mourned
    under the identity of whoever dropped it, including a resident that has since been
    retired. The default files everything under steward's own project, which is what a
    caller with no fleet in hand would have said anyway.
    """

    store: Store
    emitter: ev.Emitter
    #: ``default_factory`` rather than ``default``: a plain function as a field default is
    #: also a class attribute, and a function class attribute binds as a method on access,
    #: so ``self.project_of(claimant)`` would pass ``self`` as the agent id. ``slots=True``
    #: happens to hide that today — the rebuilt class pops the defaults out of the class
    #: dict — but the correctness of this line should not depend on a ``slots`` interaction.
    project_of: Callable[[str], str] = field(default_factory=lambda: _steward_project, repr=False)

    # -- posting -------------------------------------------------------------------------

    def post(
        self,
        *,
        title: str,
        detail: str = "",
        required_skills: Sequence[str] = (),
        posted_by: str = "api",
    ) -> Transition[JobRecord]:
        """Put a task on the board and announce it. Nobody is prompted.

        The board *burrow* renders is rebuilt from events alone, so a task that was never
        announced does not exist as far as the village is concerned. There is no guard
        here and no way to lose: an insert with a fresh id always wins.

        The identity the notice is filed under is not a parameter, unlike every other act
        here. The others are told the resident because a caller already resolved one; a
        posted notice has exactly one possible author — the board itself — so
        :func:`steward.events.task_posted_event` supplies ``steward:api`` and there is no
        knob for a caller to reach for and get wrong.
        """
        job = self.store.post_job(
            title=title,
            detail=detail,
            required_skills=required_skills,
            posted_by=posted_by,
        )
        return applied(
            self.emitter,
            job,
            ev.task_posted_event(
                task_id=job.task_id,
                title=job.title,
                required_skills=job.required_skills,
                posted_by=job.posted_by,
            ),
        )

    # -- claiming ------------------------------------------------------------------------

    def claim(
        self,
        *,
        claimant: str,
        project: str,
        skills: Iterable[str],
        now: datetime,
        lease_s: int,
    ) -> Transition[JobRecord]:
        """Claim the oldest open task this claimant is qualified for, and say who holds it.

        The lease is measured from ``now`` rather than from the top of a dispatch, so the
        Nth claim of a slow drain is born with a full lease rather than one already eaten
        into by the sessions before it (steward #73).

        A caller that loses the race emits nothing at all: the store's conditional
        ``UPDATE … WHERE status = 'open'`` is what makes exactly one ``task_claimed`` per
        claim true, and this is the branch that honours it.
        """
        job = self.store.claim_next_job(
            claimant=claimant,
            skills=skills,
            lease_expires_at=ev.utc_now_iso(now + timedelta(seconds=lease_s)),
            now=ev.utc_now_iso(now),
        )
        if job is None:
            return refused(NOTHING_TO_CLAIM)
        return self._announce_claim(job, claimant=claimant, project=project)

    def take_delivery(
        self,
        *,
        assignee: str,
        claimant: str,
        project: str,
        now: datetime,
        lease_s: int,
    ) -> Transition[JobRecord]:
        """Pick up the oldest letter waiting in one resident's inbox, and say who holds it.

        Deliberately the *same* transition as :meth:`claim`: one conditional write, a
        lease rather than a deed, and ``task_claimed`` under the receiver's own agent id.
        A delegated item is a task addressed to one resident, so everything the board
        already knows how to do to a task applies to it unchanged — including the lease
        sweep that puts it back if the receiver dies mid-session.

        ``assignee`` is the resident id the item was addressed to; ``claimant`` is the
        burrow agent id the pickup is recorded and emitted under.
        """
        job = self.store.claim_next_delegated(
            assignee=assignee,
            claimant=claimant,
            lease_expires_at=ev.utc_now_iso(now + timedelta(seconds=lease_s)),
            now=ev.utc_now_iso(now),
        )
        if job is None:
            return refused(NOTHING_TO_CLAIM)
        return self._announce_claim(job, claimant=claimant, project=project)

    def _announce_claim(
        self, job: JobRecord, *, claimant: str, project: str
    ) -> Transition[JobRecord]:
        """Tell the village who holds this work now, delegated or claimed."""
        return applied(
            self.emitter,
            job,
            ev.task_claimed_event(
                task_id=job.task_id,
                title=job.title,
                claimant=claimant,
                project=project,
                parent_task_id=job.parent_task_id,
            ),
        )

    # -- closing -------------------------------------------------------------------------

    def finish(  # noqa: PLR0913 — one keyword per fact the close is built from
        self,
        job: JobRecord,
        *,
        claimant: str,
        project: str,
        result: RunResult,
        run_id: str,
        now: datetime,
        announce: bool = True,
    ) -> Transition[JobRecord]:
        """Close a claimed task on the board and say how it went. Only its claimant may.

        Whether this is a ``done`` or a ``failed`` is derived here, from one place, rather
        than in each caller: the row's status, its ``reason`` column, and the event type
        all follow from :attr:`steward.runners.RunResult.ok`, and a caller that decided
        two of the three itself could disagree with the third.

        The close carries the lease token this claim was handed (``job.claimed_at``), so a
        session whose lease expired, was swept, and re-claimed cannot come back and close
        the *live* claim it no longer holds (steward #72). When that happens the write
        matches nothing and the transition is **superseded**: the board keeps its own
        record, and the village hears nothing, because the task genuinely is somebody
        else's now.

        ``run_id`` names *this attempt's* run registry row rather than the task, because a
        task claimed, dropped on a dead lease and claimed again is two sessions and the
        watchdog has to be able to tell their closes apart (steward #39).
        """
        status = STATUS_DONE if result.ok else STATUS_FAILED
        reason = None if result.ok else f"{result.outcome}: {result.summary()}"
        closed = self.store.finish_job(
            job.task_id,
            status=status,
            claimant=claimant,
            outcome=str(result.outcome),
            reason=reason,
            artifacts=result.artifacts,
            lease=job.claimed_at,
            now=ev.utc_now_iso(now),
        )
        if closed is None:
            # Nothing is logged here on purpose. This seam knows the *claimant* — a burrow
            # agent id — and the line the board has always written names the resident, so
            # it stays with the caller that has one (:meth:`steward.board.Dispatcher._record`).
            # A superseded close comes back carrying its reason and no record; what an
            # operator reads about it is the board's sentence to write.
            return superseded(LEASE_LOST)
        if result.ok:
            fact = ev.task_done_event(
                task_id=job.task_id,
                title=job.title,
                claimant=claimant,
                project=project,
                artifacts=result.artifacts,
                parent_task_id=job.parent_task_id,
                run_id=run_id,
            )
        else:
            fact = ev.task_failed_event(
                task_id=job.task_id,
                title=job.title,
                claimant=claimant,
                project=project,
                reason=reason or str(result.outcome),
                parent_task_id=job.parent_task_id,
                run_id=run_id,
            )
        if not announce:
            return Transition(APPLIED, record=closed, fact=fact, reason=reason or "")
        return applied(self.emitter, closed, fact, reason or "")

    # -- the deadline --------------------------------------------------------------------

    def expire_leases(self, *, now: datetime) -> list[Transition[JobRecord]]:
        """Return every task whose lease ran out to the board, loudly.

        Returns one **applied** transition per lease swept, each carrying the row *as it
        was when the lease died* — still ``claimed``, claimant still named — because that
        is what the ``task_failed`` has to carry, and because the caller reports what was
        reopened rather than what is open now.

        Transitions rather than bare rows, like :meth:`ApprovalTransitions.expire`: a
        sweep is a batch of transitions and this says so. Building one per row and
        dropping it would make :func:`steward.transitions.outcome.applied` a conduit for a
        fire-and-forget emit, which is the shape this package exists to not have.

        A row that changed under the sweep is simply absent: the store's per-row
        conditional update refused it, so nothing was written here and nothing is said
        about it.

        The event carries no ``run_id`` on purpose. This is the board mourning a claim,
        not a session reporting back: steward does not know which session dropped it, and
        naming one would answer that session's registry row — the very silence the
        registry exists to catch — while the retry claimed moments later in this same pass
        is exactly the row a guess would land on (steward #39).
        """
        swept: list[Transition[JobRecord]] = []
        for job in self.store.expire_leases(ev.utc_now_iso(now)):
            claimant = job.claimant or ev.API_AGENT_ID
            log.warning(
                "task %s (%s): lease held by %s expired at %s — back on the board",
                job.task_id,
                job.title,
                claimant,
                job.lease_expires_at,
            )
            swept.append(
                applied(
                    self.emitter,
                    job,
                    ev.task_failed_event(
                        task_id=job.task_id,
                        title=job.title,
                        claimant=claimant,
                        project=self.project_of(claimant),
                        reason=LEASE_EXPIRED,
                        parent_task_id=job.parent_task_id,
                    ),
                )
            )
        return swept
