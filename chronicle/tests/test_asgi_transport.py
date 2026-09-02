import http.server
import dataclasses
import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from fastapi.testclient import TestClient

import serve
from config import Config
from tests.http_test_support import wait_until


def settled_notification_status(client, expected):
    """Read ``/transport/status`` once its notification counters show ``expected``.

    The webhook has answered before the worker commits the outcome, so a status
    read taken straight after the webhook's receipt can run ahead of the
    counters (warren#315). The last reading is returned either way, so a real
    mismatch still fails on the named counter.
    """
    status = None

    def settled():
        nonlocal status
        status = client.get("/transport/status").json()["notifications"]
        return all(status[key] == value for key, value in expected.items())

    wait_until(settled)
    return status


class ASGITransportContractTests(unittest.TestCase):
    def test_workers_render_identity_from_their_own_runtime_resident_directory(self):
        received = [[], []]

        def handler_for(index):
            class RecordingWebhook(http.server.BaseHTTPRequestHandler):
                def do_POST(self):
                    body = self.rfile.read(int(self.headers["Content-Length"]))
                    received[index].append((self.headers["Title"], body.decode()))
                    self.send_response(200)
                    self.end_headers()

                def log_message(self, *_args):
                    pass

            return RecordingWebhook

        webhooks = [
            http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler_for(index))
            for index in range(2)
        ]
        threads = [
            threading.Thread(target=webhook.serve_forever, daemon=True)
            for webhook in webhooks
        ]
        for thread in threads:
            thread.start()

        event = {
            "v": 0,
            "ts": "2026-08-24T12:00:00.000Z",
            "source": "test",
            "agent_id": "test:resident-owner",
            "project": "chronicle",
            "cwd": "",
            "type": "needs_human",
            "payload": {"message": "runtime identity"},
        }
        try:
            with tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                configs = []
                for index, (name, webhook) in enumerate(
                    zip(("Aster", "Birch"), webhooks, strict=True)
                ):
                    villagers = root / f"villagers-{index}"
                    villagers.mkdir()
                    (villagers / "resident.md").write_text(
                        f"---\nagent_id: test:resident-owner\nname: {name}\n---\n",
                        encoding="utf-8",
                    )
                    configs.append(
                        dataclasses.replace(
                            Config(),
                            events=root / f"events-{index}.jsonl",
                            villagers_dir=villagers,
                            notify_url=f"http://127.0.0.1:{webhook.server_port}/",
                            notify_workers=1,
                        )
                    )

                with (
                    TestClient(serve.create_app(configs[0])) as first,
                    TestClient(serve.create_app(configs[1])) as second,
                ):
                    self.assertEqual(first.post("/events", json=event).status_code, 204)
                    self.assertEqual(
                        second.post("/events", json=event).status_code, 204
                    )
                    wait_until(lambda: all(received))

                self.assertEqual(
                    received[0],
                    [
                        (
                            "Aster is at your door (chronicle)",
                            "Aster · chronicle\nruntime identity",
                        )
                    ],
                )
                self.assertEqual(
                    received[1],
                    [
                        (
                            "Birch is at your door (chronicle)",
                            "Birch · chronicle\nruntime identity",
                        )
                    ],
                )
        finally:
            for webhook in webhooks:
                webhook.shutdown()
                webhook.server_close()
            for thread in threads:
                thread.join(3)

    def test_configured_notification_queue_capacity_is_enforced_and_reported(self):
        entered = threading.Event()
        release = threading.Event()

        class BlockingWebhook(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                entered.set()
                release.wait(3)
                self.send_response(200)
                self.end_headers()

            def log_message(self, *_args):
                pass

        webhook = http.server.ThreadingHTTPServer(("127.0.0.1", 0), BlockingWebhook)
        webhook_thread = threading.Thread(target=webhook.serve_forever, daemon=True)
        webhook_thread.start()
        try:
            with tempfile.TemporaryDirectory() as temporary:
                config = dataclasses.replace(
                    Config(),
                    events=Path(temporary) / "events.jsonl",
                    notify_url=f"http://127.0.0.1:{webhook.server_port}/",
                    notify_workers=1,
                    notify_queue=1,
                )
                event = {
                    "v": 0,
                    "ts": "2026-08-24T12:00:00.000Z",
                    "source": "test",
                    "agent_id": "test:capacity-0",
                    "project": "chronicle",
                    "cwd": "",
                    "type": "needs_human",
                    "payload": {"message": "bounded"},
                }
                with TestClient(serve.create_app(config)) as client:
                    self.assertEqual(
                        client.post("/events", json=event).status_code, 204
                    )
                    self.assertTrue(entered.wait(3))
                    for index in (1, 2):
                        queued = dict(event, agent_id=f"test:capacity-{index}")
                        self.assertEqual(
                            client.post("/events", json=queued).status_code, 204
                        )
                    status = client.get("/transport/status").json()["notifications"]
                    self.assertEqual(status["queue_capacity"], 1)
                    self.assertEqual(status["queued"], 1)
                    self.assertEqual(status["saturated"], 1)
                    release.set()
        finally:
            release.set()
            webhook.shutdown()
            webhook.server_close()
            webhook_thread.join(3)

    def test_notification_transports_are_isolated_per_application_runtime(self):
        first_in_flight = threading.Event()
        release_first = threading.Event()

        def handler_for(index, received):
            class RecordingWebhook(http.server.BaseHTTPRequestHandler):
                def do_POST(self):
                    body = self.rfile.read(int(self.headers["Content-Length"]))
                    received.append(body)
                    if index == 0 and b"held collision" in body:
                        first_in_flight.set()
                        release_first.wait(3)
                    failed_attempt = index == 0 and b"retry isolation" in body
                    self.send_response(500 if failed_attempt else 200)
                    self.end_headers()

                def log_message(self, *_args):
                    pass

            return RecordingWebhook

        received = [[], []]
        webhooks = [
            http.server.ThreadingHTTPServer(
                ("127.0.0.1", 0), handler_for(index, received[index])
            )
            for index in range(2)
        ]
        threads = [
            threading.Thread(target=webhook.serve_forever, daemon=True)
            for webhook in webhooks
        ]
        for thread in threads:
            thread.start()

        collision_event = {
            "v": 0,
            "ts": "2026-08-24T12:00:00.000Z",
            "source": "test",
            "agent_id": "test:runtime-owner",
            "project": "chronicle",
            "cwd": "",
            "type": "needs_human",
            "payload": {"message": "held collision"},
        }
        retry_event = dict(
            collision_event,
            agent_id="test:runtime-retry-owner",
            payload={"message": "retry isolation"},
        )
        # One delivered, three failed attempts at the retry event (two retries,
        # then dropped) in the first runtime; every knock delivered in the second.
        first_expected = {"delivered": 1, "failed": 3, "retried": 2, "dropped": 1}
        second_expected = {"delivered": 3, "failed": 0}
        try:
            with tempfile.TemporaryDirectory() as temporary:
                configs = [
                    dataclasses.replace(
                        Config(),
                        events=Path(temporary) / f"events-{index}.jsonl",
                        notify_url=f"http://127.0.0.1:{webhook.server_port}/",
                        notify_workers=1,
                        notify_queue=index + 2,
                    )
                    for index, webhook in enumerate(webhooks)
                ]
                apps = [serve.create_app(config) for config in configs]
                with TestClient(apps[1]) as second:
                    with TestClient(apps[0]) as first:
                        self.assertEqual(
                            first.post("/events", json=collision_event).status_code, 204
                        )
                        self.assertTrue(first_in_flight.wait(3))

                        # The identical terminal key must not collide with the
                        # first runtime's process-local in-flight claim.
                        self.assertEqual(
                            second.post("/events", json=collision_event).status_code,
                            204,
                        )
                        wait_until(lambda: len(received[1]) >= 1)
                        self.assertEqual(len(received[1]), 1)
                        release_first.set()

                        # Exhaust one runtime's durable attempt ledger, then
                        # prove the same terminal identity still delivers in
                        # the other runtime's independent store.
                        self.assertEqual(
                            first.post("/events", json=retry_event).status_code, 204
                        )
                        wait_until(lambda: len(received[0]) >= 4)
                        self.assertEqual(len(received[0]), 4)
                        self.assertEqual(
                            second.post("/events", json=retry_event).status_code, 204
                        )
                        wait_until(lambda: len(received[1]) >= 2)
                        first_status = settled_notification_status(
                            first, first_expected
                        )

                    # Shutting down the first app must not stop the second app's
                    # independently owned worker.
                    shutdown_event = dict(
                        collision_event,
                        agent_id="test:second-runtime-after-shutdown",
                    )
                    self.assertEqual(
                        second.post("/events", json=shutdown_event).status_code, 204
                    )
                    wait_until(lambda: len(received[1]) >= 3)
                    second_status = settled_notification_status(
                        second, second_expected
                    )

                self.assertEqual(first_status["queue_capacity"], 2)
                self.assertEqual(second_status["queue_capacity"], 3)
                self.assertEqual(list(map(len, received)), [4, 3])
                self.assertEqual(
                    {key: first_status[key] for key in first_expected}, first_expected
                )
                self.assertEqual(
                    {key: second_status[key] for key in second_expected},
                    second_expected,
                )
        finally:
            release_first.set()
            for webhook in webhooks:
                webhook.shutdown()
                webhook.server_close()
            for thread in threads:
                thread.join(3)

    def test_no_browser_client_is_served_from_any_path(self):
        """Burrow is the log, the projection and the API; clients are separate repos.

        The old in-tree viewer answered `/`, `/village/…` and every unmatched path.
        Those paths must now fail honestly rather than return a stale page, so a
        client pinned to this origin breaks loudly instead of rendering old state.
        """
        removed_client_paths = [
            "/",
            "/index.html",
            "/village/",
            "/village/index.html",
            "/village/state-transport.js",
            "/observatory/",
            "/observatory/agents/keeper",
        ]

        with TestClient(serve.app) as client:
            responses = {path: client.get(path) for path in removed_client_paths}

        for path, response in responses.items():
            with self.subTest(path=path):
                self.assertEqual(response.status_code, 404, path)
                self.assertNotIn("<html", response.text.lower())
                self.assertNotIn("createStateTransport", response.text)

    def test_live_api_surface_survives_the_client_removal(self):
        with TestClient(serve.app) as client:
            self.assertEqual(client.get("/villagers").status_code, 200)
            self.assertEqual(client.get("/transport/status").status_code, 200)
            self.assertEqual(client.get("/retention-policy.json").status_code, 200)

    def test_fastapi_openapi_names_public_wire_contracts(self):
        schema = serve.app.openapi()
        self.assertEqual(schema["info"]["title"], "Chronicle Village API")
        self.assertIn("ProtocolEvent", schema["components"]["schemas"])
        self.assertIn("StateEnvelope", schema["components"]["schemas"])
        self.assertIn("TransportStatus", schema["components"]["schemas"])
        state_schema = schema["components"]["schemas"]["VillageState"]
        self.assertEqual(
            {
                "schema_version",
                "generation",
                "cursor",
                "log_generation",
                "evaluated_at",
                "villagers",
                "residents",
                "diagnostic_residents",
                "artifacts",
                "tasks",
                "approvals",
                "journals",
                "routines",
                "diagnostics",
                "capacity",
                "capabilities",
            },
            set(state_schema["properties"]),
        )
        for field, model in {
            "villagers": "VillagerWire",
            "residents": "ResidentWire",
            "diagnostic_residents": "ResidentDiagnosticWire",
            "artifacts": "ArtifactWire",
            "tasks": "TaskWire",
            "approvals": "ApprovalWire",
            "journals": "JournalWire",
            "routines": "RoutineWire",
            "diagnostics": "DiagnosticWire",
        }.items():
            self.assertEqual(
                {"$ref": f"#/components/schemas/{model}"},
                state_schema["properties"][field]["items"],
            )
        self.assertIn("/state/stream", schema["paths"])
        event_operation = schema["paths"]["/events"]["post"]
        self.assertEqual(
            event_operation["requestBody"]["content"]["application/json"]["schema"],
            {"$ref": "#/components/schemas/ProtocolEvent"},
        )
        self.assertEqual(
            event_operation["responses"]["400"]["content"]["text/plain"]["schema"],
            {"$ref": "#/components/schemas/ErrorResponse"},
        )
        self.assertIn(
            "generation",
            {item["name"] for item in schema["paths"]["/state"]["get"]["parameters"]},
        )

    def test_ingest_rejects_wire_model_shape_and_runs_storage_off_event_loop(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = os.path.join(temporary, "events.jsonl")
            event = {
                "v": 0,
                "ts": "2026-08-24T12:00:00.000Z",
                "source": "test",
                "agent_id": "test:one",
                "project": "burrow",
                "cwd": "",
                "type": "idle",
                "payload": {},
            }
            request_thread = threading.get_ident()
            append_threads = []
            original = serve.append_event

            def observed_append(item):
                append_threads.append(threading.get_ident())
                return original(item)

            with (
                mock.patch.object(serve, "EVENTS", path),
                mock.patch.object(serve, "append_event", side_effect=observed_append),
                TestClient(serve.app) as client,
            ):
                self.assertEqual(client.post("/events", json={"v": 0}).status_code, 400)
                self.assertEqual(client.post("/events", json=event).status_code, 204)
            self.assertEqual(len(append_threads), 1)
            self.assertNotEqual(append_threads[0], request_thread)

    def test_lifespan_waits_for_owned_notification_workers_to_stop(self):
        entered = threading.Event()
        release = threading.Event()

        class BlockingWebhook(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                entered.set()
                release.wait(3)
                self.send_response(200)
                self.end_headers()

            def log_message(self, *_args):
                pass

        webhook = http.server.ThreadingHTTPServer(("127.0.0.1", 0), BlockingWebhook)
        webhook_thread = threading.Thread(target=webhook.serve_forever, daemon=True)
        webhook_thread.start()
        begin_shutdown = threading.Event()
        session_done = threading.Event()

        with tempfile.TemporaryDirectory() as temporary:
            path = os.path.join(temporary, "events.jsonl")
            notification_url = f"http://127.0.0.1:{webhook.server_port}/"
            event = {
                "v": 0,
                "ts": "2026-08-24T12:00:00.000Z",
                "source": "test",
                "agent_id": "test:worker-owner",
                "project": "burrow",
                "cwd": "",
                "type": "needs_human",
                "payload": {"message": "wait for delivery"},
            }

            def run_session():
                config = dataclasses.replace(
                    Config(),
                    events=Path(path),
                    notify_url=notification_url,
                    notify_workers=1,
                )
                with TestClient(serve.create_app(config)) as client:
                    self.assertEqual(
                        client.post("/events", json=event).status_code, 204
                    )
                    begin_shutdown.wait(3)
                session_done.set()

            session = threading.Thread(target=run_session)
            session.start()
            try:
                self.assertTrue(entered.wait(3))
                begin_shutdown.set()
                self.assertFalse(session_done.wait(1.2))
            finally:
                release.set()
                session.join(3)
                webhook.shutdown()
                webhook.server_close()
                webhook_thread.join(3)
            self.assertTrue(session_done.is_set())


if __name__ == "__main__":
    unittest.main()
