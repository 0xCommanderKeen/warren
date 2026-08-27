"""Emitting: the shape burrow accepts, and what happens when burrow is not there."""

import json
import socket
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

from steward import events as ev


def context() -> ev.RunContext:
    return ev.RunContext(
        agent_id="claude-code:life-agent",
        project="life-agent",
        routine="daily-summary",
        run_id="0d1a…",
        cwd="/data/residents/life-agent/memory",
    )


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


# --------------------------------------------------------------------------- event shape


def test_a_routine_event_validates_against_the_protocol_shape() -> None:
    event = context().started("schedule")
    body = event.to_dict()
    assert ev.validate_event(body) == ()
    assert body["v"] == 0
    assert body["source"] == "steward"
    assert body["agent_id"] == "claude-code:life-agent"
    assert body["project"] == "life-agent"
    assert body["cwd"] == "/data/residents/life-agent/memory"
    assert body["type"] == "routine_started"
    assert body["payload"] == {
        "routine": "daily-summary",
        "run_id": "0d1a…",
        "trigger": "schedule",
    }


def test_the_timestamp_is_utc_with_milliseconds_and_a_z() -> None:
    ts = context().started("schedule").to_dict()["ts"]
    assert ts.endswith("Z")
    assert len(ts) == len("2026-08-24T14:03:22.114Z")
    assert ev.validate_event({**context().started("s").to_dict(), "ts": ts}) == ()


def test_finished_and_failed_carry_what_the_issue_asks_for() -> None:
    finished = context().finished(
        outcome="ok", artifacts=["journal/2026-08-24.md"], duration_s=42.4
    )
    assert ev.validate_event(finished.to_dict()) == ()
    assert finished.payload == {
        "routine": "daily-summary",
        "run_id": "0d1a…",
        "outcome": "ok",
        "artifacts": ["journal/2026-08-24.md"],
        "duration_s": 42.4,
    }

    failed = context().failed(error="boom", duration_s=1.0)
    assert ev.validate_event(failed.to_dict()) == ()
    assert failed.type == "routine_failed"
    assert failed.payload["error"] == "boom"


def test_the_added_types_are_the_additive_set() -> None:
    assert ev.EVENT_TYPES == (
        "routine_started",
        "routine_finished",
        "routine_failed",
        "task_posted",
        "task_claimed",
        "task_done",
        "task_failed",
        "task_delegated",
        "needs_human",
        "needs_human_resolved",
        "resident_restarted",
    )


def test_task_posted_carries_the_board_payload() -> None:
    event = ev.task_posted_event(task_id="t1", title="Research X", required_skills=["research"])
    assert ev.validate_event(event.to_dict()) == ()
    assert event.agent_id == ev.API_AGENT_ID
    assert event.payload == {
        "task_id": "t1",
        "title": "Research X",
        "required_skills": ["research"],
        "posted_by": "api",
    }


def test_a_needs_human_knock_is_bounded_so_it_can_never_be_a_transcript() -> None:
    """A session that prints a huge escalation must not become a 200KB village event."""
    event = ev.needs_human_event(
        message="x " * 5000,
        request_id="r1",
        action="approve_transfer",
        agent_id="claude-code:life-agent",
        project="household",
        detail={f"field_{i}": "A" * 50_000 for i in range(10)},
    )
    assert ev.validate_event(event.to_dict()) == ()
    payload = event.payload
    assert len(payload["message"]) <= ev.ERROR_MAX_CHARS
    # A detail that still serializes past the cap even after per-field trimming is dropped
    # for a marker, not emitted whole.
    encoded = json.dumps(payload["detail"])
    assert len(encoded) <= ev.DETAIL_MAX_CHARS
    assert set(payload["detail"]) == {"note"}
    assert "omitted" in payload["detail"]["note"]
    assert "A" * 1000 not in encoded


def test_a_needs_human_detail_that_fits_is_kept_but_each_field_is_capped() -> None:
    """A reasonable detail survives; only an oversized single field is trimmed."""
    detail = ev.bounded_detail({"resident": "life-agent", "reason": "y" * 10_000})
    assert detail["resident"] == "life-agent"
    assert len(detail["reason"]) <= ev.DETAIL_FIELD_MAX_CHARS
    assert detail["reason"].endswith("…")


