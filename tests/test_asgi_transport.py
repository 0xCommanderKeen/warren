import http.server
import os
import tempfile
import threading
import unittest
from unittest import mock

from fastapi.testclient import TestClient

import serve


class ASGITransportContractTests(unittest.TestCase):
    def test_village_is_served_and_removed_observatory_routes_are_not(self):
        with TestClient(serve.app) as client:
            village = client.get("/village/")
            observatory = client.get("/observatory/")
            agent_page = client.get("/observatory/agents/keeper")
            transport = client.get("/village/state-transport.js")

        self.assertEqual(village.status_code, 200)
        self.assertIn("Burrow", village.text)
        self.assertEqual(observatory.status_code, 404)
        self.assertNotIn("Burrow", observatory.text)
        self.assertEqual(agent_page.status_code, 404)
        self.assertNotIn("Burrow", agent_page.text)
        self.assertEqual(transport.status_code, 200)
        self.assertIn("createStateTransport", transport.text)

    def test_fastapi_openapi_names_public_wire_contracts(self):
        schema = serve.app.openapi()
        self.assertEqual(schema["info"]["title"], "Burrow Village API")
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
                with (
                    mock.patch.object(serve, "EVENTS", path),
                    mock.patch.object(serve, "NOTIFY_URL", notification_url),
                    mock.patch.object(serve, "NOTIFY_WORKERS", 1),
                    TestClient(serve.app) as client,
                ):
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
