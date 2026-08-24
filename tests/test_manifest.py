"""The manifest library: what passes, what fails, and how loudly it fails."""

from pathlib import Path

import pytest
import yaml

from conftest import VALID_SOUL, ResidentWriter, valid_manifest
from steward import manifest as m


def field_paths(result: m.ValidationResult) -> list[str]:
    return [d.field_path for d in result.diagnostics]


def problem_for(result: m.ValidationResult, field_path: str) -> str:
    matches = [d.problem for d in result.diagnostics if d.field_path == field_path]
    assert matches, f"no diagnostic for {field_path}; got {field_paths(result)}"
    return matches[0]


# ---------------------------------------------------------------------------- happy path


def test_valid_manifest_passes(write_resident: ResidentWriter) -> None:
    result = m.validate_manifest(write_resident())
    assert result.ok, [d.render() for d in result.diagnostics]
    assert result.diagnostics == ()
    (resident,) = result.residents
    assert resident.id == "test-agent"
    assert resident.manifest.soul.name == "Testy"
    assert resident.manifest.charter.rules[0].startswith("Never send email")
    assert resident.soul.voice == "Flat, factual, short."
    assert resident.directory.name == "test-agent"


def test_skills_accept_bare_strings_and_mappings(write_resident: ResidentWriter) -> None:
    resident = m.load_manifest(write_resident())
    assert [skill.id for skill in resident.manifest.skills] == ["daily-summary", "write-journal"]
    assert resident.manifest.skills[0].source == "library"
    assert resident.manifest.skills[1].note == "end of run"


def test_structured_escalation_is_accepted(write_resident: ResidentWriter) -> None:
    data = valid_manifest()
    data["charter"]["escalation"] = {"when": ["Anything irreversible."], "how": "needs_human"}
    resident = m.load_manifest(write_resident(data))
    escalation = resident.manifest.charter.escalation
    assert isinstance(escalation, m.Escalation)
    assert escalation.how == "needs_human"


def test_union_field_diagnostics_name_the_field_not_the_union_arm(
    write_resident: ResidentWriter,
) -> None:
    data = valid_manifest()
    data["charter"]["escalation"] = 5
    result = m.validate_manifest(write_resident(data))
    assert not result.ok
    assert field_paths(result) == ["charter.escalation", "charter.escalation"]
    assert "needs_human" in result.diagnostics[0].example


def test_project_scoped_resident_needs_no_agent_id(write_resident: ResidentWriter) -> None:
    data = valid_manifest()
    del data["agent_id"]
    data["project"] = "burrow"
    soul = VALID_SOUL.replace("agent_id: claude-code:test-agent", "project: burrow")
    assert m.validate_manifest(write_resident(data, soul=soul)).ok


def test_load_manifest_returns_a_resident(write_resident: ResidentWriter) -> None:
    resident = m.load_manifest(write_resident())
    assert resident.manifest.agent_id == "claude-code:test-agent"
    assert resident.manifest.runner.kind == "claude"
    assert resident.manifest.routines[0].timeout_s == 900


def test_load_manifest_raises_with_diagnostics(write_resident: ResidentWriter) -> None:
    data = valid_manifest()
    del data["memory"]
    with pytest.raises(m.ManifestError) as excinfo:
        m.load_manifest(write_resident(data))
    assert "memory" in str(excinfo.value)
    assert excinfo.value.diagnostics[0].example


# -------------------------------------------------------------- the five capability dimensions


@pytest.mark.parametrize(
    "dimension",
    ["soul", "skills", "memory", "routes", "app_grants"],
)
def test_missing_capability_dimension_fails(write_resident: ResidentWriter, dimension: str) -> None:
    data = valid_manifest()
    del data[dimension]
    result = m.validate_manifest(write_resident(data))
    assert not result.ok
    assert problem_for(result, dimension) == "required field is missing"
    assert dimension.split("_", maxsplit=1)[0] in result.diagnostics[0].example


