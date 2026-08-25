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
        self.assertIn("<unknown>", rendered)
        self.assertNotIn("access_token", rendered)
        self.assertNotIn("secret-value", rendered)

    def test_hostile_unknown_keys_never_enter_public_diagnostics(self):
        cases = (
            ("top-level AWS key", (), "AKIAIOSFODNN7EXAMPLE", "$.<unknown>"),
            ("match token", ("match",), "access_token", "$.match.<unknown>"),
            ("soul assignment", ("soul",), "PASSWORD=publish-me", "$.soul.<unknown>"),
            ("skill opaque", ("skills", 0), "7f3a91d4c6b208e5a7f9d1c3b6e804a2",
             "$.skills[0].<unknown>"),
            ("route HTML", ("routes", 0), "<script>alert('key')</script>",
             "$.routes[0].<unknown>"),
            ("grant control", ("app_grants", 0), "bad\nkey\u001b[31m",
             "$.app_grants[0].<unknown>"),
            ("memory secret", ("memory",), "private_key", "$.memory.<unknown>"),
        )
        for name, location, hostile_key, expected_path in cases:
            with self.subTest(name=name):
                manifest = valid_manifest()
                target = manifest
                for part in location:
                    target = target[part]
                hostile_value = "rejected-value-for-" + str(len(location))
                target[hostile_key] = hostile_value

                report = self.load({"hostile.resident.json": manifest})
                rendered = json.dumps(report)

                self.assertEqual(report["residents"], [])
                self.assertTrue(report["diagnostics"])
                self.assertEqual(report["diagnostics"][0]["path"], expected_path)
                self.assertNotIn(json.dumps(hostile_key) + ":", rendered)
                self.assertNotIn(hostile_value, rendered)
                for partial in report["diagnostic_residents"]:
                    self.assertFalse(partial["valid"])
                    self.assertNotIn("home", partial)

    def test_schema_shaped_unknown_and_sensitive_text_never_enters_reports(self):
        cases = (
            ((), "soul.name", "ordinary-value"),
            ((), "skills[0].id", "ordinary-value"),
            ((), "match.agent_id", "ordinary-value"),
            ((), "evil[987654]", "ordinary-value"),
            ((), "private_key.soul.name", "top-level-secret-value"),
            (("skills", 0), "soul.name[3].match.agent_id", "ordinary-value"),
            (("routes", 0), "private_key.skills[0].id", "nested-secret-value"),
            (("memory",), "token[987654].soul.name", "nested-secret-value"),
        )
        hostile_fragments = (
            "soul.name", "skills[0].id", "match.agent_id", "evil[987654]",
            "987654", "private_key", "token[987654]", "top-level-secret-value",
            "nested-secret-value",
        )
        for location, hostile_key, hostile_value in cases:
            with self.subTest(location=location, hostile_key=hostile_key):
                manifest = valid_manifest()
                target = manifest
                for part in location:
                    target = target[part]
                target[hostile_key] = hostile_value

                report = self.load({"hostile.resident.json": manifest})
                rendered = json.dumps(report)

                self.assertEqual(report["residents"], [])
                self.assertTrue(report["diagnostics"])
                self.assertNotIn(hostile_key, rendered)
                self.assertNotIn(hostile_value, rendered)
                for fragment in hostile_fragments:
                    if fragment in hostile_key or fragment in hostile_value:
                        self.assertNotIn(fragment, rendered)
                [partial] = report["diagnostic_residents"]
                self.assertFalse(partial["valid"])
                self.assertNotIn("home", partial)
                if location and location[0] in {"skills", "routes", "app_grants"}:
                    [row] = partial["capabilities"][location[0]]
                    self.assertTrue(row["invalid"])
                    self.assertIn("diagnostic_path", row)

    def test_sensitive_values_below_hostile_keys_use_only_trusted_parent_paths(self):
        cases = (
            ((), "outer.soul[987654]", "payload.match[42]",
             "api_key=top-level-hostile-secret", "$.<unknown>.<unknown>"),
            (("skills", 0), "vault.routes[123456]", "payload.agent_id",
             "Bearer nested-hostile-secret", "$.skills[0].<unknown>.<unknown>"),
        )
        for location, outer, inner, secret, expected_path in cases:
            with self.subTest(location=location):
                manifest = valid_manifest()
                target = manifest
                for part in location:
                    target = target[part]
                target[outer] = {inner: secret}

                report = self.load({"hostile.resident.json": manifest})
                rendered = json.dumps(report)

                self.assertEqual(report["residents"], [])
                self.assertEqual(report["diagnostics"][0]["path"], expected_path)
                for hostile in (outer, inner, secret, "987654", "123456"):
                    if hostile in outer or hostile in inner or hostile in secret:
                        self.assertNotIn(hostile, rendered)
                [partial] = report["diagnostic_residents"]
                self.assertFalse(partial["valid"])
                self.assertNotIn("home", partial)
                if location:
                    [row] = partial["capabilities"][location[0]]
                    self.assertTrue(row["invalid"])
                    self.assertEqual(row["diagnostic_path"], expected_path)

    def test_known_field_name_is_still_suppressed_in_an_unknown_context(self):
        problems = residents.validate_manifest(valid_manifest(id="untrusted-value"))
        self.assertEqual(problems[0]["path"], "$.<unknown>")
        rendered = json.dumps(problems)
        self.assertNotIn('"id":', rendered)
        self.assertNotIn("untrusted-value", rendered)

    def test_malformed_capability_gets_safe_non_resident_diagnostic_projection(self):
        manifest = valid_manifest(match={"agent_id": "safe-agent"}, home=5,
                                  app_grants=[{
                                      "id": "gmail", "status_ref": "config:gmail",
                                      "access_token": "hostile-secret-value",
                                  }])
        report = self.load({"unsafe.resident.json": manifest})
        self.assertEqual(report["residents"], [])
        [partial] = report["diagnostic_residents"]
        self.assertFalse(partial["valid"])
        self.assertTrue(partial["diagnostic"])
        self.assertEqual(partial["match"], {"agent_id": "safe-agent"})
        self.assertEqual(partial["declared_home"], 5)
        [grant] = partial["capabilities"]["app_grants"]
        self.assertEqual(grant["id"], "gmail")
        self.assertEqual(grant["status_ref"], "config:gmail")
        self.assertTrue(grant["invalid"])
        rendered = json.dumps(report)
        self.assertNotIn("hostile-secret-value", rendered)
        self.assertNotIn("access_token\": \"", rendered)

    def test_credential_material_in_allowed_values_is_rejected_without_echoing_it(self):
        unsafe_values = {
            "aws_access_key": (("routes", 0, "status_ref"), "AKIAIOSFODNN7EXAMPLE",
                               "$.routes[0].status_ref"),
            "bearer": (("soul", "description"),
                       "Bearer eyJhbGciOiJIUzI1NiJ9.payload.signature",
                       "$.soul.description"),
            "opaque": (("skills", 0, "status_ref"),
                       "7f3a91d4c6b208e5a7f9d1c3b6e804a2f5d7c9b1",
                       "$.skills[0].status_ref"),
            "assignment": (("memory", "ref"), "api_key=do-not-publish",
                           "$.memory.ref"),
        }
        for name, (location, value, expected_path) in unsafe_values.items():
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
                self.assertEqual(report["diagnostics"][0]["path"], expected_path)

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
