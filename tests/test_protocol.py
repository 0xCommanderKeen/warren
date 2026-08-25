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
        case = next(case for case in cases if case["name"] == "routine finish missing artifacts")
        self.assertEqual(validate_event(case["event"]), "invalid payload.artifacts")

    def test_only_steward_is_authoritative_for_routine_events(self):
        cases = json.loads(FIXTURES.read_text())
        case = next(case for case in cases if case["name"] == "routine start from non-Steward source")
        self.assertEqual(validate_event(case["event"]), case["error"])


if __name__ == "__main__":
    unittest.main()
