import os
import tempfile
import unittest

from notification_persistence import NotificationPersistence, NOTIFIED, terminal_key


class NotificationPersistenceInterfaceTest(unittest.TestCase):
    def test_public_lifecycle_journals_commits_and_recovers(self):
        with tempfile.TemporaryDirectory() as directory:
            events = os.path.join(directory, "events.jsonl")
            store = NotificationPersistence(lambda: events, lambda: (8, 4096), 4)
            event = {
                "agent_id": "a",
                "ts": "t",
                "type": "needs_human",
                "payload": {"message": "help"},
                "delivery_id": "direct-test",
            }
            self.assertTrue(store.journal(event))
            recovered = store.recover()
            self.assertEqual(len(recovered), 1)
            self.assertEqual(recovered[0][2], [event])
            self.assertTrue(store.commit_terminal(event, NOTIFIED))
            self.assertTrue(store.contains(NOTIFIED, terminal_key(event)))
            self.assertTrue(store.retire_replay_if_terminal(*recovered[0]))

    def test_lock_names_are_stable_and_domain_checked(self):
        with tempfile.TemporaryDirectory() as directory:
            events = os.path.join(directory, "events.jsonl")
            store = NotificationPersistence(lambda: events, lambda: (8, 4096), 4)
            self.assertEqual(
                store.notification_lock_path(3),
                os.path.abspath(events) + ".notify-lock-03",
            )
            with self.assertRaisesRegex(ValueError, "invalid durable ledger kind"):
                store.ledger_path("unknown")
            with self.assertRaisesRegex(ValueError, "invalid notification lock shard"):
                store.notification_lock_path(4)


if __name__ == "__main__":
    unittest.main()
