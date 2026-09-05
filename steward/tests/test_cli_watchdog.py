"""CLI behavior: watchdog."""

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from click.testing import CliRunner

from conftest import (
    ResidentWriter,
    StubWriter,
)
from steward import cli
from steward.cli import main
from steward.store import Store
from support.cli import (
    budgeted_manifest,
    docker_naming_itself,
    supervised_manifest,
)
from support.cli import (
    runner as runner,  # noqa: PLC0414 — pytest fixture discovery
)


def test_watchdog_tick_reports_a_quiet_pass(
    runner: CliRunner, write_resident: ResidentWriter, tmp_path: Path
) -> None:
    """A watchdog with nothing to do says so, and names what it could not see."""
    residents_dir = write_resident(budgeted_manifest()).parent.parent
    result = runner.invoke(
        main,
        ["watchdog", "tick", "--residents", str(residents_dir), "--db", str(tmp_path / "s.db")],
    )
    assert result.exit_code == 0, result.output
    # Nothing can actually see this resident's process today — no container is declared,
    # and steward's own state only ever proves stuckness — so it says so rather than
    # printing a green tick it has not earned.
    assert "test-agent: unsupervised" in result.output
    assert "nothing to intervene in" in result.output


def test_watchdog_tick_closes_a_run_that_never_reported_back(
    runner: CliRunner, write_resident: ResidentWriter, tmp_path: Path, monkeypatch
) -> None:
    residents_dir = write_resident(budgeted_manifest()).parent.parent
    log = tmp_path / "events.jsonl"
    monkeypatch.setenv("STEWARD_EVENTS_FALLBACK", str(log))
    log.write_text(
        json.dumps(
            {
                "v": 0,
                "ts": "2020-01-01T00:00:00.000Z",
                "source": "steward",
                "agent_id": "claude-code:test-agent",
                "project": "test-agent",
                "type": "routine_started",
                "payload": {"routine": "daily-summary", "run_id": "gone"},
            }
        )
        + "\n",
        encoding="utf-8",
    )

    result = runner.invoke(
        main,
        ["watchdog", "tick", "--residents", str(residents_dir), "--db", str(tmp_path / "s.db")],
    )

    assert result.exit_code == 1, result.output
    assert "closed run gone" in result.output
    emitted = [json.loads(line) for line in log.read_text().splitlines() if line.strip()]
    assert [e["type"] for e in emitted if e["payload"].get("run_id") == "gone"] == [
        "routine_started",
        "routine_failed",
    ]


def test_watchdog_tick_reports_json(
    runner: CliRunner, write_resident: ResidentWriter, tmp_path: Path
) -> None:
    residents_dir = write_resident(budgeted_manifest()).parent.parent
    result = runner.invoke(
        main,
        [
            "watchdog", "tick", "--residents", str(residents_dir),
            "--db", str(tmp_path / "s.db"), "--format", "json",
        ],
    )  # fmt: skip
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["interventions"] == 0
    assert payload["health"][0]["resident"] == "test-agent"


def test_watchdog_run_makes_the_passes_it_was_asked_for(
    runner: CliRunner, write_resident: ResidentWriter, tmp_path: Path
) -> None:
    residents_dir = write_resident(budgeted_manifest()).parent.parent
    db = tmp_path / "s.db"
    result = runner.invoke(
        main,
        [
            "watchdog", "run", "--residents", str(residents_dir),
            "--db", str(db), "--interval", "0", "--max-passes", "2",
        ],
    )  # fmt: skip
    assert result.exit_code == 0, result.output
    with Store(db) as store:
        last = store.last_watchdog_pass()
    assert last is not None
    assert last["passes"] == 2


