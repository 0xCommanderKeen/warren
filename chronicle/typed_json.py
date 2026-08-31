"""Exact identity for JSON values: canonical escaping and a typed binary64 graph.

Nothing here knows about approvals, events or HTTP.  The module answers one
question — *are these two JSON values the same value?* — in a domain where
``json.dumps`` is not an answer: it renders ``1`` and ``1.0`` differently while
treating ``0.1 + 0.2`` and ``0.30000000000000004`` as the same text, it leans on
dict ordering, and its escaping varies with keyword arguments.  Here numbers are
compared as the exact IEEE-754 binary64 the browser and the server both hold,
object keys are sorted, and every string is escaped one way.

**The public interface**

- ``canonical_string(value)`` — a JSON string literal, ASCII-only, one spelling.
- ``typed_graph(value)`` / ``decode_graph(graph)`` — a value as a flat, tagged,
  child-before-parent node list, and back.
- ``semantic_key(value)`` — ``typed_graph`` rendered to one comparable string.
- ``freeze_json(value)`` / ``thaw_json(value)`` — detached immutable containers,
  and ordinary ones again for an I/O boundary.

**The byte obligation**

This encoding is a wire and storage format, not an implementation detail, so the
bytes are part of the interface:

``retention._encode_mood_authority`` concatenates ``canonical_string`` and
``semantic_key`` output into a ``typed-binary64-v1`` capsule, ``event_log``
writes that line into the rotated log and its archives, and the next boot reads
it back through ``decode_graph``.  A capsule that fails to decode is dropped
silently, taking Mood authority history with it — so a change to the key sort
order or to a binary64 token orphans records already on disk, with no error to
notice.  Every other caller compares values in memory and is free to change.

Until 2026-08 the format also had to match a JavaScript twin, ``typed-json.js``,
byte for byte.  That file and the whole browser viewer were deleted in
warren#219, so the obligation is now to *stored data* rather than to a second
implementation; the surviving parity vectors in
``tests/fixtures/mood-capsule-parity.json`` are re-pointed at this codec alone.
``tests/test_typed_json.py`` pins the format with golden strings — control
characters, non-BMP surrogate pairs, binary64 tokens and a mood-authority
record.  Changing one of those is a log-format migration, not a refactor.

Durable *notification* identity is a separate encoder living in
``notification_persistence``; it depends on the approval shape, not on these
bytes.  Do not assume a change here is safe there, or the reverse.

**The hardening**

``decode_graph`` takes bytes off disk and, historically, off the wire, so it
validates before it restores: no forward or repeated references, no unsorted or
duplicated object keys, no non-canonical binary64 token, no orphan nodes, and a
final re-encode that must reproduce the input exactly.  Both directions run
iteratively — approval detail thousands of levels deep must not exhaust the
recursion stack in a server that has to stay up.
"""

from collections.abc import Mapping
import math
import re
import struct
from types import MappingProxyType


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
            [
                item[key]
                for key in sorted(item, key=lambda key: canonical_string(str(key)))
            ]
            if isinstance(item, Mapping)
            else list(item)
        )
        stack.extend((child, False) for child in reversed(children))
    return results[id(value)]


def freeze_json(value):
    """Return an immutable, iteratively detached representation of JSON data."""
    return _transform_json(value, True)


def thaw_json(value):
    """Return ordinary JSON containers for serialization at an I/O boundary."""
    return _transform_json(value, False)


def canonical_string(value):
    """Return ``value`` as a canonical compact ASCII JSON string literal.

    One spelling per string: short escapes for the five that have them, literal
    printable ASCII, and lowercase ``\\uXXXX`` for everything else — including
    DEL and every non-BMP character, which is emitted as its UTF-16 surrogate
    pair.  Unpaired surrogates pass through as their own code unit rather than
    raising, because Python strings decoded with ``surrogatepass`` carry them.
    """
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


def typed_graph(value):
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
            sorted(item, key=lambda key: canonical_string(str(key)))
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
            return canonical_string(value)
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
                "[" + canonical_string(key) + "," + str(index) + "]"
                for key, index in node[1]
            ]
            encoded_nodes.append('["o",[' + ",".join(entries) + "]]")
    return "[[" + ",".join(encoded_nodes) + "]," + str(root) + "]"


def decode_graph(graph):
    """Validate and iteratively restore a value produced by ``typed_graph``."""
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
                        and canonical_string(prior) >= canonical_string(entry[0])
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
    if _graph_json(typed_graph(value)) != _graph_json(graph):
        raise ValueError("noncanonical typed JSON graph")
    return value


def semantic_key(value):
    """Hashable exact identity in the browser's IEEE-754 consumer domain."""
    return _graph_json(typed_graph(value))
