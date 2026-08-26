import http.client
import json
import os
import tempfile
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
        self.patchers = [mock.patch.object(serve, "EVENTS", self.events),
                         mock.patch.object(serve, "VILLAGERS_DIR", self.villagers),
                         mock.patch.object(serve, "MAX_LOG_BYTES", 0)]
        for patcher in self.patchers:
            patcher.start(); self.addCleanup(patcher.stop)
        with open(self.events, "w", encoding="utf-8") as stream:
            stream.write(json.dumps(event("tool_called", tool="Read")) + "\n")
        self.running = RunningServer(serve)
        self.addCleanup(self.running.stop)

    def request(self, path):
        connection = http.client.HTTPConnection(*self.running.server.server_address, timeout=2)
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
            f'/state?generation={snapshot["generation"]}&cursor={snapshot["cursor"]}')
        self.assertEqual((204, b""), (response.status, body))

    def test_stale_cursor_gets_atomic_reset(self):
        response, body = self.request("/state?generation=999&cursor=old")
        envelope = json.loads(body)
        self.assertEqual("reset", envelope["kind"])
        self.assertIn("villagers", envelope["snapshot"])


if __name__ == "__main__":
    unittest.main()
