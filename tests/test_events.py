import http.client
import json
import os
import tempfile
import threading
import unittest

import serve


class EventsEndpointTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.events = os.path.join(self.tmp.name, "events.jsonl")
        self.previous_events = serve.EVENTS
        serve.EVENTS = self.events
        self.server = serve.http.server.ThreadingHTTPServer(("127.0.0.1", 0), serve.Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        serve.EVENTS = self.previous_events
        self.tmp.cleanup()

    def get_events(self, since=0):
        conn = http.client.HTTPConnection(*self.server.server_address)
        conn.request("GET", f"/events?since={since}")
        response = conn.getresponse()
        body = response.read()
        headers = dict(response.getheaders())
        conn.close()
        return response.status, headers, body

    def append(self, *events):
        with open(self.events, "ab") as stream:
            for event in events:
                stream.write(json.dumps(event).encode() + b"\n")

    def test_incremental_fetch_and_empty_steady_state(self):
        first = {"type": "idle", "agent_id": "one"}
        second = {"type": "tool_called", "agent_id": "two"}
        self.append(first)

        status, headers, body = self.get_events()
        cursor = headers["X-Burrow-Cursor"]
        self.assertEqual(status, 200)
        self.assertEqual([first], [json.loads(line) for line in body.splitlines()])

        _, headers, body = self.get_events(cursor)
        self.assertEqual(body, b"")
        self.assertEqual(headers["X-Burrow-Cursor"], cursor)

        self.append(second)
        _, headers, body = self.get_events(cursor)
        self.assertEqual([second], [json.loads(line) for line in body.splitlines()])
        self.assertNotEqual(headers["X-Burrow-Cursor"], cursor)

    def test_cursor_beyond_rotated_log_resets(self):
        self.append({"type": "idle", "agent_id": "old"})
        _, headers, _ = self.get_events()
        old_cursor = headers["X-Burrow-Cursor"]

        with open(self.events, "wb") as stream:
            stream.write(b'{"type":"idle","agent_id":"new"}\n')

        _, headers, body = self.get_events(old_cursor)
        self.assertEqual(headers["X-Burrow-Reset"], "1")
        self.assertEqual(json.loads(body), {"type": "idle", "agent_id": "new"})

    def test_replaced_log_that_regrows_past_cursor_resets(self):
        self.append({"type": "idle", "agent_id": "old"})
        _, headers, _ = self.get_events()
        old_cursor = headers["X-Burrow-Cursor"]

        replacement = {"type": "idle", "agent_id": "replacement-with-longer-content"}
        replacement_path = self.events + ".new"
        with open(replacement_path, "wb") as stream:
            stream.write(json.dumps(replacement).encode() + b"\n")
        os.replace(replacement_path, self.events)

        _, headers, body = self.get_events(old_cursor)
        self.assertEqual(headers["X-Burrow-Reset"], "1")
        self.assertEqual([replacement], [json.loads(line) for line in body.splitlines()])

    def test_in_place_rotation_resets_a_cursor_still_within_the_new_log(self):
        ts = "2099-01-01T00:00:00.000Z"
        self.append({"type": "idle", "agent_id": "one", "ts": ts})
        _, headers, _ = self.get_events()
        old_cursor = headers["X-Burrow-Cursor"]

        for index in range(100):
            self.append({"type": "tool_called", "agent_id": "one", "ts": ts,
                         "payload": {"tool": "Read", "detail": str(index)}})
        with serve.LOG_LOCK:
            archive = serve.rotate(os.path.getsize(self.events))
        self.assertIsNotNone(archive)
        self.assertGreater(os.path.getsize(self.events), int(old_cursor.rsplit(":", 1)[-1]))

        _, headers, body = self.get_events(old_cursor)
        self.assertEqual(headers.get("X-Burrow-Reset"), "1")
        self.assertTrue(body.startswith(b'{"type"'))

    def test_incomplete_line_is_not_returned_or_consumed(self):
        with open(self.events, "wb") as stream:
            stream.write(b'{"type":"idle"')
        _, headers, body = self.get_events()
        self.assertEqual(body, b"")
        self.assertEqual(headers["X-Burrow-Cursor"], "0")

    def test_invalid_cursor_is_rejected(self):
        status, _, _ = self.get_events(-1)
        self.assertEqual(status, 400)

    def test_sse_pushes_new_events_and_resumes_from_last_event_id(self):
        first = {"type": "idle", "agent_id": "one"}
        second = {"type": "tool_called", "agent_id": "one"}
        self.append(first)

        conn = http.client.HTTPConnection(*self.server.server_address, timeout=2)
        conn.request("GET", "/events/stream")
        response = conn.getresponse()
        self.assertEqual(response.status, 200)
        self.assertEqual(response.headers["Content-Type"],
                         "text/event-stream; charset=utf-8")
        self.assertEqual(response.headers["X-Accel-Buffering"], "no")
        event_id = response.readline().decode().removeprefix("id: ").strip()
        self.assertEqual(json.loads(response.readline().decode().removeprefix("data: ")),
                         first)
        self.assertEqual(response.readline(), b"\n")
        conn.close()

        self.append(second)
        conn = http.client.HTTPConnection(*self.server.server_address, timeout=2)
        conn.request("GET", "/events/stream", headers={"Last-Event-ID": event_id})
        response = conn.getresponse()
        resumed_id = response.readline().decode().removeprefix("id: ").strip()
        self.assertNotEqual(resumed_id, event_id)
        self.assertEqual(json.loads(response.readline().decode().removeprefix("data: ")),
                         second)
        conn.close()


if __name__ == "__main__":
    unittest.main()
