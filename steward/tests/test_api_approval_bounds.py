"""API behavior: approval bounds."""

import asyncio
import json
from typing import Any

import pytest
from starlette.types import Message, Receive, Scope, Send

from steward.api import (
    _ApprovalBodyDepthMiddleware,
)
from steward.input_bounds import (
    APPROVAL_BODY_MAX_BYTES,
    EDIT_MAX_BYTES,
    EDIT_MAX_CONTAINER_ITEMS,
    EDIT_MAX_DEPTH,
    EDIT_MAX_KEY_CHARS,
    EDIT_MAX_STRING_CHARS,
)
from steward.runners import Outcome, RunRequest, RunResult
from support.api import (
    TOKEN,
    ApiFactory,
    _pending,
)
from support.api import (
    api as api,  # noqa: PLC0414 — pytest fixture discovery
)


def _nested_edit(depth: int) -> dict[str, Any]:
    value: dict[str, Any] = {"leaf": "ok"}
    for _ in range(depth - 1):
        value = {"next": value}
    return value


def test_edit_accepts_every_structural_boundary(api: ApiFactory) -> None:
    harness = api()
    request_id = _pending(harness)
    edit = _nested_edit(EDIT_MAX_DEPTH)
    edit["members"] = {str(i): i for i in range(EDIT_MAX_CONTAINER_ITEMS)}
    edit["long-key" * 0 + "k" * EDIT_MAX_KEY_CHARS] = "x" * EDIT_MAX_STRING_CHARS
    response = harness.client.post(
        f"/approvals/{request_id}", json={"decision": "edit", "edit": edit}
    )
    assert response.status_code == 202


@pytest.mark.parametrize(
    "edit",
    [
        pytest.param(_nested_edit(EDIT_MAX_DEPTH + 1), id="depth"),
        pytest.param({str(i): i for i in range(EDIT_MAX_CONTAINER_ITEMS + 1)}, id="object-members"),
        pytest.param({"items": list(range(EDIT_MAX_CONTAINER_ITEMS + 1))}, id="array-items"),
        pytest.param({"k" * (EDIT_MAX_KEY_CHARS + 1): "x"}, id="key"),
        pytest.param({"text": "x" * (EDIT_MAX_STRING_CHARS + 1)}, id="string"),
        pytest.param({"emoji": "🦉" * 5_000}, id="multibyte-serialized-bytes"),
    ],
)
def test_invalid_edits_are_422_before_write_event_or_prompt(
    api: ApiFactory, edit: dict[str, Any]
) -> None:
    prompts: list[str] = []

    def record(request: RunRequest) -> RunResult:
        prompts.append(request.prompt)
        return RunResult(outcome=Outcome.OK, output="done")

    harness = api(behavior=record)
    request_id = _pending(harness)
    response = harness.client.post(
        f"/approvals/{request_id}", json={"decision": "edit", "edit": edit}
    )
    assert response.status_code == 422
    record_after = harness.store.approval(request_id)
    assert record_after is not None
    assert record_after.pending
    assert record_after.edit is None
    assert harness.store.export_request_history() == []
    assert harness.events() == []
    harness.client.post("/residents/test-agent/routines/daily-summary/run")
    harness.settle()
    assert prompts
    assert "the human edited it to" not in prompts[0]


def test_edit_serialized_byte_boundary_is_exact(api: ApiFactory) -> None:
    # Compact JSON overhead for these three keys is 22 bytes.
    edit = {"a": "x" * 8_000, "b": "x" * 8_000, "c": "x" * (EDIT_MAX_BYTES - 16_022)}
    assert (
        len(json.dumps(edit, ensure_ascii=False, separators=(",", ":")).encode()) == EDIT_MAX_BYTES
    )
    harness = api()
    request_id = _pending(harness)
    assert (
        harness.client.post(
            f"/approvals/{request_id}", json={"decision": "edit", "edit": edit}
        ).status_code
        == 202
    )


