"""CLI behavior: skills."""

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from conftest import (
    REPO_ROOT,
    ResidentWriter,
    valid_manifest,
)
from steward.cli import main
from support.cli import (
    runner as runner,  # noqa: PLC0414 — pytest fixture discovery
)

# --------------------------------------------------------------------------------------
# skills
# --------------------------------------------------------------------------------------


def test_skills_lists_the_shipped_library_and_every_resident(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(REPO_ROOT)
    result = runner.invoke(main, ["skills"])
    assert result.exit_code == 0, result.output
    assert "write-journal  [default]" in result.output
    assert "read-inbox  [granted]" in result.output
    assert "hob: escalate, write-journal, vault-keeper, morning-digest" in (result.output)
    assert "burrow-builder" not in result.output
    # Named as a copy the CLI does not discover: since steward #206 a claude session is
    # launched with `--setting-sources ""`, and `.claude/skills` is discovered through the
    # project setting source. The prompt is the delivery path; printing two working
    # channels here would be the claim that is no longer true.
    assert "a copy in .claude/skills/ the session's CLI does not discover" in result.output


def test_skills_reports_json(runner: CliRunner, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(REPO_ROOT)
    result = runner.invoke(main, ["skills", "--format", "json"])
    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["library"].endswith("skills")
    assert {"errands", "escalate"} <= {skill["name"] for skill in payload["skills"]}
    assert "vault-keeper" in payload["residents"]["hob"]
    assert payload["diagnostics"] == []


def test_skills_says_so_when_there_is_no_library(
    runner: CliRunner, write_resident: ResidentWriter, tmp_path: Path
) -> None:
    write_resident()
    result = runner.invoke(main, ["skills", "--residents", str(tmp_path / "residents")])
    assert result.exit_code == 0
    assert "no skills library found" in result.output
    assert "test-agent: none" in result.output


def test_skills_exits_non_zero_on_a_broken_library(
    runner: CliRunner, write_resident: ResidentWriter, write_skill, tmp_path: Path
) -> None:
    write_skill("broken", text="---\nname: broken\n---\n\nNo description.\n")
    write_resident()
    result = runner.invoke(main, ["skills", "--residents", str(tmp_path / "residents")])
    assert result.exit_code == 1
    assert "description" in result.output


def test_skills_takes_an_explicit_library(
    runner: CliRunner, write_resident: ResidentWriter, write_skill, tmp_path: Path
) -> None:
    write_skill("errands", root=tmp_path / "other-library", defaults=True)
    data = valid_manifest()
    data["skills"] = []
    data["routines"] = []
    write_resident(data)
    result = runner.invoke(
        main,
        [
            "skills",
            "--residents",
            str(tmp_path / "residents"),
            "--skills",
            str(tmp_path / "other-library"),
        ],
    )
    assert result.exit_code == 0
    assert "test-agent: errands" in result.output


def test_validate_takes_an_explicit_library(
    runner: CliRunner, write_resident: ResidentWriter, write_skill, tmp_path: Path
) -> None:
    write_skill("read-inbox", root=tmp_path / "other-library")
    data = valid_manifest()
    data["skills"] = ["errands"]
    data["routines"] = []
    manifest_path = write_resident(data)
    result = runner.invoke(
        main, ["validate", str(manifest_path), "--skills", str(tmp_path / "other-library")]
    )
    assert result.exit_code == 1
    assert "not in the skills library" in result.output
