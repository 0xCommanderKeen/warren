import http.client
import json
import os
import tempfile
import threading
import unittest
import socket

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

    @staticmethod
    def valid_event(**changes):
        event = {
            "v": 0, "ts": "2026-08-24T12:00:00.000Z", "source": "test",
            "agent_id": "test:one", "project": "burrow", "cwd": "/tmp",
            "type": "idle", "payload": {},
        }
        event.update(changes)
        return event

    def post_event(self, event):
        body = json.dumps(event).encode()
        conn = http.client.HTTPConnection(*self.server.server_address)
        conn.request("POST", "/events", body, {"Content-Type": "application/json"})
        response = conn.getresponse()
        status, data = response.status, response.read()
        conn.close()
        return status, data

    def test_ingest_rejects_the_shared_protocol_contract_without_appending(self):
        fixtures = os.path.join(os.path.dirname(__file__), "fixtures",
                                "protocol-v0-validation.json")
        with open(fixtures, encoding="utf-8") as stream:
            cases = json.load(stream)
        for case in cases:
            with self.subTest(case["name"]):
                before = os.path.getsize(self.events) if os.path.exists(self.events) else 0
                status, _ = self.post_event(case["event"])
                self.assertEqual(status, 204 if case["valid"] else 400)
                after = os.path.getsize(self.events) if os.path.exists(self.events) else 0
                self.assertEqual(after > before, case["valid"])

    def test_ingest_rejects_non_standard_json_constants_without_appending(self):
        template = json.dumps(
            self.valid_event(payload={"unknown": "constant"}),
            separators=(",", ":"),
        ).encode()
        for constant in (b"NaN", b"Infinity", b"-Infinity"):
            with self.subTest(constant=constant):
                body = template.replace(b'"constant"', constant)
                raw = (b"POST /events HTTP/1.1\r\nHost: x\r\n"
                       b"Content-Type: application/json\r\n"
                       b"Content-Length: " + str(len(body)).encode() + b"\r\n\r\n"
                       + body +
                       b"GET /events HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n")
                with socket.create_connection(self.server.server_address,
                                              timeout=2) as conn:
                    conn.sendall(raw)
                    response = b""
                    while True:
                        chunk = conn.recv(4096)
                        if not chunk:
                            break
                        response += chunk

                self.assertEqual(response.count(b"HTTP/1.1"), 2)
                self.assertIn(b"HTTP/1.1 400", response)
                self.assertIn(b"not a protocol event", response)
                self.assertFalse(os.path.exists(self.events))

    def test_malformed_length_returns_stable_error_and_closes_before_pipeline(self):
        raw = (b"POST /events HTTP/1.1\r\nHost: x\r\nContent-Length: nope\r\n\r\n"
               b"GET /events HTTP/1.1\r\nHost: x\r\n\r\n")
        with socket.create_connection(self.server.server_address, timeout=2) as conn:
            conn.sendall(raw)
            chunks = []
            while True:
                chunk = conn.recv(4096)
                if not chunk:
                    break
                chunks.append(chunk)
            response = b"".join(chunks)
            self.assertIn(b"HTTP/1.1 400", response)
            self.assertIn(b"invalid content length", response)
            self.assertEqual(response.count(b"HTTP/1.1"), 1)

        # The server stays healthy and a fresh keep-alive connection is aligned.
        self.assertEqual(self.post_event(self.valid_event())[0], 204)

    def test_transfer_encoding_is_rejected_and_closes_before_pipeline(self):
        body = json.dumps(self.valid_event()).encode()
        raw = (b"POST /events HTTP/1.1\r\nHost: x\r\n"
               b"Content-Length: " + str(len(body)).encode() + b"\r\n"
               b"Transfer-Encoding: chunked\r\n\r\n" + body +
               b"GET /events HTTP/1.1\r\nHost: x\r\n\r\n")
        with socket.create_connection(self.server.server_address, timeout=2) as conn:
            conn.sendall(raw)
            response = b""
            while True:
                chunk = conn.recv(4096)
                if not chunk:
                    break
                response += chunk
        self.assertIn(b"HTTP/1.1 400", response)
        self.assertIn(b"unsupported transfer encoding", response)
        self.assertEqual(response.count(b"HTTP/1.1"), 1)
        self.assertFalse(os.path.exists(self.events))

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