@pytest.mark.parametrize("charter_field", ["mission", "duties", "rules", "escalation"])
def test_missing_charter_field_fails(write_resident: ResidentWriter, charter_field: str) -> None:
    data = valid_manifest()
    del data["charter"][charter_field]
    result = m.validate_manifest(write_resident(data))
    assert not result.ok
    assert problem_for(result, f"charter.{charter_field}") == "required field is missing"


def test_missing_charter_fails(write_resident: ResidentWriter) -> None:
    data = valid_manifest()
    del data["charter"]
    result = m.validate_manifest(write_resident(data))
    assert problem_for(result, "charter") == "required field is missing"


def test_empty_dimensions_are_allowed_when_declared(write_resident: ResidentWriter) -> None:
    data = valid_manifest()
    data["skills"] = []
    data["routes"] = []
    data["app_grants"] = []
    data["routines"] = []
    assert m.validate_manifest(write_resident(data)).ok


def test_empty_charter_lists_fail(write_resident: ResidentWriter) -> None:
    data = valid_manifest()
    data["charter"]["duties"] = []
    result = m.validate_manifest(write_resident(data))
    assert "at least 1 item" in problem_for(result, "charter.duties")


def test_blank_charter_entry_fails(write_resident: ResidentWriter) -> None:
    data = valid_manifest()
    data["charter"]["rules"] = ["   "]
    result = m.validate_manifest(write_resident(data))
    assert "must not be empty" in problem_for(result, "charter.rules")


def test_every_diagnostic_has_file_field_problem_and_example(
    write_resident: ResidentWriter,
) -> None:
    data = valid_manifest()
    del data["soul"]
    del data["routes"]
    result = m.validate_manifest(write_resident(data))
    assert len(result.diagnostics) >= 2
    for diagnostic in result.diagnostics:
        assert diagnostic.file.name == "manifest.yaml"
        assert diagnostic.field_path
        assert diagnostic.problem
        assert diagnostic.example
        assert diagnostic.field_path in diagnostic.render()


# ------------------------------------------------------------------------ credential rejection


@pytest.mark.parametrize(
    "key",
    ["token", "api_key", "password", "client_secret", "credentials", "refresh_token"],
)
def test_credential_shaped_key_is_rejected(write_resident: ResidentWriter, key: str) -> None:
    data = valid_manifest()
    data["app_grants"][0][key] = "whatever"
    result = m.validate_manifest(write_resident(data))
    assert not result.ok
    assert "credential-shaped" in problem_for(result, f"app_grants[0].{key}")
    assert result.residents == ()


def test_credential_shaped_key_at_top_level_is_rejected(write_resident: ResidentWriter) -> None:
    data = valid_manifest()
    data["burrow_token"] = "abc"
    result = m.validate_manifest(write_resident(data))
    assert "credential-shaped" in problem_for(result, "burrow_token")


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("sk-abcdefghijklmnopqrstuvwxyz0123", "an inline API key"),
        ("ghp_abcdefghijklmnopqrstuvwxyz0123456789", "an inline GitHub token"),
        ("xoxb-1234567890-abcdefghijkl", "an inline Slack token"),
        ("AKIAIOSFODNN7EXAMPLE", "an inline AWS access key id"),
        ("-----BEGIN RSA PRIVATE KEY-----", "an inline private key"),
        ("https://user:hunter22@mail.example.com/inbox", "an inline password in a URL"),
        (
            "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dozjgNryP4J3jVmNHl0w5N",
            "an inline JWT",
        ),
    ],
)
def test_inline_secret_values_are_rejected(
    write_resident: ResidentWriter, value: str, expected: str
) -> None:
    data = valid_manifest()
    data["routes"][0]["address"] = value
    result = m.validate_manifest(write_resident(data))
    assert not result.ok
    assert expected in problem_for(result, "routes[0].address")


