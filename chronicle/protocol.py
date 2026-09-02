"""Strict Chronicle protocol-v0 validation.

This module is the server-side adapter for the contract documented in
``docs/protocol.md``.  The browser implements the same small interface as
``validateEvent`` and both adapters are exercised by one fixture matrix.
"""

import datetime
import math
import re

from journal_observations import validate_journal_event


EVENT_TYPES = frozenset(
    {
        "task_started",
        "tool_called",
        "tool_failed",
        "artifact_produced",
        "heartbeat",
        "needs_human",
        "idle",
        "session_ended",
        "routine_started",
        "routine_finished",
        "routine_failed",
        "task_posted",
        "task_claimed",
        "task_done",
        "task_failed",
        "task_session_finished",
        "task_delegated",
        "needs_human_resolved",
        "resident_restarted",
        "chat_message_dropped",
        "journal_written",
    }
)
_TIMESTAMP = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$")
_REQUIRED_TEXT = {
    "task_started": ("prompt",),
    "tool_called": ("tool",),
    "tool_failed": ("tool",),
    "artifact_produced": ("artifact",),
    "needs_human": ("message",),
}
_OPTIONAL_TEXT = frozenset(
    {
        "prompt",
        "tool",
        "artifact",
        "message",
        "detail",
        "error",
        "phase",
        "turn_id",
        "agent_type",
        "parent_agent_id",
    }
)


def _nonempty_text(value):
    return isinstance(value, str) and bool(value.strip())


def _artifacts_error(payload):
    """Evidence of what a finished piece of work left behind, or nothing it can mean."""
    artifacts = payload.get("artifacts")
    if not isinstance(artifacts, list) or not all(
        _nonempty_text(item) for item in artifacts
    ):
        return "invalid payload.artifacts"
    return None


def _duration_error(payload):
    """How long a run took: a real, finite, non-negative number of seconds."""
    duration = payload.get("duration_s")
    if (
        type(duration) not in (int, float)
        or not math.isfinite(duration)
        or duration < 0
    ):
        return "invalid payload.duration_s"
    return None


def _validate_routine(event):
    event_type, payload = event["type"], event["payload"]
    for field in ("routine", "run_id"):
        if not _nonempty_text(payload.get(field)):
            return f"invalid payload.{field}"
    if event_type == "routine_started":
        if payload.get("trigger") not in ("manual", "schedule"):
            return "invalid payload.trigger"
    elif event_type == "routine_finished":
        if not _nonempty_text(payload.get("outcome")):
            return "invalid payload.outcome"
        return _artifacts_error(payload) or _duration_error(payload)
    elif event_type == "routine_failed":
        if not _nonempty_text(payload.get("error")):
            return "invalid payload.error"
        # A watchdog closing a run nobody watched knows no duration and says so by
        # leaving the field out; a present one is still held to the same shape.
        if "duration_s" in payload:
            return _duration_error(payload)
    return None


def _validate_task(event):
    event_type, payload, agent_id = event["type"], event["payload"], event["agent_id"]
    for field in ("task_id", "title"):
        if not _nonempty_text(payload.get(field)):
            return f"invalid payload.{field}"
    if event_type == "task_posted":
        if not _nonempty_text(payload.get("posted_by")):
            return "invalid payload.posted_by"
        skills = payload.get("required_skills")
        if not isinstance(skills, list) or not all(
            isinstance(item, str) for item in skills
        ):
            return "invalid payload.required_skills"
    else:
        if not _nonempty_text(payload.get("claimant")):
            return "invalid payload.claimant"
        if payload["claimant"] != agent_id:
            return "payload.claimant must match agent_id"
    if "parent_task_id" in payload and not _nonempty_text(payload["parent_task_id"]):
        return "invalid payload.parent_task_id"
    if event_type == "task_done":
        return _artifacts_error(payload)
    if event_type == "task_failed" and not _nonempty_text(payload.get("reason")):
        return "invalid payload.reason"
    return None


def _validate_approval_resolution(event):
    payload = event["payload"]
    for field in ("request_id", "decided_by", "action"):
        if not _nonempty_text(payload.get(field)):
            return f"invalid payload.{field}"
    if payload.get("decision") not in ("approve", "deny", "edit"):
        return "invalid payload.decision"
    return None


def _validate_delegation(event):
    """A handoff names both ends: who carried it, who it is for, and through which door."""
    payload, agent_id = event["payload"], event["agent_id"]
    for field in ("task_id", "title", "from", "to", "route"):
        if not _nonempty_text(payload.get(field)):
            return f"invalid payload.{field}"
    if payload["from"] != agent_id:
        return "payload.from must match agent_id"
    # Unlike the job facts this one always carries the field: a handoff that starts a
    # chain says so with an explicit null rather than by leaving the field out.
    parent = payload.get("parent_task_id")
    if parent is not None and not _nonempty_text(parent):
        return "invalid payload.parent_task_id"
    if type(payload.get("depth")) is not int or payload["depth"] < 0:
        return "invalid payload.depth"
    return None


def _validate_session_report(event):
    """A run that reported back after losing its claim, as strict as the close it is not."""
    payload, agent_id = event["payload"], event["agent_id"]
    for field in ("task_id", "title", "claimant", "run_id", "outcome", "reason"):
        if not _nonempty_text(payload.get(field)):
            return f"invalid payload.{field}"
    if payload["claimant"] != agent_id:
        return "payload.claimant must match agent_id"
    return _artifacts_error(payload) or _duration_error(payload)


