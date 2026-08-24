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
        "needs_human",
        "needs_human_resolved",
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

    def refuse(_self: ev.EventEmitter, url: str, body: bytes) -> bool:
        attempts.append(url)
        assert body
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
    seen: list[tuple[str, dict]] = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            length = int(self.headers["Content-Length"])
            seen.append(
                (self.headers.get("Authorization", ""), json.loads(self.rfile.read(length)))
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

    (authorization, body) = seen[0]
    assert authorization == "Bearer s3cret"
    assert ev.validate_event(body) == ()
    assert not fallback.exists(), "a delivered event is not also written locally"


def test_no_url_means_straight_to_the_local_log(tmp_path: Path) -> None:
    fallback = tmp_path / "nested" / "events.jsonl"
    emitter = ev.EventEmitter(fallback=fallback)
    emitter.emit_many([context().started("schedule"), context().failed(error="x", duration_s=0.1)])
    assert len(fallback.read_text().splitlines()) == 2


def test_an_unwritable_fallback_never_takes_a_routine_down(tmp_path: Path) -> None:
    blocked = tmp_path / "a-file"
    blocked.write_text("not a directory", encoding="utf-8")
    emitter = ev.EventEmitter(fallback=blocked / "events.jsonl")
    assert emitter.emit(context().started("schedule")) is False


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
