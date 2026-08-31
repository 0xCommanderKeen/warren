"""One resident, one live session — and the promise kept across processes.

The scheduler has always promised *one run per routine at a time*, and it kept that
promise with ``threading.Lock``s: :meth:`steward.scheduler.Scheduler._claim` and the
per-resident locks beside it. Locks are invisible to a second process, and steward has had
several for a long time — the API fires manual runs, ``steward board dispatch`` works
tasks, and a chat daemon (warren#108) will answer messages that arrive whenever they
arrive. Every one of them could open a session for a resident that was already mid-run,
and none of them could see the lock that said so (warren#111).

Budgets never had this hole, and the reason is the whole design of this module: the ledger
lives in the shared database, so two processes reading it read the same fact. The overlap
claim is moved to the same medium — one row per resident in ``resident_claims``, taken with
a conditional write, exactly the way the board claims a task (``UPDATE … WHERE
status='open'``) and the watchdog claims a terminal close.

**A claim is a lease, not a deed.** A holder that died — SIGKILL, a machine that went away,
a container that stopped — releases nothing, so the claim has to be reclaimable without it.
It is kept alive by a heartbeat thread and becomes takeable once that heartbeat is
:data:`CLAIM_GRACE_S` old, which is the same number the run lease already uses to decide
that a session's owner is gone (:data:`steward.run_lifecycle.RUN_LEASE_GRACE_S`). One
notion of "the holder is dead" rather than two.

**A reclaimed holder cannot undo the reclaim.** Every write is fenced by the token minted
when the claim was taken, so a process that was declared dead and came back cannot renew or
release the claim that replaced it — the same trick ``owner_token`` plays for run closes.

**The lease is measured on wall clocks, and they have to agree.** Every process writes its
own ``now`` and judges staleness against its own ``now``, so a clock more than
:data:`CLAIM_GRACE_S` ahead of its neighbours could declare a live holder dead. That is the
same assumption ``close_stale_run`` and the watchdog already make about the same file, and
the same answer: the burrows steward runs on keep their clocks in step.

**A claim steward cannot take is not a session steward refuses.** An unreachable database
is a warning and the caller runs, exactly as an unwritable run registry is
(:meth:`steward.scheduler.Scheduler._open_run`: "a lost row is not a lost run"). The claim
exists to prevent a real overlap, not to become a new way for the whole fleet to stop
firing. The budget refuses on an unreadable read and this does not, because a budget is an
authorization and this is bookkeeping.

The seam is one call, and it is all a fourth firing process needs::

    with claims.hold(resident_id, kind="chat", ref=conversation_id) as claim:
        if isinstance(claim, ClaimRefused):
            ...  # say why, and open no session
        else:
            ...  # this process is the resident's one live session
"""

import logging
import os
import socket
import threading
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Protocol

from steward.events import utc_now_iso
from steward.run_lifecycle import RUN_HEARTBEAT_EVERY_S, RUN_LEASE_GRACE_S

__all__ = [
    "CLAIM_GRACE_S",
    "CLAIM_HEARTBEAT_EVERY_S",
    "ONE_SESSION_PER_RESIDENT",
    "ClaimHeld",
    "ClaimRefused",
    "ClaimStore",
    "ResidentClaim",
    "ResidentClaims",
    "stale_before",
    "this_process",
]

log = logging.getLogger("steward.claims")

#: How long a claim survives without a heartbeat before another process may take it.
#: Deliberately the run lease's number: steward already decides an owner is gone when its
#: run heartbeat is this old, and a second, different answer to the same question would be
#: a second clock to keep in sync.
CLAIM_GRACE_S = RUN_LEASE_GRACE_S

#: How often a holder says it is still here. The run lease's cadence, for the same reason:
#: eight beats inside one grace window means a claim survives a slow database, a starved
#: thread, or a long pause without its holder being declared dead.
CLAIM_HEARTBEAT_EVERY_S = RUN_HEARTBEAT_EVERY_S

#: The rule itself, in one clause, so the scheduler's log line, the API's 409 and anything
#: chat writes are all quoting the same sentence rather than three paraphrases of it.
ONE_SESSION_PER_RESIDENT = "steward runs one session per resident at a time"


def stale_before(now: datetime | None = None, *, grace_s: float = CLAIM_GRACE_S) -> str:
    """Return the cutoff a holder's heartbeat must be newer than to still hold the claim.

    The one definition of "this holder is gone", so a reader — ``steward doctor``, say —
    judges a claim by exactly the arithmetic the writer used to defend it.
    """
    return utc_now_iso((now or datetime.now(UTC)) - timedelta(seconds=grace_s))


def this_process() -> str:
    """Name the process a claim is held by, so a refusal can say who is holding it."""
    try:
        host = socket.gethostname() or "?"
    except OSError:  # pragma: no cover — a host with no name is not a real deployment
        host = "?"
    return f"{host}:{os.getpid()}"


