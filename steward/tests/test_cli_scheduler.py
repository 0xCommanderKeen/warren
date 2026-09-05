"""CLI behavior: scheduler."""

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from click.testing import CliRunner

from conftest import (
    ResidentWriter,
    valid_manifest,
)
from steward import cli
from steward.cli import main
from steward.runners import Outcome, RunResult
from steward.scheduler import FireReport, SchedulerState, load_scheduled
from steward.store import Store
from support.cli import (
    board_manifest,
)
from support.cli import (
    runner as runner,  # noqa: PLC0414 — pytest fixture discovery
)


def scheduler_builder(engine: object, cleanup: Mock):
    """Return a CLI builder double whose ownership boundary can be asserted."""

    @contextmanager
    def build(*_args: object, **_kwargs: object) -> Iterator[object]:
        try:
            yield engine
        finally:
            cleanup()

    return build


# ---------------------------------------------------------------------------- scheduler

#: The one routine :func:`mock_resident` declares, and the key the scheduler files it
#: under — the resident id and the routine id, which is how an anchor is addressed.
MOCK_ROUTINE_ID = "inbox-read"


MOCK_ROUTINE_KEY = f"test-agent/{MOCK_ROUTINE_ID}"


def scheduler_state_file(tmp_path: Path) -> Path:
    """Return the state file every scheduler test in this file schedules against.

    One spelling, because ``--state`` and anything that seeds an anchor into it have to
    name the same file: two literals that drifted apart would leave the seed writing
    somewhere the CLI never reads, and the test would pass by asserting the fresh-state
    behaviour it was written to distinguish from.
    """
    return tmp_path / "state.json"


def scheduler_args(path: Path, tmp_path: Path) -> list[str]:
    return [
        "--residents",
        str(path.parent),
        "--state",
        str(scheduler_state_file(tmp_path)),
        "--workdir",
        str(tmp_path),
    ]


def seed_anchor(tmp_path: Path, ago: timedelta, key: str = MOCK_ROUTINE_KEY) -> Path:
    """Write a state file whose anchor for ``key`` is already that far in the past.

    First sight anchors a routine at *now*, so nothing is ever due against a state file
    that has never been written — which is the right answer and a useless fixture. Every
    test that needs a routine to actually be due says so here.
    """
    state = SchedulerState(path=scheduler_state_file(tmp_path))
    state.set_anchor(key, datetime.now(UTC) - ago)
    state.save()
    return state.path


def mock_resident() -> dict:
    data = valid_manifest()
    data["runner"] = {"kind": "mock", "model": "pretend"}
    data["routines"] = [
        {
            "id": MOCK_ROUTINE_ID,
            "schedule": "* * * * *",
            "prompt": "Read the mail.",
            "timeout_s": 60,
            "enabled": True,
        }
    ]
    return data


