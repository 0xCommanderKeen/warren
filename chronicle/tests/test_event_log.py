import dataclasses
import json
import tempfile
import unittest
from pathlib import Path

from config import Config
from event_log import EventCursor, EventLog


class MemoryDeliveryStore:
    def __init__(self):
        self.keys = set()

    def load_ledger(self, kind):
        assert kind == "delivery-ids"
        return set(self.keys)

    def remember(self, kind, key):
        assert kind == "delivery-ids"
        self.keys.add(key)


class EventLogTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.path = Path(self.temporary.name) / "events.jsonl"
        self.store = MemoryDeliveryStore()
        self.duplicates = []
        self.log = EventLog(
            dataclasses.replace(Config(), events=self.path, max_log_bytes=0),
            self.store,
            lambda: self.duplicates.append(True),
        )

    @staticmethod
    def event(delivery_id, **values):
        return {"type": "idle", "delivery_id": delivery_id, **values}

    def test_append_deduplicates_against_log_after_ledger_eviction(self):
        event = self.event("durable-delivery-0001")
        self.assertTrue(self.log.append(event))
        self.store.keys.clear()

        self.assertFalse(self.log.append(event))
        self.assertEqual(self.duplicates, [True])
        self.assertEqual(
            [json.loads(line) for line in self.path.read_text().splitlines()], [event]
        )
        self.assertEqual(self.store.keys, {"durable-delivery-0001"})

    def test_cursor_reads_only_complete_new_records_and_resets_after_restart(self):
        first = self.event("first-delivery-0001")
        second = self.event("second-delivery-0002")
        self.log.append(first)
        records, cursor, reset = self.log.read_records("a" * 32, EventCursor.initial())
        self.assertEqual(
            ([json.loads(line) for _, line in records], reset), ([first], False)
        )

        with self.path.open("ab") as stream:
            stream.write(json.dumps(second).encode())
        records, unchanged, reset = self.log.read_records("a" * 32, cursor)
        self.assertEqual((records, unchanged.offset, reset), ([], cursor.offset, False))

        with self.path.open("ab") as stream:
            stream.write(b"\n")
        records, _, reset = self.log.read_records("a" * 32, cursor)
        self.assertEqual(
            ([json.loads(line) for _, line in records], reset), ([second], False)
        )
        records, _, reset = self.log.read_records("b" * 32, cursor)
        self.assertEqual(
            ([json.loads(line) for _, line in records], reset), ([first, second], True)
        )

    def test_projection_inputs_exposes_invalid_records_without_hiding_evidence(self):
        self.path.write_bytes(b'{"type":"idle"}\n{"bad":NaN}\nnot-json\n')
        events, cursor, generation = self.log.projection_inputs("a" * 32)
        self.assertEqual(events, [{"type": "idle"}, None, None])
        self.assertTrue(cursor.endswith(f":{self.path.stat().st_size}"))
        self.assertEqual(generation, 0)


if __name__ == "__main__":
    unittest.main()
