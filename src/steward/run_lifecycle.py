"""Run ownership and terminal-close transition seam.

A timeout is a deadline for work, not proof that its owner vanished. This seam keeps a
durable lease alive for the entire session lifecycle and makes the session and watchdog
compete for the same conditional close in SQLite.
"""

import logging
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import Protocol

from steward import events as ev

log = logging.getLogger("steward.run_lifecycle")
RUN_HEARTBEAT_EVERY_S = 15.0


class RunStore(Protocol):
    """Storage operations needed by run-close arbitration."""

    def renew_run(self, run_id: str, *, now: str | None = None) -> bool:
        """Renew an open run."""
        ...

    def close_run(self, run_id: str, *, now: str | None = None) -> bool:
        """Claim a normal terminal close."""
        ...

    def close_stale_run(self, run_id: str, *, stale_before: str, now: str | None = None) -> bool:
        """Claim a watchdog close for an expired lease."""
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
    def owned(self, run_id: str) -> Iterator[None]:
        """Renew ``run_id`` through its result/accounting/event tail."""
        stop = threading.Event()

        def beat() -> None:
            while not stop.wait(self.heartbeat_every_s):
                try:
                    if not self.store.renew_run(run_id):
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

    def session_close(self, run_id: str, *, now: datetime) -> bool:
        """Let the session atomically claim the terminal close."""
        return self.store.close_run(run_id, now=ev.utc_now_iso(now))

    def watchdog_close(self, run_id: str, *, now: datetime, grace_s: float) -> bool:
        """Let the watchdog close only a lease silent for a full grace."""
        return self.store.close_stale_run(
            run_id,
            stale_before=ev.utc_now_iso(now - timedelta(seconds=grace_s)),
            now=ev.utc_now_iso(now),
        )
