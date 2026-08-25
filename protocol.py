"""Strict Burrow protocol-v0 validation.

This module is the server-side adapter for the contract documented in
``docs/protocol.md``.  The browser implements the same small interface as
``validateEvent`` and both adapters are exercised by one fixture matrix.
"""
import datetime
import math
import re


EVENT_TYPES = frozenset({
    "task_started", "tool_called", "tool_failed", "artifact_produced",
    "heartbeat", "needs_human", "idle", "session_ended",
    "routine_started", "routine_finished", "routine_failed",
    "task_posted", "task_claimed", "task_done", "task_failed",
    "needs_human_resolved",
})
_TIMESTAMP = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$")
_REQUIRED_TEXT = {
    "task_started": ("prompt",),
    "tool_called": ("tool",),
    "tool_failed": ("tool",),
    "artifact_produced": ("artifact",),
    "needs_human": ("message",),
}
_OPTIONAL_TEXT = frozenset({
    "prompt", "tool", "artifact", "message", "detail", "error", "phase",
    "turn_id", "agent_type", "parent_agent_id",
})


def _nonempty_text(value):
    return isinstance(value, str) and bool(value.strip())


def _validate_routine_payload(event_type, payload):
    for field in ("routine", "run_id"):
        if not _nonempty_text(payload.get(field)):
            return f"invalid payload.{field}"
    if event_type == "routine_started":
        if payload.get("trigger") not in ("manual", "schedule"):
            return "invalid payload.trigger"
    elif event_type == "routine_finished":
        if not _nonempty_text(payload.get("outcome")):
            return "invalid payload.outcome"
        artifacts = payload.get("artifacts")
        if not isinstance(artifacts, list) or not all(_nonempty_text(item) for item in artifacts):
            return "invalid payload.artifacts"
        duration = payload.get("duration_s")
        if (type(duration) not in (int, float) or not math.isfinite(duration)
                or duration < 0):
            return "invalid payload.duration_s"
    elif event_type == "routine_failed":
        if not _nonempty_text(payload.get("error")):
            return "invalid payload.error"
        if "duration_s" in payload:
            duration = payload["duration_s"]
            if (type(duration) not in (int, float) or not math.isfinite(duration)
                    or duration < 0):
                return "invalid payload.duration_s"
    return None


def _validate_task_payload(event_type, payload, agent_id):
    for field in ("task_id", "title"):
        if not _nonempty_text(payload.get(field)):
            return f"invalid payload.{field}"
    if event_type == "task_posted":
        if not _nonempty_text(payload.get("posted_by")):
            return "invalid payload.posted_by"
        skills = payload.get("required_skills")
        if not isinstance(skills, list) or not all(isinstance(item, str) for item in skills):
            return "invalid payload.required_skills"
    else:
        if not _nonempty_text(payload.get("claimant")):
            return "invalid payload.claimant"
        if payload["claimant"] != agent_id:
            return "payload.claimant must match agent_id"
    if "parent_task_id" in payload and not _nonempty_text(payload["parent_task_id"]):
        return "invalid payload.parent_task_id"
    if event_type == "task_done":
        artifacts = payload.get("artifacts")
        if not isinstance(artifacts, list) or not all(_nonempty_text(item) for item in artifacts):
            return "invalid payload.artifacts"
    if event_type == "task_failed" and not _nonempty_text(payload.get("reason")):
        return "invalid payload.reason"
    return None


def _validate_approval_resolution(payload):
    for field in ("request_id", "decided_by", "action"):
        if not _nonempty_text(payload.get(field)):
            return f"invalid payload.{field}"
    if payload.get("decision") not in ("approve", "deny", "edit"):
        return "invalid payload.decision"
    return None


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
    if event_type.startswith("routine_"):
        error = _validate_routine_payload(event_type, payload)
        if error:
            return error
        if event["source"] != "steward":
            return "routine events require source steward"
    if event_type in ("task_posted", "task_claimed", "task_done", "task_failed"):
        error = _validate_task_payload(event_type, payload, event["agent_id"])
        if error:
            return error
        if event["source"] != "steward":
            return "task events require source steward"
    if event_type == "needs_human_resolved":
        error = _validate_approval_resolution(payload)
        if error:
            return error
        if event["source"] != "steward":
            return "approval resolutions require source steward"
    for field in _OPTIONAL_TEXT:
        # Structured approvals add an object-valued detail to the legacy
        # string-valued knock. Shape validation remains a projection concern so
        # a malformed structured attempt can degrade to the plain knock.
        if (field in payload and not isinstance(payload[field], str)
                and not (event_type == "needs_human" and field == "detail")):
            return f"invalid payload.{field}"
    if "stop_hook_active" in payload and type(payload["stop_hook_active"]) is not bool:
        return "invalid payload.stop_hook_active"
    return None


def is_event(event):
    return validate_event(event) is None
