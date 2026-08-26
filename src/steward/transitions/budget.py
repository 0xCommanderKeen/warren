"""Budget transitions: stopping a resident, and letting a person start it again.

Two acts, and neither of them has an event type of its own. What the village sees when a
budget trips is the ``needs_human`` the pause knocks with, and what it sees when somebody
lifts one from a terminal is the ``needs_human_resolved`` that answers it. That is on
purpose: a pause is not a new kind of thing the protocol has to learn, it is a question
steward asks about a resident, and it is asked through the same door every other gated
action uses.

Which is exactly why the two must not be conflated in the other direction either. A budget
pause is *not* a generic approval: it never expires (deny-by-default protects nothing when
the safe state is already the current one), it is exempt from the repeat-deny guard, and
it is one-per-condition by its own conditional insert rather than by anything the approval
machinery does. Those three facts live here, beside the write they qualify.

Gauge policy — what a budget is, what a window is, what counts as tripped, whether a
standing allowance covers this moment — stays in :mod:`steward.budgets`. This module is
asked to stop a resident, or to start it again.
"""

import logging
from dataclasses import dataclass
from datetime import datetime

from steward import events as ev
from steward.approvals import NeedsHuman
from steward.manifest import ResidentManifest
from steward.store import PauseRecord, Store, new_id
from steward.transitions.approval import ApprovalTransitions
from steward.transitions.outcome import Transition, applied, carried, refused, superseded

__all__ = ["ALREADY_PAUSED", "NOTHING_PAUSED", "BudgetTransitions"]

log = logging.getLogger("steward.transitions.budget")

#: Why a resume did nothing: this resident was not stopped in the first place.
NOTHING_PAUSED = "no budget pause to lift"

#: Why a pause added nothing: somebody tripped the same budget in the same instant and
#: already knocked. Their row and their request stand.
ALREADY_PAUSED = "this resident was already paused"


@dataclass(frozen=True, slots=True)
class BudgetTransitions:
    """The pause row, the knock that goes with it, and the unpause that lifts both."""

    store: Store
    emitter: ev.Emitter

    @property
    def approvals(self) -> ApprovalTransitions:
        """The approval seam a pause knocks through and a resume answers through."""
        return ApprovalTransitions(store=self.store, emitter=self.emitter)

    def pause(  # noqa: PLR0913 — one keyword per column of the pause or field of the knock
        self,
        *,
        manifest: ResidentManifest,
        agent_id: str,
        budget: str,
        spent: float,
        cap: float,
        reason: str,
        knock: NeedsHuman,
        message: str,
        window_end: str = "",
        now: datetime | None = None,
    ) -> Transition[PauseRecord]:
        """Stop this resident and knock once. The conditional insert decides who knocks.

        The request id is minted here and threaded into *both* halves, so the pause row and
        the approval it can be lifted by name each other. That is what
        ``steward budget unpause`` and ``POST /approvals/{id}`` both navigate by, and what
        lets a refusal tell a human which request would still lift the stop.

        **superseded** means somebody tripped the same budget in the same instant and
        already knocked: their row and their request stand, this call adds nothing, and
        nobody is knocked on twice for one stop. The existing record still comes back, so
        the caller can render the refusal it was going to render anyway.

        The knock is raised with ``repeat_guard=False`` deliberately. A human denying
        yesterday's unpause must not swallow today's pause: that deny answered "may this
        resident run again", not "has it stopped again".
        """
        request_id = new_id()
        record, created = self.store.pause_resident(
            resident=manifest.id,
            agent_id=agent_id,
            budget=budget,
            spent=spent,
            cap=cap,
            reason=reason,
            request_id=request_id,
            window_end=window_end,
            now=ev.utc_now_iso(now) if now is not None else None,
        )
        if not created:
            return superseded(ALREADY_PAUSED, record=record)
        log.warning("%s: %s — pausing this resident and knocking at the door", manifest.id, reason)
        knocked = self.approvals.raise_request(
            manifest=manifest,
            request=knock,
            message=message,
            now=now,
            request_id=request_id,
            repeat_guard=False,
        )
        return carried(record, knocked)

    def resume(
        self,
        resident_id: str,
        *,
        decided_by: str = "cli",
        decide: bool = True,
    ) -> Transition[PauseRecord]:
        """Lift a budget pause, and answer the request that was waiting on it.

        ``decide=False`` is the API's path: ``POST /approvals/{id}`` has already recorded
        the decision and emitted ``needs_human_resolved``, and recording it twice would put
        two answers in the log for one question. ``decide=True`` is the CLI's path, where
        nobody has answered anything yet, so the same request is resolved here and the same
        event is emitted — an unpause from a terminal and an unpause from a panel leave the
        village looking identical, because they are the same act.

        The unpause is the durable change either way; on the API path it simply has no fact
        of its own to carry. **refused** means there was no pause to lift, and then nothing
        is written and no allowance is granted.
        """
        pause = self.store.unpause_resident(resident_id)
        if pause is None:
            return refused(NOTHING_PAUSED)
        if pause.window_end:
            # "Carry on" scoped to the day it was said about. Without this the next fire
            # would re-trip the same cap and knock again, and answering a question would
            # be answering it into a loop.
            self.store.grant_budget_allowance(
                resident_id,
                until=pause.window_end,
                granted_by=decided_by,
                reason=pause.reason,
            )
        log.info("%s: budget pause lifted by %s (%s)", resident_id, decided_by, pause.reason)
        if decide and pause.request_id:
            return carried(
                pause, self.approvals.decide(pause.request_id, "approve", decided_by=decided_by)
            )
        return applied(self.emitter, pause)
