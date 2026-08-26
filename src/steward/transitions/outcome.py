"""What a named transition did, and the one place a fact reaches an emitter.

A transition is a durable state change plus the burrow fact that says it happened. The
two are not one atomic write and cannot be: the store is SQLite and the emitter is an HTTP
POST to another process with a local JSONL fallback (:mod:`steward.events`). What *can* be
made true, and what this module makes structural, is narrower:

    a fact is handed to the emitter only on the branch where the durable change
    actually happened in this call, and it is handed over exactly once.

Note the direction. Every fact belongs to an applied transition; not every applied
transition has a fact. A budget resume answered through the API has none of its own — the
decision that triggered it already emitted ``needs_human_resolved``, and saying it twice
would put two answers in the log for one question.

:func:`applied` is the only function in :mod:`steward.transitions` that takes an emitter,
so it is the only place a fact can be sent from, and it sends nothing when there is
nothing to send. The other constructors do not receive an emitter — a refusal, a replay,
an expiry, a lost race and a self-answered request *cannot* emit, rather than merely not
doing so today. Grepping this package for a call to ``emit`` is expected to turn up
exactly one line, and a second one is the bug this shape exists to make visible.
"""

from dataclasses import dataclass

from steward import events as ev

__all__ = [
    "ANSWERED",
    "APPLIED",
    "EXPIRED",
    "OUTCOMES",
    "REFUSED",
    "REPLAYED",
    "SUPERSEDED",
    "Transition",
    "answered",
    "applied",
    "carried",
    "expired",
    "refused",
    "replayed",
    "superseded",
]

#: The durable change happened here, and its fact was handed to the emitter.
APPLIED = "applied"

#: A precondition said no before anything was written. Nothing written, nothing emitted.
REFUSED = "refused"

#: Already recorded. The recorded outcome is returned and nothing new is emitted — a
#: double-tapped notification and a retried request change nothing.
REPLAYED = "replayed"

#: Past its deadline, so deny-by-default keeps the last word. Nothing written, nothing
#: emitted; the sweep is what records the deny.
EXPIRED = "expired"

#: A conditional write lost to another writer — a race, or a lease that died under a
#: session. This call wrote nothing, so it emits nothing: a fact claiming a win that
#: another writer took is the one lie the pairing exists to prevent.
SUPERSEDED = "superseded"

#: A durable row was written and, deliberately, nobody was knocked on. Exactly one
#: transition ends this way — the repeat-deny guard in
#: :meth:`steward.transitions.approval.ApprovalTransitions.raise_request` — and it is
#: named rather than folded into :data:`APPLIED` so that "a durable change with no fact"
#: can never happen quietly anywhere else.
ANSWERED = "answered"

OUTCOMES = (APPLIED, REFUSED, REPLAYED, EXPIRED, SUPERSEDED, ANSWERED)


@dataclass(frozen=True, slots=True)
class Transition[RecordT]:
    """One named transition's result: what it did, what it wrote, and what it said.

    ``record`` is the durable row as the store returned it — the *new* row for an applied
    change, the already-recorded one for a replay, and ``None`` when there was nothing to
    act on at all.

    ``fact`` is the event that was handed to the emitter, and it is present only on an
    applied transition. It is returned so a test can assert the row and the event together
    without reaching into an emitter, and so a caller can name what was said without
    rebuilding it. It is **not** a delivery receipt: whether that event reached burrow or
    only the fallback log is the emitter's business, and steward never claims otherwise.
    """

    outcome: str
    record: RecordT | None = None
    fact: ev.Event | None = None
    reason: str = ""

    @property
    def applied(self) -> bool:
        """True when this call made the durable change and said so."""
        return self.outcome == APPLIED

    @property
    def refused(self) -> bool:
        """True when a precondition said no and nothing was written or emitted."""
        return self.outcome == REFUSED

    @property
    def replayed(self) -> bool:
        """True when this was already recorded and the recorded outcome came back."""
        return self.outcome == REPLAYED

    @property
    def expired(self) -> bool:
        """True when the deadline had already passed and deny-by-default keeps the word."""
        return self.outcome == EXPIRED

    @property
    def superseded(self) -> bool:
        """True when a conditional write lost to another writer."""
        return self.outcome == SUPERSEDED

    @property
    def answered(self) -> bool:
        """True when steward recorded the answer itself and knocked on nobody."""
        return self.outcome == ANSWERED

    @property
    def silent(self) -> bool:
        """True when this transition handed no fact to the emitter."""
        return self.fact is None

    def require(self) -> RecordT:
        """Return the durable record, refusing to guess when there is none.

        For the acts that always read a row back however they went — a pause that lost its
        conditional insert still comes back holding the pause that won. A caller reaching
        for a record a refusal never produced has a bug, and it should surface here rather
        than as a ``None`` threaded three call frames up.
        """
        if self.record is None:
            raise ValueError(f"a {self.outcome} transition has no durable record: {self.reason}")
        return self.record