def test_a_needs_human_knock_has_its_secrets_redacted_before_it_leaves() -> None:
    """A secret a session buries in a knock's message or detail is scrubbed, not egressed (#65)."""
    event = ev.needs_human_event(
        message="my key is sk-ant-abcdef0123456789ghij and it broke",
        request_id="r1",
        action="debug",
        agent_id="claude-code:life-agent",
        project="household",
        detail={
            "env": "BURROW_TOKEN=supersecretvalue1234567890",
            "pem": "-----BEGIN PRIVATE KEY-----\nMIIBjunk\n-----END PRIVATE KEY-----",
            "jwt": "eyJhbGciOiJIUzI1Niabc.eyJzdWIiOiIxMjM0NQ.SflKxwRJSMeKKF2QT4",
            "url": "postgres://user:hunter2secret@db.internal/app",
            "nested": {"deeper": ["ok", "sk-ant-ZZZZZZZZZZZZZZZZ1234"]},
            "runs": 3,  # a non-string fact steward built passes through untouched
        },
    )
    payload = event.payload
    assert "sk-ant-" not in payload["message"]
    assert "[redacted:secret]" in payload["message"]
    # The rest of the knock is intact — a human still reads the question.
    assert "it broke" in payload["message"]
    encoded = json.dumps(payload["detail"])
    for leaked in ("supersecretvalue", "MIIBjunk", "SflKxwRJSMeKKF2QT4", "hunter2secret", "ZZZZ"):
        assert leaked not in encoded
    assert "BURROW_TOKEN=[redacted:secret]" in payload["detail"]["env"]
    assert payload["detail"]["runs"] == 3
    assert payload["detail"]["nested"]["deeper"][0] == "ok"


def test_a_clean_needs_human_knock_is_byte_for_byte_unchanged() -> None:
    """Redaction never touches a knock that carries no secret (#65)."""
    message = "Testy wants to send email"
    detail = {"to": "anna@example.com", "subject": "Re: Thursday", "runs": 2}
    event = ev.needs_human_event(
        message=message,
        request_id="r1",
        action="send_email",
        agent_id="claude-code:life-agent",
        project="household",
        detail=detail,
    )
    assert event.payload["message"] == message
    assert event.payload["detail"] == detail


def test_needs_human_resolved_is_emitted_as_the_villager_who_knocked() -> None:
    event = ev.needs_human_resolved_event(
        request_id="r1",
        decision="approve",
        action="send_email",
        agent_id="claude-code:life-agent",
        project="household",
    )
    assert ev.validate_event(event.to_dict()) == ()
    assert event.agent_id == "claude-code:life-agent"
    assert event.payload == {
        "request_id": "r1",
        "decision": "approve",
        "decided_by": "api",
        "action": "send_email",
    }


def test_a_claim_names_its_parent_only_when_it_was_delegated() -> None:
    """A delegated item carries its parent through the whole claim/done/failed bracket."""
    plain = ev.task_claimed_event(
        task_id="t1", title="Research X", claimant="claude-code:hob", project="hob"
    )
    assert ev.validate_event(plain.to_dict()) == ()
    assert "parent_task_id" not in plain.payload

    delegated = ev.task_claimed_event(
        task_id="t2",
        title="Research Y",
        claimant="claude-code:hob",
        project="hob",
        parent_task_id="t1",
    )
    assert delegated.payload["parent_task_id"] == "t1"


def test_done_and_failed_carry_the_parent_when_delegated() -> None:
    """The receiver closes a delegated item still naming where the work came from."""
    done = ev.task_done_event(
        task_id="t2", title="Y", claimant="claude-code:hob", project="hob", parent_task_id="t1"
    )
    assert done.payload["parent_task_id"] == "t1"
    plain_done = ev.task_done_event(
        task_id="t9", title="Z", claimant="claude-code:hob", project="hob"
    )
    assert "parent_task_id" not in plain_done.payload

    failed = ev.task_failed_event(
        task_id="t2",
        title="Y",
        claimant="claude-code:hob",
        project="hob",
        reason="lease_expired",
        parent_task_id="t1",
    )
    assert failed.payload["parent_task_id"] == "t1"
    plain_failed = ev.task_failed_event(
        task_id="t9", title="Z", claimant="claude-code:hob", project="hob", reason="boom"
    )
    assert "parent_task_id" not in plain_failed.payload


