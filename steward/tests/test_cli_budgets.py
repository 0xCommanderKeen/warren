"""CLI behavior: budgets."""

import json
from pathlib import Path

from click.testing import CliRunner

from conftest import (
    ResidentWriter,
)
from steward.budgets import BudgetGuard
from steward.cli import main
from steward.manifest import load_manifest
from steward.store import Store
from support.cli import (
    budgeted_manifest,
    ledger_a_run,
)
from support.cli import (
    runner as runner,  # noqa: PLC0414 — pytest fixture discovery
)


def test_budget_show_prints_the_gauges_and_the_window(
    runner: CliRunner, write_resident: ResidentWriter, tmp_path: Path
) -> None:
    residents_dir = write_resident(budgeted_manifest(daily_cost_usd=5.0)).parent.parent
    db = tmp_path / "steward.db"
    ledger_a_run(db, 1.5)

    result = runner.invoke(
        main, ["budget", "show", "--residents", str(residents_dir), "--db", str(db)]
    )

    assert result.exit_code == 0, result.output
    assert "daily_cost_usd: 1.50 of 5" in result.output
    assert "daily_tokens: 100 spent, no limit" in result.output
    assert "1 run(s)" in result.output


def test_budget_show_says_no_limit_out_loud(
    runner: CliRunner, write_resident: ResidentWriter, tmp_path: Path
) -> None:
    residents_dir = write_resident(budgeted_manifest()).parent.parent
    result = runner.invoke(
        main,
        ["budget", "show", "--residents", str(residents_dir), "--db", str(tmp_path / "s.db")],
    )
    assert result.exit_code == 0
    assert "test-agent: no limit" in result.output


def test_budget_show_reports_json(
    runner: CliRunner, write_resident: ResidentWriter, tmp_path: Path
) -> None:
    residents_dir = write_resident(budgeted_manifest(daily_tokens=1000)).parent.parent
    db = tmp_path / "steward.db"
    ledger_a_run(db, 0.0)
    result = runner.invoke(
        main,
        [
            "budget", "show", "test-agent",
            "--residents", str(residents_dir), "--db", str(db), "--format", "json",
        ],
    )  # fmt: skip
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload[0]["resident"] == "test-agent"
    assert payload[0]["spent"]["tokens"] == 100
    assert payload[0]["window"]["day"]


def test_budget_show_by_origin_rolls_a_chain_up_to_the_question_that_started_it(
    runner: CliRunner, write_resident: ResidentWriter, tmp_path: Path
) -> None:
    """The other half of the ledger: not who spent it, but what it was spent answering."""
    residents_dir = write_resident(budgeted_manifest(daily_cost_usd=50.0)).parent.parent
    db = tmp_path / "steward.db"
    with Store(db) as store:
        store.record_run(
            resident="test-agent",
            agent_id="claude-code:test-agent",
            kind="delegated",
            run_id="a-letter",
            ref="a-letter",
            origin="task:root",
            cost_usd=3.25,
            input_tokens=40,
            output_tokens=60,
        )

    result = runner.invoke(
        main,
        [
            "budget", "show", "test-agent", "--by-origin",
            "--residents", str(residents_dir), "--db", str(db),
        ],
    )  # fmt: skip

    assert result.exit_code == 0, result.output
    assert "by origin" in result.output
    assert "task:root: $3.2500, 100 token(s), 1 run(s)" in result.output


def test_budget_show_by_origin_says_so_when_the_ledger_is_empty(
    runner: CliRunner, write_resident: ResidentWriter, tmp_path: Path
) -> None:
    """An empty rollup is an answer, not a blank space where a number should be."""
    residents_dir = write_resident(budgeted_manifest()).parent.parent
    result = runner.invoke(
        main,
        [
            "budget", "show", "--by-origin",
            "--residents", str(residents_dir), "--db", str(tmp_path / "steward.db"),
        ],
    )  # fmt: skip
    assert result.exit_code == 0, result.output
    assert "nothing on the ledger in this window" in result.output


