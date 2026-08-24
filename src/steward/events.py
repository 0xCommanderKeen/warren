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
- Anything not delivered is appended to a local JSONL fallback
  (``STEWARD_EVENTS_FALLBACK``, default ``~/.burrow/events.jsonl``) — the same file
  burrow's emitter falls back to, so nothing is lost, only its remoteness.

Emitting never raises. A village that cannot be reached must not turn into a failed
routine: that would be steward lying about its own work.
"""

import hashlib
import json
import os
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

__all__ = [
    "API_AGENT_ID",
    "API_PROJECT",
    "BREAKER_SECONDS",
    "ERROR_MAX_CHARS",
    "EVENT_SOURCE",
    "EVENT_TYPES",
    "EVENT_VERSION",
    "LOOPBACK_BREAKER_SECONDS",
    "POST_TIMEOUT_S",
    "Emitter",
    "Event",
    "EventEmitter",
    "NullEmitter",
    "RunContext",
    "default_fallback_path",
    "needs_human_event",
    "needs_human_resolved_event",
    "task_claimed_event",
    "task_done_event",
    "task_failed_event",
    "task_posted_event",
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
NEEDS_HUMAN = "needs_human"
NEEDS_HUMAN_RESOLVED = "needs_human_resolved"

#: The event types steward adds to the protocol. Additive: a v0 consumer that does not
#: know them ignores them, which is why burrow needs no change to stay correct.
EVENT_TYPES = (
    ROUTINE_STARTED,
    ROUTINE_FINISHED,
    ROUTINE_FAILED,
    TASK_POSTED,
    TASK_CLAIMED,
    TASK_DONE,
    TASK_FAILED,
    NEEDS_HUMAN,
    NEEDS_HUMAN_RESOLVED,
)

#: Steward's own identity, for the work steward itself does rather than a resident.
#: A job posted through the API is posted by the API, and the log should say so.
API_AGENT_ID = "steward:api"
API_PROJECT = "steward"

POST_TIMEOUT_S = 2.0
BREAKER_SECONDS = 60.0
LOOPBACK_BREAKER_SECONDS = 5.0
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "[::1]", "::1"})

#: Errors travel into the village as one line of explanation, never a transcript.
ERROR_MAX_CHARS = 500

FALLBACK_ENV = "STEWARD_EVENTS_FALLBACK"
URL_ENV = "BURROW_URL"
TOKEN_ENV = "BURROW_TOKEN"  # noqa: S105 — an env var name, not a credential

_REQUIRED_FIELDS = ("v", "ts", "source", "agent_id", "project", "type", "payload")
_TS_FORMAT = "%Y-%m-%dT%H:%M:%S.%fZ"


def default_fallback_path() -> Path:
    """Where undelivered events land: ``$STEWARD_EVENTS_FALLBACK`` or ``~/.burrow``."""
    configured = (os.environ.get(FALLBACK_ENV) or "").strip()
    if configured:
        return Path(configured).expanduser()
    return Path.home() / ".burrow" / "events.jsonl"


def truncate_error(text: str, limit: int = ERROR_MAX_CHARS) -> str:
    """Shorten an error to something a villager panel can hold, marking the cut."""
    collapsed = " ".join(str(text).split())
    if len(collapsed) <= limit:
        return collapsed
    return collapsed[: limit - 1].rstrip() + "…"


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

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> EventEmitter:
        """Build an emitter from ``BURROW_URL``/``BURROW_TOKEN``/the fallback var."""
        source = os.environ if env is None else env
        fallback = (source.get(FALLBACK_ENV) or "").strip()
        return cls(
            url=source.get(URL_ENV),
            token=source.get(TOKEN_ENV),
            fallback=Path(fallback).expanduser() if fallback else None,
        )

    # -- transport -------------------------------------------------------------------

    def _breaker_open(self, url: str) -> bool:
        until = self._breaker_until.get(url)
        return until is not None and self._clock() < until

    def _trip_breaker(self, url: str) -> None:
        window = LOOPBACK_BREAKER_SECONDS if _is_loopback(url) else BREAKER_SECONDS
        self._breaker_until[url] = self._clock() + window

    def _post(self, url: str, body: bytes) -> bool:
        if not url.startswith(("http://", "https://")):
            return False
        headers = {"Content-Type": "application/json"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        request = urllib.request.Request(  # noqa: S310 — scheme checked just above
            f"{url}/events", data=body, headers=headers, method="POST"
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout_s):  # noqa: S310
                return True
        except OSError, urllib.error.URLError, ValueError:
            return False

    def _append_fallback(self, line: str) -> None:
        try:
            self.fallback.parent.mkdir(parents=True, exist_ok=True)
            with self.fallback.open("a", encoding="utf-8") as handle:
                handle.write(line + "\n")
        except OSError:
            # The village losing an event must never take a routine down with it.
            pass

    def emit(self, event: Event) -> bool:
        """Deliver one event, falling back to the local log. Never raises."""
        line = event.to_json()
        if self.url and not self._breaker_open(self.url):
            if self._post(self.url, line.encode("utf-8")):
                return True
            self._trip_breaker(self.url)
        self._append_fallback(line)
        return False

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


def task_claimed_event(*, task_id: str, title: str, claimant: str, project: str) -> Event:
    """Announce that one resident, and only one, now holds this task.

    Emitted under the claimant's own ``agent_id`` rather than steward's, so burrow walks
    *that* villager to the notice board. Exactly one of these can exist per claim: the
    store's conditional ``UPDATE … WHERE status = 'open'`` is what makes that true, and
    the loser of a race emits nothing at all.
    """
    return Event(
        type=TASK_CLAIMED,
        agent_id=claimant,
        project=project,
        payload={"task_id": task_id, "title": title, "claimant": claimant},
    )


def task_done_event(
    *,
    task_id: str,
    title: str,
    claimant: str,
    project: str,
    artifacts: Sequence[str] = (),
) -> Event:
    """Report a claimed task the resident finished, and what it left behind.

    ``artifacts`` is best effort, exactly as it is for a routine: steward reports what
    the run actually named and never invents a file it did not see.
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
        },
    )


def task_failed_event(
    *, task_id: str, title: str, claimant: str, project: str, reason: str
) -> Event:
    """Report a task its claimant did not finish — including one whose lease ran out.

    A lease expiry is a failure with the reason ``lease_expired``, not a silence: work
    that quietly returned to the board would let the village show a task nobody is doing
    as a task somebody is doing.
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
    """
    return Event(
        type=NEEDS_HUMAN,
        agent_id=agent_id,
        project=project,
        payload={
            "message": message,
            "request_id": request_id,
            "action": action,
            "detail": dict(detail or {}),
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

    Emitted under the *resident's* identity, not steward's, because the villager burrow
    has to walk away from your door is the one who knocked.
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
