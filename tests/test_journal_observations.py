import copy
import json
import http.client
import os
import pathlib
import tempfile
import unittest
from unittest import mock

from journal_observations import MAX_DAYS, keep_indexes, reduce_indexed
from protocol import validate_event
import serve
from tests.http_test_support import RunningServer


FIXTURES = pathlib.Path(__file__).parent / "fixtures" / "journal-observations.json"


def fixture_cases():
    cases = json.loads(FIXTURES.read_text())
    for case in cases:
        case = copy.deepcopy(case)
        prefix = case.pop("path_prefix", None)
        if prefix:
            case["event"]["payload"]["path"] = (
                "/" + prefix["scalar"] * prefix["count"] + "/"
                + case["event"]["payload"]["path"])
        yield case


class JournalObservationTest(unittest.TestCase):
    def test_shared_validation_matrix(self):
        for case in fixture_cases():
            with self.subTest(case["name"]):
                self.assertEqual(validate_event(case["event"]) is None, case["valid"])
                if "error" in case:
                    self.assertEqual(validate_event(case["event"]), case["error"])

    @staticmethod
    def event(day, **changes):
        event = {"v": 0, "ts": "2026-08-25T20:31:00.000Z", "source": "steward",
                 "agent_id": "codex:life", "project": "life", "type": "journal_written",
                 "payload": {"routine": "close-of-day", "day": day,
                             "path": f"/journal/{day}.md"}}
        event.update(changes)
        return event

    def test_first_append_owns_and_rotation_keeps_one_conflict(self):
        canonical = self.event("2026-08-25")
        replay = self.event("2026-08-25", ts="1999-01-01T00:00:00.000Z")
        conflict = self.event("2026-08-25")
        conflict["payload"] = {**conflict["payload"], "routine": "nightly"}
        later = self.event("2026-08-25")
        later["payload"] = {**later["payload"], "path": "/other/2026-08-25.md"}
        records, malformed = reduce_indexed(enumerate([canonical, replay, conflict, later]))
        record = records[("codex:life", "2026-08-25")]
        self.assertEqual(record["canonical"], (0, canonical))
        self.assertEqual(record["conflict"], (2, conflict))
        self.assertEqual(malformed, [])
        self.assertEqual(keep_indexes(list(enumerate([canonical, replay, conflict, later]))), {0, 2})

    def test_capacity_keeps_highest_day_keys_globally(self):
        events = [self.event(f"2026-07-{day:02d}") for day in range(1, 32)]
        events += [self.event(f"2026-08-{day:02d}") for day in range(1, 12)]
        records, _ = reduce_indexed(enumerate(events))
        self.assertEqual(len(records), MAX_DAYS)
        self.assertNotIn(("codex:life", "2026-07-01"), records)
        self.assertIn(("codex:life", "2026-08-11"), records)

    def test_evicted_replay_and_conflict_stay_below_monotonic_frontier(self):
        events = [self.event("2026-08-24", agent_id=f"codex:{index:02d}")
                  for index in range(MAX_DAYS)]
        events.append(self.event("2026-08-25", agent_id="codex:new"))
        replay = self.event("2026-08-24", agent_id="codex:00")
        conflict = self.event("2026-08-24", agent_id="codex:00")
        conflict["payload"] = {**conflict["payload"], "routine": "nightly"}
        events.extend([replay, conflict, replay, conflict])
        records, malformed = reduce_indexed(enumerate(events))
        self.assertEqual(len(records), MAX_DAYS)
        self.assertNotIn(("codex:00", "2026-08-24"), records)
        self.assertIn(("codex:01", "2026-08-24"), records)
        self.assertEqual(sum(record["conflict"] is not None
                             for record in records.values()), 0)
        self.assertEqual(malformed, [])

    def test_same_day_ties_compare_agent_ids_by_unicode_scalar(self):
        events = [self.event("2026-08-24", agent_id=f"codex:{index:02d}")
                  for index in range(39)]
        events.extend([
            self.event("2026-08-25", agent_id="codex:\uE000"),
            self.event("2026-08-25", agent_id="codex:😀"),
        ])
        records, _ = reduce_indexed(enumerate(events))
        self.assertNotIn(("codex:00", "2026-08-24"), records)
        self.assertIn(("codex:\uE000", "2026-08-25"), records)
        self.assertIn(("codex:😀", "2026-08-25"), records)

    def test_slug_path_and_gregorian_boundaries_are_exact(self):
        event = self.event("0001-01-01")
        event["payload"]["routine"] = "a" * 128
        filename = "0001-01-01.md"
        event["payload"]["path"] = "/" + "a" * (2048 - len(filename) - 2) + "/" + filename
        self.assertEqual(len(event["payload"]["path"]), 2048)
        self.assertIsNone(validate_event(event))
        event["payload"]["path"] = "x" + event["payload"]["path"]
        self.assertEqual(validate_event(event), "invalid payload.path")
        event = self.event("9999-12-31")
        self.assertIsNone(validate_event(event))


class JournalIngestTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.previous = serve.EVENTS
        serve.EVENTS = os.path.join(self.tmp.name, "events.jsonl")
        self.running = RunningServer(serve)

    def tearDown(self):
        self.running.stop()
        serve.EVENTS = self.previous
        self.tmp.cleanup()

    def post(self, event):
        connection = http.client.HTTPConnection(*self.running.server.server_address)
        connection.request("POST", "/events", json.dumps(event).encode(),
                           {"Content-Type": "application/json"})
        response = connection.getresponse()
        status = response.status
        response.read()
        connection.close()
        return status

    def test_http_rejects_every_invalid_shape_without_append_and_never_notifies(self):
        cases = list(fixture_cases())
        valid = next(case["event"] for case in cases if case["valid"])
        with mock.patch.object(serve, "notify_async") as notify:
            self.assertEqual(self.post(valid), 204)
            for case in (item for item in cases if not item["valid"]):
                with self.subTest(case["name"]):
                    self.assertEqual(self.post(case["event"]), 400)
            notify.assert_not_called()
        with open(serve.EVENTS, encoding="utf-8") as stream:
            self.assertEqual([json.loads(line) for line in stream], [valid])


if __name__ == "__main__":
    unittest.main()
