"""Shared bounds for human-supplied work before it becomes durable or prompt text."""

import json
from collections.abc import Mapping
from typing import Any

TITLE_MAX_CHARS = 200
DETAIL_MAX_CHARS = 8_000
IDENTIFIER_MAX_CHARS = 100
SKILLS_MAX_ITEMS = 100

EDIT_MAX_BYTES = 16_384
EDIT_MAX_DEPTH = 8
EDIT_MAX_CONTAINER_ITEMS = 100
EDIT_MAX_NODES = 1_000
EDIT_MAX_STRING_CHARS = 8_000
EDIT_MAX_KEY_CHARS = 200


def _mapping_children(value: Mapping[object, object], depth: int) -> list[tuple[object, int]]:
    """Validate one object and return its children."""
    if depth > EDIT_MAX_DEPTH:
        raise ValueError(f"edit exceeds the {EDIT_MAX_DEPTH} level nesting limit")
    if len(value) > EDIT_MAX_CONTAINER_ITEMS:
        raise ValueError(f"edit object exceeds the {EDIT_MAX_CONTAINER_ITEMS} member limit")
    for key in value:
        if not isinstance(key, str):
            raise TypeError("edit object keys must be strings")
        if len(key) > EDIT_MAX_KEY_CHARS:
            raise ValueError(f"edit key exceeds the {EDIT_MAX_KEY_CHARS} character limit")
    return [(child, depth + 1) for child in value.values()]


def _bounded_children(value: object, depth: int) -> list[tuple[object, int]]:
    """Validate one value and return children for the iterative edit walk."""
    if isinstance(value, Mapping):
        return _mapping_children(value, depth)
    if isinstance(value, list):
        if depth > EDIT_MAX_DEPTH:
            raise ValueError(f"edit exceeds the {EDIT_MAX_DEPTH} level nesting limit")
        if len(value) > EDIT_MAX_CONTAINER_ITEMS:
            raise ValueError(f"edit array exceeds the {EDIT_MAX_CONTAINER_ITEMS} item limit")
        return [(child, depth + 1) for child in value]
    if isinstance(value, str) and len(value) > EDIT_MAX_STRING_CHARS:
        raise ValueError(f"edit string exceeds the {EDIT_MAX_STRING_CHARS} character limit")
    return []


def validate_work_text(title: str, detail: str) -> None:
    """Refuse work text that would bypass the block grammar's durable limits."""
    if not title.strip():
        raise ValueError("title must not be empty")
    if len(title) > TITLE_MAX_CHARS:
        raise ValueError(f"title exceeds the {TITLE_MAX_CHARS} character limit")
    if len(detail) > DETAIL_MAX_CHARS:
        raise ValueError(f"detail exceeds the {DETAIL_MAX_CHARS} character limit")


def validate_identifier(value: str, name: str) -> None:
    """Bound a caller-controlled lookup key before it reaches matching or storage."""
    if not value.strip():
        raise ValueError(f"{name} must not be empty")
    if len(value) > IDENTIFIER_MAX_CHARS:
        raise ValueError(f"{name} exceeds the {IDENTIFIER_MAX_CHARS} character limit")


def validate_approval_edit(edit: Mapping[str, Any] | None) -> None:
    """Bound approval-edit JSON without recursively walking attacker-controlled input."""
    if edit is None:
        return
    nodes = 0
    stack: list[tuple[object, int]] = [(edit, 1)]
    while stack:
        value, depth = stack.pop()
        nodes += 1
        if nodes > EDIT_MAX_NODES:
            raise ValueError(f"edit exceeds the {EDIT_MAX_NODES} value limit")
        stack.extend(_bounded_children(value, depth))
    encoded = json.dumps(edit, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    if len(encoded) > EDIT_MAX_BYTES:
        raise ValueError(f"edit exceeds the {EDIT_MAX_BYTES} byte serialized limit")
