import concurrent.futures
import datetime as dt
import http.client
import json
import os
import tempfile
import time
import unittest
from unittest import mock

import serve
from tests.http_test_support import RunningServer
from tests.test_village_state import event


class StateHTTPTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.events = os.path.join(self.tmp.name, "events.jsonl")
        self.villagers = os.path.join(self.tmp.name, "villagers")
        os.mkdir(self.villagers)
        self.patchers = [
            mock.patch.object(serve, "EVENTS", self.events),
            mock.patch.object(serve, "VILLAGERS_DIR", self.villagers),
            mock.patch.object(serve, "MAX_LOG_BYTES", 0),
        ]
        for patcher in self.patchers:
            patcher.start()
            self.addCleanup(patcher.stop)
        initial_event = event("tool_called", tool="Read")
        initial_event["ts"] = (
            dt.datetime.now(dt.UTC)
            .isoformat(timespec="milliseconds")
            .replace("+00:00", "Z")
        )
        with open(self.events, "w", encoding="utf-8") as stream:
            stream.write(json.dumps(initial_event) + "\n")
        self.running = RunningServer(serve)
        self.addCleanup(self.running.stop)

    def request(self, path):
        connection = http.client.HTTPConnection(
            *self.running.server.server_address, timeout=2
        )
        connection.request("GET", path)
        response = connection.getresponse()
        body = response.read()
        connection.close()
        return response, body

    def test_snapshot_and_conditional_no_change(self):
        response, body = self.request("/state")
        self.assertEqual(200, response.status)
        envelope = json.loads(body)
        self.assertEqual("snapshot", envelope["kind"])
        snapshot = envelope["snapshot"]
        self.assertEqual("agent-1", snapshot["villagers"][0]["id"])
        response, body = self.request(
            f"/state?generation={snapshot['generation']}&cursor={snapshot['cursor']}"
        )
        self.assertEqual((204, b""), (response.status, body))

    def test_stale_cursor_gets_atomic_reset(self):
        response, body = self.request("/state?generation=999&cursor=old")
        envelope = json.loads(body)
        self.assertEqual("reset", envelope["kind"])
        self.assertIn("villagers", envelope["snapshot"])

    def test_stream_sends_complete_snapshot_with_event_id_and_unbuffered_headers(self):
        connection = http.client.HTTPConnection(
            *self.running.server.server_address, timeout=2
        )
        connection.request("GET", "/state/stream?generation=999&cursor=old")
        response = connection.getresponse()
        self.assertEqual(response.status, 200)
        self.assertEqual(response.headers["X-Accel-Buffering"], "no")
        lines = [response.readline().decode() for _ in range(4)]
        self.assertTrue(lines[0].startswith("id: "))
        self.assertEqual(lines[1].strip(), "event: snapshot")
        envelope = json.loads(lines[2].removeprefix("data: ").strip())
        self.assertEqual(envelope["kind"], "reset")
        self.assertIn("villagers", envelope["snapshot"])
        connection.close()

    def test_stalled_stream_holds_no_ingest_rotation_or_coordinator_lock(self):
        stalled = http.client.HTTPConnection(
            *self.running.server.server_address, timeout=2
        )
        stalled.request("GET", "/state/stream")
        response = stalled.getresponse()
        self.assertEqual(response.status, 200)

        body = json.dumps(event("idle")).encode()

        def ingest():
            connection = http.client.HTTPConnection(
                *self.running.server.server_address, timeout=1
            )
            connection.request(
                "POST",
                "/events",
                body,
                {
                    "Content-Type": "application/json",
                    "Content-Length": str(len(body)),
                },
            )
            result = connection.getresponse()
            result.read()
            connection.close()
            return result.status

        def rotate():
            with serve.LOG_LOCK:
                return serve.rotate(os.path.getsize(self.events))

        with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
            posted = pool.submit(ingest)
            projected = pool.submit(self.request, "/state")
            rotated = pool.submit(rotate)
            self.assertEqual(posted.result(timeout=1), 204)
            self.assertEqual(projected.result(timeout=1)[0].status, 200)
            rotated.result(timeout=1)

        started = time.monotonic()
        stalled.close()
        self.running.stop()
        self.assertLess(time.monotonic() - started, 1.5)


if __name__ == "__main__":
    unittest.main()
