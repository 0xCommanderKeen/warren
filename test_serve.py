import collections
import glob
import json
import multiprocessing
import os
import sys
import tempfile
import threading
import unittest
from unittest import mock

with mock.patch.object(sys, "argv", ["serve.py"]):
    import serve


def _append_same_deliveries(events, delivery_ids, barrier):
    serve.EVENTS = events
    serve._delivery_ids_by_log.clear()
    for delivery_id in delivery_ids:
        event = {
            "v": 0, "ts": "2026-08-24T12:00:00.000Z", "source": "test",
            "agent_id": "test:one", "project": "burrow", "cwd": "",
            "type": "idle", "payload": {}, "delivery_id": delivery_id,
        }
        barrier.wait()
        serve.append_event(event)


def _remember_ledger_keys(events, kind, keys, barrier):
    serve.EVENTS = events
    cache = {}
    barrier.wait()
    for key in keys:
        serve._remember_durable(kind, cache, key)


def _remember_ledger_batch(events, keys, barrier):
    serve.EVENTS = events
    barrier.wait()
    serve._remember_durable_batch("notified", {}, keys)


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
        serve._notified.clear()
        serve._notifying.clear()
        serve._notified_by_log.clear()
        serve._dropped_by_log.clear()
        serve._knocks_by_log.clear()
        serve._knock_attempts.clear()

    @staticmethod
    def event(agent_id="agent-a", project="burrow", message="help", ts="2026-08-24T12:00:00Z"):
        return {
            "v": 1,
            "ts": ts,
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
        with open(os.path.join(self.villagers, filename), "w", encoding="utf-8") as stream:
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
                "name": name, "char": "Monk", "accent": "#a68a4f",
                "role": "resident", "description": "A validated resident.",
            },
            "skills": [{"id": "summary", "status_ref": "bundled"}],
            "memory": {"ref": "file:///memory.md", "status_ref": "mounted"},
            "routes": [{"id": "local", "status_ref": "configured"}],
            "app_grants": [{"id": "mail", "status_ref": "configured"}],
        }
        with open(os.path.join(self.villagers, filename), "w", encoding="utf-8") as stream:
            json.dump(manifest, stream)

    def test_failed_delivery_can_be_claimed_again_but_success_is_deduplicated(self):
        event = self.event()
        self.assertTrue(serve.claim_knock(event))
        with mock.patch.object(serve, "notify", return_value=False):
            serve.deliver_knock(event)
        self.assertTrue(serve.claim_knock(event))
        with mock.patch.object(serve, "notify", return_value=True):
            serve.deliver_knock(event)
        self.assertFalse(serve.claim_knock(event))

    def test_project_soul_is_consumed_once_across_the_fleet(self):
        self.write_soul("burrow.md", project="burrow", name="Maren")
        first = self.event(agent_id="a")
        second = self.event(agent_id="q", ts="2026-08-24T12:00:01Z")
        self.write_events(first, second)

        # Keep the fixture fleet inside the viewer's visibility window regardless
        # of the wall-clock date on the machine running the suite.
        with mock.patch.object(serve.time, "time", return_value=1787574600):
            self.assertEqual("Maren", serve.villager_name(first))
            self.assertEqual("Poppy", serve.villager_name(second))

    def test_fallback_names_probe_hash_collisions_across_the_fleet(self):
        first = self.event(agent_id="a")
        second = self.event(agent_id="q", ts="2026-08-24T12:00:01Z")
        self.write_events(first, second)

        self.assertEqual({"a": "Poppy", "q": "Wren"},
                         serve.villager_names([first, second]))

    def test_hash_matches_javascript_for_non_bmp_agent_ids(self):
        event = self.event(agent_id="agent-U0001f407")
        self.assertEqual("Reed", serve.villager_names([event])["agent-U0001f407"])

    def test_exact_soul_is_not_reused_by_another_agent(self):
        self.write_soul("resident.md", agent_id="resident", project="burrow", name="Maren")
        ephemeral = self.event(agent_id="ephemeral")
        resident = self.event(agent_id="resident", ts="2026-08-24T12:00:01Z")
        self.write_events(ephemeral, resident)

        names = serve.villager_names([ephemeral, resident])
        self.assertNotEqual("Maren", names["ephemeral"])
        self.assertEqual("Maren", names["resident"])

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

    def test_child_lineage_survives_later_events_for_notification_names(self):
        self.write_soul("burrow.md", project="burrow", name="Maren")
        parent = self.event(agent_id="z-parent")
        child_start = self.event(agent_id="a-child", ts="2026-08-24T11:59:58Z")
        child_start["type"] = "task_started"
        child_start["payload"] = {"parent_agent_id": "z-parent", "agent_type": "reviewer"}
        child_tool = self.event(agent_id="a-child", ts="2026-08-24T11:59:59Z")
        child_tool["type"] = "tool_called"
        child_tool["payload"] = {"tool": "Read"}
        child_knock = self.event(agent_id="a-child")
        self.write_events(child_start, child_tool, parent)

        names = serve.villager_names([child_start, child_tool, parent, child_knock])
        self.assertEqual("Maren", names["z-parent"])
        self.assertNotEqual("Maren", names["a-child"])
        self.assertEqual(names["a-child"], serve.villager_name(child_knock))

    def test_unicode_title_is_ascii_safe_for_http(self):
        event = self.event(project="项目")
        self.write_soul("unicode.md", agent_id="agent-a", name="玛伦")
        self.write_events(event)
        captured = {}

        def open_request(request, timeout):
            captured["request"] = request
            captured["timeout"] = timeout
            return mock.MagicMock()

        with mock.patch.object(serve.urllib.request, "urlopen", side_effect=open_request):
            self.assertTrue(serve.notify(event))

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

        with mock.patch.object(serve.urllib.request, "urlopen", side_effect=open_request):
            self.assertTrue(serve.notify(event))

        name = serve.villager_name(event)
        self.assertEqual(
            f"{name} · burrow\n  first line\nsecond line\n".encode(), captured["body"])

        empty = self.event(message="")
        with mock.patch.object(serve.urllib.request, "urlopen", side_effect=open_request):
            self.assertTrue(serve.notify(empty))
        self.assertEqual(f"{name} · burrow\n".encode(), captured["body"])

    def test_notification_has_stable_receiver_dedupe_header(self):
        event = self.event()
        captured = {}
        def open_request(request, timeout):
            captured["request"] = request
            return mock.MagicMock()
        with mock.patch.object(serve.urllib.request, "urlopen", side_effect=open_request):
            self.assertTrue(serve.notify(event))
        self.assertEqual(captured["request"].get_header("X-burrow-delivery-id"),
                         serve.receiver_delivery_id(event))

    def test_notification_queue_saturation_is_bounded_and_inspectable(self):
        tiny = serve.queue.Queue(maxsize=1)
        with mock.patch.object(serve, "_knock_queue", tiny), \
                mock.patch.object(serve, "ensure_knock_workers"):
            self.assertTrue(serve.notify_async(self.event(agent_id="first")))
            self.assertFalse(serve.notify_async(self.event(agent_id="second")))
        status = serve.transport_status()
        self.assertGreaterEqual(status["notifications"]["saturated"], 1)

    def test_successful_knock_is_not_reclaimed_after_restart_or_log_retention(self):
        event = self.event()
        event["delivery_id"] = "durable-delivery-id-0001"
        self.assertTrue(serve.claim_knock(event))
        with mock.patch.object(serve, "notify", return_value=True):
            serve.deliver_knock(event)
        serve._notified.clear()
        serve._notifying.clear()
        serve._notified_by_log.clear()
        self.write_events()
        self.assertFalse(serve.claim_knock(event))

    def test_accepted_knock_is_recovered_from_journal_after_restart(self):
        event = self.event()
        self.assertTrue(serve.persist_knock(event))
        recovered = serve.queue.Queue(maxsize=4)
        with mock.patch.object(serve, "_knock_queue", recovered):
            serve._recover_knocks()
        self.assertEqual(recovered.get_nowait(), event)

    def test_recovery_handoff_does_not_lose_a_concurrent_append(self):
        first = self.event(agent_id="first")
        second = self.event(agent_id="second", ts="2026-08-24T12:00:01Z")
        self.assertTrue(serve.persist_knock(first))
        real_replace = os.replace
        appended = []
        appenders = []

        def append_after_handoff(source, destination):
            real_replace(source, destination)
            worker = threading.Thread(
                target=lambda: appended.append(serve.persist_knock(second)))
            worker.start()
            appenders.append(worker)

        recovered = serve.queue.Queue(maxsize=4)
        with mock.patch.object(serve, "_knock_queue", recovered):
            with mock.patch.object(serve.os, "replace",
                                   side_effect=append_after_handoff):
                serve._recover_knocks()
            appenders[0].join(1)
            self.assertEqual(appended, [True])
            self.assertEqual(recovered.get_nowait(), first)
            serve._recover_knocks()
            self.assertEqual(recovered.get_nowait(), second)

    def test_recovery_preserves_unresolved_replay_and_new_active_work(self):
        old = self.event(agent_id="old")
        new = self.event(agent_id="new", ts="2026-08-24T12:00:01Z")
        self.assertTrue(serve.persist_knock(old))
        first_restart = serve.queue.Queue(maxsize=4)
        with mock.patch.object(serve, "_knock_queue", first_restart):
            serve._recover_knocks()
        self.assertEqual(first_restart.get_nowait(), old)
        # A concurrent/newer writer may have created an active generation after
        # the old replay was handed off.
        with open(self.events + ".knocks", "w", encoding="utf-8") as stream:
            stream.write(json.dumps({"event": new, "attempts": 0}) + "\n")
            stream.flush()
            os.fsync(stream.fileno())

        # Simulate a restart: neither unresolved in-memory claim survives.
        serve._notifying.clear()
        second_restart = serve.queue.Queue(maxsize=4)
        with mock.patch.object(serve, "_knock_queue", second_restart):
            serve._recover_knocks()
        recovered = {serve.knock_key(second_restart.get_nowait()),
                     serve.knock_key(second_restart.get_nowait())}
        self.assertEqual(recovered, {serve.knock_key(old), serve.knock_key(new)})

    def test_legacy_knock_uses_stable_ascii_receiver_delivery_id(self):
        event = self.event()
        requests = []

        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

        with mock.patch.object(serve.urllib.request, "urlopen",
                               side_effect=lambda request, timeout: requests.append(request)
                               or Response()):
            self.assertTrue(serve.notify(event))
            self.assertTrue(serve.notify(event))
        values = [request.get_header("X-burrow-delivery-id") for request in requests]
        self.assertEqual(values[0], values[1])
        self.assertTrue(values[0].isascii())
        self.assertNotIn("\0", values[0])

    def test_recovery_is_idempotent_after_crash_leaves_handoff(self):
        event = self.event()
        self.assertTrue(serve.persist_knock(event))
        recovered = serve.queue.Queue(maxsize=4)
        with mock.patch.object(serve, "_knock_queue", recovered):
            serve._recover_knocks()
            serve._recover_knocks()
        self.assertEqual(recovered.get_nowait(), event)
        with self.assertRaises(serve.queue.Empty):
            recovered.get_nowait()

    def test_repeated_crash_recovery_never_accumulates_replay_generations(self):
        self.assertTrue(serve.persist_knock(self.event()))
        recovered = serve.queue.Queue(maxsize=20)
        with mock.patch.object(serve, "_knock_queue", recovered):
            for _ in range(8):
                serve._recover_knocks()
                generations = glob.glob(self.events + ".knocks.replay.*")
                self.assertEqual(len(generations), 1)
                # Simulate compaction crashing after the active publish but
                # before its replay source is retired.
                with open(generations[0], "rb") as source, \
                        open(self.events + ".knocks", "wb") as active:
                    active.write(source.read())
                    active.flush()
                    os.fsync(active.fileno())

    def test_partial_crash_tail_does_not_hide_a_complete_knock(self):
        event = self.event()
        self.assertTrue(serve.persist_knock(event))
        with open(self.events + ".knocks", "a", encoding="utf-8") as stream:
            stream.write('{"partial":')
        recovered = serve.queue.Queue(maxsize=4)
        with mock.patch.object(serve, "_knock_queue", recovered):
            serve._recover_knocks()
        self.assertEqual(recovered.get_nowait(), event)

    def test_completed_replay_generation_is_reclaimed_only_after_durable_outcomes(self):
        delivered = self.event(agent_id="delivered")
        dropped = self.event(agent_id="dropped", ts="2026-08-24T12:00:01Z")
        self.assertTrue(serve.persist_knock(delivered))
        self.assertTrue(serve.persist_knock(dropped))
        recovered = serve.queue.Queue(maxsize=4)
        with mock.patch.object(serve, "_knock_queue", recovered):
            serve._recover_knocks()
        generations = glob.glob(self.events + ".knocks.replay.*")
        self.assertEqual(len(generations), 1)
        serve._remember_durable("notified", serve._notified_by_log,
                                serve.terminal_knock_key(delivered))
        serve._recover_knocks()
        self.assertTrue(os.path.exists(generations[0]))
        serve._remember_durable("notify-dropped", serve._dropped_by_log,
                                serve.terminal_knock_key(dropped))
        serve._recover_knocks()
        self.assertFalse(os.path.exists(generations[0]))

    def test_server_startup_recovers_pending_knocks_without_new_ingest(self):
        event = self.event()
        self.assertTrue(serve.persist_knock(event))
        fake_server = mock.MagicMock()
        with mock.patch.object(serve, "ensure_knock_workers") as ensure, \
                mock.patch.object(serve, "BurrowHTTPServer",
                                  return_value=fake_server):
            serve.serve_forever()
        ensure.assert_called_once_with()
        fake_server.serve_forever.assert_called_once_with()

    def test_knock_is_not_acknowledgeable_when_durable_journal_fails(self):
        event = self.event()
        with mock.patch("builtins.open", side_effect=OSError("disk full")):
            self.assertFalse(serve.persist_knock(event))

    def test_failed_worker_delivery_has_bounded_retry_and_drop_accounting(self):
        event = self.event()
        tiny = serve.queue.Queue(maxsize=4)
        with mock.patch.object(serve, "_knock_queue", tiny), \
                mock.patch.object(serve, "notify", return_value=False), \
                mock.patch.object(serve, "_recover_knocks"):
            self.assertTrue(serve.claim_knock(event))
            serve._process_knock(event)
            serve._process_knock(tiny.get_nowait())
            serve._process_knock(tiny.get_nowait())
        status = serve.transport_status()["notifications"]
        self.assertGreaterEqual(status["retried"], 2)
        self.assertGreaterEqual(status["dropped"], 1)
        serve._dropped_by_log.clear()
        self.assertFalse(serve.claim_knock(event))

    def test_failed_attempts_survive_restart_and_reach_terminal_drop(self):
        event = self.event()
        self.assertTrue(serve.persist_knock(event))
        first_queue = serve.queue.Queue(maxsize=4)
        with mock.patch.object(serve, "_knock_queue", first_queue):
            serve._recover_knocks()
        with mock.patch.object(serve, "_knock_queue", first_queue), \
                mock.patch.object(serve, "notify", return_value=False), \
                mock.patch.object(serve, "_recover_knocks"):
            serve._process_knock(first_queue.get_nowait())

        serve._notifying.clear()
        serve._knock_attempts.clear()
        serve._knocks_by_log.clear()
        restarted = serve.queue.Queue(maxsize=4)
        with mock.patch.object(serve, "_knock_queue", restarted):
            serve._recover_knocks()
        with mock.patch.object(serve, "_knock_queue", restarted), \
                mock.patch.object(serve, "notify", return_value=False), \
                mock.patch.object(serve, "_recover_knocks"):
            serve._process_knock(restarted.get_nowait())
            serve._process_knock(restarted.get_nowait())

        serve._dropped_by_log.clear()
        self.assertFalse(serve.claim_knock(event))
        self.assertTrue(os.path.exists(self.events + ".notify-dropped"))


class TransportDiagnosticsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.events = os.path.join(self.tmp.name, "events.jsonl")
        self.patch = mock.patch.object(serve, "EVENTS", self.events)
        self.patch.start()
        self.addCleanup(self.patch.stop)
        serve._delivery_ids_by_log.clear()

    @staticmethod
    def event(delivery_id):
        return {
            "v": 0, "ts": "2026-08-24T12:00:00.000Z", "source": "test",
            "agent_id": "test:one", "project": "burrow", "cwd": "",
            "type": "idle", "payload": {}, "delivery_id": delivery_id,
        }

    def test_retried_delivery_is_appended_exactly_once_within_dedupe_window(self):
        event = self.event("a" * 32)
        self.assertTrue(serve.append_event(event))
        self.assertFalse(serve.append_event(event))
        with open(self.events, encoding="utf-8") as stream:
            self.assertEqual(sum(1 for line in stream if line.strip()), 1)
        self.assertGreaterEqual(serve.transport_status()["ingest"]["duplicates"], 1)

    def test_delivery_id_dedupe_survives_restart_and_live_log_retention(self):
        event = self.event("persistent-delivery-0001")
        self.assertTrue(serve.append_event(event))
        with open(self.events, "w", encoding="utf-8"):
            pass
        serve._delivery_ids_by_log.clear()
        self.assertFalse(serve.append_event(event))
        with open(self.events, encoding="utf-8") as stream:
            self.assertEqual(stream.read(), "")

    def test_event_log_repairs_crash_between_event_fsync_and_delivery_ledger(self):
        event = self.event("crash-window-delivery-0001")
        with mock.patch.object(serve, "_remember_durable",
                               side_effect=OSError("crash after event fsync")):
            with self.assertRaises(OSError):
                serve.append_event(event)
        serve._delivery_ids_by_log.clear()
        self.assertFalse(serve.append_event(event))
        with open(self.events, encoding="utf-8") as stream:
            self.assertEqual(sum(1 for line in stream if line.strip()), 1)

    def test_two_processes_append_each_delivery_id_exactly_once(self):
        delivery_ids = [f"multiprocess-delivery-{index:04d}" for index in range(20)]
        context = multiprocessing.get_context("fork")
        barrier = context.Barrier(2)
        processes = [context.Process(
            target=_append_same_deliveries,
            args=(self.events, delivery_ids, barrier),
        ) for _ in range(2)]

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
        with mock.patch.object(serve, "LEDGER_RECORDS", 4), \
                mock.patch.object(serve, "LEDGER_BYTES", 24):
            for index in range(10):
                serve._remember_durable("delivery-ids",
                                        serve._delivery_ids_by_log,
                                        f"key-{index}")
            serve._delivery_ids_by_log.clear()
            remembered = serve._load_ledger("delivery-ids",
                                            serve._delivery_ids_by_log)
        self.assertEqual(remembered, {"key-6", "key-7", "key-8", "key-9"})
        self.assertLessEqual(os.path.getsize(self.events + ".delivery-ids"), 24)

    def test_existing_ledger_key_refreshes_newest_retention(self):
        with mock.patch.object(serve, "LEDGER_RECORDS", 2), \
                mock.patch.object(serve, "LEDGER_BYTES", 4096):
            serve._remember_durable_batch("notified", {}, ("A", "B"))
            serve._remember_durable("notified", {}, "A")
            serve._remember_durable("notified", {}, "C")
        with open(self.events + ".notified", encoding="utf-8") as stream:
            self.assertEqual(stream.read().splitlines(), ["A", "C"])

    def test_multiprocess_batch_refresh_preserves_atomic_order(self):
        with mock.patch.object(serve, "LEDGER_RECORDS", 3), \
                mock.patch.object(serve, "LEDGER_BYTES", 4096):
            serve._remember_durable_batch("notified", {}, ("A", "B", "C"))
            context = multiprocessing.get_context("fork")
            gate = context.Barrier(2)
            processes = [
                context.Process(target=_remember_ledger_batch,
                                args=(self.events, keys, gate))
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
        events = [self.event(f"retained-delivery-{index:04d}")
                  for index in range(3)]
        with mock.patch.object(serve, "LEDGER_RECORDS", 2), \
                mock.patch.object(serve, "LEDGER_BYTES", 4096):
            for event in events:
                self.assertTrue(serve.append_event(event))
            serve._delivery_ids_by_log.clear()
            self.assertFalse(serve.append_event(events[0]))
        with open(self.events, encoding="utf-8") as stream:
            self.assertEqual(sum(1 for line in stream if line.strip()), 3)

    def test_multiprocess_ledger_compaction_loses_no_retained_writes(self):
        context = multiprocessing.get_context("fork")
        barrier = context.Barrier(2)
        first = [f"first-{index}" for index in range(20)]
        second = [f"second-{index}" for index in range(20)]
        processes = [
            context.Process(target=_remember_ledger_keys,
                            args=(self.events, "notified", first, barrier)),
            context.Process(target=_remember_ledger_keys,
                            args=(self.events, "notified", second, barrier)),
        ]
        with mock.patch.object(serve, "LEDGER_RECORDS", 64), \
                mock.patch.object(serve, "LEDGER_BYTES", 4096):
            for process in processes:
                process.start()
            for process in processes:
                process.join(10)
                self.assertEqual(process.exitcode, 0)
        serve._notified_by_log.clear()
        remembered = serve._load_ledger("notified", serve._notified_by_log)
        self.assertEqual(remembered, set(first + second))


if __name__ == "__main__":
    unittest.main()
