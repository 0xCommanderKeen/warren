"""CLI behavior: retirement."""

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from conftest import (
    ScratchRepo,
)
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


def test_retire_stops_the_container_and_commits_the_decision(
    runner: CliRunner, scratch_repo: ScratchRepo, charter_file: Path, nas: LocalTransport
) -> None:
    runner.invoke(main, new_resident_argv(scratch_repo, charter_file))

    result = runner.invoke(
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

    assert result.exit_code == 0, result.output
    assert "note-keeper is retired" in result.output
    assert "resident_retired was emitted under the resident's identity" in result.output
    assert scratch_repo.log()[0] == "chore(residents): retire note-keeper"
    assert nas.calls[-2][-2:] == ("down", "--remove-orphans")
    # …and the token goes with it, once the container that was reading it is gone (#157).
    assert nas.calls[-1][:2] == ("rm", "-f")
    assert "claude/" in result.output


def test_retire_no_deploy_marks_and_commits_but_reaches_no_host(
    runner: CliRunner, scratch_repo: ScratchRepo, charter_file: Path, nas: LocalTransport
) -> None:
    """--no-deploy is the host-less path: the resident stops, but no ssh is run (#90)."""
    runner.invoke(main, new_resident_argv(scratch_repo, charter_file))
    nas.calls.clear()

    result = runner.invoke(
        main,
        [
            "retire",
            "note-keeper",
            "--residents",
            str(scratch_repo.residents),
            "--repo",
            str(scratch_repo.root),
            "--no-deploy",
        ],
    )

    assert result.exit_code == 0, result.output
    assert "note-keeper is retired" in result.output
    assert nas.calls == [], "no host was reached"
    assert scratch_repo.log()[0] == "chore(residents): retire note-keeper"


def test_retire_dry_run_changes_nothing(
    runner: CliRunner, scratch_repo: ScratchRepo, charter_file: Path, nas: LocalTransport
) -> None:
    runner.invoke(main, new_resident_argv(scratch_repo, charter_file))
    before = scratch_repo.head()
    nas.calls.clear()

    result = runner.invoke(
        main,
        [
            "retire",
            "note-keeper",
            "--residents",
            str(scratch_repo.residents),
            "--repo",
            str(scratch_repo.root),
            "--dry-run",
        ],
    )

    assert result.exit_code == 0, result.output
    assert "nothing was stopped, marked, or committed" in result.output
    assert scratch_repo.head() == before
    assert nas.calls == []


@pytest.mark.usefixtures("nas")
def test_retire_reports_json_when_asked(
    runner: CliRunner,
    scratch_repo: ScratchRepo,
    charter_file: Path,
    nas: LocalTransport,  # noqa: ARG001 — the fixture is the setup
) -> None:
    runner.invoke(main, new_resident_argv(scratch_repo, charter_file))

    result = runner.invoke(
        main,
        [
            "retire",
            "note-keeper",
            "--residents",
            str(scratch_repo.residents),
            "--repo",
            str(scratch_repo.root),
            "--format",
            "json",
        ],
    )

    payload = json.loads(result.output)
    assert payload["marked"] is True
    assert payload["stopped"] is True


@pytest.mark.usefixtures("nas")
def test_retiring_a_resident_nobody_declared_exits_non_zero(
    runner: CliRunner,
    scratch_repo: ScratchRepo,
    nas: LocalTransport,  # noqa: ARG001 — the fixture is the setup
) -> None:
    result = runner.invoke(
        main,
        [
            "retire",
            "ghost",
            "--residents",
            str(scratch_repo.residents),
            "--repo",
            str(scratch_repo.root),
        ],
    )

    assert result.exit_code == 1
    assert "no resident 'ghost'" in result.output