def test_opaque_blob_where_a_reference_is_expected_is_rejected(
    write_resident: ResidentWriter,
) -> None:
    data = valid_manifest()
    data["memory"]["path"] = "aGVsbG9Xb3JsZDEyMzQ1Njc4OTBhYmNkZWZnaGlqaw"
    result = m.validate_manifest(write_resident(data))
    assert "opaque blob" in problem_for(result, "memory.path")


def test_hex_digest_where_a_reference_is_expected_is_rejected(
    write_resident: ResidentWriter,
) -> None:
    data = valid_manifest()
    data["memory"]["path"] = "d41d8cd98f00b204e9800998ecf8427e0123abcd"
    result = m.validate_manifest(write_resident(data))
    assert "opaque blob" in problem_for(result, "memory.path")


def test_ordinary_paths_and_urls_survive_the_blob_check() -> None:
    for reference in (
        "/data/residents/life-agent/memory",
        "~/.steward/memory/burrow-builder",
        "https://github.com/0xCommanderKeen/burrow/issues",
        "op://Private/Gmail",
        "steward:scheduler",
    ):
        assert not m._looks_like_opaque_blob(reference)


def test_secret_in_the_soul_body_is_rejected(write_resident: ResidentWriter) -> None:
    soul = VALID_SOUL + "\nMy key is ghp_abcdefghijklmnopqrstuvwxyz0123456789.\n"
    result = m.validate_manifest(write_resident(soul=soul))
    assert not result.ok
    assert "an inline GitHub token" in problem_for(result, "body")


# --------------------------------------------------------------------------------- soul file


def test_voice_cap_is_enforced(write_resident: ResidentWriter) -> None:
    soul = VALID_SOUL.replace("Flat, factual, short.", "x" * (m.VOICE_MAX_CHARS + 1))
    result = m.validate_manifest(write_resident(soul=soul))
    assert not result.ok
    assert str(m.VOICE_MAX_CHARS) in problem_for(result, "## Voice")


def test_voice_at_the_cap_passes(write_resident: ResidentWriter) -> None:
    soul = VALID_SOUL.replace("Flat, factual, short.", "x" * m.VOICE_MAX_CHARS)
    assert m.validate_manifest(write_resident(soul=soul)).ok


def test_voice_section_stops_at_the_next_heading(write_resident: ResidentWriter) -> None:
    soul = VALID_SOUL + "\n## Skills\n- " + "y" * m.VOICE_MAX_CHARS + "\n"
    result = m.validate_manifest(write_resident(soul=soul))
    assert result.ok
    assert result.residents[0].soul.voice == "Flat, factual, short."


def test_soul_without_a_voice_section_is_fine(write_resident: ResidentWriter) -> None:
    soul = VALID_SOUL.split("## Voice")[0]
    result = m.validate_manifest(write_resident(soul=soul))
    assert result.ok
    assert result.residents[0].soul.voice is None


def test_missing_soul_file_fails(write_resident: ResidentWriter) -> None:
    result = m.validate_manifest(write_resident(soul=None))
    assert "does not exist" in problem_for(result, "soul.file")


def test_soul_without_frontmatter_fails(write_resident: ResidentWriter) -> None:
    result = m.validate_manifest(write_resident(soul="Just a body.\n"))
    assert "no --- frontmatter" in problem_for(result, "frontmatter")


def test_soul_with_broken_frontmatter_yaml_fails(write_resident: ResidentWriter) -> None:
    result = m.validate_manifest(write_resident(soul="---\nname: [unclosed\n---\nbody\n"))
    assert "not valid YAML" in problem_for(result, "frontmatter")


def test_soul_with_scalar_frontmatter_fails(write_resident: ResidentWriter) -> None:
    result = m.validate_manifest(write_resident(soul="---\njust a string\n---\nbody\n"))
    assert "must be a mapping" in problem_for(result, "frontmatter")


def test_soul_contradicting_the_manifest_fails(write_resident: ResidentWriter) -> None:
    soul = VALID_SOUL.replace("name: Testy", "name: Someone Else")
    result = m.validate_manifest(write_resident(soul=soul))
    assert "source of truth" in problem_for(result, "frontmatter.name")