def test_a_tasks_close_names_the_session_that_closed_it() -> None:
    """A task id is shared by every attempt; ``run_id`` is what tells them apart (#39).

    Absent when there is no session to name — the lease sweep mourns a claim rather than
    reporting a run back — because a close that names no session must answer none.
    """
    done = ev.task_done_event(
        task_id="t2", title="Y", claimant="claude-code:hob", project="hob", run_id="run-7"
    )
    assert done.payload["run_id"] == "run-7"
    failed = ev.task_failed_event(
        task_id="t2",
        title="Y",
        claimant="claude-code:hob",
        project="hob",
        reason="boom",
        run_id="run-7",
    )
    assert failed.payload["run_id"] == "run-7"

    swept = ev.task_failed_event(
        task_id="t2", title="Y", claimant="claude-code:hob", project="hob", reason="lease_expired"
    )
    assert "run_id" not in swept.payload
    assert (
        "run_id"
        not in ev.task_done_event(
            task_id="t2", title="Y", claimant="claude-code:hob", project="hob"
        ).payload
    )


def test_an_error_is_truncated_to_one_line() -> None:
    truncated = ev.truncate_error("a\nvery " + "long " * 400 + "story")
    assert len(truncated) == ev.ERROR_MAX_CHARS
    assert "\n" not in truncated
    assert truncated.endswith("…")
    assert ev.truncate_error("  short   enough  ") == "short enough"


def test_cwd_is_omitted_when_a_run_has_no_directory() -> None:
    event = ev.Event(type="routine_started", agent_id="steward:x", project="x")
    assert "cwd" not in event.to_dict()
    assert ev.validate_event(event.to_dict()) == ()


def test_validate_event_names_every_way_a_body_can_be_wrong() -> None:
    problems = ev.validate_event({})
    assert any("missing field 'v'" in p for p in problems)
    assert any("ts must be a string" in p for p in problems)

    body = context().started("schedule").to_dict()
    assert "v must be 0" in " ".join(ev.validate_event({**body, "v": 1}))
    assert "not UTC ISO-8601" in " ".join(ev.validate_event({**body, "ts": "yesterday"}))
    assert "agent_id must be" in " ".join(ev.validate_event({**body, "agent_id": ""}))
    assert "payload must be an object" in " ".join(ev.validate_event({**body, "payload": []}))
    assert "cwd must be a string" in " ".join(ev.validate_event({**body, "cwd": 7}))


# ------------------------------------------------------------------------------ transport


def test_an_unreachable_url_falls_back_to_the_local_log(tmp_path: Path) -> None:
    fallback = tmp_path / "events.jsonl"
    emitter = ev.EventEmitter(url=f"http://127.0.0.1:{free_port()}", fallback=fallback)

    assert emitter.emit(context().started("schedule")) is False

    (line,) = fallback.read_text(encoding="utf-8").splitlines()
    assert ev.validate_event(json.loads(line)) == ()


def test_a_failed_post_trips_a_breaker_so_the_caller_is_never_slowed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    now = [0.0]
    url = f"http://192.0.2.1:{free_port()}"  # TEST-NET-1: guaranteed to go nowhere
    emitter = ev.EventEmitter(
        url=url, fallback=tmp_path / "events.jsonl", timeout_s=0.05, clock=lambda: now[0]
    )
    attempts: list[str] = []

    def refuse(_self: ev.EventEmitter, url: str, body: bytes, delivery_id: str = "") -> bool:
        attempts.append(url)
        assert body
        assert delivery_id
        return False

    monkeypatch.setattr(ev.EventEmitter, "_post", refuse)

    emitter.emit(context().started("schedule"))
    emitter.emit(context().started("schedule"))
    assert attempts == [url], "the breaker holds the second attempt back"

    now[0] += ev.BREAKER_SECONDS + 1
    emitter.emit(context().started("schedule"))
    assert attempts == [url, url], "and lets go once the window passes"

    assert len((tmp_path / "events.jsonl").read_text().splitlines()) == 3