@dataclass(frozen=True, slots=True)
class ResidentClaim:
    """One resident's live-session claim, exactly as the database holds it.

    A row is never deleted, only released and overwritten, so the last thing that ran for a
    resident stays readable — which is what lets a refusal name it.
    """

    resident_id: str
    token: str
    holder: str
    claimed_at: str
    heartbeat_at: str
    kind: str = ""
    ref: str = ""
    run_id: str = ""
    released_at: str | None = None

    def live_at(self, stale_before: str) -> bool:
        """Say whether this claim still holds against a staleness cutoff."""
        return self.released_at is None and self.heartbeat_at > stale_before

    def describe(self) -> str:
        """Name what is running, in the words a refusal is written in.

        Every part is optional except the holder and the moment, because a caller that knows
        only "this resident is busy" must still produce a sentence — a refusal that named
        nothing would send an operator to the logs to find out what it meant.
        """
        kind = self.kind or "steward"
        subject = f"{kind} {self.ref!r}" if self.ref else f"a {kind} session"
        run = f" (run {self.run_id})" if self.run_id else ""
        return f"{subject}{run} held by {self.holder} since {self.claimed_at}"


class ClaimStore(Protocol):
    """The conditional writes a cross-process claim is made of.

    A structural protocol rather than an import of :class:`steward.store.Store`, for the
    usual reason in this codebase and one extra: ``Store`` imports the scheduler and the
    scheduler needs this module. What satisfies it is that ``Store``.
    """

    def claim_resident(  # noqa: PLR0913 — one parameter per fact the claim records
        self,
        resident_id: str,
        *,
        token: str,
        holder: str = "",
        kind: str = "",
        ref: str = "",
        run_id: str = "",
        stale_before: str,
        now: str | None = None,
    ) -> ResidentClaim | None:
        """Take a resident's claim, or return ``None`` and leave the live holder alone."""
        ...

    def renew_resident_claim(self, resident_id: str, *, token: str, now: str | None = None) -> bool:
        """Say this holder is still here. ``False`` means the claim is no longer its."""
        ...

    def release_resident_claim(
        self, resident_id: str, *, token: str, now: str | None = None
    ) -> bool:
        """Give the claim back. ``False`` means somebody else already holds it."""
        ...

    def resident_claim(self, resident_id: str) -> ResidentClaim | None:
        """Return the last claim recorded for a resident, live or not."""
        ...


@dataclass(frozen=True, slots=True)
class ClaimHeld:
    """This process may open the resident's session for as long as the block runs.

    ``claim`` is ``None`` in exactly one case: the store could not be written, so the
    session runs *unclaimed* — held by nothing, protected by nothing, and logged as such.
    """

    claim: ResidentClaim | None = None


@dataclass(frozen=True, slots=True)
class ClaimRefused:
    """Somebody else is already running this resident, and here is who.

    ``holder`` is ``None`` when the row could not be read back after the claim was lost —
    rare, and still an honest refusal: what is being reported is that this process did not
    get the claim.
    """

    resident_id: str
    reason: str
    holder: ResidentClaim | None = None