def test_soul_claiming_a_project_the_manifest_lacks_fails(
    write_resident: ResidentWriter,
) -> None:
    soul = VALID_SOUL.replace("role: test bot", "role: test bot\nproject: burrow")
    result = m.validate_manifest(write_resident(soul=soul))
    assert "remove project" in [d.example for d in result.diagnostics]


# ------------------------------------------------------------------------------ cross-checks


def test_id_must_match_the_directory(write_resident: ResidentWriter) -> None:
    result = m.validate_manifest(write_resident(directory="somewhere-else"))
    assert "does not match directory" in problem_for(result, "id")


def test_routine_requiring_an_ungranted_skill_fails(write_resident: ResidentWriter) -> None:
    data = valid_manifest()
    data["routines"][0]["requires"] = ["daily-summry"]
    result = m.validate_manifest(write_resident(data))
    assert "does not grant" in problem_for(result, "routines[0].requires[0]")
    assert "daily-summary" in result.diagnostics[0].example


def test_routine_requiring_an_unknown_skill_still_names_a_fix(
    write_resident: ResidentWriter,
) -> None:
    data = valid_manifest()
    data["routines"][0]["requires"] = ["zzzzzzzz"]
    result = m.validate_manifest(write_resident(data))
    assert "grant it under skills" in result.diagnostics[0].example


def test_duplicate_ids_fail(write_resident: ResidentWriter) -> None:
    data = valid_manifest()
    data["routes"].append({"id": "schedule", "kind": "cli", "address": "claude-code:interactive"})
    result = m.validate_manifest(write_resident(data))
    assert "duplicate id" in problem_for(result, "routes[1].id")


def test_manifest_needs_agent_id_or_project(write_resident: ResidentWriter) -> None:
    data = valid_manifest()
    del data["agent_id"]
    soul = VALID_SOUL.replace("agent_id: claude-code:test-agent\n", "")
    result = m.validate_manifest(write_resident(data, soul=soul))
    assert "agent_id" in problem_for(result, "<root>")


# ----------------------------------------------------------------------------- field shapes


@pytest.mark.parametrize(
    ("path", "value"),
    [
        (("soul", "accent"), "a68a4f"),
        (("agent_id",), "no-source-prefix"),
        (("id",), "Test_Agent"),
        (("version",), 1),
        (("memory", "kind"), "brainwave"),
    ],
)
def test_malformed_scalar_fields_fail(
    write_resident: ResidentWriter, path: tuple[str, ...], value: object
) -> None:
    data = valid_manifest()
    target = data
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value
    result = m.validate_manifest(write_resident(data, directory="test-agent"))
    assert not result.ok
    assert result.diagnostics[0].example


def test_unknown_field_is_rejected(write_resident: ResidentWriter) -> None:
    data = valid_manifest()
    data["memory"]["retention"] = "forever"
    result = m.validate_manifest(write_resident(data))
    assert "Extra inputs are not permitted" in problem_for(result, "memory.retention")


@pytest.mark.parametrize("schedule", ["every morning", "0 7 * *", "0 7 * * * *", "@daily"])
def test_bad_cron_schedules_fail(write_resident: ResidentWriter, schedule: str) -> None:
    data = valid_manifest()
    data["routines"][0]["schedule"] = schedule
    result = m.validate_manifest(write_resident(data))
    assert "five-field cron" in problem_for(result, "routines[0].schedule")


@pytest.mark.parametrize("schedule", ["0 7 * * *", "*/15 * * * 1-5", "15,45 8-18 * * *"])
def test_good_cron_schedules_pass(write_resident: ResidentWriter, schedule: str) -> None:
    data = valid_manifest()
    data["routines"][0]["schedule"] = schedule
    assert m.validate_manifest(write_resident(data)).ok


