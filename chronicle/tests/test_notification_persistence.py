import json
import os
import tempfile
import unittest

from notification_persistence import (
    NotificationPersistence,
    NOTIFIED,
    knock_key,
    terminal_key,
)


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

    def test_store_owns_attempt_progression_and_process_reset(self):
        with tempfile.TemporaryDirectory() as directory:
            events = os.path.join(directory, "events.jsonl")
            store = NotificationPersistence(lambda: events, lambda: (8, 4096), 4)
            event = {
                "agent_id": "a",
                "ts": "t",
                "type": "needs_human",
                "payload": {"message": "help"},
                "delivery_id": "attempt-test",
            }

            self.assertEqual(store.next_attempt(event), 1)
            self.assertEqual(store.next_attempt(event), 2)
            self.assertFalse(store.attempts_exhausted(event))
            self.assertEqual(store.next_attempt(event), 3)
            self.assertTrue(store.attempts_exhausted(event))

            store.clear_attempts(event)
            self.assertFalse(store.attempts_exhausted(event))
            self.assertEqual(store.next_attempt(event), 1)
            store.reset_process_state()
            self.assertEqual(store.next_attempt(event), 1)

    def test_recovery_restores_attempt_policy_through_the_store_interface(self):
        with tempfile.TemporaryDirectory() as directory:
            events = os.path.join(directory, "events.jsonl")
            store = NotificationPersistence(lambda: events, lambda: (8, 4096), 4)
            event = {
                "agent_id": "a",
                "ts": "t",
                "type": "needs_human",
                "payload": {"message": "help"},
                "delivery_id": "recovered-attempt-test",
            }

            self.assertTrue(store.record_attempt(event, 2))
            store.reset_process_state()
            self.assertEqual(store.recover()[0][2], [event])
            self.assertFalse(store.attempts_exhausted(event))
            self.assertEqual(store.next_attempt(event), 3)
            self.assertTrue(store.attempts_exhausted(event))


class DamagedDurableStateTest(unittest.TestCase):
    """What the store does with bytes it did not write itself."""

    def store(self, directory, **limits):
        events = os.path.join(directory, "events.jsonl")
        return events, NotificationPersistence(
            lambda: events, lambda: limits.get("knocks", (8, 4096)), 4
        )

    def event(self, delivery_id):
        return {
            "agent_id": "a",
            "ts": "t",
            "type": "needs_human",
            "payload": {"message": "help"},
            "delivery_id": delivery_id,
        }

    def test_journal_entry_without_a_usable_event_is_skipped_not_fatal(self):
        """A null or scalar "event" must not escape the OSError degradation.

        knock_key raises AttributeError on a non-dict, and AttributeError is
        not OSError, so it would sail past every caller's fallback and reach
        the HTTP server instead of quietly skipping one unusable line.
        """
        with tempfile.TemporaryDirectory() as directory:
            events, store = self.store(directory)
            good = self.event("usable")
            with open(events + ".knocks", "w", encoding="utf-8") as stream:
                for entry in (
                    {"event": None, "attempts": 0},
                    {"event": "not-an-object", "attempts": 0},
                    {"event": 5},
                    {"event": good, "attempts": 0},
                ):
                    stream.write(json.dumps(entry) + "\n")

            self.assertEqual(store.read_journal_keys(), {knock_key(good)})
            self.assertTrue(store.journal(self.event("added")))
            self.assertTrue(store.record_attempt(good, 1))
            self.assertTrue(store.commit_terminal(good, NOTIFIED))
            self.assertIn(
                knock_key(self.event("added")),
                store.read_journal_keys(),
                "the usable entries still round-trip",
            )

    def test_damaged_ledger_is_refused_rather_than_silently_truncated(self):
        """Rewriting only the decodable prefix would forget terminal keys.

        A forgotten terminal outcome lets an already-answered notification
        knock again forever, which is the same loss the capacity refusal
        exists to prevent -- so damage refuses on the same terms.
        """
        with tempfile.TemporaryDirectory() as directory:
            events, store = self.store(directory)
            ledger = events + ".notified"
            kept = terminal_key(self.event("survivor"))
            damaged = (
                b"burrow-sha256-" + b"a" * 64 + b"\n"
                b"\xff\xfe not utf-8\n" + kept.encode("utf-8") + b"\n"
            )
            with open(ledger, "wb") as stream:
                stream.write(damaged)

            with self.assertRaisesRegex(OSError, "damaged durable ledger"):
                store.remember(NOTIFIED, terminal_key(self.event("newcomer")))
            with open(ledger, "rb") as stream:
                self.assertEqual(
                    stream.read(), damaged, "the ledger is left byte-identical"
                )
            self.assertTrue(
                store.contains(NOTIFIED, kept),
                "and a key behind the damage is still found",
            )

    def test_ledger_reading_skips_damage_instead_of_stopping_at_it(self):
        """Every key still readable is one that cannot knock again."""
        with tempfile.TemporaryDirectory() as directory:
            events, store = self.store(directory)
            behind = terminal_key(self.event("behind-the-damage"))
            with open(events + ".notified", "wb") as stream:
                stream.write(b"\xff\xfe bad\n" + behind.encode("utf-8") + b"\n")
            self.assertIn(behind, store.load_ledger(NOTIFIED))

    def test_blank_ledger_lines_are_not_keys(self):
        with tempfile.TemporaryDirectory() as directory:
            events, store = self.store(directory)
            real = terminal_key(self.event("real"))
            with open(events + ".notified", "w", encoding="utf-8") as stream:
                stream.write("\n   \n" + real + "\n")
            self.assertEqual(store.load_ledger(NOTIFIED), {real})
            self.assertEqual(store.terminal_counts(), (1, 0))


if __name__ == "__main__":
    unittest.main()
