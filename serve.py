#!/usr/bin/env python3
"""burrow v0 server: serves viewer/index.html, the raw event log, and accepts
protocol events over HTTP (POST /events, one JSON event per request).

    python3 serve.py [port]     # default 8737

Env:
    BURROW_HOST    bind address (default 127.0.0.1; 0.0.0.0 in the container)
    BURROW_EVENTS  event log path (default ~/.burrow/events.jsonl)
"""
import http.server
import json
import os
import sys

PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 8737
HOST = os.environ.get("BURROW_HOST", "127.0.0.1")
ROOT = os.path.dirname(os.path.abspath(__file__))
EVENTS = os.environ.get("BURROW_EVENTS") or os.path.expanduser("~/.burrow/events.jsonl")

MAX_EVENT_BYTES = 64 * 1024


class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path in ("/", "/index.html"):
            self._send_file(os.path.join(ROOT, "viewer", "index.html"),
                            "text/html; charset=utf-8")
        elif self.path.split("?")[0] == "/events":
            data = b""
            if os.path.exists(EVENTS):
                with open(EVENTS, "rb") as f:
                    data = f.read()
            self._send(200, data, "application/x-ndjson")
        else:
            self._send(404, b"not found", "text/plain")

    def do_POST(self):
        if self.path.split("?")[0] != "/events":
            self._send(404, b"not found", "text/plain")
            return
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0 or length > MAX_EVENT_BYTES:
            self._send(413, b"bad length", "text/plain")
            return
        body = self.rfile.read(length)
        try:
            event = json.loads(body)
            assert isinstance(event, dict) and event.get("type") and event.get("agent_id")
        except Exception:
            self._send(400, b"not a protocol event", "text/plain")
            return
        os.makedirs(os.path.dirname(EVENTS), exist_ok=True)
        with open(EVENTS, "a", encoding="utf-8") as f:
            f.write(json.dumps(event, ensure_ascii=False) + "\n")
        self._send(204, b"", "text/plain")

    def _send_file(self, path, ctype):
        try:
            with open(path, "rb") as f:
                self._send(200, f.read(), ctype)
        except OSError:
            self._send(404, b"missing: " + path.encode(), "text/plain")

    def _send(self, code, data, ctype):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        if data:
            self.wfile.write(data)

    def log_message(self, *args):
        pass


if __name__ == "__main__":
    print(f"burrow village at http://{HOST}:{PORT}, log at {EVENTS}")
    http.server.ThreadingHTTPServer((HOST, PORT), Handler).serve_forever()
