import datetime as dt
import threading
import unittest
from unittest.mock import patch

from state_coordinator import StateCoordinator
from village_state import ProjectionPolicy

from tests.test_village_state import NOW, event


class StateCoordinatorTests(unittest.TestCase):
    def test_reads_only_new_projection_inputs_after_the_initial_build(self):
        old = [event("tool_called", tool="Read") for _ in range(100)]
        newest = event("idle", minutes=1)
        update_calls = []

        def updates(cursor):
            update_calls.append(cursor)
            return [newest], "cursor-2", 1, False

        coordinator = StateCoordinator(
            lambda: (old, "cursor-1", 1),
            lambda: [],
            ProjectionPolicy(),
            read_updates=updates,
        )
        coordinator.evaluate(NOW)
        changed = coordinator.evaluate(NOW + dt.timedelta(minutes=1))

        self.assertEqual(["cursor-1"], update_calls)
        self.assertEqual("cursor-2", changed["cursor"])
        self.assertEqual("resting", changed["villagers"][0]["state"])

    def test_steady_state_projection_work_is_bounded_by_retained_evidence(self):
        old = [event("tool_called", tool="Read", minutes=index) for index in range(500)]
        newest = event("idle", minutes=501)
        coordinator = StateCoordinator(
            lambda: (old, "cursor-1", 1),
            lambda: [],
            ProjectionPolicy(),
            read_updates=lambda _cursor: ([newest], "cursor-2", 1, False),
        )

        with patch(
            "state_coordinator.project_village",
            wraps=__import__("village_state").project_village,
        ) as project:
            coordinator.evaluate(NOW + dt.timedelta(minutes=500))
            coordinator.evaluate(NOW + dt.timedelta(minutes=501))

        initial_work = len(project.call_args_list[0].args[0])
        steady_work = len(project.call_args_list[1].args[0])
        self.assertEqual(len(old), initial_work)
        self.assertLess(steady_work, initial_work)

    def test_a_projection_cursor_reset_rebuilds_from_the_canonical_live_log(self):
        initial = event("tool_called", tool="Read")
        retained = event("idle", minutes=1)
        coordinator = StateCoordinator(
            lambda: ([initial], "cursor-1", 0),
            lambda: [],
            ProjectionPolicy(),
            read_updates=lambda _cursor: ([retained], "cursor-2", 1, True),
        )
        coordinator.evaluate(NOW)
        rebuilt = coordinator.evaluate(NOW + dt.timedelta(minutes=1))

        self.assertEqual("cursor-2", rebuilt["cursor"])
        self.assertEqual("resting", rebuilt["villagers"][0]["state"])
        self.assertEqual(1, rebuilt["log_generation"])

    def test_restart_after_an_interrupted_fold_rebuilds_the_same_snapshot(self):
        first = event("tool_called", tool="Read")
        second = event("idle", minutes=1)
        interrupted = StateCoordinator(
            lambda: ([first], "cursor-1", 0),
            lambda: [],
            ProjectionPolicy(),
            read_updates=lambda _cursor: ([second], "cursor-2", 0, False),
        )
        interrupted.evaluate(NOW)
        with patch(
            "state_coordinator.retention.carry_forward", side_effect=OSError("crash")
        ):
            with self.assertRaises(OSError):
                interrupted.evaluate(NOW + dt.timedelta(minutes=1))

        retried = interrupted.evaluate(NOW + dt.timedelta(minutes=1))
        restarted = StateCoordinator(
            lambda: ([first, second], "cursor-2", 0),
            lambda: [],
            ProjectionPolicy(),
        ).evaluate(NOW + dt.timedelta(minutes=1))
        expected = __import__("village_state").project_village(
            [first, second], [], NOW + dt.timedelta(minutes=1), cursor="cursor-2"
        )
        for snapshot in (retried, restarted, expected):
            snapshot.pop("generation", None)
            snapshot.pop("log_generation", None)
        self.assertEqual(expected, retried)
        self.assertEqual(expected, restarted)

    def test_an_unchanged_fold_keeps_malformed_evidence_visible(self):
        coordinator = StateCoordinator(
            lambda: ([None], "cursor-1", 0),
            lambda: [],
            ProjectionPolicy(),
            read_updates=lambda _cursor: ([], "cursor-1", 0, False),
        )
        first = coordinator.evaluate(NOW)
        same = coordinator.evaluate(NOW)

        self.assertEqual(first["diagnostics"], same["diagnostics"])
        self.assertEqual("malformed_event", same["diagnostics"][0]["kind"])

    def test_atomically_publishes_new_generations_and_ignores_no_change(self):
        evidence = [event("tool_called", tool="Read")]
        coordinator = StateCoordinator(
            lambda: (list(evidence), "cursor-1", 1), lambda: [], ProjectionPolicy()
        )
        first = coordinator.evaluate(NOW)
        same = coordinator.evaluate(NOW)
        evidence.append(event("idle", minutes=1))
        changed = coordinator.evaluate(NOW + dt.timedelta(minutes=1))
        self.assertIs(first, same)
        self.assertEqual((1, 2), (first["generation"], changed["generation"]))
        self.assertEqual("resting", changed["villagers"][0]["state"])

    def test_clock_boundary_publishes_without_new_evidence(self):
        coordinator = StateCoordinator(
            lambda: ([event("tool_called", tool="Read")], "c", 1),
            lambda: [],
            ProjectionPolicy(stale_seconds=10),
        )
        first = coordinator.evaluate(NOW)
        changed = coordinator.evaluate(NOW + dt.timedelta(seconds=11))
        self.assertEqual("working", first["villagers"][0]["state"])
        self.assertEqual("stale", changed["villagers"][0]["state"])
        self.assertGreater(changed["generation"], first["generation"])

    def test_reconnect_decision_is_newer_snapshot_or_reset(self):
        current = "v1:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa:1:2:4:20"
        coordinator = StateCoordinator(
            lambda: ([], current, 4), lambda: [], ProjectionPolicy()
        )
        snapshot = coordinator.evaluate(NOW)
        prior_offset = "v1:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa:1:2:4:10"
        stale_namespace = "v1:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb:1:2:4:20"
        self.assertEqual(
            "snapshot",
            coordinator.delivery(snapshot["generation"] - 1, prior_offset)["kind"],
        )
        self.assertEqual(
            "unchanged",
            coordinator.delivery(snapshot["generation"], prior_offset)["kind"],
        )
        self.assertEqual(
            "reset",
            coordinator.delivery(snapshot["generation"], stale_namespace)["kind"],
        )

    def test_evaluation_and_delivery_share_one_published_snapshot(self):
        current = "v1:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa:1:2:4:20"
        coordinator = StateCoordinator(
            lambda: ([], current, 4), lambda: [], ProjectionPolicy()
        )

        delivery = coordinator.evaluate_delivery(0, current, NOW)

        self.assertEqual("snapshot", delivery["kind"])
        self.assertEqual(current, delivery["snapshot"]["cursor"])
        self.assertEqual(1, delivery["snapshot"]["generation"])

    def test_overlapping_evaluations_cannot_publish_an_older_cursor_last(self):
        first_read = threading.Event()
        release_first = threading.Event()
        calls_lock = threading.Lock()
        calls = 0

        def read_events():
            nonlocal calls
            with calls_lock:
                calls += 1
                call = calls
            if call == 1:
                first_read.set()
                release_first.wait(1)
                return [event("tool_called", tool="Read")], "cursor-1", 1
            return (
                [event("tool_called", tool="Read"), event("idle", minutes=1)],
                "cursor-2",
                1,
            )

        coordinator = StateCoordinator(read_events, lambda: [], ProjectionPolicy())
        older = threading.Thread(target=lambda: coordinator.evaluate(NOW))
        newer = threading.Thread(
            target=lambda: coordinator.evaluate(NOW + dt.timedelta(minutes=1))
        )
        older.start()
        self.assertTrue(first_read.wait(1))
        newer.start()
        release_first.set()
        older.join(1)
        newer.join(1)
        self.assertEqual("cursor-2", coordinator.snapshot()["cursor"])
        self.assertEqual("resting", coordinator.snapshot()["villagers"][0]["state"])


if __name__ == "__main__":
    unittest.main()
