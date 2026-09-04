"""Task-ledger retention: which board rows survive rotation, and their ends.

The bottom of the selector graph — ``event_ms`` lives here because every other
family orders by it. The timestamp-plus-tie order itself is
``village_state.later_task_event``, shared with the projection rather than
mirrored (warren#285).
"""

import datetime
import functools

from protocol import validate_event
from retention_policy import KEEP_TASKS
from village_state import (
    TASK_LEDGER_TYPES,
    TASK_ORIGIN_TYPES,
    later_task_event,
    reopened_by_lease,
)


def event_ms(event):
    """Return event time in epoch ms, or zero when missing or invalid."""
    ts = event.get("ts")
    if not isinstance(ts, str):
        return 0
    try:
        when = datetime.datetime.fromisoformat(ts.strip().replace("Z", "+00:00"))
    except ValueError:
        return 0
    if when.tzinfo is None:
        when = when.replace(tzinfo=datetime.timezone.utc)
    return int(when.timestamp() * 1000)


def _later_task_event(candidate, current):
    return later_task_event(candidate, current)


def _remember_task_event(task, slot, index, event):
    """Mirror the browser's constant-space timestamp/tie-order fold."""
    current = task.get(slot)
    if current is None or _later_task_event(event, current[1]):
        task[slot] = (index, event)


def _closed_row(transition):
    """Whether the board shows this row finished — the newest transition decides.

    Not the newest *event*: a replayed post is newer than the ``task_done`` before it
    and closes nothing, so reading the row's standing off the newest event of any kind
    would call a finished job unfinished and spend capacity keeping it (warren#282).
    """
    if transition is None:
        return False
    event = transition[1]
    return event["type"] == "task_done" or (
        event["type"] == "task_failed" and not reopened_by_lease(event["payload"])
    )


def _ledger_events(parsed):
    """Yield every valid board event with the row identity it belongs to."""
    for index, event in parsed:
        if event.get("type") not in TASK_LEDGER_TYPES or validate_event(event):
            continue
        yield index, event, event["payload"]["task_id"].strip()


def _task_keep_indexes(parsed):
    """Return the bounded cross-agent task projection needed after rotation.

    A task begins under ``steward:api`` — or, when it was handed to somebody
    rather than posted, under the villager that handed it over — and moves to its
    claimant.  It therefore cannot share villager lifecycle retention: a
    claimant's ``session_ended`` must not erase a done/failed/reopened task or
    resurrect the older event that opened its row.
    """

    def compare(left, right):
        left_id, left_task = left
        right_id, right_task = right
        left_event, right_event = left_task["latest"][1], right_task["latest"][1]
        left_terminal = _closed_row(left_task["transition"])
        right_terminal = _closed_row(right_task["transition"])
        if left_terminal != right_terminal:
            return 1 if left_terminal else -1
        if event_ms(left_event) != event_ms(right_event):
            return -1 if event_ms(left_event) > event_ms(right_event) else 1
        if left_id == right_id:
            return 0
        return -1 if left_id > right_id else 1

    tasks = {}
    for index, event, task_id in _ledger_events(parsed):
        task = tasks.setdefault(
            task_id, {"origin": None, "transition": None, "latest": None}
        )
        if event["type"] in TASK_ORIGIN_TYPES:
            _remember_task_event(task, "origin", index, event)
        else:
            _remember_task_event(task, "transition", index, event)
        _remember_task_event(task, "latest", index, event)
        if len(tasks) > KEEP_TASKS:
            # Mirror the browser after every accepted event. An origin evicted at
            # capacity cannot be silently reconstructed merely because its
            # later claim appears in the same transport/reset batch.
            selected_ids = {
                key
                for key, _ in sorted(tasks.items(), key=functools.cmp_to_key(compare))[
                    :KEEP_TASKS
                ]
            }
            tasks = {key: value for key, value in tasks.items() if key in selected_ids}

    selected = sorted(tasks.items(), key=functools.cmp_to_key(compare))[:KEEP_TASKS]
    keep = set()
    for _, task in selected:
        # Both ends, tracked apart. Reading the second end off "the newest event"
        # loses the claim as soon as a replayed origin is newer than it, and the row
        # then rotates into a job nobody has taken (warren#282).
        for slot in ("origin", "transition"):
            if task[slot] is not None:
                keep.add(task[slot][0])
    return keep


def _paired_transition_keep_indexes(parsed, keep):
    """Return the newest transition owed to every already-retained row origin.

    ``_task_keep_indexes`` pairs the two ends of every row it selects, but it is
    not the only reason a line survives rotation: a ``task_delegated`` is also the
    delegator's own activity, so the villager, journal and mood selectors retain
    handoffs on their own terms — a row the ledger dropped at capacity included.
    An origin retained without its newest transition is worse than a dropped row,
    because the board then shows claimed or finished work as open and unclaimed.

    This pass needs no budget of its own: it adds at most one line per origin
    another selector already chose to keep, so it inherits whatever bound that
    selector applied.
    """
    origins = {
        task_id
        for index, event, task_id in _ledger_events(parsed)
        if index in keep and event["type"] in TASK_ORIGIN_TYPES
    }
    if not origins:
        return set()
    latest = {}
    for index, event, task_id in _ledger_events(parsed):
        # Transitions only. A row whose origin was restated has a *newer* origin than
        # its claim, and pairing that with itself retains nothing — the row rotates
        # into an open job all over again (warren#282).
        if task_id in origins and event["type"] not in TASK_ORIGIN_TYPES:
            _remember_task_event(latest, task_id, index, event)
    return {item[0] for item in latest.values()} - keep
