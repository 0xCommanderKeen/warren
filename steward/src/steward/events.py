"""Burrow protocol v0 events, emitted by steward.

Steward is an emitter like any other: it appends facts about what it actually did to
burrow's log, and burrow renders them. See burrow's ``docs/protocol.md`` for the shape.

Transport, in the spirit of burrow's own emitter but simpler because steward's
scheduler is a long-lived process rather than a one-shot hook:

- ``POST <BURROW_URL>/events`` with the event as the body, carrying
  ``Authorization: Bearer $BURROW_TOKEN`` when the token is set.
- A failed POST (refusal, timeout, 401, 5xx) trips an **in-process circuit breaker**
  for that target — 60 s, or 5 s for loopback where failure is an instant refusal —
  so an unreachable village never slows a routine down. The breaker lives in memory
  rather than in a dotfile because the process that owns it outlives every event it
  sends.
- **Every** event is also appended to a local JSONL log
  (``STEWARD_EVENTS_FALLBACK``, default ``~/.burrow/events.jsonl``), whether or not it
  reached burrow. That file is the watchdog's substrate: it scans the log for a
  ``routine_started`` no closing event ever answered, and a bracket split across a
  transient outage — started written locally because burrow was down, finished
  delivered remotely once it came back — used to read as a run that never reported
  back. Writing the closing event locally too keeps the local record complete, so a
  success is never buried as a failure. Consumers that read burrow read burrow; the
  local log is not a second delivery, only steward's own complete copy.
- A remote-bound event first enters the sibling ``events.jsonl.pending`` durable queue
  with a stable delivery ID. Replay is oldest-first, retires only acknowledged IDs by
  atomic replacement, and uses Burrow's delivery-ID deduplication to close the
  acknowledgement-before-retirement crash window. Corrupt/torn records are quarantined.

Emitting never raises. A village that cannot be reached must not turn into a failed
routine: that would be steward lying about its own work.
"""

import contextlib
import fcntl
import hashlib
import json
import logging
import os
import re
import time
import urllib.error
import urllib.request
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

from steward.manifest import Resident, redact_mapping, redact_secrets

__all__ = [
    "API_AGENT_ID",
    "API_PROJECT",
    "BREAKER_SECONDS",
    "DETAIL_MAX_CHARS",
    "ERROR_MAX_CHARS",
    "EVENT_SOURCE",
    "EVENT_TYPES",
    "EVENT_VERSION",
    "LOOPBACK_BREAKER_SECONDS",
    "POST_TIMEOUT_S",
    "Emitter",
    "Event",
    "EventEmitter",
    "FlushReport",
    "ImportReport",
    "NullEmitter",
    "RunContext",
    "bounded_detail",
    "chat_message_dropped_event",
    "default_fallback_path",
    "needs_human_event",
    "needs_human_resolved_event",
    "resident_declared_event",
    "resident_restarted_event",
    "resident_retired_event",
    "routine_failed_event",
    "task_claimed_event",
    "task_delegated_event",
    "task_done_event",
    "task_failed_event",
    "task_posted_event",
    "task_session_finished_event",
    "truncate_error",
    "validate_event",
]

EVENT_VERSION = 0
EVENT_SOURCE = "steward"

ROUTINE_STARTED = "routine_started"
ROUTINE_FINISHED = "routine_finished"
ROUTINE_FAILED = "routine_failed"
TASK_POSTED = "task_posted"
TASK_CLAIMED = "task_claimed"
TASK_DONE = "task_done"
TASK_FAILED = "task_failed"
TASK_SESSION_FINISHED = "task_session_finished"
TASK_DELEGATED = "task_delegated"
NEEDS_HUMAN = "needs_human"
NEEDS_HUMAN_RESOLVED = "needs_human_resolved"
RESIDENT_RESTARTED = "resident_restarted"
RESIDENT_DECLARED = "resident_declared"
RESIDENT_RETIRED = "resident_retired"
CHAT_MESSAGE_DROPPED = "chat_message_dropped"

#: The event types steward adds to the protocol. Additive in *shape* — a v0 consumer that
#: does not know one still parses the record — but not free: chronicle validates ``type``
#: against its own frozenset and 400s anything outside it. A type added here and not added
#: to ``chronicle/protocol.py`` in the same change reaches steward's local fallback log and
#: never the village, which is a silence nobody looking at the village can see. warren#276
#: was exactly that drift, four types deep.
EVENT_TYPES = (
    ROUTINE_STARTED,
    ROUTINE_FINISHED,
    ROUTINE_FAILED,
    TASK_POSTED,
    TASK_CLAIMED,
    TASK_DONE,
    TASK_FAILED,
    TASK_SESSION_FINISHED,
    TASK_DELEGATED,
    NEEDS_HUMAN,
    NEEDS_HUMAN_RESOLVED,
    RESIDENT_RESTARTED,
    RESIDENT_DECLARED,
    RESIDENT_RETIRED,
    CHAT_MESSAGE_DROPPED,
)

#: Steward's own identity, for the work steward itself does rather than a resident.
#: A job posted through the API is posted by the API, and the log should say so.
API_AGENT_ID = "steward:api"
API_PROJECT = "steward"

POST_TIMEOUT_S = 2.0
BREAKER_SECONDS = 60.0
LOOPBACK_BREAKER_SECONDS = 5.0
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "[::1]", "::1"})
REPLAY_BATCH_SIZE = 16
QUEUE_VERSION = 1

log = logging.getLogger("steward.events")

#: Errors travel into the village as one line of explanation, never a transcript.
ERROR_MAX_CHARS = 500

#: A ``needs_human`` knock is a notice, not a log. Its ``detail`` map is bounded so no
#: session can print a 200KB block and have steward serialize the whole thing into a
#: village event: each string value is capped, and a detail that still serializes past the
#: total cap is dropped for a marker rather than emitted.
DETAIL_FIELD_MAX_CHARS = 2_000
DETAIL_MAX_CHARS = 8_000

FALLBACK_ENV = "STEWARD_EVENTS_FALLBACK"
URL_ENV = "CHRONICLE_URL"
TOKEN_ENV = "CHRONICLE_TOKEN"  # noqa: S105 — an env var name, not a credential
#: The pre-rename spellings (warren#216), still read so an environment written before
#: the rename keeps configuring steward's own emitter. The new spelling wins.
LEGACY_URL_ENV = "BURROW_URL"
LEGACY_TOKEN_ENV = "BURROW_TOKEN"  # noqa: S105 — an env var name, not a credential

