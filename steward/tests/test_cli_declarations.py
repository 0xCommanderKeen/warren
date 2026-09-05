"""CLI behavior: declarations."""

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from click.testing import CliRunner

from conftest import (
    REPO_ROOT,
    ScratchRepo,
)
from steward import cli
from steward.cli import main
from steward.deploy import LocalTransport, TransportError
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


def test_the_charter_example_is_a_charter_steward_accepts(
    runner: CliRunner, scratch_repo: ScratchRepo, tmp_path: Path, nas: LocalTransport
) -> None:
    """The example a refusal prints is the only spec `--charter` has (warren#90).

    An operator meets it at the moment they got the format wrong, so copying it has to
    produce a charter the validator takes. An example that drifted would document the
    file format wrongly, which is worse than not documenting it at all.
    """
    charter = tmp_path / "from-the-example.yaml"
    charter.write_text(cli.CHARTER_EXAMPLE, encoding="utf-8")

    result = runner.invoke(main, new_resident_argv(scratch_repo, charter, "--dry-run"))

    assert result.exit_code == 0, result.output
    assert not nas.touched


def test_the_readme_carries_the_cli_s_charter_example_verbatim() -> None:
    """The README's charter block is a copy, and a copy is a thing that drifts.

    Both are the documentation of `--charter`, so they have to be the same bytes: the
    test above proves one of them works, and this is what makes that cover the other.
    """
    readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
    assert cli.CHARTER_EXAMPLE in readme, "README's charter block has drifted from the CLI's"


def test_new_resident_raises_a_resident_end_to_end(
    runner: CliRunner, scratch_repo: ScratchRepo, charter_file: Path, nas: LocalTransport
) -> None:
    result = runner.invoke(main, new_resident_argv(scratch_repo, charter_file))

    assert result.exit_code == 0, result.output
    assert "note-keeper is raised" in result.output
    assert (scratch_repo.residents / "note-keeper" / "soul.md").is_file()
    assert scratch_repo.log()[0] == "feat(residents): declare note-keeper"
    assert (
        nas.root / "docker" / "warren" / "residents" / "note-keeper" / "docker-compose.yaml"
    ).is_file()


@pytest.mark.usefixtures("nas")
def test_new_resident_is_a_no_op_the_second_time(
    runner: CliRunner,
    scratch_repo: ScratchRepo,
    charter_file: Path,
    nas: LocalTransport,  # noqa: ARG001 — the fixture is the setup
) -> None:
    runner.invoke(main, new_resident_argv(scratch_repo, charter_file))
    commits = scratch_repo.log()

    result = runner.invoke(main, new_resident_argv(scratch_repo, charter_file))

    assert result.exit_code == 0, result.output
    assert "converged" in result.output
    assert scratch_repo.log() == commits


def test_new_resident_dry_run_prints_the_plan_and_changes_nothing(
    runner: CliRunner, scratch_repo: ScratchRepo, charter_file: Path, nas: LocalTransport
) -> None:
    result = runner.invoke(main, new_resident_argv(scratch_repo, charter_file, "--dry-run"))

    assert result.exit_code == 0, result.output
    assert "plan for note-keeper" in result.output
    assert "docker compose" in result.output
    assert "nothing was written, sent, or committed" in result.output
    assert not (scratch_repo.residents / "note-keeper").exists()
    assert not nas.touched


@pytest.mark.usefixtures("nas")
def test_new_resident_reports_json_when_asked(
    runner: CliRunner,
    scratch_repo: ScratchRepo,
    charter_file: Path,
    nas: LocalTransport,  # noqa: ARG001 — the fixture is the setup
) -> None:
    result = runner.invoke(main, new_resident_argv(scratch_repo, charter_file, "--format", "json"))

    payload = json.loads(result.output)
    assert payload["resident"] == "note-keeper"
    assert payload["provision"]["target"]["container"] == "steward-note-keeper"
    assert "cli-village-token" not in result.output


@pytest.mark.parametrize("output_format", ["text", "json"])
def test_new_resident_register_problems_exit_non_zero(
    runner: CliRunner,
    scratch_repo: ScratchRepo,
    charter_file: Path,
    monkeypatch: pytest.MonkeyPatch,
    output_format: str,
) -> None:
    report = SimpleNamespace(
        register=SimpleNamespace(problems=("claude is not on PATH",)),
        dry_run=False,
        changed=True,
        resident_id="note-keeper",
        render=lambda: ["raised note-keeper", "register", "  claude is not on PATH"],
        to_dict=lambda: {
            "resident": "note-keeper",
            "register": {"ok": False, "problems": ["claude is not on PATH"]},
        },
    )
    monkeypatch.setattr(cli, "raise_resident", lambda *_args, **_kwargs: report)

    result = runner.invoke(
        main,
        new_resident_argv(
            scratch_repo, charter_file, "--format", output_format, "--no-deploy", "--no-commit"
        ),
    )

    assert result.exit_code == 1, result.output


