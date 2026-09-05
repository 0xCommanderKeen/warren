"""Thread-safe coordinator for authoritative Village State snapshots."""

from __future__ import annotations

import copy
import datetime as dt
import threading

import retention
from village_state import ProjectionPolicy, project_village


class StateCoordinator:
    """Read projection inputs and atomically replace complete snapshots.

    ``read_events`` returns ``(events, cursor, log_generation)``.  Keeping this
    adapter injectable makes the coordinator testable without files or HTTP.
    """

    def __init__(
        self,
        read_events,
        read_residents,
        policy=None,
        capabilities=None,
        *,
        read_updates=None,
        apply_telemetry=None,
    ):
        self._apply_telemetry = apply_telemetry
        self._read_events = read_events
        self._read_updates = read_updates
        self._read_residents = read_residents
        self._policy = policy or ProjectionPolicy()
        self._capabilities = capabilities or {}
        self._lock = threading.Condition()
        self._snapshot = None
        self._signature = None
        self._generation = 0
        self._log_generation = None
        self._fold = retention.ProjectionFold()
        self._initialized = False
        self._cursor = None

    def evaluate(self, evaluated_at=None):
        evaluated_at = evaluated_at or dt.datetime.now(dt.UTC)
        with self._lock:
            # Capture, project, and publish as one serialized operation. Otherwise
            # a slow projection of an older cursor can overtake a newer one and be
            # assigned the higher public generation.
            if not self._initialized or self._read_updates is None:
                events, cursor, log_generation = self._read_events()
                events = self._fold.replace(events, evaluated_at)
                self._initialized = True
            else:
                updates, cursor, log_generation, reset = self._read_updates(
                    self._cursor
                )
                if reset:
                    events = self._fold.replace(updates, evaluated_at)
                else:
                    events = self._fold.extend(updates, evaluated_at)
            self._cursor = cursor
            residents = self._read_residents()
            candidate = project_village(
                events,
                residents,
                evaluated_at,
                self._policy,
                cursor=cursor,
                generation=0,
                capabilities=self._capabilities,
            )
            if self._apply_telemetry is not None:
                self._apply_telemetry(candidate, residents, evaluated_at)
            semantic = dict(candidate)
            semantic.pop("generation", None)
            semantic.pop("evaluated_at", None)
            signature = semantic
            if self._snapshot is not None and signature == self._signature:
                return self._snapshot
            self._generation += 1
            candidate["generation"] = self._generation
            candidate["log_generation"] = int(log_generation)
            self._snapshot = candidate
            self._signature = signature
            self._log_generation = log_generation
            self._lock.notify_all()
            return self._snapshot

    def snapshot(self):
        with self._lock:
            return self._snapshot

    def delivery(self, generation=None, cursor=None):
        with self._lock:
            return self._delivery_locked(generation, cursor)

    def evaluate_delivery(self, generation=None, cursor=None, evaluated_at=None):
        """Evaluate and select one delivery from the same published snapshot."""
        with self._lock:
            self.evaluate(evaluated_at)
            return self._delivery_locked(generation, cursor)

    def _delivery_locked(self, generation, cursor):
        snapshot = self._snapshot
        if snapshot is None:
            return {"kind": "unavailable"}
        if cursor is not None and self._cursor_namespace(
            cursor
        ) != self._cursor_namespace(snapshot["cursor"]):
            return {"kind": "reset", "snapshot": copy.deepcopy(snapshot)}
        if generation is not None and generation >= snapshot["generation"]:
            return {
                "kind": "unchanged",
                "generation": snapshot["generation"],
                "cursor": snapshot["cursor"],
            }
        return {"kind": "snapshot", "snapshot": copy.deepcopy(snapshot)}

    @staticmethod
    def _cursor_namespace(cursor):
        parts = str(cursor).split(":")
        return tuple(parts[:5]) if len(parts) == 6 and parts[0] == "v1" else None

    def wait_for_newer(self, generation, timeout):
        with self._lock:
            self._lock.wait_for(
                lambda: self._snapshot is not None
                and self._snapshot["generation"] > generation,
                timeout=timeout,
            )
            return self._snapshot
