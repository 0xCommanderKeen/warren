"""CLI behavior: board."""

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from click.testing import CliRunner

from conftest import (
    ResidentWriter,
)
from steward import cli
from steward.cli import main
from steward.store import Store
from support.cli import (
    board_manifest,
)
from support.cli import (
    runner as runner,  # noqa: PLC0414 — pytest fixture discovery
)


def test_board_dispatch_claims_and_works_a_task(
    runner: CliRunner, write_resident: ResidentWriter, tmp_path: Path
) -> None:
    residents_dir = write_resident(board_manifest()).parent.parent
    db = tmp_path / "board.db"
    with Store(db) as store:
        store.post_job(title="Research X")

    result = runner.invoke(
        main, ["board", "dispatch", "--residents", str(residents_dir), "--db", str(db)]
    )
    assert result.exit_code == 0, result.output
    assert "done test-agent" in result.output
    with Store(db) as after:
        assert [job.status for job in after.jobs()] == ["done"]


def test_board_dispatch_dry_run_plans_without_spending(
    runner: CliRunner, write_resident: ResidentWriter, tmp_path: Path
) -> None:
    """The first dispatch against shipped residents must be seeable before it spends (#88)."""
    residents_dir = write_resident(board_manifest()).parent.parent
    db = tmp_path / "board.db"
    with Store(db) as store:
        store.post_job(title="Research X")

    result = runner.invoke(
        main,
        ["board", "dispatch", "--residents", str(residents_dir), "--db", str(db), "--dry-run"],
    )
    assert result.exit_code == 0, result.output
    assert "would claim" in result.output
    assert "test-agent" in result.output
    # Nothing was claimed and no session ran: the task is still open.
    with Store(db) as after:
        assert [job.status for job in after.jobs()] == ["open"]


def test_board_dispatch_with_an_empty_board_says_so(
    runner: CliRunner, write_resident: ResidentWriter, tmp_path: Path
) -> None:
    residents_dir = write_resident(board_manifest()).parent.parent
    result = runner.invoke(
        main,
        ["board", "dispatch", "--residents", str(residents_dir), "--db", str(tmp_path / "b.db")],
    )
    assert result.exit_code == 0, result.output
    assert "nothing claimed" in result.output


@pytest.mark.parametrize(("done", "expected"), [((True,), 0), ((True, False), 1), ((False,), 1)])
def test_board_dispatch_carries_clean_partial_and_failed_outcomes(
    runner: CliRunner,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    done: tuple[bool, ...],
    expected: int,
) -> None:
    reports = tuple(
        SimpleNamespace(
            done=ok,
            delegated=False,
            resident_id="test-agent",
            task=SimpleNamespace(
                task_id=f"task-{index}", title="work", delegated=False, delegated_by=None
            ),
            reason=None if ok else "exit 7",
            raised=(),
            handed_over=(),
        )
        for index, ok in enumerate(done)
    )
    dispatch = SimpleNamespace(reopened=(), expired_approvals=(), reports=reports, planned=())
    monkeypatch.setattr(
        cli.Dispatcher,
        "from_path",
        lambda *_args, **_kwargs: SimpleNamespace(dispatch=lambda: dispatch),
    )

    result = runner.invoke(
        main, ["board", "dispatch", "--residents", str(tmp_path), "--db", str(tmp_path / "b.db")]
    )

    assert result.exit_code == expected, result.output