@pytest.mark.parametrize("timeout", [0, -1, 90000])
def test_bad_routine_timeouts_fail(write_resident: ResidentWriter, timeout: int) -> None:
    data = valid_manifest()
    data["routines"][0]["timeout_s"] = timeout
    result = m.validate_manifest(write_resident(data))
    assert not result.ok


def test_command_runner_requires_a_template(write_resident: ResidentWriter) -> None:
    data = valid_manifest()
    data["runner"] = {"kind": "command"}
    result = m.validate_manifest(write_resident(data))
    assert "requires a command template" in problem_for(result, "runner")


def test_command_runner_requires_the_prompt_placeholder(
    write_resident: ResidentWriter,
) -> None:
    data = valid_manifest()
    data["runner"] = {"kind": "command", "command": ["my-agent", "--cwd", "{workdir}"]}
    result = m.validate_manifest(write_resident(data))
    assert "{prompt}" in problem_for(result, "runner")


def test_command_runner_rejects_unknown_placeholders(write_resident: ResidentWriter) -> None:
    data = valid_manifest()
    data["runner"] = {"kind": "command", "command": ["my-agent", "{prompt}", "{home}"]}
    result = m.validate_manifest(write_resident(data))
    assert "unknown placeholder" in problem_for(result, "runner")


def test_command_runner_accepts_a_valid_template(write_resident: ResidentWriter) -> None:
    data = valid_manifest()
    data["runner"] = {
        "kind": "command",
        "command": ["my-agent", "--prompt", "{prompt}", "--cwd", "{workdir}"],
    }
    assert m.validate_manifest(write_resident(data)).ok


def test_non_command_runner_rejects_a_template(write_resident: ResidentWriter) -> None:
    data = valid_manifest()
    data["runner"] = {"kind": "claude", "command": ["claude", "-p", "{prompt}"]}
    result = m.validate_manifest(write_resident(data))
    assert "does not take a command template" in problem_for(result, "runner")


def test_unknown_runner_kind_fails(write_resident: ResidentWriter) -> None:
    data = valid_manifest()
    data["runner"] = {"kind": "telepathy"}
    result = m.validate_manifest(write_resident(data))
    assert "runner.kind" in field_paths(result)


def test_runner_defaults_to_claude(write_resident: ResidentWriter) -> None:
    data = valid_manifest()
    del data["runner"]
    resident = m.load_manifest(write_resident(data))
    assert resident.manifest.runner.kind == "claude"
    assert resident.manifest.runner.model is None


# -------------------------------------------------------------------------- broken files


def test_invalid_yaml_fails(tmp_path: Path) -> None:
    path = tmp_path / "manifest.yaml"
    path.write_text("id: [unclosed\n", encoding="utf-8")
    result = m.validate_manifest(path)
    assert "not valid YAML" in problem_for(result, "<file>")


def test_empty_manifest_fails(tmp_path: Path) -> None:
    path = tmp_path / "manifest.yaml"
    path.write_text("", encoding="utf-8")
    result = m.validate_manifest(path)
    assert problem_for(result, "<file>") == "manifest is empty"


def test_non_mapping_manifest_fails(tmp_path: Path) -> None:
    path = tmp_path / "manifest.yaml"
    path.write_text("- one\n- two\n", encoding="utf-8")
    result = m.validate_manifest(path)
    assert "mapping of fields" in problem_for(result, "<root>")


def test_unreadable_manifest_fails(tmp_path: Path) -> None:
    result = m.validate_manifest(tmp_path)
    assert "cannot read manifest" in problem_for(result, "<file>")


# ---------------------------------------------------------------------------- tree walking


def test_validate_tree_reports_every_resident(
    write_resident: ResidentWriter, tmp_path: Path
) -> None:
    write_resident()
    second = valid_manifest()
    second["id"] = "other-agent"
    second["agent_id"] = "claude-code:other-agent"
    soul = VALID_SOUL.replace("test-agent", "other-agent")
    write_resident(second, soul=soul)
    result = m.validate_tree(tmp_path / "residents")
    assert result.ok
    assert {resident.id for resident in result.residents} == {"test-agent", "other-agent"}


