#!/usr/bin/env python3
"""burrow v0 server: serves viewer/index.html, the raw event log, and accepts
protocol events over HTTP (POST /events, one JSON event per request).

    python3 serve.py [port]     # default 8737

Env:
    BURROW_HOST          bind address (default 127.0.0.1; 0.0.0.0 in the container)
    BURROW_EVENTS        event log path (default ~/.burrow/events.jsonl)
    BURROW_VILLAGERS     resident-manifest directory (default: villagers/ next to this file)
    BURROW_ARCHIVE       rotated log directory (default <events dir>/archive)
    BURROW_MAX_LOG       rotate once the live log passes this many bytes
    BURROW_NOTIFY_URL    POST target for needs_human knocks (unset = no notifications)
    BURROW_NOTIFY_TOKEN  optional bearer token for that target (e.g. a private ntfy topic)
    BURROW_NOTIFY_TIMEOUT  seconds to wait on the webhook (default 5)
"""
import collections
import datetime
import email.header
import fcntl
import hmac
import http.server
import json
import os
import sys
import threading
import time
import urllib.request
import urllib.parse

import residents as resident_manifests

PORT = int(sys.argv[1]) if len(sys.argv) > 1 and sys.argv[1].isdigit() else 8737
HOST = os.environ.get("BURROW_HOST", "127.0.0.1")
ROOT = os.path.dirname(os.path.abspath(__file__))
EVENTS = os.environ.get("BURROW_EVENTS") or os.path.expanduser("~/.burrow/events.jsonl")

MAX_EVENT_BYTES = 64 * 1024
VILLAGERS_DIR = os.environ.get("BURROW_VILLAGERS") or os.path.join(ROOT, "villagers")
TOKEN = (os.environ.get("BURROW_TOKEN") or "").strip()
ARCHIVE_DIR = os.environ.get("BURROW_ARCHIVE") or ""
MAX_LOG_BYTES = int(os.environ.get("BURROW_MAX_LOG") or 5 * 1024 * 1024)

# the viewer's reduction rules, mirrored here so a rotated log still projects to
# exactly the same village (see docs/protocol.md and viewer/index.html)
EVENT_TYPES = {"task_started", "tool_called", "artifact_produced",
               "heartbeat", "needs_human", "idle", "session_ended"}
KEEP_PER_AGENT = 80                  # events the viewer keeps per villager
VIEWER_LINE_LIMIT = 4000             # viewer/index.html bootstrap window
DROP_MS = 12 * 60 * 60 * 1000        # villagers quiet longer than this are gone

# every read, append and rotation of the log goes through this, so an event can
# never land in the gap between reading the log and swapping it out
LOG_LOCK = threading.Lock()
_rotate_floor = 0                    # don't re-check until the log grows past this
_log_generation = 0                 # changes when rotation rewrites the live inode

NOTIFY_URL = (os.environ.get("BURROW_NOTIFY_URL") or "").strip()
NOTIFY_TOKEN = (os.environ.get("BURROW_NOTIFY_TOKEN") or "").strip()
try:
    NOTIFY_TIMEOUT = float(os.environ.get("BURROW_NOTIFY_TIMEOUT") or 5)
except ValueError:
    NOTIFY_TIMEOUT = 5.0
NOTIFY_MEMORY = 512      # how many knocks we remember, to not knock twice
DROP_SECONDS = 12 * 60 * 60
VIEWER_EVENT_TYPES = {"task_started", "tool_called", "artifact_produced",
                      "needs_human", "idle", "session_ended"}


CTYPES = {".html": "text/html; charset=utf-8", ".js": "text/javascript",
          ".css": "text/css", ".png": "image/png", ".json": "application/json"}


def read_villagers():
    """Validated residents plus legacy soul files for v0 client compatibility."""
    out = read_residents()["residents"]
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


def read_residents():
    """Load valid resident declarations and actionable validation diagnostics."""
    return resident_manifests.load_resident_manifests(VILLAGERS_DIR)


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
_notifying = set()
_notified_lock = threading.Lock()


def js_hash(s):
    """The viewer's hashCode, verbatim, so a nameless villager is called the same
    thing in the notification as it is on screen."""
    h = 0
    encoded = s.encode("utf-16-be", errors="surrogatepass")
    for index in range(0, len(encoded), 2):
        code_unit = int.from_bytes(encoded[index:index + 2], "big")
        h = (h * 31 + code_unit) & 0xFFFFFFFF
    if h >= 0x80000000:
        h -= 0x100000000
    return abs(h)