def test_board_dispatch_scrubs_the_text_a_session_wrote(
    runner: CliRunner,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A dispatch line prints a task title and a handoff message on every sweep.

    Both carry text somebody else wrote — a job posted over the API, a title another
    resident's session chose — and both land in a terminal scrollback (steward #144). A
    knock's message is derived from `soul.name` and the action, so it is here for
    completeness rather than because it can carry a secret.
    """
    leak = "ghp_abcdefghijklmnopqrstuvwxyz0123456789"
    with Store(tmp_path / "raised.db") as store:
        record = store.create_approval_request(
            agent_id="claude-code:test-agent",
            project="p",
            action="send_email",
            message=f"Testy needs {leak}",
        )
    reports = (
        SimpleNamespace(
            done=True,
            delegated=False,
            resident_id="test-agent",
            task=SimpleNamespace(
                task_id="t1", title=f"file the key {leak}", delegated=False, delegated_by=None
            ),
            reason=None,
            raised=(record,),
            handed_over=(
                SimpleNamespace(
                    task=None, reason="not_permitted", message=f"tried to delegate {leak!r}"
                ),
            ),
        ),
    )
    dispatch = SimpleNamespace(reopened=(), expired_approvals=(), reports=reports, planned=())
    monkeypatch.setattr(
        cli.Dispatcher,
        "from_path",
        lambda *_args, **_kwargs: SimpleNamespace(dispatch=lambda: dispatch),
    )

    result = runner.invoke(
        main, ["board", "dispatch", "--residents", str(tmp_path), "--db", str(tmp_path / "b.db")]
    )

    assert result.exit_code == 0, result.output
    assert "ghp_" not in result.output
    # All three lines printed, and all three scrubbed.
    assert result.output.count("[redacted:secret]") == 3
    assert "file the key" in result.output  # only the secret is cut


def test_board_dispatch_reports_the_deadlines_it_swept(
    runner: CliRunner, write_resident: ResidentWriter, tmp_path: Path
) -> None:
    residents_dir = write_resident(board_manifest()).parent.parent
    db = tmp_path / "board.db"
    with Store(db) as store:
        store.post_job(title="Abandoned")
        store.claim_next_job(
            claimant="claude-code:ghost", skills=[], lease_expires_at="2020-01-01T00:00:00.000Z"
        )
        store.create_approval_request(
            agent_id="a:b",
            project="p",
            action="spend_money",
            message="…",
            expires_at="2020-01-01T00:00:00.000Z",
        )

    result = runner.invoke(
        main,
        ["board", "dispatch", "--residents", str(residents_dir), "--db", str(db), "--sweep-only"],
    )
    assert result.exit_code == 0, result.output
    assert "lease expired" in result.output
    assert "approval expired: spend_money denied by default" in result.output
    with Store(db) as after:
        assert after.jobs("open"), "a sweep reopens the lease, and claims nothing"


def test_board_list_shows_the_board_and_who_could_take_it(
    runner: CliRunner, write_resident: ResidentWriter, tmp_path: Path
) -> None:
    residents_dir = write_resident(board_manifest()).parent.parent
    db = tmp_path / "board.db"
    with Store(db) as store:
        store.post_job(title="Anyone", required_skills=["daily-summary"])
        store.post_job(title="Nobody here", required_skills=["surgery"])

    result = runner.invoke(
        main, ["board", "list", "--residents", str(residents_dir), "--db", str(db)]
    )
    assert result.exit_code == 0, result.output
    assert "claimable by: test-agent" in result.output
    assert "claimable by: nobody on this tree" in result.output
    assert "skills: surgery" in result.output


def test_board_list_reports_json_and_an_empty_board(
    runner: CliRunner, write_resident: ResidentWriter, tmp_path: Path
) -> None:
    residents_dir = write_resident(board_manifest()).parent.parent
    db = tmp_path / "board.db"
    empty = runner.invoke(
        main, ["board", "list", "--residents", str(residents_dir), "--db", str(db)]
    )
    assert "the board is empty" in empty.output

    with Store(db) as store:
        store.post_job(title="One thing")
        store.claim_next_job(claimant="a:b", skills=[], lease_expires_at="2030-01-01T00:00:00.000Z")
    result = runner.invoke(
        main,
        ["board", "list", "--residents", str(residents_dir), "--db", str(db), "--format", "json"],
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload[0]["status"] == "claimed"
    assert payload[0]["claimant"] == "a:b"
