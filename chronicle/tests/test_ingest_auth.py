"""Authentication remains scoped to event ingestion at the ASGI seam."""

import os
import tempfile
import unittest
from unittest import mock

from fastapi.testclient import TestClient

import serve


EVENT = {
    "v": 0,
    "ts": "2026-08-24T14:03:22.114Z",
    "source": "claude-code",
    "agent_id": "claude-code:test",
    "project": "burrow",
    "cwd": "/tmp",
    "type": "idle",
    "payload": {},
}


class IngestAuthenticationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.events = os.path.join(self.tmp.name, "events.jsonl")
        self.events_patch = mock.patch.object(serve, "EVENTS", self.events)
        self.events_patch.start()
        self.addCleanup(self.events_patch.stop)

    def test_configured_token_accepts_bearer_or_legacy_header_only(self):
        with (
            mock.patch.object(serve, "TOKEN", "s3cret"),
            TestClient(serve.app) as client,
        ):
            for headers in (
                {},
                {"Authorization": "Bearer nope"},
                {"X-Burrow-Token": "nope"},
            ):
                self.assertEqual(
                    client.post("/events", json=EVENT, headers=headers).status_code, 401
                )
            self.assertEqual(
                client.post(
                    "/events", json=EVENT, headers={"Authorization": "Bearer s3cret"}
                ).status_code,
                204,
            )
            self.assertEqual(
                client.post(
                    "/events", json=EVENT, headers={"X-Burrow-Token": "s3cret"}
                ).status_code,
                204,
            )
            self.assertEqual(client.get("/events").status_code, 200)
            self.assertEqual(client.get("/villagers").status_code, 200)

    def test_unconfigured_token_leaves_ingest_open(self):
        with mock.patch.object(serve, "TOKEN", ""), TestClient(serve.app) as client:
            self.assertEqual(client.post("/events", json=EVENT).status_code, 204)
            self.assertEqual(
                client.post(
                    "/events", json=EVENT, headers={"Authorization": "Bearer anything"}
                ).status_code,
                204,
            )


if __name__ == "__main__":
    unittest.main()