_REQUIRED_FIELDS = ("v", "ts", "source", "agent_id", "project", "type", "payload")
_TS_FORMAT = "%Y-%m-%dT%H:%M:%S.%fZ"


def default_fallback_path() -> Path:
    """Where undelivered events land: ``$STEWARD_EVENTS_FALLBACK`` or the local state dir."""
    configured = (os.environ.get(FALLBACK_ENV) or "").strip()
    if configured:
        return Path(configured).expanduser()
    # Same rule as chronicle's own state directory (warren#216): prefer the new name, but
    # keep using an existing ~/.burrow rather than stranding a record the watchdog reads.
    home = Path.home()
    if not (home / ".chronicle").is_dir() and (home / ".burrow").is_dir():
        return home / ".burrow" / "events.jsonl"
    return home / ".chronicle" / "events.jsonl"


def truncate_error(text: str, limit: int = ERROR_MAX_CHARS) -> str:
    """Shorten an error to something a villager panel can hold, marking the cut."""
    collapsed = " ".join(str(text).split())
    if len(collapsed) <= limit:
        return collapsed
    return collapsed[: limit - 1].rstrip() + "…"


def _cap(text: str, limit: int) -> str:
    """Shorten a string to ``limit`` characters, marking the cut, keeping its structure.

    Unlike :func:`truncate_error` this does not collapse whitespace: a ``detail`` value may
    be a block a person is meant to read, and its newlines are worth keeping.
    """
    return text if len(text) <= limit else text[: limit - 1] + "…"


def bounded_detail(detail: Mapping[str, Any] | None) -> dict[str, Any]:
    """Trim a ``needs_human`` detail map so no single event can carry a transcript.

    Every string value is capped to :data:`DETAIL_FIELD_MAX_CHARS`; if the whole map still
    serializes past :data:`DETAIL_MAX_CHARS` — many fields, or non-string bulk — it is
    replaced with a marker naming the size, because an unbounded ``detail`` is exactly how
    a session's output (secrets and all) would end up POSTed to burrow in full.
    """
    bounded = {
        str(key): _cap(value, DETAIL_FIELD_MAX_CHARS) if isinstance(value, str) else value
        for key, value in dict(detail or {}).items()
    }
    encoded = json.dumps(bounded, ensure_ascii=False, default=str)
    if len(encoded) > DETAIL_MAX_CHARS:
        return {"note": f"detail omitted: {len(encoded)} chars exceeds the {DETAIL_MAX_CHARS} cap"}
    return bounded


def utc_now_iso(moment: datetime | None = None) -> str:
    """Render a moment as burrow's timestamp: UTC ISO-8601, milliseconds, ``Z``."""
    stamp = (moment or datetime.now(UTC)).astimezone(UTC)
    return stamp.isoformat(timespec="milliseconds").replace("+00:00", "Z")


@dataclass(frozen=True, slots=True)
class Event:
    """One burrow protocol v0 event, as steward emits it."""

    type: str
    agent_id: str
    project: str
    payload: Mapping[str, Any] = field(default_factory=dict)
    cwd: str | None = None
    ts: str = field(default_factory=utc_now_iso)
    source: str = EVENT_SOURCE
    v: int = EVENT_VERSION

    def to_dict(self) -> dict[str, Any]:
        """Return the wire form. ``cwd`` is omitted when the run has no directory."""
        body: dict[str, Any] = {
            "v": self.v,
            "ts": self.ts,
            "source": self.source,
            "agent_id": self.agent_id,
            "project": self.project,
        }
        if self.cwd:
            body["cwd"] = self.cwd
        body["type"] = self.type
        body["payload"] = dict(self.payload)
        return body

    def to_json(self) -> str:
        """Return the single JSONL line for this event."""
        return json.dumps(self.to_dict(), ensure_ascii=False)


def validate_event(data: Mapping[str, Any]) -> tuple[str, ...]:
    """Return the reasons ``data`` is not a valid v0 event; empty means it is one.

    Used by tests and by anything that wants to check its own output before burrow
    does. It checks the shape the protocol documents, nothing more.
    """
    problems: list[str] = [
        f"missing field {name!r}" for name in _REQUIRED_FIELDS if name not in data
    ]
    if data.get("v") != EVENT_VERSION:
        problems.append(f"v must be {EVENT_VERSION}, got {data.get('v')!r}")
    ts = data.get("ts")
    if not isinstance(ts, str):
        problems.append("ts must be a string")
    else:
        try:
            datetime.strptime(ts, _TS_FORMAT).replace(tzinfo=UTC)
        except ValueError:
            problems.append(f"ts {ts!r} is not UTC ISO-8601 with milliseconds and a Z suffix")
    for name in ("source", "agent_id", "project", "type"):
        value = data.get(name)
        if name in data and (not isinstance(value, str) or not value):
            problems.append(f"{name} must be a non-empty string")
    if "payload" in data and not isinstance(data.get("payload"), Mapping):
        problems.append("payload must be an object")
    if "cwd" in data and not isinstance(data.get("cwd"), str):
        problems.append("cwd must be a string when present")
    return tuple(problems)


class Emitter(Protocol):
    """Anything that can take an event off steward's hands."""

    def emit(self, event: Event) -> bool:
        """Send one event. Returns whether it reached a remote target."""
        ...


class NullEmitter:
    """Emits nothing, and says so. Used by ``--dry-run``: a rehearsal is not work."""

    def __init__(self) -> None:
        """Start with an empty record of what was offered."""
        self.events: list[Event] = []

    def emit(self, event: Event) -> bool:
        """Record the event for inspection and send it nowhere."""
        self.events.append(event)
        return False

    def emit_durable(self, event: Event) -> bool:
        """Treat the explicit in-memory test record as the durable sink for arbitration tests."""
        self.events.append(event)
        return True


@dataclass(frozen=True, slots=True)
class FlushReport:
    """Observable outcome of one durable replay pass."""

    delivered: int = 0
    retired_records: int = 0
    pending: int = 0
    corrupt: int = 0
    foreign: int = 0
    failed: int = 0
    busy: int = 0
    errors: int = 0
    unknown: int = 0