def _validate_resident_restart(event):
    """Steward took a resident down and brought it back, and counted the attempt."""
    payload = event["payload"]
    if not _nonempty_text(payload.get("reason")):
        return "invalid payload.reason"
    attempt = payload.get("attempt")
    # Attempts are counted from one within a bounded budget, so a crash loop reads as a
    # crash loop rather than as unrelated hiccups; a zeroth attempt never happened.
    if type(attempt) is not int or attempt < 1:
        return "invalid payload.attempt"
    if "supervisor" in payload and not _nonempty_text(payload["supervisor"]):
        return "invalid payload.supervisor"
    return None


def _validate_chat_drop(event):
    """Somebody knocked on a resident's chat route and was deliberately not answered.

    The payload carries the door and who knocked, never what they said: a stranger's text
    is the one string here written by somebody the fleet has no relationship with, and
    the village renders what it is given.

    ``suppressed`` is the one number an outsider's volume decides, so it is checked as
    strictly as anything else here. Steward records one knock per stranger per door per
    window and counts the rest into it (warren#278), so a record stands for ``1 +
    suppressed`` knocks. Optional, because a Steward older than the limiter emits every
    knock and counts none — and a missing count reads as the zero that means the same
    thing.
    """
    payload = event["payload"]
    for field in ("route", "address", "from", "reason"):
        if not _nonempty_text(payload.get(field)):
            return f"invalid payload.{field}"
    if "suppressed" in payload:
        suppressed = payload["suppressed"]
        if type(suppressed) is not int or suppressed < 0:
            return "invalid payload.suppressed"
    return None


_ROUTINE_AUTHORITY = "routine events require source steward"
_TASK_AUTHORITY = "task events require source steward"
_APPROVAL_AUTHORITY = "approval resolutions require source steward"
_RESIDENT_AUTHORITY = "resident lifecycle events require source steward"
_CHAT_AUTHORITY = "chat events require source steward"

#: Every fact only Steward can witness, because only Steward runs the routines, the board,
#: the watchdog and the chat routes. One table so the trust boundary is a list somebody can
#: read: each type names the payload rule it must satisfy and the error a forgery gets.
_STEWARD_AUTHORED = {
    "routine_started": (_validate_routine, _ROUTINE_AUTHORITY),
    "routine_finished": (_validate_routine, _ROUTINE_AUTHORITY),
    "routine_failed": (_validate_routine, _ROUTINE_AUTHORITY),
    "task_posted": (_validate_task, _TASK_AUTHORITY),
    "task_claimed": (_validate_task, _TASK_AUTHORITY),
    "task_done": (_validate_task, _TASK_AUTHORITY),
    "task_failed": (_validate_task, _TASK_AUTHORITY),
    "task_delegated": (_validate_delegation, _TASK_AUTHORITY),
    "task_session_finished": (_validate_session_report, _TASK_AUTHORITY),
    "needs_human_resolved": (_validate_approval_resolution, _APPROVAL_AUTHORITY),
    "resident_restarted": (_validate_resident_restart, _RESIDENT_AUTHORITY),
    "chat_message_dropped": (_validate_chat_drop, _CHAT_AUTHORITY),
}


def _plain_object(value):
    return isinstance(value, dict)


def validate_event(event):
    """Return ``None`` for a valid v0 event, otherwise a stable error string."""
    if not _plain_object(event):
        return "event must be an object"
    if type(event.get("v")) is not int or event["v"] != 0:
        return "unsupported protocol version"
    ts = event.get("ts")
    if not isinstance(ts, str) or not _TIMESTAMP.fullmatch(ts):
        return "invalid timestamp"
    try:
        datetime.datetime.strptime(ts, "%Y-%m-%dT%H:%M:%S.%fZ")
    except ValueError:
        return "invalid timestamp"
    for field in ("source", "agent_id", "project"):
        if not isinstance(event.get(field), str) or not event[field].strip():
            return f"invalid {field}"
    if "cwd" in event and not isinstance(event["cwd"], str):
        return "invalid cwd"
    event_type = event.get("type")
    if not isinstance(event_type, str) or event_type not in EVENT_TYPES:
        return "unsupported event type"
    payload = event.get("payload")
    if not _plain_object(payload):
        return "payload must be an object"
    for field in _REQUIRED_TEXT.get(event_type, ()):
        if not isinstance(payload.get(field), str) or not payload[field].strip():
            return f"invalid payload.{field}"
    steward_authored = _STEWARD_AUTHORED.get(event_type)
    if steward_authored:
        validator, authority = steward_authored
        error = validator(event)
        if error:
            return error
        if event["source"] != "steward":
            return authority
    if event_type == "journal_written":
        error = validate_journal_event(event)
        if error:
            return error
    for field in _OPTIONAL_TEXT:
        # Structured approvals add an object-valued detail to the legacy
        # string-valued knock. Shape validation remains a projection concern so
        # a malformed structured attempt can degrade to the plain knock.
        if (
            field in payload
            and not isinstance(payload[field], str)
            and not (event_type == "needs_human" and field == "detail")
        ):
            return f"invalid payload.{field}"
    if "stop_hook_active" in payload and type(payload["stop_hook_active"]) is not bool:
        return "invalid payload.stop_hook_active"
    return None


def is_event(event):
    return validate_event(event) is None