@pytest.mark.parametrize("command", [("tick",), ("run", "--max-passes", "1")])
@pytest.mark.parametrize(("failure", "expected"), [(None, 0), ("gave_up", 1), ("paused", 1)])
def test_watchdog_commands_carry_pass_outcomes(  # noqa: PLR0913, PLR0917
    runner: CliRunner,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    command: tuple[str, ...],
    failure: str | None,
    expected: int,
) -> None:
    health = SimpleNamespace(resident_id="test-agent", detail="could not restart")
    report = SimpleNamespace(
        gave_up=(health,) if failure == "gave_up" else (),
        paused=("test-agent",) if failure == "paused" else (),
        restarted=(),
        buried=(),
        reopened=(),
        expired_approvals=(),
        health=(),
        __bool__=lambda: failure is not None,
    )
    # `topology` because the CLI asks the watchdog where it is before it asks it to work
    # (#59); an empty survey is the honest answer for a double with no residents.
    dog = SimpleNamespace(
        tick=lambda: report,
        run=lambda **_kwargs: [report],
        topology=lambda: cli.survey([]),
    )
    monkeypatch.setattr(cli.Watchdog, "from_path", lambda *_args, **_kwargs: dog)

    result = runner.invoke(
        main,
        ["watchdog", *command, "--residents", str(tmp_path), "--db", str(tmp_path / "s.db")],
    )

    assert result.exit_code == expected, result.output


def test_watchdog_daemon_interrupt_is_a_clean_operator_stop(
    runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def interrupted(**_kwargs: object) -> list[object]:
        raise KeyboardInterrupt

    monkeypatch.setattr(
        cli.Watchdog,
        "from_path",
        lambda *_args, **_kwargs: SimpleNamespace(run=interrupted, topology=lambda: cli.survey([])),
    )
    result = runner.invoke(
        main,
        ["watchdog", "run", "--residents", str(tmp_path), "--db", str(tmp_path / "s.db")],
    )
    assert result.exit_code == 0, result.output
    assert "stopped" in result.output


def test_the_watchdog_does_not_supervise_another_burrows_containers(
    runner: CliRunner,
    write_resident: ResidentWriter,
    stub_bin: StubWriter,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """The deploy.host partition keeps another burrow out of every watchdog pass."""
    stub_bin("docker", docker_naming_itself("laptop"))
    monkeypatch.setenv("STEWARD_BURROW", "laptop")
    residents_dir = write_resident(supervised_manifest(host="dxp2800")).parent.parent

    result = runner.invoke(
        main,
        ["watchdog", "tick", "--residents", str(residents_dir), "--db", str(tmp_path / "s.db")],
    )

    assert result.exit_code == 0, result.output
    assert "nothing here needs docker" in result.output
    assert "steward-test-agent" not in result.output


def test_the_watchdog_refuses_an_absent_local_container_once_per_pass(
    runner: CliRunner,
    write_resident: ResidentWriter,
    stub_bin: StubWriter,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    stub_bin(
        "docker",
        'case "$1" in info) printf "dxp2800\\t27.3.1\\n" ;; '
        'inspect) echo "No such container: steward-test-agent" >&2; exit 1 ;; esac',
    )
    monkeypatch.setenv("STEWARD_BURROW", "dxp2800")
    residents_dir = write_resident(supervised_manifest(host="dxp2800")).parent.parent

    result = runner.invoke(
        main,
        ["watchdog", "tick", "--residents", str(residents_dir), "--db", str(tmp_path / "s.db")],
    )

    refusal = "test-agent: refused: declared container 'steward-test-agent' is absent"
    assert result.exit_code == 0, result.output
    assert result.output.count(refusal) == 1
    assert "unsupervised" not in result.output


def test_the_watchdog_json_report_excludes_another_burrows_residents(
    runner: CliRunner,
    write_resident: ResidentWriter,
    stub_bin: StubWriter,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """The machine report contains only the residents this daemon is responsible for."""
    stub_bin("docker", docker_naming_itself("laptop"))
    monkeypatch.setenv("STEWARD_BURROW", "laptop")
    residents_dir = write_resident(supervised_manifest(host="dxp2800")).parent.parent

    result = runner.invoke(
        main,
        [
            "watchdog", "tick", "--residents", str(residents_dir),
            "--db", str(tmp_path / "s.db"), "--format", "json",
        ],
    )  # fmt: skip

    assert result.exit_code == 0, result.output
    document = json.loads(result.output)
    assert document["interventions"] == 0
    assert document["health"] == []
    assert "topology: docker at" not in result.output, "a green line would corrupt the JSON"
