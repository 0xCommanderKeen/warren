"""HTTP ingestion of ordered, retryable delivery batches."""

import datetime as dt
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from hooks import presence

from fastapi.testclient import TestClient

import serve
from config import Config


class DeliveryIngestTests(unittest.TestCase):
    def test_batch_retry_has_no_duplicate_semantic_effects(self):
        with tempfile.TemporaryDirectory() as root:
            config = Config(
                events=Path(root) / "events.jsonl",
                villagers_dir=Path(root) / "villagers",
                token="secret",
            )
            event = dict(
                v=0,
                ts=dt.datetime.now(dt.UTC)
                .isoformat(timespec="milliseconds")
                .replace("+00:00", "Z"),
                source="codex",
                agent_id="codex:test",
                project="test",
                type="idle",
                payload={},
            )
            batch = {"records": [{"delivery_id": "synthetic-record-1", "event": event}]}
            with TestClient(serve.create_app(config)) as client:
                self.assertEqual(
                    401, client.post("/events/batch", json=batch).status_code
                )
                for _ in range(2):
                    self.assertEqual(
                        204,
                        client.post(
                            "/events/batch",
                            json=batch,
                            headers={"Authorization": "Bearer secret"},
                        ).status_code,
                    )
                self.assertEqual(1, len(config.events.read_text().splitlines()))
                self.assertEqual(
                    "resting",
                    client.get("/state").json()["snapshot"]["villagers"][0]["state"],
                )

    def test_presence_survives_old_history_and_expires_without_producer_report(self):
        with tempfile.TemporaryDirectory() as root:
            config = Config(
                events=Path(root) / "events.jsonl",
                villagers_dir=Path(root) / "villagers",
            )
            now = dt.datetime.now(dt.UTC)
            observed = now.timestamp()
            observation = dict(
                agent_id="codex:test",
                session_id="session",
                epoch=1,
                sequence=2,
                observed_at=observed,
                source="codex",
                project="test",
                state="resting",
            )
            health = dict(
                producer="producer1",
                target="target1",
                observed_at=observed,
                queue_depth=20,
                oldest_at=observed - 120,
                last_success=observed,
                error=None,
                retry_at=0,
                failures=0,
                worker="running",
                overflow=0,
                presence_overflow=0,
            )
            with TestClient(serve.create_app(config)) as client:
                self.assertEqual(
                    204,
                    client.post(
                        "/telemetry", json=dict(health=health, presence=[observation])
                    ).status_code,
                )
                snapshot = client.get("/state").json()["snapshot"]
                self.assertEqual("delayed", snapshot["producer_health"][0]["status"])
                self.assertEqual("resting", snapshot["villagers"][0]["state"])
                old = dict(
                    v=0,
                    ts=(now - dt.timedelta(seconds=10))
                    .isoformat(timespec="milliseconds")
                    .replace("+00:00", "Z"),
                    source="codex",
                    agent_id="codex:test",
                    project="test",
                    type="heartbeat",
                    payload={},
                )
                self.assertEqual(204, client.post("/events", json=old).status_code)
                self.assertEqual(
                    "resting",
                    client.get("/state").json()["snapshot"]["villagers"][0]["state"],
                )
                coordinator = client.app.state.runtime.state_coordinator
                aged = coordinator.evaluate(now + dt.timedelta(seconds=61))
                self.assertEqual("unknown", aged["producer_health"][0]["status"])
                self.assertEqual(
                    "unknown", aged["villagers"][0]["presence"]["freshness"]
                )
                self.assertEqual("stale", aged["villagers"][0]["state"])

    def test_ended_session_cannot_be_revived_by_reordering_or_server_restart(self):
        with tempfile.TemporaryDirectory() as root:
            config = Config(
                events=Path(root) / "events.jsonl",
                villagers_dir=Path(root) / "villagers",
            )
            now = dt.datetime.now(dt.UTC).timestamp()
            health = dict(
                producer="producer1",
                target="target1",
                observed_at=now,
                queue_depth=0,
                oldest_at=None,
                last_success=now,
                error=None,
                retry_at=0,
                failures=0,
                worker="running",
                overflow=0,
                presence_overflow=0,
            )
            observation = dict(
                agent_id="codex:test",
                session_id="session",
                epoch=1,
                sequence=2,
                observed_at=now,
                source="codex",
                project="test",
                state="ended",
            )
            with TestClient(serve.create_app(config)) as client:
                self.assertEqual(
                    204,
                    client.post(
                        "/telemetry", json=dict(health=health, presence=[observation])
                    ).status_code,
                )
            with TestClient(serve.create_app(config)) as client, patch.object(presence, "MAX_PRESENCE", 1):
                other = dict(observation, agent_id="codex:other", session_id="other", epoch=3, sequence=3, state="working")
                self.assertEqual(204, client.post("/telemetry", json=dict(health=health, presence=[other])).status_code)
                old_history = dict(v=0, ts=dt.datetime.fromtimestamp(now, dt.UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
                    source="codex", agent_id="codex:test", project="test", type="heartbeat", payload={}, telemetry_managed=True)
                self.assertEqual(204, client.post("/events", json=old_history).status_code)
                self.assertNotIn("codex:test", [row["id"] for row in client.get("/state").json()["snapshot"]["villagers"]])
                for sequence in (1, 3):
                    replay = dict(observation, sequence=sequence, state="working")
                    self.assertEqual(
                        204,
                        client.post(
                            "/telemetry", json=dict(health=health, presence=[replay])
                        ).status_code,
                    )
                    self.assertNotIn("codex:test", [row["id"] for row in client.get("/state").json()["snapshot"]["villagers"]])
                new_session = dict(
                    observation,
                    session_id="new-session",
                    epoch=4,
                    sequence=4,
                    state="working",
                )
                self.assertEqual(
                    204,
                    client.post(
                        "/telemetry", json=dict(health=health, presence=[new_session])
                    ).status_code,
                )
                self.assertEqual(
                    "working",
                    client.get("/state").json()["snapshot"]["villagers"][0]["state"],
                )


if __name__ == "__main__":
    unittest.main()
