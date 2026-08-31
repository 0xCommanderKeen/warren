"""Shared, side-effect-free classification for structured approval knocks.

The server has two consumers of this wire shape: log rotation and durable
notification identity.  Keeping classification here prevents either consumer
from accepting a request that the other one treats as a legacy knock.
"""

from collections.abc import Mapping
from dataclasses import dataclass
import math
import re
import struct
from types import MappingProxyType


APPROVAL_DECISIONS = frozenset(("approve", "deny", "edit"))
_ACTION = re.compile(r"[a-z0-9][a-z0-9_-]*\Z")


def _transform_json(value, freeze):
    """Transform JSON containers without consuming Python's recursion stack."""
    results = {}
    seen = set()
    active = set()
    stack = [(value, False)]
    while stack:
        item, exiting = stack.pop()
        if not isinstance(item, (Mapping, list, tuple)):
            results[id(item)] = item
            continue
        identity = id(item)
        if exiting:
            active.remove(identity)
            if isinstance(item, Mapping):
                mapped = {key: results[id(child)] for key, child in item.items()}
                results[identity] = MappingProxyType(mapped) if freeze else mapped
            else:
                values = [results[id(child)] for child in item]
                results[identity] = tuple(values) if freeze else values
            continue
        if identity in seen:
            raise ValueError(
                "cyclic JSON value" if identity in active else "aliased JSON value"
            )
        seen.add(identity)
        active.add(identity)
        stack.append((item, True))
        children = (
            [item[key] for key in sorted(item, key=lambda key: _string_token(str(key)))]
            if isinstance(item, Mapping)
            else list(item)
        )
        stack.extend((child, False) for child in reversed(children))
    return results[id(value)]


def _freeze_json(value):
    """Return an immutable, iteratively detached representation of JSON data."""
    return _transform_json(value, True)


def thaw_json(value):
    """Return ordinary JSON containers for serialization at an I/O boundary."""
    return _transform_json(value, False)


def _string_token(value):
    encoded = ['"']
    short = {8: "\\b", 9: "\\t", 10: "\\n", 12: "\\f", 13: "\\r"}
    for character in value:
        codepoint = ord(character)
        units = (
            [codepoint]
            if codepoint <= 0xFFFF
            else [
                0xD800 + ((codepoint - 0x10000) >> 10),
                0xDC00 + ((codepoint - 0x10000) & 0x3FF),
            ]
        )
        for code in units:
            if code == 34:
                encoded.append('\\"')
            elif code == 92:
                encoded.append("\\\\")
            elif code in short:
                encoded.append(short[code])
            elif 0x20 <= code <= 0x7E:
                encoded.append(chr(code))
            else:
                encoded.append(f"\\u{code:04x}")
    return "".join(encoded) + '"'


def json_typed_graph(value):
    """Flat, unambiguous JSON AST with exact normalized binary64 numbers.

    Children always precede their parent, so both encoding and decoding remain
    iterative even when valid public approval detail is thousands of levels deep.
    """
    nodes = []
    seen = set()
    active = set()
    root = [None]
    stack = [(value, root, 0, False, None, None)]
    while stack:
        item, target, target_index, exiting, keys, child_indexes = stack.pop()
        container = isinstance(item, (Mapping, list, tuple))
        if not container:
            if item is None:
                node = ["n"]
            elif type(item) is bool:
                node = ["b", 1 if item else 0]
            elif type(item) in (int, float):
                try:
                    number = float(item)
                except (OverflowError, ValueError):
                    number = math.inf
                bits = (
                    (
                        "0000000000000000"
                        if number == 0
                        else struct.pack(">d", number).hex()
                    )
                    if math.isfinite(number)
                    else "nonfinite"
                )
                node = ["f", bits]
            elif isinstance(item, str):
                node = ["s", item]
            else:
                node = ["x"]
            nodes.append(node)
            target[target_index] = len(nodes) - 1
            continue
        identity = id(item)
        if exiting:
            active.remove(identity)
            if isinstance(item, Mapping):
                node = [
                    "o",
                    [
                        [str(key), child_indexes[index]]
                        for index, key in enumerate(keys)
                    ],
                ]
            else:
                node = ["a", child_indexes]
            nodes.append(node)
            target[target_index] = len(nodes) - 1
            continue
        if identity in seen:
            raise ValueError(
                "cyclic JSON value" if identity in active else "aliased JSON value"
            )
        seen.add(identity)
        keys = (
            sorted(item, key=lambda key: _string_token(str(key)))
            if isinstance(item, Mapping)
            else None
        )
        children = (
            [item[key] for key in keys] if isinstance(item, Mapping) else list(item)
        )
        child_indexes = [None] * len(children)
        active.add(identity)
        stack.append((item, target, target_index, True, keys, child_indexes))
        for index in range(len(children) - 1, -1, -1):
            stack.append((children[index], child_indexes, index, False, None, None))
    return [nodes, root[0]]