def applied[RecordT](
    emitter: ev.Emitter, record: RecordT, fact: ev.Event | None = None, reason: str = ""
) -> Transition[RecordT]:
    """Hand one fact to the emitter and report the durable change it belongs to.

    The only function in this package that touches an emitter. Every transition that wins
    its write ends here, and every transition that does not cannot reach here, because the
    other constructors are not given an emitter to reach it with.

    ``fact`` is optional because a few durable changes genuinely have no protocol surface
    of their own — a budget resume the API already answered elsewhere. Passing ``None``
    emits nothing, which is the difference between "this act says nothing" and "somebody
    forgot to emit": the first is written down here, at the call site, in the branch that
    means it.

    Emitting never raises (:meth:`steward.events.EventEmitter.emit`), so an unreachable
    village never turns a completed transition into a failed one — the fact lands in the
    fallback log and the row stands either way.

    ``reason`` carries a durable detail the caller would otherwise re-derive — the failure
    line a closed task is recorded and reported under, say. It is the same string in the
    row and in the fact, which is the point of passing it back rather than rebuilding it.
    """
    if fact is not None:
        emitter.emit(fact)
    return Transition(APPLIED, record=record, fact=fact, reason=reason)


def carried[RecordT, InnerT](
    record: RecordT, inner: Transition[InnerT], reason: str = ""
) -> Transition[RecordT]:
    """Report a durable change whose fact was handed over by a transition it delegated to.

    A budget pause has no event of its own: what the village sees is the ``needs_human``
    the approval transition raised for it, and a resume from a terminal is visible as the
    ``needs_human_resolved`` that same approval transition emitted. The outer change still
    happened here, so the outcome is :data:`APPLIED` whatever the inner act came to — but
    the fact is the inner one's, named rather than rebuilt, so the two can never disagree
    and nobody is tempted to emit a second copy.
    """
    return Transition(APPLIED, record=record, fact=inner.fact, reason=reason or inner.reason)


def refused[RecordT](reason: str, record: RecordT | None = None) -> Transition[RecordT]:
    """Report a precondition that said no. Nothing was written and nothing was emitted."""
    return Transition(REFUSED, record=record, reason=reason)


def replayed[RecordT](record: RecordT, reason: str = "") -> Transition[RecordT]:
    """Report an act somebody already performed, carrying what was recorded then."""
    return Transition(REPLAYED, record=record, reason=reason)


def expired[RecordT](record: RecordT, reason: str = "") -> Transition[RecordT]:
    """Report a deadline that had already passed, leaving deny-by-default the last word."""
    return Transition(EXPIRED, record=record, reason=reason)


def superseded[RecordT](reason: str = "", record: RecordT | None = None) -> Transition[RecordT]:
    """Report a conditional write this call lost. Nothing written here, so nothing said."""
    return Transition(SUPERSEDED, record=record, reason=reason)


def answered[RecordT](record: RecordT, reason: str = "") -> Transition[RecordT]:
    """Report a row steward answered itself, on purpose, without knocking on anybody."""
    return Transition(ANSWERED, record=record, reason=reason)
