import glob
import json
import os
import tempfile
import unittest
from unittest import mock

from hooks import durable


class DurableGenerationTests(unittest.TestCase):
    def test_publish_orders_file_fsync_replace_directory_fsync_and_retirement(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "authority")
            replay = path + durable.REPLAY_PREFIX + "old"
            with open(replay, "w", encoding="utf-8") as stream:
                stream.write("old\n")
            operations = []
            real_replace = os.replace
            real_unlink = os.unlink

            def replace(source, target):
                operations.append("replace")
                real_replace(source, target)

            def unlink(target):
                operations.append("retire")
                real_unlink(target)

            with (
                mock.patch.object(durable.os, "replace", side_effect=replace),
                mock.patch.object(durable.os, "unlink", side_effect=unlink),
                mock.patch.object(
                    durable,
                    "fsync_parent",
                    side_effect=lambda _path: operations.append("dir-fsync"),
                ),
            ):
                durable.publish_lines(path, ("new\n",), retire=(replay,))

            self.assertEqual(
                operations, ["replace", "dir-fsync", "retire", "dir-fsync"]
            )
            with open(path, encoding="utf-8") as stream:
                self.assertEqual(stream.read(), "new\n")
            self.assertFalse(os.path.exists(replay))

    def test_generation_path_conventions_are_centralized(self):
        self.assertEqual(durable.pending_path("state"), "state.pending")
        self.assertEqual(durable.lock_path("state"), "state.lock")
        self.assertEqual(durable.replay_path("state", "one"), "state.replay.one")
        with self.assertRaisesRegex(ValueError, "invalid replay generation"):
            durable.replay_path("state", "../elsewhere")

    def test_retirement_syncs_prior_success_before_propagating_real_error(self):
        operations = []
        failure = PermissionError("authority cannot be retired")

        def unlink(path):
            operations.append(("unlink", path))
            if path == "second":
                raise failure

        with (
            mock.patch.object(durable.os, "unlink", side_effect=unlink),
            mock.patch.object(
                durable,
                "fsync_parent",
                side_effect=lambda path: operations.append(("fsync", path)),
            ),
        ):
            with self.assertRaises(PermissionError) as raised:
                durable.retire_files(("first", "second", "never-attempted"))
        self.assertIs(raised.exception, failure)
        self.assertEqual(
            [kind for kind, _ in operations], ["unlink", "unlink", "fsync"]
        )

    def test_retirement_suppresses_only_missing_files(self):
        missing = FileNotFoundError("already gone")
        denied = OSError(5, "I/O failure")
        unlink = mock.Mock(side_effect=(missing, None, denied))
        fsync = mock.Mock()
        with (
            mock.patch.object(durable.os, "unlink", unlink),
            mock.patch.object(durable, "fsync_parent", fsync),
        ):
            with self.assertRaises(OSError) as raised:
                durable.retire_files(("missing", "removed", "failed"))
        self.assertIs(raised.exception, denied)
        fsync.assert_called_once()