def villager_names(events):
    """Resolve names for a fleet exactly as the viewer does.

    Souls and fallback names are unique within the fleet, so resolving an event in
    isolation can disagree with the name on screen.
    """
    latest = {}
    parent_by_agent = {}
    for event in events:
        if isinstance(event, dict) and event.get("agent_id"):
            agent_id = str(event["agent_id"])
            latest[agent_id] = event
            payload = event.get("payload") or {}
            if isinstance(payload, dict) and payload.get("parent_agent_id"):
                parent_by_agent[agent_id] = str(payload["parent_agent_id"])

    soul_by_agent = {}
    soul_by_project = {}

    def is_resident(soul):
        return (soul.get("valid") is True
                and soul.get("manifest_version") == 1
                and type(soul.get("home")) is int)

    def index_soul(index, key, soul):
        current = index.get(key)
        if current is None or is_resident(soul) or not is_resident(current):
            index[key] = soul

    for soul in read_villagers():
        meta = soul.get("meta") or {}
        if meta.get("agent_id"):
            index_soul(soul_by_agent, meta["agent_id"], soul)
        if meta.get("project"):
            index_soul(soul_by_project, meta["project"], soul)

    names = {}
    used_souls = set()
    taken_names = set()
    assigned = {}
    # Exact identities are reserved first, independently of lexical event order.
    for agent_id in sorted(latest):
        if latest[agent_id].get("type") == "session_ended":
            continue
        soul = soul_by_agent.get(agent_id)
        soul_key = soul and soul.get("file")
        if soul and soul_key not in used_souls:
            assigned[agent_id] = soul
            used_souls.add(soul_key)
    for agent_id in sorted(latest):
        if agent_id in assigned or latest[agent_id].get("type") == "session_ended":
            continue
        if agent_id in parent_by_agent:
            continue
        project = str(latest[agent_id].get("project") or "unknown")
        soul = soul_by_project.get(project)
        soul_key = soul and soul.get("file")
        if soul and soul_key not in used_souls:
            assigned[agent_id] = soul
            used_souls.add(soul_key)
    for agent_id in sorted(latest):
        event = latest[agent_id]
        if event.get("type") == "session_ended":
            continue
        project = str(event.get("project") or "unknown")
        soul = assigned.get(agent_id)

        h = js_hash(agent_id)
        offset = 0
        while (taken_names and NAMES[(h + offset) % len(NAMES)] in taken_names
               and offset < len(NAMES)):
            offset += 1
        name = NAMES[(h + offset) % len(NAMES)]
        if soul and (soul.get("meta") or {}).get("name"):
            name = soul["meta"]["name"]
        taken_names.add(name)
        names[agent_id] = name
    return names


def _fleet_events(event):
    """Read the same bounded event window as the viewer and include this event."""
    events = []
    try:
        with open(EVENTS, encoding="utf-8") as stream:
            lines = collections.deque(stream, maxlen=4000)
        for line in lines:
            try:
                parsed = json.loads(line)
            except (TypeError, ValueError):
                continue
            if (isinstance(parsed, dict) and parsed.get("agent_id")
                    and parsed.get("type") in VIEWER_EVENT_TYPES):
                events.append(parsed)
    except (OSError, UnicodeDecodeError):
        pass
    events.append(event)
    latest = {str(item["agent_id"]): item for item in events}
    visible_agents = set()
    for agent_id, item in latest.items():
        try:
            timestamp = str(item.get("ts") or "").replace("Z", "+00:00")
            event_time = datetime.datetime.fromisoformat(timestamp).timestamp()
        except (TypeError, ValueError):
            event_time = 0
        if item is event or time.time() - event_time <= DROP_SECONDS:
            visible_agents.add(agent_id)
    return [item for item in events if str(item["agent_id"]) in visible_agents]


def villager_name(event):
    agent_id = str(event.get("agent_id") or "")
    return villager_names(_fleet_events(event)).get(
        agent_id, NAMES[js_hash(agent_id) % len(NAMES)])


def knock_key(event):
    payload = event.get("payload") or {}
    return "\x00".join(str(x) for x in (
        event.get("agent_id"), event.get("ts"),
        payload.get("message") if isinstance(payload, dict) else ""))


def claim_knock(event):
    """Claim a knock unless it is in flight or has already been delivered."""
    if not NOTIFY_URL or event.get("type") != "needs_human":
        return False
    key = knock_key(event)
    with _notified_lock:
        if key in _notified or key in _notifying:
            if key in _notified:
                _notified.move_to_end(key)
            return False
        _notifying.add(key)
    return True


def finish_knock(event, delivered):
    """Release an attempt, remembering only successful deliveries."""
    key = knock_key(event)
    with _notified_lock:
        _notifying.discard(key)
        if delivered:
            _notified[key] = True
            _notified.move_to_end(key)
            while len(_notified) > NOTIFY_MEMORY:
                _notified.popitem(last=False)


