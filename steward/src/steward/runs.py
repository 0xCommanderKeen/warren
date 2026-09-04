"""Shared protocol vocabulary for persisted resident runs.

This module deliberately has no steward dependencies.  Both the scheduler, which emits
routine events, and the store, which persists runs, can therefore depend on one owner of
the values they must agree on.
"""

from typing import Literal

RUN_ROUTINE = "routine"
RUN_TASK = "task"
RUN_DELEGATED = "delegated"
#: A session an operator's message woke (warren#108). Its own kind rather than a routine
#: with an unusual trigger: nothing in a manifest declares it, it answers a conversation
#: rather than a schedule, and the ledger has to be able to say which of the two a
#: resident spent its day on.
RUN_CHAT = "chat"
RUN_KINDS = (RUN_ROUTINE, RUN_TASK, RUN_DELEGATED, RUN_CHAT)


class AlreadyRunningError(Exception):
    """Raised when a resident is asked to run while one of its sessions is going."""

    def __init__(self, reason: str) -> None:
        """Carry the sentence this refusal is served with."""
        super().__init__(reason)
        self.reason = reason


TRIGGER_SCHEDULE = "schedule"
TRIGGER_MANUAL = "manual"
#: What started a chat session: a message, and there is only ever the one answer. Carried
#: anyway, because the ``routine_started`` event steward brackets every session with names
#: its trigger, and a chat wake-up reading ``schedule`` there would be a lie in the village.
TRIGGER_CHAT = "chat"
ROUTINE_TRIGGERS = (TRIGGER_SCHEDULE, TRIGGER_MANUAL)
CHAT_TRIGGERS = (TRIGGER_CHAT,)
RUN_TRIGGERS = ("", *ROUTINE_TRIGGERS, *CHAT_TRIGGERS)

#: The kinds steward brackets with the ``routine_*`` trio, and whose death is therefore the
#: watchdog's to mourn. A task's is not: a board claim has a lease behind it and the sweep
#: that reopens it owns that ending (:mod:`steward.watchdog`). A chat session has no lease
#: and nobody else watching, so it is bracketed and buried exactly like a routine fire.
ROUTINE_BRACKETED = (RUN_ROUTINE, RUN_CHAT)

#: Which triggers each kind may truthfully carry. A kind absent from here takes no trigger
#: at all — a board task is claimed rather than triggered, and saying otherwise would put a
#: word in the ledger that answers a question nobody asked.
_TRIGGERS_BY_KIND = {RUN_ROUTINE: ROUTINE_TRIGGERS, RUN_CHAT: CHAT_TRIGGERS}


def validate_kind_trigger(kind: str, trigger: str) -> None:
    """Reject a run shape that cannot truthfully describe how that kind was started."""
    if kind not in RUN_KINDS:
        raise ValueError(f"invalid run kind: {kind!r}")
    allowed = _TRIGGERS_BY_KIND.get(kind, ("",))
    if trigger not in allowed:
        raise ValueError(f"invalid trigger {trigger!r} for run kind {kind!r}")


#: What became of a finished routine's final message when its manifest said
#: ``deliver:`` (warren#385). Written on the run's row by the scheduler, beside the outcome
#: and never instead of it: a message that did not reach the phone is a delivery that
#: failed, not a routine that did. A run whose routine delivers nowhere carries none.
DeliveryStatus = Literal["delivered", "quiet", "delivery_failed"]
DELIVERED: DeliveryStatus = "delivered"
QUIET: DeliveryStatus = "quiet"
DELIVERY_FAILED: DeliveryStatus = "delivery_failed"
DELIVERY_STATUSES: tuple[DeliveryStatus, ...] = (DELIVERED, QUIET, DELIVERY_FAILED)
