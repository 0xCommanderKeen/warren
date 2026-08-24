"""Strict Burrow protocol-v0 validation.

This module is the server-side adapter for the contract documented in
``docs/protocol.md``.  The browser implements the same small interface as
``validateEvent`` and both adapters are exercised by one fixture matrix.
"""
import datetime
import re


EVENT_TYPES = frozenset({
    "task_started", "tool_called", "tool_failed", "artifact_produced",
    "heartbeat", "needs_human", "idle", "session_ended",
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
    for field in _OPTIONAL_TEXT:
        if field in payload and not isinstance(payload[field], str):
            return f"invalid payload.{field}"
    if "stop_hook_active" in payload and type(payload["stop_hook_active"]) is not bool:
        return "invalid payload.stop_hook_active"
    return None


def is_event(event):
    return validate_event(event) is None
