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
import contextvars
import dataclasses
import datetime
import email.header
import fcntl
import glob
import hmac
import json
import os
import queue
import re
import secrets
import sys
import threading
import time
import tomllib
import urllib.request
from contextlib import asynccontextmanager
from typing import Any, Literal

import anyio
import uvicorn
from fastapi import FastAPI, Header, Query, Request
from fastapi.openapi.utils import get_openapi
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, PlainTextResponse, Response
from pydantic import BaseModel, ConfigDict, JsonValue, RootModel, model_validator
from sse_starlette import EventSourceResponse, ServerSentEvent

import notification_persistence
import residents as resident_manifests
import retention
from state_coordinator import StateCoordinator
from approval_protocol import structured_approval, thaw_json
from hooks import durable
from protocol import validate_event
from config import Config

# Deprecated default-value aliases remain for direct function callers during the
# notification-store extraction. Runtime HTTP traffic uses ``Runtime.config``.
_DEFAULT_CONFIG = Config()
PORT = _DEFAULT_CONFIG.port
HOST = _DEFAULT_CONFIG.host
ROOT = os.path.dirname(os.path.abspath(__file__))
EVENTS = str(_DEFAULT_CONFIG.events)

MAX_EVENT_BYTES = 64 * 1024
VILLAGERS_DIR = str(_DEFAULT_CONFIG.villagers_dir)
TOKEN = _DEFAULT_CONFIG.token
ARCHIVE_DIR = ""
MAX_LOG_BYTES = _DEFAULT_CONFIG.max_log_bytes

# every read, append and rotation of the log goes through this, so an event can
# never land in the gap between reading the log and swapping it out
LOG_LOCK = threading.Lock()
_rotate_floor = 0  # don't re-check until the log grows past this
_log_generation = 0  # changes when rotation rewrites the live inode
NOTIFY_URL = _DEFAULT_CONFIG.notify_url
NOTIFY_TOKEN = _DEFAULT_CONFIG.notify_token
NOTIFY_TIMEOUT = _DEFAULT_CONFIG.notify_timeout
NOTIFY_MEMORY = 512  # how many knocks we remember, to not knock twice
NOTIFY_WORKERS = 2
NOTIFY_QUEUE = 64
KNOCK_RECORDS = _DEFAULT_CONFIG.knock_records
KNOCK_BYTES = _DEFAULT_CONFIG.knock_bytes
LEDGER_RECORDS = KNOCK_RECORDS
LEDGER_BYTES = KNOCK_BYTES
KNOCK_LOCK_SHARDS = 32
LEDGER_DELIVERY_IDS = notification_persistence.DELIVERY_IDS
LEDGER_KNOCKS = notification_persistence.KNOCKS
LEDGER_NOTIFIED = notification_persistence.NOTIFIED
LEDGER_NOTIFY_DROPPED = notification_persistence.DROPPED
LEDGER_KINDS = notification_persistence.KINDS
DROP_SECONDS = 12 * 60 * 60
_active_runtime = contextvars.ContextVar("burrow_runtime", default=None)


def _setting(name, fallback):
    runtime = _active_runtime.get()
    return getattr(runtime.config, name) if runtime is not None else fallback


def _events_path():
    return str(_setting("events", EVENTS))


def _villagers_path():
    return str(_setting("villagers_dir", VILLAGERS_DIR))


def _store():
    runtime = _active_runtime.get()
    return runtime.notification_store if runtime is not None else _notification_store


def _legacy_config():
    """Compatibility adapter for direct callers pending notification-store #72."""
    return dataclasses.replace(
        _DEFAULT_CONFIG,
        host=HOST,
        port=PORT,
        events=os.path.abspath(EVENTS),
        villagers_dir=os.path.abspath(VILLAGERS_DIR),
        token=TOKEN,
        archive_dir=os.path.abspath(ARCHIVE_DIR) if ARCHIVE_DIR else None,
        max_event_bytes=MAX_EVENT_BYTES,
        max_log_bytes=MAX_LOG_BYTES,
        notify_url=NOTIFY_URL,
        notify_token=NOTIFY_TOKEN,
        notify_timeout=NOTIFY_TIMEOUT,
        notify_workers=NOTIFY_WORKERS,
        notify_queue=NOTIFY_QUEUE,
        knock_records=KNOCK_RECORDS,
        knock_bytes=KNOCK_BYTES,
        ledger_records=LEDGER_RECORDS,
        ledger_bytes=LEDGER_BYTES,
        knock_lock_shards=KNOCK_LOCK_SHARDS,
        drop_seconds=DROP_SECONDS,
    )