def test_scheduler_tick_fires_and_reports(
    runner: CliRunner,
    write_resident: ResidentWriter,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("STEWARD_EVENTS_FALLBACK", str(tmp_path / "events.jsonl"))
    path = write_resident(mock_resident())
    args = scheduler_args(path, tmp_path)

    first = runner.invoke(main, ["scheduler", "tick", *args])
    assert first.exit_code == 0, first.output
    assert "nothing due" in first.output  # first sight only anchors

    second = runner.invoke(main, ["scheduler", "tick", *args])
    assert second.exit_code == 0, second.output


def test_scheduler_tick_exits_non_zero_on_an_unpersistable_state(
    runner: CliRunner,
    write_resident: ResidentWriter,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A scheduler that cannot persist its anchor must stop the cron run, not fire blind."""
    monkeypatch.setenv("STEWARD_EVENTS_FALLBACK", str(tmp_path / "events.jsonl"))
    path = write_resident(mock_resident())
    state_dir = tmp_path / "state-is-a-directory"
    state_dir.mkdir()
    args = ["--residents", str(path.parent), "--state", str(state_dir), "--workdir", str(tmp_path)]

    result = runner.invoke(main, ["scheduler", "tick", *args])
    assert result.exit_code == 1
    assert "STEWARD_STATE names a directory" in result.output


def test_scheduler_dry_run_prints_the_prompt_and_emits_nothing(
    runner: CliRunner,
    write_resident: ResidentWriter,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fallback = tmp_path / "events.jsonl"
    monkeypatch.setenv("STEWARD_EVENTS_FALLBACK", str(fallback))
    path = write_resident(mock_resident())
    state_file = seed_anchor(tmp_path, timedelta(minutes=5))
    before = state_file.read_text(encoding="utf-8")
    result = runner.invoke(
        main, ["scheduler", "tick", *scheduler_args(path, tmp_path), "--dry-run"]
    )
    assert result.exit_code == 0, result.output
    assert f"would fire {MOCK_ROUTINE_KEY}" in result.output
    assert "YOUR CHARTER (AUTHORITATIVE, LAST WORD)" in result.output
    assert not fallback.exists()
    assert state_file.read_text(encoding="utf-8") == before  # a rehearsal anchors nothing


@pytest.mark.parametrize("command", ["tick", "run"])
def test_scheduler_dry_run_rehearses_only_what_is_due(
    runner: CliRunner,
    write_resident: ResidentWriter,
    tmp_path: Path,
    command: str,
) -> None:
    """A rehearsal is a rehearsal of the *next tick*, so it answers the tick's question.

    Printing every routine as "would fire" said something that was not true of any of
    them, and it said it loudest on the fleet with the most routines — the operator
    reading it cannot tell the 07:00 summary that is about to run from the one that runs
    in nine hours (warren#90).
    """
    data = mock_resident()
    data["routines"].append(
        {
            "id": "nightly",
            "schedule": "0 4 * * *",
            "prompt": "Sleep.",
            "timeout_s": 60,
            "enabled": True,
        }
    )
    path = write_resident(data)
    seed_anchor(tmp_path, timedelta(minutes=5))
    result = runner.invoke(
        main, ["scheduler", command, *scheduler_args(path, tmp_path), "--dry-run"]
    )
    assert result.exit_code == 0, result.output
    assert f"would fire {MOCK_ROUTINE_KEY}" in result.output
    assert "test-agent/nightly" not in result.output


@pytest.mark.parametrize("command", ["tick", "run"])
def test_scheduler_dry_run_on_a_fresh_state_has_nothing_to_rehearse(
    runner: CliRunner,
    write_resident: ResidentWriter,
    tmp_path: Path,
    command: str,
) -> None:
    """First sight anchors at now, so nothing is due yet — and the rehearsal says so."""
    path = write_resident(mock_resident())
    result = runner.invoke(
        main, ["scheduler", command, *scheduler_args(path, tmp_path), "--dry-run"]
    )
    assert result.exit_code == 0, result.output
    assert "nothing due" in result.output
    assert "would fire" not in result.output
    assert not scheduler_state_file(tmp_path).exists()


def test_scheduler_run_dry_run_does_not_loop(
    runner: CliRunner, write_resident: ResidentWriter, tmp_path: Path
) -> None:
    path = write_resident(mock_resident())
    seed_anchor(tmp_path, timedelta(minutes=5))
    result = runner.invoke(main, ["scheduler", "run", *scheduler_args(path, tmp_path), "--dry-run"])
    assert result.exit_code == 0, result.output
    assert "would fire" in result.output


def test_scheduler_run_stops_after_max_ticks(
    runner: CliRunner,
    write_resident: ResidentWriter,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("STEWARD_EVENTS_FALLBACK", str(tmp_path / "events.jsonl"))
    monkeypatch.setattr("steward.scheduler.MAX_SLEEP_S", 0.01)
    path = write_resident(mock_resident())
    result = runner.invoke(
        main, ["scheduler", "run", *scheduler_args(path, tmp_path), "--max-ticks", "1"]
    )
    assert result.exit_code == 0, result.output


@pytest.mark.parametrize(
    "terminal_error", [None, "run ownership lost: terminal outcome already chosen"]
)
@pytest.mark.parametrize("command", [("tick",), ("run", "--max-ticks", "1")])
@pytest.mark.parametrize(
    ("outcomes", "expected"),
    [((True,), 0), ((True, False), 1), ((False,), 1), ((None,), 0)],
)
def test_scheduler_commands_carry_fire_outcomes(  # noqa: PLR0913, PLR0917
    runner: CliRunner,
    write_resident: ResidentWriter,
    terminal_error: str | None,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    command: tuple[str, ...],
    outcomes: tuple[bool | None, ...],
    expected: int,
) -> None:
    path = write_resident(mock_resident())
    [scheduled] = load_scheduled(path.parent)
    reports = [
        FireReport(
            run_id=f"run-{index}",
            terminal_error=terminal_error,
            fired=ok is not None,
            scheduled=scheduled,
            result=(
                RunResult(
                    outcome=Outcome.OK if ok else Outcome.FAILED, duration_s=0.1, error="exit 7"
                )
                if ok is not None
                else None
            ),
            skipped_reason="policy refusal" if ok is None else None,
        )
        for index, ok in enumerate(outcomes)
    ]
    engine = SimpleNamespace(
        scheduled=(),
        require_ready=lambda: None,
        tick=lambda: reports,
        run=lambda **_kwargs: reports,
    )
    cleanup = Mock()
    monkeypatch.setattr(cli, "_build_scheduler", scheduler_builder(engine, cleanup))

    result = runner.invoke(main, ["scheduler", *command, "--residents", str(tmp_path)])

    if terminal_error and any(ok is not None for ok in outcomes):
        expected = 1
        assert terminal_error in result.output
    assert result.exit_code == expected, result.output
    cleanup.assert_called_once_with()


@pytest.mark.parametrize(
    ("command", "expected", "message"),
    [(("tick",), 1, "Aborted!"), (("run",), 0, "stopped")],
)
def test_scheduler_commands_release_resources_on_interrupt(  # noqa: PLR0913, PLR0917
    runner: CliRunner,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    command: tuple[str, ...],
    expected: int,
    message: str,
) -> None:
    def interrupted(**_kwargs: object) -> list[object]:
        raise KeyboardInterrupt

    engine = SimpleNamespace(
        scheduled=(), require_ready=lambda: None, tick=interrupted, run=interrupted
    )
    cleanup = Mock()
    monkeypatch.setattr(cli, "_build_scheduler", scheduler_builder(engine, cleanup))
    result = runner.invoke(main, ["scheduler", *command, "--residents", str(tmp_path)])
    assert result.exit_code == expected, result.output
    assert message in result.output
    cleanup.assert_called_once_with()


@pytest.mark.parametrize("command", [("tick",), ("run", "--max-ticks", "1")])
def test_scheduler_commands_release_resources_on_scheduler_error(
    runner: CliRunner,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    command: tuple[str, ...],
) -> None:
    def failed(*_args: object, **_kwargs: object) -> list[object]:
        raise cli.SchedulerError("scheduler broke")

    engine = SimpleNamespace(scheduled=(), require_ready=lambda: None, tick=failed, run=failed)
    cleanup = Mock()
    monkeypatch.setattr(cli, "_build_scheduler", scheduler_builder(engine, cleanup))

    result = runner.invoke(main, ["scheduler", *command, "--residents", str(tmp_path)])

    assert result.exit_code == 1, result.output
    assert "scheduler broke" in result.output
    cleanup.assert_called_once_with()


def test_scheduler_builder_closes_store_when_construction_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = Mock()
    monkeypatch.setattr(cli, "_load_or_exit", lambda _residents: [])
    monkeypatch.setattr(cli, "_open_store", lambda _db: store)
    monkeypatch.setattr(cli.Dispatcher, "from_path", lambda *_args, **_kwargs: Mock())
    monkeypatch.setattr(cli, "Scheduler", Mock(side_effect=cli.SchedulerError("build broke")))

    with (
        pytest.raises(cli.SchedulerError, match="build broke"),
        cli._build_scheduler(
            tmp_path, tmp_path / "state.json", tmp_path, tmp_path / "store.db", 60, dry_run=False
        ),
    ):
        pass

    store.close.assert_called_once_with()


@pytest.mark.usefixtures("empty_path")
def test_scheduler_refuses_to_start_without_the_declared_binary(
    runner: CliRunner, write_resident: ResidentWriter, tmp_path: Path
) -> None:
    data = mock_resident()
    data["runner"] = {"kind": "claude", "model": "claude-opus-5"}
    path = write_resident(data)
    for command in ("tick", "run"):
        result = runner.invoke(main, ["scheduler", command, *scheduler_args(path, tmp_path)])
        assert result.exit_code == 1, result.output
        assert "not on PATH" in result.output


def test_scheduler_refuses_an_invalid_tree(
    runner: CliRunner, write_resident: ResidentWriter, tmp_path: Path
) -> None:
    data = valid_manifest()
    del data["memory"]
    path = write_resident(data)
    result = runner.invoke(main, ["scheduler", "tick", *scheduler_args(path, tmp_path)])
    assert result.exit_code == 1
    assert "memory" in result.output


def test_a_scheduler_tick_sweeps_the_board_too(
    runner: CliRunner, write_resident: ResidentWriter, tmp_path: Path
) -> None:
    """The board is swept on the scheduler's own rhythm, not on a second timer."""
    residents_dir = write_resident(board_manifest()).parent.parent
    db = tmp_path / "board.db"
    with Store(db) as store:
        store.post_job(title="Picked up by a tick")

    result = runner.invoke(
        main,
        [
            "scheduler", "tick",
            "--residents", str(residents_dir),
            "--state", str(tmp_path / "state.json"),
            "--db", str(db),
            "--workdir", str(tmp_path),
        ],
    )  # fmt: skip
    assert result.exit_code == 0, result.output
    with Store(db) as after:
        assert [job.status for job in after.jobs()] == ["done"]


def test_a_dry_run_tick_touches_no_database(
    runner: CliRunner, write_resident: ResidentWriter, tmp_path: Path
) -> None:
    residents_dir = write_resident(board_manifest()).parent.parent
    db = tmp_path / "board.db"
    with Store(db) as store:
        store.post_job(title="Not tonight")

    result = runner.invoke(
        main,
        [
            "scheduler", "tick", "--dry-run",
            "--residents", str(residents_dir),
            "--state", str(tmp_path / "state.json"),
            "--db", str(db),
        ],
    )  # fmt: skip
    assert result.exit_code == 0, result.output
    with Store(db) as after:
        assert [job.status for job in after.jobs()] == ["open"]
