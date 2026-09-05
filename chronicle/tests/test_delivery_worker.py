"""Delivery service boundary: durable input, observable transport, controlled time."""

import os
import tempfile
import unittest
from unittest.mock import patch

from hooks import delivery_worker as delivery
from hooks import emit


class DeliveryWorkerTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        root = directory.name
        settings = patch.multiple(
            emit,
            LOG_DIR=root,
            LOG=root + "/events.jsonl",
            OUTBOX=root + "/primary-outbox.jsonl",
            DIAGNOSTICS=root + "/diagnostics.json",
        )
        settings.start()
        self.addCleanup(settings.stop)
        environment = patch.dict(
            os.environ, CHRONICLE_URL="http://chronicle", CHRONICLE_MIRROR=""
        )
        environment.start()
        self.addCleanup(environment.stop)
        self.now = 1800000000.0
        self.accepted = {}
        self.online = True

    def event(self, index):
        return dict(
            v=0,
            ts="2027-01-15T08:00:00.000Z",
            source="codex",
            agent_id="codex:session",
            project="synthetic",
            cwd="",
            type="heartbeat",
            payload={"index": index},
        )

    def transport(self, url, path, body, token):
        self.now += 0.450
        if not self.online:
            return "connect"
        if path == "/events/batch":
            for record in body["records"]:
                self.accepted[record["delivery_id"]] = record["event"]
        return None

    def worker(self):
        return delivery.DeliveryWorker(
            clock=lambda: self.now, transport=self.transport, jitter=lambda: 0.5
        )

    def test_450ms_backlog_drains_after_five_hooks_stop(self):
        worker = self.worker()
        for index in range(20):
            delivery.enqueue(self.event(index), session_id="session")
        for index in range(20, 25):
            delivery.enqueue(self.event(index), session_id="session")
        for _ in range(10):
            worker.tick()
            if not emit._read_durable_outbox_snapshot():
                break
        self.assertEqual(25, len(self.accepted))
        self.assertEqual([], emit._read_durable_outbox_snapshot())

    def test_1024_outage_recovers_without_any_more_hooks(self):
        worker = self.worker()
        for index in range(1024):
            delivery.enqueue(self.event(index), session_id="session")
        self.online = False
        worker.tick()
        self.assertEqual(1024, len(emit._read_durable_outbox_snapshot()))
        self.online = True
        for _ in range(100):
            self.now += 5
            worker.tick()
            if not emit._read_durable_outbox_snapshot():
                break
        self.assertEqual(1024, len(self.accepted))
        self.assertEqual([], emit._read_durable_outbox_snapshot())

    def test_presence_precedes_backlog_and_worker_never_refreshes_observation(self):
        messages = []

        def post(url, path, body, token):
            messages.append((path, body))
            return self.transport(url, path, body, token)

        worker = delivery.DeliveryWorker(clock=lambda: self.now, transport=post)
        delivery.enqueue(self.event(1), session_id="session")
        worker.tick()
        self.assertEqual("/telemetry", messages[0][0])
        first = messages[0][1]["presence"][0]
        self.now += 120
        worker.tick()
        reports = [body for path, body in messages if path == "/telemetry"]
        self.assertEqual(
            first["observed_at"], reports[-1]["presence"][0]["observed_at"]
        )
        self.assertEqual(first["sequence"], reports[-1]["presence"][0]["sequence"])

    def test_sustained_five_events_per_second_returns_to_baseline(self):
        worker = self.worker()
        for index in range(200):
            delivery.enqueue(self.event(index), session_id="session")
        started = self.now
        arrivals = 0
        for _ in range(30):
            due = int((self.now - started) * 5)
            while arrivals < due:
                delivery.enqueue(self.event(200 + arrivals), session_id="session")
                arrivals += 1
            worker.tick()
            self.now += .25
        self.assertEqual(200 + arrivals, len(self.accepted))
        self.assertEqual([], emit._read_durable_outbox_snapshot())

    def test_ambiguous_acceptance_and_restart_reuse_delivery_ids(self):
        worker = self.worker()
        delivery.enqueue(self.event(1), session_id="session")

        def ambiguous(url, path, body, token):
            self.transport(url, path, body, token)
            return "timeout" if path == "/events/batch" else None

        worker.transport = ambiguous
        worker.tick()
        self.assertEqual(1, len(self.accepted))
        self.assertEqual(1, len(emit._read_durable_outbox_snapshot()))
        self.now += 40
        self.worker().tick()
        self.assertEqual(1, len(self.accepted))
        self.assertEqual([], emit._read_durable_outbox_snapshot())

    def test_duplicate_service_cannot_deliver_while_owner_is_running(self):
        import fcntl
        import threading

        with open(os.path.join(emit.LOG_DIR, "delivery-owner.lock"), "a") as owner:
            fcntl.flock(owner, fcntl.LOCK_EX | fcntl.LOCK_NB)
            delivery.enqueue(self.event(1), session_id="session")
            self.worker().run(threading.Event())
        self.assertEqual({}, self.accepted)

    def test_outbox_capacity_loss_is_reported_explicitly(self):
        reports = []

        def post(url, path, body, token):
            if path == "/telemetry":
                reports.append(body["health"])
            return self.transport(url, path, body, token)

        with patch.object(emit, "OUTBOX_RECORDS", 2):
            for index in range(3):
                delivery.enqueue(self.event(index), session_id="session")
        delivery.DeliveryWorker(clock=lambda: self.now, transport=post).tick()
        self.assertEqual(1, reports[-1]["overflow"])
        self.assertEqual(2, len(self.accepted))

    def test_presence_disk_failure_does_not_lose_semantic_history(self):
        with patch.object(delivery.presence, "observe", side_effect=OSError("disk")):
            delivery.enqueue(self.event(1), session_id="session")
        self.worker().tick()
        self.assertEqual(1, len(self.accepted))

    def test_concurrent_codex_and_claude_share_the_spool_without_losing_events(self):
        from concurrent.futures import ThreadPoolExecutor
        def hook(index):
            event = self.event(index)
            event["source"] = "codex" if index % 2 else "claude-code"
            event["agent_id"] = event["source"] + ":session"
            delivery.enqueue(event, session_id="session")
        with ThreadPoolExecutor(max_workers=4) as pool:
            list(pool.map(hook, range(20)))
        self.worker().tick()
        self.assertEqual(20, len(self.accepted))

    def test_transport_diagnostics_never_contain_exception_text_or_credentials(self):
        import socket
        import urllib.error
        cases = [
            (urllib.error.URLError(socket.gaierror("private hostname")), "dns"),
            (urllib.error.URLError(ConnectionRefusedError("secret URL")), "connect"),
            (TimeoutError("secret body"), "timeout"),
            (urllib.error.HTTPError("secret URL", 401, "secret body", {}, None), "authentication"),
            (urllib.error.HTTPError("secret URL", 400, "secret body", {}, None), "invalid_event"),
        ]
        for error, expected in cases:
            with self.subTest(expected=expected), patch.object(delivery.urllib.request, "urlopen", side_effect=error):
                self.assertEqual(expected, delivery.post_json("http://synthetic", "/events/batch", {}, "secret token"))

    def test_codex_stop_is_observed_as_resting_not_a_working_heartbeat(self):
        kind, payload = emit.codex_events({"hook_event_name": "Stop"})[0]
        delivery.enqueue(dict(self.event(1), type=kind, payload=payload), session_id="session")
        report = delivery.presence.report(os.path.join(emit.LOG_DIR, "latest-presence.json"))
        self.assertEqual("resting", next(iter(report["presence"].values()))["state"])

    def test_evicted_terminal_presence_keeps_its_session_fence(self):
        with patch.object(delivery.presence, "MAX_PRESENCE", 1):
            delivery.enqueue(dict(self.event(1), type="session_ended"), session_id="ended")
            delivery.enqueue(dict(self.event(2), agent_id="codex:other"), session_id="other")
            delivery.enqueue(self.event(3), session_id="ended")
        report = delivery.presence.report(os.path.join(emit.LOG_DIR, "latest-presence.json"))
        self.assertFalse(any(row["agent_id"] == "codex:session" and row["state"] == "working" for row in report["presence"].values()))

    def test_full_session_fences_reject_new_presence_without_losing_history(self):
        with patch.object(delivery.presence, "MAX_SESSIONS", 1):
            delivery.enqueue(dict(self.event(1), type="session_ended"), session_id="ended")
            delivery.enqueue(dict(self.event(2), agent_id="codex:other"), session_id="other")
        report = delivery.presence.report(os.path.join(emit.LOG_DIR, "latest-presence.json"))
        self.assertEqual(1, report["presence_overflow"])
        self.assertEqual(["ended"], [row["state"] for row in report["presence"].values()])
        self.worker().tick()
        self.assertEqual(2, len(self.accepted))


if __name__ == "__main__":
    unittest.main()