VIEWER_EVENT_TYPES = {
    "task_started",
    "tool_called",
    "tool_failed",
    "artifact_produced",
    "needs_human",
    "idle",
    "session_ended",
}


CTYPES = {
    ".html": "text/html; charset=utf-8",
    ".js": "text/javascript",
    ".css": "text/css",
    ".png": "image/png",
    ".json": "application/json",
}


def read_villagers():
    """Validated residents plus legacy soul files for v0 client compatibility."""
    out = read_residents()["residents"]
    villagers_dir = _villagers_path()
    if not os.path.isdir(villagers_dir):
        return out
    for fn in sorted(os.listdir(villagers_dir)):
        if not fn.endswith(".md") or fn.startswith("."):
            continue
        try:
            with open(os.path.join(villagers_dir, fn), encoding="utf-8") as f:
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
    return resident_manifests.load_resident_manifests(_villagers_path())


# ————— knocks: push a needs_human event to a webhook —————
#
# The village can't knock on a door you're not looking at, so a `needs_human`
# ingest also fires one POST at BURROW_NOTIFY_URL. Body is plain text and the
# title rides in headers, which is exactly what ntfy wants; anything that
# accepts a POST works. It happens on a daemon thread and swallows every
# error: a knock we fail to forward must never slow down or fail the ingest.

NAMES = [
    "Bramble",
    "Poppy",
    "Wren",
    "Sorrel",
    "Fern",
    "Alder",
    "Maple",
    "Rowan",
    "Thistle",
    "Clover",
    "Hazel",
    "Juniper",
    "Moss",
    "Reed",
    "Tansy",
    "Willow",
]

_notified = collections.OrderedDict()
_notifying = set()
_notified_lock = threading.Lock()
_knock_queue = queue.Queue(maxsize=NOTIFY_QUEUE)
_knock_workers_started = False
_knock_workers_lock = threading.Lock()
_knock_worker_stop = threading.Event()
_knock_worker_threads = []
_transport_lock = threading.Lock()
_transport_counters = {
    "ingest_duplicates": 0,
    "notify_delivered": 0,
    "notify_failed": 0,
    "notify_retried": 0,
    "notify_saturated": 0,
    "notify_dropped": 0,
}
_notification_store = notification_persistence.NotificationPersistence(
    _events_path,
    lambda: (
        _setting("knock_records", KNOCK_RECORDS),
        _setting("knock_bytes", KNOCK_BYTES),
    ),
    KNOCK_LOCK_SHARDS,
    ledger_limits=lambda: (
        _setting("ledger_records", LEDGER_RECORDS),
        _setting("ledger_bytes", LEDGER_BYTES),
    ),
)
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
        code_unit = int.from_bytes(encoded[index : index + 2], "big")
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
        return (
            soul.get("valid") is True
            and soul.get("manifest_version") == 1
            and type(soul.get("home")) is int
        )

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
        while (
            taken_names
            and NAMES[(h + offset) % len(NAMES)] in taken_names
            and offset < len(NAMES)
        ):
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
        with open(_events_path(), encoding="utf-8") as stream:
            lines = collections.deque(
                stream, maxlen=retention.POLICY["viewer_line_limit"]
            )
        for line in lines:
            try:
                parsed = json.loads(line)
            except (TypeError, ValueError):
                continue
            if (
                isinstance(parsed, dict)
                and parsed.get("agent_id")
                and parsed.get("type") in VIEWER_EVENT_TYPES
            ):
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
        if item is event or time.time() - event_time <= _setting(
            "drop_seconds", DROP_SECONDS
        ):
            visible_agents.add(agent_id)
    return [item for item in events if str(item["agent_id"]) in visible_agents]


def villager_name(event):
    agent_id = str(event.get("agent_id") or "")
    return villager_names(_fleet_events(event)).get(
        agent_id, NAMES[js_hash(agent_id) % len(NAMES)]
    )


def knock_key(event):
    return notification_persistence.knock_key(event)


def terminal_knock_key(event):
    """Fixed-size non-sensitive key for every durable terminal boundary."""
    return notification_persistence.terminal_key(event)


def terminal_knock_keys(event):
    """Current identity plus only migration aliases proven to be exact."""
    return notification_persistence.terminal_keys(event)


def _ledger_path(kind):
    return _store().ledger_path(kind)


