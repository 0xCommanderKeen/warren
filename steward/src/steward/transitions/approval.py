"""Approval transitions: a knock at the door, an answer, and a deadline that answers itself.

Four acts, and the safety properties of :mod:`steward.approvals` live in the branching
here rather than in each caller:

- **deny by default** — :meth:`expire` is what makes ``expires_at`` real, and
  :meth:`decide` refuses a request whose deadline has already passed, so a click a minute
  late cannot slip a gated action through ahead of the sweep;
- **first decision wins** — a replay changes nothing, returns what was recorded, and
  emits nothing;
- **a deny answers for a while** — a repeat inside the window is recorded as an auto-deny
  and *nobody's phone buzzes*, which is the one durable write in all of steward that
  deliberately has no fact beside it;
- **a session that tried to ask and failed still reaches a person** — an unreadable block
  is raised like any other request, carrying its complaint.

The grammar stays in :mod:`steward.approvals`: what a ``<needs-human>`` block may say,
what a duration parses to, how a decision reads back to a resident. Nothing here
implements any of it — :meth:`ApprovalTransitions.harvest` calls
:func:`steward.approvals.extract_requests` to read a session's blocks, and the rules that
function applies are that module's, not this one's. This module writes, and it says what
it wrote.
"""

import logging
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from steward import events as ev
from steward import prompt
from steward.approvals import (
    DETAIL_MAX_CHARS,
    REPEAT_GUARD_EXEMPT_ACTIONS,
    NeedsHuman,
    extract_requests,
    human_message,
    repeat_deny_window_s,
)
from steward.input_bounds import validate_approval_edit
from steward.manifest import ResidentManifest
from steward.store import DECIDED_BY_REPEAT, ApprovalRecord, Store
from steward.transitions.outcome import (
    Transition,
    answered,
    applied,
    deliver,
    expired,
    refused,
    replayed,
)

__all__ = [
    "ALREADY_DECIDED",
    "DECISION_NOT_OFFERED",
    "PAST_DEADLINE",
    "UNKNOWN_REQUEST",
    "ApprovalOutboxWorker",
    "ApprovalTransitions",
]

log = logging.getLogger("steward.transitions.approval")


class ApprovalOutboxWorker:
    """One wakeable, non-overlapping owner of announcement retries and completion."""

    def __init__(
        self,
        transitions: "ApprovalTransitions",  # noqa: UP037 — class is declared below
        complete: Callable[[ApprovalRecord, str], bool],
        *,
        poll_interval: float = 1.0,
        close_timeout: float = 5.0,
    ) -> None:
        """Bind one transition seam and its idempotent post-ack completion."""
        if poll_interval <= 0:
            raise ValueError("approval outbox poll interval must be positive")
        if close_timeout <= 0:
            raise ValueError("approval outbox close timeout must be positive")
        self.transitions = transitions
        self.complete = complete
        self.poll_interval = poll_interval
        self.close_timeout = close_timeout
        self._lifecycle_lock = threading.Lock()
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        """Start the single worker thread and request an immediate startup pass."""
        with self._lifecycle_lock:
            if self._thread is not None and self._thread.is_alive():
                raise RuntimeError("approval outbox worker is already running")
            self._wake = threading.Event()
            self._stop = threading.Event()
            self._thread = threading.Thread(target=self._run, name="approval-outbox", daemon=True)
            self._thread.start()
            self._wake.set()

    def notify(self) -> None:
        """Wake the worker because a producer committed new work."""
        self._wake.set()

    def close(self, timeout: float | None = None) -> None:
        """Request shutdown, raising when an active pass cannot stop in time."""
        with self._lifecycle_lock:
            if self._thread is None:
                return
            self._stop.set()
            self._wake.set()
            self._thread.join(self.close_timeout if timeout is None else timeout)
            if self._thread.is_alive():
                raise TimeoutError(
                    "approval outbox worker did not stop before its shutdown deadline"
                )

    @property
    def alive(self) -> bool:
        """Whether the lifecycle-owned thread is still running."""
        return self._thread is not None and self._thread.is_alive()

    def _run(self) -> None:
        while not self._stop.is_set():
            failed = False
            try:
                self.transitions.reconcile_announcements()
                while not self._stop.is_set() and (
                    claimed := self.transitions.store.claim_approval_effects()
                ):
                    record, token = claimed
                    try:
                        if not self.complete(record, token):
                            self.transitions.store.release_approval_effects(
                                record.request_id, token
                            )
                            break
                    except Exception:
                        self.transitions.store.release_approval_effects(record.request_id, token)
                        raise
            except Exception:
                log.exception("approval outbox pass failed; it will retry")
                failed = True
            due = self.transitions.store.next_approval_work_at()
            timeout = 0.1 if failed else self.poll_interval
            if due is not None:
                deadline = datetime.fromisoformat(due)
                timeout = min(timeout, max(0.0, (deadline - datetime.now(UTC)).total_seconds()))
            self._wake.wait(timeout)
            self._wake.clear()