class SpoolOrderingTests(unittest.TestCase):
    """The crash ordering every durable log in burrow shares, proven once.

    These are operation-sequence tests: they assert *when* each syscall
    happens relative to the others, because the end state alone cannot
    distinguish a correct publish from one that retires its sources first.
    """

    def spool(self, directory, name="spool", **options):
        options.setdefault("limits", lambda: (16, 4096))
        return durable.Spool(os.path.join(directory, name), **options)

    def record_operations(self, spool):
        """Trace the syscalls that define crash ordering, in order."""
        operations = []
        real_replace = os.replace
        real_unlink = os.unlink

        def replace(source, target):
            operations.append(("replace", os.path.basename(target)))
            real_replace(source, target)

        def unlink(target):
            operations.append(("unlink", os.path.basename(target)))
            real_unlink(target)

        return operations, (
            mock.patch.object(durable.os, "replace", side_effect=replace),
            mock.patch.object(durable.os, "unlink", side_effect=unlink),
            mock.patch.object(
                durable,
                "fsync_parent",
                side_effect=lambda _path: operations.append(("dir-fsync", "")),
            ),
        )

    def test_publish_commits_the_generation_before_retiring_any_source(self):
        """G1 then G2: no source may be unlinked before the replacement is durable."""
        with tempfile.TemporaryDirectory() as directory:
            spool = self.spool(directory)
            source = spool.generation_path("old")
            with open(source, "w", encoding="utf-8") as stream:
                stream.write('{"id":"kept"}\n')
            operations, patches = self.record_operations(spool)
            with patches[0], patches[1], patches[2]:
                spool.publish([{"id": "kept"}], retire=(source,))
            self.assertEqual(
                [kind for kind, _ in operations],
                ["replace", "dir-fsync", "unlink", "dir-fsync"],
            )
            self.assertEqual(operations[0][1], "spool")
            self.assertFalse(os.path.exists(source))
            self.assertEqual(
                spool.read().records, [{"id": "kept"}], "published generation"
            )

    def test_publish_quarantines_evidence_before_erasing_its_source(self):
        """G3: torn bytes outlive the file they came from."""
        with tempfile.TemporaryDirectory() as directory:
            spool = self.spool(directory)
            source = spool.generation_path("crashed")
            with open(source, "wb") as stream:
                stream.write(b'{"id":"kept"}\n{"id":"unfinis')
            torn = spool.read(source).torn
            self.assertEqual(torn, b'{"id":"unfinis')
            operations, patches = self.record_operations(spool)
            with patches[0], patches[1], patches[2]:
                spool.publish(
                    [{"id": "kept"}],
                    quarantine=((source, torn),),
                    retire=(source,),
                )
            kinds = [kind for kind, _ in operations]
            self.assertEqual(
                kinds, ["replace", "dir-fsync", "dir-fsync", "unlink", "dir-fsync"]
            )
            quarantined = glob.glob(source + durable.TORN_PREFIX + "*")
            self.assertEqual(len(quarantined), 1, "one forensic sample")
            with open(quarantined[0], "rb") as stream:
                self.assertEqual(stream.read(), torn, "byte-exact evidence")
            self.assertFalse(os.path.exists(source), "source retired after evidence")

    def test_publish_commits_every_extra_target_before_any_retirement(self):
        """A multi-file publish is one transaction: all replaces, then sources go."""
        with tempfile.TemporaryDirectory() as directory:
            spool = self.spool(directory)
            sidecar = os.path.join(directory, "sidecar.json")
            source = spool.generation_path("old")
            with open(source, "w", encoding="utf-8") as stream:
                stream.write('{"id":"a"}\n')
            operations, patches = self.record_operations(spool)
            with patches[0], patches[1], patches[2]:
                staged = durable.stage_json(sidecar, {"generation": 1})
                spool.publish(
                    [{"id": "a"}], extra=((staged, sidecar),), retire=(source,)
                )
            self.assertEqual(
                operations,
                [
                    ("replace", "spool"),
                    ("replace", "sidecar.json"),
                    ("dir-fsync", ""),
                    ("unlink", "spool.replay.old"),
                    ("dir-fsync", ""),
                ],
                "both targets replace before the single directory fsync and retirement",
            )
            with open(sidecar, encoding="utf-8") as stream:
                self.assertEqual(json.load(stream), {"generation": 1})

    def test_crash_between_publish_and_retire_leaves_duplicates_dedupe_collapses(self):
        """G2's crash window is safe only because every reader dedupes (G9)."""
        with tempfile.TemporaryDirectory() as directory:
            spool = self.spool(directory, key=lambda record: record["id"])
            source = spool.generation_path("old")
            with open(source, "w", encoding="utf-8") as stream:
                stream.write('{"id":"a","n":1}\n{"id":"b","n":1}\n')
            with mock.patch.object(
                durable.os, "unlink", side_effect=OSError("crash before retirement")
            ):
                with self.assertRaises(OSError):
                    spool.publish(
                        [{"id": "a", "n": 2}, {"id": "b", "n": 2}], retire=(source,)
                    )
            self.assertTrue(os.path.exists(source), "both copies survive the crash")
            self.assertEqual(
                [record["id"] for record in spool.collect()],
                ["a", "b"],
                "each record is seen once, in its original slot",
            )

    def test_a_generation_overrides_the_active_authority_it_duplicates(self):
        """Read order is active-then-generations, and the last read wins.

        A generation is written by a writer that could not reach the active
        authority, or handed off from one; either way it is the copy that
        knows something the active file does not.
        """
        with tempfile.TemporaryDirectory() as directory:
            spool = self.spool(directory, key=lambda record: record["id"])
            with open(spool.path, "w", encoding="utf-8") as stream:
                stream.write('{"id":"a","n":1}\n')
            with open(spool.generation_path("later"), "w", encoding="utf-8") as stream:
                stream.write('{"id":"a","n":2}\n')
            self.assertEqual(spool.collect(), [{"id": "a", "n": 2}])

    def test_handoff_renames_only_a_non_empty_authority(self):
        """G6: the rename is the handoff point, and it is durable before return."""
        with tempfile.TemporaryDirectory() as directory:
            spool = self.spool(directory)
            operations, patches = self.record_operations(spool)
            with patches[0], patches[1], patches[2]:
                self.assertIsNone(spool.handoff(), "absent authority hands off nothing")
                with open(spool.path, "w", encoding="utf-8") as stream:
                    stream.write("")
                self.assertIsNone(spool.handoff(), "empty authority hands off nothing")
                self.assertEqual(operations, [], "and touches no directory")
                with open(spool.path, "w", encoding="utf-8") as stream:
                    stream.write('{"id":"a"}\n')
                generation = spool.handoff("handed")
            self.assertEqual(
                [kind for kind, _ in operations],
                ["replace", "dir-fsync"],
                "rename then directory fsync, before any appender may run",
            )
            self.assertEqual(generation, spool.generation_path("handed"))
            self.assertFalse(
                os.path.exists(spool.path), "a concurrent append starts a new active"
            )
            self.assertEqual(spool.generation_paths(), [generation])

    def test_handoff_rejects_a_generation_name_that_escapes_the_spool(self):
        with tempfile.TemporaryDirectory() as directory:
            spool = self.spool(directory)
            with self.assertRaisesRegex(ValueError, "invalid replay generation"):
                spool.generation_path("../elsewhere")
            with self.assertRaisesRegex(ValueError, "invalid replay generation"):
                spool.generation_path("")

    def test_staging_is_never_promoted_however_valid_it_looks(self):
        """G5: only os.replace commits a generation; syntax proves nothing."""
        with tempfile.TemporaryDirectory() as directory:
            spool = self.spool(directory)
            with open(spool.path, "w", encoding="utf-8") as stream:
                stream.write('{"id":"authority"}\n')
            for content in (b"", b'{"id":"orphan"}\n', b'{"id":"orphan"}\n{"tor'):
                with open(spool.pending_path(), "wb") as stream:
                    stream.write(content)
                self.assertTrue(spool.discard_staging())
                self.assertFalse(os.path.exists(spool.pending_path()))
                self.assertEqual(
                    spool.read().records,
                    [{"id": "authority"}],
                    "the previous generation remains the only authority",
                )
            self.assertFalse(spool.discard_staging(), "nothing left to discard")

    def test_damage_stops_the_generation_and_returns_the_tail_verbatim(self):
        with tempfile.TemporaryDirectory() as directory:
            spool = self.spool(directory)
            with open(spool.path, "wb") as stream:
                stream.write(b'{"id":"a"}\n{"id":"b"}\n{"id":"c"\n{"id":"d"}\n')
            generation = spool.read(damage=durable.STOP_AT_DAMAGE)
            self.assertEqual(generation.records, [{"id": "a"}, {"id": "b"}])
            self.assertEqual(generation.torn, b'{"id":"c"\n{"id":"d"}\n')
            self.assertFalse(generation.complete)

    def test_damage_may_instead_be_skipped_and_only_mark_incompleteness(self):
        """SKIP_DAMAGE: a torn line hides one record, never the ones behind it."""
        with tempfile.TemporaryDirectory() as directory:
            spool = self.spool(directory)
            with open(spool.path, "wb") as stream:
                stream.write(b'{"id":"a"}\n{"id":"c"\n{"id":"d"}\n')
            generation = spool.read(damage=durable.SKIP_DAMAGE)
            self.assertEqual(generation.records, [{"id": "a"}, {"id": "d"}])
            self.assertEqual(generation.torn, b"", "nothing is quarantined")
            self.assertFalse(generation.complete, "but the generation is not retirable")

    def test_unreadable_generation_is_distinguishable_from_an_empty_one(self):
        with tempfile.TemporaryDirectory() as directory:
            spool = self.spool(directory)
            self.assertIsNone(spool.read(), "absent authority reads as None")
            with open(spool.path, "wb") as stream:
                stream.write(b"")
            self.assertEqual(spool.read().records, [], "an empty one reads as empty")

    def test_bound_evicts_oldest_until_both_caps_hold_and_names_its_victims(self):
        """G8: capacity is measured in encoded bytes, and victims are returned."""
        with tempfile.TemporaryDirectory() as directory:
            spool = self.spool(directory, limits=lambda: (2, 4096))
            kept, victims = spool.bound([{"id": "a"}, {"id": "b"}, {"id": "c"}])
            self.assertEqual(kept, [{"id": "b"}, {"id": "c"}])
            self.assertEqual(victims, [{"id": "a"}], "the oldest goes first")
            spool = self.spool(directory, limits=lambda: (16, 12))
            kept, victims = spool.bound([{"id": "a"}, {"id": "b"}])
            self.assertEqual(kept, [{"id": "b"}], "bytes bind before records do")
            self.assertEqual(victims, [{"id": "a"}])
            kept, victims = spool.bound([{"id": "a"}], max_records=0)
            self.assertEqual((kept, victims), ([], [{"id": "a"}]), "caps override")

    def test_dedupe_keeps_the_last_value_in_the_first_slot(self):
        """G9: a crash-window copy replaces the older value without jumping the queue."""
        with tempfile.TemporaryDirectory() as directory:
            spool = self.spool(directory, key=lambda record: record["id"])
            self.assertEqual(
                spool.dedupe(
                    [
                        {"id": "a", "n": 1},
                        {"id": "b", "n": 1},
                        {"id": "a", "n": 2},
                    ]
                ),
                [{"id": "a", "n": 2}, {"id": "b", "n": 1}],
            )

    def test_generations_list_active_first_and_never_count_quarantine_files(self):
        with tempfile.TemporaryDirectory() as directory:
            spool = self.spool(directory)
            older = spool.generation_path("aaa")
            newer = spool.generation_path("bbb")
            for path in (spool.path, older, newer):
                with open(path, "w", encoding="utf-8") as stream:
                    stream.write("")
            quarantine = spool.quarantine_tail(b"torn", older)
            self.assertIn(durable.TORN_PREFIX, quarantine)
            self.assertEqual(spool.generations(), [spool.path, older, newer])
            self.assertNotIn(quarantine, spool.generation_paths())

    def test_quarantine_retains_the_newest_within_both_caps(self):
        with tempfile.TemporaryDirectory() as directory:
            spool = self.spool(directory, torn_files=2, torn_bytes=9)
            for index in range(4):
                spool.quarantine_tail(bytes([index]) * 4)
            retained = glob.glob(spool.path + durable.TORN_PREFIX + "*")
            self.assertLessEqual(len(retained), 2)
            self.assertLessEqual(sum(os.path.getsize(p) for p in retained), 9)
            with open(max(retained), "rb") as stream:
                self.assertEqual(stream.read(), bytes([3]) * 4, "newest survives")

    def test_active_and_generation_quarantines_share_one_budget(self):
        """Evidence from every generation draws on the same bounded allowance."""
        with tempfile.TemporaryDirectory() as directory:
            spool = self.spool(directory, torn_files=2, torn_bytes=64)
            spool.quarantine_tail(b"active-tear")
            for index in range(3):
                spool.quarantine_tail(b"replay-tear", spool.generation_path("g%d" % index))
            retained = glob.glob(spool.path + durable.TORN_PREFIX + "*") + glob.glob(
                spool.path + durable.REPLAY_PREFIX + "*" + durable.TORN_PREFIX + "*"
            )
            self.assertLessEqual(len(retained), 2, "one cap across both roots")

    def test_publish_can_rewrite_one_generation_in_place_from_outside_the_glob(self):
        """A per-generation rewrite must not stage inside the generation glob."""
        with tempfile.TemporaryDirectory() as directory:
            spool = self.spool(directory)
            generation = spool.generation_path("old")
            with open(generation, "w", encoding="utf-8") as stream:
                stream.write('{"id":"a"}\n{"id":"b"}\n')
            staging = spool.path + ".prune-old"
            observed = []
            real_replace = os.replace

            def replace(source, target):
                observed.append(sorted(os.path.basename(p) for p in spool.generations()))
                real_replace(source, target)

            with mock.patch.object(durable.os, "replace", side_effect=replace):
                spool.publish([{"id": "b"}], target=generation, staging=staging)
            self.assertEqual(spool.read(generation).records, [{"id": "b"}])
            self.assertEqual(
                observed,
                [["spool", "spool.replay.old"]],
                "the staging file is never mistaken for a generation",
            )

    def test_lock_is_a_stable_sidecar_that_reports_contention_without_waiting(self):
        with tempfile.TemporaryDirectory() as directory:
            spool = self.spool(directory)
            self.assertEqual(spool.lock_path(), spool.path + durable.LOCK_SUFFIX)
            self.assertEqual(
                spool.lock_path(".transaction"),
                spool.path + ".transaction" + durable.LOCK_SUFFIX,
            )
            with spool.lock(create=True) as held:
                self.assertIsNotNone(held)
                with spool.lock(blocking=False) as contended:
                    self.assertIsNone(contended, "contention is reported, never awaited")
            with spool.lock(blocking=False) as released:
                self.assertIsNotNone(released, "and the lock is released on exit")


if __name__ == "__main__":
    unittest.main()
