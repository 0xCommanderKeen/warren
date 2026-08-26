"""Named transitions: one durable state change, and the burrow fact that says it happened.

Callers used to know the choreography. Posting a task meant writing a row, then building
the right event, then emitting it; claiming meant interpreting a ``rowcount``, then
choosing an identity and a project, then emitting under the claimant rather than under
steward; closing meant deriving a status and a reason, checking a lease token, and knowing
that a lost lease must emit *nothing*. That knowledge was repeated for posting, claiming,
finishing, failing, lease expiry, delegation, approval creation, resolution and expiry,
and budget pause and resume — and a single caller getting one branch wrong is either work
the village cannot see or work the village renders that never happened.

This package puts each of those acts behind one named domain module:

- :mod:`steward.transitions.task` — post, claim, take delivery, finish, expire leases
- :mod:`steward.transitions.approval` — raise, decide, expire, harvest
- :mod:`steward.transitions.delegation` — deliver an accepted handoff
- :mod:`steward.transitions.budget` — pause, resume

They sit **above** :mod:`steward.store` and :mod:`steward.events` and change neither. The
store still owns SQL and record mapping; the emitter still owns the POST, the circuit
breaker, and the local fallback log. There is no event bus here, no outbox, no callback
out of the store, and no retry policy: a transition orders two independent systems and
refuses to lie about the second one.

What every method returns is a :class:`steward.transitions.outcome.Transition` — the
durable record, which of six named outcomes happened, and the fact that was handed over.
The outcomes are the vocabulary this refactor is really about: *applied*, *refused*,
*replayed*, *expired*, *superseded*, *answered*. Only the first ever emits.

**Every owner builds its own seam, and that is deliberate.** A ``Watchdog`` pass ends up
holding three or four ``ApprovalTransitions`` over the same ``(store, emitter)`` — its own,
its dispatcher's, its budget guard's, and the delegator's — which looks like waste and is
not. These classes are frozen value dataclasses over exactly those two fields: they hold no
connection, no cursor, no buffer and no state between calls, so two of them are the same
seam in every sense a caller can observe. What building one per owner buys is that "which
emitter did that fact go to" is answered by the owner's own two fields rather than by
tracing an injected object back through however many constructors passed it along — and a
test handing one owner a ``NullEmitter`` cannot accidentally silence another.

``docs/transitions.md`` holds the full matrix: every transition, its guard, its outcomes,
its event payload, and what stays outside these modules.
"""

from steward.transitions.approval import ApprovalTransitions
from steward.transitions.budget import BudgetTransitions
from steward.transitions.delegation import DelegationTransitions
from steward.transitions.outcome import (
    ANSWERED,
    APPLIED,
    EXPIRED,
    OUTCOMES,
    REFUSED,
    REPLAYED,
    SUPERSEDED,
    Transition,
)
from steward.transitions.task import LEASE_EXPIRED, TaskTransitions

__all__ = [
    "ANSWERED",
    "APPLIED",
    "EXPIRED",
    "LEASE_EXPIRED",
    "OUTCOMES",
    "REFUSED",
    "REPLAYED",
    "SUPERSEDED",
    "ApprovalTransitions",
    "BudgetTransitions",
    "DelegationTransitions",
    "TaskTransitions",
    "Transition",
]