def test_wire_cap_accepts_near_maximum_edit_with_ascii_content_unicode_escaped(
    api: ApiFactory,
) -> None:
    # Every content byte is legally written as a six-byte escape. This is the wire
    # expansion that a ratio based only on non-ASCII UTF-8 misses.
    edit = {"a": "a" * 8_000, "b": "a" * 8_000, "c": "a" * (EDIT_MAX_BYTES - 16_022)}
    compact = json.dumps(edit, ensure_ascii=False, separators=(",", ":"))
    assert len(compact.encode()) == EDIT_MAX_BYTES
    escaped_edit = compact.replace("a", r"\u0061")
    raw = f'{{"decision":"edit","edit":{escaped_edit}}}'.encode()
    assert 98_000 < len(raw) <= APPROVAL_BODY_MAX_BYTES

    harness = api()
    request_id = _pending(harness)
    response = harness.client.post(
        f"/approvals/{request_id}", content=raw, headers={"content-type": "application/json"}
    )
    assert response.status_code == 202
    assert harness.store.approval(request_id).edit == edit  # ty: ignore[unresolved-attribute]


def test_unicode_escaped_wire_form_does_not_bypass_semantic_edit_limit(api: ApiFactory) -> None:
    edit = {"a": "a" * 8_000, "b": "a" * 8_000, "c": "a" * (EDIT_MAX_BYTES - 16_021)}
    compact = json.dumps(edit, ensure_ascii=False, separators=(",", ":"))
    assert len(compact.encode()) == EDIT_MAX_BYTES + 1
    escaped_edit = compact.replace("a", r"\u0061")
    raw = f'{{"decision":"edit","edit":{escaped_edit}}}'.encode()
    assert len(raw) <= APPROVAL_BODY_MAX_BYTES

    harness = api()
    request_id = _pending(harness)
    response = harness.client.post(
        f"/approvals/{request_id}", content=raw, headers={"content-type": "application/json"}
    )
    assert response.status_code == 422
    assert harness.store.approval(request_id).pending  # ty: ignore[unresolved-attribute]


@pytest.mark.parametrize(
    ("character", "counts"),
    [
        pytest.param("\u0080", (8_000, 184), id="bmp"),
        pytest.param("🦉", (4_092, 0), id="surrogate-pair"),
    ],
)
@pytest.mark.parametrize("wire_encoding", ["utf8", "ascii"])
def test_wire_cap_accepts_equivalent_near_maximum_utf8_edits(
    api: ApiFactory, wire_encoding: str, character: str, counts: tuple[int, int]
) -> None:
    # U+0080 is the worst JSON escaping ratio: two compact UTF-8 bytes become six ASCII
    # bytes; astral characters use two \uXXXX escapes. Both edits are one byte below
    # the semantic serialized limit in compact UTF-8.
    edit = {"a": character * counts[0], "b": character * counts[1]}
    assert len(json.dumps(edit, ensure_ascii=False, separators=(",", ":")).encode()) == 16_383
    raw = json.dumps(
        {"decision": "edit", "edit": edit},
        ensure_ascii=wire_encoding == "ascii",
        separators=(",", ":"),
    ).encode()
    harness = api()
    request_id = _pending(harness)
    response = harness.client.post(
        f"/approvals/{request_id}", content=raw, headers={"content-type": "application/json"}
    )
    assert response.status_code == 202


def test_approval_wire_body_boundary_and_oversize_inputs_have_no_effect(api: ApiFactory) -> None:
    harness = api()
    request_id = _pending(harness)
    prefix = b'{"decision":"approve","padding":"'
    suffix = b'"}'
    exact = prefix + b" " * (APPROVAL_BODY_MAX_BYTES - len(prefix) - len(suffix)) + suffix
    assert len(exact) == APPROVAL_BODY_MAX_BYTES
    assert (
        harness.client.post(
            f"/approvals/{request_id}", content=exact, headers={"content-type": "application/json"}
        ).status_code
        == 422
    )

    for raw in (b" " * (APPROVAL_BODY_MAX_BYTES + 1), b'"' + b"x" * APPROVAL_BODY_MAX_BYTES):
        response = harness.client.post(
            f"/approvals/{request_id}", content=raw, headers={"content-type": "application/json"}
        )
        assert response.status_code == 413
        assert response.json()["detail"]["error"] == "approval_body_too_large"
    assert harness.store.approval(request_id).pending  # ty: ignore[unresolved-attribute]
    assert harness.store.export_request_history() == []
    assert harness.events() == []