def _notification_lock_path(shard):
    return _store().notification_lock_path(shard)


def _load_ledger(kind, cache):
    return _store().load_ledger(kind, cache)


def _remember_durable_batch(kind, cache, keys, preserve_existing=()):
    """Atomically retain a bounded ordered batch under a stable process lock.

    A successful batch may evict the oldest prior entries, matching single-key
    ledger retention.  The complete requested batch and any named keys that
    already exist must remain represented; otherwise the authoritative file
    and cache are left unchanged.
    """
    return _store().remember_batch(
        kind, keys, preserve_existing=preserve_existing, cache=cache
    )


def _remember_durable(kind, cache, key):
    """Atomically retain one key in a bounded ordered durable ledger."""
    _store().remember(kind, key, cache)


def _ledger_contains(kind, key):
    """Read terminal authority afresh; process caches are never authoritative."""
    return _store().contains(kind, key)


def _knock_delivery_lock(key):
    return _store().delivery_lock_path(key)


def receiver_delivery_id(event):
    """Stable non-sensitive ASCII projection of the internal knock identity."""
    return terminal_knock_key(event)


def _fsync_parent(path):
    durable.fsync_parent(path)


def _knock_journal_paths(path):
    return _store().journal_paths(path)


def _read_knock_keys(path):
    return _store().read_journal_keys(path)


def _compact_knocks_locked(path, addition=None):
    """Publish one bounded latest-state generation while ``path.lock`` is held.

    Capacity victims receive a durable terminal-drop entry before the compacted
    authority is published, so a crash or restart cannot make them eligible.
    """
    return _store().compact_locked(path, addition)


def _publish_knock_compaction(path, lines):
    """Durably replace journal authority while its stable lock is held."""
    return _store().publish_compaction(path, lines)


def _commit_knock_terminal(event, kind):
    """Commit terminal outcome while preserving every retained source."""
    return _store().commit_terminal(event, kind)


def persist_knock(event):
    """Durably journal notification work before the ingest acknowledges it."""
    if not _setting("notify_url", NOTIFY_URL) or event.get("type") != "needs_human":
        return True
    return _store().journal(event)


def _persist_knock_attempt(event, attempts):
    """Append a durable retry-state transition to the knock journal."""
    return _store().record_attempt(event, attempts)


def claim_knock(event):
    """Claim a knock unless it is in flight or has already been delivered."""
    if not _setting("notify_url", NOTIFY_URL) or event.get("type") != "needs_human":
        return False
    key = terminal_knock_key(event)
    with _notified_lock:
        delivered = _load_ledger(LEDGER_NOTIFIED, _notified_by_log)
        dropped = _load_ledger(LEDGER_NOTIFY_DROPPED, _dropped_by_log)
        if any(
            candidate in delivered or candidate in dropped
            for candidate in terminal_knock_keys(event)
        ):
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
        title = (
            structured.action if structured else f"{name} is at your door ({project})"
        )
        if not title.isascii():
            title = email.header.Header(title, charset="utf-8", maxlinelen=0).encode()
        # Receiver IDs hash the internal identity; structured fallbacks include
        # request_id so distinct same-millisecond requests remain distinct.
        headers = {
            "Content-Type": "text/plain; charset=utf-8",
            "Title": title,
            "Tags": "door",
            "Priority": "high",
            "X-Burrow-Delivery-ID": receiver_delivery_id(event),
        }
        notify_token = _setting("notify_token", NOTIFY_TOKEN)
        if notify_token:
            headers["Authorization"] = "Bearer " + notify_token
        if structured:
            detail = thaw_json(structured.detail)
            body_text = json.dumps(detail, ensure_ascii=False, sort_keys=True)
        else:
            body_text = f"{name} · {project}\n{message}"
        body = body_text.encode("utf-8")
        req = urllib.request.Request(
            _setting("notify_url", NOTIFY_URL),
            data=body,
            headers=headers,
            method="POST",
        )
        with urllib.request.urlopen(
            req, timeout=_setting("notify_timeout", NOTIFY_TIMEOUT)
        ):
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
        if any(
            _ledger_contains(LEDGER_NOTIFIED, candidate)
            or _ledger_contains(LEDGER_NOTIFY_DROPPED, candidate)
            for candidate in terminal_knock_keys(event)
        ):
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
    while not _knock_worker_stop.is_set():
        try:
            event = _knock_queue.get(timeout=0.1)
        except queue.Empty:
            continue
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
    for generation, complete, events in _store().recover():
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
            _store().retire_replay_if_terminal(generation, complete, events)
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
        _knock_worker_stop.clear()
        for index in range(_setting("notify_workers", NOTIFY_WORKERS)):
            worker = threading.Thread(
                target=_knock_worker, name=f"burrow-knock-{index}", daemon=True
            )
            worker.start()
            _knock_worker_threads.append(worker)
        _knock_workers_started = True
        _recover_knocks()


