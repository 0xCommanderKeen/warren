"""Run ownership and terminal-close transition seam.

A timeout is a deadline for work, not proof that its owner vanished. This seam keeps a
durable lease alive for the entire session lifecycle and makes the session and watchdog
compete for the same conditional close in SQLite.
"""

import json
import logging
import threading
import uuid
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Protocol, cast

from steward import events as ev

log = logging.getLogger("steward.run_lifecycle")
RUN_HEARTBEAT_EVERY_S = 15.0

#: How long a run's ownership lease survives without a heartbeat.
#:
#: One number with one home, because three things read it and they must agree: the watchdog
#: buries a run whose heartbeat went stale by this much (:data:`steward.watchdog.DEFAULT_GRACE_S`
#: is this constant), and a session credential is accepted only while its run could still
#: be renewed (steward #41). A credential with its own expiry would be a second clock, and
#: the run lifecycle is already the one that decides when a session is over.
RUN_LEASE_GRACE_S = 120.0


class RunStore(Protocol):
    """Storage operations needed by run-close arbitration."""

    def renew_run(self, run_id: str, *, owner_token: str = "", now: str | None = None) -> bool:
        """Renew an open run."""
        ...

    def close_run(self, run_id: str, *, now: str | None = None) -> bool:
        """Claim a normal terminal close."""
        ...

    def close_stale_run(self, run_id: str, *, stale_before: str, now: str | None = None) -> bool:
        """Claim a watchdog close for an expired lease."""
        ...

    def claim_run_terminal(  # noqa: PLR0913 - mirrors the durable store operation
        self,
        run_id: str,
        *,
        event: str,
        event_id: str,
        owner_token: str | None = None,
        stale_before: str | None = None,
        now: str | None = None,
    ) -> bool:
        """Choose an immutable terminal event under one authority condition."""
        ...

    def terminal_runs(self) -> Sequence[Any]:
        """Return chosen terminal events awaiting finalization."""
        ...

    def mark_run_terminal_published(
        self, run_id: str, event_id: str, *, now: str | None = None
    ) -> bool:
        """Finalize one published terminal event."""
        ...


def event_log_path(emitter: ev.Emitter) -> str:
    """Return the complete local event record used by this emitter, if exposed."""
    path = getattr(emitter, "fallback", None)
    return str(Path(path).resolve()) if path is not None else ""


class RunTransitions:
    """The single ownership seam shared by session owners and the watchdog."""

    def __init__(
        self, store: RunStore, *, heartbeat_every_s: float = RUN_HEARTBEAT_EVERY_S
    ) -> None:
        """Bind arbitration to one durable store."""
        self.store = store
        self.heartbeat_every_s = heartbeat_every_s

    @contextmanager
    def owned(
        self, run_id: str, owner_token: str = "", *, task_attempt: bool = False
    ) -> Iterator[None]:
        """Renew ``run_id`` through its result/accounting/event tail."""
        stop = threading.Event()

        def beat() -> None:
            while not stop.wait(self.heartbeat_every_s):
                try:
                    renew = (
                        cast("Any", self.store).renew_task_run
                        if task_attempt and hasattr(self.store, "renew_task_run")
                        else self.store.renew_run
                    )
                    if not renew(run_id, owner_token=owner_token):
                        return
                except Exception as exc:  # noqa: BLE001
                    log.warning("could not renew run %s ownership: %s", run_id, exc)

        thread = threading.Thread(target=beat, name=f"steward-run-{run_id[:8]}", daemon=True)
        thread.start()
        try:
            yield
        finally:
            stop.set()
            thread.join(timeout=self.heartbeat_every_s)

    def session_claim(
        self, run_id: str, event: ev.Event, *, owner_token: str, now: datetime
    ) -> bool:
        """Let the live owner durably choose its immutable terminal event."""
        event_id = f"run-terminal:{run_id}"
        event = _identified(event, event_id)
        store = cast("Any", self.store)
        if not hasattr(store, "claim_run_terminal"):
            return self.store.close_run(run_id, now=ev.utc_now_iso(now))
        return bool(
            store.claim_run_terminal(
                run_id,
                event=event.to_json(),
                event_id=event_id,
                owner_token=owner_token,
                now=ev.utc_now_iso(now),
            )
        )

    def task_session_claim(  # noqa: PLR0913
        self,
        job: Any,  # noqa: ANN401
        event: ev.Event,
        *,
        result: Any,  # noqa: ANN401
        claimant: str,
        owner_token: str,
        now: datetime,
    ) -> Any | None:  # noqa: ANN401
        """Atomically finish a board claim and choose the exact fact that reports it."""
        event_id = f"run-terminal:{event.payload['run_id']}"
        event = _identified(event, event_id)
        return cast("Any", self.store).finish_job_and_claim_run_terminal(
            job.task_id,
            run_id=event.payload["run_id"],
            event=event.to_json(),
            event_id=event_id,
            status="done" if result.ok else "failed",
            claimant=claimant,
            outcome=str(result.outcome),
            reason=None if result.ok else f"{result.outcome}: {result.summary()}",
            artifacts=result.artifacts,
            final_message=result.output,
            lease=job.claimed_at,
            owner_token=owner_token,
            now=ev.utc_now_iso(now),
        )

    def watchdog_claim(
        self, run_id: str, event: ev.Event, *, now: datetime, grace_s: float
    ) -> bool:
        """Let a watchdog choose failure only after the ownership heartbeat is stale."""
        event_id = f"run-terminal:{run_id}"
        event = _identified(event, event_id)
        return self.store.claim_run_terminal(
            run_id,
            event=event.to_json(),
            event_id=event_id,
            stale_before=ev.utc_now_iso(now - timedelta(seconds=grace_s)),
            now=ev.utc_now_iso(now),
        )

    def watchdog_close(self, run_id: str, *, now: datetime, grace_s: float) -> bool:
        """Compatibility spelling for old test doubles; new callers choose an event."""
        return self.store.close_stale_run(
            run_id,
            stale_before=ev.utc_now_iso(now - timedelta(seconds=grace_s)),
            now=ev.utc_now_iso(now),
        )

    def publish_pending(self, emitter: ev.Emitter, *, now: datetime) -> list[str]:
        """Replay every chosen fact and finalize it only after a durable sink accepts it."""
        published = []
        store = cast("Any", self.store)
        if not hasattr(store, "terminal_runs"):
            return published
        for run in store.terminal_runs():
            if not run.terminal_event or not run.terminal_event_id:
                continue
            event = _event_from_json(run.terminal_event)
            durable = getattr(emitter, "emit_durable", emitter.emit)(event)
            if durable and store.mark_run_terminal_published(
                run.run_id, run.terminal_event_id, now=ev.utc_now_iso(now)
            ):
                published.append(run.run_id)
        return published


def new_owner_token() -> str:
    """Return an unguessable process/session fencing token."""
    return str(uuid.uuid4())


def _identified(event: ev.Event, event_id: str) -> ev.Event:
    return ev.Event(
        type=event.type,
        agent_id=event.agent_id,
        project=event.project,
        cwd=event.cwd,
        ts=event.ts,
        source=event.source,
        v=event.v,
        payload={**event.payload, "event_id": event_id},
    )


def _event_from_json(raw: str) -> ev.Event:
    data = json.loads(raw)
    return ev.Event(
        type=data["type"],
        agent_id=data["agent_id"],
        project=data["project"],
        payload=data.get("payload", {}),
        cwd=data.get("cwd"),
        ts=data["ts"],
        source=data.get("source", ev.EVENT_SOURCE),
        v=data.get("v", ev.EVENT_VERSION),
    )
