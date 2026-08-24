#!/usr/bin/env python3
"""burrow v0 server: serves viewer/index.html, the raw event log, and accepts
protocol events over HTTP (POST /events, one JSON event per request).

    python3 serve.py [port]     # default 8737

Env:
    BURROW_HOST         bind address (default 127.0.0.1; 0.0.0.0 in the container)
    BURROW_EVENTS       event log path (default ~/.burrow/events.jsonl)
    BURROW_VILLAGERS    soul file directory (default ~/.burrow/villagers)
    BURROW_ARCHIVE      rotated log directory (default <events dir>/archive)
    BURROW_MAX_LOG      rotate once the live log passes this many bytes
                        (default 5 MiB; 0 disables rotation)
"""
import datetime
import http.server
import json
import os
import shutil
import sys
import threading
import time

PORT = int(sys.argv[1]) if len(sys.argv) > 1 and sys.argv[1].isdigit() else 8737
HOST = os.environ.get("BURROW_HOST", "127.0.0.1")
ROOT = os.path.dirname(os.path.abspath(__file__))
EVENTS = os.environ.get("BURROW_EVENTS") or os.path.expanduser("~/.burrow/events.jsonl")

MAX_EVENT_BYTES = 64 * 1024
VILLAGERS_DIR = os.environ.get("BURROW_VILLAGERS") or os.path.expanduser("~/.burrow/villagers")
ARCHIVE_DIR = os.environ.get("BURROW_ARCHIVE") or ""
MAX_LOG_BYTES = int(os.environ.get("BURROW_MAX_LOG") or 5 * 1024 * 1024)

# the viewer's reduction rules, mirrored here so a rotated log still projects to
# exactly the same village (see docs/protocol.md and viewer/index.html)
EVENT_TYPES = {"task_started", "tool_called", "artifact_produced",
               "needs_human", "idle", "session_ended"}
KEEP_PER_AGENT = 80                  # events the viewer keeps per villager
DROP_MS = 12 * 60 * 60 * 1000        # villagers quiet longer than this are gone

# every read, append and rotation of the log goes through this, so an event can
# never land in the gap between reading the log and swapping it out
LOG_LOCK = threading.Lock()
_rotate_floor = 0                    # don't re-check until the log grows past this


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


def event_ms(event):
    """Event timestamp as epoch ms; 0 when missing or unparseable — the viewer's
    `Date.parse(ts) || 0`, which puts the villager outside the drop window."""
    ts = event.get("ts")
    if not isinstance(ts, str):
        return 0
    try:
        when = datetime.datetime.fromisoformat(ts.strip().replace("Z", "+00:00"))
    except ValueError:
        return 0
    if when.tzinfo is None:
        when = when.replace(tzinfo=datetime.timezone.utc)
    return int(when.timestamp() * 1000)


def carry_forward(lines, now_ms):
    """The minimal tail of `lines` that still reduces to the same village: the
    last KEEP_PER_AGENT events of every villager the viewer would still draw —
    skipping the ones that went home or fell out of the 12 h window — kept in
    their original order, because the projection reads the latest event."""
    per_agent = {}
    for i, line in enumerate(lines):
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if not isinstance(event, dict) or not event.get("agent_id"):
            continue
        if event.get("type") not in EVENT_TYPES:
            continue
        kept = per_agent.setdefault(event["agent_id"], [])
        kept.append((i, event))
        if len(kept) > KEEP_PER_AGENT:
            kept.pop(0)
    keep = []
    for kept in per_agent.values():
        last = kept[-1][1]
        if last["type"] == "session_ended" or now_ms - event_ms(last) > DROP_MS:
            continue
        keep.extend(i for i, _ in kept)
    return [lines[i] for i in sorted(keep)]


def archive_dir():
    """Where segments land: BURROW_ARCHIVE, else `archive/` beside the live log —
    same volume in both local mode and the container's mounted /data."""
    return ARCHIVE_DIR or os.path.join(
        os.path.dirname(os.path.abspath(EVENTS)), "archive")


def archive_path(now=None):
    """<archive>/events-20260824T170430Z.jsonl, never overwriting a segment."""
    now = now or datetime.datetime.now(datetime.timezone.utc)
    into = archive_dir()
    base, ext = os.path.splitext(os.path.basename(EVENTS))
    stamp = now.strftime("%Y%m%dT%H%M%SZ")
    path = os.path.join(into, base + "-" + stamp + ext)
    n = 1
    while os.path.exists(path):
        path = os.path.join(into, "%s-%s-%d%s" % (base, stamp, n, ext))
        n += 1
    return path


def rotate(size):
    """Roll the live log into a dated archive and restart it from the tail the
    village still needs. Call with LOG_LOCK held. Returns the archive path, or
    None when there was nothing worth reclaiming."""
    global _rotate_floor
    with open(EVENTS, encoding="utf-8", errors="replace") as f:
        lines = f.read().splitlines()
    tail = carry_forward(lines, int(time.time() * 1000))
    data = "".join(line + "\n" for line in tail).encode("utf-8")
    if len(data) > size * 9 // 10:
        # every event still matters (a very busy fleet): rotating would only
        # copy the log next to itself, so wait until it has grown some more
        _rotate_floor = size + max(MAX_LOG_BYTES // 10, 1)
        return None
    os.makedirs(archive_dir(), exist_ok=True)
    pending = EVENTS + ".rotating"
    with open(pending, "wb") as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())
    archive = archive_path()
    try:
        # hardlink first: the live path never blinks out of existence, so an
        # emitter appending straight to the file cannot land in a gap
        os.link(EVENTS, archive)
    except OSError:
        # archive is on another volume, or the filesystem has no hardlinks
        shutil.move(EVENTS, archive)
    os.replace(pending, EVENTS)
    _rotate_floor = 0
    return archive


def maybe_rotate():
    """Size check on the live log. Call with LOG_LOCK held."""
    if MAX_LOG_BYTES <= 0:
        return
    try:
        size = os.path.getsize(EVENTS)
    except OSError:
        return
    if size <= max(MAX_LOG_BYTES, _rotate_floor):
        return
    try:
        rotate(size)
    except OSError:
        pass    # a log we failed to rotate beats a dropped event


def read_log():
    """The live log, rotating it first if it has outgrown the threshold. Doing
    the check here too keeps local mode bounded, where emitters append to the
    file themselves and the server only ever reads it."""
    with LOG_LOCK:
        maybe_rotate()
        try:
            with open(EVENTS, "rb") as f:
                return f.read()
        except OSError:
            return b""


def append_event(event):
    """Append one event, then rotate if the log is now too big — in that order,
    so an accepted POST is always in the live tail or in an archive."""
    line = json.dumps(event, ensure_ascii=False) + "\n"
    with LOG_LOCK:
        os.makedirs(os.path.dirname(os.path.abspath(EVENTS)), exist_ok=True)
        with open(EVENTS, "a", encoding="utf-8") as f:
            f.write(line)
        maybe_rotate()


class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        path = self.path.split("?")[0]
        if path == "/villagers":
            self._send(200, json.dumps(read_villagers()).encode("utf-8"),
                       "application/json")
            return
        if path == "/events":
            self._send(200, read_log(), "application/x-ndjson")
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
        append_event(event)
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
    if MAX_LOG_BYTES > 0:
        print(f"rotating past {MAX_LOG_BYTES} bytes into {archive_dir()}")
    http.server.ThreadingHTTPServer((HOST, PORT), Handler).serve_forever()