def test_approval_wire_limit_stops_receiving_at_first_oversize_chunk() -> None:
    received = 0
    downstream: list[bytes] = []
    messages = iter(
        [
            {"type": "http.request", "body": b"x" * APPROVAL_BODY_MAX_BYTES, "more_body": True},
            {"type": "http.request", "body": b"!", "more_body": True},
            {"type": "http.request", "body": b"never-read", "more_body": False},
        ]
    )

    async def receive() -> Message:
        nonlocal received
        received += 1
        return next(messages)

    async def app(_scope: Scope, receive: Receive, _send: Send) -> None:
        downstream.append((await receive())["body"])

    sent: list[Message] = []

    async def send(message: Message) -> None:
        sent.append(message)

    scope: Scope = {
        "type": "http",
        "method": "POST",
        "path": "/approvals/id",
        "headers": [(b"authorization", f"Bearer {TOKEN}".encode())],
    }
    asyncio.run(_ApprovalBodyDepthMiddleware(app, token=TOKEN)(scope, receive, send))
    assert received == 2
    assert downstream == []
    assert sent[0]["status"] == 413


def test_approval_wire_limit_accepts_exact_streaming_boundary() -> None:
    chunks = [b"x" * (APPROVAL_BODY_MAX_BYTES - 1), b"!"]
    messages = iter(
        [
            {"type": "http.request", "body": chunks[0], "more_body": True},
            {"type": "http.request", "body": chunks[1], "more_body": False},
        ]
    )
    downstream: list[Message] = []

    async def receive() -> Message:
        return next(messages)

    async def app(_scope: Scope, receive: Receive, _send: Send) -> None:
        downstream.append(await receive())

    async def send(_message: Message) -> None:
        pass

    scope: Scope = {
        "type": "http",
        "method": "POST",
        "path": "/approvals/id",
        "headers": [(b"authorization", f"Bearer {TOKEN}".encode())],
    }
    asyncio.run(_ApprovalBodyDepthMiddleware(app, token=TOKEN)(scope, receive, send))
    assert downstream == [{"type": "http.request", "body": b"".join(chunks), "more_body": False}]


def test_approval_middleware_replays_normal_chunks_then_preserves_disconnect() -> None:
    messages = iter(
        [
            {"type": "http.request", "body": b'{"decision":', "more_body": True},
            {"type": "http.request", "body": b'"approve"}', "more_body": False},
            {"type": "http.disconnect"},
        ]
    )
    downstream: list[Message] = []

    async def receive() -> Message:
        return next(messages)

    async def app(_scope: Scope, receive: Receive, _send: Send) -> None:
        downstream.append(await receive())
        downstream.append(await receive())

    async def send(_message: Message) -> None:
        pass

    scope: Scope = {
        "type": "http",
        "method": "POST",
        "path": "/approvals/id",
        "headers": [(b"authorization", f"Bearer {TOKEN}".encode())],
    }
    asyncio.run(_ApprovalBodyDepthMiddleware(app, token=TOKEN)(scope, receive, send))
    assert downstream == [
        {"type": "http.request", "body": b'{"decision":"approve"}', "more_body": False},
        {"type": "http.disconnect"},
    ]


def test_approval_middleware_replays_partial_body_then_disconnect_without_waiting() -> None:
    messages = iter(
        [
            {"type": "http.request", "body": b'{"decision":', "more_body": True},
            {"type": "http.disconnect"},
        ]
    )
    downstream: list[Message] = []

    async def receive() -> Message:
        return next(messages)

    async def app(_scope: Scope, receive: Receive, _send: Send) -> None:
        downstream.append(await receive())
        downstream.append(await receive())

    async def send(_message: Message) -> None:
        pass

    scope: Scope = {
        "type": "http",
        "method": "POST",
        "path": "/approvals/id",
        "headers": [(b"authorization", f"Bearer {TOKEN}".encode())],
    }

    async def exercise() -> None:
        await asyncio.wait_for(
            _ApprovalBodyDepthMiddleware(app, token=TOKEN)(scope, receive, send), timeout=0.1
        )

    asyncio.run(exercise())
    assert downstream == [
        {"type": "http.request", "body": b'{"decision":', "more_body": True},
        {"type": "http.disconnect"},
    ]