class ResidentClaims:
    """Takes, keeps, and gives back the one-session-per-resident claim.

    Non-blocking on purpose. Every caller of this seam has somewhere better to be than
    queued behind a fifteen-minute session: a scheduled fire that waited would stop being
    the occurrence it claims to be, a board dispatch that waited would stall the tick it
    runs on, and an HTTP request that waited would time out. So a contended claim is
    *refused*, with a reason naming the holder, and each caller decides what that costs —
    a skipped fire, a skipped resident, a 409.
    """

    def __init__(
        self,
        store: ClaimStore,
        *,
        clock: Callable[[], datetime] | None = None,
        grace_s: float = CLAIM_GRACE_S,
        heartbeat_every_s: float = CLAIM_HEARTBEAT_EVERY_S,
        holder: str | None = None,
    ) -> None:
        """Bind the claim to one durable store and one process identity."""
        self.store = store
        # The wall clock by default, and it has to be: the fact a claim asserts is "a
        # process is alive right now", and every other process reads it against its own
        # now. An injected clock is for tests, which own both sides of the comparison.
        self.clock = clock or (lambda: datetime.now(UTC))
        self.grace_s = grace_s
        self.heartbeat_every_s = heartbeat_every_s
        self.holder_name = holder if holder is not None else this_process()

    def holder(self, resident_id: str) -> ResidentClaim | None:
        """Return the claim currently holding this resident, or ``None`` if it is free.

        The read-only half of the seam, for a surface that wants to refuse *before* it
        accepts — the API answers a run-now with a 409 rather than a 202 it will later
        record as skipped. Advisory by nature: only :meth:`hold` settles the race, exactly
        as ``guard.allow`` at a route is advisory and admission is what settles that one.
        """
        try:
            claim = self.store.resident_claim(resident_id)
        except Exception as exc:  # noqa: BLE001 — an unreadable claim is not a held one
            log.warning("%s: could not read the session claim: %s", resident_id, exc)
            return None
        return claim if claim is not None and claim.live_at(self._stale_before()) else None

    @contextmanager
    def hold(
        self, resident_id: str, *, kind: str = "", ref: str = "", run_id: str = ""
    ) -> Iterator[ClaimHeld | ClaimRefused]:
        """Hold this resident's claim for the block, or yield why it could not be held.

        ``kind`` and ``ref`` are description, not protocol: they are what the refusal on
        the other side gets to name. A chat daemon passes ``kind="chat"`` and its
        conversation id and needs nothing added anywhere.
        """
        token = str(uuid.uuid4())
        taken = self._take(resident_id, token=token, kind=kind, ref=ref, run_id=run_id)
        if isinstance(taken, ClaimRefused):
            yield taken  # somebody else has this resident; nothing was written to give back
            return
        if taken.claim is None:
            yield taken  # the store could not be written, so there is no claim to keep alive
            return
        stop = threading.Event()
        thread = threading.Thread(
            target=self._beat,
            args=(resident_id, token, stop),
            name=f"steward-claim-{resident_id}",
            daemon=True,
        )
        # Inside the ``try`` from here on. A ``start()`` that raised — thread exhaustion is
        # the realistic one — outside it would leave a row nothing ever releases, and the
        # resident refused for a whole grace window over an error nobody could see.
        try:
            thread.start()
            yield taken
        finally:
            stop.set()
            if thread.ident is not None:  # a thread that never started cannot be joined
                thread.join(timeout=self.heartbeat_every_s)
            self._release(resident_id, token)

    # -- the three writes ----------------------------------------------------------------

    def _take(
        self, resident_id: str, *, token: str, kind: str, ref: str, run_id: str
    ) -> ClaimHeld | ClaimRefused:
        """Win the claim, lose it to a live holder, or fail open on a broken store."""
        try:
            claim = self.store.claim_resident(
                resident_id,
                token=token,
                holder=self.holder_name,
                kind=kind,
                ref=ref,
                run_id=run_id,
                stale_before=self._stale_before(),
                now=utc_now_iso(self.clock()),
            )
        except Exception as exc:  # noqa: BLE001 — see the module docstring: this fails open
            log.warning(
                "%s: could not take the session claim, so this session runs unclaimed "
                "and could overlap another process: %s",
                resident_id,
                exc,
            )
            return ClaimHeld()
        return ClaimHeld(claim) if claim is not None else self._refusal(resident_id)

    def _refusal(self, resident_id: str) -> ClaimRefused:
        """Build the refusal, naming whoever holds the resident if it can be read."""
        holder = self.holder(resident_id)
        held = f" — {holder.describe()}" if holder is not None else ""
        return ClaimRefused(
            resident_id=resident_id,
            holder=holder,
            reason=(
                f"{resident_id} already has a live session{held}; {ONE_SESSION_PER_RESIDENT} "
                "and skips the overlap rather than queueing it"
            ),
        )

    def _beat(self, resident_id: str, token: str, stop: threading.Event) -> None:
        """Say this process is still here, until the block ends or the claim is lost.

        A renew that comes back ``False`` means one of two very different things, and the
        difference is exactly whether ``stop`` is set. Unset, the claim was reclaimed under a
        running session and somebody should hear about it. Set, the block has already ended
        and :meth:`_release` has run — a beat that lost a race with its own shutdown, which
        is not news. Reporting that as a reclaim would be steward describing work that is not
        happening, which is the one thing this codebase refuses to do.
        """
        while not stop.wait(self.heartbeat_every_s):
            try:
                if not self.store.renew_resident_claim(
                    resident_id, token=token, now=utc_now_iso(self.clock())
                ):
                    if not stop.is_set():
                        log.warning(
                            "%s: this session no longer holds the resident claim; another "
                            "process reclaimed it after the heartbeat went stale",
                            resident_id,
                        )
                    return
            except Exception as exc:  # noqa: BLE001 — a heartbeat must not kill the session
                log.warning("%s: could not renew the session claim: %s", resident_id, exc)

    def _release(self, resident_id: str, token: str) -> None:
        """Give the claim back. Never raises: the session is over either way."""
        try:
            released = self.store.release_resident_claim(
                resident_id, token=token, now=utc_now_iso(self.clock())
            )
        except Exception as exc:  # noqa: BLE001 — an unreachable store is not a failed run
            log.warning("%s: could not release the session claim: %s", resident_id, exc)
            return
        if not released:
            log.warning(
                "%s: the claim on %r was taken by another process while this session was "
                "still running — it ran past its lease, or the clock moved",
                resident_id,
                resident_id,
            )

    # -- helpers -------------------------------------------------------------------------

    def _stale_before(self) -> str:
        """Return this instance's staleness cutoff, on its own clock and its own grace."""
        return stale_before(self.clock(), grace_s=self.grace_s)
