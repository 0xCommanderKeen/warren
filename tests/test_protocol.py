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

    def test_shared_timestamp_range_rejects_year_zero_exactly(self):
        cases = json.loads(FIXTURES.read_text())
        case = next(case for case in cases if case["name"] == "year zero timestamp")
        self.assertEqual(validate_event(case["event"]), case["error"])


if __name__ == "__main__":
    unittest.main()
