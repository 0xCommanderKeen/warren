import dataclasses
import json
import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from config import Config
from delivery_id_index import BATCH_SIZE, DeliveryIdIndex
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

    def test_unique_append_lookup_cost_does_not_grow_with_retained_rows(self):
        retained = [self.event(f"retained-{index:04d}") for index in range(200)]
        self.path.write_text(
            "".join(json.dumps(event) + "\n" for event in retained),
            encoding="utf-8",
        )
        archive = Path(self.temporary.name) / "archive"
        archive.mkdir()
        for generation in range(4):
            (archive / f"events-20260101T00000{generation}Z.jsonl").write_text(
                "".join(
                    json.dumps(self.event(f"archive-{generation}-{index}")) + "\n"
                    for index in range(50)
                ),
                encoding="utf-8",
            )

        # The first lookup may rebuild derived state from canonical JSONL.
        self.assertTrue(self.log.append(self.event("new-after-rebuild")))
        with (
            patch("delivery_id_index.json.loads", wraps=json.loads) as loads,
            patch("delivery_id_index.glob.glob", wraps=__import__("glob").glob) as glob,
        ):
            self.assertTrue(self.log.append(self.event("new-steady-state")))

        row_parses = [
            call for call in loads.call_args_list if isinstance(call.args[0], bytes)
        ]
        self.assertEqual(row_parses, [])
        self.assertEqual(glob.call_count, 0)

    def test_duplicate_is_rejected_across_multiple_legacy_archives(self):
        archive = Path(self.temporary.name) / "archive"
        archive.mkdir()
        for generation in range(3):
            events = [self.event(f"archive-{generation}-{index}") for index in range(4)]
            (archive / f"events-20260101T00000{generation}Z.jsonl").write_text(
                "".join(json.dumps(event) + "\n" for event in events),
                encoding="utf-8",
            )
        self.log = EventLog(
            dataclasses.replace(
                Config(), events=self.path, archive_dir=archive, max_log_bytes=0
            ),
            self.store,
            lambda: self.duplicates.append(True),
        )

        self.assertFalse(self.log.append(self.event("archive-1-2")))
        self.assertEqual(self.duplicates, [True])
        self.assertTrue(Path(str(self.path) + ".delivery-index.sqlite3").is_file())

    def test_missing_index_rebuilds_from_jsonl_and_rejects_duplicate(self):
        event = self.event("survives-index-loss")
        self.assertTrue(self.log.append(event))
        index = Path(str(self.path) + ".delivery-index.sqlite3")
        index.unlink()
        self.store.keys.clear()

        restarted = EventLog(
            dataclasses.replace(Config(), events=self.path, max_log_bytes=0),
            self.store,
            lambda: self.duplicates.append(True),
        )
        self.assertFalse(restarted.append(event))
        self.assertEqual(self.duplicates, [True])

    def test_crash_between_jsonl_fsync_and_index_commit_is_reconciled(self):
        self.assertTrue(self.log.append(self.event("indexed-before-crash")))
        crashed = self.event("jsonl-only-after-crash")
        with self.path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(crashed) + "\n")
            stream.flush()

        self.store.keys.clear()
        self.assertFalse(self.log.append(crashed))
        self.assertEqual(self.duplicates, [True])

    def test_corrupt_index_falls_back_to_jsonl_then_rebuilds(self):
        event = self.event("canonical-despite-corrupt-index")
        self.assertTrue(self.log.append(event))
        index = Path(str(self.path) + ".delivery-index.sqlite3")
        index.write_bytes(b"torn sqlite publication")
        self.store.keys.clear()

        self.assertFalse(self.log.append(event))
        self.store.keys.clear()
        self.assertTrue(self.log.append(self.event("rebuild-after-corruption")))

    def test_rotation_keeps_archived_delivery_authoritative(self):
        archive = Path(self.temporary.name) / "archive"
        self.log = EventLog(
            dataclasses.replace(
                Config(), events=self.path, archive_dir=archive, max_log_bytes=0
            ),
            self.store,
            lambda: self.duplicates.append(True),
        )
        first = self.event("before-rotation", agent_id="resident")
        self.assertTrue(self.log.append(first))
        for index in range(120):
            self.assertTrue(
                self.log.append(
                    self.event(f"rotation-fill-{index}", agent_id="resident")
                )
            )

        archived = self.log.rotate()
        self.assertIsNotNone(archived)
        self.store.keys.clear()
        self.assertFalse(self.log.append(first))
        self.assertEqual(self.duplicates, [True])

    def test_published_in_place_edit_and_rapid_replacement_reconcile(self):
        archive = Path(self.temporary.name) / "archive"
        archive.mkdir()
        path = archive / "events-20260101T000000Z.jsonl"
        removed = self.event("removed-by-edit")
        path.write_text(json.dumps(removed) + "\n", encoding="utf-8")
        index = DeliveryIdIndex(self.path, archive)
        self.assertTrue(index.contains("removed-by-edit"))

        added = self.event("added-in-place")
        with path.open("r+", encoding="utf-8") as stream:
            stream.seek(0)
            stream.write(json.dumps(added) + "\n")
            stream.truncate()
        index.publish_archives()
        self.assertFalse(index.contains("removed-by-edit"))
        self.assertTrue(index.contains("added-in-place"))

        replacement = self.event("rapid-replacement")
        path.unlink()
        path.write_text(json.dumps(replacement) + "\n", encoding="utf-8")
        index.publish_archives()
        self.assertFalse(index.contains("added-in-place"))
        self.assertTrue(index.contains("rapid-replacement"))

        stopped_edit = self.event("unpublished-while-stopped")
        path.write_text(json.dumps(stopped_edit) + "\n", encoding="utf-8")
        restarted = DeliveryIdIndex(self.path, archive)
        self.assertFalse(restarted.contains("rapid-replacement"))
        self.assertTrue(restarted.contains("unpublished-while-stopped"))

    def test_publication_during_rebuild_discards_stale_archive_membership(self):
        archive = Path(self.temporary.name) / "archive"
        archive.mkdir()
        path = archive / "events-20260101T000000Z.jsonl"
        path.write_text(json.dumps(self.event("removed-during-rebuild")) + "\n")
        index = DeliveryIdIndex(self.path, archive)
        original = index._index_file
        published = False

        def index_then_publish(database, candidate, offset):
            nonlocal published
            complete_offset = original(database, candidate, offset)
            if candidate == path.resolve() and not published:
                published = True
                path.write_text(json.dumps(self.event("new-canonical")) + "\n")
                index.publish_archives()
            return complete_offset

        with patch.object(index, "_index_file", side_effect=index_then_publish):
            self.assertTrue(index.contains("new-canonical"))
        self.assertTrue(published)
        self.assertFalse(index.contains("removed-during-rebuild"))

    def test_recovery_rebuilds_oldest_middle_and_newest_across_generations(self):
        for failure in ("missing", "corrupt", "torn", "crash-gap"):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as root:
                root = Path(root)
                archive = root / "archive"
                archive.mkdir()
                events = root / "events.jsonl"
                identities = ["oldest", "middle", "newest"]
                for generation, identity in enumerate(identities[:2]):
                    (archive / f"events-20260101T00000{generation}Z.jsonl").write_text(
                        json.dumps(self.event(identity)) + "\n", encoding="utf-8"
                    )
                live_identity = (
                    "live-before-gap" if failure == "crash-gap" else "newest"
                )
                events.write_text(
                    json.dumps(self.event(live_identity)) + "\n", encoding="utf-8"
                )
                index = DeliveryIdIndex(events, archive)
                self.assertTrue(index.contains("oldest"))
                index_path = Path(str(events) + ".delivery-index.sqlite3")
                if failure == "missing":
                    index_path.unlink()
                elif failure == "corrupt":
                    index_path.write_bytes(b"not sqlite")
                elif failure == "torn":
                    index_path.write_bytes(index_path.read_bytes()[:64])
                else:
                    with events.open("a", encoding="utf-8") as stream:
                        stream.write(json.dumps(self.event("newest")) + "\n")
                        stream.flush()
                for identity in identities:
                    self.assertTrue(index.contains(identity))

    def test_clean_membership_is_read_only_during_writer_contention(self):
        index = DeliveryIdIndex(self.path, Path(self.temporary.name) / "archive")
        self.path.write_text(json.dumps(self.event("known")) + "\n", encoding="utf-8")
        self.assertTrue(index.contains("known"))
        writer = sqlite3.connect(index.path)
        writer.execute("PRAGMA journal_mode=WAL")
        writer.execute("BEGIN IMMEDIATE")
        try:
            with patch.object(index, "_write_connection", side_effect=AssertionError):
                self.assertTrue(index.contains("known"))
                self.assertFalse(index.contains("unknown"))
        finally:
            writer.rollback()
            writer.close()

    def test_large_rebuild_inserts_bounded_batches(self):
        archive = Path(self.temporary.name) / "archive"
        archive.mkdir()
        count = BATCH_SIZE * 3 + 17
        (archive / "events-20260101T000000Z.jsonl").write_text(
            "".join(
                json.dumps(self.event(f"large-{number}")) + "\n"
                for number in range(count)
            ),
            encoding="utf-8",
        )
        index = DeliveryIdIndex(self.path, archive)
        sizes = []
        original = index._insert_rows

        def measured(database, rows):
            sizes.append(len(rows))
            return original(database, rows)

        with patch.object(index, "_insert_rows", side_effect=measured):
            self.assertTrue(index.contains(f"large-{count - 1}"))
        self.assertGreater(len(sizes), 3)
        self.assertLessEqual(max(sizes), BATCH_SIZE)

    def test_concurrent_logs_append_one_copy_of_same_delivery(self):
        other_store = MemoryDeliveryStore()
        other = EventLog(
            dataclasses.replace(Config(), events=self.path, max_log_bytes=0),
            other_store,
        )
        barrier = threading.Barrier(2)
        results = []

        def append(log):
            barrier.wait()
            results.append(log.append(self.event("concurrent-delivery")))

        threads = [
            threading.Thread(target=append, args=(log,)) for log in (self.log, other)
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        self.assertEqual(sorted(results), [False, True])
        records = [json.loads(line) for line in self.path.read_text().splitlines()]
        self.assertEqual(records, [self.event("concurrent-delivery")])

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