def stop_knock_workers():
    """Stop and join transport-owned notification workers at ASGI shutdown."""
    global _knock_workers_started
    with _knock_workers_lock:
        if not _knock_workers_started:
            return
        _knock_worker_stop.set()
        workers = list(_knock_worker_threads)
    for worker in workers:
        worker.join()
    with _knock_workers_lock:
        _knock_worker_threads.clear()
        _knock_workers_started = False


def transport_status():
    """Bounded machine-readable diagnostics for the browser live-status module."""
    with _transport_lock:
        counters = dict(_transport_counters)
    delivered, dropped = _store().terminal_counts()
    return {
        "ingest": {
            "duplicates": counters["ingest_duplicates"],
            "dedupe_window": None,
            "durable": True,
        },
        "notifications": {
            "configured": bool(_setting("notify_url", NOTIFY_URL)),
            "queued": _knock_queue.qsize(),
            "queue_capacity": _setting("notify_queue", NOTIFY_QUEUE),
            "workers": _setting("notify_workers", NOTIFY_WORKERS),
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
    configured = _setting("archive_dir", ARCHIVE_DIR)
    return (
        str(configured)
        if configured
        else os.path.join(os.path.dirname(os.path.abspath(_events_path())), "archive")
    )


def archive_path(now=None):
    """<archive>/events-20260824T170430Z.jsonl, never overwriting a segment."""
    now = now or datetime.datetime.now(datetime.timezone.utc)
    into = archive_dir()
    base, ext = os.path.splitext(os.path.basename(_events_path()))
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
    with open(_events_path(), "r+b") as live:
        fcntl.flock(live, fcntl.LOCK_EX)
        original = live.read()
        lines = original.decode("utf-8", errors="replace").splitlines()
        tail = retention.carry_forward(
            lines, int(time.time() * 1000), retention.POLICY
        ).lines
        data = "".join(line + "\n" for line in tail).encode("utf-8")
        size = len(original)
        if len(data) > size * 9 // 10:
            _rotate_floor = size + max(
                _setting("max_log_bytes", MAX_LOG_BYTES) // 10, 1
            )
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
    max_log_bytes = _setting("max_log_bytes", MAX_LOG_BYTES)
    if max_log_bytes <= 0:
        return
    try:
        size = os.path.getsize(_events_path())
    except OSError:
        return
    if size <= max(max_log_bytes, _rotate_floor):
        return
    try:
        rotate(size)
    except OSError:
        pass  # a log we failed to rotate beats a dropped event


def read_log():
    """The live log, rotating it first if it has outgrown the threshold. Doing
    the check here too keeps local mode bounded, where emitters append to the
    file themselves and the server only ever reads it."""
    with LOG_LOCK:
        maybe_rotate()
        try:
            with open(_events_path(), "rb") as f:
                return f.read()
        except OSError:
            return b""


def append_event(event):
    """Append one event, then rotate if the log is now too big — in that order,
    so an accepted POST is always in the live tail or in an archive."""
    line = json.dumps(event, ensure_ascii=False) + "\n"
    with LOG_LOCK:
        events_path = _events_path()
        os.makedirs(os.path.dirname(os.path.abspath(events_path)), exist_ok=True)
        with open(
            durable.lock_path(os.path.abspath(events_path)), "a+"
        ) as process_lock:
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
                    _remember_durable(
                        LEDGER_DELIVERY_IDS, _delivery_ids_by_log, delivery_id
                    )
                except OSError:
                    pass
                with _transport_lock:
                    _transport_counters["ingest_duplicates"] += 1
                return False
            with open(events_path, "a", encoding="utf-8") as f:
                f.write(line)
                f.flush()
                os.fsync(f.fileno())
            _fsync_parent(events_path)
            if delivery_id:
                _remember_durable(
                    LEDGER_DELIVERY_IDS, _delivery_ids_by_log, delivery_id
                )
            maybe_rotate()
            return True


def _event_log_has_delivery_id(delivery_id):
    events_path = _events_path()
    paths = [events_path]
    base, ext = os.path.splitext(os.path.basename(events_path))
    paths.extend(sorted(glob.glob(os.path.join(archive_dir(), base + "-*" + ext))))
    for path in paths:
        try:
            with open(path, encoding="utf-8") as stream:
                for line in stream:
                    try:
                        event = json.loads(line)
                    except ValueError:
                        continue
                    if (
                        isinstance(event, dict)
                        and event.get("delivery_id") == delivery_id
                    ):
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
            if (
                not field
                or len(field) > 20
                or not field.isascii()
                or not field.isdigit()
            ):
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
        return ":".join(
            str(part)
            for part in (
                "v1",
                self.boot_id,
                self.device,
                self.inode,
                self.generation,
                self.offset,
            )
        )


class ProtocolEvent(BaseModel):
    """Validated public event-ingestion wire shape."""

    model_config = ConfigDict(extra="allow", strict=True)
    v: int
    ts: str
    source: str
    agent_id: str
    project: str
    cwd: str | None = None
    type: str
    payload: dict[str, Any]

    @model_validator(mode="after")
    def require_standard_json_numbers(self):
        try:
            json.dumps(self.model_dump(), allow_nan=False)
        except (TypeError, ValueError) as error:
            raise ValueError("event must contain standard JSON values") from error
        return self


class VillagerWire(BaseModel):
    """One projected villager record."""

    id: str
    name: str
    char: str
    accent: str
    residency: Literal["resident", "visitor"]
    home: int | None
    base: Literal["home", "lodge"]
    resident_file: str | None
    state: Literal["knocking", "resting", "failed", "stale", "working"]
    project: str
    cwd: str
    last_ts: str
    last_line: str
    place: str | None
    lineage: dict[str, str]
    history: list[ProtocolEvent]
    mood: dict[str, JsonValue]
    pending_approval_ids: list[str]


class ResidentWire(BaseModel):
    """One validated resident manifest projection."""

    file: str
    valid: Literal[True]
    manifest_version: Literal[1]
    match: dict[str, str]
    home: int
    meta: dict[str, str]
    body: str
    capabilities: dict[str, JsonValue]
    routines: list[dict[str, JsonValue]]


class ResidentDiagnosticWire(BaseModel):
    """One bounded resident-manifest diagnostic."""

    file: str
    valid: Literal[False]
    diagnostic: Literal[True]
    manifest_version: int | None
    match: dict[str, str]
    declared_home: int | None
    meta: dict[str, str]
    body: str | None
    capabilities: dict[str, JsonValue]


class ArtifactWire(BaseModel):
    """One projected artifact record."""

    agent_id: str
    project: str
    artifact: str
    ts: str


class TaskWire(BaseModel):
    """One projected task lifecycle."""

    id: str
    title: str
    state: Literal["open", "claimed", "done", "failed"]
    required_skills: list[str]
    posted_by: str
    claimant: str | None
    updated_at: str


class ApprovalWire(BaseModel):
    """One projected approval lifecycle."""

    request_id: str
    agent_id: str
    project: str
    state: Literal["pending", "resolved", "collision"]
    message: str
    action: str | None
    detail: JsonValue
    options: list[JsonValue]
    expires_at_present: bool
    expires_at: str | None
    opened_at: str
    decision: str | None = None
    resolved_at: str | None = None


class JournalWire(BaseModel):
    """One projected journal observation."""

    day: str
    agent_id: str
    project: str
    source: str
    routine: str
    path: str
    observed_at: str


class RoutineWire(BaseModel):
    """One projected routine lifecycle."""

    run_id: str
    routine: str
    agent_id: str
    project: str
    source: str
    state: Literal["running", "finished", "failed"]
    trigger: str
    started_at: str
    updated_at: str
    outcome: str | None
    duration_s: int | float | None
    artifacts: list[JsonValue]
    error: str | None


class DiagnosticWire(BaseModel):
    """One bounded projection diagnostic."""

    model_config = ConfigDict(extra="allow")
    kind: str | None = None
    file: str | None = None
    path: str | None = None
    message: str | None = None


class ProjectionCapacity(BaseModel):
    villagers: int
    events_per_villager: int
    tasks: int
    approvals: int
    journals: int
    routines: int
    diagnostics: int


class VillageState(BaseModel):
    """Complete browser snapshot wire contract."""

    model_config = ConfigDict(extra="forbid")
    schema_version: Literal[1]
    generation: int
    cursor: str
    log_generation: int
    evaluated_at: str
    villagers: list[VillagerWire]
    residents: list[ResidentWire]
    diagnostic_residents: list[ResidentDiagnosticWire]
    artifacts: list[ArtifactWire]
    tasks: list[TaskWire]
    approvals: list[ApprovalWire]
    journals: list[JournalWire]
    routines: list[RoutineWire]
    diagnostics: list[DiagnosticWire]
    capacity: ProjectionCapacity
    capabilities: dict[str, bool]


class StateEnvelope(BaseModel):
    kind: Literal["snapshot", "reset"]
    snapshot: VillageState


class ErrorResponse(RootModel[str]):
    """Preserved plain-text transport error body."""


class IngestStatus(BaseModel):
    duplicates: int
    dedupe_window: int | None
    durable: bool


class NotificationStatus(BaseModel):
    configured: bool
    queued: int
    queue_capacity: int
    workers: int
    delivered: int
    failed: int
    retried: int
    saturated: int
    dropped: int


class TransportStatus(BaseModel):
    ingest: IngestStatus
    notifications: NotificationStatus


class VillagerList(RootModel[list[dict[str, Any]]]):
    pass


class ResidentReport(BaseModel):
    model_config = ConfigDict(extra="allow")
    residents: list[dict[str, Any]]


class Runtime:
    """Process-local transport state; storage and projection remain synchronous."""

    def __init__(self, config):
        self.config = config
        self.notification_store = notification_persistence.NotificationPersistence(
            lambda: str(config.events),
            lambda: (config.knock_records, config.knock_bytes),
            config.knock_lock_shards,
            ledger_limits=lambda: (config.ledger_records, config.ledger_bytes),
        )
        self.boot_id = secrets.token_hex(16)
        self.state_coordinator = StateCoordinator(
            self.projection_inputs,
            read_residents,
            capabilities={
                "ingest": True,
                "approvals": True,
                "jobs": True,
                "routines": True,
            },
        )

    def projection_inputs(self):
        with LOG_LOCK:
            maybe_rotate()
            events = []
            try:
                with open(self.config.events, "rb") as stream:
                    stat = os.fstat(stream.fileno())
                    for line in stream:
                        try:
                            events.append(
                                json.loads(line, parse_constant=_reject_json_constant)
                            )
                        except (UnicodeDecodeError, json.JSONDecodeError):
                            events.append(None)
                    cursor = EventCursor.issued(
                        self.boot_id, stat, _log_generation, stat.st_size
                    ).format()
            except FileNotFoundError:
                cursor = EventCursor.issued(
                    self.boot_id, None, _log_generation, 0
                ).format()
            return events, cursor, _log_generation

    def read_event_records(self, cursor):
        with LOG_LOCK:
            maybe_rotate()
            records = []
            try:
                with open(self.config.events, "rb") as stream:
                    stat = os.fstat(stream.fileno())
                    current = EventCursor.issued(self.boot_id, stat, _log_generation, 0)
                    offset, reset = cursor.resume(current, stat.st_size)
                    stream.seek(offset)
                    chunk = stream.read()
                    end = chunk.rfind(b"\n") + 1
                    for line in chunk[:end].splitlines(keepends=True):
                        offset += len(line)
                        records.append((offset, line))
                    return records, dataclasses.replace(current, offset=offset), reset
            except FileNotFoundError:
                current = EventCursor.issued(self.boot_id, None, _log_generation, 0)
                _, reset = cursor.resume(current, 0)
                return records, current, reset


def lifespan(config):
    @asynccontextmanager
    async def application_lifespan(application):
        resolved = config() if callable(config) else config
        runtime = Runtime(resolved)
        application.state.runtime = runtime
        application.state.config = resolved
        token = _active_runtime.set(runtime)
        try:
            await anyio.to_thread.run_sync(runtime.state_coordinator.evaluate)
            if resolved.notify_url:
                await anyio.to_thread.run_sync(ensure_knock_workers)
            try:
                yield
            finally:
                if resolved.notify_url:
                    await anyio.to_thread.run_sync(stop_knock_workers)
        finally:
            _active_runtime.reset(token)

    return application_lifespan


with open(os.path.join(ROOT, "pyproject.toml"), "rb") as _project_file:
    PROJECT_VERSION = tomllib.load(_project_file)["project"]["version"]


app = FastAPI(
    title="Burrow Village API",
    version=PROJECT_VERSION,
    lifespan=lifespan(_legacy_config),
)
app.state.config = _DEFAULT_CONFIG


def _openapi():
    if app.openapi_schema:
        return app.openapi_schema
    schema = get_openapi(title=app.title, version=app.version, routes=app.routes)
    schema.setdefault("components", {}).setdefault("schemas", {})["ErrorResponse"] = (
        ErrorResponse.model_json_schema(ref_template="#/components/schemas/{model}")
    )
    app.openapi_schema = schema
    return schema


app.openapi = _openapi


@app.exception_handler(RequestValidationError)
async def request_validation_error(_request, _error):
    return PlainTextResponse("not a protocol event", status_code=400)


def _runtime(request):
    return request.app.state.runtime


def _error(status, detail):
    return PlainTextResponse(
        detail, status_code=status, headers={"Cache-Control": "no-store"}
    )


PLAIN_ERROR_RESPONSES = {
    status: {
        "description": description,
        "content": {
            "text/plain": {"schema": {"$ref": "#/components/schemas/ErrorResponse"}}
        },
    }
    for status, description in {
        400: "Malformed framing or protocol event",
        401: "Unauthorized",
        413: "Event body too large",
        503: "Notification queue unavailable",
    }.items()
}


@app.middleware("http")
async def guard_event_ingest(request: Request, call_next):
    """Reject framing and auth before FastAPI consumes the JSON body."""
    if request.method != "POST" or request.url.path != "/events":
        return await call_next(request)
    if request.headers.get("transfer-encoding"):
        return _error(400, "unsupported transfer encoding")
    lengths = request.headers.getlist("content-length")
    if (
        len(lengths) != 1
        or not lengths[0].isascii()
        or not lengths[0].isdigit()
        or int(lengths[0]) <= 0
    ):
        return _error(400, "invalid content length")
    config = request.app.state.config
    if int(lengths[0]) > config.max_event_bytes:
        return _error(413, "event too large")
    presented = request.headers.get("x-burrow-token") or ""
    scheme, _, value = (request.headers.get("authorization") or "").partition(" ")
    if scheme.lower() == "bearer":
        presented = value.strip() or presented
    if config.token and not hmac.compare_digest(presented, config.token):
        return _error(401, "unauthorized")
    return await call_next(request)


@app.middleware("http")
async def bind_runtime(request: Request, call_next):
    token = _active_runtime.set(request.app.state.runtime)
    try:
        return await call_next(request)
    finally:
        _active_runtime.reset(token)


@app.post(
    "/events",
    status_code=204,
    responses={
        204: {"description": "Appended or deduplicated"},
        **PLAIN_ERROR_RESPONSES,
    },
)
async def ingest_event(
    request: Request,
    event_wire: ProtocolEvent,
    x_burrow_delivery_id: str | None = Header(None),
):
    event = event_wire.model_dump(mode="python", exclude_unset=True)
    error = validate_event(event)
    if error:
        return _error(400, error)
    event.pop("delivery_id", None)
    delivery_id = (x_burrow_delivery_id or "").strip()
    if delivery_id:
        if not _delivery_id_pattern.fullmatch(delivery_id):
            return _error(400, "invalid delivery id")
        event["delivery_id"] = delivery_id
    await anyio.to_thread.run_sync(append_event, event)
    if not await anyio.to_thread.run_sync(persist_knock, event):
        return _error(503, "notification queue unavailable")
    if await anyio.to_thread.run_sync(claim_knock, event):
        await anyio.to_thread.run_sync(notify_async, event)
    await anyio.to_thread.run_sync(_runtime(request).state_coordinator.evaluate)
    return Response(status_code=204)


@app.get(
    "/state",
    response_model=StateEnvelope,
    responses={204: {"description": "Snapshot unchanged"}},
)
async def get_state(
    request: Request,
    generation: int | None = Query(None, ge=0),
    cursor: str | None = Query(None),
):
    coordinator = _runtime(request).state_coordinator
    delivery = await anyio.to_thread.run_sync(
        coordinator.evaluate_delivery, generation, cursor
    )
    if delivery["kind"] == "unchanged":
        return Response(
            status_code=204,
            headers={
                "X-Burrow-State-Generation": str(delivery["generation"]),
                "X-Burrow-State-Cursor": delivery["cursor"],
            },
        )
    return delivery


@app.get(
    "/state/stream",
    response_class=EventSourceResponse,
    responses={
        200: {
            "content": {"text/event-stream": {}},
            "description": "Complete Village State snapshots",
        }
    },
)
async def stream_state(
    request: Request, generation: int = Query(0, ge=0), cursor: str | None = Query(None)
):
    coordinator = _runtime(request).state_coordinator

    async def events():
        nonlocal generation, cursor
        while not await request.is_disconnected():
            delivery = await anyio.to_thread.run_sync(
                coordinator.evaluate_delivery, generation, cursor
            )
            if delivery["kind"] in {"snapshot", "reset"}:
                snapshot = delivery["snapshot"]
                yield ServerSentEvent(
                    data=json.dumps(
                        delivery,
                        ensure_ascii=False,
                        separators=(",", ":"),
                        allow_nan=False,
                    ),
                    event="snapshot",
                    id=str(snapshot["generation"]),
                )
                generation, cursor = snapshot["generation"], snapshot["cursor"]
            await anyio.to_thread.run_sync(
                coordinator.wait_for_newer, generation, 1, abandon_on_cancel=True
            )

    return EventSourceResponse(
        events(),
        ping=15,
        headers={"Cache-Control": "no-cache, no-store", "X-Accel-Buffering": "no"},
    )


@app.get("/transport/status", response_model=TransportStatus)
async def get_transport_status():
    return await anyio.to_thread.run_sync(transport_status)


@app.get("/villagers", response_model=VillagerList)
async def get_villagers():
    return await anyio.to_thread.run_sync(read_villagers)


@app.get("/residents", response_model=ResidentReport)
async def get_residents():
    return await anyio.to_thread.run_sync(read_residents)


@app.get("/events", include_in_schema=False)
async def get_events(request: Request, since: str | None = Query(None)):
    try:
        cursor = (
            EventCursor.parse(since) if since is not None else EventCursor.initial()
        )
    except ValueError:
        return _error(400, "invalid since cursor")
    records, issued, reset = await anyio.to_thread.run_sync(
        _runtime(request).read_event_records, cursor
    )
    headers = {"X-Burrow-Cursor": issued.format(), "Cache-Control": "no-store"}
    if reset:
        headers["X-Burrow-Reset"] = "1"
    return Response(
        b"".join(line for _, line in records),
        media_type="application/x-ndjson",
        headers=headers,
    )


@app.get("/retention-policy.json", include_in_schema=False)
async def retention_policy_file():
    return FileResponse(
        os.path.join(ROOT, "retention-policy.json"), media_type="application/json"
    )


@app.get("/{asset_path:path}", include_in_schema=False)
async def static_viewer(asset_path: str):
    asset_path = asset_path or "index.html"
    base = os.path.realpath(os.path.join(ROOT, "viewer"))
    full = os.path.realpath(os.path.join(base, asset_path))
    if not full.startswith(base + os.sep) or not os.path.isfile(full):
        return _error(404, "not found")
    return FileResponse(
        full,
        media_type=CTYPES.get(os.path.splitext(full)[1], "application/octet-stream"),
    )


def create_app(config: Config) -> FastAPI:
    """Construct an isolated HTTP application and its runtime wiring."""
    application = FastAPI(
        title=app.title,
        version=app.version,
        lifespan=lifespan(config),
    )
    application.state.config = config
    application.router.routes.extend(app.router.routes)
    for middleware in reversed(app.user_middleware):
        application.add_middleware(
            middleware.cls, *middleware.args, **middleware.kwargs
        )
    application.add_exception_handler(RequestValidationError, request_validation_error)

    def application_openapi():
        if application.openapi_schema:
            return application.openapi_schema
        schema = get_openapi(
            title=application.title,
            version=application.version,
            routes=application.routes,
        )
        schema.setdefault("components", {}).setdefault("schemas", {})[
            "ErrorResponse"
        ] = ErrorResponse.model_json_schema(ref_template="#/components/schemas/{model}")
        application.openapi_schema = schema
        return schema

    application.openapi = application_openapi
    return application


def serve_forever(config: Config) -> None:
    uvicorn.run(
        create_app(config), host=config.host, port=config.port, log_level="warning"
    )


if __name__ == "__main__":
    config = Config.from_env(os.environ, sys.argv[1:])
    print(
        f"burrow village at http://{config.host}:{config.port}, log at {config.events}"
    )
    if config.notify_url:
        print(f"knocks will be pushed to {config.notify_url}")
    if config.max_log_bytes > 0:
        configured_archive = config.archive_dir or config.events.parent / "archive"
        print(f"rotating past {config.max_log_bytes} bytes into {configured_archive}")
    serve_forever(config)
