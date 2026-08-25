import http.client
import glob
import json
import multiprocessing
import os
import queue
import tempfile
import threading
import unittest
import socket
from unittest import mock

import serve


def _race_knock(events, observed, event, gate):
    serve.EVENTS = events
    serve.NOTIFY_URL = "process-safe-test"
    def observable(_event):
        with open(observed, "a", encoding="utf-8") as stream:
            serve.fcntl.flock(stream, serve.fcntl.LOCK_EX)
            stream.write("notify\n")
            stream.flush()
            os.fsync(stream.fileno())
        return True
    serve.notify = observable
    gate.wait()
    serve.deliver_knock(event)


def _terminalize_knock(events, event, kind, gate):
    serve.EVENTS = events
    gate.wait()
    serve._commit_knock_terminal(event, kind)


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

    def post_event(self, event, headers=None):
        body = json.dumps(event).encode()
        conn = http.client.HTTPConnection(*self.server.server_address)
        request_headers = {"Content-Type": "application/json"}
        request_headers.update(headers or {})
        conn.request("POST", "/events", body, request_headers)
        response = conn.getresponse()
        status, data = response.status, response.read()
        conn.close()
        return status, data

    def test_delivery_id_retry_is_exactly_once_at_the_http_seam(self):
        headers = {"X-Burrow-Delivery-ID": "response-lost-retry-0001"}
        self.assertEqual(self.post_event(self.valid_event(), headers)[0], 204)
        self.assertEqual(self.post_event(self.valid_event(), headers)[0], 204)
        status, _, body = self.get_events()
        self.assertEqual(status, 200)
        self.assertEqual(len(body.splitlines()), 1)

        conn = http.client.HTTPConnection(*self.server.server_address)
        conn.request("GET", "/transport/status")
        response = conn.getresponse()
        report = json.loads(response.read())
        conn.close()
        self.assertGreaterEqual(report["ingest"]["duplicates"], 1)

    def test_notification_work_is_not_acknowledged_until_durably_journaled(self):
        event = self.valid_event(type="needs_human", payload={"message": "help"})
        headers = {"X-Burrow-Delivery-ID": "durable-knock-journal-0001"}
        with mock.patch.object(serve, "NOTIFY_URL", "https://notify.invalid"), \
                mock.patch.object(serve, "persist_knock", return_value=False):
            self.assertEqual(self.post_event(event, headers)[0], 503)
        with mock.patch.object(serve, "NOTIFY_URL", "https://notify.invalid"), \
                mock.patch.object(serve, "persist_knock", return_value=True), \
                mock.patch.object(serve, "notify_async"):
            self.assertEqual(self.post_event(event, headers)[0], 204)

    def test_two_server_processes_notify_one_knock_exactly_once(self):
        event = self.valid_event(type="needs_human", payload={"message": "help"},
                                 delivery_id="multiprocess-knock")
        observed = os.path.join(self.tmp.name, "notifies")
        gate = multiprocessing.Barrier(2)
        processes = [multiprocessing.Process(target=_race_knock,
                                             args=(self.events, observed, event, gate))
                     for _ in range(2)]
        for process in processes:
            process.start()
        for process in processes:
            process.join(10)
            self.assertEqual(process.exitcode, 0)
        with open(observed, encoding="utf-8") as stream:
            self.assertEqual(stream.read().splitlines(), ["notify"])

    def test_knock_authority_is_bounded_and_capacity_drops_survive_restart(self):
        events = [self.valid_event(type="needs_human", payload={"message": str(i)},
                                   delivery_id="capacity-knock-%02d" % i)
                  for i in range(6)]
        with mock.patch.object(serve, "NOTIFY_URL", "unavailable"), \
                mock.patch.object(serve, "KNOCK_RECORDS", 2), \
                mock.patch.object(serve, "KNOCK_BYTES", 100000):
            for event in events:
                self.assertTrue(serve.persist_knock(event))
            path = self.events + ".knocks"
            authority = [path] + list(glob.glob(path + ".replay.*"))
            records = 0
            for candidate in authority:
                with open(candidate, encoding="utf-8") as stream:
                    records += sum(1 for line in stream if line.strip())
            self.assertLessEqual(records, 2)
            serve._dropped_by_log.clear()  # simulate restart
            self.assertFalse(serve.claim_knock(events[0]))

    def test_legacy_multiline_oversize_key_is_terminal_across_restart(self):
        event = self.valid_event(
            type="needs_human", delivery_id=None,
            agent_id="legacy\nagent\x00" + "a" * 400,
            payload={"message": "line one\nline two\x00" + "m" * 1000})
        with mock.patch.object(serve, "NOTIFY_URL", "unavailable"), \
                mock.patch.object(serve, "LEDGER_BYTES", 128):
            self.assertTrue(serve.persist_knock(event))
            key = serve.terminal_knock_key(event)
            serve._remember_durable("notify-dropped", serve._dropped_by_log, key)
            with open(self.events + ".notify-dropped", "rb") as stream:
                ledger = stream.read()
            self.assertNotIn(b"\x00", ledger)
            self.assertEqual(len(ledger.splitlines()), 1)
            self.assertTrue(ledger.decode("ascii").strip().startswith("burrow-sha256-"))
            serve._dropped_by_log.clear()
            self.assertFalse(serve.claim_knock(event))

    def test_ledger_paths_reject_unknown_domain_kinds(self):
        with self.assertRaisesRegex(ValueError, "invalid durable ledger kind"):
            serve._ledger_path("notify-lock-00")
        with self.assertRaisesRegex(ValueError, "invalid notification lock shard"):
            serve._notification_lock_path(serve.KNOCK_LOCK_SHARDS)

    def test_terminal_commit_failure_preserves_knock_capacity_victim(self):
        first = self.valid_event(type="needs_human", delivery_id=None,
                                 payload={"message": "first"})
        second = self.valid_event(type="needs_human", delivery_id=None,
                                  ts="2026-08-24T12:00:01.000Z",
                                  payload={"message": "second"})
        with mock.patch.object(serve, "NOTIFY_URL", "unavailable"), \
                mock.patch.object(serve, "KNOCK_RECORDS", 1), \
                mock.patch.object(serve, "KNOCK_BYTES", 100000), \
                mock.patch.object(serve, "LEDGER_BYTES", 1):
            self.assertTrue(serve.persist_knock(first))
            self.assertFalse(serve.persist_knock(second))
            with open(self.events + ".knocks", encoding="utf-8") as stream:
                retained = json.loads(next(stream))["event"]
            self.assertEqual(serve.knock_key(retained), serve.knock_key(first))

    def test_terminal_eviction_during_compaction_preserves_all_victim_authority(self):
        path = self.events + ".knocks"
        older = self.valid_event(type="needs_human", delivery_id="older-terminal")
        events = [self.valid_event(type="needs_human", delivery_id="victim-%d" % index,
                                   payload={"message": str(index)})
                  for index in range(3)]
        authority = ((path, events[0]), (path + ".replay.old", events[1]))
        for candidate, event in authority:
            with open(candidate, "w", encoding="utf-8") as stream:
                stream.write(json.dumps({"event": event, "attempts": 0}) + "\n")
        serve._remember_durable("notify-dropped", serve._dropped_by_log,
                                serve.terminal_knock_key(older))
        ledger_path = self.events + ".notify-dropped"
        with open(ledger_path, "rb") as stream:
            ledger_before = stream.read()
        with mock.patch.object(serve, "KNOCK_RECORDS", 1), \
                mock.patch.object(serve, "KNOCK_BYTES", 100000), \
                mock.patch.object(serve, "LEDGER_RECORDS", 1), \
                mock.patch.object(serve, "LEDGER_BYTES", 100000):
            with self.assertRaises(OSError):
                serve._compact_knocks_locked(
                    path, {"event": events[2], "attempts": 0})
        with open(ledger_path, "rb") as stream:
            self.assertEqual(stream.read(), ledger_before)
        for candidate, event in authority:
            with open(candidate, encoding="utf-8") as stream:
                self.assertEqual(json.loads(next(stream))["event"], event)

        serve._dropped_by_log.clear()
        serve._notifying.clear()
        with mock.patch.object(serve, "NOTIFY_URL", "unavailable"):
            self.assertFalse(serve.claim_knock(older))
            for _, event in authority:
                self.assertTrue(serve.claim_knock(event))
                serve.finish_knock(event, False)

    def test_compaction_does_not_retain_a_terminal_event_after_ledger_eviction(self):
        path = self.events + ".knocks"
        older = self.valid_event(type="needs_human", delivery_id="older-terminal")
        victims = [self.valid_event(type="needs_human", delivery_id="victim-%d" % index)
                   for index in range(2)]
        addition = self.valid_event(type="needs_human", delivery_id="new-work")
        for event in victims + [older]:
            with open(path, "a", encoding="utf-8") as stream:
                stream.write(json.dumps({"event": event, "attempts": 0}) + "\n")
        serve._remember_durable("notify-dropped", serve._dropped_by_log,
                                serve.terminal_knock_key(older))

        with mock.patch.object(serve, "KNOCK_RECORDS", 2), \
                mock.patch.object(serve, "KNOCK_BYTES", 100000), \
                mock.patch.object(serve, "LEDGER_RECORDS", 2), \
                mock.patch.object(serve, "LEDGER_BYTES", 100000):
            serve._compact_knocks_locked(
                path, {"event": addition, "attempts": 0})

        with open(path, encoding="utf-8") as stream:
            retained = [json.loads(line)["event"] for line in stream]
        self.assertEqual([event["delivery_id"] for event in retained],
                         ["victim-1", "new-work"])
        with open(self.events + ".notify-dropped", encoding="utf-8") as stream:
            terminal = set(stream.read().splitlines())
        self.assertIn(serve.terminal_knock_key(older), terminal)
        self.assertIn(serve.terminal_knock_key(victims[0]), terminal)

        serve._dropped_by_log.clear()
        serve._notifying.clear()
        with mock.patch.object(serve, "NOTIFY_URL", "unavailable"):
            self.assertFalse(serve.claim_knock(older))

    def test_failed_capacity_compaction_cannot_resurrect_terminal_source(self):
        path = self.events + ".knocks"
        terminal = self.valid_event(type="needs_human", delivery_id="terminal-a")
        pending = self.valid_event(type="needs_human", delivery_id="pending-b")
        replay_pending = self.valid_event(type="needs_human", delivery_id="pending-d")
        addition = self.valid_event(type="needs_human", delivery_id="addition-c")
        for event in (terminal, pending):
            with open(path, "a", encoding="utf-8") as stream:
                stream.write(json.dumps({"event": event, "attempts": 0}) + "\n")
        replay = path + ".replay.old"
        with open(replay, "w", encoding="utf-8") as stream:
            stream.write(json.dumps({"event": replay_pending, "attempts": 0}) + "\n")
        serve._remember_durable("notify-dropped", serve._dropped_by_log,
                                serve.terminal_knock_key(terminal))

        real_publish = serve._notification_store.publish_compaction

        def fail_final_compaction(candidate, lines):
            if [key for key, _ in lines] == [serve.knock_key(addition)]:
                raise OSError("injected final publication failure")
            return real_publish(candidate, lines)

        with mock.patch.object(serve, "KNOCK_RECORDS", 1), \
                mock.patch.object(serve, "KNOCK_BYTES", 100000), \
                mock.patch.object(serve, "LEDGER_RECORDS", 8), \
                mock.patch.object(serve, "LEDGER_BYTES", 100000), \
                mock.patch.object(serve._notification_store, "publish_compaction",
                                  side_effect=fail_final_compaction):
            with self.assertRaisesRegex(OSError, "injected final publication failure"):
                serve._compact_knocks_locked(
                    path, {"event": addition, "attempts": 0})

        on_disk = []
        for candidate in (path, replay):
            with open(candidate, encoding="utf-8") as stream:
                events = [json.loads(line)["event"] for line in stream]
            self.assertLessEqual(len(events), 1)
            on_disk.extend(events)
        terminal_keys = (serve._notification_store.load_ledger("notified") |
                         serve._notification_store.load_ledger("notify-dropped"))
        self.assertEqual(on_disk, [pending, replay_pending])
        self.assertNotIn(addition, on_disk)
        self.assertTrue(all(serve.terminal_knock_key(event) in terminal_keys
                            for event in on_disk))

        with mock.patch.object(serve, "KNOCK_RECORDS", 1), \
                mock.patch.object(serve, "KNOCK_BYTES", 100000), \
                mock.patch.object(serve, "LEDGER_RECORDS", 8), \
                mock.patch.object(serve, "LEDGER_BYTES", 100000):
            serve._compact_knocks_locked(path, {"event": addition, "attempts": 0})
        with open(path, encoding="utf-8") as stream:
            self.assertEqual([json.loads(line)["event"] for line in stream],
                             [addition])
        self.assertFalse(os.path.exists(replay))

    def test_mid_generation_prune_failure_aborts_before_addition_and_retries(self):
        path = self.events + ".knocks"
        terminals = [self.valid_event(type="needs_human", delivery_id="terminal-%d" % i)
                     for i in range(2)]
        addition = self.valid_event(type="needs_human", delivery_id="addition")
        generations = (path, path + ".replay.old")
        for candidate, event in zip(generations, terminals):
            with open(candidate, "w", encoding="utf-8") as stream:
                stream.write(json.dumps({"event": event, "attempts": 0}) + "\n")
        serve._notification_store.remember_batch(
            "notify-dropped", [serve.terminal_knock_key(event) for event in terminals])
        with open(self.events + ".notify-dropped", "rb") as stream:
            ledger_before = stream.read()
        real_publish = serve._notification_store.publish_generation_prune

        def fail_second_prune(journal, candidate, lines):
            if candidate == generations[1]:
                raise OSError("injected prune failure")
            return real_publish(journal, candidate, lines)

        with mock.patch.object(serve, "KNOCK_RECORDS", 1), \
                mock.patch.object(serve, "KNOCK_BYTES", 100000), \
                mock.patch.object(serve._notification_store,
                                  "publish_generation_prune",
                                  side_effect=fail_second_prune):
            with self.assertRaisesRegex(OSError, "injected prune failure"):
                serve._compact_knocks_locked(
                    path, {"event": addition, "attempts": 0})

        with open(self.events + ".notify-dropped", "rb") as stream:
            self.assertEqual(stream.read(), ledger_before)
        for candidate in generations:
            with open(candidate, encoding="utf-8") as stream:
                retained = [json.loads(line)["event"] for line in stream]
            self.assertLessEqual(len(retained), 1)
            self.assertNotIn(addition, retained)
            self.assertTrue(all(serve._notification_store.contains(
                "notify-dropped", serve.terminal_knock_key(event))
                                for event in retained))

        with mock.patch.object(serve, "KNOCK_RECORDS", 1), \
                mock.patch.object(serve, "KNOCK_BYTES", 100000):
            serve._compact_knocks_locked(path, {"event": addition, "attempts": 0})
        with open(path, encoding="utf-8") as stream:
            self.assertEqual([json.loads(line)["event"] for line in stream],
                             [addition])
        self.assertFalse(os.path.exists(generations[1]))

    def test_terminal_commits_retire_sources_before_later_ledger_eviction(self):
        events = [self.valid_event(type="needs_human", delivery_id=key)
                  for key in ("journal-a", "journal-b", "unrelated-x")]
        with mock.patch.object(serve, "NOTIFY_URL", "unavailable"), \
                mock.patch.object(serve, "KNOCK_RECORDS", 2), \
                mock.patch.object(serve, "LEDGER_RECORDS", 2), \
                mock.patch.object(serve, "KNOCK_BYTES", 100000), \
                mock.patch.object(serve, "LEDGER_BYTES", 100000):
            self.assertTrue(serve.persist_knock(events[0]))
            self.assertTrue(serve.persist_knock(events[1]))
            self.assertTrue(serve._commit_knock_terminal(events[0], "notified"))
            self.assertTrue(serve._commit_knock_terminal(events[2], "notified"))
            self.assertTrue(serve._commit_knock_terminal(events[1], "notified"))
        serve._notified_by_log.clear()
        serve._notifying.clear()
        self.assertFalse(serve.claim_knock(events[0]))
        self.assertFalse(serve.claim_knock(events[1]))
        self.assertNotIn(serve.knock_key(events[0]),
                         serve._read_knock_keys(self.events + ".knocks"))

    def test_terminal_commit_crash_copy_converges_without_losing_suppression(self):
        event = self.valid_event(type="needs_human", delivery_id="crash-terminal")
        with mock.patch.object(serve, "NOTIFY_URL", "unavailable"):
            self.assertTrue(serve.persist_knock(event))
            real_publish = serve._notification_store.publish_compaction
            publications = []

            def crash_after_ledger(path, lines):
                publications.append(1)
                if len(publications) == 2:
                    raise OSError("crash")
                return real_publish(path, lines)

            with mock.patch.object(serve._notification_store, "publish_compaction",
                                   side_effect=crash_after_ledger):
                self.assertTrue(serve._commit_knock_terminal(event, "notify-dropped"))
            self.assertFalse(serve.claim_knock(event))
            serve._notifying.clear()
            serve._recover_knocks()
        self.assertNotIn(serve.knock_key(event),
                         serve._read_knock_keys(self.events + ".knocks"))

    def test_concurrent_terminal_commits_are_counted_once_from_durable_ledgers(self):
        event = self.valid_event(type="needs_human", delivery_id="raced-terminal")
        self.assertTrue(serve.persist_knock(event))
        context = multiprocessing.get_context("fork")
        gate = context.Barrier(2)
        processes = [context.Process(target=_terminalize_knock,
                                     args=(self.events, event, "notify-dropped", gate))
                     for _ in range(2)]
        for process in processes:
            process.start()
        for process in processes:
            process.join(10)
            self.assertEqual(process.exitcode, 0)
        serve._dropped_by_log.clear()
        self.assertEqual(serve.transport_status()["notifications"]["dropped"], 1)

    def test_terminal_status_survives_process_counter_reset(self):
        delivered = self.valid_event(type="needs_human", delivery_id="status-delivered")
        dropped = self.valid_event(type="needs_human", delivery_id="status-dropped")
        self.assertTrue(serve._commit_knock_terminal(delivered, "notified"))
        self.assertTrue(serve._commit_knock_terminal(dropped, "notify-dropped"))
        serve._notified_by_log.clear()
        serve._dropped_by_log.clear()
        with mock.patch.dict(serve._transport_counters,
                             {"notify_delivered": 0, "notify_dropped": 0}):
            status = serve.transport_status()["notifications"]
        self.assertEqual(status["delivered"], 1)
        self.assertEqual(status["dropped"], 1)

    def test_knock_disjoint_active_replay_pending_has_finite_physical_ceiling(self):
        path = self.events + ".knocks"
        events = [self.valid_event(type="needs_human", delivery_id="cap-%d" % index,
                                   payload={"message": str(index)})
                  for index in range(4)]
        with mock.patch.object(serve, "KNOCK_RECORDS", 2), \
                mock.patch.object(serve, "KNOCK_BYTES", 100000):
            for candidate, subset in ((path, events[:2]),
                                      (path + ".replay.old", events[2:])):
                with open(candidate, "w", encoding="utf-8") as stream:
                    for event in subset:
                        stream.write(json.dumps({"event": event, "attempts": 0}) + "\n")
            observed = []
            real_replace = os.replace
            def line_count(candidate):
                with open(candidate, encoding="utf-8") as stream:
                    return sum(1 for _ in stream)
            def inspect_pending(source, destination):
                candidates = [path, path + ".pending", path + ".replay.old"]
                existing = [item for item in candidates if os.path.exists(item)]
                observed.append((sum(os.path.getsize(item) for item in existing),
                                 sum(line_count(item) for item in existing)))
                real_replace(source, destination)
            with mock.patch.object(serve.os, "replace", side_effect=inspect_pending):
                with open(path + ".lock", "a+"):
                    serve._compact_knocks_locked(path)
            self.assertEqual(max(count for _, count in observed),
                             3 * serve.KNOCK_RECORDS)
            self.assertLessEqual(max(size for size, _ in observed),
                                 3 * serve.KNOCK_BYTES)

    def test_terminal_commit_retries_across_restarts_without_another_post(self):
        event = self.valid_event(type="needs_human", delivery_id="terminal-recovery")
        work = queue.Queue(maxsize=8)
        posts = []
        dropped_before = serve._transport_counters["notify_dropped"]
        real_remember = serve._notification_store.remember

        def fail_drop(kind, key, cache=None):
            if kind == "notify-dropped":
                raise OSError("ledger unavailable")
            return real_remember(kind, key, cache)

        with mock.patch.object(serve, "NOTIFY_URL", "unavailable"), \
                mock.patch.object(serve, "_knock_queue", work), \
                mock.patch.object(serve, "_recover_knocks"), \
                mock.patch.object(serve, "notify",
                                  side_effect=lambda _event: posts.append(1) or False), \
                mock.patch.object(serve._notification_store, "remember",
                                  side_effect=fail_drop):
            self.assertTrue(serve.persist_knock(event))
            self.assertTrue(serve.claim_knock(event))
            serve._process_knock(event)
            serve._process_knock(work.get_nowait())
            serve._process_knock(work.get_nowait())
            self.assertEqual(len(posts), 3)
            self.assertEqual(serve._transport_counters["notify_dropped"],
                             dropped_before)

        # Simulate two fresh processes by recovering the durable attempts each time.
        for _ in range(2):
            serve._knock_attempts.clear()
            serve._notifying.clear()
            with mock.patch.object(serve, "NOTIFY_URL", "unavailable"), \
                    mock.patch.object(serve, "_knock_queue", work), \
                    mock.patch.object(
                        serve, "_recover_knocks",
                        wraps=serve._recover_knocks) as recover, \
                    mock.patch.object(serve, "notify",
                                      side_effect=lambda _event: posts.append(1) or False), \
                    mock.patch.object(serve._notification_store, "remember",
                                      side_effect=fail_drop):
                recover()
                serve._process_knock(work.get_nowait())
            self.assertEqual(len(posts), 3)
            self.assertEqual(serve._transport_counters["notify_dropped"],
                             dropped_before)

        serve._knock_attempts.clear()
        serve._notifying.clear()
        with mock.patch.object(serve, "NOTIFY_URL", "unavailable"), \
                mock.patch.object(serve, "_knock_queue", work), \
                mock.patch.object(serve, "notify",
                                  side_effect=lambda _event: posts.append(1) or False):
            serve._recover_knocks()
            serve._process_knock(work.get_nowait())
            serve._recover_knocks()
        self.assertEqual(len(posts), 3)
        self.assertEqual(serve.transport_status()["notifications"]["dropped"], 1)
        self.assertFalse(glob.glob(self.events + ".knocks.replay.*"))

    def test_retry_queue_saturation_is_reported_and_work_remains_durable(self):
        event = self.valid_event(type="needs_human", delivery_id="retry-saturated")
        work = queue.Queue(maxsize=1)
        work.put_nowait(object())
        saturated_before = serve._transport_counters["notify_saturated"]

        def failed_delivery(failed_event):
            serve.finish_knock(failed_event, False)
            return False

        with mock.patch.object(serve, "NOTIFY_URL", "unavailable"), \
                mock.patch.object(serve, "_knock_queue", work), \
                mock.patch.object(serve, "deliver_knock", side_effect=failed_delivery), \
                mock.patch.object(serve, "_recover_knocks"):
            self.assertTrue(serve.persist_knock(event))
            self.assertTrue(serve.claim_knock(event))
            serve._process_knock(event)
        self.assertEqual(serve._transport_counters["notify_saturated"],
                         saturated_before + 1)
        self.assertNotIn(serve.terminal_knock_key(event), serve._notifying)
        self.assertIn(serve.knock_key(event),
                      serve._read_knock_keys(self.events + ".knocks"))

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