@dataclass(frozen=True, slots=True)
class ImportReport:
    """Outcome of explicitly copying a legacy complete log into the replay queue."""

    scanned: int = 0
    imported: int = 0
    skipped_modern: int = 0
    skipped_duplicate: int = 0
    corrupt: int = 0
    failed: int = 0
    errors: int = 0
    unknown: int = 0


def _is_loopback(url: str) -> bool:
    host = url.split("//", 1)[-1].split("/", maxsplit=1)[0].rsplit(":", 1)[0]
    return host in LOOPBACK_HOSTS


class EventEmitter:
    """POSTs events to burrow, and falls back to a local JSONL file when it cannot."""

    def __init__(
        self,
        *,
        url: str | None = None,
        token: str | None = None,
        fallback: Path | None = None,
        timeout_s: float = POST_TIMEOUT_S,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        """Configure a target, a token, and the file that catches what does not land."""
        self.url = (url or "").strip().rstrip("/") or None
        self.token = (token or "").strip() or None
        self.fallback = Path(fallback) if fallback is not None else default_fallback_path()
        self.timeout_s = timeout_s
        self._clock = clock
        self._breaker_until: dict[str, float] = {}

    @property
    def queue(self) -> Path:
        """The durable remote-delivery queue, separate from the watchdog's local log."""
        return self.fallback.with_name(f"{self.fallback.name}.pending")

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> EventEmitter:
        """Build an emitter from ``CHRONICLE_URL``/``CHRONICLE_TOKEN``/the fallback var.

        Each is also accepted under its pre-rename ``BURROW_*`` spelling (warren#216).
        """
        source = os.environ if env is None else env
        fallback = (source.get(FALLBACK_ENV) or "").strip()
        return cls(
            url=source.get(URL_ENV) or source.get(LEGACY_URL_ENV),
            token=source.get(TOKEN_ENV) or source.get(LEGACY_TOKEN_ENV),
            fallback=Path(fallback).expanduser() if fallback else None,
        )

    # -- transport -------------------------------------------------------------------

    def _breaker_open(self, url: str) -> bool:
        until = self._breaker_until.get(url)
        return until is not None and self._clock() < until

    def _trip_breaker(self, url: str) -> None:
        window = LOOPBACK_BREAKER_SECONDS if _is_loopback(url) else BREAKER_SECONDS
        self._breaker_until[url] = self._clock() + window

    def _post(self, url: str, body: bytes, delivery_id: str = "") -> bool:
        if not url.startswith(("http://", "https://")):
            return False
        headers = {"Content-Type": "application/json"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        if delivery_id:
            headers["X-Burrow-Delivery-ID"] = delivery_id
        request = urllib.request.Request(  # noqa: S310 — scheme checked just above
            f"{url}/events", data=body, headers=headers, method="POST"
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout_s):  # noqa: S310
                return True
        except OSError, urllib.error.URLError, ValueError:
            return False

    @staticmethod
    def _lock_path(path: Path) -> Path:
        return path.with_name(f"{path.name}.lock")

    @staticmethod
    def _fsync_parent(path: Path) -> None:
        descriptor = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    def _append_line(self, path: Path, line: str, *, purpose: str) -> bool:
        """Durably append one complete line under a stable cross-process lock."""
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with self._lock_path(path).open("a+") as lock_handle:
                fcntl.flock(lock_handle, fcntl.LOCK_EX)
                try:
                    raw = path.read_bytes()
                except FileNotFoundError:
                    raw = b""
                if path == self.queue and raw and not raw.endswith(b"\n"):
                    boundary = raw.rfind(b"\n") + 1
                    torn = raw[boundary:]
                    self._append_corrupt_evidence([torn])
                    staging = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
                    try:
                        with staging.open("wb") as handle:
                            handle.write(raw[:boundary])
                            handle.flush()
                            os.fsync(handle.fileno())
                        staging.replace(path)
                        self._fsync_parent(path)
                    finally:
                        with contextlib.suppress(FileNotFoundError):
                            staging.unlink()
                descriptor = os.open(path, os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o600)
                try:
                    pending = memoryview((line + "\n").encode("utf-8"))
                    while pending:
                        pending = pending[os.write(descriptor, pending) :]
                    os.fsync(descriptor)
                finally:
                    os.close(descriptor)
                self._fsync_parent(path)
        except OSError:
            log.exception("could not persist event %s at %s", purpose, path)
            return False
        else:
            return True

    def _queue_record(self, event: Event, delivery_id: str, *, history: bool = True) -> bool:
        record = {
            "queue_v": QUEUE_VERSION,
            "delivery_id": delivery_id,
            "target": self.url,
            "event": event.to_dict(),
            "history": history,
        }
        return self._append_line(
            self.queue, json.dumps(record, ensure_ascii=False), purpose="for replay"
        )

    def _read_queue_unlocked(self) -> tuple[list[dict[str, Any]], list[bytes]]:
        try:
            raw = self.queue.read_bytes()
        except FileNotFoundError:
            return [], []
        records: list[dict[str, Any]] = []
        corrupt: list[bytes] = []
        for line in raw.splitlines(keepends=True):
            if not line.endswith(b"\n"):
                corrupt.append(line)
                continue
            try:
                value = json.loads(line)
            except UnicodeDecodeError, json.JSONDecodeError:
                corrupt.append(line)
                continue
            if not self._valid_queue_record(value):
                corrupt.append(line)
                continue
            records.append(value)
        return records, corrupt

    def _read_queue(self) -> tuple[list[dict[str, Any]], list[bytes]]:
        """Read one authority snapshot without racing a cooperating appender."""
        try:
            self.queue.stat()
        except FileNotFoundError:
            return [], []
        with self._lock_path(self.queue).open("a+") as lock_handle:
            fcntl.flock(lock_handle, fcntl.LOCK_EX)
            return self._read_queue_unlocked()

    @staticmethod
    def _valid_queue_record(value: object) -> bool:
        if not isinstance(value, dict) or value.get("queue_v") != QUEUE_VERSION:
            return False
        delivery_id = value.get("delivery_id")
        target = value.get("target")
        event = value.get("event")
        return (
            isinstance(delivery_id, str)
            and re.fullmatch(r"[A-Za-z0-9_-]{16,128}", delivery_id) is not None
            and isinstance(target, str)
            and target.startswith(("http://", "https://"))
            and isinstance(event, dict)
            and isinstance(value.get("history", True), bool)
            and not validate_event(event)
        )

    def _append_corrupt_evidence(self, chunks: Sequence[bytes]) -> None:
        """Append byte-exact corrupt chunks in independently parseable binary frames."""
        self._append_corrupt_evidence_at(self.queue, chunks)

    def _append_corrupt_evidence_at(self, path: Path, chunks: Sequence[bytes]) -> None:
        """Append byte-exact corrupt chunks associated with ``path``."""
        quarantine = path.with_name(f"{path.name}.corrupt")
        with self._lock_path(quarantine).open("a+") as lock_handle:
            fcntl.flock(lock_handle, fcntl.LOCK_EX)
            descriptor = os.open(quarantine, os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o600)
            try:
                for chunk in chunks:
                    header = f"STEWARD-CORRUPT-V1 {len(chunk)}\n".encode()
                    for part in (header, chunk, b"\nSTEWARD-CORRUPT-END\n"):
                        pending = memoryview(part)
                        while pending:
                            pending = pending[os.write(descriptor, pending) :]
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
            self._fsync_parent(quarantine)

    def _rewrite_queue(
        self,
        retired: set[tuple[str, str, str]],
        *,
        history_confirmed: set[tuple[str, str, str]] | None = None,
    ) -> None:
        """Retire acknowledged IDs without overwriting concurrent appenders."""
        self.queue.parent.mkdir(parents=True, exist_ok=True)
        with self._lock_path(self.queue).open("a+") as lock_handle:
            fcntl.flock(lock_handle, fcntl.LOCK_EX)
            records, corrupt = self._read_queue_unlocked()
            for record in records:
                if history_confirmed and self._record_identity(record) in history_confirmed:
                    record["history"] = True
            remaining = [r for r in records if self._record_identity(r) not in retired]
            if corrupt:
                self._append_corrupt_evidence(corrupt)
                quarantine = self.queue.with_name(f"{self.queue.name}.corrupt")
                log.error(
                    "quarantined %d corrupt event queue line(s) at %s",
                    len(corrupt),
                    quarantine,
                )
            staging = self.queue.with_name(f".{self.queue.name}.{uuid.uuid4().hex}.tmp")
            try:
                with staging.open("wb") as handle:
                    for record in remaining:
                        handle.write(json.dumps(record, ensure_ascii=False).encode("utf-8") + b"\n")
                    handle.flush()
                    os.fsync(handle.fileno())
                staging.replace(self.queue)
                self._fsync_parent(self.queue)
            finally:
                with contextlib.suppress(FileNotFoundError):
                    staging.unlink()

    @staticmethod
    def _history_event(record: Mapping[str, Any]) -> dict[str, Any]:
        event = dict(record["event"])
        event["steward_delivery_id"] = record["delivery_id"]
        return event

    @staticmethod
    def _event_fingerprint(event: Mapping[str, Any]) -> str:
        """Identify event content independently of legacy line position/formatting."""
        canonical = json.dumps(
            dict(event), ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        return hashlib.sha256(canonical).hexdigest()

    @classmethod
    def _record_identity(cls, record: Mapping[str, Any]) -> tuple[str, str, str]:
        """Name one exact queue record without allowing cross-target ID collisions."""
        return (
            str(record["target"]),
            str(record["delivery_id"]),
            cls._event_fingerprint(record["event"]),
        )

    def _legacy_delivery_id(self, event: Mapping[str, Any]) -> str:
        """Return the stable legacy ID for canonical content at this normalized target."""
        if self.url is None:  # pragma: no cover - import_legacy refuses this configuration
            raise ValueError("legacy delivery IDs require a target")
        canonical = json.dumps(
            dict(event), ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        digest = hashlib.sha256(self.url.encode("utf-8") + b"\0" + canonical).hexdigest()
        return f"legacy_{digest}"

    @staticmethod
    def _history_contains(raw: bytes, delivery_id: str) -> bool:
        for line in raw.splitlines():
            try:
                value = json.loads(line)
            except UnicodeDecodeError, json.JSONDecodeError:
                continue
            if isinstance(value, dict) and value.get("steward_delivery_id") == delivery_id:
                return True
        return False

    def _confirm_history(self, record: Mapping[str, Any]) -> bool:
        delivery_id = str(record["delivery_id"])
        if record.get("history", True):
            return True
        self.fallback.parent.mkdir(parents=True, exist_ok=True)
        with self._lock_path(self.fallback).open("a+") as lock_handle:
            fcntl.flock(lock_handle, fcntl.LOCK_EX)
            try:
                raw = self.fallback.read_bytes()
            except FileNotFoundError:
                raw = b""
            if self._history_contains(raw, delivery_id):
                return True
            descriptor = os.open(self.fallback, os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o600)
            try:
                line = json.dumps(self._history_event(record), ensure_ascii=False).encode() + b"\n"
                pending = memoryview(line)
                while pending:
                    pending = pending[os.write(descriptor, pending) :]
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
            self._fsync_parent(self.fallback)
            return True

    def _flush(  # noqa: C901, PLR0912, PLR0915 — preserves each known partial count
        self, *, limit: int | None = None, blocking: bool = True
    ) -> FlushReport:
        if not self.url:
            return FlushReport()
        lock_path = self.queue.with_name(f"{self.queue.name}.flush.lock")
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        with lock_path.open("a+") as flush_lock:
            operation = fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB)
            try:
                fcntl.flock(flush_lock, operation)
            except BlockingIOError:
                try:
                    records, corrupt = self._read_queue()
                except OSError:
                    log.exception("could not inspect busy event replay queue %s", self.queue)
                    return FlushReport(busy=1, errors=1, unknown=1)
                return FlushReport(pending=len(records), corrupt=len(corrupt), busy=1)
            try:
                records, corrupt = self._read_queue()
            except OSError:
                log.exception("could not read event replay queue %s", self.queue)
                return FlushReport(errors=1, unknown=1)
            matching: list[tuple[dict[str, Any], set[tuple[str, str, str]]]] = []
            matching_groups: dict[tuple[str, str, str], set[tuple[str, str, str]]] = {}
            for record in records:
                delivery_id = str(record["delivery_id"])
                if record["target"] != self.url:
                    continue
                key = (
                    (
                        str(record["target"]),
                        "legacy-content",
                        self._event_fingerprint(record["event"]),
                    )
                    if delivery_id.startswith("legacy_")
                    else (str(record["target"]), "delivery-id", delivery_id)
                )
                ids = matching_groups.get(key)
                if ids is None:
                    ids = set()
                    matching_groups[key] = ids
                    matching.append((record, ids))
                ids.add(self._record_identity(record))
            selected = matching[:limit] if limit is not None else matching
            retired: set[tuple[str, str, str]] = set()
            delivered = 0
            failed = 0
            errors = 0
            history_confirmed: set[tuple[str, str, str]] = set()
            foreign = sum(record["target"] != self.url for record in records)
            for record, equivalent_ids in selected:
                try:
                    history_ok = self._confirm_history(record)
                except OSError:
                    log.exception("could not inspect watchdog history %s", self.fallback)
                    history_ok = False
                if not history_ok:
                    errors += 1
                    break
                if not record.get("history", True):
                    history_confirmed.add(self._record_identity(record))
                body = json.dumps(record["event"], ensure_ascii=False).encode("utf-8")
                if not self._post(self.url, body, str(record["delivery_id"])):
                    self._trip_breaker(self.url)
                    failed += 1
                    break
                retired.update(equivalent_ids)
                delivered += 1
            retired_records = sum(self._record_identity(record) in retired for record in records)
            if retired or corrupt or history_confirmed:
                try:
                    if history_confirmed:
                        self._rewrite_queue(retired, history_confirmed=history_confirmed)
                    else:
                        self._rewrite_queue(retired)
                except OSError:
                    log.exception("could not compact event replay queue %s", self.queue)
                    return FlushReport(
                        delivered=delivered,
                        retired_records=retired_records,
                        pending=len(records),
                        corrupt=len(corrupt),
                        foreign=foreign,
                        failed=failed,
                        errors=errors + 1,
                        unknown=1,
                    )
            try:
                remaining, remaining_corrupt = self._read_queue()
            except OSError:
                log.exception("could not confirm event replay queue %s", self.queue)
                return FlushReport(
                    delivered=delivered,
                    retired_records=retired_records,
                    corrupt=len(corrupt),
                    foreign=foreign,
                    failed=failed,
                    errors=errors + 1,
                    unknown=1,
                )
            return FlushReport(
                delivered=delivered,
                retired_records=retired_records,
                pending=len(remaining),
                corrupt=len(corrupt) + len(remaining_corrupt),
                foreign=foreign,
                failed=failed,
                errors=errors,
            )

    def flush(self, *, limit: int | None = None, blocking: bool = True) -> FlushReport:
        """Replay queued events oldest first; stop at failure and never raise."""
        try:
            return self._flush(limit=limit, blocking=blocking)
        except OSError:
            log.exception("could not flush event replay queue %s", self.queue)
            return FlushReport(errors=1, unknown=1)

    def import_legacy(self, path: Path | None = None) -> ImportReport:
        """Queue each distinct ID-less event in an old log once while it is pending.

        A history record with a valid ``steward_delivery_id`` is modern: its matching
        queue record is already replay authority, or it was acknowledged and retired.
        Requeueing it under a legacy hash would escape that dedupe domain, so it is
        skipped. ID-less records receive content-stable IDs and are deduplicated under
        the history and queue locks. Once such a record is delivered and retired there
        is no durable local seen-set, so a later import can still queue it again.
        """
        source = path or self.fallback
        if not self.url:
            return ImportReport()
        source.parent.mkdir(parents=True, exist_ok=True)
        try:
            with self._lock_path(source).open("a+") as source_lock:
                fcntl.flock(source_lock, fcntl.LOCK_EX)
                try:
                    lines = source.read_bytes().splitlines()
                except FileNotFoundError:
                    return ImportReport()
                return self._import_legacy_lines(lines)
        except OSError:
            log.exception("could not read legacy event log %s", source)
            return ImportReport(errors=1, unknown=1)

    def _import_legacy_lines(  # noqa: C901, PLR0912, PLR0915 — explicit outcome accounting
        self, lines: Sequence[bytes]
    ) -> ImportReport:
        """Import one source snapshot while its caller retains the history lock."""
        candidates: list[tuple[str, dict[str, Any], str]] = []
        skipped_modern = 0
        corrupt = 0
        seen_source: set[str] = set()
        for raw in lines:
            try:
                event = json.loads(raw)
            except UnicodeDecodeError, json.JSONDecodeError:
                corrupt += 1
                continue
            if not isinstance(event, dict) or validate_event(event):
                corrupt += 1
                continue
            modern_id = event.get("steward_delivery_id")
            if isinstance(modern_id, str) and re.fullmatch(r"[A-Za-z0-9_-]{16,128}", modern_id):
                skipped_modern += 1
                continue
            delivery_id = self._legacy_delivery_id(event)
            fingerprint = self._event_fingerprint(event)
            candidates.append((delivery_id, event, fingerprint))

        self.queue.parent.mkdir(parents=True, exist_ok=True)
        imported = 0
        skipped_duplicate = 0
        failed = 0
        committed = False
        try:
            with self._lock_path(self.queue).open("a+") as queue_lock:
                fcntl.flock(queue_lock, fcntl.LOCK_EX)
                try:
                    original = self.queue.read_bytes()
                except FileNotFoundError:
                    original = b""
                records, _queue_corrupt = self._read_queue_unlocked()
                pending_ids = {
                    (str(record["target"]), str(record["delivery_id"])) for record in records
                }
                pending_content = {
                    (str(record["target"]), self._event_fingerprint(record["event"]))
                    for record in records
                }
                staged: list[bytes] = []
                for delivery_id, event, fingerprint in candidates:
                    content_key = (str(self.url), fingerprint)
                    if (
                        (str(self.url), delivery_id) in pending_ids
                        or fingerprint in seen_source
                        or content_key in pending_content
                    ):
                        skipped_duplicate += 1
                        continue
                    record = {
                        "queue_v": QUEUE_VERSION,
                        "delivery_id": delivery_id,
                        "target": self.url,
                        "event": event,
                    }
                    staged.append(json.dumps(record, ensure_ascii=False).encode("utf-8") + b"\n")
                    pending_ids.add((str(self.url), delivery_id))
                    pending_content.add(content_key)
                    seen_source.add(fingerprint)
                    imported += 1
                if staged:
                    separator = b"\n" if original and not original.endswith(b"\n") else b""
                    staging = self.queue.with_name(f".{self.queue.name}.{uuid.uuid4().hex}.tmp")
                    try:
                        descriptor = os.open(staging, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
                        try:
                            pending = memoryview(original + separator + b"".join(staged))
                            while pending:
                                written = os.write(descriptor, pending)
                                if written <= 0:
                                    raise OSError("short write while staging legacy import")
                                pending = pending[written:]
                            os.fsync(descriptor)
                        finally:
                            os.close(descriptor)
                        staging.replace(self.queue)
                        committed = True
                        self._fsync_parent(self.queue)
                    finally:
                        with contextlib.suppress(FileNotFoundError):
                            staging.unlink()
        except OSError:
            log.exception("could not atomically commit imported legacy events")
            if committed:
                return ImportReport(
                    scanned=len(lines),
                    imported=imported,
                    skipped_modern=skipped_modern,
                    skipped_duplicate=skipped_duplicate,
                    corrupt=corrupt,
                    errors=1,
                    unknown=1,
                )
            failed = imported
            imported = 0
            return ImportReport(
                scanned=len(lines),
                imported=0,
                skipped_modern=skipped_modern,
                skipped_duplicate=skipped_duplicate,
                corrupt=corrupt,
                failed=failed,
                errors=1,
                unknown=1,
            )
        return ImportReport(
            scanned=len(lines),
            imported=imported,
            skipped_modern=skipped_modern,
            skipped_duplicate=skipped_duplicate,
            corrupt=corrupt,
            failed=failed,
        )

    def emit(self, event: Event) -> bool:
        """Deliver one event and keep a local copy. Returns remote reach. Never raises.

        The local log is written on *every* event, delivered or not, because it is the
        watchdog's record of what actually ran: a closing event that reached burrow but
        was never written locally would leave a completed run looking unbracketed. The
        return value still reports only whether the event reached a remote target.
        """
        line = event.to_json()
        if not self.url:
            self._append_line(self.fallback, line, purpose="in the watchdog log")
            return False
        if not self.url.startswith(("http://", "https://")):
            self._append_line(self.fallback, line, purpose="in the watchdog log")
            self._trip_breaker(self.url)
            return False
        delivery_id = uuid.uuid4().hex
        queued = self._queue_record(event, delivery_id, history=False)
        if not queued:
            # Posting without durable retry authority would make an ambiguous response
            # unrecoverable. Keep the complete local log available for explicit import.
            self._append_line(self.fallback, line, purpose="in the watchdog log")
            return False
        # History is reconciled by flush before POST. A crash at any point leaves the
        # queue as authority; its stable marker makes a repeated append detectable.
        try:
            records, _ = self._read_queue()
        except OSError:
            log.exception("could not recover queued event history from %s", self.queue)
            return False
        own_record = next(
            (record for record in records if record["delivery_id"] == delivery_id), None
        )
        try:
            history_ok = own_record is not None and self._confirm_history(own_record)
        except OSError:
            log.exception("could not inspect watchdog history %s", self.fallback)
            history_ok = False
        if not history_ok:
            return False
        if self._breaker_open(self.url):
            return False
        self.flush(limit=REPLAY_BATCH_SIZE, blocking=False)
        try:
            remaining, _ = self._read_queue()
        except OSError:
            log.exception("could not confirm event delivery from replay queue %s", self.queue)
            return False
        return not any(r["delivery_id"] == delivery_id for r in remaining)

    def emit_durable(self, event: Event) -> bool:
        """Deliver an event and report whether any durable sink accepted it.

        Unlike :meth:`emit`'s historical remote-only receipt, this is the acknowledgement
        durable outboxes need: a successful remote POST or a successful fallback append
        is enough. Total transport and fallback failure remains non-raising but returns
        ``False``, so the outbox stays pending.
        """
        line = event.to_json()
        delivered = False
        if self.url and not self._breaker_open(self.url):
            if self._post(self.url, line.encode("utf-8")):
                delivered = True
            else:
                self._trip_breaker(self.url)
        persisted = self._append_line(self.fallback, line, purpose="in the watchdog log")
        return delivered or persisted

    def emit_many(self, events: Sequence[Event]) -> None:
        """Deliver several events in order."""
        for event in events:
            self.emit(event)

    def __repr__(self) -> str:
        """Describe the target without ever showing the token."""
        target = self.url or "local"
        digest = hashlib.sha256(self.token.encode()).hexdigest()[:8] if self.token else "none"
        return f"<EventEmitter target={target} token={digest} fallback={self.fallback}>"


# --------------------------------------------------------------------------------------
# steward's event types
# --------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RunContext:
    """Who is running and where, so a routine event says it once instead of six times."""

    agent_id: str
    project: str
    routine: str
    run_id: str
    cwd: str | None = None

    def _event(self, event_type: str, payload: Mapping[str, Any]) -> Event:
        return Event(
            type=event_type,
            agent_id=self.agent_id,
            project=self.project,
            cwd=self.cwd,
            payload={"routine": self.routine, "run_id": self.run_id, **payload},
        )

    def started(self, trigger: str) -> Event:
        """Announce that a routine's session is about to start."""
        return self._event(ROUTINE_STARTED, {"trigger": trigger})

    def finished(self, *, outcome: str, artifacts: Sequence[str], duration_s: float) -> Event:
        """Report a session that ended on its own terms, and what it left behind."""
        return self._event(
            ROUTINE_FINISHED,
            {
                "outcome": outcome,
                "artifacts": list(artifacts),
                "duration_s": round(duration_s, 3),
            },
        )

    def failed(self, *, error: str, duration_s: float) -> Event:
        """Report a run that failed or was killed at its timeout. Never left silent."""
        return self._event(
            ROUTINE_FAILED,
            {"error": truncate_error(error), "duration_s": round(duration_s, 3)},
        )


def routine_failed_event(
    *, agent_id: str, project: str, routine: str, run_id: str, error: str
) -> Event:
    """Close a run's bracket from outside the run, when the run itself never did.

    :meth:`RunContext.failed` is what a scheduler that watched a session emits; this is
    what the watchdog emits for a session nobody watched to the end — the daemon was
    killed, the machine rebooted, the process vanished. There is no ``duration_s`` in the
    payload on purpose: steward does not know how long that run lasted, only that it
    started and never came back, and a made-up duration would be exactly the kind of
    plausible detail this project refuses to invent.
    """
    return Event(
        type=ROUTINE_FAILED,
        agent_id=agent_id,
        project=project,
        payload={"routine": routine, "run_id": run_id, "error": truncate_error(error)},
    )


def resident_restarted_event(
    *, agent_id: str, project: str, reason: str, attempt: int, supervisor: str = ""
) -> Event:
    """Say out loud that steward took a resident down and brought it back.

    A silent restart is a lie by omission: the village would show an unbroken villager
    where a process actually died. ``attempt`` is the number of this try within the
    watchdog's bounded budget, so the log shows a crash loop as a crash loop rather than
    as three unrelated hiccups.
    """
    payload: dict[str, Any] = {"reason": truncate_error(reason), "attempt": attempt}
    if supervisor:
        payload["supervisor"] = supervisor
    return Event(type=RESIDENT_RESTARTED, agent_id=agent_id, project=project, payload=payload)


def resident_declared_event(*, resident: Resident) -> Event:
    """Publish only the display-safe identity steward authoritatively declares."""
    manifest = resident.manifest
    soul = manifest.soul
    return Event(
        type=RESIDENT_DECLARED,
        agent_id=resident.agent_id,
        project=resident.project,
        payload={
            "name": soul.name,
            "char": soul.char,
            "accent": soul.accent,
            "role": soul.role,
            "summary": manifest.summary,
            "resident_id": resident.id,
            "uid": resident.uid,
            "home": manifest.home,
        },
    )


def resident_retired_event(*, resident: Resident) -> Event:
    """Publish the honest terminal counterpart of a resident declaration."""
    return Event(
        type=RESIDENT_RETIRED,
        agent_id=resident.agent_id,
        project=resident.project,
        payload={"resident_id": resident.id, "uid": resident.uid},
    )


def task_posted_event(  # noqa: PLR0913 — every field is keyword-only and part of the payload
    *,
    task_id: str,
    title: str,
    required_skills: Sequence[str] = (),
    posted_by: str = "api",
    agent_id: str = API_AGENT_ID,
    project: str = API_PROJECT,
) -> Event:
    """Announce that a task landed on the job board (steward #6's first transition).

    The board's own storage is steward's; the *board* burrow renders is rebuilt from
    these events alone, so a task that was never announced does not exist as far as
    the village is concerned.
    """
    return Event(
        type=TASK_POSTED,
        agent_id=agent_id,
        project=project,
        payload={
            "task_id": task_id,
            "title": title,
            "required_skills": list(required_skills),
            "posted_by": posted_by,
        },
    )


def _lineage(parent_task_id: str | None) -> dict[str, Any]:
    """Add ``parent_task_id`` to a payload, and only when there is one.

    A task nobody delegated has no parent, and saying ``"parent_task_id": null`` on every
    board task would be steward answering a question that was not asked. The key appears
    exactly when the fact does, so the payload of an ordinary claim is byte-identical to
    what it was before delegation existed.
    """
    return {"parent_task_id": parent_task_id} if parent_task_id else {}


def _session(run_id: str | None) -> dict[str, Any]:
    """Name the session a task's close came out of, and only when there was one.

    A ``task_done`` / ``task_failed`` names a *task*, and a task id outlives its attempts:
    the board's ordinary flow is claim, die, expire the lease, re-claim, and every attempt
    carries the same id. ``run_id`` is what tells those attempts apart — it is the id of
    the run registry row this close answers, so the watchdog can match a close to the one
    session it belongs to instead of guessing from timestamps (steward #39).

    The lease sweep's ``task_failed`` has no session to name — it is the board mourning a
    claim, not a run reporting back — so the key is absent there, and absent is the whole
    point: a close that names no session answers no session's row.
    """
    return {"run_id": run_id} if run_id else {}


def task_claimed_event(
    *, task_id: str, title: str, claimant: str, project: str, parent_task_id: str | None = None
) -> Event:
    """Announce that one resident, and only one, now holds this task.

    Emitted under the claimant's own ``agent_id`` rather than steward's, so burrow walks
    *that* villager to the notice board. Exactly one of these can exist per claim: the
    store's conditional ``UPDATE … WHERE status = 'open'`` is what makes that true, and
    the loser of a race emits nothing at all.

    A delegated item is picked up through this same event, carrying the ``parent_task_id``
    it was handed under, so the chain from the first task to the last is readable from the
    log alone.
    """
    return Event(
        type=TASK_CLAIMED,
        agent_id=claimant,
        project=project,
        payload={
            "task_id": task_id,
            "title": title,
            "claimant": claimant,
            **_lineage(parent_task_id),
        },
    )


def task_done_event(  # noqa: PLR0913 — every field is keyword-only and part of the payload
    *,
    task_id: str,
    title: str,
    claimant: str,
    project: str,
    artifacts: Sequence[str] = (),
    parent_task_id: str | None = None,
    run_id: str | None = None,
) -> Event:
    """Report a claimed task the resident finished, and what it left behind.

    ``artifacts`` is best effort, exactly as it is for a routine: steward reports what
    the run actually named and never invents a file it did not see.

    ``run_id`` names the session that finished it, so this close can be told from the
    close of an earlier attempt at the same task — see :func:`_session`.
    """
    return Event(
        type=TASK_DONE,
        agent_id=claimant,
        project=project,
        payload={
            "task_id": task_id,
            "title": title,
            "claimant": claimant,
            "artifacts": list(artifacts),
            **_session(run_id),
            **_lineage(parent_task_id),
        },
    )


def task_failed_event(  # noqa: PLR0913 — every field is keyword-only and part of the payload
    *,
    task_id: str,
    title: str,
    claimant: str,
    project: str,
    reason: str,
    parent_task_id: str | None = None,
    run_id: str | None = None,
) -> Event:
    """Report a task its claimant did not finish — including one whose lease ran out.

    A lease expiry is a failure with the reason ``lease_expired``, not a silence: work
    that quietly returned to the board would let the village show a task nobody is doing
    as a task somebody is doing. A delegated item that nobody finished fails the same way,
    still naming its parent, so the chain shows where the work stopped.

    ``run_id`` names the session that failed, where there was one. A lease expiry passes
    none: the session it mourns is precisely the one that never reported back, and saying
    otherwise would answer its registry row — see :func:`_session`.
    """
    return Event(
        type=TASK_FAILED,
        agent_id=claimant,
        project=project,
        payload={
            "task_id": task_id,
            "title": title,
            "claimant": claimant,
            "reason": truncate_error(reason),
            **_session(run_id),
            **_lineage(parent_task_id),
        },
    )


def task_session_finished_event(  # noqa: PLR0913 — one keyword per reported fact
    *,
    task_id: str,
    title: str,
    claimant: str,
    project: str,
    run_id: str,
    outcome: str,
    artifacts: Sequence[str] = (),
    duration_s: float = 0.0,
    reason: str,
) -> Event:
    """Report a session that returned after losing its claim, without closing the task.

    This is deliberately not ``task_done`` or ``task_failed``: both describe the board
    row, while this fact describes only the late session.  The lease sweep remains the
    authority on the task and this event names the particular run that reported back.
    """
    return Event(
        type=TASK_SESSION_FINISHED,
        agent_id=claimant,
        project=project,
        payload={
            "task_id": task_id,
            "title": title,
            "claimant": claimant,
            "run_id": run_id,
            "outcome": outcome,
            "artifacts": list(artifacts),
            "duration_s": round(duration_s, 3),
            "reason": truncate_error(reason),
        },
    )


def task_delegated_event(  # noqa: PLR0913 — the payload the issue documents
    *,
    task_id: str,
    title: str,
    sender: str,
    recipient: str,
    route: str,
    project: str,
    depth: int,
    parent_task_id: str | None = None,
) -> Event:
    """Announce that one resident handed work to another, and name both ends.

    Emitted under the **delegating** resident's agent id, because the villager walking
    across the village is the one carrying the letter. ``to`` names the doorway it
    is walking to, and ``route`` names which door — a resident may declare more than one.

    Nothing else in the chain needs a new event type: the receiver picks the item up with
    ``task_claimed`` and closes it with ``task_done``/``task_failed``, both carrying the
    same ``parent_task_id``, so the whole handoff is reconstructible from the log.
    """
    return Event(
        type=TASK_DELEGATED,
        agent_id=sender,
        project=project,
        payload={
            "task_id": task_id,
            "title": title,
            "from": sender,
            "to": recipient,
            "route": route,
            "parent_task_id": parent_task_id,
            "depth": depth,
        },
    )


def chat_message_dropped_event(  # noqa: PLR0913 — one keyword per fact worth recording
    *,
    agent_id: str,
    project: str,
    route: str,
    address: str,
    sender: str,
    reason: str,
    suppressed: int = 0,
) -> Event:
    """Say that a message reached a resident's chat route and was dropped without a reply.

    The visible half of the chat bridge's auth rule (warren#108). A message from anybody
    who is not a named operator gets *no* answer — a reply would confirm to a scanner that
    something is listening on that bot — but silence at both ends would mean an operator
    could never find out that somebody had found their resident. So the drop is a fact in
    the village: which resident's door, which route, and who knocked.

    **What they said is deliberately not here.** A stranger's text is the one string in this
    system written by somebody steward has no relationship with, and the village renders
    what it is given: putting it in an event would make a chat bot a way to publish text
    into the operator's own panel. The sender id is enough to recognise a wrong number, add
    a second account to :data:`steward.chat.OPERATORS_ENV`, or notice a stranger — and it is
    the only part of the message steward needs to keep.

    ``suppressed`` is how many *other* knocks this one record stands for (warren#278). It
    is the one field here a stranger does not cause: this is the only event in the log
    somebody outside the fleet triggers, so :class:`steward.chat.KnockLimiter` emits one
    per door per stranger per window and counts the rest into the next record. A reader
    who wants the number of knocks adds one to it; a reader who does not care sees the
    zero every unrepeated knock carries.
    """
    return Event(
        type=CHAT_MESSAGE_DROPPED,
        agent_id=agent_id,
        project=project,
        payload={
            "route": route,
            "address": address,
            "from": truncate_error(sender),
            "reason": truncate_error(reason),
            "suppressed": suppressed,
        },
    )


def needs_human_event(  # noqa: PLR0913 — the payload the protocol documents
    *,
    message: str,
    request_id: str,
    action: str,
    agent_id: str,
    project: str,
    detail: Mapping[str, Any] | None = None,
    options: Sequence[str] = (),
    expires_at: str | None = None,
) -> Event:
    """Knock at the door with a question that can actually be answered.

    Backwards compatible on purpose: ``message`` is still the one-line knock burrow
    renders and ntfy forwards, and everything else is additive. A consumer that only
    knows the old bare ``needs_human`` keeps working unchanged; one that knows the new
    fields can offer the buttons.

    Both ``message`` and ``detail`` are bounded here rather than trusted from the caller:
    a knock is a notice, and a session that prints a 200KB escalation must not be able to
    turn that into a 200KB event POSTed to burrow.

    They are also *scrubbed* here (steward #65): a secret a session writes into its
    message or a detail value — an ``sk-…`` key, a ``BURROW_TOKEN=…``, a PEM/JWT/URL
    password — is redacted before it can leave the village, and redacted *before* it is
    bounded so a secret cut in half by the length cap can never surface a live prefix.
    """
    return Event(
        type=NEEDS_HUMAN,
        agent_id=agent_id,
        project=project,
        payload={
            "message": truncate_error(redact_secrets(message)),
            "request_id": request_id,
            "action": action,
            "detail": bounded_detail(redact_mapping(detail)),
            "options": list(options),
            "expires_at": expires_at,
        },
    )


def needs_human_resolved_event(  # noqa: PLR0913 — the payload the protocol documents
    *,
    request_id: str,
    decision: str,
    action: str,
    agent_id: str,
    project: str,
    decided_by: str = "api",
) -> Event:
    """Close the loop on a knock at the door: the human answered, and this is the answer.

    Emitted under the *resident's* identity, not steward's, because the villager walking
    away from your door is the one who knocked.
    """
    return Event(
        type=NEEDS_HUMAN_RESOLVED,
        agent_id=agent_id,
        project=project,
        payload={
            "request_id": request_id,
            "decision": decision,
            "decided_by": decided_by,
            "action": action,
        },
    )