def notify(event):
    """POST one knock and return whether it was delivered. Never raises."""
    try:
        payload = event.get("payload") or {}
        message = payload.get("message", "") if isinstance(payload, dict) else ""
        if not isinstance(message, str):
            message = str(message)
        name = villager_name(event)
        project = str(event.get("project") or "unknown")
        title = f"{name} is at your door ({project})"
        if not title.isascii():
            title = email.header.Header(
                title, charset="utf-8", maxlinelen=0).encode()
        headers = {
            "Content-Type": "text/plain; charset=utf-8",
            "Title": title,
            "Tags": "door",
            "Priority": "high",
        }
        if NOTIFY_TOKEN:
            headers["Authorization"] = "Bearer " + NOTIFY_TOKEN
        body = f"{name} · {project}\n{message}".encode("utf-8")
        req = urllib.request.Request(NOTIFY_URL, data=body, headers=headers, method="POST")
        with urllib.request.urlopen(req, timeout=NOTIFY_TIMEOUT):
            pass
        return True
    except Exception:
        return False


def deliver_knock(event):
    finish_knock(event, notify(event))


def notify_async(event):
    threading.Thread(target=deliver_knock, args=(event,), daemon=True).start()
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
    # The browser deliberately ignores anything older than this global window.
    # Applying it here too prevents rotation from resurrecting a sparse agent.
    lines = lines[-VIEWER_LINE_LIMIT:]
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
        agent = per_agent.setdefault(
            event["agent_id"], {"events": [], "last": None, "lineage": None})
        agent["last"] = (i, event)
        payload = event.get("payload")
        if isinstance(payload, dict) and payload.get("parent_agent_id"):
            agent["lineage"] = (i, event)
        # Heartbeat is liveness-only in the projection. It must survive when it
        # is latest, but must not consume one of the 80 visible-history slots.
        if event["type"] != "heartbeat":
            agent["events"].append((i, event))
            if len(agent["events"]) > KEEP_PER_AGENT:
                agent["events"].pop(0)
    keep = []
    for agent in per_agent.values():
        last_i, last = agent["last"]
        if last["type"] == "session_ended" or now_ms - event_ms(last) > DROP_MS:
            continue
        keep.extend(i for i, _ in agent["events"])
        if agent["lineage"]:
            lineage_i, _ = agent["lineage"]
            if lineage_i not in keep:
                keep.append(lineage_i)
        if last["type"] == "heartbeat":
            keep.append(last_i)
    return [lines[i] for i in sorted(set(keep))]


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
    global _rotate_floor, _log_generation
    # Keep the inode: local emitters may already have EVENTS open for append.
    # An inode swap strands such descriptors in the archive. Advisory locking
    # coordinates the bundled emitter, while retaining the inode also makes a
    # descriptor that writes after rotation append to the new live contents.
    with open(EVENTS, "r+b") as live:
        fcntl.flock(live, fcntl.LOCK_EX)
        original = live.read()
        lines = original.decode("utf-8", errors="replace").splitlines()
        tail = carry_forward(lines, int(time.time() * 1000))
        data = "".join(line + "\n" for line in tail).encode("utf-8")
        size = len(original)
        if len(data) > size * 9 // 10:
            _rotate_floor = size + max(MAX_LOG_BYTES // 10, 1)
            return None
        os.makedirs(archive_dir(), exist_ok=True)
        archive = archive_path()
        with open(archive, "xb") as archived:
            archived.write(original)
            archived.flush()
            os.fsync(archived.fileno())
        live.seek(0)
        live.write(data)
        live.truncate()
        live.flush()
        os.fsync(live.fileno())
        _log_generation += 1
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
            fcntl.flock(f, fcntl.LOCK_EX)
            f.write(line)
            f.flush()
        maybe_rotate()


class Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _authorized(self):
        if not TOKEN:
            return True
        presented = self.headers.get("X-Burrow-Token") or ""
        scheme, _, value = (self.headers.get("Authorization") or "").partition(" ")
        if scheme.lower() == "bearer":
            presented = value.strip() or presented
        return hmac.compare_digest(presented, TOKEN)

    def do_GET(self):
        parsed = urllib.parse.urlsplit(self.path)
        path = parsed.path
        if path == "/villagers":
            self._send(200, json.dumps(read_villagers()).encode("utf-8"),
                       "application/json")
            return
        if path == "/residents":
            self._send(200, json.dumps(read_residents()).encode("utf-8"),
                       "application/json")
            return
        if path == "/events":
            params = urllib.parse.parse_qs(parsed.query, keep_blank_values=True)
            raw_since = params.get("since", ["0"])[0]
            try:
                cursor_identity, since = self._parse_cursor(raw_since)
            except (TypeError, ValueError):
                self._send(400, b"invalid since cursor", "text/plain")
                return

            records, identity, cursor, reset = self._read_event_records(
                cursor_identity, since)
            data = b"".join(line for _, line in records)

            headers = {"X-Burrow-Cursor": self._format_cursor(identity, cursor)}
            if reset:
                headers["X-Burrow-Reset"] = "1"
            self._send(200, data, "application/x-ndjson", headers)
            return
        if path == "/events/stream":
            self._stream_events(parsed)
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
        if not self._authorized():
            try:
                pending = min(int(self.headers.get("Content-Length") or 0), MAX_EVENT_BYTES)
                if pending > 0:
                    self.rfile.read(pending)
            except (ValueError, OSError):
                pass
            self._send(401, b"unauthorized", "text/plain")
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
        if claim_knock(event):
            notify_async(event)
        self._send(204, b"", "text/plain")

    def _send_file(self, path, ctype):
        try:
            with open(path, "rb") as f:
                self._send(200, f.read(), ctype)
        except OSError:
            self._send(404, b"missing: " + path.encode(), "text/plain")

    @staticmethod
    def _parse_cursor(raw):
        parts = raw.split(":")
        if len(parts) == 4:
            identity = (int(parts[0]), int(parts[1]), int(parts[2]))
            offset = int(parts[3])
        elif len(parts) == 3:  # cursor issued before rotation generations existed
            identity, offset = (int(parts[0]), int(parts[1])), int(parts[2])
        elif len(parts) == 1:
            identity, offset = None, int(raw)
        else:
            raise ValueError
        if offset < 0:
            raise ValueError
        return identity, offset

    @staticmethod
    def _format_cursor(identity, offset):
        if not identity or not offset:
            return "0"
        return ":".join(str(part) for part in (*identity, offset))

    @staticmethod
    def _read_event_records(cursor_identity, offset):
        """Read complete records once for both polling and SSE transports."""
        records, reset = [], False
        with LOG_LOCK:
            maybe_rotate()
            try:
                with open(EVENTS, "rb") as stream:
                    stat = os.fstat(stream.fileno())
                    identity = (stat.st_dev, stat.st_ino, _log_generation)
                    if offset > stat.st_size or (
                            cursor_identity is not None and cursor_identity != identity):
                        offset, reset = 0, True
                    stream.seek(offset)
                    chunk = stream.read()
                    end = chunk.rfind(b"\n") + 1
                    for line in chunk[:end].splitlines(keepends=True):
                        offset += len(line)
                        records.append((offset, line))
                    return records, identity, offset, reset
            except FileNotFoundError:
                return records, None, 0, offset > 0

    def _stream_events(self, parsed):
        """Tail complete JSONL records as SSE messages.

        Each message id is the same inode-aware byte cursor used by GET /events.
        That makes Last-Event-ID and the polling fallback interchangeable.
        """
        params = urllib.parse.parse_qs(parsed.query, keep_blank_values=True)
        raw_cursor = (self.headers.get("Last-Event-ID")
                      or params.get("since", ["0"])[0])
        try:
            identity, offset = self._parse_cursor(raw_cursor)
        except (TypeError, ValueError):
            self._send(400, b"invalid event cursor", "text/plain")
            return

        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache, no-store")
        self.send_header("Connection", "keep-alive")
        # nginx honours this even when proxy_buffering is enabled globally.
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()

        last_keepalive = time.monotonic()
        try:
            while True:
                records, identity, offset, reset = self._read_event_records(identity, offset)
                if reset:
                    self.wfile.write(b"event: reset\ndata: {}\n\n")
                for record_offset, line in records:
                    event_id = self._format_cursor(identity, record_offset)
                    self.wfile.write(b"id: " + event_id.encode("ascii") + b"\n")
                    self.wfile.write(b"data: " + line.rstrip(b"\r\n") + b"\n\n")
                now = time.monotonic()
                if records or reset or now - last_keepalive >= 15:
                    if not records and not reset:
                        self.wfile.write(b": keepalive\n\n")
                    self.wfile.flush()
                    last_keepalive = now
                time.sleep(0.1)
        except (BrokenPipeError, ConnectionResetError, OSError):
            return

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
    print(f"burrow village at http://{HOST}:{PORT}, log at {EVENTS}")
    if NOTIFY_URL:
        print(f"knocks will be pushed to {NOTIFY_URL}")
    if MAX_LOG_BYTES > 0:
        print(f"rotating past {MAX_LOG_BYTES} bytes into {archive_dir()}")
    http.server.ThreadingHTTPServer((HOST, PORT), Handler).serve_forever()
