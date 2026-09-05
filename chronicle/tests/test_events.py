from pathlib import Path
from config import Config
import glob
import dataclasses
import functools
import http.client
import json
import multiprocessing
import os
import queue
import tempfile
import unittest
import socket
from unittest import mock

import notification_persistence as knocks
import serve
from tests.http_test_support import RunningServer


def _race_knock(events, observed, event, gate):
    runtime = serve.Runtime(Config(events=Path(events), notify_url="process-safe-test"))

    def observable(_event, _runtime):
        with open(observed, "a", encoding="utf-8") as stream:
            serve.fcntl.flock(stream, serve.fcntl.LOCK_EX)
            stream.write("notify\n")
            stream.flush()
            os.fsync(stream.fileno())
        return True

    serve.notify = observable
    gate.wait()
    serve.deliver_knock(event, runtime)


def _terminalize_knock(events, event, kind, gate):
    runtime = serve.Runtime(Config(events=Path(events)))
    gate.wait()
    runtime.notification_store.commit_terminal(event, kind)


class EventsEndpointTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.events = os.path.join(self.tmp.name, "events.jsonl")
        self.config = Config(events=Path(self.events))
        self.runtime = None
        self.reset_notification_runtime(notify_url="unavailable")
        self.running_server = RunningServer(serve.create_app(self.config))
        self.server = self.running_server.server

    def reset_notification_runtime(self, **changes):
        if self.runtime is not None:
            serve.stop_knock_workers(self.runtime)
        config = dataclasses.replace(self.runtime.config if self.runtime else self.config, **changes)
        self.runtime = serve.Runtime(config)
        for name in (
            "claim_knock",
            "deliver_knock",
            "finish_knock",
            "persist_knock",
            "transport_status",
            "_process_knock",
            "_recover_knocks",
        ):
            setattr(
                self,
                name,
                functools.partial(getattr(serve, name), runtime=self.runtime),
            )

    def tearDown(self):
        self.running_server.stop()
        serve.stop_knock_workers(self.runtime)
        self.tmp.cleanup()

    def get_events(self, since=None):
        conn = http.client.HTTPConnection(*self.server.server_address)
        path = "/events" if since is None else f"/events?since={since}"
        conn.request("GET", path)
        response = conn.getresponse()
        body = response.read()
        headers = {name.title(): value for name, value in response.getheaders()}
        conn.close()
        return response.status, headers, body

    def append(self, *events):
        with open(self.events, "ab") as stream:
            for event in events:
                stream.write(json.dumps(event).encode() + b"\n")

    def restart_server(self):
        self.running_server.restart()
        self.server = self.running_server.server

    @staticmethod
    def valid_event(**changes):
        event = {
            "v": 0,
            "ts": "2026-08-24T12:00:00.000Z",
            "source": "test",
            "agent_id": "test:one",
            "project": "burrow",
            "cwd": "/tmp",
            "type": "idle",
            "payload": {},
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
        self.reset_notification_runtime(notify_url='https://notify.invalid')
        event = self.valid_event(type="needs_human", payload={"message": "help"})
        headers = {"X-Burrow-Delivery-ID": "durable-knock-journal-0001"}
        with mock.patch.object(serve, 'persist_knock', return_value=False):
            self.assertEqual(self.post_event(event, headers)[0], 503)
        with (
            mock.patch.object(serve, 'persist_knock', return_value=True),
            mock.patch.object(serve, 'notify_async'),
        ):
            self.assertEqual(self.post_event(event, headers)[0], 204)

    def test_two_server_processes_notify_one_knock_exactly_once(self):
        event = self.valid_event(
            type="needs_human",
            payload={"message": "help"},
            delivery_id="multiprocess-knock",
        )
        observed = os.path.join(self.tmp.name, "notifies")
        gate = multiprocessing.Barrier(2)
        processes = [
            multiprocessing.Process(
                target=_race_knock, args=(self.events, observed, event, gate)
            )
            for _ in range(2)
        ]
        for process in processes:
            process.start()
        for process in processes:
            process.join(10)
            self.assertEqual(process.exitcode, 0)
        with open(observed, encoding="utf-8") as stream:
            self.assertEqual(stream.read().splitlines(), ["notify"])

    def test_knock_authority_is_bounded_and_capacity_drops_survive_restart(self):
        self.reset_notification_runtime(
            notify_url="unavailable", knock_records=2, knock_bytes=100000
        )
        events = [
            self.valid_event(
                type="needs_human",
                payload={"message": str(i)},
                delivery_id="capacity-knock-%02d" % i,
            )
            for i in range(6)
        ]
        for event in events:
            self.assertTrue(self.persist_knock(event))
        path = self.events + ".knocks"
        authority = [path] + list(glob.glob(path + ".replay.*"))
        records = 0
        for candidate in authority:
            with open(candidate, encoding="utf-8") as stream:
                records += sum(1 for line in stream if line.strip())
        self.assertLessEqual(records, 2)
        self.assertFalse(self.claim_knock(events[0]))

    def test_legacy_multiline_oversize_key_is_terminal_across_restart(self):
        self.reset_notification_runtime(notify_url='unavailable', ledger_bytes=128)
        event = self.valid_event(
            type="needs_human",
            delivery_id=None,
            agent_id="legacy\nagent\x00" + "a" * 400,
            payload={"message": "line one\nline two\x00" + "m" * 1000},
        )
        self.assertTrue(self.persist_knock(event))
        key = knocks.terminal_key(event)
        self.runtime.notification_store.remember("notify-dropped", key)
        with open(self.events + ".notify-dropped", "rb") as stream:
            ledger = stream.read()
        self.assertNotIn(b"\x00", ledger)
        self.assertEqual(len(ledger.splitlines()), 1)
        self.assertTrue(ledger.decode("ascii").strip().startswith("burrow-sha256-"))
        self.assertFalse(self.claim_knock(event))

    def test_ledger_paths_reject_unknown_domain_kinds(self):
        with self.assertRaisesRegex(ValueError, "invalid durable ledger kind"):
            self.runtime.notification_store.ledger_path("notify-lock-00")
        with self.assertRaisesRegex(ValueError, "invalid notification lock shard"):
            self.runtime.notification_store.notification_lock_path(self.runtime.config.knock_lock_shards)

    def test_terminal_commit_failure_preserves_knock_capacity_victim(self):
        self.reset_notification_runtime(
            notify_url="unavailable",
            knock_records=1,
            knock_bytes=100000,
            ledger_bytes=1,
        )
        first = self.valid_event(
            type="needs_human", delivery_id=None, payload={"message": "first"}
        )
        second = self.valid_event(
            type="needs_human",
            delivery_id=None,
            ts="2026-08-24T12:00:01.000Z",
            payload={"message": "second"},
        )
        self.assertTrue(self.persist_knock(first))
        self.assertFalse(self.persist_knock(second))
        with open(self.events + ".knocks", encoding="utf-8") as stream:
            retained = json.loads(next(stream))["event"]
        self.assertEqual(knocks.knock_key(retained), knocks.knock_key(first))

    def test_terminal_eviction_during_compaction_preserves_all_victim_authority(self):
        self.reset_notification_runtime(
            knock_records=1,
            knock_bytes=100000,
            ledger_records=1,
            ledger_bytes=100000,
            notify_url='unavailable',
        )
        path = self.events + ".knocks"
        older = self.valid_event(type="needs_human", delivery_id="older-terminal")
        events = [
            self.valid_event(
                type="needs_human",
                delivery_id="victim-%d" % index,
                payload={"message": str(index)},
            )
            for index in range(3)
        ]
        authority = ((path, events[0]), (path + ".replay.old", events[1]))
        for candidate, event in authority:
            with open(candidate, "w", encoding="utf-8") as stream:
                stream.write(json.dumps({"event": event, "attempts": 0}) + "\n")
        self.runtime.notification_store.remember("notify-dropped", knocks.terminal_key(older))
        ledger_path = self.events + ".notify-dropped"
        with open(ledger_path, "rb") as stream:
            ledger_before = stream.read()
        with self.assertRaises(OSError):
            self.runtime.notification_store.compact_locked(path, {"event": events[2], "attempts": 0})
        with open(ledger_path, "rb") as stream:
            self.assertEqual(stream.read(), ledger_before)
        for candidate, event in authority:
            with open(candidate, encoding="utf-8") as stream:
                self.assertEqual(json.loads(next(stream))["event"], event)

        self.runtime.notifying.clear()
        self.assertFalse(self.claim_knock(older))
        for _, event in authority:
            self.assertTrue(self.claim_knock(event))
            self.finish_knock(event, False)

    def test_compaction_does_not_retain_a_terminal_event_after_ledger_eviction(self):
        self.reset_notification_runtime(
            knock_records=2,
            knock_bytes=100000,
            ledger_records=2,
            ledger_bytes=100000,
            notify_url='unavailable',
        )
        path = self.events + ".knocks"
        older = self.valid_event(type="needs_human", delivery_id="older-terminal")
        victims = [
            self.valid_event(type="needs_human", delivery_id="victim-%d" % index)
            for index in range(2)
        ]
        addition = self.valid_event(type="needs_human", delivery_id="new-work")
        for event in victims + [older]:
            with open(path, "a", encoding="utf-8") as stream:
                stream.write(json.dumps({"event": event, "attempts": 0}) + "\n")
        self.runtime.notification_store.remember("notify-dropped", knocks.terminal_key(older))

        self.runtime.notification_store.compact_locked(path, {"event": addition, "attempts": 0})

        with open(path, encoding="utf-8") as stream:
            retained = [json.loads(line)["event"] for line in stream]
        self.assertEqual(
            [event["delivery_id"] for event in retained], ["victim-1", "new-work"]
        )
        with open(self.events + ".notify-dropped", encoding="utf-8") as stream:
            terminal = set(stream.read().splitlines())
        self.assertIn(knocks.terminal_key(older), terminal)
        self.assertIn(knocks.terminal_key(victims[0]), terminal)

        self.runtime.notifying.clear()
        self.assertFalse(self.claim_knock(older))

    def test_failed_capacity_compaction_cannot_resurrect_terminal_source(self):
        self.reset_notification_runtime(
            knock_records=1,
            knock_bytes=100000,
            ledger_records=8,
            ledger_bytes=100000,
        )
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
        self.runtime.notification_store.remember("notify-dropped", knocks.terminal_key(terminal))

        real_publish = self.runtime.notification_store.publish_compaction

        def fail_final_compaction(candidate, lines):
            if [key for key, _ in lines] == [knocks.knock_key(addition)]:
                raise OSError("injected final publication failure")
            return real_publish(candidate, lines)

        with (
            mock.patch.object(self.runtime.notification_store, 'publish_compaction', side_effect=fail_final_compaction),
        ):
            with self.assertRaisesRegex(OSError, "injected final publication failure"):
                self.runtime.notification_store.compact_locked(path, {"event": addition, "attempts": 0})

        on_disk = []
        for candidate in (path, replay):
            with open(candidate, encoding="utf-8") as stream:
                events = [json.loads(line)["event"] for line in stream]
            self.assertLessEqual(len(events), 1)
            on_disk.extend(events)
        terminal_keys = self.runtime.notification_store.load_ledger(
            "notified"
        ) | self.runtime.notification_store.load_ledger("notify-dropped")
        self.assertEqual(on_disk, [pending, replay_pending])
        self.assertNotIn(addition, on_disk)
        self.assertTrue(
            all(knocks.terminal_key(event) in terminal_keys for event in on_disk)
        )

        self.runtime.notification_store.compact_locked(path, {"event": addition, "attempts": 0})
        with open(path, encoding="utf-8") as stream:
            self.assertEqual([json.loads(line)["event"] for line in stream], [addition])
        self.assertFalse(os.path.exists(replay))

    def test_mid_generation_prune_failure_aborts_before_addition_and_retries(self):
        self.reset_notification_runtime(knock_records=1, knock_bytes=100000)
        path = self.events + ".knocks"
        terminals = [
            self.valid_event(type="needs_human", delivery_id="terminal-%d" % i)
            for i in range(2)
        ]
        addition = self.valid_event(type="needs_human", delivery_id="addition")
        generations = (path, path + ".replay.old")
        for candidate, event in zip(generations, terminals):
            with open(candidate, "w", encoding="utf-8") as stream:
                stream.write(json.dumps({"event": event, "attempts": 0}) + "\n")
        self.runtime.notification_store.remember_batch(
            "notify-dropped", [knocks.terminal_key(event) for event in terminals]
        )
        with open(self.events + ".notify-dropped", "rb") as stream:
            ledger_before = stream.read()
        real_publish = self.runtime.notification_store.publish_generation_prune

        def fail_second_prune(journal, candidate, lines):
            if candidate == generations[1]:
                raise OSError("injected prune failure")
            return real_publish(journal, candidate, lines)

        with (
            mock.patch.object(self.runtime.notification_store, 'publish_generation_prune', side_effect=fail_second_prune),
        ):
            with self.assertRaisesRegex(OSError, "injected prune failure"):
                self.runtime.notification_store.compact_locked(path, {"event": addition, "attempts": 0})

        with open(self.events + ".notify-dropped", "rb") as stream:
            self.assertEqual(stream.read(), ledger_before)
        for candidate in generations:
            with open(candidate, encoding="utf-8") as stream:
                retained = [json.loads(line)["event"] for line in stream]
            self.assertLessEqual(len(retained), 1)
            self.assertNotIn(addition, retained)
            self.assertTrue(
                all(
                    self.runtime.notification_store.contains(
                        "notify-dropped", knocks.terminal_key(event)
                    )
                    for event in retained
                )
            )

        self.runtime.notification_store.compact_locked(path, {"event": addition, "attempts": 0})
        with open(path, encoding="utf-8") as stream:
            self.assertEqual([json.loads(line)["event"] for line in stream], [addition])
        self.assertFalse(os.path.exists(generations[1]))

    def test_terminal_commits_retire_sources_before_later_ledger_eviction(self):
        self.reset_notification_runtime(
            notify_url='unavailable',
            knock_records=2,
            ledger_records=2,
            knock_bytes=100000,
            ledger_bytes=100000,
        )
        events = [
            self.valid_event(type="needs_human", delivery_id=key)
            for key in ("journal-a", "journal-b", "unrelated-x")
        ]
        self.assertTrue(self.persist_knock(events[0]))
        self.assertTrue(self.persist_knock(events[1]))
        self.assertTrue(
            self.runtime.notification_store.commit_terminal(events[0], "notified")
        )
        self.assertTrue(
            self.runtime.notification_store.commit_terminal(events[2], "notified")
        )
        self.assertTrue(
            self.runtime.notification_store.commit_terminal(events[1], "notified")
        )
        self.runtime.notifying.clear()
        self.assertNotIn(
            knocks.terminal_key(events[0]),
            self.runtime.notification_store.load_ledger("notified"),
        )
        # The first terminal key has left the bounded ledger; its retired
        # journal source must not recreate delivery work during recovery.
        self._recover_knocks()
        self.assertTrue(self.runtime.knock_queue.empty())
        self.assertFalse(self.claim_knock(events[1]))
        self.assertNotIn(
            knocks.knock_key(events[0]),
            self.runtime.notification_store.read_journal_keys(self.events + ".knocks"),
        )

    def test_terminal_commit_crash_copy_converges_without_losing_suppression(self):
        self.reset_notification_runtime(notify_url='unavailable')
        event = self.valid_event(type="needs_human", delivery_id="crash-terminal")
        self.assertTrue(self.persist_knock(event))
        real_publish = self.runtime.notification_store.publish_compaction
        publications = []
        def crash_after_ledger(path, lines):
            publications.append(1)
            if len(publications) == 2:
                raise OSError("crash")
            return real_publish(path, lines)
        with mock.patch.object(
            self.runtime.notification_store,
            "publish_compaction",
            side_effect=crash_after_ledger,
        ):
            self.assertTrue(self.runtime.notification_store.commit_terminal(event, "notify-dropped"))
        self.assertFalse(self.claim_knock(event))
        self.runtime.notifying.clear()
        self._recover_knocks()
        self.assertNotIn(
            knocks.knock_key(event),
            self.runtime.notification_store.read_journal_keys(self.events + ".knocks"),
        )

    def test_concurrent_terminal_commits_are_counted_once_from_durable_ledgers(self):
        event = self.valid_event(type="needs_human", delivery_id="raced-terminal")
        self.assertTrue(self.persist_knock(event))
        context = multiprocessing.get_context("fork")
        gate = context.Barrier(2)
        processes = [
            context.Process(
                target=_terminalize_knock,
                args=(self.events, event, "notify-dropped", gate),
            )
            for _ in range(2)
        ]
        for process in processes:
            process.start()
        for process in processes:
            process.join(10)
            self.assertEqual(process.exitcode, 0)
        self.assertEqual(self.transport_status()["notifications"]["dropped"], 1)

    def test_terminal_status_survives_process_counter_reset(self):
        delivered = self.valid_event(type="needs_human", delivery_id="status-delivered")
        dropped = self.valid_event(type="needs_human", delivery_id="status-dropped")
        self.assertTrue(self.runtime.notification_store.commit_terminal(delivered, "notified"))
        self.assertTrue(self.runtime.notification_store.commit_terminal(dropped, "notify-dropped"))
        with mock.patch.dict(
            self.runtime.transport_counters, {"notify_delivered": 0, "notify_dropped": 0}
        ):
            status = self.transport_status()["notifications"]
        self.assertEqual(status["delivered"], 1)
        self.assertEqual(status["dropped"], 1)

    def test_knock_disjoint_active_replay_pending_has_finite_physical_ceiling(self):
        self.reset_notification_runtime(knock_records=2, knock_bytes=100000)
        path = self.events + ".knocks"
        events = [
            self.valid_event(
                type="needs_human",
                delivery_id="cap-%d" % index,
                payload={"message": str(index)},
            )
            for index in range(4)
        ]
        for candidate, subset in (
            (path, events[:2]),
            (path + ".replay.old", events[2:]),
        ):
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
            observed.append(
                (
                    sum(os.path.getsize(item) for item in existing),
                    sum(line_count(item) for item in existing),
                )
            )
            real_replace(source, destination)
        with mock.patch.object(serve.os, "replace", side_effect=inspect_pending):
            with open(path + ".lock", "a+"):
                self.runtime.notification_store.compact_locked(path)
        self.assertEqual(
            max(count for _, count in observed), 3 * self.runtime.config.knock_records
        )
        self.assertLessEqual(
            max(size for size, _ in observed), 3 * self.runtime.config.knock_bytes
        )

    def test_terminal_commit_retries_across_restarts_without_another_post(self):
        self.reset_notification_runtime(notify_url='unavailable')
        event = self.valid_event(type="needs_human", delivery_id="terminal-recovery")
        work = queue.Queue(maxsize=8)
        posts = []
        dropped_before = self.runtime.transport_counters["notify_dropped"]
        real_remember = self.runtime.notification_store.remember

        def fail_drop(kind, key):
            if kind == "notify-dropped":
                raise OSError("ledger unavailable")
            return real_remember(kind, key)

        with (
            mock.patch.object(self.runtime, 'knock_queue', work),
            mock.patch.object(serve, '_recover_knocks'),
            mock.patch.object(serve, 'notify', side_effect=lambda _event, _runtime: posts.append(1) or False),
            mock.patch.object(self.runtime.notification_store, 'remember', side_effect=fail_drop),
        ):
            self.assertTrue(self.persist_knock(event))
            self.assertTrue(self.claim_knock(event))
            self._process_knock(event)
            self._process_knock(work.get_nowait())
            self._process_knock(work.get_nowait())
            self.assertEqual(len(posts), 3)
            self.assertEqual(
                self.runtime.transport_counters["notify_dropped"], dropped_before
            )

        # Simulate two fresh processes by recovering the durable attempts each time.
        for _ in range(2):
            self.runtime.notification_store.clear_attempts(event)
            self.runtime.notifying.clear()
            with (
                mock.patch.object(self.runtime, 'knock_queue', work),
                mock.patch.object(serve, '_recover_knocks', wraps=self._recover_knocks) as recover,
                mock.patch.object(serve, 'notify', side_effect=lambda _event, _runtime: posts.append(1) or False),
                mock.patch.object(self.runtime.notification_store, 'remember', side_effect=fail_drop),
            ):
                recover()
                self._process_knock(work.get_nowait())
            self.assertEqual(len(posts), 3)
            self.assertEqual(
                self.runtime.transport_counters["notify_dropped"], dropped_before
            )

        self.runtime.notification_store.clear_attempts(event)
        self.runtime.notifying.clear()
        with (
            mock.patch.object(self.runtime, 'knock_queue', work),
            mock.patch.object(serve, 'notify', side_effect=lambda _event, _runtime: posts.append(1) or False),
        ):
            self._recover_knocks()
            self._process_knock(work.get_nowait())
            self._recover_knocks()
        self.assertEqual(len(posts), 3)
        self.assertEqual(self.transport_status()["notifications"]["dropped"], 1)
        self.assertFalse(glob.glob(self.events + ".knocks.replay.*"))

    def test_retry_queue_saturation_is_reported_and_work_remains_durable(self):
        self.reset_notification_runtime(notify_url='unavailable')
        event = self.valid_event(type="needs_human", delivery_id="retry-saturated")
        work = queue.Queue(maxsize=1)
        work.put_nowait(object())
        saturated_before = self.runtime.transport_counters["notify_saturated"]

        def failed_delivery(failed_event, _runtime):
            self.finish_knock(failed_event, False)
            return False

        with (
            mock.patch.object(self.runtime, 'knock_queue', work),
            mock.patch.object(serve, 'deliver_knock', side_effect=failed_delivery),
            mock.patch.object(serve, '_recover_knocks'),
        ):
            self.assertTrue(self.persist_knock(event))
            self.assertTrue(self.claim_knock(event))
            self._process_knock(event)
        self.assertEqual(
            self.runtime.transport_counters["notify_saturated"], saturated_before + 1
        )
        self.assertNotIn(knocks.terminal_key(event), self.runtime.notifying)
        self.assertIn(
            knocks.knock_key(event),
            self.runtime.notification_store.read_journal_keys(self.events + ".knocks"),
        )

    def test_ingest_rejects_the_shared_protocol_contract_without_appending(self):
        fixtures = os.path.join(
            os.path.dirname(__file__), "fixtures", "protocol-v0-validation.json"
        )
        with open(fixtures, encoding="utf-8") as stream:
            cases = json.load(stream)
        for case in cases:
            with self.subTest(case["name"]):
                before = (
                    os.path.getsize(self.events) if os.path.exists(self.events) else 0
                )
                status, _ = self.post_event(case["event"])
                self.assertEqual(status, 204 if case["valid"] else 400)
                after = (
                    os.path.getsize(self.events) if os.path.exists(self.events) else 0
                )
                self.assertEqual(after > before, case["valid"])

    def test_ingest_rejects_non_standard_json_constants_without_appending(self):
        template = json.dumps(
            self.valid_event(payload={"unknown": "constant"}),
            separators=(",", ":"),
        ).encode()
        for constant in (b"NaN", b"Infinity", b"-Infinity"):
            with self.subTest(constant=constant):
                body = template.replace(b'"constant"', constant)
                raw = (
                    b"POST /events HTTP/1.1\r\nHost: x\r\n"
                    b"Content-Type: application/json\r\n"
                    b"Content-Length: "
                    + str(len(body)).encode()
                    + b"\r\n\r\n"
                    + body
                    + b"GET /events HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n"
                )
                with socket.create_connection(
                    self.server.server_address, timeout=2
                ) as conn:
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
        raw = (
            b"POST /events HTTP/1.1\r\nHost: x\r\nContent-Length: nope\r\n\r\n"
            b"GET /events HTTP/1.1\r\nHost: x\r\n\r\n"
        )
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
            self.assertEqual(response.count(b"HTTP/1.1"), 1)

        # The server stays healthy and a fresh keep-alive connection is aligned.
        self.assertEqual(self.post_event(self.valid_event())[0], 204)

    def test_transfer_encoding_is_rejected_and_closes_before_pipeline(self):
        body = json.dumps(self.valid_event()).encode()
        raw = (
            b"POST /events HTTP/1.1\r\nHost: x\r\n"
            b"Content-Length: " + str(len(body)).encode() + b"\r\n"
            b"Transfer-Encoding: chunked\r\n\r\n"
            + body
            + b"GET /events HTTP/1.1\r\nHost: x\r\n\r\n"
        )
        with socket.create_connection(self.server.server_address, timeout=2) as conn:
            conn.sendall(raw)
            response = b""
            while True:
                chunk = conn.recv(4096)
                if not chunk:
                    break
                response += chunk
        self.assertIn(b"HTTP/1.1 400", response)
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

    def test_two_server_instances_have_distinct_boot_identities(self):
        other = serve.Runtime(serve.Config())
        self.assertRegex(self.server.boot_id, r"^[0-9a-f]{32}$")
        self.assertNotEqual(self.server.boot_id, other.boot_id)

    def test_pre_restart_cursor_resets_and_replays_changed_live_log(self):
        old = {"type": "idle", "agent_id": "old-content"}
        replacement = {"type": "idle", "agent_id": "new-content-is-longer"}
        self.append(old)
        _, headers, _ = self.get_events()
        old_cursor = headers["X-Burrow-Cursor"]

        # A fresh process gets a new boot identity while the path, inode, and
        # process-local generation can all be unchanged.
        with open(self.events, "wb") as stream:
            stream.write(json.dumps(replacement).encode() + b"\n")
        old_boot_id = self.server.boot_id
        self.restart_server()
        _, headers, body = self.get_events(old_cursor)

        self.assertEqual(headers["X-Burrow-Reset"], "1")
        self.assertEqual(
            [replacement], [json.loads(line) for line in body.splitlines()]
        )
        self.assertNotEqual(self.server.boot_id, old_boot_id)
        self.assertTrue(
            headers["X-Burrow-Cursor"].startswith("v1:" + self.server.boot_id + ":")
        )

    def test_positive_numeric_cursor_resets_instead_of_reading_mid_record(self):
        event = {"type": "idle", "agent_id": "complete-replay-is-long-enough"}
        self.append(event)
        _, headers, body = self.get_events(7)
        self.assertEqual(headers["X-Burrow-Reset"], "1")
        self.assertEqual([event], [json.loads(line) for line in body.splitlines()])

    def test_explicit_numeric_zero_resets_but_absent_initial_cursor_does_not(self):
        event = {"type": "idle", "agent_id": "initial"}
        self.append(event)
        _, initial_headers, initial_body = self.get_events(None)
        _, explicit_headers, explicit_body = self.get_events(0)
        self.assertNotIn("X-Burrow-Reset", initial_headers)
        self.assertEqual(initial_body, explicit_body)
        self.assertEqual(explicit_headers["X-Burrow-Reset"], "1")

    def test_legacy_structured_cursors_reset_truthfully(self):
        event = {"type": "idle", "agent_id": "legacy-replay"}
        self.append(event)
        stat = os.stat(self.events)
        for cursor in (
            f"{stat.st_dev}:{stat.st_ino}:1",
            f"{stat.st_dev}:{stat.st_ino}:0:1",
        ):
            with self.subTest(cursor=cursor):
                _, headers, body = self.get_events(cursor)
                self.assertEqual(headers["X-Burrow-Reset"], "1")
                self.assertEqual(
                    [event], [json.loads(line) for line in body.splitlines()]
                )

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
        self.assertEqual(
            [replacement], [json.loads(line) for line in body.splitlines()]
        )

    def test_in_place_rotation_resets_a_cursor_still_within_the_new_log(self):
        ts = "2099-01-01T00:00:00.000Z"
        self.append(
            {
                "type": "idle",
                "agent_id": "one",
                "ts": ts,
                "v": 0,
                "source": "test",
                "project": "burrow",
                "payload": {},
            }
        )
        _, headers, _ = self.get_events()
        old_cursor = headers["X-Burrow-Cursor"]

        for index in range(100):
            self.append(
                {
                    "type": "tool_called",
                    "agent_id": "one",
                    "ts": ts,
                    "v": 0,
                    "source": "test",
                    "project": "burrow",
                    "payload": {"tool": "Read", "detail": str(index)},
                }
            )
        archive = self.server.event_log.rotate()
        self.assertIsNotNone(archive)
        self.assertGreater(
            os.path.getsize(self.events), int(old_cursor.rsplit(":", 1)[-1])
        )

        _, headers, body = self.get_events(old_cursor)
        self.assertEqual(headers.get("X-Burrow-Reset"), "1")
        self.assertTrue(body.startswith(b'{"type"'))

    def test_incomplete_line_is_not_returned_or_consumed(self):
        with open(self.events, "wb") as stream:
            stream.write(b'{"type":"idle"')
        _, headers, body = self.get_events()
        self.assertEqual(body, b"")
        self.assertRegex(headers["X-Burrow-Cursor"], r"^v1:[0-9a-f]{32}:\d+:\d+:\d+:0$")

    def test_empty_log_cursor_restarts_with_reset_and_complete_polling_replay(self):
        _, headers, body = self.get_events(None)
        empty_cursor = headers["X-Burrow-Cursor"]
        self.assertEqual(body, b"")
        self.assertTrue(empty_cursor.endswith(":0"))
        replacement = {"type": "idle", "agent_id": "after-empty-restart"}
        self.append(replacement)
        self.restart_server()
        _, headers, body = self.get_events(empty_cursor)
        self.assertEqual(headers["X-Burrow-Reset"], "1")
        self.assertEqual(
            [replacement], [json.loads(line) for line in body.splitlines()]
        )

    def test_invalid_cursor_is_rejected(self):
        for cursor in (
            -1,
            "v1:short:1:2:3:4",
            "v2:" + "a" * 32 + ":1:2:3:4",
            "v1:" + "a" * 32 + ":1:2:-3:4",
            "1:2:3:4:5",
            "v1:" + "a" * 32 + ":" + "1" * 21 + ":2:3:4",
            "v1:" + "a" * 32 + ":1:2:3:4" + "0" * 161,
        ):
            with self.subTest(cursor=cursor):
                status, _, _ = self.get_events(cursor)
                self.assertEqual(status, 400)


if __name__ == "__main__":
    unittest.main()
