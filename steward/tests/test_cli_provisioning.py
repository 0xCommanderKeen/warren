"""CLI behavior: provisioning."""

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml
from click.testing import CliRunner

from conftest import (
    ScratchRepo,
)
from steward import cli
from steward.cli import main
from steward.deploy import LocalTransport
from support.cli import (
    charter_file as charter_file,  # noqa: PLC0414 — pytest fixture discovery
)
from support.cli import (
    nas as nas,  # noqa: PLC0414 — pytest fixture discovery
)
from support.cli import (
    new_resident_argv,
)
from support.cli import (
    runner as runner,  # noqa: PLC0414 — pytest fixture discovery
)

# ------------------------------------- `steward provision`: the manifest is the source


def hand_write_manifest(repo: ScratchRepo, resident_id: str = "note-keeper") -> Path:
    """Give a declared resident an app grant no `new-resident` flag can say, and commit it."""
    path = repo.residents / resident_id / "manifest.yaml"
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    data["app_grants"] = [{"id": "gmail", "name": "Gmail", "status": "granted"}]
    path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    repo.git("commit", "-am", f"feat(residents): grant {resident_id} Gmail")
    return path


def provision_argv(repo: ScratchRepo, *extra: str) -> list[str]:
    """Build the provision command line, so each test varies only what it is about."""
    return [
        "provision",
        "note-keeper",
        "--residents",
        str(repo.residents),
        "--repo",
        str(repo.root),
        *extra,
    ]


def test_provision_builds_a_manifest_new_resident_would_refuse(
    runner: CliRunner, scratch_repo: ScratchRepo, charter_file: Path, nas: LocalTransport
) -> None:
    """The command #270 asked for: the declaration is the source of truth, not the flags."""
    runner.invoke(main, new_resident_argv(scratch_repo, charter_file, "--no-deploy"))
    hand_write_manifest(scratch_repo)
    assert runner.invoke(main, new_resident_argv(scratch_repo, charter_file)).exit_code == 1

    result = runner.invoke(main, provision_argv(scratch_repo))

    assert result.exit_code == 0, result.output
    assert "note-keeper is provisioned" in result.output
    assert (
        nas.root / "docker" / "warren" / "residents" / "note-keeper" / "docker-compose.yaml"
    ).is_file()


def test_the_refusal_new_resident_gives_names_the_command_that_works(
    runner: CliRunner, scratch_repo: ScratchRepo, charter_file: Path, nas: LocalTransport
) -> None:
    runner.invoke(main, new_resident_argv(scratch_repo, charter_file, "--no-deploy"))
    hand_write_manifest(scratch_repo)

    result = runner.invoke(main, new_resident_argv(scratch_repo, charter_file))

    assert result.exit_code == 1
    assert "steward provision note-keeper" in result.output
    assert not nas.touched


@pytest.mark.usefixtures("nas")
def test_provision_commits_nothing_and_does_not_mind_a_dirty_worktree(
    runner: CliRunner,
    scratch_repo: ScratchRepo,
    charter_file: Path,
    nas: LocalTransport,  # noqa: ARG001 — the fixture is the setup
) -> None:
    """There is no commit to protect, so there is no dirty-worktree refusal to make."""
    runner.invoke(main, new_resident_argv(scratch_repo, charter_file, "--no-deploy"))
    commits = scratch_repo.log()
    (scratch_repo.root / "scratch.txt").write_text("mid-thought\n", encoding="utf-8")

    result = runner.invoke(main, provision_argv(scratch_repo))

    assert result.exit_code == 0, result.output
    assert scratch_repo.log() == commits


@pytest.mark.usefixtures("nas")
def test_provision_says_out_loud_when_it_is_building_uncommitted_bytes(
    runner: CliRunner,
    scratch_repo: ScratchRepo,
    charter_file: Path,
    nas: LocalTransport,  # noqa: ARG001 — the fixture is the setup
) -> None:
    runner.invoke(main, new_resident_argv(scratch_repo, charter_file, "--no-deploy"))
    path = scratch_repo.residents / "note-keeper" / "manifest.yaml"
    path.write_text(path.read_text(encoding="utf-8") + "summary: uncommitted\n", encoding="utf-8")

    result = runner.invoke(main, provision_argv(scratch_repo))

    assert result.exit_code == 0, result.output
    assert "is not committed" in result.output


def test_provision_dry_run_prints_the_plan_and_touches_nothing(
    runner: CliRunner, scratch_repo: ScratchRepo, charter_file: Path, nas: LocalTransport
) -> None:
    runner.invoke(main, new_resident_argv(scratch_repo, charter_file, "--no-deploy"))

    result = runner.invoke(main, provision_argv(scratch_repo, "--dry-run"))

    assert result.exit_code == 0, result.output
    assert "plan for note-keeper" in result.output
    assert "docker compose" in result.output
    assert "nothing was written, sent, or committed" in result.output
    assert not nas.touched


