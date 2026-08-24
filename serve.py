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
VILLAGERS_DIR = os.environ.get("BURROW_VILLAGERS") or os.path.expanduser("~/.burrow/villagers")


CTYPES = {".html": "text/html; charset=utf-8", ".js": "text/javascript",
          ".css": "text/css", ".png": "image/png", ".json": "application/json"}


def read_villagers():
    """Soul files: ~/.burrow/villagers/*.md with a simple `key: value` frontmatter
    between --- fences; the body is free-form markdown shown in the viewer panel."""
    out = []
    if not os.path.isdir(VILLAGERS_DIR):
        return out
    for fn in sorted(os.listdir(VILLAGERS_DIR)):
        if not fn.endswith(".md"):
            continue
        try:
            with open(os.path.join(VILLAGERS_DIR, fn), encoding="utf-8") as f:
                text = f.read()
        except OSError:
            continue
        meta, body = {}, text.strip()
        if text.startswith("---"):
            parts = text.split("---", 2)
            if len(parts) >= 3:
                for line in parts[1].strip().splitlines():
                    if ":" in line:
                        k, v = line.split(":", 1)
                        meta[k.strip()] = v.strip()
                body = parts[2].strip()
        out.append({"file": fn, "meta": meta, "body": body})
    return out


class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        path = self.path.split("?")[0]
        if path == "/villagers":
            self._send(200, json.dumps(read_villagers()).encode("utf-8"),
                       "application/json")
            return
        if path == "/events":
            data = b""
            if os.path.exists(EVENTS):
                with open(EVENTS, "rb") as f:
                    data = f.read()
            self._send(200, data, "application/x-ndjson")
            return
        # everything else is a static file under viewer/
        if path in ("/", "/index.html"):
            path = "/index.html"
        base = os.path.join(ROOT, "viewer")
        full = os.path.realpath(os.path.join(base, path.lstrip("/")))
        if not full.startswith(base + os.sep) or not os.path.isfile(full):
            self._send(404, b"not found", "text/plain")
            return
        ctype = CTYPES.get(os.path.splitext(full)[1], "application/octet-stream")
        self._send_file(full, ctype)

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