def test_deep_raw_approval_json_is_422_before_materialization(api: ApiFactory) -> None:
    prompts: list[str] = []

    def record(request: RunRequest) -> RunResult:
        prompts.append(request.prompt)
        return RunResult(outcome=Outcome.OK, output="done")

    harness = api(behavior=record)
    request_id = _pending(harness)
    raw = '{"decision":"edit","edit":' + "[" * 1_100 + "0" + "]" * 1_100 + "}"
    response = harness.client.post(
        f"/approvals/{request_id}", content=raw, headers={"content-type": "application/json"}
    )
    assert response.status_code == 422
    assert response.json()["detail"][0]["loc"] == ["body", "edit"]
    assert harness.store.approval(request_id).pending  # ty: ignore[unresolved-attribute]
    assert harness.store.export_request_history() == []
    assert harness.events() == []
    harness.client.post("/residents/test-agent/routines/daily-summary/run")
    harness.settle()
    assert prompts
    assert "the human edited it to" not in prompts[0]


def test_raw_depth_guard_is_quote_escape_and_utf8_aware(api: ApiFactory) -> None:
    harness = api()
    request_id = _pending(harness)
    edit = _nested_edit(EDIT_MAX_DEPTH)
    edit["text"] = '🦉 braces { [ and an escaped quote: " still text } ]'
    raw = json.dumps({"decision": "edit", "edit": edit}, ensure_ascii=False)
    response = harness.client.post(
        f"/approvals/{request_id}",
        content=raw.encode(),
        headers={"content-type": "application/json"},
    )
    assert response.status_code == 202


@pytest.mark.parametrize("number", ["NaN", "Infinity", "-Infinity"])
def test_non_finite_api_edit_numbers_are_422_without_effect(api: ApiFactory, number: str) -> None:
    harness = api()
    request_id = _pending(harness)
    raw = f'{{"decision":"edit","edit":{{"value":{number}}}}}'
    response = harness.client.post(
        f"/approvals/{request_id}", content=raw, headers={"content-type": "application/json"}
    )
    assert response.status_code == 422
    assert harness.store.approval(request_id).pending  # ty: ignore[unresolved-attribute]
    assert harness.events() == []


@pytest.mark.parametrize("raw", ["", '{"decision":'])
def test_raw_guard_leaves_empty_and_malformed_json_to_standard_validation(
    api: ApiFactory, raw: str
) -> None:
    harness = api()
    request_id = _pending(harness)
    response = harness.client.post(
        f"/approvals/{request_id}", content=raw, headers={"content-type": "application/json"}
    )
    assert response.status_code == 422
    assert harness.store.approval(request_id).pending  # ty: ignore[unresolved-attribute]


def test_raw_guard_preserves_json_content_types_and_duplicate_key_semantics(
    api: ApiFactory,
) -> None:
    harness = api()
    request_id = _pending(harness)
    response = harness.client.post(
        f"/approvals/{request_id}",
        content='{"decision":"deny","decision":"approve"}',
        headers={"content-type": "application/vnd.api+json"},
    )
    assert response.status_code == 202
    assert harness.store.approval(request_id).decision == "approve"  # ty: ignore[unresolved-attribute]


def test_unauthorized_deep_approval_body_still_uses_the_auth_error(api: ApiFactory) -> None:
    harness = api()
    request_id = _pending(harness)
    raw = '{"decision":"edit","edit":' + "[" * 1_100 + "0" + "]" * 1_100 + "}"
    response = harness.client.post(
        f"/approvals/{request_id}",
        content=raw,
        headers={"content-type": "application/json", "authorization": "Bearer wrong"},
    )
    assert response.status_code == 401
    assert response.json()["detail"]["error"] == "unauthorized"
