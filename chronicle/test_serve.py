import collections
import functools
import glob
import json
import multiprocessing
import os
import pathlib
import queue
import sys
import tempfile
import threading
import unittest
from unittest import mock

import approval_protocol
import notification_persistence as knocks

PROJECT_AGENT_FIXTURE = pathlib.Path(__file__).parent / "tests" / "fixtures" / "project-agent.resident.json"
PROJECT_AGENT = json.loads(PROJECT_AGENT_FIXTURE.read_text(encoding="utf-8"))

with mock.patch.object(sys, "argv", ["serve.py"]):
    import serve


def _append_same_deliveries(events, delivery_ids, barrier):
    serve.EVENTS = events
    serve._store().reset_process_state()
    for delivery_id in delivery_ids:
        event = {
            "v": 0,
            "ts": "2026-08-24T12:00:00.000Z",
            "source": "test",
            "agent_id": "test:one",
            "project": "burrow",
            "cwd": "",
            "type": "idle",
            "payload": {},
            "delivery_id": delivery_id,
        }
        barrier.wait()
        serve.append_event(event)


def _remember_ledger_keys(events, kind, keys, barrier):
    serve.EVENTS = events
    barrier.wait()
    for key in keys:
        serve._store().remember(kind, key)


def _remember_ledger_batch(events, keys, barrier):
    serve.EVENTS = events
    barrier.wait()
    serve._store().remember_batch("notified", keys)


class NotificationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.events = os.path.join(self.tmp.name, "events.jsonl")
        self.villagers = os.path.join(self.tmp.name, "villagers")
        os.mkdir(self.villagers)
        self.patches = [
            mock.patch.object(serve, "EVENTS", self.events),
            mock.patch.object(serve, "VILLAGERS_DIR", self.villagers),
            mock.patch.object(serve, "NOTIFY_URL", "https://notify.invalid/topic"),
        ]
        for patcher in self.patches:
            patcher.start()
            self.addCleanup(patcher.stop)
        self.runtime = serve.Runtime(serve._legacy_config())
        self.addCleanup(serve.stop_knock_workers, self.runtime)
        for name in (
            "claim_knock",
            "deliver_knock",
            "finish_knock",
            "notify",
            "notify_async",
            "persist_knock",
            "transport_status",
            "villager_name",
            "_process_knock",
            "_recover_knocks",
        ):
            setattr(
                self,
                name,
                functools.partial(getattr(serve, name), runtime=self.runtime),
            )

    @staticmethod
    def event(
        agent_id="agent-a", project="burrow", message="help", ts="2026-08-24T12:00:00Z"
    ):
        return {
            "v": 0,
            "source": "claude-code",
            "ts": ts.replace("Z", ".000Z") if "." not in ts else ts,
            "agent_id": agent_id,
            "project": project,
            "type": "needs_human",
            "payload": {"message": message},
        }

    def write_events(self, *events):
        with open(self.events, "w", encoding="utf-8") as stream:
            for event in events:
                stream.write(json.dumps(event) + "\n")

    def write_soul(self, filename, **metadata):
        with open(
            os.path.join(self.villagers, filename), "w", encoding="utf-8"
        ) as stream:
            stream.write("---\n")
            for key, value in metadata.items():
                stream.write(f"{key}: {value}\n")
            stream.write("---\n")

    def write_resident(self, filename, match, name="Resident", home=0):
        manifest = {
            "manifest_version": 1,
            "match": match,
            "home": home,
            "soul": {
                "name": name,
                "char": "Monk",
                "accent": "#a68a4f",
                "role": "resident",
                "description": "A validated resident.",
            },
            "skills": [{"id": "summary", "status_ref": "bundled"}],
            "memory": {"ref": "file:///memory.md", "status_ref": "mounted"},
            "routes": [{"id": "local", "status_ref": "configured"}],
            "app_grants": [{"id": "mail", "status_ref": "configured"}],
        }
        with open(
            os.path.join(self.villagers, filename), "w", encoding="utf-8"
        ) as stream:
            json.dump(manifest, stream)

    def test_failed_delivery_can_be_claimed_again_but_success_is_deduplicated(self):
        event = self.event()
        self.assertTrue(self.claim_knock(event))
        with mock.patch.object(serve, "notify", return_value=False):
            self.deliver_knock(event)
        self.assertTrue(self.claim_knock(event))
        with mock.patch.object(serve, "notify", return_value=True):
            self.deliver_knock(event)
        self.assertFalse(self.claim_knock(event))

    def test_structured_knock_push_uses_action_and_detail(self):
        event = self.event(message="legacy fallback")
        event["payload"].update(
            {
                "request_id": "r1",
                "action": "send_email",
                "detail": {"subject": "Thursday", "to": "anna@example.com"},
                "options": ["approve", "deny", "edit"],
            }
        )
        with mock.patch.object(serve.urllib.request, "urlopen") as opened:
            opened.return_value.__enter__.return_value = object()
            self.assertTrue(self.notify(event))
        request = opened.call_args.args[0]
        self.assertEqual(request.headers["Title"], "send_email")
        self.assertEqual(
            json.loads(request.data.decode("utf-8")), event["payload"]["detail"]
        )
        self.assertEqual(
            request.headers["X-burrow-delivery-id"], serve.receiver_delivery_id(event)
        )

    def test_structured_knock_push_preserves_null_detail(self):
        event = self.event(message="legacy fallback")
        event["payload"].update(
            {
                "request_id": "r-null",
                "action": "restart_service",
                "detail": None,
                "options": ["approve", "deny"],
            }
        )
        with mock.patch.object(serve.urllib.request, "urlopen") as opened:
            opened.return_value.__enter__.return_value = object()
            self.assertTrue(self.notify(event))
        request = opened.call_args.args[0]
        self.assertEqual(request.headers["Title"], "restart_service")
        self.assertIsNone(json.loads(request.data.decode("utf-8")))

    def test_malformed_structured_push_degrades_to_the_legacy_notification(self):
        event = self.event(message="please inspect")
        event["payload"].update({"action": "send_email", "detail": {}, "options": [{}]})
        with mock.patch.object(serve.urllib.request, "urlopen") as opened:
            opened.return_value.__enter__.return_value = object()
            self.assertTrue(self.notify(event))
        request = opened.call_args.args[0]
        self.assertIn("is at your door", request.headers["Title"])
        self.assertIn("please inspect", request.data.decode("utf-8"))

    def test_structured_shape_matrix_matches_projection_rotation_and_notification(self):
        path = os.path.join(
            os.path.dirname(__file__), "tests", "fixtures", "approval-shapes.json"
        )
        with open(path, encoding="utf-8") as stream:
            cases = json.load(stream)
        for case in cases:
            event = self.event()
            event["payload"] = case["payload"]
            classified = approval_protocol.classify_approval(event)
            self.assertEqual(classified.kind, case["kind"], case["name"])
            self.assertIs(
                serve.structured_approval,
                serve.notification_persistence.structured_approval,
            )
            self.assertIs(
                serve.structured_approval, approval_protocol.structured_approval
            )
            self.assertEqual(
                serve.structured_approval(event) is not None,
                case["kind"] == "structured",
                case["name"],
            )

    def test_shared_structured_parser_returns_a_complete_deeply_immutable_shape(self):
        event = self.event(message="exact message")
        event["payload"].update(
            {
                "request_id": " request ",
                "action": "send_email",
                "detail": {"nested": {"count": 1}, "items": [True, None]},
                "options": ["approve", "approve", "edit"],
                "expires_at": None,
            }
        )
        shape = approval_protocol.structured_approval(event)
        self.assertEqual(shape.request_id, " request ")
        self.assertEqual(shape.options, ("approve", "approve", "edit"))
        self.assertTrue(shape.message_present)
        self.assertTrue(shape.expires_at_present)
        event["payload"]["detail"]["nested"]["count"] = 2
        event["payload"]["options"].append("deny")
        self.assertEqual(shape.detail["nested"]["count"], 1)
        self.assertEqual(shape.options, ("approve", "approve", "edit"))
        with self.assertRaises(TypeError):
            shape.detail["new"] = "mutable"
        with self.assertRaises((AttributeError, TypeError)):
            shape.request_id = "changed"

    def test_distinct_same_millisecond_structured_requests_have_distinct_durable_identity(
        self,
    ):
        first = self.event(ts="2026-08-24T12:00:00.000Z")
        first["payload"].update(
            {
                "request_id": "request-a",
                "action": "send_email",
                "detail": {},
                "options": ["approve"],
            }
        )
        second = json.loads(json.dumps(first))
        second["payload"]["request_id"] = "request-b"
        self.assertNotEqual(knocks.knock_key(first), knocks.knock_key(second))
        self.assertNotEqual(knocks.terminal_key(first), knocks.terminal_key(second))
        self.assertTrue(self.claim_knock(first))
        self.assertTrue(self.claim_knock(second))
        self.assertTrue(self.finish_knock(first, True))
        self.assertTrue(self.finish_knock(second, True))
        self.assertFalse(self.claim_knock(first), "exact replay stays terminal")
        self.assertFalse(
            self.claim_knock(second), "each distinct request is terminal once"
        )

    def test_structured_notification_identity_covers_exact_wire_and_immutable_shape(
        self,
    ):
        baseline = self.event(
            agent_id=" agent ", project=" project ", ts="2026-08-24T12:00:00.000Z"
        )
        baseline["source"] = "codex"
        baseline["payload"].update(
            {
                "request_id": " request ",
                "action": "send_email",
                "detail": {"count": 1, "nested": {"b": True}},
                "options": ["approve", "approve", "deny"],
                "expires_at": "2026-08-25T12:00:00Z",
            }
        )
        exact_replay = json.loads(json.dumps(baseline))
        semantic_replay = json.loads(json.dumps(baseline))
        semantic_replay["payload"]["detail"] = {
            "nested": {"b": True},
            "count": 1.0,
        }
        self.assertEqual(knocks.knock_key(baseline), knocks.knock_key(exact_replay))
        self.assertEqual(knocks.knock_key(baseline), knocks.knock_key(semantic_replay))
        self.assertTrue(knocks.knock_key(baseline).startswith("structured-v3-sha256-"))
        self.assertNotIn(
            "request",
            knocks.knock_key(baseline),
            "hashed identity must not retain approval data",
        )

        variants = []
        for mutate in (
            lambda item: item.update(agent_id="agent "),
            lambda item: item.update(project="project "),
            lambda item: item["payload"].update(request_id="request "),
            lambda item: item["payload"].update(action="restart_service"),
            lambda item: item["payload"].update(message="help "),
            lambda item: item["payload"].update(detail={"count": 2}),
            lambda item: item["payload"].update(options=["approve"]),
            lambda item: item["payload"].update(options=["approve", "deny", "approve"]),
            lambda item: item["payload"].update(expires_at="2026-08-25T12:00:01Z"),
            lambda item: item["payload"].pop("expires_at"),
            lambda item: item.update(ts="2026-08-24T12:00:00.001Z"),
        ):
            variant = json.loads(json.dumps(baseline))
            mutate(variant)
            variants.append(variant)
        keys = {
            knocks.knock_key(baseline),
            *(knocks.knock_key(item) for item in variants),
        }
        self.assertEqual(len(keys), 1 + len(variants))

    def test_structured_notification_identity_pins_the_persisted_digest_format(self):
        """``notification-identity.json`` is a golden digest, not a parity vector.

        It was written when a JavaScript half had to agree on the same key; that
        half is gone (warren#219) and no other language computes this digest.
        What survives is a storage obligation: the `structured-v3-sha256-` key is
        written to the durable notification store, so changing how it is derived
        would re-notify every knock already on disk. The vector deliberately
        carries untrimmed whitespace, an astral-plane character, a repeated
        option, unsorted keys and `1.0`, because `_canonical_json_bytes` folds
        key order and number spelling but not whitespace or array order.

        This encoder is intentionally not ``typed_json`` (warren#249): it hashes
        UTF-16BE code units for a JavaScript-shaped key sort, while typed_json
        owns the on-disk capsule format. The two must be free to diverge.
        """
        path = os.path.join(
            os.path.dirname(__file__), "tests", "fixtures", "notification-identity.json"
        )
        with open(path, encoding="utf-8") as stream:
            vector = json.load(stream)
        self.assertEqual(knocks.knock_key(vector["event"]), vector["expected_key"])

    def test_plain_knock_cannot_suppress_structured_knock_with_same_legacy_tuple(self):
        plain = self.event(ts="2026-08-24T12:00:00.000Z")
        structured = json.loads(json.dumps(plain))
        structured["payload"].update(
            {
                "request_id": "request",
                "action": "send_email",
                "detail": {},
                "options": ["approve"],
            }
        )
        self.assertNotEqual(knocks.knock_key(plain), knocks.knock_key(structured))
        self.assertTrue(self.claim_knock(plain))
        self.assertTrue(self.finish_knock(plain, True))
        self.assertTrue(self.claim_knock(structured))

    def test_ambiguous_pre_v3_terminal_keys_do_not_suppress_structured_upgrade(self):
        event = self.event(ts="2026-08-24T12:00:00.000Z")
        event["payload"].update(
            {
                "request_id": "migrated",
                "action": "send_email",
                "detail": {},
                "options": ["approve"],
            }
        )
        legacy = (
            "burrow-sha256-"
            + __import__("hashlib")
            .sha256(
                serve.notification_persistence.legacy_knock_key(event).encode(
                    "utf-8", "surrogatepass"
                )
            )
            .hexdigest()
        )
        v2 = "\x00".join(
            str(value)
            for value in (
                "structured-v2",
                event.get("agent_id"),
                event.get("ts"),
                event.get("project"),
                event.get("source"),
                "migrated",
                "send_email",
                event["payload"]["message"],
            )
        )
        v2 = (
            "burrow-sha256-"
            + __import__("hashlib")
            .sha256(v2.encode("utf-8", "surrogatepass"))
            .hexdigest()
        )
        serve._store().remember(serve.LEDGER_NOTIFIED, legacy)
        serve._store().remember(serve.LEDGER_NOTIFIED, v2)
        self.assertEqual(knocks.terminal_keys(event), (knocks.terminal_key(event),))
        self.assertTrue(
            self.claim_knock(event),
            "ambiguous historical aliases favor one safe re-notification",
        )

    def test_plain_legacy_terminal_key_still_suppresses_exact_plain_replay(self):
        event = self.event(ts="2026-08-24T12:00:00.000Z")
        key = knocks.terminal_key(event)
        serve._store().remember(serve.LEDGER_NOTIFIED, key)
        self.assertEqual(knocks.terminal_keys(event), (key,))
        self.assertFalse(self.claim_knock(event))

    def test_repeated_options_are_structured_in_notification_and_survive_restart_once(
        self,
    ):
        event = self.event(message="choose twice")
        event["payload"].update(
            {
                "request_id": "repeat",
                "action": "send_email",
                "detail": {"subject": "Repeated approval"},
                "options": ["approve", "approve"],
            }
        )
        self.assertEqual(
            serve.structured_approval(event).options, ("approve", "approve")
        )
        with mock.patch.object(serve.urllib.request, "urlopen") as opened:
            opened.return_value.__enter__.return_value = object()
            self.assertTrue(self.notify(event))
        request = opened.call_args.args[0]
        self.assertEqual(request.headers["Title"], "send_email")
        self.assertEqual(
            json.loads(request.data.decode("utf-8")), {"subject": "Repeated approval"}
        )
        self.assertTrue(self.persist_knock(event))
        recovered = queue.Queue()
        with mock.patch.object(self.runtime, "knock_queue", recovered):
            self._recover_knocks()
        replay = recovered.get_nowait()
        self.assertIn(knocks.terminal_key(replay), self.runtime.notifying)
        with mock.patch.object(serve, "notify", return_value=True):
            self.assertTrue(self.deliver_knock(replay))
        self.runtime.notified.clear()
        self.runtime.notifying.clear()
        serve._store().reset_process_state()
        self.assertFalse(
            self.claim_knock(event), "durable exact replay stays claimed once"
        )

    def test_project_resident_is_consumed_once_across_the_fleet(self):
        self.write_resident(
            "project.resident.json",
            {"project": PROJECT_AGENT["match"]["project"]},
            name=PROJECT_AGENT["soul"]["name"],
        )
        first = self.event(agent_id="a", project=PROJECT_AGENT["match"]["project"])
        second = self.event(
            agent_id="q",
            project=PROJECT_AGENT["match"]["project"],
            ts="2026-08-24T12:00:01Z",
        )
        self.write_events(first, second)

        # Keep the fixture fleet inside the viewer's visibility window regardless
        # of the wall-clock date on the machine running the suite.
        with mock.patch.object(serve.time, "time", return_value=1787574600):
            self.assertEqual(PROJECT_AGENT["soul"]["name"], self.villager_name(first))
            self.assertEqual("Juniper", self.villager_name(second))

    def test_notification_fleet_includes_routine_activity_before_allocating_home(self):
        self.write_resident("project.resident.json", {"project": "burrow"})
        first = self.event(agent_id="a")
        first["type"] = "routine_finished"
        first["source"] = "steward"
        first["payload"] = {"routine": "daily", "run_id": "run-1", "outcome": "done", "duration_s": 1, "artifacts": []}
        second = self.event(agent_id="q")
        self.write_events(first, second)
        with mock.patch.object(serve.time, "time", return_value=1787574600):
            self.assertEqual("Juniper", self.villager_name(second))

    def test_notification_uses_authoritative_absence_policy_with_custom_drop_setting(self):
        self.write_resident("project.resident.json", {"project": "burrow"})
        event = self.event(agent_id="a")
        self.write_events(event)
        config = serve.dataclasses.replace(self.runtime.config, drop_seconds=1)
        with (mock.patch.object(self.runtime, "config", config),
              mock.patch.object(serve.time, "time", return_value=1787574600)):
            self.assertEqual("Resident", self.villager_name(event))

    def test_notification_retains_identity_evidence_beyond_raw_viewer_tail(self):
        self.write_resident("project.resident.json", {"project": "burrow"})
        now = serve.datetime.datetime(2026, 8, 24, 12, tzinfo=serve.datetime.UTC)
        child = self.event(agent_id="a")
        child.update(type="task_started", payload={"prompt": "review", "parent_agent_id": "q"})
        parent_idle = self.event(agent_id="q")
        parent_idle.update(type="idle", payload={})
        declaration = self.event(agent_id="a")
        declaration.update(type="resident_declared", source="steward", payload={
            "name": "Pip", "char": "Monk", "accent": "#123456", "role": "helper",
            "summary": None, "resident_id": "pip", "uid": "0198-uid", "home": 2,
        })
        heartbeat = self.event(agent_id="a")
        heartbeat.update(type="heartbeat", payload={})
        knock = self.event(agent_id="a")
        for history, expected in [
            ([child] + [parent_idle] * 4000 + [knock], "Hazel"),
            ([declaration] + [heartbeat] * 4000 + [knock], "Pip"),
        ]:
            with self.subTest(expected=expected):
                self.write_events(*history)
                runtime = serve.Runtime(self.runtime.config)
                snapshot = runtime.state_coordinator.evaluate(now)
                projected = {item["id"]: item["name"] for item in snapshot["villagers"]}
                self.assertEqual(expected, projected["a"])
                with mock.patch.object(serve.time, "time", return_value=now.timestamp()):
                    self.assertEqual(projected["a"], serve.villager_name(knock, runtime))

    def test_fallback_names_use_the_projected_identity_algorithm(self):
        first = self.event(agent_id="a")
        second = self.event(agent_id="q", ts="2026-08-24T12:00:01Z")
        self.write_events(first, second)

        self.assertEqual(
            {"a": "Hazel", "q": "Juniper"}, serve.villager_names([first, second])
        )

    def test_fallback_identity_hashes_non_bmp_agent_ids_as_utf8(self):
        event = self.event(agent_id="agent-U0001f407")
        self.assertEqual("Thistle", serve.villager_names([event])["agent-U0001f407"])

    def test_exact_resident_is_not_reused_by_another_agent(self):
        self.write_resident(
            "resident.resident.json",
            {"agent_id": "resident"},
            name=PROJECT_AGENT["soul"]["name"],
        )
        ephemeral = self.event(agent_id="ephemeral")
        resident = self.event(agent_id="resident", ts="2026-08-24T12:00:01Z")
        self.write_events(ephemeral, resident)

        names = serve.villager_names([ephemeral, resident])
        self.assertNotEqual(PROJECT_AGENT["soul"]["name"], names["ephemeral"])
        self.assertEqual(PROJECT_AGENT["soul"]["name"], names["resident"])

    def test_resident_manifest_name_wins_over_legacy_soul_for_same_agent(self):
        self.write_resident("resident.resident.json", {"agent_id": "shared"})
        self.write_soul("legacy.md", agent_id="shared", name="Legacy")

        names = serve.villager_names([self.event(agent_id="shared")])

        self.assertEqual("Resident", names["shared"])

    def test_resident_manifest_name_wins_over_legacy_soul_for_same_project(self):
        self.write_resident("resident.resident.json", {"project": "burrow"})
        self.write_soul("legacy.md", project="burrow", name="Legacy")

        names = serve.villager_names([self.event(agent_id="visitor")])

        self.assertEqual("Resident", names["visitor"])

    def test_invalid_diagnostic_resident_never_enters_villager_projection(self):
        self.write_resident("unsafe.resident.json", {"agent_id": "unsafe"}, home=7)
        path = os.path.join(self.villagers, "unsafe.resident.json")
        with open(path, encoding="utf-8") as stream:
            manifest = json.load(stream)
        manifest["skills"][0]["password"] = "do-not-leak-this-secret"
        with open(path, "w", encoding="utf-8") as stream:
            json.dump(manifest, stream)

        report = serve.read_residents()
        self.assertEqual(report["residents"], [])
        self.assertEqual(len(report["diagnostic_residents"]), 1)
        self.assertEqual(serve.read_villagers(), [])
        rendered = json.dumps(report)
        self.assertNotIn("do-not-leak-this-secret", rendered)
        self.assertNotIn('"password": "', rendered)

    def test_child_lineage_survives_later_events_for_notification_names(self):
        self.write_resident(
            "project.resident.json",
            {"project": PROJECT_AGENT["match"]["project"]},
            name=PROJECT_AGENT["soul"]["name"],
        )
        project = PROJECT_AGENT["match"]["project"]
        parent = self.event(agent_id="z-parent", project=project)
        child_start = self.event(
            agent_id="a-child", project=project, ts="2026-08-24T11:59:58Z"
        )
        child_start["type"] = "task_started"
        child_start["payload"] = {
            "prompt": "review",
            "parent_agent_id": "z-parent",
            "agent_type": "reviewer",
        }
        child_tool = self.event(
            agent_id="a-child", project=project, ts="2026-08-24T11:59:59Z"
        )
        child_tool["type"] = "tool_called"
        child_tool["payload"] = {"tool": "Read"}
        child_knock = self.event(agent_id="a-child", project=project)
        self.write_events(child_start, child_tool, parent)

        names = serve.villager_names([child_start, child_tool, parent, child_knock])
        self.assertEqual(PROJECT_AGENT["soul"]["name"], names["z-parent"])
        self.assertNotEqual(PROJECT_AGENT["soul"]["name"], names["a-child"])
        self.assertEqual(names["a-child"], self.villager_name(child_knock))

    def test_unicode_title_is_ascii_safe_for_http(self):
        event = self.event(project="项目")
        self.write_soul("unicode.md", agent_id="agent-a", name="玛伦")
        self.write_events(event)
        captured = {}

        def open_request(request, timeout):
            captured["request"] = request
            captured["timeout"] = timeout
            return mock.MagicMock()

        with mock.patch.object(
            serve.urllib.request, "urlopen", side_effect=open_request
        ):
            self.assertTrue(self.notify(event))

        title = captured["request"].get_header("Title")
        title.encode("latin-1")
        self.assertIn("=?utf-8?", title.lower())

    def test_message_is_preserved_verbatim(self):
        event = self.event(message="  first line\nsecond line\n")
        self.write_events(event)
        captured = {}

        def open_request(request, timeout):
            captured["body"] = request.data
            return mock.MagicMock()

        with mock.patch.object(
            serve.urllib.request, "urlopen", side_effect=open_request
        ):
            self.assertTrue(self.notify(event))

        name = self.villager_name(event)
        self.assertEqual(
            f"{name} · burrow\n  first line\nsecond line\n".encode(), captured["body"]
        )

        empty = self.event(message="")
        with mock.patch.object(
            serve.urllib.request, "urlopen", side_effect=open_request
        ):
            self.assertTrue(self.notify(empty))
        self.assertEqual(f"{name} · burrow\n".encode(), captured["body"])

    def test_notification_has_stable_receiver_dedupe_header(self):
        event = self.event()
        captured = {}

        def open_request(request, timeout):
            captured["request"] = request
            return mock.MagicMock()

        with mock.patch.object(
            serve.urllib.request, "urlopen", side_effect=open_request
        ):
            self.assertTrue(self.notify(event))
        self.assertEqual(
            captured["request"].get_header("X-burrow-delivery-id"),
            serve.receiver_delivery_id(event),
        )

    def test_notification_queue_saturation_is_bounded_and_inspectable(self):
        tiny = serve.queue.Queue(maxsize=1)
        with (
            mock.patch.object(self.runtime, "knock_queue", tiny),
            mock.patch.object(serve, "ensure_knock_workers"),
        ):
            self.assertTrue(self.notify_async(self.event(agent_id="first")))
            self.assertFalse(self.notify_async(self.event(agent_id="second")))
        status = self.transport_status()
        self.assertGreaterEqual(status["notifications"]["saturated"], 1)

    def test_successful_knock_is_not_reclaimed_after_restart_or_log_retention(self):
        event = self.event()
        event["delivery_id"] = "durable-delivery-id-0001"
        self.assertTrue(self.claim_knock(event))
        with mock.patch.object(serve, "notify", return_value=True):
            self.deliver_knock(event)
        self.runtime.notified.clear()
        self.runtime.notifying.clear()
        serve._store().reset_process_state()
        self.write_events()
        self.assertFalse(self.claim_knock(event))

    def test_accepted_knock_is_recovered_from_journal_after_restart(self):
        event = self.event()
        self.assertTrue(self.persist_knock(event))
        recovered = serve.queue.Queue(maxsize=4)
        with mock.patch.object(self.runtime, "knock_queue", recovered):
            self._recover_knocks()
        self.assertEqual(recovered.get_nowait(), event)

    def test_recovery_handoff_does_not_lose_a_concurrent_append(self):
        first = self.event(agent_id="first")
        second = self.event(agent_id="second", ts="2026-08-24T12:00:01Z")
        self.assertTrue(self.persist_knock(first))
        real_replace = os.replace
        appended = []
        appenders = []

        def append_after_handoff(source, destination):
            real_replace(source, destination)
            worker = threading.Thread(
                target=lambda: appended.append(self.persist_knock(second))
            )
            worker.start()
            appenders.append(worker)

        recovered = serve.queue.Queue(maxsize=4)
        with mock.patch.object(self.runtime, "knock_queue", recovered):
            with mock.patch.object(
                serve.os, "replace", side_effect=append_after_handoff
            ):
                self._recover_knocks()
            appenders[0].join(1)
            self.assertEqual(appended, [True])
            self.assertEqual(recovered.get_nowait(), first)
            self._recover_knocks()
            self.assertEqual(recovered.get_nowait(), second)

    def test_recovery_preserves_unresolved_replay_and_new_active_work(self):
        old = self.event(agent_id="old")
        new = self.event(agent_id="new", ts="2026-08-24T12:00:01Z")
        self.assertTrue(self.persist_knock(old))
        first_restart = serve.queue.Queue(maxsize=4)
        with mock.patch.object(self.runtime, "knock_queue", first_restart):
            self._recover_knocks()
        self.assertEqual(first_restart.get_nowait(), old)
        # A concurrent/newer writer may have created an active generation after
        # the old replay was handed off.
        with open(self.events + ".knocks", "w", encoding="utf-8") as stream:
            stream.write(json.dumps({"event": new, "attempts": 0}) + "\n")
            stream.flush()
            os.fsync(stream.fileno())

        # Simulate a restart: neither unresolved in-memory claim survives.
        self.runtime.notifying.clear()
        second_restart = serve.queue.Queue(maxsize=4)
        with mock.patch.object(self.runtime, "knock_queue", second_restart):
            self._recover_knocks()
        recovered = {
            knocks.knock_key(second_restart.get_nowait()),
            knocks.knock_key(second_restart.get_nowait()),
        }
        self.assertEqual(recovered, {knocks.knock_key(old), knocks.knock_key(new)})

    def test_legacy_knock_uses_stable_ascii_receiver_delivery_id(self):
        event = self.event()
        requests = []

        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

        with mock.patch.object(
            serve.urllib.request,
            "urlopen",
            side_effect=lambda request, timeout: requests.append(request) or Response(),
        ):
            self.assertTrue(self.notify(event))
            self.assertTrue(self.notify(event))
        values = [request.get_header("X-burrow-delivery-id") for request in requests]
        self.assertEqual(values[0], values[1])
        self.assertTrue(values[0].isascii())
        self.assertNotIn("\0", values[0])

    def test_recovery_is_idempotent_after_crash_leaves_handoff(self):
        event = self.event()
        self.assertTrue(self.persist_knock(event))
        recovered = serve.queue.Queue(maxsize=4)
        with mock.patch.object(self.runtime, "knock_queue", recovered):
            self._recover_knocks()
            self._recover_knocks()
        self.assertEqual(recovered.get_nowait(), event)
        with self.assertRaises(serve.queue.Empty):
            recovered.get_nowait()

    def test_repeated_crash_recovery_never_accumulates_replay_generations(self):
        self.assertTrue(self.persist_knock(self.event()))
        recovered = serve.queue.Queue(maxsize=20)
        with mock.patch.object(self.runtime, "knock_queue", recovered):
            for _ in range(8):
                self._recover_knocks()
                generations = glob.glob(self.events + ".knocks.replay.*")
                self.assertEqual(len(generations), 1)
                # Simulate compaction crashing after the active publish but
                # before its replay source is retired.
                with (
                    open(generations[0], "rb") as source,
                    open(self.events + ".knocks", "wb") as active,
                ):
                    active.write(source.read())
                    active.flush()
                    os.fsync(active.fileno())

    def test_partial_crash_tail_does_not_hide_a_complete_knock(self):
        event = self.event()
        self.assertTrue(self.persist_knock(event))
        with open(self.events + ".knocks", "a", encoding="utf-8") as stream:
            stream.write('{"partial":')
        recovered = serve.queue.Queue(maxsize=4)
        with mock.patch.object(self.runtime, "knock_queue", recovered):
            self._recover_knocks()
        self.assertEqual(recovered.get_nowait(), event)

    def test_completed_replay_generation_is_reclaimed_only_after_durable_outcomes(self):
        delivered = self.event(agent_id="delivered")
        dropped = self.event(agent_id="dropped", ts="2026-08-24T12:00:01Z")
        self.assertTrue(self.persist_knock(delivered))
        self.assertTrue(self.persist_knock(dropped))
        recovered = serve.queue.Queue(maxsize=4)
        with mock.patch.object(self.runtime, "knock_queue", recovered):
            self._recover_knocks()
        generations = glob.glob(self.events + ".knocks.replay.*")
        self.assertEqual(len(generations), 1)
        serve._store().remember("notified", knocks.terminal_key(delivered))
        self._recover_knocks()
        self.assertTrue(os.path.exists(generations[0]))
        serve._store().remember("notify-dropped", knocks.terminal_key(dropped))
        self._recover_knocks()
        self.assertFalse(os.path.exists(generations[0]))

    def test_server_startup_recovers_pending_knocks_without_new_ingest(self):
        event = self.event()
        self.assertTrue(self.persist_knock(event))
        from fastapi.testclient import TestClient

        with (
            mock.patch.object(serve, "ensure_knock_workers") as ensure,
            mock.patch.object(serve, "stop_knock_workers") as stop,
            TestClient(serve.app),
        ):
            pass
        ensure.assert_called_once()
        self.assertIsInstance(ensure.call_args.args[0], serve.Runtime)
        stop.assert_called_once_with(ensure.call_args.args[0])

    def test_knock_is_not_acknowledgeable_when_durable_journal_fails(self):
        event = self.event()
        with mock.patch("builtins.open", side_effect=OSError("disk full")):
            self.assertFalse(self.persist_knock(event))

    def test_failed_worker_delivery_has_bounded_retry_and_drop_accounting(self):
        event = self.event()
        tiny = serve.queue.Queue(maxsize=4)
        with (
            mock.patch.object(self.runtime, "knock_queue", tiny),
            mock.patch.object(serve, "notify", return_value=False),
            mock.patch.object(serve, "_recover_knocks"),
        ):
            self.assertTrue(self.claim_knock(event))
            self._process_knock(event)
            self._process_knock(tiny.get_nowait())
            self._process_knock(tiny.get_nowait())
        status = self.transport_status()["notifications"]
        self.assertGreaterEqual(status["retried"], 2)
        self.assertGreaterEqual(status["dropped"], 1)
        serve._store().reset_process_state()
        self.assertFalse(self.claim_knock(event))

    def test_failed_attempts_survive_restart_and_reach_terminal_drop(self):
        event = self.event()
        self.assertTrue(self.persist_knock(event))
        first_queue = serve.queue.Queue(maxsize=4)
        with mock.patch.object(self.runtime, "knock_queue", first_queue):
            self._recover_knocks()
        with (
            mock.patch.object(self.runtime, "knock_queue", first_queue),
            mock.patch.object(serve, "notify", return_value=False),
            mock.patch.object(serve, "_recover_knocks"),
        ):
            self._process_knock(first_queue.get_nowait())

        self.runtime.notifying.clear()
        serve._store().reset_process_state()
        restarted = serve.queue.Queue(maxsize=4)
        with mock.patch.object(self.runtime, "knock_queue", restarted):
            self._recover_knocks()
        with (
            mock.patch.object(self.runtime, "knock_queue", restarted),
            mock.patch.object(serve, "notify", return_value=False),
            mock.patch.object(serve, "_recover_knocks"),
        ):
            self._process_knock(restarted.get_nowait())
            self._process_knock(restarted.get_nowait())

        serve._store().reset_process_state()
        self.assertFalse(self.claim_knock(event))
        self.assertTrue(os.path.exists(self.events + ".notify-dropped"))


class TransportDiagnosticsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.events = os.path.join(self.tmp.name, "events.jsonl")
        self.patch = mock.patch.object(serve, "EVENTS", self.events)
        self.patch.start()
        self.addCleanup(self.patch.stop)
        serve._store().reset_process_state()

    @staticmethod
    def event(delivery_id):
        return {
            "v": 0,
            "ts": "2026-08-24T12:00:00.000Z",
            "source": "test",
            "agent_id": "test:one",
            "project": "burrow",
            "cwd": "",
            "type": "idle",
            "payload": {},
            "delivery_id": delivery_id,
        }

    def test_retried_delivery_is_appended_exactly_once_within_dedupe_window(self):
        event = self.event("a" * 32)
        self.assertTrue(serve.append_event(event))
        self.assertFalse(serve.append_event(event))
        with open(self.events, encoding="utf-8") as stream:
            self.assertEqual(sum(1 for line in stream if line.strip()), 1)
        self.assertGreaterEqual(serve._transport_counters["ingest_duplicates"], 1)

    def test_delivery_id_dedupe_survives_restart_and_live_log_retention(self):
        event = self.event("persistent-delivery-0001")
        self.assertTrue(serve.append_event(event))
        with open(self.events, "w", encoding="utf-8"):
            pass
        serve._store().reset_process_state()
        self.assertFalse(serve.append_event(event))
        with open(self.events, encoding="utf-8") as stream:
            self.assertEqual(stream.read(), "")

    def test_event_log_repairs_crash_between_event_fsync_and_delivery_ledger(self):
        event = self.event("crash-window-delivery-0001")
        with mock.patch.object(
            serve._store(), "remember", side_effect=OSError("crash after event fsync")
        ):
            with self.assertRaises(OSError):
                serve.append_event(event)
        serve._store().reset_process_state()
        self.assertFalse(serve.append_event(event))
        with open(self.events, encoding="utf-8") as stream:
            self.assertEqual(sum(1 for line in stream if line.strip()), 1)

    def test_two_processes_append_each_delivery_id_exactly_once(self):
        delivery_ids = [f"multiprocess-delivery-{index:04d}" for index in range(20)]
        context = multiprocessing.get_context("fork")
        barrier = context.Barrier(2)
        processes = [
            context.Process(
                target=_append_same_deliveries,
                args=(self.events, delivery_ids, barrier),
            )
            for _ in range(2)
        ]

        for process in processes:
            process.start()
        for process in processes:
            process.join(10)
            self.assertEqual(process.exitcode, 0)

        with open(self.events, encoding="utf-8") as stream:
            events = [json.loads(line) for line in stream if line.strip()]
        counts = collections.Counter(event["delivery_id"] for event in events)
        self.assertEqual(counts, collections.Counter({key: 1 for key in delivery_ids}))

    def test_auxiliary_ledger_is_bounded_by_records_and_bytes_after_restart(self):
        with (
            mock.patch.object(serve, "LEDGER_RECORDS", 4),
            mock.patch.object(serve, "LEDGER_BYTES", 24),
        ):
            for index in range(10):
                serve._store().remember("delivery-ids", f"key-{index}")
            serve._store().reset_process_state()
            remembered = serve._store().load_ledger("delivery-ids")
        self.assertEqual(remembered, {"key-6", "key-7", "key-8", "key-9"})
        self.assertLessEqual(os.path.getsize(self.events + ".delivery-ids"), 24)

    def test_existing_ledger_key_refreshes_newest_retention(self):
        with (
            mock.patch.object(serve, "LEDGER_RECORDS", 2),
            mock.patch.object(serve, "LEDGER_BYTES", 4096),
        ):
            serve._store().remember_batch("notified", ("A", "B"))
            serve._store().remember("notified", "A")
            serve._store().remember("notified", "C")
        with open(self.events + ".notified", encoding="utf-8") as stream:
            self.assertEqual(stream.read().splitlines(), ["A", "C"])

    def test_multiprocess_batch_refresh_preserves_atomic_order(self):
        with (
            mock.patch.object(serve, "LEDGER_RECORDS", 3),
            mock.patch.object(serve, "LEDGER_BYTES", 4096),
        ):
            serve._store().remember_batch("notified", ("A", "B", "C"))
            context = multiprocessing.get_context("fork")
            gate = context.Barrier(2)
            processes = [
                context.Process(
                    target=_remember_ledger_batch, args=(self.events, keys, gate)
                )
                for keys in (("A",), ("D",))
            ]
            for process in processes:
                process.start()
            for process in processes:
                process.join(10)
                self.assertEqual(process.exitcode, 0)
        with open(self.events + ".notified", encoding="utf-8") as stream:
            retained = stream.read().splitlines()
        self.assertEqual(set(retained), {"A", "C", "D"})
        self.assertEqual(retained[-1], "D" if retained[-2] == "A" else "A")

    def test_evicted_delivery_id_still_deduplicates_from_retained_event_authority(self):
        events = [self.event(f"retained-delivery-{index:04d}") for index in range(3)]
        with (
            mock.patch.object(serve, "LEDGER_RECORDS", 2),
            mock.patch.object(serve, "LEDGER_BYTES", 4096),
        ):
            for event in events:
                self.assertTrue(serve.append_event(event))
            serve._store().reset_process_state()
            self.assertFalse(serve.append_event(events[0]))
        with open(self.events, encoding="utf-8") as stream:
            self.assertEqual(sum(1 for line in stream if line.strip()), 3)

    def test_multiprocess_ledger_compaction_loses_no_retained_writes(self):
        context = multiprocessing.get_context("fork")
        barrier = context.Barrier(2)
        first = [f"first-{index}" for index in range(20)]
        second = [f"second-{index}" for index in range(20)]
        processes = [
            context.Process(
                target=_remember_ledger_keys,
                args=(self.events, "notified", first, barrier),
            ),
            context.Process(
                target=_remember_ledger_keys,
                args=(self.events, "notified", second, barrier),
            ),
        ]
        with (
            mock.patch.object(serve, "LEDGER_RECORDS", 64),
            mock.patch.object(serve, "LEDGER_BYTES", 4096),
        ):
            for process in processes:
                process.start()
            for process in processes:
                process.join(10)
                self.assertEqual(process.exitcode, 0)
        serve._store().reset_process_state()
        remembered = serve._store().load_ledger("notified")
        self.assertEqual(remembered, set(first + second))


if __name__ == "__main__":
    unittest.main()
