#!/usr/bin/env python3
"""chronicle v0 server: the event log, the village projection and the HTTP API.

Accepts protocol events (POST /events, one JSON event per request) and publishes
the projection over /state and /state/stream. It serves no browser client of its
own; UIs are separate repositories consuming the versioned state contract (see
docs/ui-clients.md).

    python3 serve.py [port]                          # default 8737
    uvicorn serve:app --host 127.0.0.1 --port 8737   # the same app under uvicorn's flags

Env, read by both entry points (each also accepted under its pre-rename BURROW_*
spelling for one release; the CHRONICLE_* spelling wins wherever both are set):
    CHRONICLE_HOST          bind address (default 127.0.0.1; 0.0.0.0 in the container;
                            under uvicorn its --host/--port bind instead)
    CHRONICLE_EVENTS        event log path (default ~/.chronicle/events.jsonl)
    CHRONICLE_VILLAGERS     resident-manifest directory (default: villagers/ next to this file)
    CHRONICLE_ARCHIVE       rotated log directory (default <events dir>/archive)
    CHRONICLE_MAX_LOG       rotate once the live log passes this many bytes
    CHRONICLE_NOTIFY_URL    POST target for needs_human knocks (unset = no notifications)
    CHRONICLE_NOTIFY_TOKEN  optional bearer token for that target (e.g. a private ntfy topic)
    CHRONICLE_NOTIFY_TIMEOUT  seconds to wait on the webhook (default 5)

GET /transport/status exposes bounded ingest-deduplication and knock-forwarding
pressure for the browser's live transport status.
"""

import collections
import contextvars
import dataclasses
import datetime
import email.header
import fcntl
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
import event_log
from identity import fallback_identity
import residents as resident_manifests
import retention
from state_coordinator import StateCoordinator
from approval_protocol import structured_approval
from typed_json import thaw_json
from protocol import validate_event
from config import Config

# The module-level ``app`` — what ``uvicorn serve:app`` serves — takes its settings
# from the environment at import, exactly as ``python serve.py`` does at ``__main__``.
# It used to be built from ``Config()`` defaults, and the NAS ran it that way for six
# days with every setting silently ignored: the log went into the container layer,
# knocks stopped, the token never applied (warren#313). The aliases below are seeded
# from it for direct function callers and the tests that still monkeypatch them
# during the notification-store extraction; runtime HTTP traffic uses ``Runtime.config``.
_ENVIRONMENT_CONFIG = Config.from_env(os.environ)
PORT = _ENVIRONMENT_CONFIG.port
HOST = _ENVIRONMENT_CONFIG.host
ROOT = os.path.dirname(os.path.abspath(__file__))
EVENTS = str(_ENVIRONMENT_CONFIG.events)

MAX_EVENT_BYTES = 64 * 1024
VILLAGERS_DIR = str(_ENVIRONMENT_CONFIG.villagers_dir)
TOKEN = _ENVIRONMENT_CONFIG.token
ARCHIVE_DIR = str(_ENVIRONMENT_CONFIG.archive_dir or "")
MAX_LOG_BYTES = _ENVIRONMENT_CONFIG.max_log_bytes

NOTIFY_URL = _ENVIRONMENT_CONFIG.notify_url
NOTIFY_TOKEN = _ENVIRONMENT_CONFIG.notify_token
NOTIFY_TIMEOUT = _ENVIRONMENT_CONFIG.notify_timeout
NOTIFY_MEMORY = 512  # how many knocks we remember, to not knock twice
NOTIFY_WORKERS = 2
NOTIFY_QUEUE = 64
KNOCK_RECORDS = _ENVIRONMENT_CONFIG.knock_records
KNOCK_BYTES = _ENVIRONMENT_CONFIG.knock_bytes
LEDGER_RECORDS = KNOCK_RECORDS
LEDGER_BYTES = KNOCK_BYTES
KNOCK_LOCK_SHARDS = 32
LEDGER_DELIVERY_IDS = notification_persistence.DELIVERY_IDS
LEDGER_KNOCKS = notification_persistence.KNOCKS
LEDGER_NOTIFIED = notification_persistence.NOTIFIED
LEDGER_NOTIFY_DROPPED = notification_persistence.DROPPED
LEDGER_KINDS = notification_persistence.KINDS
DROP_SECONDS = 12 * 60 * 60
_active_runtime = contextvars.ContextVar("chronicle_runtime", default=None)


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
    """The module-level app's settings: the environment, under any alias a caller
    overrode. Compatibility adapter pending notification-store #72."""
    return dataclasses.replace(
        _ENVIRONMENT_CONFIG,
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
    "resident_declared",
    "resident_retired",
    "task_started",
    "tool_called",
    "tool_failed",
    "artifact_produced",
    "needs_human",
    "idle",
    "session_ended",
}


