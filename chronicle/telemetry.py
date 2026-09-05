"""Chronicle's durable authority for expiring producer health and runtime presence.

Delivery is not activity. Receipt cannot extend the lifetime of a producer's old
observation. Session epochs and sequences fence delayed/reordered observations;
terminal sessions remain terminal through worker and server restarts.
"""

import datetime as dt
import json
import threading
import time
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from hooks import presence
from village_state import project_village


class Observation(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)
    agent_id: str = Field(min_length=1, max_length=256)
    session_id: str = Field(min_length=1, max_length=256)
    epoch: int = Field(ge=0)
    sequence: int = Field(ge=0)
    observed_at: float = Field(ge=0)
    source: Literal["codex", "claude-code"]
    project: str = Field(max_length=256)
    state: Literal["working", "resting", "knocking", "failed", "ended"]


class ProducerHealth(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)
    producer: str = Field(pattern=r"^[A-Za-z0-9_-]{1,64}$")
    target: str = Field(pattern=r"^[A-Za-z0-9_-]{1,64}$")
    observed_at: float = Field(ge=0)
    queue_depth: int = Field(ge=0)
    oldest_at: float | None
    last_success: float | None
    error: (
        Literal[
            "dns",
            "connect",
            "timeout",
            "authentication",
            "invalid_event",
            "http",
            "disk",
            "worker",
        ]
        | None
    )
    retry_at: float
    failures: int = Field(ge=0)
    worker: Literal["running"]
    overflow: int = Field(ge=0)
    presence_overflow: int = Field(ge=0)


class TelemetryReport(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    health: ProducerHealth
    presence: list[Observation] = Field(max_length=presence.MAX_PRESENCE)


class PresenceWire(Observation):
    producer: str
    freshness: Literal["fresh", "unknown"]
    expires_at: float


class HealthWire(ProducerHealth):
    status: Literal["healthy", "delayed", "unknown", "overloaded"]
    expires_at: float
    received_at: float


class TelemetryStore:
    def __init__(self, path):
        self.path = str(path)
        self.lock = threading.RLock()

    def accept(self, report, now=None):
        now = time.time() if now is None else now
        wire = report.model_dump()
        if wire["health"]["observed_at"] > now + 5 or any(
            item["observed_at"] > now + 5 for item in wire["presence"]
        ):
            raise ValueError("observation is in the future")
        key = wire["health"]["producer"] + ":" + wire["health"]["target"]
        with self.lock, presence.transaction(self.path) as state:
            producers = state.setdefault("producers", {})
            previous = producers.get(key, {})
            old_health = previous.get("health", {})
            if wire["health"]["observed_at"] < old_health.get("observed_at", 0):
                return
            records = previous.get("presence", {})
            for item in wire["presence"]:
                identity = json.dumps([item["agent_id"], item["session_id"]])
                prior = records.get(identity)
                if prior and (
                    prior["state"] == "ended" or item["sequence"] <= prior["sequence"]
                ):
                    continue
                # Sequence and epoch belong to the runner session, not the sender process.
                if prior and item["epoch"] != prior["epoch"]:
                    continue
                records[identity] = item
            records = dict(
                sorted(records.items(), key=lambda pair: pair[1]["sequence"])[
                    -presence.MAX_PRESENCE :
                ]
            )
            producers[key] = dict(
                health=wire["health"], received_at=now, presence=records
            )
            if len(producers) > 64:
                del producers[
                    min(producers, key=lambda key: producers[key]["received_at"])
                ]

    def apply(self, snapshot, residents, now):
        with self.lock:
            state = presence.read(self.path)
        instant = now.timestamp()
        health = []
        observations = {}
        for entry in state.get("producers", {}).values():
            report = entry["health"]
            expires = (
                min(report["observed_at"], entry["received_at"])
                + presence.HEALTH_SECONDS
            )
            status = (
                "unknown"
                if instant >= expires
                else "overloaded"
                if report["queue_depth"] >= 1024
                or report["overflow"]
                or report["presence_overflow"]
                else "delayed"
                if report["error"]
                or (
                    report["oldest_at"] is not None
                    and instant - report["oldest_at"] >= presence.DELAY_SECONDS
                )
                else "healthy"
            )
            health.append(
                dict(
                    report,
                    status=status,
                    expires_at=expires,
                    received_at=entry["received_at"],
                )
            )
            for observation in entry["presence"].values():
                agent = observation["agent_id"]
                prior = observations.get(agent)
                if prior and (prior["epoch"], prior["sequence"]) >= (
                    observation["epoch"],
                    observation["sequence"],
                ):
                    continue
                expires = observation["observed_at"] + presence.PRESENCE_SECONDS
                observations[agent] = dict(
                    observation,
                    producer=report["producer"],
                    expires_at=expires,
                    freshness="fresh" if instant < expires else "unknown",
                )
        snapshot["producer_health"] = sorted(
            health, key=lambda row: (row["producer"], row["target"])
        )
        villagers = {row["id"]: row for row in snapshot["villagers"]}
        for agent, observation in observations.items():
            row = villagers.get(agent)
            if observation["state"] == "ended":
                villagers.pop(agent, None)
                continue
            if row is None:
                # This projection input is derived only from an observed runtime callback.
                # It is never appended as history or used to trigger semantic effects.
                event = dict(
                    v=0,
                    agent_id=agent,
                    source=observation["source"],
                    project=observation["project"],
                    ts=dt.datetime.fromtimestamp(
                        float(observation["observed_at"]), dt.UTC
                    )
                    .isoformat(timespec="milliseconds")
                    .replace("+00:00", "Z"),
                    type="heartbeat",
                    payload={},
                )
                projected = project_village([event], residents, now)["villagers"]
                if not projected:
                    continue
                row = projected[0]
                row["history"] = []
                villagers[agent] = row
            row["presence"] = observation
            row["state"] = (
                observation["state"] if observation["freshness"] == "fresh" else "stale"
            )
            if row["pending_approval_ids"]:
                row["state"] = "knocking"
            if row["state"] not in {"working", "stale"}:
                row["place"] = None
        for row in villagers.values():
            if row["id"] not in observations and any(
                event.get("telemetry_managed") for event in row["history"]
            ):
                row["state"] = "stale"
        snapshot["villagers"] = list(villagers.values())[
            -snapshot["capacity"]["villagers"] :
        ]