@pytest.mark.usefixtures("nas")
def test_provision_reports_json_when_asked(
    runner: CliRunner,
    scratch_repo: ScratchRepo,
    charter_file: Path,
    nas: LocalTransport,  # noqa: ARG001 — the fixture is the setup
) -> None:
    runner.invoke(main, new_resident_argv(scratch_repo, charter_file, "--no-deploy"))

    result = runner.invoke(main, provision_argv(scratch_repo, "--format", "json"))

    payload = json.loads(result.output)
    assert payload["act"] == "provision"
    assert payload["declare"]["written"] is False
    assert payload["provision"]["target"]["container"] == "steward-note-keeper"
    assert "cli-village-token" not in result.output


@pytest.mark.usefixtures("nas")
def test_provision_is_a_no_op_the_second_time(
    runner: CliRunner,
    scratch_repo: ScratchRepo,
    charter_file: Path,
    nas: LocalTransport,  # noqa: ARG001 — the fixture is the setup
) -> None:
    runner.invoke(main, new_resident_argv(scratch_repo, charter_file))

    result = runner.invoke(main, provision_argv(scratch_repo))

    assert result.exit_code == 0, result.output
    assert "converged" in result.output


@pytest.mark.usefixtures("nas")
def test_provisioning_an_unknown_resident_suggests_the_one_you_meant(
    runner: CliRunner,
    scratch_repo: ScratchRepo,
    charter_file: Path,
    nas: LocalTransport,  # noqa: ARG001 — the fixture is the setup
) -> None:
    runner.invoke(main, new_resident_argv(scratch_repo, charter_file, "--no-deploy"))

    result = runner.invoke(
        main,
        ["provision", "note-keper", "--residents", str(scratch_repo.residents)],
    )

    assert result.exit_code == 1
    assert "did you mean 'note-keeper'" in result.output


def test_provisioning_a_retired_resident_is_refused(
    runner: CliRunner, scratch_repo: ScratchRepo, charter_file: Path, nas: LocalTransport
) -> None:
    runner.invoke(main, new_resident_argv(scratch_repo, charter_file))
    runner.invoke(
        main,
        [
            "retire",
            "note-keeper",
            "--residents",
            str(scratch_repo.residents),
            "--repo",
            str(scratch_repo.root),
        ],
    )
    nas.calls.clear()

    result = runner.invoke(main, provision_argv(scratch_repo))

    assert result.exit_code == 1
    assert "is retired" in result.output
    assert not nas.calls


def test_provisioning_with_nowhere_to_emit_is_one_line_not_a_traceback(
    runner: CliRunner,
    scratch_repo: ScratchRepo,
    charter_file: Path,
    nas: LocalTransport,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner.invoke(main, new_resident_argv(scratch_repo, charter_file, "--no-deploy"))
    monkeypatch.delenv("CHRONICLE_URL", raising=False)

    result = runner.invoke(main, provision_argv(scratch_repo))

    assert result.exit_code == 1
    assert "could not provision note-keeper" in result.output
    assert "CHRONICLE_URL" in result.output
    assert not nas.touched


@pytest.mark.usefixtures("nas")
def test_provisioning_a_broken_manifest_prints_the_diagnostics(
    runner: CliRunner,
    scratch_repo: ScratchRepo,
    charter_file: Path,
    nas: LocalTransport,  # noqa: ARG001 — the fixture is the setup
) -> None:
    """The field-by-field diagnostics, not just "it does not validate"."""
    runner.invoke(main, new_resident_argv(scratch_repo, charter_file, "--no-deploy"))
    path = scratch_repo.residents / "note-keeper" / "manifest.yaml"
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    data["soul"]["accent"] = "not-a-colour"
    path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")

    result = runner.invoke(main, provision_argv(scratch_repo))

    assert result.exit_code == 1
    assert "does not validate" in result.output
    assert "accent" in result.output


@pytest.mark.parametrize("output_format", ["text", "json"])
def test_provision_register_problems_exit_non_zero(
    runner: CliRunner,
    scratch_repo: ScratchRepo,
    monkeypatch: pytest.MonkeyPatch,
    output_format: str,
) -> None:
    """The container is up and the schedule is not: a zero exit would say only the first."""
    report = SimpleNamespace(
        register=SimpleNamespace(problems=("claude is not on PATH",)),
        dry_run=False,
        changed=True,
        resident_id="note-keeper",
        verb="provisioned",
        render=lambda: ["provisioned note-keeper", "register", "  claude is not on PATH"],
        to_dict=lambda: {
            "resident": "note-keeper",
            "register": {"ok": False, "problems": ["claude is not on PATH"]},
        },
    )
    monkeypatch.setattr(cli, "provision_resident", lambda *_args, **_kwargs: report)

    result = runner.invoke(main, provision_argv(scratch_repo, "--format", output_format))

    assert result.exit_code == 1, result.output
