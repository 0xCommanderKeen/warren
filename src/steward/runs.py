"""Shared protocol vocabulary for persisted resident runs.

This module deliberately has no steward dependencies.  Both the scheduler, which emits
routine events, and the store, which persists runs, can therefore depend on one owner of
the values they must agree on.
"""

RUN_ROUTINE = "routine"
RUN_TASK = "task"
RUN_DELEGATED = "delegated"
RUN_KINDS = (RUN_ROUTINE, RUN_TASK, RUN_DELEGATED)

TRIGGER_SCHEDULE = "schedule"
TRIGGER_MANUAL = "manual"
ROUTINE_TRIGGERS = (TRIGGER_SCHEDULE, TRIGGER_MANUAL)
RUN_TRIGGERS = ("", *ROUTINE_TRIGGERS)


def validate_kind_trigger(kind: str, trigger: str) -> None:
    """Reject a run shape that cannot truthfully describe how that kind was started."""
    if kind not in RUN_KINDS:
        raise ValueError(f"invalid run kind: {kind!r}")
    if kind == RUN_ROUTINE:
        if trigger not in ROUTINE_TRIGGERS:
            raise ValueError(f"invalid trigger {trigger!r} for run kind {kind!r}")
    elif trigger != "":
        raise ValueError(f"invalid trigger {trigger!r} for run kind {kind!r}")