def test_budget_show_by_origin_wraps_the_json_only_when_it_is_asked_for(
    runner: CliRunner, write_resident: ResidentWriter, tmp_path: Path
) -> None:
    """The bare list is the shape something is already parsing; the flag is what wraps it."""
    residents_dir = write_resident(budgeted_manifest(daily_cost_usd=5.0)).parent.parent
    db = tmp_path / "steward.db"
    ledger_a_run(db, 1.0)
    result = runner.invoke(
        main,
        [
            "budget", "show", "--by-origin",
            "--residents", str(residents_dir), "--db", str(db), "--format", "json",
        ],
    )  # fmt: skip
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["residents"][0]["resident"] == "test-agent"
    # A routine came off no task, so it rolls up under the resident whose day it was.
    assert payload["by_origin"] == [
        {
            "origin": "resident:test-agent",
            "runs": 1,
            "estimated_cost_runs": 0,
            "cost_usd": 1.0,
            "tokens": 100,
            "duration_s": 0.0,
        }
    ]


def test_budget_show_refuses_an_unknown_resident(
    runner: CliRunner, write_resident: ResidentWriter, tmp_path: Path
) -> None:
    residents_dir = write_resident(budgeted_manifest()).parent.parent
    result = runner.invoke(
        main,
        [
            "budget",
            "show",
            "nobody",
            "--residents",
            str(residents_dir),
            "--db",
            str(tmp_path / "s"),
        ],
    )
    assert result.exit_code == 1
    assert "no valid resident 'nobody'" in result.output


def test_budget_show_names_the_gap_when_a_brain_reported_nothing(
    runner: CliRunner, write_resident: ResidentWriter, tmp_path: Path
) -> None:
    residents_dir = write_resident(budgeted_manifest(daily_cost_usd=5.0)).parent.parent
    db = tmp_path / "steward.db"
    with Store(db) as store:
        store.record_run(
            resident="test-agent",
            agent_id="claude-code:test-agent",
            kind="routine",
            trigger="schedule",
            run_id="quiet",
            usage_known=False,
        )
    result = runner.invoke(
        main, ["budget", "show", "--residents", str(residents_dir), "--db", str(db)]
    )
    assert "did not report what they cost" in result.output


def test_budget_unpause_lifts_a_pause_and_says_what_it_was(
    runner: CliRunner, write_resident: ResidentWriter, tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("STEWARD_EVENTS_FALLBACK", str(tmp_path / "events.jsonl"))
    residents_dir = write_resident(budgeted_manifest(daily_cost_usd=1.0)).parent.parent
    db = tmp_path / "steward.db"
    ledger_a_run(db, 4.0)
    # Trip the budget through the same path a scheduled fire would.
    with Store(db) as store:
        resident = load_manifest(residents_dir / "test-agent" / "manifest.yaml")
        BudgetGuard(store).allow(resident.manifest)

    result = runner.invoke(
        main,
        [
            "budget", "unpause", "test-agent", "--residents", str(residents_dir),
            "--db", str(db),
        ],
    )  # fmt: skip

    assert result.exit_code == 0, result.output
    assert "test-agent resumed" in result.output
    assert "daily_cost_usd" in result.output
    with Store(db) as store:
        assert store.budget_pause("test-agent") is None
        assert store.approvals()[0].decision == "approve"


def test_budget_unpause_on_a_running_resident_is_a_successful_no_op(
    runner: CliRunner, write_resident: ResidentWriter, tmp_path: Path
) -> None:
    residents_dir = write_resident(budgeted_manifest()).parent.parent
    result = runner.invoke(
        main,
        [
            "budget",
            "unpause",
            "test-agent",
            "--residents",
            str(residents_dir),
            "--db",
            str(tmp_path / "steward.db"),
        ],
    )
    assert result.exit_code == 0
    assert "is not paused by a budget" in result.output


def test_budget_unpause_refuses_an_unknown_resident(
    runner: CliRunner, write_resident: ResidentWriter, tmp_path: Path
) -> None:
    residents_dir = write_resident(budgeted_manifest()).parent.parent
    result = runner.invoke(
        main,
        [
            "budget",
            "unpause",
            "typo",
            "--residents",
            str(residents_dir),
            "--db",
            str(tmp_path / "steward.db"),
        ],
    )
    assert result.exit_code == 1
    assert "no valid resident 'typo'" in result.output
