import json
import pathlib
import unittest
from unittest.mock import patch

import serve
from scripts.export_state_contract import binding_error, snapshot_shape_fingerprint
from scripts.export_openapi import rendered_schema
from village_state import SCHEMA_VERSION


ROOT = pathlib.Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "fixtures" / "state-contract"
STATE_SHAPE_BINDING = ROOT / "docs" / "state-shape.json"


class UIStateContractTests(unittest.TestCase):
    def test_checked_in_state_shape_matches_current_snapshot_models(self):
        binding = json.loads(STATE_SHAPE_BINDING.read_text(encoding="utf-8"))
        error = binding_error(binding)
        self.assertIsNone(error, error)

    def test_state_shape_guard_accepts_a_recorded_version_bump(self):
        bumped_binding = {"schema_version": 2, "fingerprint": "sha256:new"}
        self.assertIsNone(
            binding_error(
                bumped_binding,
                current_version=2,
                current_fingerprint="sha256:new",
            )
        )

    def test_state_shape_guard_explains_both_resolutions_for_same_version_drift(self):
        error = binding_error(
            {"schema_version": 1, "fingerprint": "sha256:old"},
            current_version=1,
            current_fingerprint="sha256:new",
        )
        self.assertIn("bump SCHEMA_VERSION", error)
        self.assertIn("re-recorded fingerprint", error)

    def test_state_shape_fingerprint_ignores_key_order_and_documentation(self):
        first = {
            "title": "First prose",
            "type": "object",
            "properties": {"name": {"description": "Old prose", "type": "string"}},
        }
        second = {
            "properties": {"name": {"type": "string", "description": "New prose"}},
            "type": "object",
            "title": "Second prose",
        }
        with patch.object(serve.VillageState, "model_json_schema", return_value=first):
            first_fingerprint = snapshot_shape_fingerprint()
        with patch.object(serve.VillageState, "model_json_schema", return_value=second):
            second_fingerprint = snapshot_shape_fingerprint()
        self.assertEqual(first_fingerprint, second_fingerprint)

    def test_state_shape_fingerprint_keeps_fields_named_like_documentation(self):
        without_title_field = {"type": "object", "properties": {}}
        with_title_field = {
            "type": "object",
            "properties": {"title": {"type": "string"}},
        }
        with patch.object(
            serve.VillageState,
            "model_json_schema",
            return_value=without_title_field,
        ):
            first_fingerprint = snapshot_shape_fingerprint()
        with patch.object(
            serve.VillageState,
            "model_json_schema",
            return_value=with_title_field,
        ):
            second_fingerprint = snapshot_shape_fingerprint()
        self.assertNotEqual(first_fingerprint, second_fingerprint)

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
                self.assertEqual(SCHEMA_VERSION, snapshot.schema_version)
                self.assertTrue(snapshot.villagers)
                self.assertTrue(snapshot.residents)
                self.assertTrue(snapshot.tasks)
                self.assertTrue(snapshot.approvals)


if __name__ == "__main__":
    unittest.main()