def read_villagers(villagers_dir=None):
    """Validated residents plus legacy soul files for v0 client compatibility."""
    out = read_residents(villagers_dir)["residents"]
    villagers_dir = (
        str(villagers_dir) if villagers_dir is not None else _villagers_path()
    )
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


def read_residents(villagers_dir=None):
    """Load valid resident declarations and actionable validation diagnostics."""
    path = str(villagers_dir) if villagers_dir is not None else _villagers_path()
    return resident_manifests.load_resident_manifests(path)


# ————— knocks: push a needs_human event to a webhook —————
#
# The village can't knock on a door you're not looking at, so a `needs_human`
# ingest also fires one POST at CHRONICLE_NOTIFY_URL. Body is plain text and the
# title rides in headers, which is exactly what ntfy wants; anything that
# accepts a POST works. It happens on a daemon thread and swallows every
# error: a knock we fail to forward must never slow down or fail the ingest.

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
_delivery_id_pattern = re.compile(r"^[A-Za-z0-9_-]{16,128}$")


def villager_names(events, villagers_dir=None):
    """Resolve stable names for a fleet.

    Resident manifests and legacy project souls are assigned once within the fleet;
    fallback names come from the projection's shared identity algorithm.
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

    for soul in read_villagers(villagers_dir):
        meta = soul.get("meta") or {}
        if meta.get("agent_id"):
            index_soul(soul_by_agent, meta["agent_id"], soul)
        if meta.get("project"):
            index_soul(soul_by_project, meta["project"], soul)

    # Steward declarations are the primary identity source. A retirement removes the
    # declaration; a later launch may declare the resident again after an explicit revival.
    declared = {}
    for event in events:
        if not isinstance(event, dict) or not event.get("agent_id"):
            continue
        if event.get("type") == "resident_declared":
            declared[str(event["agent_id"])] = {"meta": dict(event.get("payload") or {})}
        elif event.get("type") == "resident_retired":
            active = declared.get(str(event["agent_id"]))
            payload = event.get("payload") or {}
            meta = (active or {}).get("meta") or {}
            if active is not None and all(
                payload.get(field) == meta.get(field) for field in ("resident_id", "uid")
            ):
                declared.pop(str(event["agent_id"]), None)

    names = {}
    used_souls = set()
    assigned = {}
    # Exact identities are reserved first, independently of lexical event order.
    for agent_id in sorted(latest):
        if latest[agent_id].get("type") == "session_ended":
            continue
        soul = declared.get(agent_id) or soul_by_agent.get(agent_id)
        soul_key = soul and (soul.get("file") or f"declared:{agent_id}")
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

        name = fallback_identity(agent_id).name
        if soul and (soul.get("meta") or {}).get("name"):
            name = soul["meta"]["name"]
        names[agent_id] = name
    return names


def _fleet_events(event, runtime):
    """Read the same bounded event window as the viewer and include this event."""
    events = []
    config = runtime.config
    events_path = str(config.events)
    try:
        with open(events_path, encoding="utf-8") as stream:
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
        drop_seconds = config.drop_seconds
        if item is event or time.time() - event_time <= drop_seconds:
            visible_agents.add(agent_id)
    return [item for item in events if str(item["agent_id"]) in visible_agents]


def villager_name(event, runtime):
    agent_id = str(event.get("agent_id") or "")
    return villager_names(
        _fleet_events(event, runtime), runtime.config.villagers_dir
    ).get(agent_id, fallback_identity(agent_id).name)


def receiver_delivery_id(event):
    """Stable non-sensitive ASCII projection of the internal knock identity."""
    return notification_persistence.terminal_key(event)


def persist_knock(event, runtime):
    """Durably journal notification work before the ingest acknowledges it."""
    if not runtime.config.notify_url or event.get("type") != "needs_human":
        return True
    return runtime.notification_store.journal(event)


def claim_knock(event, runtime):
    """Claim a knock unless it is in flight or has already been delivered."""
    store = runtime.notification_store
    notified = runtime.notified
    notifying = runtime.notifying
    notified_lock = runtime.notified_lock
    if not runtime.config.notify_url or event.get("type") != "needs_human":
        return False
    key = notification_persistence.terminal_key(event)
    with notified_lock:
        delivered = store.load_ledger(LEDGER_NOTIFIED)
        dropped = store.load_ledger(LEDGER_NOTIFY_DROPPED)
        if any(
            candidate in delivered or candidate in dropped
            for candidate in notification_persistence.terminal_keys(event)
        ):
            return False
        if key in notified or key in notifying:
            if key in notified:
                notified.move_to_end(key)
            return False
        notifying.add(key)
    return True


def finish_knock(event, delivered, runtime):
    """Release an attempt, remembering only successful deliveries."""
    key = notification_persistence.terminal_key(event)
    store = runtime.notification_store
    notified = runtime.notified
    notifying = runtime.notifying
    notified_lock = runtime.notified_lock
    memory = runtime.config.notify_memory
    with notified_lock:
        notifying.discard(key)
        if delivered:
            if not store.commit_terminal(event, LEDGER_NOTIFIED):
                # Without a durable acknowledgement the event remains eligible
                # for recovery; never pretend volatile success is final.
                return False
            notified[key] = True
            notified.move_to_end(key)
            while len(notified) > memory:
                notified.popitem(last=False)
            return True
    return not delivered


def notify(event, runtime):
    """POST one knock and return whether it was delivered. Never raises."""
    try:
        payload = event.get("payload") or {}
        message = payload.get("message", "") if isinstance(payload, dict) else ""
        if not isinstance(message, str):
            message = str(message)
        name = villager_name(event, runtime)
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
        config = runtime.config
        notify_token = config.notify_token
        if notify_token:
            headers["Authorization"] = "Bearer " + notify_token
        if structured:
            detail = thaw_json(structured.detail)
            body_text = json.dumps(detail, ensure_ascii=False, sort_keys=True)
        else:
            body_text = f"{name} · {project}\n{message}"
        body = body_text.encode("utf-8")
        req = urllib.request.Request(
            config.notify_url,
            data=body,
            headers=headers,
            method="POST",
        )
        with urllib.request.urlopen(
            req,
            timeout=config.notify_timeout,
        ):
            pass
        return True
    except Exception:
        return False


def deliver_knock(event, runtime):
    """Attempt under a bounded cross-process claim and commit its outcome.

    The stable shard is held across terminal-ledger recheck, external POST, and
    durable outcome. The shard suppresses concurrent duplicates; a receiver
    acceptance followed by a process crash can still cause a later retry.
    """
    key = notification_persistence.terminal_key(event)
    store = runtime.notification_store
    transport_lock = runtime.transport_lock
    counters = runtime.transport_counters
    path = store.delivery_lock_path(key)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if any(
            store.contains(LEDGER_NOTIFIED, candidate)
            or store.contains(LEDGER_NOTIFY_DROPPED, candidate)
            for candidate in notification_persistence.terminal_keys(event)
        ):
            finish_knock(event, False, runtime)
            return True
        delivered = notify(event, runtime)
        if delivered:
            if not store.commit_terminal(event, LEDGER_NOTIFIED):
                delivered = False
        finish_knock(event, False, runtime)
    with transport_lock:
        key = "notify_delivered" if delivered else "notify_failed"
        counters[key] += 1
    return delivered


def notify_async(event, runtime):
    ensure_knock_workers(runtime)
    work_queue = runtime.knock_queue
    transport_lock = runtime.transport_lock
    counters = runtime.transport_counters
    try:
        work_queue.put_nowait(event)
        return True
    except queue.Full:
        finish_knock(event, False, runtime)
        with transport_lock:
            counters["notify_saturated"] += 1
        return False


def _process_knock(event, runtime):
    store = runtime.notification_store
    work_queue = runtime.knock_queue
    transport_lock = runtime.transport_lock
    counters = runtime.transport_counters
    if store.attempts_exhausted(event):
        if not store.commit_terminal(event, LEDGER_NOTIFY_DROPPED):
            finish_knock(event, False, runtime)
            return
        finish_knock(event, False, runtime)
        store.clear_attempts(event)
        _recover_knocks(runtime)
        return

    delivered = deliver_knock(event, runtime)
    if delivered:
        store.clear_attempts(event)
    else:
        attempts = store.next_attempt(event)
        durable_attempt = store.record_attempt(event, attempts)
        exhausted = store.attempts_exhausted(event)
        if not exhausted and durable_attempt and claim_knock(event, runtime):
            try:
                work_queue.put_nowait(event)
                with transport_lock:
                    counters["notify_retried"] += 1
            except queue.Full:
                finish_knock(event, False, runtime)
                with transport_lock:
                    counters["notify_saturated"] += 1
        elif exhausted and durable_attempt:
            if not store.commit_terminal(event, LEDGER_NOTIFY_DROPPED):
                return
            store.clear_attempts(event)
        elif not durable_attempt:
            return
    _recover_knocks(runtime)


def _knock_worker(runtime):
    worker_stop = runtime.knock_worker_stop
    work_queue = runtime.knock_queue
    while not worker_stop.is_set():
        try:
            event = work_queue.get(timeout=0.1)
        except queue.Empty:
            continue
        try:
            _process_knock(event, runtime)
        finally:
            work_queue.task_done()


def _recover_knocks(runtime):
    """Atomically hand off new work and replay immutable generations.

    Replay files remain until their keys have durable delivered/drop outcomes.
    Re-reading them after a crash is safe because ``claim_knock`` consults those
    durable ledgers and the in-flight set before queueing.
    """
    store = runtime.notification_store
    work_queue = runtime.knock_queue
    transport_lock = runtime.transport_lock
    counters = runtime.transport_counters
    for generation, complete, events in store.recover():
        for event in events:
            if not claim_knock(event, runtime):
                continue
            try:
                work_queue.put_nowait(event)
            except queue.Full:
                finish_knock(event, False, runtime)
                with transport_lock:
                    counters["notify_saturated"] += 1
                return
        try:
            store.retire_replay_if_terminal(generation, complete, events)
        except OSError:
            # Retirement failure leaves replay authority for the next recovery.
            pass


def ensure_knock_workers(runtime):
    started = runtime.knock_workers_started
    workers_lock = runtime.knock_workers_lock
    worker_stop = runtime.knock_worker_stop
    worker_threads = runtime.knock_worker_threads
    if started:
        return
    with workers_lock:
        started = runtime.knock_workers_started
        if started:
            return
        worker_stop.clear()
        count = runtime.config.notify_workers
        for index in range(count):
            worker = threading.Thread(
                target=_knock_worker,
                args=(runtime,),
                name=f"chronicle-knock-{index}",
                daemon=True,
            )
            worker.start()
            worker_threads.append(worker)
        runtime.knock_workers_started = True
        _recover_knocks(runtime)


def stop_knock_workers(runtime):
    """Stop and join transport-owned notification workers at ASGI shutdown."""
    workers_lock = runtime.knock_workers_lock
    worker_stop = runtime.knock_worker_stop
    worker_threads = runtime.knock_worker_threads
    with workers_lock:
        started = runtime.knock_workers_started
        if not started:
            return
        worker_stop.set()
        workers = list(worker_threads)
    for worker in workers:
        worker.join()
    with workers_lock:
        worker_threads.clear()
        runtime.knock_workers_started = False


def transport_status(runtime):
    """Bounded machine-readable diagnostics for the browser live-status module."""
    transport_lock = runtime.transport_lock
    source_counters = runtime.transport_counters
    work_queue = runtime.knock_queue
    store = runtime.notification_store
    config = runtime.config
    with transport_lock:
        counters = dict(source_counters)
    delivered, dropped = store.terminal_counts()
    return {
        "ingest": {
            "duplicates": counters["ingest_duplicates"],
            "dedupe_window": None,
            "durable": True,
        },
        "notifications": {
            "configured": bool(config.notify_url),
            "queued": work_queue.qsize(),
            "queue_capacity": work_queue.maxsize,
            "workers": config.notify_workers,
            "delivered": delivered,
            "failed": counters["notify_failed"],
            "retried": counters["notify_retried"],
            "saturated": counters["notify_saturated"],
            "dropped": dropped,
        },
    }


def _count_ingest_duplicate():
    with _transport_lock:
        _transport_counters["ingest_duplicates"] += 1


def _event_log():
    runtime = _active_runtime.get()
    if runtime is not None:
        return runtime.event_log
    return event_log.EventLog(_legacy_config(), _store(), _count_ingest_duplicate)


def append_event(event):
    return _event_log().append(event)


EventCursor = event_log.EventCursor


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
    """One projected task lifecycle.

    ``assignee`` names the resident a job was handed to, and is null for a job posted
    to the open board. Only its addressee may claim it, so an open row with an
    assignee is not work anybody can take — that is the whole difference between the
    two ways Steward opens a row.
    """

    id: str
    title: str
    state: Literal["open", "claimed", "done", "failed"]
    required_skills: list[str]
    posted_by: str
    assignee: str | None
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
    #: How much of a villager's history, and of the diagnostics channel, an outsider's
    #: knocking is guaranteed — and all it gets when either is contested (warren#278).
    ambient_events_per_villager: int
    tasks: int
    approvals: int
    journals: int
    routines: int
    diagnostics: int
    ambient_diagnostics: int


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
        self.notified = collections.OrderedDict()
        self.notifying = set()
        self.notified_lock = threading.Lock()
        self.knock_queue = queue.Queue(maxsize=config.notify_queue)
        self.knock_workers_started = False
        self.knock_workers_lock = threading.Lock()
        self.knock_worker_stop = threading.Event()
        self.knock_worker_threads = []
        self.transport_lock = threading.Lock()
        self.transport_counters = {
            "ingest_duplicates": 0,
            "notify_delivered": 0,
            "notify_failed": 0,
            "notify_retried": 0,
            "notify_saturated": 0,
            "notify_dropped": 0,
        }
        self.boot_id = secrets.token_hex(16)
        self.event_log = event_log.EventLog(
            config, self.notification_store, self.count_ingest_duplicate
        )
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
        return self.event_log.projection_inputs(self.boot_id)

    def read_event_records(self, cursor):
        return self.event_log.read_records(self.boot_id, cursor)

    def count_ingest_duplicate(self):
        with self.transport_lock:
            self.transport_counters["ingest_duplicates"] += 1


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
                await anyio.to_thread.run_sync(ensure_knock_workers, runtime)
            try:
                yield
            finally:
                if resolved.notify_url:
                    await anyio.to_thread.run_sync(stop_knock_workers, runtime)
        finally:
            _active_runtime.reset(token)

    return application_lifespan


with open(os.path.join(ROOT, "pyproject.toml"), "rb") as _project_file:
    PROJECT_VERSION = tomllib.load(_project_file)["project"]["version"]


app = FastAPI(
    title="Chronicle Village API",
    version=PROJECT_VERSION,
    lifespan=lifespan(_legacy_config),
)
app.state.config = _ENVIRONMENT_CONFIG


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
    runtime = _runtime(request)
    if not await anyio.to_thread.run_sync(persist_knock, event, runtime):
        return _error(503, "notification queue unavailable")
    if await anyio.to_thread.run_sync(claim_knock, event, runtime):
        await anyio.to_thread.run_sync(notify_async, event, runtime)
    await anyio.to_thread.run_sync(runtime.state_coordinator.evaluate)
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
async def get_transport_status(request: Request):
    return await anyio.to_thread.run_sync(transport_status, _runtime(request))


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
        f"chronicle village at http://{config.host}:{config.port}, log at {config.events}"
    )
    if config.notify_url:
        print(f"knocks will be pushed to {config.notify_url}")
    if config.max_log_bytes > 0:
        configured_archive = config.archive_dir or config.events.parent / "archive"
        print(f"rotating past {config.max_log_bytes} bytes into {configured_archive}")
    serve_forever(config)
