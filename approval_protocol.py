"""Shared, side-effect-free classification for structured approval knocks.

The server has two consumers of this wire shape: log rotation and durable
notification identity.  Keeping classification here prevents either consumer
from accepting a request that the other one treats as a legacy knock.
"""
from collections.abc import Mapping
from dataclasses import dataclass
import math
import re
from types import MappingProxyType


APPROVAL_DECISIONS = frozenset(("approve", "deny", "edit"))
_ACTION = re.compile(r"[a-z0-9][a-z0-9_-]*\Z")


def _freeze_json(value):
    """Return an immutable, recursively detached representation of JSON data."""
    if isinstance(value, dict):
        return MappingProxyType({key: _freeze_json(item)
                                 for key, item in value.items()})
    if isinstance(value, list):
        return tuple(_freeze_json(item) for item in value)
    return value


def thaw_json(value):
    """Return ordinary JSON containers for serialization at an I/O boundary."""
    if isinstance(value, Mapping):
        return {key: thaw_json(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [thaw_json(item) for item in value]
    return value


def json_semantic_key(value):
    """Hashable JSON equality in the browser's IEEE-754 consumer domain."""
    if value is None:
        return ("null",)
    if type(value) is bool:
        return ("bool", value)
    if type(value) in (int, float):
        try:
            number = float(value)
        except (OverflowError, ValueError):
            return ("invalid-number",)
        return (("number", 0.0 if number == 0 else number)
                if math.isfinite(number) else ("invalid-number",))
    if isinstance(value, str):
        return ("string", value)
    if isinstance(value, (list, tuple)):
        return ("array", tuple(json_semantic_key(item) for item in value))
    if isinstance(value, Mapping):
        return ("object", tuple(sorted(
            (str(key), json_semantic_key(item)) for key, item in value.items())))
    return ("invalid", type(value).__name__)


@dataclass(frozen=True, slots=True)
class StructuredApproval:
    """The complete immutable request shape shared by all Python consumers."""

    request_id: str
    action: str
    detail: object
    options: tuple[str, ...]
    message_present: bool
    message: object
    expires_at_present: bool
    expires_at: object

    def notification_shape(self):
        """Return the historical JSON shape used by durable identity v3."""
        return {
            "request_id": self.request_id,
            "action": self.action,
            "message": self.message if self.message_present else "",
            "detail": thaw_json(self.detail),
            "options": list(self.options),
            "expires_at": {
                "present": self.expires_at_present,
                "value": thaw_json(self.expires_at),
            },
        }


@dataclass(frozen=True, slots=True)
class ApprovalClassification:
    kind: str
    shape: StructuredApproval | None = None
    reason: str | None = None


def classify_approval(event):
    """Classify a knock as plain, malformed, structured, or unrelated.

    Once any structured field is attempted, all four core fields are required.
    ``detail`` must be present but may be JSON null. Repeated valid options are
    intentional wire data and retain their exact order.
    """
    if not isinstance(event, dict) or event.get("type") != "needs_human":
        return ApprovalClassification("other")
    payload = event.get("payload")
    if not isinstance(payload, dict):
        return ApprovalClassification("plain")
    fields = ("action", "detail", "options", "request_id")
    if not any(field in payload for field in fields):
        return ApprovalClassification("plain")
    action = payload.get("action")
    request_id = payload.get("request_id")
    options = payload.get("options")
    if not isinstance(action, str) or _ACTION.fullmatch(action) is None:
        return ApprovalClassification(
            "malformed", reason="structured knock action must be a lowercase action slug")
    if not isinstance(request_id, str) or not request_id.strip():
        return ApprovalClassification(
            "malformed", reason="structured knock has no request_id")
    if ("detail" not in payload or
            not (payload["detail"] is None or isinstance(payload["detail"], dict))):
        return ApprovalClassification(
            "malformed", reason="structured knock detail must be an object or null")
    if not isinstance(options, list) or not options:
        return ApprovalClassification(
            "malformed", reason="structured knock options must be non-empty")
    if any(not isinstance(option, str) or option not in APPROVAL_DECISIONS
           for option in options):
        return ApprovalClassification(
            "malformed",
            reason="structured knock options must be approve, deny, or edit values")
    return ApprovalClassification("structured", StructuredApproval(
        request_id=request_id,
        action=action,
        detail=_freeze_json(payload["detail"]),
        options=tuple(options),
        message_present="message" in payload,
        message=_freeze_json(payload.get("message")),
        expires_at_present="expires_at" in payload,
        expires_at=_freeze_json(payload.get("expires_at")),
    ))


def structured_approval(event):
    """Return the immutable structured shape, or ``None`` for every other kind."""
    classified = classify_approval(event)
    return classified.shape if classified.kind == "structured" else None
