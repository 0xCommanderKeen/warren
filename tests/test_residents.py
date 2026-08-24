import json
import pathlib
import tempfile
import unittest

import residents


def valid_manifest(**overrides):
    manifest = {
        "manifest_version": 1,
        "match": {"agent_id": "claude-code:resident"},
        "home": 2,
        "soul": {
            "name": "Hob", "char": "Monk", "accent": "#a68a4f",
            "role": "household spirit", "description": "Keeps the household moving.",
        },
        "skills": [{"id": "daily-summary", "status_ref": "bundled"}],
        "memory": {"ref": "file:///data/memory.md", "status_ref": "mounted"},
        "routes": [{"id": "telegram", "status_ref": "life/config#telegram"}],
        "app_grants": [{"id": "gmail", "status_ref": "life/config#gmail"}],
    }
    manifest.update(overrides)
    return manifest


class ResidentManifestTest(unittest.TestCase):
    def load(self, files):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            for name, value in files.items():
                (root / name).write_text(json.dumps(value), encoding="utf-8")
            return residents.load_resident_manifests(root)

    def test_valid_manifest_exposes_all_five_capability_dimensions(self):
        report = self.load({"hob.resident.json": valid_manifest()})
        self.assertEqual(report["diagnostics"], [])
        [resident] = report["residents"]
        self.assertEqual(resident["home"], 2)
        self.assertEqual(set(resident["capabilities"]),
                         {"soul", "skills", "memory", "routes", "app_grants"})
        self.assertEqual(resident["capabilities"]["skills"],
                         [{"id": "daily-summary", "status_ref": "bundled"}])
        self.assertEqual(resident["capabilities"]["memory"],
                         {"ref": "file:///data/memory.md", "status_ref": "mounted"})
        self.assertEqual(resident["capabilities"]["routes"],
                         [{"id": "telegram", "status_ref": "life/config#telegram"}])
        self.assertEqual(resident["capabilities"]["app_grants"],
                         [{"id": "gmail", "status_ref": "life/config#gmail"}])
        self.assertEqual(resident["meta"]["agent_id"], "claude-code:resident")
        self.assertEqual(resident["body"], "Keeps the household moving.")

    def test_manifest_version_requires_exact_integer_one(self):
        for version in (True, False, 1.0):
            with self.subTest(version=version):
                report = self.load({
                    "hob.resident.json": valid_manifest(manifest_version=version),
                })
                self.assertEqual(report["residents"], [])
                self.assertEqual(report["diagnostics"], [{
                    "file": "hob.resident.json",
                    "path": "$.manifest_version",
                    "message": "must equal integer 1",
                }])

        report = self.load({"hob.resident.json": valid_manifest(manifest_version=1)})
        self.assertEqual(report["diagnostics"], [])
        self.assertEqual(len(report["residents"]), 1)

    def test_missing_dimension_is_rejected_with_an_actionable_path(self):
        manifest = valid_manifest()
        del manifest["memory"]
        report = self.load({"incomplete.resident.json": manifest})
        self.assertEqual(report["residents"], [])
        self.assertIn("memory", report["diagnostics"][0]["path"])
        self.assertIn("required", report["diagnostics"][0]["message"])

    def test_credentials_and_secrets_are_rejected_not_stored(self):
        manifest = valid_manifest(app_grants=[{
            "id": "gmail", "status_ref": "configured", "access_token": "secret-value",
        }])
        report = self.load({"unsafe.resident.json": manifest})
        self.assertEqual(report["residents"], [])
        rendered = json.dumps(report)
        self.assertIn("access_token", rendered)
        self.assertNotIn("secret-value", rendered)

    def test_credential_material_in_allowed_values_is_rejected_without_echoing_it(self):
        unsafe_values = {
            "aws_access_key": (("routes", 0, "status_ref"), "AKIAIOSFODNN7EXAMPLE"),
            "bearer": (("soul", "description"), "Bearer eyJhbGciOiJIUzI1NiJ9.payload.signature"),
            "opaque": (("skills", 0, "status_ref"), "7f3a91d4c6b208e5a7f9d1c3b6e804a2f5d7c9b1"),
            "assignment": (("memory", "ref"), "api_key=do-not-publish"),
        }
        for name, (location, value) in unsafe_values.items():
            with self.subTest(name=name):
                manifest = valid_manifest()
                target = manifest
                for part in location[:-1]:
                    target = target[part]
                target[location[-1]] = value
                report = self.load({f"{name}.resident.json": manifest})
                self.assertEqual(report["residents"], [])
                rendered = json.dumps(report)
                self.assertNotIn(value, rendered)
                self.assertIn("credential", rendered)

    def test_duplicate_home_is_rejected_instead_of_silently_reassigned(self):
        first = valid_manifest(match={"agent_id": "one"}, home=4)
        second = valid_manifest(match={"project": "two"}, home=4)
        report = self.load({"a.resident.json": first, "b.resident.json": second})
        self.assertEqual([resident["file"] for resident in report["residents"]],
                         ["a.resident.json"])
        self.assertIn("already reserved", report["diagnostics"][0]["message"])

    def test_checked_in_manifests_all_validate(self):
        root = pathlib.Path(__file__).resolve().parents[1] / "villagers"
        report = residents.load_resident_manifests(root)
        self.assertEqual(report["diagnostics"], [])
        self.assertEqual({resident["file"] for resident in report["residents"]},
                         {"burrow.resident.json", "life.resident.json"})


if __name__ == "__main__":
    unittest.main()
