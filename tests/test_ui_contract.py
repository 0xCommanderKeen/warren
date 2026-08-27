import json
import pathlib
import unittest

import serve
from scripts.export_openapi import rendered_schema


ROOT = pathlib.Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "fixtures" / "state-contract"


class UIStateContractTests(unittest.TestCase):
    def test_checked_in_openapi_matches_runtime_schema(self):
        self.assertEqual(
            (ROOT / "docs" / "openapi.json").read_text(encoding="utf-8"),
            rendered_schema(),
            "OpenAPI changed; run `uv run python scripts/export_openapi.py` and review it",
        )
        schema = serve.app.openapi()
        self.assertEqual({"post"}, set(schema["paths"]["/events"]))
        self.assertIn("get", schema["paths"]["/state"])
        self.assertIn("get", schema["paths"]["/state/stream"])

    def test_complete_snapshot_fixtures_match_the_server_model(self):
        paths = sorted(FIXTURES.glob("*.json"))
        self.assertTrue(paths)
        for path in paths:
            with self.subTest(path=path.name):
                envelope = json.loads(path.read_text(encoding="utf-8"))
                validated = serve.StateEnvelope.model_validate(envelope)
                snapshot = validated.snapshot
                self.assertTrue(snapshot.villagers)
                self.assertTrue(snapshot.residents)
                self.assertTrue(snapshot.tasks)
                self.assertTrue(snapshot.approvals)


if __name__ == "__main__":
    unittest.main()