def test_validate_tree_collects_failures(write_resident: ResidentWriter, tmp_path: Path) -> None:
    write_resident()
    broken = valid_manifest()
    broken["id"] = "broken-agent"
    del broken["memory"]
    write_resident(broken)
    result = m.validate_tree(tmp_path / "residents")
    assert not result.ok
    assert len(result.residents) == 1
    assert "memory" in field_paths(result)


def test_validate_tree_flags_a_directory_without_a_manifest(tmp_path: Path) -> None:
    (tmp_path / "residents" / "empty-agent").mkdir(parents=True)
    result = m.validate_tree(tmp_path / "residents")
    assert "no manifest.yaml" in problem_for(result, "<directory>")


def test_validate_tree_on_a_missing_directory_fails(tmp_path: Path) -> None:
    result = m.validate_tree(tmp_path / "nope")
    assert "does not exist" in problem_for(result, "<path>")


def test_validate_tree_on_an_empty_directory_warns_only(tmp_path: Path) -> None:
    (tmp_path / "residents").mkdir()
    result = m.validate_tree(tmp_path / "residents")
    assert result.ok
    assert result.warnings
    assert result.warnings[0].severity is m.Severity.WARNING


def test_validate_path_accepts_file_directory_or_tree(
    write_resident: ResidentWriter, tmp_path: Path
) -> None:
    manifest_path = write_resident()
    assert m.validate_path(manifest_path).ok
    assert m.validate_path(manifest_path.parent).ok
    assert m.validate_path(tmp_path / "residents").ok


def test_validate_paths_merges_results(write_resident: ResidentWriter, tmp_path: Path) -> None:
    manifest_path = write_resident()
    broken = tmp_path / "broken"
    broken.mkdir()
    (broken / "manifest.yaml").write_text("id: [unclosed\n", encoding="utf-8")
    result = m.validate_paths([manifest_path, broken / "manifest.yaml"])
    assert not result.ok
    assert len(result.residents) == 1
    assert len(result.errors) == 1


# ------------------------------------------------------------------------- library surface


def test_diagnostic_renders_all_four_facts(tmp_path: Path) -> None:
    diagnostic = m.Diagnostic(
        file=tmp_path / "manifest.yaml",
        field_path="charter.mission",
        problem="required field is missing",
        example="mission: Keep the household running.",
    )
    rendered = str(diagnostic)
    assert "manifest.yaml" in rendered
    assert "charter.mission" in rendered
    assert "required field is missing" in rendered
    assert "mission: Keep the household running." in rendered
    assert "error" in rendered


def test_validation_result_merging_is_ordered(tmp_path: Path) -> None:
    warning = m.Diagnostic(tmp_path, "a", "p", "e", m.Severity.WARNING)
    error = m.Diagnostic(tmp_path, "b", "p", "e")
    merged = m.ValidationResult(diagnostics=(warning,)).merged_with(
        m.ValidationResult(diagnostics=(error,))
    )
    assert merged.diagnostics == (warning, error)
    assert merged.warnings == (warning,)
    assert merged.errors == (error,)
    assert not merged.ok


def test_json_schema_covers_every_dimension() -> None:
    schema = m.manifest_json_schema()
    assert schema["title"] == "steward resident manifest v0"
    assert "$id" in schema
    for dimension in ("soul", "charter", "skills", "memory", "routes", "app_grants", "runner"):
        assert dimension in schema["properties"]
    assert set(schema["required"]) >= {"id", "soul", "charter", "skills", "memory", "routes"}


def test_scan_for_credentials_walks_lists(tmp_path: Path) -> None:
    data = yaml.safe_load("grants:\n  - name: gmail\n    api_key: abc\n")
    diagnostics = m.scan_for_credentials(data, tmp_path / "manifest.yaml")
    assert [d.field_path for d in diagnostics] == ["grants[0].api_key"]
