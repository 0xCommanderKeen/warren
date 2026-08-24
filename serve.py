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
import urllib.parse

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
        if not fn.endswith(".md") or fn.startswith("."):
            continue
        try:
            with open(os.path.join(VILLAGERS_DIR, fn), encoding="utf-8") as f:
                text = f.read()
        except (OSError, UnicodeDecodeError):
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
        parsed = urllib.parse.urlsplit(self.path)
        path = parsed.path
        if path == "/villagers":
            self._send(200, json.dumps(read_villagers()).encode("utf-8"),
                       "application/json")
            return
        if path == "/events":
            params = urllib.parse.parse_qs(parsed.query, keep_blank_values=True)
            raw_since = params.get("since", ["0"])[0]
            try:
                cursor_parts = raw_since.split(":")
                if len(cursor_parts) == 3:
                    cursor_identity = (int(cursor_parts[0]), int(cursor_parts[1]))
                    since = int(cursor_parts[2])
                elif len(cursor_parts) == 1:
                    # Accept the original numeric cursor during rolling upgrades.
                    cursor_identity = None
                    since = int(raw_since)
                else:
                    raise ValueError
                if since < 0:
                    raise ValueError
            except ValueError:
                self._send(400, b"invalid since cursor", "text/plain")
                return

            data, cursor, reset = b"", 0, False
            try:
                with open(EVENTS, "rb") as f:
                    stat = os.fstat(f.fileno())
                    identity = (stat.st_dev, stat.st_ino)
                    if since > stat.st_size or (
                            cursor_identity is not None and cursor_identity != identity):
                        since, reset = 0, True
                    f.seek(since)
                    chunk = f.read()
                    # Do not advance over an event that is still being appended.
                    end = chunk.rfind(b"\n") + 1
                    if end:
                        data = chunk[:end]
                    cursor = since + end
                    if cursor:
                        cursor = f"{identity[0]}:{identity[1]}:{cursor}"
            except FileNotFoundError:
                reset = since > 0

            headers = {"X-Burrow-Cursor": str(cursor)}
            if reset:
                headers["X-Burrow-Reset"] = "1"
            self._send(200, data, "application/x-ndjson", headers)
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

    def _send(self, code, data, ctype, headers=None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(data)))
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        self.end_headers()
        if data:
            self.wfile.write(data)

    def log_message(self, *args):
        pass


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8737
    print(f"burrow village at http://{HOST}:{port}, log at {EVENTS}")
    http.server.ThreadingHTTPServer((HOST, port), Handler).serve_forever()