def test_a_loopback_breaker_lets_go_sooner(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    now = [0.0]
    emitter = ev.EventEmitter(
        url="http://127.0.0.1:8737", fallback=tmp_path / "e.jsonl", clock=lambda: now[0]
    )
    monkeypatch.setattr(ev.EventEmitter, "_post", lambda *_args: False)
    emitter.emit(context().started("schedule"))
    assert emitter._breaker_until["http://127.0.0.1:8737"] == ev.LOOPBACK_BREAKER_SECONDS


def test_a_reachable_burrow_gets_the_event_with_the_bearer_token(tmp_path: Path) -> None:
    seen: list[tuple[str, str, dict]] = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            length = int(self.headers["Content-Length"])
            seen.append(
                (
                    self.headers.get("Authorization", ""),
                    self.headers.get("X-Burrow-Delivery-ID", ""),
                    json.loads(self.rfile.read(length)),
                )
            )
            self.send_response(204)
            self.end_headers()

        def log_message(self, format: str, *args: object) -> None:  # noqa: A002
            """Keep the test output quiet."""

    server = HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        fallback = tmp_path / "events.jsonl"
        emitter = ev.EventEmitter(
            url=f"http://127.0.0.1:{server.server_port}", token="s3cret", fallback=fallback
        )
        assert emitter.emit(context().started("schedule")) is True
    finally:
        server.shutdown()
        server.server_close()

    (authorization, delivery_id, body) = seen[0]
    assert authorization == "Bearer s3cret"
    assert len(delivery_id) == 32
    assert ev.validate_event(body) == ()
    # A delivered event is *also* kept locally: the fallback log is the watchdog's own
    # complete record, so a bracket is never split across a transient outage.
    assert fallback.exists(), "a delivered event is still written to the local record"
    assert len(fallback.read_text(encoding="utf-8").splitlines()) == 1


def test_every_delivered_event_is_also_kept_in_the_complete_local_record(tmp_path: Path) -> None:
    """A whole bracket lands locally even when burrow takes every event.

    A finish delivered remotely must never leave the start looking unanswered, so the
    watchdog scanning the local log sees both ends of the run.
    """
    seen: list[dict] = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            length = int(self.headers["Content-Length"])
            seen.append(json.loads(self.rfile.read(length)))
            self.send_response(204)
            self.end_headers()

        def log_message(self, format: str, *args: object) -> None:  # noqa: A002
            """Quiet."""

    server = HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    fallback = tmp_path / "events.jsonl"
    try:
        emitter = ev.EventEmitter(url=f"http://127.0.0.1:{server.server_port}", fallback=fallback)
        started = emitter.emit(context().started("schedule"))
        finished = emitter.emit(context().finished(outcome="ok", artifacts=[], duration_s=1.0))
    finally:
        server.shutdown()
        server.server_close()

    assert started is True, "the start reached burrow"
    assert finished is True, "and so did the finish"
    assert len(seen) == 2, "and burrow got both, once each"
    types = [json.loads(line)["type"] for line in fallback.read_text().splitlines()]
    assert types == ["routine_started", "routine_finished"], "the local record is complete"


def test_no_url_means_straight_to_the_local_log(tmp_path: Path) -> None:
    fallback = tmp_path / "nested" / "events.jsonl"
    emitter = ev.EventEmitter(fallback=fallback)
    emitter.emit_many([context().started("schedule"), context().failed(error="x", duration_s=0.1)])
    assert len(fallback.read_text().splitlines()) == 2


def test_an_unwritable_fallback_never_takes_a_routine_down(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    blocked = tmp_path / "a-file"
    blocked.write_text("not a directory", encoding="utf-8")
    emitter = ev.EventEmitter(fallback=blocked / "events.jsonl")
    assert emitter.emit(context().started("schedule")) is False
    assert "could not persist event in the watchdog log" in caplog.text


def test_outage_is_replayed_after_restart_oldest_first(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fallback = tmp_path / "events.jsonl"
    offline = ev.EventEmitter(url="https://village.example", fallback=fallback)
    monkeypatch.setattr(ev.EventEmitter, "_post", lambda *_args: False)
    assert offline.emit(context().started("schedule")) is False
    assert offline.emit(context().failed(error="no", duration_s=1)) is False

    seen: list[tuple[str, str]] = []

    def accept(_self: ev.EventEmitter, _url: str, body: bytes, delivery_id: str = "") -> bool:
        seen.append((json.loads(body)["type"], delivery_id))
        return True

    monkeypatch.setattr(ev.EventEmitter, "_post", accept)
    restarted = ev.EventEmitter(url="https://village.example", fallback=fallback)
    report = restarted.flush()
    assert report == ev.FlushReport(delivered=2)
    assert [kind for kind, _delivery_id in seen] == ["routine_started", "routine_failed"]
    assert len({delivery_id for _kind, delivery_id in seen}) == 2


def test_real_http_outage_then_recovery_drains_the_durable_queue(tmp_path: Path) -> None:
    healthy = [False]
    deliveries: list[str] = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            self.rfile.read(int(self.headers["Content-Length"]))
            deliveries.append(self.headers["X-Burrow-Delivery-ID"])
            self.send_response(204 if healthy[0] else 503)
            self.end_headers()

        def log_message(self, format: str, *args: object) -> None:  # noqa: A002
            """Quiet."""

    server = HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    fallback = tmp_path / "events.jsonl"
    try:
        url = f"http://127.0.0.1:{server.server_port}"
        assert (
            ev.EventEmitter(url=url, fallback=fallback).emit(context().started("schedule")) is False
        )
        healthy[0] = True
        report = ev.EventEmitter(url=url, fallback=fallback).flush()
    finally:
        server.shutdown()
        server.server_close()
    assert report.delivered == 1
    assert deliveries[0] == deliveries[1]


def test_partial_flush_retires_success_and_stops_at_first_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    emitter = ev.EventEmitter(url="https://village.example", fallback=tmp_path / "events.jsonl")
    events = [context().started("schedule"), context().failed(error="x", duration_s=1)]
    for index, event in enumerate(events):
        assert emitter._queue_record(event, f"delivery-{index:08d}")
    attempts = 0

    def partial(*_args: object) -> bool:
        nonlocal attempts
        attempts += 1
        return attempts == 1

    monkeypatch.setattr(ev.EventEmitter, "_post", partial)
    report = emitter.flush()
    assert report.delivered == 1
    assert report.pending == 1
    assert report.failed == 1


def test_foreign_target_backlog_does_not_starve_current_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fallback = tmp_path / "events.jsonl"
    old = ev.EventEmitter(url="https://old.example", fallback=fallback)
    for index in range(ev.REPLAY_BATCH_SIZE + 1):
        assert old._queue_record(context().started("schedule"), f"foreign-{index:016d}")
    current = ev.EventEmitter(url="https://current.example", fallback=fallback)
    assert current._queue_record(context().started("schedule"), "delivery-current-0001")
    monkeypatch.setattr(ev.EventEmitter, "_post", lambda *_args: True)
    report = current.flush(limit=ev.REPLAY_BATCH_SIZE)
    assert report.delivered == 1
    assert report.foreign == ev.REPLAY_BATCH_SIZE + 1
    assert report.pending == ev.REPLAY_BATCH_SIZE + 1


def test_corrupt_and_torn_queue_lines_are_quarantined_without_blocking_valid_replay(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    emitter = ev.EventEmitter(url="https://village.example", fallback=tmp_path / "events.jsonl")
    assert emitter._queue_record(context().started("schedule"), "delivery-00000000")
    with emitter.queue.open("ab") as handle:
        handle.write(b'not-json\n{"torn":')
    monkeypatch.setattr(ev.EventEmitter, "_post", lambda *_args: True)
    report = emitter.flush()
    assert report.delivered == 1
    assert report.pending == 0
    assert report.corrupt == 2
    assert emitter.queue.with_name(f"{emitter.queue.name}.corrupt").exists()


def test_corrupt_quarantine_preserves_invalid_utf8_and_newline_bytes_exactly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    emitter = ev.EventEmitter(url="https://village.example", fallback=tmp_path / "events.jsonl")
    evidence = [b"bad-\xff\n", b'{"torn":']
    emitter.queue.parent.mkdir(parents=True, exist_ok=True)
    emitter.queue.write_bytes(b"".join(evidence))
    monkeypatch.setattr(ev.EventEmitter, "_post", lambda *_args: True)

    assert emitter.flush().corrupt == 2
    framed = emitter.queue.with_name(f"{emitter.queue.name}.corrupt").read_bytes()
    assert framed == (
        b"STEWARD-CORRUPT-V1 6\n"
        + evidence[0]
        + b"\nSTEWARD-CORRUPT-END\n"
        + b"STEWARD-CORRUPT-V1 8\n"
        + evidence[1]
        + b"\nSTEWARD-CORRUPT-END\n"
    )


def test_failed_corrupt_quarantine_leaves_original_queue_authoritative(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    emitter = ev.EventEmitter(url="https://village.example", fallback=tmp_path / "events.jsonl")
    emitter.queue.parent.mkdir(parents=True, exist_ok=True)
    emitter.queue.write_bytes(b"corrupt\n")
    monkeypatch.setattr(
        emitter,
        "_append_corrupt_evidence",
        lambda _chunks: (_ for _ in ()).throw(OSError("simulated quarantine failure")),
    )
    report = emitter.flush()
    assert report.errors == 1
    assert report.unknown == 1
    assert emitter.queue.read_bytes() == b"corrupt\n"


def test_queue_read_failure_is_not_reported_as_a_clean_drain(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    emitter = ev.EventEmitter(url="https://village.example", fallback=tmp_path / "events.jsonl")
    assert emitter._queue_record(context().started("schedule"), "delivery-unreadable")

    def unreadable() -> tuple[list[dict[str, object]], list[bytes]]:
        raise OSError("simulated unreadable queue")

    monkeypatch.setattr(emitter, "_read_queue_unlocked", unreadable)
    report = emitter.flush()
    assert report.errors == 1
    assert report.unknown == 1


def test_repeated_delivery_after_crash_uses_the_same_id(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    emitter = ev.EventEmitter(url="https://village.example", fallback=tmp_path / "events.jsonl")
    assert emitter._queue_record(context().started("schedule"), "delivery-crash-window")
    ids: list[str] = []

    def accept(_self: ev.EventEmitter, _url: str, _body: bytes, delivery_id: str = "") -> bool:
        ids.append(delivery_id)
        return True

    monkeypatch.setattr(ev.EventEmitter, "_post", accept)
    original = emitter._rewrite_queue

    def crash(_retired: set[str]) -> None:
        raise OSError("simulated crash before retirement")

    monkeypatch.setattr(emitter, "_rewrite_queue", crash)
    report = emitter.flush()
    assert report.delivered == 1
    assert report.errors == 1
    assert report.unknown == 1
    monkeypatch.setattr(emitter, "_rewrite_queue", original)
    assert emitter.flush().delivered == 1
    assert ids == ["delivery-crash-window", "delivery-crash-window"]


def test_queue_append_failure_never_posts_without_retry_authority(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    emitter = ev.EventEmitter(url="https://village.example", fallback=tmp_path / "events.jsonl")
    posts: list[object] = []
    monkeypatch.setattr(emitter, "_queue_record", lambda *_args, **_kwargs: False)
    monkeypatch.setattr(emitter, "_post", lambda *_args: posts.append(object()) or True)
    assert emitter.emit(context().started("schedule")) is False
    assert posts == []


def test_history_failure_is_recovered_before_remote_delivery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    emitter = ev.EventEmitter(url="https://village.example", fallback=tmp_path / "events.jsonl")
    event = context().started("schedule")
    assert emitter._queue_record(event, "delivery-history-retry", history=False)
    original = emitter._confirm_history
    attempts = 0
    posts: list[str] = []

    def fail_once(record: dict[str, object]) -> bool:
        nonlocal attempts
        if attempts == 0:
            attempts += 1
            return False
        return original(record)

    monkeypatch.setattr(emitter, "_confirm_history", fail_once)
    monkeypatch.setattr(
        emitter,
        "_post",
        lambda _url, _body, delivery_id="": posts.append(delivery_id) or True,
    )
    first = emitter.flush()
    assert first.delivered == 0
    assert first.errors == 1
    assert posts == []
    assert emitter.queue.exists()

    second = emitter.flush()
    assert second.delivered == 1
    assert posts == ["delivery-history-retry"]
    history = [json.loads(line) for line in emitter.fallback.read_bytes().splitlines()]
    assert [row["steward_delivery_id"] for row in history] == ["delivery-history-retry"]


def test_concurrent_append_survives_a_flush_rewrite(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    emitter = ev.EventEmitter(url="https://village.example", fallback=tmp_path / "events.jsonl")
    assert emitter._queue_record(context().started("schedule"), "delivery-concurrent-old")
    posting = threading.Event()
    release = threading.Event()

    def accept(*_args: object) -> bool:
        posting.set()
        assert release.wait(timeout=2)
        return True

    monkeypatch.setattr(ev.EventEmitter, "_post", accept)
    worker = threading.Thread(target=emitter.flush)
    worker.start()
    assert posting.wait(timeout=2)
    assert emitter._queue_record(
        context().failed(error="later", duration_s=1), "delivery-concurrent-new"
    )
    release.set()
    worker.join(timeout=2)
    assert not worker.is_alive()
    remaining, corrupt = emitter._read_queue()
    assert corrupt == []
    assert [record["delivery_id"] for record in remaining] == ["delivery-concurrent-new"]


def test_legacy_import_is_stable_explicit_and_reports_invalid_lines(tmp_path: Path) -> None:
    fallback = tmp_path / "events.jsonl"
    event = context().started("schedule")
    fallback.write_text(event.to_json() + "\nnot-json\n", encoding="utf-8")
    emitter = ev.EventEmitter(url="https://village.example", fallback=fallback)
    first = emitter.import_legacy()
    second = emitter.import_legacy()
    records, corrupt = emitter._read_queue()
    assert first == second == ev.ImportReport(queued=1, invalid=1)
    assert corrupt == []
    assert len(records) == 2
    assert records[0]["delivery_id"] == records[1]["delivery_id"]


def test_legacy_import_reports_non_missing_os_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "legacy.jsonl"
    source.write_text("unreadable", encoding="utf-8")
    emitter = ev.EventEmitter(url="https://village.example", fallback=tmp_path / "events.jsonl")
    original = Path.read_bytes

    def deny(path: Path) -> bytes:
        if path == source:
            raise PermissionError("simulated")
        return original(path)

    monkeypatch.setattr(Path, "read_bytes", deny)
    assert emitter.import_legacy(source) == ev.ImportReport(errors=1, unknown=1)


def test_a_non_http_url_is_never_opened(tmp_path: Path) -> None:
    emitter = ev.EventEmitter(url="file:///etc/passwd", fallback=tmp_path / "e.jsonl")
    assert emitter.emit(context().started("schedule")) is False
    assert (tmp_path / "e.jsonl").exists()


# ---------------------------------------------------------------------------- environment


def test_the_emitter_reads_burrow_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("BURROW_URL", "https://village.example/")
    monkeypatch.setenv("BURROW_TOKEN", "  tok  ")
    monkeypatch.setenv("STEWARD_EVENTS_FALLBACK", str(tmp_path / "fallback.jsonl"))
    emitter = ev.EventEmitter.from_env()
    assert emitter.url == "https://village.example"
    assert emitter.token == "tok"
    assert emitter.fallback == tmp_path / "fallback.jsonl"


def test_the_default_fallback_is_burrows_own_log(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("STEWARD_EVENTS_FALLBACK", raising=False)
    assert ev.default_fallback_path() == Path.home() / ".burrow" / "events.jsonl"


def test_repr_never_shows_the_token() -> None:
    text = repr(ev.EventEmitter(url="https://village.example", token="s3cret"))
    assert "s3cret" not in text
    assert "village.example" in text


def test_the_null_emitter_sends_nothing_and_remembers_everything() -> None:
    emitter = ev.NullEmitter()
    assert emitter.emit(context().started("schedule")) is False
    assert [event.type for event in emitter.events] == ["routine_started"]