def test_new_resident_can_skip_the_container_entirely(
    runner: CliRunner, scratch_repo: ScratchRepo, charter_file: Path, nas: LocalTransport
) -> None:
    result = runner.invoke(main, new_resident_argv(scratch_repo, charter_file, "--no-deploy"))

    assert result.exit_code == 0, result.output
    assert not nas.touched
    assert scratch_repo.log()[0] == "feat(residents): declare note-keeper"


@pytest.mark.usefixtures("nas")
def test_new_resident_can_skip_the_commit(
    runner: CliRunner,
    scratch_repo: ScratchRepo,
    charter_file: Path,
    nas: LocalTransport,  # noqa: ARG001 — the fixture is the setup
) -> None:
    result = runner.invoke(main, new_resident_argv(scratch_repo, charter_file, "--no-commit"))

    assert result.exit_code == 0, result.output
    assert scratch_repo.log() == ["chore: scratch repo"]
    assert (scratch_repo.residents / "note-keeper" / "manifest.yaml").is_file()


def test_new_resident_refuses_a_dirty_worktree(
    runner: CliRunner, scratch_repo: ScratchRepo, charter_file: Path, nas: LocalTransport
) -> None:
    (scratch_repo.root / "scratch.txt").write_text("mid-thought\n", encoding="utf-8")

    result = runner.invoke(main, new_resident_argv(scratch_repo, charter_file))

    assert result.exit_code == 1
    assert "uncommitted changes" in result.output
    assert not nas.touched


@pytest.mark.usefixtures("nas")
def test_new_resident_needs_a_charter(
    runner: CliRunner,
    scratch_repo: ScratchRepo,
    nas: LocalTransport,  # noqa: ARG001 — the fixture is the setup
) -> None:
    result = runner.invoke(
        main,
        [
            "new-resident",
            "--id",
            "note-keeper",
            "--name",
            "Quill",
            "--char",
            "Scribe",
            "--accent",
            "#4f7ea6",
            "--role",
            "note bot",
            "--residents",
            str(scratch_repo.residents),
            "--repo",
            str(scratch_repo.root),
        ],
    )

    assert result.exit_code == 1
    assert "--charter is required" in result.output


@pytest.mark.usefixtures("nas")
def test_a_charter_that_is_not_a_charter_says_what_one_looks_like(
    runner: CliRunner,
    scratch_repo: ScratchRepo,
    tmp_path: Path,
    nas: LocalTransport,  # noqa: ARG001 — the fixture is the setup
) -> None:
    charter = tmp_path / "charter.yaml"
    charter.write_text("- just a list\n", encoding="utf-8")

    result = runner.invoke(main, new_resident_argv(scratch_repo, charter))

    assert result.exit_code == 1
    assert "mission" in result.output


@pytest.mark.usefixtures("nas")
def test_a_charter_that_is_not_yaml_at_all_is_named(
    runner: CliRunner,
    scratch_repo: ScratchRepo,
    tmp_path: Path,
    nas: LocalTransport,  # noqa: ARG001 — the fixture is the setup
) -> None:
    charter = tmp_path / "charter.yaml"
    charter.write_text("mission: [unclosed\n", encoding="utf-8")

    result = runner.invoke(main, new_resident_argv(scratch_repo, charter))

    assert result.exit_code == 1
    assert "cannot read the charter" in result.output


@pytest.mark.usefixtures("nas")
def test_a_spec_that_cannot_bind_to_the_schema_names_the_field(
    runner: CliRunner,
    scratch_repo: ScratchRepo,
    charter_file: Path,
    nas: LocalTransport,  # noqa: ARG001 — the fixture is the setup
) -> None:
    result = runner.invoke(
        main, new_resident_argv(scratch_repo, charter_file, "--accent", "not-a-colour")
    )

    assert result.exit_code == 1
    assert "accent" in result.output


def test_new_resident_reports_a_transport_failure_cleanly(
    runner: CliRunner, tmp_path: Path, charter_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A host that will not answer is an operator problem, not a traceback (#90)."""

    def boom(*_args: object, **_kwargs: object) -> object:
        raise TransportError("no route to dxp2800")

    monkeypatch.setattr("steward.cli.raise_resident", boom)
    residents_dir = tmp_path / "residents"
    residents_dir.mkdir()

    result = runner.invoke(
        main,
        [
            "new-resident",
            "--id",
            "note-keeper",
            "--name",
            "Quill",
            "--char",
            "Scribe",
            "--accent",
            "#4f7ea6",
            "--role",
            "note bot",
            "--charter",
            str(charter_file),
            "--residents",
            str(residents_dir),
        ],
    )
    assert result.exit_code == 1
    assert "could not reach the host" in result.output
    assert "Traceback" not in result.output
