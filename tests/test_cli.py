"""The CLI is what CI gates on, so its exit codes are part of the contract."""

import json
from datetime import date
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner

from conftest import REPO_ROOT, ResidentWriter, StubWriter, valid_manifest
from steward.budgets import BudgetGuard
from steward.cli import main
from steward.journal import write_entry
from steward.manifest import load_manifest
from steward.store import Store


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


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
    assert payload["residents"][0]["agent_id"] == "claude-code:test-agent"


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


def test_help_lists_the_commands(runner: CliRunner) -> None:
    result = runner.invoke(main, ["--help"])
    assert result.exit_code == 0
    for command in ("validate", "schema", "doctor", "scheduler"):
        assert command in result.output


# ------------------------------------------------------------------------------- doctor


def test_doctor_names_the_brain_and_the_next_fire(
    runner: CliRunner, stub_bin: StubWriter, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    stub_bin("claude", "exit 0")
    monkeypatch.setenv("STEWARD_STATE", str(tmp_path / "state.json"))
    monkeypatch.chdir(REPO_ROOT)
    result = runner.invoke(main, ["doctor"])
    assert result.exit_code == 0, result.output
    assert "life-agent: runner claude (claude-opus-5) — ready" in result.output
    assert "life-agent/daily-summary: '0 7 * * *' Europe/Ljubljana" in result.output


@pytest.mark.usefixtures("empty_path")
def test_doctor_fails_loudly_when_the_binary_is_missing(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("STEWARD_STATE", str(tmp_path / "state.json"))
    monkeypatch.chdir(REPO_ROOT)
    result = runner.invoke(main, ["doctor"])
    assert result.exit_code == 1
    assert "not on PATH" in result.output


def test_doctor_reports_an_invalid_tree(runner: CliRunner, write_resident: ResidentWriter) -> None:
    data = valid_manifest()
    del data["charter"]
    path = write_resident(data)
    result = runner.invoke(main, ["doctor", str(path.parent)])
    assert result.exit_code == 1
    assert "charter" in result.output


def test_doctor_says_so_when_nothing_is_scheduled(
    runner: CliRunner, write_resident: ResidentWriter, stub_bin: StubWriter
) -> None:
    stub_bin("claude", "exit 0")
    data = valid_manifest()
    data["routines"] = []
    path = write_resident(data)
    result = runner.invoke(main, ["doctor", str(path.parent)])
    assert result.exit_code == 0
    assert "no enabled routines" in result.output


def test_doctor_says_where_the_journal_lives_and_who_closes_the_day(
    runner: CliRunner, stub_bin: StubWriter, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    stub_bin("claude", "exit 0")
    monkeypatch.setenv("STEWARD_STATE", str(tmp_path / "state.json"))
    monkeypatch.chdir(REPO_ROOT)
    result = runner.invoke(main, ["doctor"])
    assert result.exit_code == 0, result.output
    assert "life-agent: journal /data/residents/life-agent/memory/journal" in result.output
    assert "closed by close-of-day" in result.output
    assert "burrow-builder: journal" in result.output
    assert "no routine closes the day" in result.output


def test_doctor_complains_about_a_memory_that_cannot_hold_a_journal(
    runner: CliRunner, write_resident: ResidentWriter, stub_bin: StubWriter
) -> None:
    stub_bin("claude", "exit 0")
    data = valid_manifest()
    data["memory"] = {"kind": "file", "path": "/data/test-agent/memory.md"}
    path = write_resident(data)
    result = runner.invoke(main, ["doctor", str(path.parent)])
    assert result.exit_code == 1
    assert "journal — memory.kind is 'file'" in result.output


# ------------------------------------------------------------------------------ journal


def journaling_resident(tmp_path: Path) -> dict:
    data = valid_manifest()
    data["memory"] = {"kind": "directory", "path": str(tmp_path / "memory")}
    return data


def test_journal_prints_entries_newest_first(
    runner: CliRunner, write_resident: ResidentWriter, tmp_path: Path
) -> None:
    data = journaling_resident(tmp_path)
    path = write_resident(data)
    manifest = load_manifest(path).manifest
    write_entry(manifest, date(2026, 8, 23), "close-of-day", "The night before.")
    write_entry(manifest, date(2026, 8, 24), "close-of-day", "Two drafts still waiting.")

    result = runner.invoke(main, ["journal", "test-agent", "--residents", str(path.parent.parent)])
    assert result.exit_code == 0, result.output
    assert result.output.index("2026-08-24") < result.output.index("2026-08-23")
    assert "Two drafts still waiting." in result.output
    assert "close-of-day" in result.output


def test_journal_honours_the_limit(
    runner: CliRunner, write_resident: ResidentWriter, tmp_path: Path
) -> None:
    path = write_resident(journaling_resident(tmp_path))
    manifest = load_manifest(path).manifest
    for day in (22, 23, 24):
        write_entry(manifest, date(2026, 8, day), "close-of-day", f"the {day}th")

    args = ["--residents", str(path.parent.parent), "--limit", "1"]
    result = runner.invoke(main, ["journal", "test-agent", *args])
    assert result.exit_code == 0
    assert "the 24th" in result.output
    assert "the 22nd" not in result.output


def test_journal_reports_json_for_machines(
    runner: CliRunner, write_resident: ResidentWriter, tmp_path: Path
) -> None:
    path = write_resident(journaling_resident(tmp_path))
    write_entry(load_manifest(path).manifest, date(2026, 8, 24), "close-of-day", "Quiet.")

    args = ["--residents", str(path.parent.parent), "--format", "json"]
    result = runner.invoke(main, ["journal", "test-agent", *args])
    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload[0]["date"] == "2026-08-24"
    assert payload[0]["routine"] == "close-of-day"
    assert payload[0]["text"] == "Quiet."


def test_journal_says_so_when_nothing_has_been_written(
    runner: CliRunner, write_resident: ResidentWriter, tmp_path: Path
) -> None:
    path = write_resident(journaling_resident(tmp_path))
    result = runner.invoke(main, ["journal", "test-agent", "--residents", str(path.parent.parent)])
    assert result.exit_code == 0
    assert "has not written a journal entry yet" in result.output


def test_journal_names_the_residents_it_knows_about(
    runner: CliRunner, write_resident: ResidentWriter
) -> None:
    path = write_resident()
    result = runner.invoke(main, ["journal", "nobody", "--residents", str(path.parent.parent)])
    assert result.exit_code == 1
    assert "no valid resident 'nobody'" in result.output
    assert "test-agent" in result.output


def test_journal_refuses_a_memory_it_cannot_read_a_journal_out_of(
    runner: CliRunner, write_resident: ResidentWriter
) -> None:
    data = valid_manifest()
    data["memory"] = {"kind": "file", "path": "/data/test-agent/memory.md"}
    path = write_resident(data)
    result = runner.invoke(main, ["journal", "test-agent", "--residents", str(path.parent.parent)])
    assert result.exit_code == 1
    assert "nowhere to keep one entry per day" in result.output


# ---------------------------------------------------------------------------- scheduler


def scheduler_args(path: Path, tmp_path: Path) -> list[str]:
    return [
        "--residents",
        str(path.parent),
        "--state",
        str(tmp_path / "state.json"),
        "--workdir",
        str(tmp_path),
    ]


def mock_resident() -> dict:
    data = valid_manifest()
    data["runner"] = {"kind": "mock", "model": "pretend"}
    data["routines"] = [
        {
            "id": "inbox-read",
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


def test_scheduler_dry_run_prints_the_prompt_and_emits_nothing(
    runner: CliRunner,
    write_resident: ResidentWriter,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fallback = tmp_path / "events.jsonl"
    monkeypatch.setenv("STEWARD_EVENTS_FALLBACK", str(fallback))
    path = write_resident(mock_resident())
    result = runner.invoke(
        main, ["scheduler", "tick", *scheduler_args(path, tmp_path), "--dry-run"]
    )
    assert result.exit_code == 0, result.output
    assert "would fire test-agent/inbox-read" in result.output
    assert "YOUR CHARTER (AUTHORITATIVE, LAST WORD)" in result.output
    assert not fallback.exists()
    assert not (tmp_path / "state.json").exists()


def test_scheduler_run_dry_run_does_not_loop(
    runner: CliRunner, write_resident: ResidentWriter, tmp_path: Path
) -> None:
    path = write_resident(mock_resident())
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


# --------------------------------------------------------------------------------------
# serve
# --------------------------------------------------------------------------------------


def test_serve_refuses_to_start_without_a_token(
    runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("STEWARD_TOKEN", raising=False)
    result = runner.invoke(
        main, ["serve", "--residents", str(tmp_path), "--db", str(tmp_path / "s.db")]
    )
    assert result.exit_code == 1
    assert "STEWARD_TOKEN" in result.output
    assert "--allow-open" in result.output
    assert not (tmp_path / "s.db").exists()


def test_serve_binds_loopback_by_default(
    runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("STEWARD_TOKEN", "a-shared-secret")
    monkeypatch.setenv("STEWARD_CORS_ORIGINS", "http://village.local")
    served: dict[str, object] = {}
    monkeypatch.setattr(
        "steward.cli.run_server",
        lambda app, *, host, port: served.update(app=app, host=host, port=port),
    )
    result = runner.invoke(
        main, ["serve", "--residents", str(tmp_path), "--db", str(tmp_path / "s.db")]
    )
    assert result.exit_code == 0, result.output
    assert served["host"] == "127.0.0.1"
    assert served["port"] == 8801
    assert "http://127.0.0.1:8801" in result.output
    assert "http://village.local" in result.output


def test_serve_says_out_loud_when_it_has_no_token(
    runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("STEWARD_TOKEN", raising=False)
    monkeypatch.delenv("STEWARD_CORS_ORIGINS", raising=False)
    monkeypatch.setattr("steward.cli.run_server", lambda *_a, **_k: None)
    result = runner.invoke(
        main,
        [
            "serve",
            "--allow-open",
            "--port",
            "9000",
            "--residents",
            str(tmp_path),
            "--db",
            str(tmp_path / "s.db"),
        ],
    )
    assert result.exit_code == 0, result.output
    assert "without a token" in result.output
    assert "cors: none" in result.output


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
    assert "life-agent: daily-summary, escalate, research, write-journal, read-inbox" in (
        result.output
    )
    assert "burrow-builder: daily-summary, escalate, research, write-journal" in result.output
    assert ".claude/skills/ in the session's working directory" in result.output


def test_skills_reports_json(runner: CliRunner, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(REPO_ROOT)
    result = runner.invoke(main, ["skills", "--format", "json"])
    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["library"].endswith("skills")
    assert {"errands", "escalate"} <= {skill["name"] for skill in payload["skills"]}
    assert "errands" in payload["residents"]["life-agent"]
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


# --------------------------------------------------------------------------------------
# the board
# --------------------------------------------------------------------------------------


def board_manifest() -> dict[str, Any]:
    """Build a manifest that opts into the board, with the route its declaration needs."""
    data = valid_manifest()
    data["routes"] = [
        *data["routes"],
        {"id": "job-board", "kind": "job-board", "address": "steward:job-board"},
    ]
    data["board"] = {"claim": True}
    data["runner"] = {"kind": "mock"}
    return data


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


# --------------------------------------------------------------------------------------
# approvals
# --------------------------------------------------------------------------------------


def test_approval_raise_records_a_request_a_human_can_answer(
    runner: CliRunner,
    write_resident: ResidentWriter,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The token-free path: a session with a shell, not with steward's API token."""
    monkeypatch.setenv("STEWARD_EVENTS_FALLBACK", str(tmp_path / "events.jsonl"))
    monkeypatch.delenv("BURROW_URL", raising=False)
    residents_dir = write_resident().parent.parent
    db = tmp_path / "approvals.db"

    result = runner.invoke(
        main,
        [
            "approval", "raise", "test-agent",
            "--action", "send_email",
            "--detail-json", '{"to": "plumber@example.com"}',
            "--expires-in", "4h",
            "--options", "approve,deny",
            "--residents", str(residents_dir),
            "--db", str(db),
        ],
    )  # fmt: skip
    assert result.exit_code == 0, result.output
    assert "Testy wants to send email" in result.output
    request_id = result.output.strip().splitlines()[-1]

    with Store(db) as store:
        record = store.approval(request_id)
    assert record is not None
    assert record.pending
    assert record.action == "send_email"
    assert record.detail == {"to": "plumber@example.com"}
    assert record.options == ("approve", "deny")
    assert record.resident == "test-agent"

    emitted = [
        json.loads(line)
        for line in (tmp_path / "events.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert [event["type"] for event in emitted] == ["needs_human"]
    assert emitted[0]["payload"]["request_id"] == request_id


def test_approval_raise_accepts_a_plain_note(
    runner: CliRunner,
    write_resident: ResidentWriter,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("STEWARD_EVENTS_FALLBACK", str(tmp_path / "events.jsonl"))
    residents_dir = write_resident().parent.parent
    db = tmp_path / "approvals.db"
    result = runner.invoke(
        main,
        [
            "approval", "raise", "test-agent",
            "--action", "cancel_thursday",
            "--note", "Should I cancel Thursday?",
            "--residents", str(residents_dir),
            "--db", str(db),
        ],
    )  # fmt: skip
    assert result.exit_code == 0, result.output
    with Store(db) as store:
        assert store.pending_approvals()[0].detail == {"note": "Should I cancel Thursday?"}


@pytest.mark.parametrize(
    ("flags", "complaint"),
    [
        (["--action", "Send Email"], "is not a slug"),
        (["--action", "send_email", "--detail-json", "{oops"], "does not parse"),
        (["--action", "send_email", "--detail-json", "[1]"], "must be a JSON object"),
        (["--action", "send_email", "--expires-in", "soon"], "is not a duration"),
        (["--action", "send_email", "--options", "maybe"], "unknown option"),
    ],
)
def test_approval_raise_refuses_a_request_it_cannot_read(
    runner: CliRunner,
    write_resident: ResidentWriter,
    tmp_path: Path,
    flags: list[str],
    complaint: str,
) -> None:
    residents_dir = write_resident().parent.parent
    db = tmp_path / "approvals.db"
    result = runner.invoke(
        main,
        [
            "approval", "raise", "test-agent", *flags,
            "--residents", str(residents_dir), "--db", str(db),
        ],
    )  # fmt: skip
    assert result.exit_code == 1
    assert complaint in result.output
    with Store(db) as store:
        assert store.approvals() == []


def test_approval_raise_refuses_both_detail_and_note(
    runner: CliRunner, write_resident: ResidentWriter, tmp_path: Path
) -> None:
    residents_dir = write_resident().parent.parent
    result = runner.invoke(
        main,
        [
            "approval", "raise", "test-agent",
            "--action", "send_email",
            "--detail-json", "{}",
            "--note", "also this",
            "--residents", str(residents_dir),
            "--db", str(tmp_path / "a.db"),
        ],
    )  # fmt: skip
    assert result.exit_code == 1
    assert "not both" in result.output


def test_approval_raise_needs_a_resident_that_exists(
    runner: CliRunner, write_resident: ResidentWriter, tmp_path: Path
) -> None:
    residents_dir = write_resident().parent.parent
    result = runner.invoke(
        main,
        [
            "approval", "raise", "nobody",
            "--action", "send_email",
            "--residents", str(residents_dir),
            "--db", str(tmp_path / "a.db"),
        ],
    )  # fmt: skip
    assert result.exit_code == 1
    assert "no valid resident 'nobody'" in result.output


def test_approval_show_is_the_audit_query(runner: CliRunner, tmp_path: Path) -> None:
    db = tmp_path / "approvals.db"
    with Store(db) as store:
        record = store.create_approval_request(
            agent_id="claude-code:test-agent",
            project="p",
            action="send_email",
            message="Testy wants to send email",
            resident="test-agent",
            detail={"to": "a@example.com"},
            expires_at="2030-01-01T00:00:00.000Z",
        )
        waiting = runner.invoke(main, ["approval", "show", record.request_id, "--db", str(db)])
        assert "still waiting" in waiting.output
        store.decide(record.request_id, "edit", decided_by="api", edit={"subject": "shorter"})

    result = runner.invoke(main, ["approval", "show", record.request_id, "--db", str(db)])
    assert result.exit_code == 0, result.output
    assert "decision:  edit by api" in result.output
    assert "shorter" in result.output
    assert "not yet told to the resident" in result.output
    assert "a@example.com" in result.output

    as_json = runner.invoke(
        main, ["approval", "show", record.request_id, "--db", str(db), "--format", "json"]
    )
    assert json.loads(as_json.output)["decision"] == "edit"


def test_approval_show_says_when_it_has_never_heard_of_a_request(
    runner: CliRunner, tmp_path: Path
) -> None:
    result = runner.invoke(main, ["approval", "show", "nope", "--db", str(tmp_path / "a.db")])
    assert result.exit_code == 1
    assert "no approval request 'nope'" in result.output


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


# ------------------------------------------------------- budgets and the watchdog (#8)


def budgeted_manifest(**budgets: object) -> dict[str, Any]:
    """Build a manifest with a mock runner and the budgets a test wants to try."""
    data = valid_manifest()
    data["runner"] = {"kind": "mock", "model": "pretend"}
    if budgets:
        data["budgets"] = dict(budgets)
    return data


def ledger_a_run(db: Path, cost: float, *, resident: str = "test-agent") -> None:
    """Put one finished run on the ledger, as a scheduler would."""
    with Store(db) as store:
        store.record_run(
            resident=resident,
            agent_id="claude-code:test-agent",
            kind="routine",
            run_id="already-ran",
            ref="daily-summary",
            cost_usd=cost,
            input_tokens=50,
            output_tokens=50,
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

    result = runner.invoke(main, ["budget", "unpause", "test-agent", "--db", str(db)])

    assert result.exit_code == 0, result.output
    assert "test-agent resumed" in result.output
    assert "daily_cost_usd" in result.output
    with Store(db) as store:
        assert store.budget_pause("test-agent") is None
        assert store.approvals()[0].decision == "approve"


def test_budget_unpause_on_a_running_resident_says_so(runner: CliRunner, tmp_path: Path) -> None:
    result = runner.invoke(
        main, ["budget", "unpause", "test-agent", "--db", str(tmp_path / "steward.db")]
    )
    assert result.exit_code == 0
    assert "is not paused by a budget" in result.output


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

    assert result.exit_code == 0, result.output
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


def test_doctor_reports_the_budget_and_the_watchdog(
    runner: CliRunner, write_resident: ResidentWriter, stub_bin: StubWriter, tmp_path: Path
) -> None:
    stub_bin("claude", "exit 0")
    data = budgeted_manifest(daily_cost_usd=5.0)
    data["runner"] = {"kind": "claude"}
    residents_dir = write_resident(data).parent.parent
    db = tmp_path / "steward.db"
    ledger_a_run(db, 2.0)

    result = runner.invoke(main, ["doctor", str(residents_dir), "--db", str(db)])

    assert result.exit_code == 0, result.output
    assert "test-agent: budget daily_cost_usd: 2 of 5" in result.output
    assert "watchdog: has never made a pass" in result.output


def test_doctor_says_a_paused_resident_will_not_fire_tonight(
    runner: CliRunner, write_resident: ResidentWriter, stub_bin: StubWriter, tmp_path: Path
) -> None:
    stub_bin("claude", "exit 0")
    data = budgeted_manifest(daily_cost_usd=1.0)
    data["runner"] = {"kind": "claude"}
    residents_dir = write_resident(data).parent.parent
    db = tmp_path / "steward.db"
    ledger_a_run(db, 9.0)
    with Store(db) as store:
        BudgetGuard(store).allow(
            load_manifest(residents_dir / "test-agent" / "manifest.yaml").manifest
        )

    result = runner.invoke(main, ["doctor", str(residents_dir), "--db", str(db)])

    assert result.exit_code == 1
    assert "budget — paused: budget exceeded" in result.output


def test_doctor_names_the_last_watchdog_pass(
    runner: CliRunner, write_resident: ResidentWriter, stub_bin: StubWriter, tmp_path: Path
) -> None:
    stub_bin("claude", "exit 0")
    data = budgeted_manifest()
    data["runner"] = {"kind": "claude"}
    residents_dir = write_resident(data).parent.parent
    db = tmp_path / "steward.db"
    with Store(db) as store:
        store.record_watchdog_pass(interventions=2, now="2026-08-24T12:00:00.000Z")

    result = runner.invoke(main, ["doctor", str(residents_dir), "--db", str(db)])

    assert result.exit_code == 0, result.output
    assert "watchdog: last pass 2026-08-24T12:00:00.000Z" in result.output
    assert "2 intervention(s)" in result.output
