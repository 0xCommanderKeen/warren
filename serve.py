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

GET /transport/status exposes bounded ingest-deduplication and knock-forwarding
pressure for the browser's live transport status.
"""
import collections
import dataclasses
import datetime
import email.header
import errno
import fcntl
import glob
import hmac
import http.server
import json
import os
import queue
import re
import secrets
import sys
import threading
import time
import urllib.parse
import urllib.request
import weakref

import notification_persistence
import residents as resident_manifests
import retention
from state_coordinator import StateCoordinator
from approval_protocol import structured_approval, thaw_json
from hooks import durable
from protocol import validate_event

PORT = int(sys.argv[1]) if len(sys.argv) > 1 and sys.argv[1].isdigit() else 8737
HOST = os.environ.get("BURROW_HOST", "127.0.0.1")
ROOT = os.path.dirname(os.path.abspath(__file__))
EVENTS = os.environ.get("BURROW_EVENTS") or os.path.expanduser("~/.burrow/events.jsonl")

MAX_EVENT_BYTES = 64 * 1024
VILLAGERS_DIR = os.environ.get("BURROW_VILLAGERS") or os.path.join(ROOT, "villagers")
TOKEN = (os.environ.get("BURROW_TOKEN") or "").strip()
ARCHIVE_DIR = os.environ.get("BURROW_ARCHIVE") or ""
MAX_LOG_BYTES = int(os.environ.get("BURROW_MAX_LOG") or 5 * 1024 * 1024)

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
NOTIFY_WORKERS = 2
NOTIFY_QUEUE = 64
KNOCK_RECORDS = int(os.environ.get("BURROW_KNOCK_RECORDS") or 1024)
KNOCK_BYTES = int(os.environ.get("BURROW_KNOCK_BYTES") or 5 * 1024 * 1024)
LEDGER_RECORDS = KNOCK_RECORDS
LEDGER_BYTES = KNOCK_BYTES
KNOCK_LOCK_SHARDS = 32
LEDGER_DELIVERY_IDS = notification_persistence.DELIVERY_IDS
LEDGER_KNOCKS = notification_persistence.KNOCKS
LEDGER_NOTIFIED = notification_persistence.NOTIFIED
LEDGER_NOTIFY_DROPPED = notification_persistence.DROPPED
LEDGER_KINDS = notification_persistence.KINDS
DROP_SECONDS = 12 * 60 * 60
VIEWER_EVENT_TYPES = {"task_started", "tool_called", "tool_failed",
                      "artifact_produced", "needs_human", "idle",
                      "session_ended"}


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
_knock_queue = queue.Queue(maxsize=NOTIFY_QUEUE)
_knock_workers_started = False
_knock_workers_lock = threading.Lock()
_transport_lock = threading.Lock()
_transport_counters = {
    "ingest_duplicates": 0, "notify_delivered": 0,
    "notify_failed": 0, "notify_retried": 0, "notify_saturated": 0,
    "notify_dropped": 0,
}
_notification_store = notification_persistence.NotificationPersistence(
    lambda: EVENTS, lambda: (KNOCK_RECORDS, KNOCK_BYTES), KNOCK_LOCK_SHARDS,
    ledger_limits=lambda: (LEDGER_RECORDS, LEDGER_BYTES))
_delivery_ids_by_log = _notification_store.caches[LEDGER_DELIVERY_IDS]
_notified_by_log = _notification_store.caches[LEDGER_NOTIFIED]
_dropped_by_log = _notification_store.caches[LEDGER_NOTIFY_DROPPED]
_knocks_by_log = _notification_store.caches[LEDGER_KNOCKS]
_knock_journal_lock = _notification_store.journal_lock
_ledger_lock = _notification_store.ledger_lock
_knock_attempts = _notification_store.attempts
_knock_attempts_lock = _notification_store.attempts_lock
_delivery_id_pattern = re.compile(r"^[A-Za-z0-9_-]{16,128}$")


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
            lines = collections.deque(
                stream, maxlen=retention.POLICY["viewer_line_limit"])
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
    return notification_persistence.knock_key(event)


def terminal_knock_key(event):
    """Fixed-size non-sensitive key for every durable terminal boundary."""
    return notification_persistence.terminal_key(event)


def terminal_knock_keys(event):
    """Current identity plus only migration aliases proven to be exact."""
    return notification_persistence.terminal_keys(event)


def _ledger_path(kind):
    return _notification_store.ledger_path(kind)


def _notification_lock_path(shard):
    return _notification_store.notification_lock_path(shard)


def _load_ledger(kind, cache):
    return _notification_store.load_ledger(kind, cache)


def _remember_durable_batch(kind, cache, keys, preserve_existing=()):
    """Atomically retain a bounded ordered batch under a stable process lock.

    A successful batch may evict the oldest prior entries, matching single-key
    ledger retention.  The complete requested batch and any named keys that
    already exist must remain represented; otherwise the authoritative file
    and cache are left unchanged.
    """
    return _notification_store.remember_batch(
        kind, keys, preserve_existing=preserve_existing, cache=cache)


def _remember_durable(kind, cache, key):
    """Atomically retain one key in a bounded ordered durable ledger."""
    _notification_store.remember(kind, key, cache)


def _ledger_contains(kind, key):
    """Read terminal authority afresh; process caches are never authoritative."""
    return _notification_store.contains(kind, key)


def _knock_delivery_lock(key):
    return _notification_store.delivery_lock_path(key)


def receiver_delivery_id(event):
    """Stable non-sensitive ASCII projection of the internal knock identity."""
    return terminal_knock_key(event)


def _fsync_parent(path):
    durable.fsync_parent(path)


def _knock_journal_paths(path):
    return _notification_store.journal_paths(path)


def _read_knock_keys(path):
    return _notification_store.read_journal_keys(path)


def _compact_knocks_locked(path, addition=None):
    """Publish one bounded latest-state generation while ``path.lock`` is held.

    Capacity victims receive a durable terminal-drop entry before the compacted
    authority is published, so a crash or restart cannot make them eligible.
    """
    return _notification_store.compact_locked(path, addition)


def _publish_knock_compaction(path, lines):
    """Durably replace journal authority while its stable lock is held."""
    return _notification_store.publish_compaction(path, lines)


def _commit_knock_terminal(event, kind):
    """Commit terminal outcome while preserving every retained source."""
    return _notification_store.commit_terminal(event, kind)


def persist_knock(event):
    """Durably journal notification work before the ingest acknowledges it."""
    if not NOTIFY_URL or event.get("type") != "needs_human":
        return True
    return _notification_store.journal(event)


def _persist_knock_attempt(event, attempts):
    """Append a durable retry-state transition to the knock journal."""
    return _notification_store.record_attempt(event, attempts)


def claim_knock(event):
    """Claim a knock unless it is in flight or has already been delivered."""
    if not NOTIFY_URL or event.get("type") != "needs_human":
        return False
    key = terminal_knock_key(event)
    with _notified_lock:
        delivered = _load_ledger(LEDGER_NOTIFIED, _notified_by_log)
        dropped = _load_ledger(LEDGER_NOTIFY_DROPPED, _dropped_by_log)
        if any(candidate in delivered or candidate in dropped
               for candidate in terminal_knock_keys(event)):
            return False
        if key in _notified or key in _notifying:
            if key in _notified:
                _notified.move_to_end(key)
            return False
        _notifying.add(key)
    return True


def finish_knock(event, delivered):
    """Release an attempt, remembering only successful deliveries."""
    key = terminal_knock_key(event)
    with _notified_lock:
        _notifying.discard(key)
        if delivered:
            if not _commit_knock_terminal(event, LEDGER_NOTIFIED):
                # Without a durable acknowledgement the event remains eligible
                # for recovery; never pretend volatile success is final.
                return False
            _notified[key] = True
            _notified.move_to_end(key)
            while len(_notified) > NOTIFY_MEMORY:
                _notified.popitem(last=False)
            return True
    return not delivered


def notify(event):
    """POST one knock and return whether it was delivered. Never raises."""
    try:
        payload = event.get("payload") or {}
        message = payload.get("message", "") if isinstance(payload, dict) else ""
        if not isinstance(message, str):
            message = str(message)
        name = villager_name(event)
        project = str(event.get("project") or "unknown")
        structured = structured_approval(event)
        title = structured.action if structured else f"{name} is at your door ({project})"
        if not title.isascii():
            title = email.header.Header(
                title, charset="utf-8", maxlinelen=0).encode()
        # Receiver IDs hash the internal identity; structured fallbacks include
        # request_id so distinct same-millisecond requests remain distinct.
        headers = {
            "Content-Type": "text/plain; charset=utf-8",
            "Title": title,
            "Tags": "door",
            "Priority": "high",
            "X-Burrow-Delivery-ID": receiver_delivery_id(event),
        }
        if NOTIFY_TOKEN:
            headers["Authorization"] = "Bearer " + NOTIFY_TOKEN
        if structured:
            detail = thaw_json(structured.detail)
            body_text = json.dumps(detail, ensure_ascii=False, sort_keys=True)
        else:
            body_text = f"{name} · {project}\n{message}"
        body = body_text.encode("utf-8")
        req = urllib.request.Request(NOTIFY_URL, data=body, headers=headers, method="POST")
        with urllib.request.urlopen(req, timeout=NOTIFY_TIMEOUT):
            pass
        return True
    except Exception:
        return False


def deliver_knock(event):
    """Attempt under a bounded cross-process claim and commit its outcome.

    The stable shard is held across terminal-ledger recheck, external POST, and
    durable outcome. The shard suppresses concurrent duplicates; a receiver
    acceptance followed by a process crash can still cause a later retry.
    """
    key = terminal_knock_key(event)
    path = _knock_delivery_lock(key)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if any(_ledger_contains(LEDGER_NOTIFIED, candidate)
               or _ledger_contains(LEDGER_NOTIFY_DROPPED, candidate)
               for candidate in terminal_knock_keys(event)):
            finish_knock(event, False)
            return True
        delivered = notify(event)
        if delivered:
            if not _commit_knock_terminal(event, LEDGER_NOTIFIED):
                delivered = False
        if not delivered:
            with _knock_attempts_lock:
                next_attempt = _knock_attempts.get(key, 0) + 1
            _persist_knock_attempt(event, next_attempt)
        finish_knock(event, False)
    with _transport_lock:
        key = "notify_delivered" if delivered else "notify_failed"
        _transport_counters[key] += 1
    return delivered


def notify_async(event):
    ensure_knock_workers()
    try:
        _knock_queue.put_nowait(event)
        return True
    except queue.Full:
        finish_knock(event, False)
        with _transport_lock:
            _transport_counters["notify_saturated"] += 1
        return False


def _process_knock(event):
    key = terminal_knock_key(event)
    with _knock_attempts_lock:
        prior_attempts = _knock_attempts.get(key, 0)
    if prior_attempts >= 3:
        if not _commit_knock_terminal(event, LEDGER_NOTIFY_DROPPED):
            finish_knock(event, False)
            return
        finish_knock(event, False)
        with _knock_attempts_lock:
            _knock_attempts.pop(key, None)
        _recover_knocks()
        return

    delivered = deliver_knock(event)
    if delivered:
        with _knock_attempts_lock:
            _knock_attempts.pop(key, None)
    else:
        with _knock_attempts_lock:
            attempts = _knock_attempts.get(key, 0) + 1
            _knock_attempts[key] = attempts
        durable_attempt = _persist_knock_attempt(event, attempts)
        if attempts < 3 and durable_attempt and claim_knock(event):
            try:
                _knock_queue.put_nowait(event)
                with _transport_lock:
                    _transport_counters["notify_retried"] += 1
            except queue.Full:
                finish_knock(event, False)
                with _transport_lock:
                    _transport_counters["notify_saturated"] += 1
        elif attempts >= 3 and durable_attempt:
            if not _commit_knock_terminal(event, LEDGER_NOTIFY_DROPPED):
                return
            with _knock_attempts_lock:
                _knock_attempts.pop(key, None)
        elif not durable_attempt:
            return
    _recover_knocks()


def _knock_worker():
    while True:
        event = _knock_queue.get()
        try:
            _process_knock(event)
        finally:
            _knock_queue.task_done()


def _recover_knocks():
    """Atomically hand off new work and replay immutable generations.

    Replay files remain until their keys have durable delivered/drop outcomes.
    Re-reading them after a crash is safe because ``claim_knock`` consults those
    durable ledgers and the in-flight set before queueing.
    """
    for generation, complete, events in _notification_store.recover():
        for event in events:
            if not claim_knock(event):
                continue
            try:
                _knock_queue.put_nowait(event)
            except queue.Full:
                finish_knock(event, False)
                with _transport_lock:
                    _transport_counters["notify_saturated"] += 1
                return
        try:
            _notification_store.retire_replay_if_terminal(
                generation, complete, events)
        except OSError:
            # Retirement failure leaves replay authority for the next recovery.
            pass


def ensure_knock_workers():
    global _knock_workers_started
    if _knock_workers_started:
        return
    with _knock_workers_lock:
        if _knock_workers_started:
            return
        for index in range(NOTIFY_WORKERS):
            threading.Thread(target=_knock_worker,
                             name=f"burrow-knock-{index}", daemon=True).start()
        _knock_workers_started = True
        _recover_knocks()


class BurrowHTTPServer(http.server.ThreadingHTTPServer):
    """A process-bound server lifecycle, including its cursor namespace.

    Construction establishes the parent's namespace.  If the listening server
    is inherited across fork, the child hook replaces both the identity and its
    lock before child code or handler threads can use either.  PID checks at
    serving and cursor boundaries are a defensive fallback; serving refreshes
    before ThreadingHTTPServer is allowed to create child handler threads.
    """

    def __init__(self, *args, **kwargs):
        self._boot_id_lock = threading.Lock()
        self._boot_id_pid = os.getpid()
        self._boot_id = secrets.token_hex(16)
        super().__init__(*args, **kwargs)
        self.state_coordinator = StateCoordinator(
            self._projection_inputs,
            read_residents,
            capabilities={"ingest": True, "approvals": True, "jobs": True,
                          "routines": True},
        )
        self.state_coordinator.evaluate()
        if hasattr(os, "register_at_fork"):
            server_ref = weakref.ref(self)

            def refresh_in_child():
                server = server_ref()
                if server is not None:
                    server._refresh_process_identity()

            # register_at_fork has no unregister API.  Its registry retains only
            # this closure and the closure retains the server weakly.
            self._refresh_in_child = refresh_in_child
            os.register_at_fork(after_in_child=refresh_in_child)

    def _projection_inputs(self):
        """Return one log/cursor boundary for the pure projection seam."""
        with LOG_LOCK:
            maybe_rotate()
            events = []
            try:
                with open(EVENTS, "rb") as stream:
                    stat = os.fstat(stream.fileno())
                    for line in stream:
                        try:
                            events.append(json.loads(line, parse_constant=_reject_json_constant))
                        except (UnicodeDecodeError, json.JSONDecodeError):
                            events.append(None)
                    cursor = EventCursor.issued(
                        self.boot_id, stat, _log_generation, stat.st_size).format()
            except FileNotFoundError:
                cursor = EventCursor.issued(
                    self.boot_id, None, _log_generation, 0).format()
            return events, cursor, _log_generation

    def _refresh_process_identity(self):
        # Never acquire a lock copied from a multi-threaded parent: its owner no
        # longer exists in the child.  Publish the replacement lock before the
        # new identity.
        self._boot_id_lock = threading.Lock()
        with self._boot_id_lock:
            self._boot_id = secrets.token_hex(16)
            self._boot_id_pid = os.getpid()

    def _ensure_process_identity(self):
        if self._boot_id_pid != os.getpid():
            # On fork-capable Python the child hook runs synchronously while the
            # child still has one thread.  This fallback is also reached before
            # serve_forever/handle_request can create handler threads.
            self._refresh_process_identity()

    @property
    def boot_id(self):
        self._ensure_process_identity()
        with self._boot_id_lock:
            return self._boot_id

    def serve_forever(self, *args, **kwargs):
        self._ensure_process_identity()
        return super().serve_forever(*args, **kwargs)

    def handle_request(self):
        self._ensure_process_identity()
        return super().handle_request()


def serve_forever():
    """Start background delivery before accepting requests, then serve."""
    if NOTIFY_URL:
        ensure_knock_workers()
    BurrowHTTPServer((HOST, PORT), Handler).serve_forever()


def transport_status():
    """Bounded machine-readable diagnostics for the browser live-status module."""
    with _transport_lock:
        counters = dict(_transport_counters)
    delivered, dropped = _notification_store.terminal_counts()
    return {
        "ingest": {"duplicates": counters["ingest_duplicates"],
                   "dedupe_window": None, "durable": True},
        "notifications": {
            "configured": bool(NOTIFY_URL),
            "queued": _knock_queue.qsize(), "queue_capacity": NOTIFY_QUEUE,
            "workers": NOTIFY_WORKERS,
            "delivered": delivered,
            "failed": counters["notify_failed"],
            "retried": counters["notify_retried"],
            "saturated": counters["notify_saturated"],
            "dropped": dropped,
        },
    }
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
        tail = retention.carry_forward(
            lines, int(time.time() * 1000), retention.POLICY).lines
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
        _fsync_parent(archive)
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
        with open(durable.lock_path(os.path.abspath(EVENTS)), "a+") as process_lock:
            fcntl.flock(process_lock, fcntl.LOCK_EX)
            delivery_id = event.get("delivery_id")
            remembered = _load_ledger(LEDGER_DELIVERY_IDS, _delivery_ids_by_log)
            if delivery_id and delivery_id in remembered:
                with _transport_lock:
                    _transport_counters["ingest_duplicates"] += 1
                return False
            if delivery_id and _event_log_has_delivery_id(delivery_id):
                # The event log is the canonical commit record. Repair a missing
                # acceleration ledger left by a crash after the event fsync.
                try:
                    _remember_durable(LEDGER_DELIVERY_IDS, _delivery_ids_by_log,
                                      delivery_id)
                except OSError:
                    pass
                with _transport_lock:
                    _transport_counters["ingest_duplicates"] += 1
                return False
            with open(EVENTS, "a", encoding="utf-8") as f:
                f.write(line)
                f.flush()
                os.fsync(f.fileno())
            _fsync_parent(EVENTS)
            if delivery_id:
                _remember_durable(LEDGER_DELIVERY_IDS, _delivery_ids_by_log,
                                  delivery_id)
            maybe_rotate()
            return True


def _event_log_has_delivery_id(delivery_id):
    paths = [EVENTS]
    base, ext = os.path.splitext(os.path.basename(EVENTS))
    paths.extend(sorted(glob.glob(os.path.join(archive_dir(), base + "-*" + ext))))
    for path in paths:
        try:
            with open(path, encoding="utf-8") as stream:
                for line in stream:
                    try:
                        event = json.loads(line)
                    except ValueError:
                        continue
                    if (isinstance(event, dict)
                            and event.get("delivery_id") == delivery_id):
                        return True
        except OSError:
            continue
    return False


def _reject_json_constant(value):
    raise json.JSONDecodeError("non-standard JSON constant", value, 0)


@dataclasses.dataclass(frozen=True)
class EventCursor:
    """Validated event-log position with explicit resume policy."""

    boot_id: str | None = None
    device: int = 0
    inode: int = 0
    generation: int = 0
    offset: int = 0
    reset_only: bool = False

    MAX_ENCODED_BYTES = 160
    MAX_INTEGER = (1 << 64) - 1

    @classmethod
    def initial(cls):
        return cls()

    @classmethod
    def parse(cls, raw):
        if not isinstance(raw, str) or not raw or len(raw) > cls.MAX_ENCODED_BYTES:
            raise ValueError
        parts = raw.split(":")
        if len(parts) == 6 and parts[0] == "v1":
            boot_id = parts[1]
            if not re.fullmatch(r"[0-9a-f]{32}", boot_id):
                raise ValueError
            values = cls._parse_integers(parts[2:])
            return cls(boot_id, *values)
        if len(parts) in (3, 4):
            values = cls._parse_integers(parts)
            return cls(offset=values[-1], reset_only=True)
        if len(parts) == 1:
            return cls(offset=cls._parse_integers(parts)[0], reset_only=True)
        raise ValueError

    @classmethod
    def _parse_integers(cls, fields):
        values = []
        for field in fields:
            if not field or len(field) > 20 or not field.isascii() or not field.isdigit():
                raise ValueError
            value = int(field)
            if value > cls.MAX_INTEGER:
                raise ValueError
            values.append(value)
        return values

    @classmethod
    def issued(cls, boot_id, stat, generation, offset):
        device, inode = (stat.st_dev, stat.st_ino) if stat is not None else (0, 0)
        return cls(boot_id, device, inode, generation, offset)

    def resume(self, current, size):
        """Return (offset, reset) against the current issued cursor identity."""
        if self.boot_id is None and not self.reset_only:
            return 0, False
        identity_matches = (
            self.boot_id == current.boot_id
            and self.device == current.device
            and self.inode == current.inode
            and self.generation == current.generation
        )
        if self.reset_only or not identity_matches or self.offset > size:
            return 0, True
        return self.offset, False

    def format(self):
        if self.boot_id is None:
            raise ValueError("only server-issued cursors can be formatted")
        return ":".join(str(part) for part in (
            "v1", self.boot_id, self.device, self.inode, self.generation,
            self.offset,
        ))


class Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    @staticmethod
    def _expected_disconnect(error):
        """Browser/proxy departure is normal; unrelated I/O failures are not."""
        return isinstance(error, (BrokenPipeError, ConnectionResetError,
                                  ConnectionAbortedError)) or getattr(
            error, "errno", None) in {errno.EPIPE, errno.ECONNRESET,
                                      errno.ECONNABORTED}

    def handle(self):
        # Disconnects can surface while BaseHTTPRequestHandler parses the next
        # keep-alive request, outside the SSE write loop.
        try:
            super().handle()
        except OSError as error:
            if not self._expected_disconnect(error):
                raise

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
        if path == "/state":
            params = urllib.parse.parse_qs(parsed.query, keep_blank_values=True)
            try:
                generation = int(params["generation"][0]) if "generation" in params else None
                cursor = params["cursor"][0] if "cursor" in params else None
                if generation is not None and generation < 0:
                    raise ValueError
            except (TypeError, ValueError):
                self._send(400, b"invalid state position", "text/plain")
                return
            snapshot = self.server.state_coordinator.evaluate()
            delivery = self.server.state_coordinator.delivery(generation, cursor)
            if delivery["kind"] == "unchanged":
                self._send(204, b"", "application/json", {
                    "X-Burrow-State-Generation": str(snapshot["generation"]),
                    "X-Burrow-State-Cursor": snapshot["cursor"],
                })
                return
            self._send(200, json.dumps(delivery, ensure_ascii=False,
                       separators=(",", ":"), allow_nan=False).encode("utf-8"),
                       "application/json")
            return
        if path == "/state/stream":
            self._stream_state(parsed)
            return
        if path == "/villagers":
            self._send(200, json.dumps(read_villagers()).encode("utf-8"),
                       "application/json")
            return
        if path == "/residents":
            self._send(200, json.dumps(read_residents()).encode("utf-8"),
                       "application/json")
            return
        if path == "/transport/status":
            self._send(200, json.dumps(transport_status()).encode("utf-8"),
                       "application/json")
            return
        if path == "/retention-policy.json":
            self._send_file(os.path.join(ROOT, "retention-policy.json"),
                            "application/json")
            return
        if path == "/events":
            params = urllib.parse.parse_qs(parsed.query, keep_blank_values=True)
            try:
                cursor = (EventCursor.parse(params["since"][0])
                          if "since" in params else EventCursor.initial())
            except (TypeError, ValueError):
                self._send(400, b"invalid since cursor", "text/plain")
                return

            records, cursor, reset = self._read_event_records(cursor)
            data = b"".join(line for _, line in records)

            headers = {"X-Burrow-Cursor": cursor.format()}
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
        # Validate framing before authorization. Otherwise malformed or
        # conflicting framing gets a credential-dependent response and can
        # leave unread bytes to be mistaken for a keep-alive request.
        length = self._event_content_length(send_error=True)
        if length is None:
            return
        if not self._authorized():
            try:
                self.rfile.read(length)
            except OSError:
                self.close_connection = True
            self._send(401, b"unauthorized", "text/plain")
            return
        body = self.rfile.read(length)
        try:
            event = json.loads(body, parse_constant=_reject_json_constant)
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._send(400, b"not a protocol event", "text/plain")
            return
        error = validate_event(event)
        if error:
            self._send(400, error.encode("ascii"), "text/plain")
            return
        # Delivery identity is transport metadata owned by this adapter. Ignore
        # any same-named body extension so only the authenticated header can
        # participate in deduplication.
        event = dict(event)
        event.pop("delivery_id", None)
        delivery_id = (self.headers.get("X-Burrow-Delivery-ID") or "").strip()
        if delivery_id:
            if not _delivery_id_pattern.fullmatch(delivery_id):
                self._send(400, b"invalid delivery id", "text/plain")
                return
            event["delivery_id"] = delivery_id
        append_event(event)
        if not persist_knock(event):
            self._send(503, b"notification queue unavailable", "text/plain")
            return
        if claim_knock(event):
            notify_async(event)
        coordinator = getattr(self.server, "state_coordinator", None)
        if coordinator is not None:
            coordinator.evaluate()
        self._send(204, b"", "text/plain")

    def _stream_state(self, parsed):
        params = urllib.parse.parse_qs(parsed.query, keep_blank_values=True)
        try:
            generation = int(params.get("generation", ["0"])[0])
            cursor = params.get("cursor", [None])[0]
        except ValueError:
            self._send(400, b"invalid state generation", "text/plain")
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache, no-store")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()
        try:
            while True:
                snapshot = self.server.state_coordinator.evaluate()
                delivery = self.server.state_coordinator.delivery(generation, cursor)
                if delivery["kind"] in {"snapshot", "reset"}:
                    payload = json.dumps(delivery,
                                         ensure_ascii=False, separators=(",", ":"), allow_nan=False)
                    self.wfile.write(f"id: {snapshot['generation']}\nevent: snapshot\ndata: {payload}\n\n".encode())
                    self.wfile.flush()
                    generation = snapshot["generation"]
                    cursor = snapshot["cursor"]
                self.server.state_coordinator.wait_for_newer(generation, 15)
                self.wfile.write(b": keepalive\n\n")
                self.wfile.flush()
        except OSError as error:
            if not self._expected_disconnect(error):
                raise

    def _event_content_length(self, send_error=False):
        """Parse one unambiguous request length or close the connection.

        An invalid/oversized length cannot be drained safely: treating bytes as
        a following keep-alive request would desynchronise the HTTP stream.
        """
        if self.headers.get_all("Transfer-Encoding", failobj=[]):
            self.close_connection = True
            if send_error:
                self._send(400, b"unsupported transfer encoding", "text/plain",
                           {"Connection": "close"})
            return None
        values = self.headers.get_all("Content-Length", failobj=[])
        if len(values) != 1 or not values[0].isascii() or not values[0].isdigit():
            self.close_connection = True
            if send_error:
                self._send(400, b"invalid content length", "text/plain",
                           {"Connection": "close"})
            return None
        length = int(values[0])
        if length <= 0:
            self.close_connection = True
            if send_error:
                self._send(400, b"invalid content length", "text/plain",
                           {"Connection": "close"})
            return None
        if length > MAX_EVENT_BYTES:
            self.close_connection = True
            if send_error:
                self._send(413, b"event too large", "text/plain",
                           {"Connection": "close"})
            return None
        return length

    def _send_file(self, path, ctype):
        try:
            with open(path, "rb") as f:
                self._send(200, f.read(), ctype)
        except OSError:
            self._send(404, b"missing: " + path.encode(), "text/plain")

    def _read_event_records(self, cursor):
        """Read complete records once for both polling and SSE transports."""
        with LOG_LOCK:
            return self._read_event_records_locked(cursor)

    def _read_event_records_locked(self, cursor):
        """Read a log snapshot while the caller owns LOG_LOCK."""
        records, reset = [], False
        maybe_rotate()
        try:
            with open(EVENTS, "rb") as stream:
                stat = os.fstat(stream.fileno())
                current = EventCursor.issued(
                    self.server.boot_id, stat, _log_generation, 0)
                offset, reset = cursor.resume(current, stat.st_size)
                stream.seek(offset)
                chunk = stream.read()
                end = chunk.rfind(b"\n") + 1
                for line in chunk[:end].splitlines(keepends=True):
                    offset += len(line)
                    records.append((offset, line))
                return records, dataclasses.replace(current, offset=offset), reset
        except FileNotFoundError:
            current = EventCursor.issued(
                self.server.boot_id, None, _log_generation, 0)
            _, reset = cursor.resume(current, 0)
            return records, current, reset

    def _write_sse_records(self, records, cursor, reset):
        if reset:
            self.wfile.write(b"event: reset\ndata: {}\n\n")
        for record_offset, line in records:
            event_id = dataclasses.replace(cursor, offset=record_offset).format()
            self.wfile.write(b"id: " + event_id.encode("ascii") + b"\n")
            self.wfile.write(b"data: " + line.rstrip(b"\r\n") + b"\n\n")

    def _stream_events(self, parsed):
        """Tail complete JSONL records as SSE messages.

        Each message id is the same inode-aware byte cursor used by GET /events.
        That makes Last-Event-ID and the polling fallback interchangeable.
        """
        params = urllib.parse.parse_qs(parsed.query, keep_blank_values=True)
        try:
            if self.headers.get("Last-Event-ID"):
                cursor = EventCursor.parse(self.headers["Last-Event-ID"])
            elif "since" in params:
                cursor = EventCursor.parse(params["since"][0])
            else:
                cursor = EventCursor.initial()
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
        recovering = True
        try:
            while True:
                if recovering:
                    # Snapshot one exact readiness boundary while appenders are
                    # excluded, then release the log lock before touching the
                    # socket. Appends after this snapshot are tailed from its
                    # cursor after `ready`; a backpressured client can never
                    # stall ingestion, polling, or rotation globally.
                    with LOG_LOCK:
                        records, cursor, reset = self._read_event_records_locked(cursor)
                else:
                    records, cursor, reset = self._read_event_records(cursor)
                self._write_sse_records(records, cursor, reset)
                if recovering:
                    encoded = cursor.format().encode("ascii")
                    self.wfile.write(b"event: ready\n")
                    self.wfile.write(b"id: " + encoded + b"\n")
                    self.wfile.write(b"data: {\"cursor\":\"" + encoded + b"\"}\n\n")
                    self.wfile.flush()
                    recovering = False
                now = time.monotonic()
                if records or reset or now - last_keepalive >= 15:
                    if not records and not reset:
                        self.wfile.write(b": keepalive\n\n")
                    self.wfile.flush()
                    last_keepalive = now
                time.sleep(0.1)
        except OSError as error:
            if self._expected_disconnect(error):
                return
            raise

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
    serve_forever()
