"""CLI behavior: validation."""

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from conftest import (
    REPO_ROOT,
    VALID_RESIDENT_UID,
    ResidentWriter,
    valid_manifest,
)
from steward import cli
from steward.cli import main
from support.cli import (
    runner as runner,  # noqa: PLC0414 — pytest fixture discovery
)


def test_validate_defaults_to_the_residents_tree(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(REPO_ROOT)
    result = runner.invoke(main, ["validate"])
    assert result.exit_code == 0, result.output
    assert "2 valid resident(s)" in result.output


def test_validate_accepts_explicit_paths(runner: CliRunner, write_resident: ResidentWriter) -> None:
    manifest_path = write_resident()
    result = runner.invoke(main, ["validate", str(manifest_path)])
    assert result.exit_code == 0
    assert "ok:" in result.output


def test_validate_exits_non_zero_on_error(
    runner: CliRunner, write_resident: ResidentWriter
) -> None:
    data = valid_manifest()
    del data["app_grants"]
    manifest_path = write_resident(data)
    result = runner.invoke(main, ["validate", str(manifest_path)])
    assert result.exit_code == 1
    assert "app_grants" in result.output
    assert "required field is missing" in result.output
    assert "failed:" in result.output


def test_validate_reports_invalid_utf8_without_a_traceback(
    runner: CliRunner, tmp_path: Path
) -> None:
    manifest_path = tmp_path / "manifest.yaml"
    manifest_path.write_bytes(b"\xff\xfe")

    result = runner.invoke(main, ["validate", str(manifest_path)])

    assert result.exit_code == 1
    assert isinstance(result.exception, SystemExit)
    assert "manifest is not valid UTF-8" in result.output


def test_validate_reports_json(runner: CliRunner, write_resident: ResidentWriter) -> None:
    data = valid_manifest()
    del data["memory"]
    manifest_path = write_resident(data)
    result = runner.invoke(main, ["validate", "--format", "json", str(manifest_path)])
    assert result.exit_code == 1
    payload = json.loads(result.output)
    assert payload["ok"] is False
    assert payload["residents"] == []
    assert payload["diagnostics"][0]["field_path"] == "memory"
    assert payload["diagnostics"][0]["severity"] == "error"
    assert payload["diagnostics"][0]["example"]


def test_validate_json_lists_valid_residents(
    runner: CliRunner, write_resident: ResidentWriter
) -> None:
    manifest_path = write_resident()
    result = runner.invoke(main, ["validate", "--format", "json", str(manifest_path)])
    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["residents"][0]["uid"] == VALID_RESIDENT_UID
    assert payload["residents"][0]["agent_id"] == "claude-code:test-agent"


def test_validate_with_no_path_fails_when_it_found_nothing(
    runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The merge gate must not pass green having validated nothing (steward #137).

    CI runs a bare ``uv run steward validate``, which falls back to a *relative*
    ``residents``, resolved against the process cwd. Rename the tree, move it, or change
    the job's working directory and this used to print ``ok: 0 valid resident(s)`` and
    exit 0 — the step whose stated purpose is that an invalid manifest must never merge,
    reporting success without reading a manifest.
    """
    (tmp_path / "residents").mkdir()
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(main, ["validate"])

    assert result.exit_code == 1, result.output
    assert "failed:" in result.output
    assert "this run validated nothing" in result.output


def test_validate_with_no_path_names_the_tree_it_actually_looked_in(
    runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A green run in the wrong directory should be visible in the log by its path."""
    (tmp_path / "residents").mkdir()
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(main, ["validate"])

    assert str((tmp_path / "residents").resolve()) in result.output


def test_validate_with_no_path_reports_the_failure_as_json_too(
    runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Both reporters read the same result, so neither can disagree about ``ok``."""
    (tmp_path / "residents").mkdir()
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(main, ["validate", "--format", "json"])

    assert result.exit_code == 1
    payload = json.loads(result.output)
    assert payload["ok"] is False
    assert payload["diagnostics"][0]["severity"] == "error"


def test_validate_with_no_path_fails_even_if_the_empty_tree_warning_is_reworded(
    runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The gate keys on the count, not on the wording (steward #137).

    An earlier draft promoted the diagnostic whose ``problem`` matched
    ``NO_MANIFESTS_PROBLEM`` exactly, which fails *open*: reword that string at the
    source, or reach zero residents down a path that words it differently, and the
    promotion matches nothing and CI silently goes back to exiting 0 on a run that
    validated nothing. A merge gate has to fail closed.
    """
    (tmp_path / "residents").mkdir()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli, "NO_MANIFESTS_PROBLEM", "something else entirely")

    result = runner.invoke(main, ["validate"])

    assert result.exit_code == 1, result.output
    assert "failed:" in result.output


def test_validate_on_a_named_empty_tree_is_still_only_a_warning(
    runner: CliRunner, tmp_path: Path
) -> None:
    """Asking about an empty directory is a fair question, and gets a fair answer.

    Only the *defaulted* run is held to "you must have found something": naming a tree is
    a deliberate act, and ``steward validate ./drafts`` before anything is drafted is not
    a failure.
    """
    empty = tmp_path / "drafts"
    empty.mkdir()

    result = runner.invoke(main, ["validate", str(empty)])

    assert result.exit_code == 0, result.output
    assert "ok:" in result.output
    assert "warning" in result.output


def test_validate_rejects_a_missing_path(runner: CliRunner, tmp_path: Path) -> None:
    result = runner.invoke(main, ["validate", str(tmp_path / "nope")])
    assert result.exit_code == 2


def test_validate_multiple_targets(runner: CliRunner, write_resident: ResidentWriter) -> None:
    first = write_resident()
    second_data = valid_manifest()
    second_data["id"] = "other-agent"
    second = write_resident(second_data, soul=None)
    result = runner.invoke(main, ["validate", str(first.parent), str(second.parent)])
    assert result.exit_code == 1
    assert "soul.file" in result.output


def test_schema_command_emits_json_schema(runner: CliRunner) -> None:
    result = runner.invoke(main, ["schema"])
    assert result.exit_code == 0
    schema = json.loads(result.output)
    assert schema["title"] == "steward resident manifest v0"


def test_schema_output_writes_exactly_what_stdout_prints(runner: CliRunner, tmp_path: Path) -> None:
    """`make schema-write` regenerates the committed artifact through this flag.

    Byte-identical to stdout, or the committed copy and the printed one would be two
    different contracts and tests/test_schema_contract.py would fail for no real reason.
    """
    target = tmp_path / "nested" / "resident-manifest-v0.json"
    printed = runner.invoke(main, ["schema"])
    written = runner.invoke(main, ["schema", "--output", str(target)])

    assert written.exit_code == 0, written.output
    assert not written.output, "--output writes the file; it does not also print it"
    assert target.read_text(encoding="utf-8") == printed.output
    assert printed.output.endswith("}\n")


def test_openapi_command_emits_the_document_the_api_serves_to_nobody(runner: CliRunner) -> None:
    """The offline export that stands in for the schema route steward refuses to serve."""
    result = runner.invoke(main, ["openapi"])
    assert result.exit_code == 0
    document = json.loads(result.output)
    assert document["info"]["title"] == "steward"
    assert "/residents" in document["paths"]


def test_openapi_output_writes_exactly_what_stdout_prints(
    runner: CliRunner, tmp_path: Path
) -> None:
    """`make openapi-write` regenerates the committed artifact through this flag."""
    target = tmp_path / "nested" / "openapi.json"
    printed = runner.invoke(main, ["openapi"])
    written = runner.invoke(main, ["openapi", "--output", str(target)])

    assert written.exit_code == 0, written.output
    assert not written.output, "--output writes the file; it does not also print it"
    assert target.read_text(encoding="utf-8") == printed.output
    assert printed.output.endswith("}\n")


def test_help_lists_the_commands(runner: CliRunner) -> None:
    result = runner.invoke(main, ["--help"])
    assert result.exit_code == 0
    for command in ("validate", "schema", "openapi", "doctor", "scheduler", "show"):
        assert command in result.output
