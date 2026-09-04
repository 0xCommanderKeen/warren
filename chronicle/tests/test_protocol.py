import json
import pathlib
import unittest

from protocol import validate_event


FIXTURES = pathlib.Path(__file__).parent / "fixtures" / "protocol-v0-validation.json"


class ProtocolContractTest(unittest.TestCase):
    def test_discord_outbound_events_accept_their_minimal_payloads_without_text(self):
        payloads = {
            "chat_message_posted": {
                "resident": "pip",
                "route": "discord:pip",
                "channel": "household",
                "length": 42,
            },
            "chat_post_refused": {
                "resident": "pip",
                "route": "discord:pip",
                "channel": "household",
                "reason": "channel not allowed",
            },
            "discord_channel_created": {
                "resident": "herald",
                "route": "discord:herald",
                "channel": "announcements",
            },
            "discord_thread_created": {
                "resident": "herald",
                "route": "discord:herald",
                "channel": "announcements",
                "thread": "release-42",
            },
            "discord_thread_archived": {
                "resident": "herald",
                "route": "discord:herald",
                "channel": "announcements",
                "thread": "release-42",
            },
            "discord_message_pinned": {
                "resident": "herald",
                "route": "discord:herald",
                "channel": "announcements",
                "message": "1234567890",
            },
            "discord_topic_set": {
                "resident": "herald",
                "route": "discord:herald",
                "channel": "announcements",
            },
        }
        base = {
            "v": 0,
            "ts": "2026-08-25T10:04:00.000Z",
            "source": "steward",
            "agent_id": "resident:pip",
            "project": "life",
        }

        for kind, payload in payloads.items():
            with self.subTest(kind=kind):
                event = {**base, "type": kind, "payload": payload}
                self.assertIsNone(validate_event(event))
                event["payload"] = {**payload, "text": "private message body"}
                self.assertEqual(validate_event(event), "payload.text is forbidden")

    def test_discord_outbound_event_shapes_are_strict_and_steward_authoritative(self):
        event = {
            "v": 0,
            "ts": "2026-08-25T10:04:00.000Z",
            "source": "steward",
            "agent_id": "resident:pip",
            "project": "life",
            "type": "chat_message_posted",
            "payload": {
                "resident": "pip",
                "route": "discord:pip",
                "channel": "household",
                "length": 42,
            },
        }
        for field in ("resident", "route", "channel"):
            with self.subTest(field=field):
                broken = {**event, "payload": {**event["payload"], field: " "}}
                self.assertEqual(validate_event(broken), f"invalid payload.{field}")
        event["payload"]["length"] = True
        self.assertEqual(validate_event(event), "invalid payload.length")
        event["payload"]["length"] = 42
        event["source"] = "claude-code"
        self.assertEqual(validate_event(event), "discord events require source steward")

    def test_shared_validation_matrix(self):
        for case in json.loads(FIXTURES.read_text()):
            with self.subTest(case["name"]):
                self.assertEqual(validate_event(case["event"]) is None, case["valid"])
                if "error" in case:
                    self.assertEqual(validate_event(case["event"]), case["error"])

    def test_shared_timestamp_range_rejects_year_zero_exactly(self):
        cases = json.loads(FIXTURES.read_text())
        case = next(case for case in cases if case["name"] == "year zero timestamp")
        self.assertEqual(validate_event(case["event"]), case["error"])

    def test_routine_durations_must_be_finite_across_both_adapters(self):
        cases = json.loads(FIXTURES.read_text())
        for case in (case for case in cases if "infinite duration" in case["name"]):
            with self.subTest(case["name"]):
                self.assertEqual(validate_event(case["event"]), case["error"])

    def test_finished_routine_requires_explicit_artifact_evidence(self):
        cases = json.loads(FIXTURES.read_text())
        case = next(
            case for case in cases if case["name"] == "routine finish missing artifacts"
        )
        self.assertEqual(validate_event(case["event"]), "invalid payload.artifacts")

    def test_only_steward_is_authoritative_for_routine_events(self):
        cases = json.loads(FIXTURES.read_text())
        case = next(
            case
            for case in cases
            if case["name"] == "routine start from non-Steward source"
        )
        self.assertEqual(validate_event(case["event"]), case["error"])

    def test_structured_knock_shape_is_additive_and_source_independent(self):
        event = {
            "v": 0,
            "ts": "2026-08-25T10:00:00.000Z",
            "source": "codex",
            "agent_id": "codex:keeper",
            "project": "life",
            "type": "needs_human",
            "payload": {
                "message": "May I?",
                "request_id": "r1",
                "action": "send_email",
                "detail": {"to": "a@example.com"},
                "options": ["approve", "deny", "edit"],
            },
        }
        self.assertIsNone(validate_event(event))
        # Shape defects remain protocol-valid legacy knocks; the approval
        # projection diagnoses them and never renders dead buttons.
        event["payload"]["options"] = []
        self.assertIsNone(validate_event(event))

    def test_resolution_contract_is_strict_and_steward_authoritative(self):
        event = {
            "v": 0,
            "ts": "2026-08-25T10:01:00.000Z",
            "source": "steward",
            "agent_id": "codex:keeper",
            "project": "life",
            "type": "needs_human_resolved",
            "payload": {
                "request_id": "r1",
                "decision": "approve",
                "decided_by": "api",
                "action": "send_email",
            },
        }
        self.assertIsNone(validate_event(event))
        event["payload"]["decision"] = "maybe"
        self.assertEqual(validate_event(event), "invalid payload.decision")
        event["payload"]["decision"] = "approve"
        event["source"] = "custom"
        self.assertEqual(
            validate_event(event), "approval resolutions require source steward"
        )

    def test_delegation_names_both_ends_and_only_steward_may_say_it(self):
        event = {
            "v": 0,
            "ts": "2026-08-25T10:02:00.000Z",
            "source": "steward",
            "agent_id": "claude-code:hob",
            "project": "life",
            "type": "task_delegated",
            "payload": {
                "task_id": "task-2",
                "title": "Draft the letter",
                "from": "claude-code:hob",
                "to": "codex:keeper",
                "route": "inbox",
                "parent_task_id": None,
                "depth": 1,
            },
        }
        self.assertIsNone(validate_event(event))
        # The carrier is the villager the event is filed under: a handoff attributed to
        # somebody who did not send it would draw the wrong villager walking.
        event["payload"]["from"] = "codex:other"
        self.assertEqual(validate_event(event), "payload.from must match agent_id")
        event["payload"]["from"] = "claude-code:hob"
        # A root handoff carries an explicit null parent; a blank one is a lost chain.
        event["payload"]["parent_task_id"] = ""
        self.assertEqual(validate_event(event), "invalid payload.parent_task_id")
        event["payload"]["parent_task_id"] = None
        event["source"] = "codex"
        self.assertEqual(validate_event(event), "task events require source steward")

    def test_a_late_session_report_is_as_strict_as_the_close_it_is_not(self):
        """``task_session_finished`` describes a run, not the board row it lost."""
        event = {
            "v": 0,
            "ts": "2026-08-25T10:03:00.000Z",
            "source": "steward",
            "agent_id": "claude-code:hob",
            "project": "life",
            "type": "task_session_finished",
            "payload": {
                "task_id": "task-1",
                "title": "Research X",
                "claimant": "claude-code:hob",
                "run_id": "run-7",
                "outcome": "ok",
                "artifacts": [],
                "duration_s": 12.5,
                "reason": "lease_lost",
            },
        }
        self.assertIsNone(validate_event(event))
        # The run is the whole subject of this fact, so it cannot be anonymous.
        del event["payload"]["run_id"]
        self.assertEqual(validate_event(event), "invalid payload.run_id")
        event["payload"]["run_id"] = "run-7"
        event["payload"]["claimant"] = "codex:other"
        self.assertEqual(validate_event(event), "payload.claimant must match agent_id")

    def test_a_dropped_chat_message_records_who_knocked(self):
        """The visible half of the chat bridge's auth rule (warren#108)."""
        event = {
            "v": 0,
            "ts": "2026-08-25T10:04:00.000Z",
            "source": "steward",
            "agent_id": "claude-code:hob",
            "project": "life",
            "type": "chat_message_dropped",
            "payload": {
                "route": "telegram",
                "address": "@life_agent_bot",
                "from": "87654321",
                "reason": "not an operator",
            },
        }
        self.assertIsNone(validate_event(event))
        # Who knocked is the fact an operator acts on; an unattributed drop is noise.
        del event["payload"]["from"]
        self.assertEqual(validate_event(event), "invalid payload.from")
        event["payload"]["from"] = "87654321"
        # Only Steward runs the chat routes, so only Steward can witness a drop.
        event["source"] = "claude-code"
        self.assertEqual(validate_event(event), "chat events require source steward")

    def test_a_drop_may_say_how_many_knocks_it_stands_for(self):
        """The count Steward's limiter puts on the record it did emit (warren#278)."""
        event = {
            "v": 0,
            "ts": "2026-08-25T10:04:00.000Z",
            "source": "steward",
            "agent_id": "claude-code:hob",
            "project": "life",
            "type": "chat_message_dropped",
            "payload": {
                "route": "telegram",
                "address": "@life_agent_bot",
                "from": "87654321",
                "reason": "not an operator",
                "suppressed": 199,
            },
        }
        self.assertIsNone(validate_event(event))
        # Optional: a Steward older than the limiter emits every knock and counts none.
        del event["payload"]["suppressed"]
        self.assertIsNone(validate_event(event))
        # A count is a count. Anything else is somebody inventing a number the panel
        # renders, and a negative one would read as knocks that never happened.
        for bad in (-1, "12", 1.0, True, None):
            event["payload"]["suppressed"] = bad
            self.assertEqual(validate_event(event), "invalid payload.suppressed")

    def test_a_restart_counts_its_attempt_so_a_crash_loop_reads_as_one(self):
        event = {
            "v": 0,
            "ts": "2026-08-25T10:05:00.000Z",
            "source": "steward",
            "agent_id": "claude-code:hob",
            "project": "life",
            "type": "resident_restarted",
            "payload": {"reason": "container was not running", "attempt": 3},
        }
        self.assertIsNone(validate_event(event))
        event["payload"]["attempt"] = 0
        self.assertEqual(validate_event(event), "invalid payload.attempt")
        event["payload"]["attempt"] = 1
        event["source"] = "claude-code"
        self.assertEqual(
            validate_event(event), "resident lifecycle events require source steward"
        )

    def test_resident_identity_is_strict_and_steward_authoritative(self):
        declared = {
            "v": 0, "ts": "2026-08-25T10:05:00.000Z", "source": "steward",
            "agent_id": "steward:pip", "project": "pip", "type": "resident_declared",
            "payload": {"name": "Pip", "char": "Monk", "accent": "#123456",
                        "role": "helper", "summary": None, "resident_id": "pip",
                        "uid": "0198-uid", "home": 0},
        }
        self.assertIsNone(validate_event(declared))
        declared["source"] = "claude-code"
        self.assertEqual(validate_event(declared), "resident lifecycle events require source steward")


if __name__ == "__main__":
    unittest.main()
