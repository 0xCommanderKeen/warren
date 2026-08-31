import datetime as dt
import threading
import unittest

from state_coordinator import StateCoordinator
from village_state import ProjectionPolicy

from tests.test_village_state import NOW, event


class StateCoordinatorTests(unittest.TestCase):
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