#: Why a decision was refused outright: there is no such request to decide.
UNKNOWN_REQUEST = "no such approval request"

#: Why a decision was refused although the request exists: its deadline has passed and
#: deny-by-default keeps the last word, whatever a late click says.
PAST_DEADLINE = "this request expired and denies by default"

#: Why a decision changed nothing: somebody already answered, and the first answer wins.
ALREADY_DECIDED = "this request was already decided"

#: Why a pending request refused an otherwise globally valid decision.
DECISION_NOT_OFFERED = "this decision was not offered for this approval request"


@dataclass(frozen=True, slots=True)
class ApprovalTransitions:
    """Approval requests, decisions and deadlines, each with the fact that closes it."""

    store: Store
    emitter: ev.Emitter

    # -- raising -------------------------------------------------------------------------

    def raise_request(
        self,
        *,
        manifest: ResidentManifest,
        request: NeedsHuman,
        now: datetime | None = None,
    ) -> Transition[ApprovalRecord]:
        """Persist what a *session* asked for, and knock. The record is what a human answers.

        A request that could not be parsed is persisted too, under
        :data:`steward.approvals.UNREADABLE_ACTION`, with the raw block and the complaint
        in its detail. That is why this takes a :class:`steward.approvals.NeedsHuman`
        rather than an action string: an escalation steward failed to read still has to
        reach a person.

        A session never gets to write its own knock: the one-liner is derived by
        :func:`steward.approvals.human_message` from the action, so the message can never
        disagree with what the decision is recorded against.

        The repeat-deny guard is always on here, because a session is exactly what it
        exists to stop. Two outcomes, and the second is the interesting one. **applied**
        is a pending row and a ``needs_human``. **answered** is the guard: the row is
        written already resolved, so the ledger still shows the resident asked and what it
        was told and the resident still hears the answer in its next preamble — but
        nothing is emitted, because a looping resident must not be able to knock on every
        wake-up.
        """
        return self._raise(manifest=manifest, request=request, now=now, repeat_guard=True)

    def knock(
        self,
        *,
        manifest: ResidentManifest,
        request: NeedsHuman,
        message: str,
        now: datetime | None = None,
        request_id: str | None = None,
    ) -> Transition[ApprovalRecord]:
        """Persist a request steward raises *about* a resident, and knock. Always applied.

        The other half of :meth:`raise_request`, named rather than spelled as a flag,
        because the two differ in every way that matters and a caller reaching for the
        wrong one gets no error. A budget pause, a watchdog give-up and a refused handoff
        are steward's own knocks: steward noticed, steward wrote the message, and the
        resident is the subject rather than the asker.

        Two consequences follow, and both are structural here rather than remembered at
        five call sites:

        - ``message`` is required, because steward knows what the action slug cannot
          carry — the number that tripped a cap, the recipient a handoff was refused for.
        - the repeat-deny guard is **off**, because these knocks are one-per-condition by
          their own conditional insert, they never expire, and the deny a human gave
          yesterday answers a *different* question. A knock that inherited the guard
          would be auto-denied on arrival with nobody's phone buzzing, which for a paused
          resident or a crash-looping one is exactly the news that must not go missing.

        With the guard off there is no auto-deny branch to take, so this always applies:
        a pending row and a ``needs_human``.

        ``request_id`` mints the row under an id the caller already threaded elsewhere —
        a budget pause names the request that can lift it, in the pause row itself.
        """
        return self._raise(
            manifest=manifest,
            request=request,
            message=message,
            now=now,
            request_id=request_id,
            repeat_guard=False,
        )

    def _raise(  # noqa: PLR0913 — the request plus everything the two public acts differ in
        self,
        *,
        manifest: ResidentManifest,
        request: NeedsHuman,
        message: str | None = None,
        now: datetime | None = None,
        request_id: str | None = None,
        repeat_guard: bool,
    ) -> Transition[ApprovalRecord]:
        """Write one request and, unless the guard already answered it, knock.

        The shared body of :meth:`raise_request` and :meth:`knock`. Private on purpose:
        ``repeat_guard`` is a fact about *which act this is*, not a knob a caller should
        be picking, and every choice of it is made by one of the two public methods above.
        """
        moment = now or datetime.now(UTC)
        agent_id = manifest.chronicle_agent_id
        project = manifest.chronicle_project
        detail: dict[str, Any] = dict(request.detail)
        if request.problem is not None:
            log.warning(
                "%s: could not read a needs-human block — %s; raising it anyway so "
                "somebody sees it",
                manifest.id,
                request.problem,
            )
            detail = {"problem": request.problem, "raw": request.raw[:DETAIL_MAX_CHARS]}

        repeat = repeat_guard and self._denied_recently(manifest.id, request.action, moment)
        record = self.store.create_approval_request(
            agent_id=agent_id,
            project=project,
            action=request.action,
            message=message or human_message(manifest, request.action),
            resident=manifest.id,
            detail=detail,
            options=request.options,
            expires_at=request.expires_at(moment),
            request_id=request_id,
            denied_by=DECIDED_BY_REPEAT if repeat else None,
        )
        if repeat:
            log.info(
                "%s: %s was already denied within the last %d h — auto-denied as a "
                "repeat, nobody knocked",
                manifest.id,
                request.action,
                repeat_deny_window_s() // 3600,
            )
            return answered(record, "already denied inside the repeat window")
        return applied(
            self.emitter,
            record,
            ev.needs_human_event(
                message=record.message,
                request_id=record.request_id,
                action=record.action,
                agent_id=record.agent_id,
                project=record.project,
                detail=record.detail,
                options=record.options,
                expires_at=record.expires_at,
            ),
        )

    def _denied_recently(self, resident: str, action: str, moment: datetime) -> bool:
        """Report whether this resident was already told no about this action, recently.

        The fingerprint is deliberately coarse: ``(resident, action)`` and nothing else. A
        request's ``detail`` is free-form, un-normalized JSON, so two asks that differ only
        in a timestamp inside it would read as different questions to any comparison and
        the guard would catch nothing. Coarse means a resident denied ``send_email`` to one
        address cannot ask about a *different* address for the rest of the window either —
        the trade steward makes on purpose, because the failure it exists to stop is a
        resident knocking on every wake-up.

        That trade is only payable when the action names what the resident asked for. For
        the slugs in :data:`steward.approvals.REPEAT_GUARD_EXEMPT_ACTIONS` it does not:
        they are catch-alls steward assigns, and every ask that lands under one is a
        *different* question wearing the same name.
        """
        if action in REPEAT_GUARD_EXEMPT_ACTIONS:
            return False
        window_s = repeat_deny_window_s()
        if window_s <= 0:
            return False
        since = ev.utc_now_iso(moment - timedelta(seconds=window_s))
        return self.store.recent_denials(resident, action, since) > 0

    def harvest(
        self,
        *,
        manifest: ResidentManifest,
        output: str,
        now: datetime | None = None,
    ) -> list[Transition[ApprovalRecord]]:
        """Turn every ``<needs-human>`` block a finished session wrote into a request.

        The one place a session's output becomes an approval, called by both session types
        — the scheduler's routines and the board's claimed tasks — so "how does a resident
        ask?" has a single answer that does not depend on why it woke up.

        Only the session's machine-read region is scanned
        (:func:`steward.prompt.harvestable`), and quoted or fenced parts of it are stripped
        first, so a ``<needs-human>`` block a session quoted back from an attacker-supplied
        job or task detail is not mistaken for the session actually asking (steward #62).

        Returns one transition per ask, raised and auto-denied alike, for the same reason
        the sweeps do: a batch of transitions is a batch of transitions, and collapsing it
        to rows here would throw away the one distinction
        :data:`steward.transitions.outcome.ANSWERED` exists to make.
        ``raise_request`` is the single act with two row-writing outcomes, and a caller
        reporting what a session asked for — ``steward approval raise`` renders it already
        — should be able to tell an ask somebody was knocked about from one the repeat
        guard swallowed on arrival. A caller that only wants the rows reads them off with
        ``require``, which is safe here because both outcomes wrote one (see
        :attr:`steward.transitions.outcome.Transition.wrote`).
        """
        return [
            self.raise_request(manifest=manifest, request=request, now=now)
            for request in extract_requests(prompt.harvestable(output))
        ]

    # -- deciding ------------------------------------------------------------------------

    def decide(  # noqa: PLR0913 — mirrors the atomic store decision inputs
        self,
        request_id: str,
        decision: str,
        *,
        decided_by: str = "api",
        edit: Mapping[str, Any] | None = None,
        now: datetime | None = None,
        request_log: tuple[str, str, str] | None = None,
    ) -> Transition[ApprovalRecord]:
        """Record one decision and close the loop in the log. The first decision wins.

        Four outcomes, and only the first says anything:

        - **applied** — this call recorded the answer, and ``needs_human_resolved`` was
          emitted under the *resident's* identity, because the villager walking away from
          your door is the one who knocked;
        - **refused** — there is no such request (``record`` is ``None``), or the pending
          request did not offer this decision (``record`` carries its offered set);
        - **expired** — the request exists and is *still pending*, which after a refused
          conditional write can only mean its deadline had passed. Deny-by-default has the
          last word and the sweep is what records it, so nothing is written or said here;
        - **replayed** — somebody already answered. A double-tapped notification changes
          nothing, reads back what was recorded, and emits nothing new.

        Replay and expiry take precedence over offered-set validation: retrying a different
        button still reads back the first answer, and a late click still denies by default.
        The store's conditional write remains the final race guard after these preconditions.
        """
        existing = self.store.approval(request_id)
        if existing is None:
            return refused(UNKNOWN_REQUEST)

        moment = ev.utc_now_iso(now) if now is not None else ev.utc_now_iso()
        if existing.pending:
            if existing.expires_at is not None and existing.expires_at <= moment:
                return expired(existing, PAST_DEADLINE)
            if decision not in existing.options:
                return refused(DECISION_NOT_OFFERED, existing)

        validate_approval_edit(edit)
        record, recorded = self.store.decide(
            request_id,
            decision,
            decided_by=decided_by,
            edit=edit,
            now=moment,
            request_log=request_log,
        )
        if record is None:
            return refused(UNKNOWN_REQUEST)
        if recorded:
            fact = self._announce(request_id)
            return Transition("applied", record=record, fact=fact)
        if record.pending:
            return expired(record, PAST_DEADLINE)
        # A replay is also a recovery opportunity: the decision is still exactly once,
        # but an announcement left pending by a dead process is retried.
        recovered = self._announce(request_id)
        if recovered is not None:
            return Transition("replayed", record=record, fact=recovered, reason=ALREADY_DECIDED)
        return replayed(record, ALREADY_DECIDED)

    def reconcile_announcements(self, *, limit: int | None = None) -> int:
        """Retry durable decision announcements left behind by an earlier process."""
        completed = 0
        while limit is None or completed < limit:
            if self._announce() is None:
                break
            completed += 1
        return completed

    def _announce(self, request_id: str | None = None) -> ev.Event | None:
        claimed = self.store.claim_approval_announcement(request_id)
        if claimed is None:
            return None
        record, token = claimed
        fact = ev.needs_human_resolved_event(
            request_id=record.request_id,
            decision=record.decision or "deny",
            action=record.action,
            agent_id=record.agent_id,
            project=record.project,
            decided_by=record.decided_by or "expiry",
        )
        accepted = False
        try:
            accepted = deliver(self.emitter, fact)
        finally:
            self.store.finish_approval_announcement(record.request_id, token, accepted=accepted)
        return fact if accepted else None

    # -- the deadline --------------------------------------------------------------------

    def expire(self, now: datetime | None = None) -> list[Transition[ApprovalRecord]]:
        """Deny every request whose deadline has passed, and close the loop in the log.

        Called on every board dispatch — which the scheduler tick, ``steward board
        dispatch`` and every watchdog pass all reach — and that is what makes the deadline
        real rather than decorative: nothing sweeps a queue that nobody visits.

        Returns one **applied** transition per request denied, rather than bare records.
        A sweep is a batch of transitions and this says so: each one carries the row that
        was denied and the ``needs_human_resolved`` that announced it, and a caller that
        only wants the rows reads them off. Building a transition and dropping it would
        make :func:`steward.transitions.outcome.applied` a conduit for a fire-and-forget
        emit, which is the shape this package exists to not have.

        A request a human answered in the same instant is simply absent: their answer won
        the store's conditional write, so this call wrote nothing about it and says
        nothing about it.
        """
        expired_records = self.store.expire_approvals(ev.utc_now_iso(now or datetime.now(UTC)))
        swept: list[Transition[ApprovalRecord]] = []
        for record in expired_records:
            log.info(
                "approval %s (%s) expired at %s — denied by default",
                record.request_id,
                record.action,
                record.expires_at,
            )
            swept.append(
                Transition("applied", record=record, fact=self._announce(record.request_id))
            )
        return swept
