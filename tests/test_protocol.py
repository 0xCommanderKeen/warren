import json
import pathlib
import unittest

from protocol import validate_event


FIXTURES = pathlib.Path(__file__).parent / "fixtures" / "protocol-v0-validation.json"


class ProtocolContractTest(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
