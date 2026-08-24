#!/usr/bin/env python3
"""burrow v0 server: serves viewer/index.html, the raw event log, and accepts
protocol events over HTTP (POST /events, one JSON event per request).

    python3 serve.py [port]     # default 8737

Env:
    BURROW_HOST          bind address (default 127.0.0.1; 0.0.0.0 in the container)
    BURROW_EVENTS        event log path (default ~/.burrow/events.jsonl)
    BURROW_VILLAGERS     soul file directory (default ~/.burrow/villagers)
    BURROW_NOTIFY_URL    POST target for needs_human knocks (unset = no notifications)
    BURROW_NOTIFY_TOKEN  optional bearer token for that target (e.g. a private ntfy topic)
    BURROW_NOTIFY_TIMEOUT  seconds to wait on the webhook (default 5)
"""
import collections
import http.server
import json
import os
import sys
import threading
import urllib.request

PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 8737
HOST = os.environ.get("BURROW_HOST", "127.0.0.1")
ROOT = os.path.dirname(os.path.abspath(__file__))
EVENTS = os.environ.get("BURROW_EVENTS") or os.path.expanduser("~/.burrow/events.jsonl")

MAX_EVENT_BYTES = 64 * 1024
VILLAGERS_DIR = os.environ.get("BURROW_VILLAGERS") or os.path.expanduser("~/.burrow/villagers")

NOTIFY_URL = (os.environ.get("BURROW_NOTIFY_URL") or "").strip()
NOTIFY_TOKEN = (os.environ.get("BURROW_NOTIFY_TOKEN") or "").strip()
try:
    NOTIFY_TIMEOUT = float(os.environ.get("BURROW_NOTIFY_TIMEOUT") or 5)
except ValueError:
    NOTIFY_TIMEOUT = 5.0
NOTIFY_MEMORY = 512      # how many knocks we remember, to not knock twice


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


# ————— knocks: push a needs_human event to a webhook —————
#
# The village can't knock on a door you're not looking at, so a `needs_human`
# ingest also fires one POST at BURROW_NOTIFY_URL. Body is plain text and the
# title rides in headers, which is exactly what ntfy wants; anything that
# accepts a POST works. It happens on a daemon thread and swallows every
# error: a knock we fail to forward must never slow down or fail the ingest.

NAMES = ["Bramble", "Poppy", "Wren", "Sorrel", "Fern", "Alder", "Maple", "Rowan",
         "Thistle", "Clover", "Hazel", "Juniper", "Moss", "Reed", "Tansy", "Willow"]

_notified = collections.OrderedDict()
_notified_lock = threading.Lock()


def js_hash(s):
    """The viewer's hashCode, verbatim, so a nameless villager is called the same
    thing in the notification as it is on screen."""
    h = 0
    for ch in s:
        h = (h * 31 + ord(ch)) & 0xFFFFFFFF
    if h >= 0x80000000:
        h -= 0x100000000
    return abs(h)


def villager_name(event):
    """Soul file name (agent_id beats project), else the stable hash-based name."""
    agent_id = str(event.get("agent_id") or "")
    project = str(event.get("project") or "")
    by_project = None
    for soul in read_villagers():
        meta = soul.get("meta") or {}
        name = meta.get("name")
        if not name:
            continue
        if meta.get("agent_id") and meta["agent_id"] == agent_id:
            return name
        if by_project is None and meta.get("project") and meta["project"] == project:
            by_project = name
    return by_project or NAMES[js_hash(agent_id) % len(NAMES)]


def claim_knock(event):
    """True the first time we see this exact knock. Emitters retry and clients
    replay, so identity is (agent, timestamp, message), not arrival."""
    if not NOTIFY_URL or event.get("type") != "needs_human":
        return False
    payload = event.get("payload") or {}
    key = "\x00".join(str(x) for x in (event.get("agent_id"), event.get("ts"),
                                       payload.get("message") if isinstance(payload, dict) else ""))
    with _notified_lock:
        if key in _notified:
            _notified.move_to_end(key)
            return False
        _notified[key] = True
        while len(_notified) > NOTIFY_MEMORY:
            _notified.popitem(last=False)
    return True


def notify(event):
    """Fire-and-forget POST. Never raises."""
    try:
        payload = event.get("payload") or {}
        message = str(payload.get("message") or "").strip() if isinstance(payload, dict) else ""
        message = message or "(no message)"
        name = villager_name(event)
        project = str(event.get("project") or "unknown")
        headers = {
            "Content-Type": "text/plain; charset=utf-8",
            "Title": f"{name} is at your door ({project})",
            "Tags": "door",
            "Priority": "high",
        }
        if NOTIFY_TOKEN:
            headers["Authorization"] = "Bearer " + NOTIFY_TOKEN
        body = f"{name} · {project}\n{message}".encode("utf-8")
        req = urllib.request.Request(NOTIFY_URL, data=body, headers=headers, method="POST")
        with urllib.request.urlopen(req, timeout=NOTIFY_TIMEOUT):
            pass
    except Exception:
        pass


def notify_async(event):
    threading.Thread(target=notify, args=(event,), daemon=True).start()


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
        if claim_knock(event):
            notify_async(event)
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
    if NOTIFY_URL:
        print(f"knocks will be pushed to {NOTIFY_URL}")
    http.server.ThreadingHTTPServer((HOST, PORT), Handler).serve_forever()