def _graph_json(graph):
    """Canonical shallow JSON for a typed graph."""

    def scalar(value):
        if isinstance(value, str):
            return _string_token(value)
        return str(value)

    nodes, root = graph
    encoded_nodes = []
    for node in nodes:
        tag = node[0]
        if tag in {"n", "x"}:
            encoded_nodes.append('["' + tag + '"]')
        elif tag in {"b", "f", "s"}:
            encoded_nodes.append('["' + tag + '",' + scalar(node[1]) + "]")
        elif tag == "a":
            encoded_nodes.append('["a",[' + ",".join(map(str, node[1])) + "]]")
        else:
            entries = [
                "[" + _string_token(key) + "," + str(index) + "]"
                for key, index in node[1]
            ]
            encoded_nodes.append('["o",[' + ",".join(entries) + "]]")
    return "[[" + ",".join(encoded_nodes) + "]," + str(root) + "]"


def decode_json_typed_graph(graph):
    """Validate and iteratively restore a value produced by ``json_typed_graph``."""
    if (
        not isinstance(graph, list)
        or len(graph) != 2
        or not isinstance(graph[0], list)
        or type(graph[1]) is not int
    ):
        raise ValueError("invalid typed JSON graph")
    values = []
    references = [0] * len(graph[0])
    for position, node in enumerate(graph[0]):
        if not isinstance(node, list) or not node or not isinstance(node[0], str):
            raise ValueError("invalid typed JSON node")
        tag = node[0]
        if tag == "n" and len(node) == 1:
            value = None
        elif (
            tag == "b" and len(node) == 2 and type(node[1]) is int and node[1] in (0, 1)
        ):
            value = bool(node[1])
        elif tag == "s" and len(node) == 2 and isinstance(node[1], str):
            value = node[1]
        elif tag == "f" and len(node) == 2 and isinstance(node[1], str):
            token = node[1]
            if token == "nonfinite":
                value = math.inf
            else:
                if re.fullmatch(r"[0-9a-f]{16}", token) is None:
                    raise ValueError("invalid binary64 token")
                value = struct.unpack(">d", bytes.fromhex(token))[0]
                if not math.isfinite(value) or (
                    value == 0 and token != "0000000000000000"
                ):
                    raise ValueError("noncanonical binary64 token")
                if value.is_integer() and abs(value) <= 9007199254740991:
                    value = int(value)
        elif tag == "a" and len(node) == 2 and isinstance(node[1], list):
            if any(
                type(index) is not int or not 0 <= index < position for index in node[1]
            ):
                raise ValueError("invalid typed JSON array reference")
            for index in node[1]:
                references[index] += 1
            value = [values[index] for index in node[1]]
        elif tag == "o" and len(node) == 2 and isinstance(node[1], list):
            value = {}
            prior = None
            for entry in node[1]:
                if (
                    not isinstance(entry, list)
                    or len(entry) != 2
                    or not isinstance(entry[0], str)
                    or type(entry[1]) is not int
                    or not 0 <= entry[1] < position
                    or (
                        prior is not None
                        and _string_token(prior) >= _string_token(entry[0])
                    )
                ):
                    raise ValueError("invalid typed JSON object entry")
                references[entry[1]] += 1
                value[entry[0]] = values[entry[1]]
                prior = entry[0]
        else:
            raise ValueError("invalid typed JSON node")
        values.append(value)
    if graph[1] < 0 or graph[1] >= len(values) or graph[1] != len(values) - 1:
        raise ValueError("invalid typed JSON root")
    if any(
        count != (0 if index == graph[1] else 1)
        for index, count in enumerate(references)
    ):
        raise ValueError("noncanonical typed JSON tree")
    value = values[graph[1]]
    if _graph_json(json_typed_graph(value)) != _graph_json(graph):
        raise ValueError("noncanonical typed JSON graph")
    return value


def json_semantic_key(value):
    """Hashable exact identity in the browser's IEEE-754 consumer domain."""
    return _graph_json(json_typed_graph(value))


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
            "malformed",
            reason="structured knock action must be a lowercase action slug",
        )
    if not isinstance(request_id, str) or not request_id.strip():
        return ApprovalClassification(
            "malformed", reason="structured knock has no request_id"
        )
    if "detail" not in payload or not (
        payload["detail"] is None or isinstance(payload["detail"], dict)
    ):
        return ApprovalClassification(
            "malformed", reason="structured knock detail must be an object or null"
        )
    if not isinstance(options, list) or not options:
        return ApprovalClassification(
            "malformed", reason="structured knock options must be non-empty"
        )
    if any(
        not isinstance(option, str) or option not in APPROVAL_DECISIONS
        for option in options
    ):
        return ApprovalClassification(
            "malformed",
            reason="structured knock options must be approve, deny, or edit values",
        )
    return ApprovalClassification(
        "structured",
        StructuredApproval(
            request_id=request_id,
            action=action,
            detail=_freeze_json(payload["detail"]),
            options=tuple(options),
            message_present="message" in payload,
            message=_freeze_json(payload.get("message")),
            expires_at_present="expires_at" in payload,
            expires_at=_freeze_json(payload.get("expires_at")),
        ),
    )


def structured_approval(event):
    """Return the immutable structured shape, or ``None`` for every other kind."""
    classified = classify_approval(event)
    return classified.shape if classified.kind == "structured" else None
