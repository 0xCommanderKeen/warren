"""Loopback Telegram and Discord servers for adapter and bridge contract tests."""

import json
import threading
from collections.abc import Iterator
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import pytest

# --------------------------------------------------------------------------------------
# the wire
# --------------------------------------------------------------------------------------


@dataclass
class BotApi:
    """A Telegram bot API that only exists on loopback, and remembers what it was asked."""

    url: str = ""
    calls: list[tuple[str, dict[str, Any]]] = field(default_factory=list)
    replies: dict[str, Any] = field(default_factory=dict)

    def method(self, name: str) -> dict[str, Any]:
        """Return the body of the one call made to ``name``."""
        return next(body for path, body in self.calls if path.endswith(f"/{name}"))


@pytest.fixture
def bot_api() -> Iterator[BotApi]:
    """Serve a fake bot API on 127.0.0.1, so the transport is tested against real HTTP.

    A stub server rather than a patched ``urlopen``, for the reason the runner tests use a
    real process: the thing worth pinning is that steward speaks the protocol it claims to —
    the token in the path, the JSON body, the ``ok`` envelope — and a mock of ``urlopen``
    would only prove that this test agrees with itself.
    """
    state = BotApi()

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            length = int(self.headers.get("Content-Length") or 0)
            payload = json.loads(self.rfile.read(length) or b"{}")
            state.calls.append((self.path, payload))
            body = json.dumps(
                state.replies.get(self.path.rsplit("/", 1)[-1], {"ok": True, "result": []})
            ).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: object) -> None:  # noqa: A002
            """Keep the test output quiet."""

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    state.url = f"http://127.0.0.1:{server.server_port}"
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield state
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


@dataclass
class DiscordApi:
    """A deterministic Discord REST API that only listens on loopback."""

    url: str = ""
    calls: list[tuple[str, str, dict[str, Any] | None, str]] = field(default_factory=list)
    replies: dict[tuple[str, str], list[tuple[int, Any]]] = field(default_factory=dict)
    user_agents: list[str] = field(default_factory=list)

    def queue(self, method: str, path: str, *answers: tuple[int, Any]) -> None:
        """Queue ordered HTTP responses for one method and exact request path."""
        self.replies[(method, path)] = list(answers)


@pytest.fixture
def discord_api() -> Iterator[DiscordApi]:
    state = DiscordApi()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            self._answer("GET")

        def do_POST(self) -> None:
            self._answer("POST")

        def do_PATCH(self) -> None:
            self._answer("PATCH")

        def do_PUT(self) -> None:
            self._answer("PUT")

        def _answer(self, method: str) -> None:
            length = int(self.headers.get("Content-Length") or 0)
            payload = json.loads(self.rfile.read(length)) if length else None
            state.calls.append((method, self.path, payload, self.headers.get("Authorization", "")))
            state.user_agents.append(self.headers.get("User-Agent", ""))
            answers = state.replies.get((method, self.path), [(200, {})])
            status, value = answers.pop(0)
            body = json.dumps(value).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: object) -> None:  # noqa: A002
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    state.url = f"http://127.0.0.1:{server.server_port}"
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield state
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def discord_message(
    snowflake: str = "101",
    *,
    sender: str = "31337",
    bot: bool = False,
    content: str = "hello",
    mentions: tuple[str, ...] = (),
) -> dict[str, Any]:
    return {
        "id": snowflake,
        "channel_id": "900",
        "author": {"id": sender, "bot": bot},
        "content": content,
        "mentions": [{"id": user_id} for user_id in mentions],
        "timestamp": "2026-09-01T12:00:00Z",
    }
