"""The CLI is what CI gates on, so its exit codes are part of the contract."""

import json
from datetime import date
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner

from conftest import (
    REPO_ROOT,
    ResidentWriter,
    ScratchRepo,
    SkillWriter,
    StubWriter,
    valid_manifest,
)
from steward.budgets import BudgetGuard
from steward.cli import main
from steward.deploy import LocalTransport, TransportError
from steward.journal import latest_entry, write_entry
from steward.manifest import load_manifest
from steward.prompt import assemble_preamble
from steward.skills import effective_skills, library_for
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
    for command in ("validate", "schema", "doctor", "scheduler", "show"):
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


def test_doctor_warns_when_the_journal_location_is_not_writable(
    runner: CliRunner, write_resident: ResidentWriter, stub_bin: StubWriter, tmp_path: Path
) -> None:
    """Doctor probes writability and says so — a warning, not a failure (#89)."""
    stub_bin("claude", "exit 0")
    blocker = tmp_path / "blocker"
    blocker.write_text("a file where the journal's parent should be", encoding="utf-8")
    data = valid_manifest()
    data["memory"] = {"kind": "directory", "path": str(blocker / "memory"), "journal": "journal"}
    path = write_resident(data)

    result = runner.invoke(main, ["doctor", str(path.parent)])
    assert result.exit_code == 0, result.output  # a container path unwritable here is a warning
    assert "not writable" in result.output


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


# --------------------------------------------------------------------------------- show


def show_args(path: Path, tmp_path: Path) -> list[str]:
    """Point at the resident tree and at a throwaway database, never the real one."""
    return ["--residents", str(path.parent.parent), "--db", str(tmp_path / "show.db")]


def test_show_prints_exactly_the_assembled_preamble(
    runner: CliRunner,
    write_resident: ResidentWriter,
    write_skill: SkillWriter,
    tmp_path: Path,
) -> None:
    """One assembly, not a second renderer: what is printed is what a session is told.

    The sections themselves are :mod:`tests.test_prompt`'s contract; this asserts only
    that the command adds nothing to and takes nothing from it.
    """
    path = write_resident(journaling_resident(tmp_path))
    write_skill("write-journal", defaults=True)
    write_skill("daily-summary")
    resident = load_manifest(path)
    write_entry(resident.manifest, date(2026, 8, 24), "close-of-day", "Two drafts still waiting.")

    result = runner.invoke(main, ["show", "test-agent", *show_args(path, tmp_path)])
    assert result.exit_code == 0, result.output
    expected = assemble_preamble(
        resident.manifest,
        resident.soul.body,
        latest_entry(resident.manifest, source=resident.path),
        effective_skills(resident.manifest, library_for(path.parent.parent)),
    )
    assert result.output == expected + "\n"


def test_show_reports_json_for_machines(
    runner: CliRunner, write_resident: ResidentWriter, tmp_path: Path
) -> None:
    path = write_resident(journaling_resident(tmp_path))
    args = [*show_args(path, tmp_path), "--format", "json"]
    result = runner.invoke(main, ["show", "test-agent", *args])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["resident"] == "test-agent"
    assert payload["journal"] is False
    assert payload["decisions"] == 0
    assert "YOUR CHARTER (AUTHORITATIVE, LAST WORD)" in payload["preamble"]


def test_show_does_not_consume_a_pending_decision(
    runner: CliRunner, write_resident: ResidentWriter, tmp_path: Path
) -> None:
    """A preview must not eat the answer the resident's next real session is owed (#74)."""
    path = write_resident(journaling_resident(tmp_path))
    resident = load_manifest(path)
    db = tmp_path / "show.db"
    with Store(db) as store:
        request = store.create_approval_request(
            agent_id=resident.agent_id,
            project="p",
            action="send_email",
            message="…",
            resident=resident.id,
        )
        store.decide(request.request_id, "approve", decided_by="api")

    result = runner.invoke(main, ["show", "test-agent", *show_args(path, tmp_path)])
    assert result.exit_code == 0, result.output
    assert "send_email: approve" in result.output
    with Store(db) as after:
        still_waiting = after.undelivered_decisions(resident.id)
    assert [record.request_id for record in still_waiting] == [request.request_id]


def test_show_redacts_a_secret_the_resident_journaled(
    runner: CliRunner, write_resident: ResidentWriter, tmp_path: Path
) -> None:
    """The journal is the one section no validator scanned: a model wrote it at runtime."""
    path = write_resident(journaling_resident(tmp_path))
    manifest = load_manifest(path).manifest
    write_entry(manifest, date(2026, 8, 24), "close-of-day", "reused sk-ant-abcdef0123456789ghij")

    result = runner.invoke(main, ["show", "test-agent", *show_args(path, tmp_path)])
    assert result.exit_code == 0, result.output
    assert "sk-ant-" not in result.output
    assert "[redacted:secret]" in result.output


def test_show_redacts_a_secret_a_decision_carries(
    runner: CliRunner, write_resident: ResidentWriter, tmp_path: Path
) -> None:
    """A decision's detail and edit are model-written too, and just as unscanned.

    The detail is whatever the session typed into its ``<needs-human>`` block and is stored
    verbatim; the edit is whatever the human answered with. Only burrow's egress redacted
    them until now, so ``steward show`` printed a live key to anyone previewing a preamble.
    """
    path = write_resident(journaling_resident(tmp_path))
    resident = load_manifest(path)
    db = tmp_path / "show.db"
    with Store(db) as store:
        request = store.create_approval_request(
            agent_id=resident.agent_id,
            project="p",
            action="rotate_token",
            message="rotate the deploy key",
            resident=resident.id,
            detail={"cmd": "curl -H 'Authorization: Bearer sk-ant-abcdef0123456789ghij'"},
        )
        store.decide(
            request.request_id,
            "edit",
            decided_by="miha",
            edit={"nested": ["use ghp_abcdefghijklmnopqrstuvwxyz012345 instead"]},
        )

    result = runner.invoke(main, ["show", "test-agent", *show_args(path, tmp_path)])
    assert result.exit_code == 0, result.output
    assert "sk-ant-" not in result.output
    assert "ghp_" not in result.output
    assert result.output.count("[redacted:secret]") == 2
    # The decision itself still reads as itself: only the secret is cut.
    assert "rotate_token: edit (decided by miha" in result.output


def test_show_names_the_residents_it_knows_about(
    runner: CliRunner, write_resident: ResidentWriter, tmp_path: Path
) -> None:
    path = write_resident()
    result = runner.invoke(main, ["show", "nobody", *show_args(path, tmp_path)])
    assert result.exit_code == 1
    assert "no valid resident 'nobody'" in result.output


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


def test_allow_open_is_refused_on_a_non_loopback_bind(
    runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """--allow-open serves every write path with no token; a public bind is refused (#81)."""
    monkeypatch.delenv("STEWARD_TOKEN", raising=False)
    served: dict[str, object] = {}
    monkeypatch.setattr("steward.cli.run_server", lambda *_a, **_k: served.setdefault("ran", True))
    result = runner.invoke(
        main,
        ["serve", "--allow-open", "--host", "0.0.0.0", "--residents", str(tmp_path)],  # noqa: S104
    )
    assert result.exit_code == 1
    assert "loopback" in result.output
    assert "ran" not in served, "the server was never started"


def test_allow_open_is_permitted_on_loopback(
    runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("STEWARD_TOKEN", raising=False)
    monkeypatch.setattr("steward.cli.run_server", lambda *_a, **_k: None)
    for host in ("127.0.0.1", "::1", "localhost"):
        result = runner.invoke(
            main, ["serve", "--allow-open", "--host", host, "--residents", str(tmp_path)]
        )
        assert result.exit_code == 0, result.output


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


def test_budget_show_by_origin_rolls_a_chain_up_to_the_question_that_started_it(
    runner: CliRunner, write_resident: ResidentWriter, tmp_path: Path
) -> None:
    """The other half of the ledger: not who spent it, but what it was spent answering."""
    residents_dir = write_resident(budgeted_manifest(daily_cost_usd=50.0)).parent.parent
    db = tmp_path / "steward.db"
    with Store(db) as store:
        letter = store.delegate_job(
            title="Check the errand list",
            assignee="test-agent",
            delegated_by="somebody",
            route="inbox",
            origin="task:root",
        )
        store.record_run(
            resident="test-agent",
            agent_id="claude-code:test-agent",
            kind="delegated",
            run_id=letter.task_id,
            ref=letter.task_id,
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
    # A routine came off no task, so it is named unattributed rather than dropped.
    assert payload["by_origin"] == [
        {"origin": "unattributed", "runs": 1, "cost_usd": 1.0, "tokens": 100, "duration_s": 0.0}
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


# --------------------------------------------------------------------------------------
# delegation
# --------------------------------------------------------------------------------------

RECEIVER_SOUL = """---
agent_id: claude-code:receiver-agent
name: Recy
char: Monk
accent: "#a68a4f"
role: test bot
---
A villager that exists only inside a test.
"""


def delegation_fleet(write_resident: ResidentWriter) -> Path:
    """Write a permitted sender and a declared receiver, and return the tree."""
    sender = valid_manifest()
    sender["delegation"] = {"send": True}
    residents_dir = write_resident(sender).parent.parent

    receiver = valid_manifest()
    receiver["id"] = "receiver-agent"
    receiver["agent_id"] = "claude-code:receiver-agent"
    receiver["soul"]["name"] = "Recy"
    receiver["routes"] = [
        *receiver["routes"],
        {"id": "inbox", "kind": "delegation", "address": "steward:delegation"},
    ]
    write_resident(receiver, soul=RECEIVER_SOUL, root=residents_dir)
    return residents_dir


def test_delegate_hands_work_over_and_prints_the_task_id(
    runner: CliRunner,
    write_resident: ResidentWriter,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The token-free path: a session with a shell, not with steward's API token."""
    monkeypatch.setenv("STEWARD_EVENTS_FALLBACK", str(tmp_path / "events.jsonl"))
    monkeypatch.delenv("BURROW_URL", raising=False)
    residents_dir = delegation_fleet(write_resident)
    db = tmp_path / "delegation.db"

    result = runner.invoke(
        main,
        [
            "delegate", "test-agent",
            "--to", "receiver-agent",
            "--route", "inbox",
            "--title", "Read the background",
            "--detail", "everything they need",
            "--residents", str(residents_dir),
            "--db", str(db),
        ],
    )  # fmt: skip
    assert result.exit_code == 0, result.output
    assert "test-agent → receiver-agent via inbox" in result.output
    task_id = result.output.strip().splitlines()[-1]

    with Store(db) as store:
        (waiting,) = store.inbox("receiver-agent")
    assert waiting.task_id == task_id
    assert waiting.detail == "everything they need"

    emitted = [
        json.loads(line)
        for line in (tmp_path / "events.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert [event["type"] for event in emitted] == ["task_delegated"]


def test_delegate_accepts_a_json_detail(
    runner: CliRunner,
    write_resident: ResidentWriter,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("STEWARD_EVENTS_FALLBACK", str(tmp_path / "events.jsonl"))
    residents_dir = delegation_fleet(write_resident)
    db = tmp_path / "delegation.db"
    result = runner.invoke(
        main,
        [
            "delegate", "test-agent",
            "--to", "receiver-agent", "--route", "inbox", "--title", "Read it",
            "--detail-json", '{"question": "what is on the list?"}',
            "--residents", str(residents_dir), "--db", str(db),
        ],
    )  # fmt: skip
    assert result.exit_code == 0, result.output
    with Store(db) as store:
        (waiting,) = store.inbox("receiver-agent")
    assert json.loads(waiting.detail) == {"question": "what is on the list?"}


def test_delegate_refuses_loudly_and_writes_nothing(
    runner: CliRunner,
    write_resident: ResidentWriter,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("STEWARD_EVENTS_FALLBACK", str(tmp_path / "events.jsonl"))
    residents_dir = write_resident(valid_manifest()).parent.parent
    db = tmp_path / "delegation.db"
    result = runner.invoke(
        main,
        [
            "delegate", "test-agent",
            "--to", "nobody", "--route", "inbox", "--title", "Read it",
            "--residents", str(residents_dir), "--db", str(db),
        ],
    )  # fmt: skip
    assert result.exit_code == 1
    assert "refused (unknown_recipient)" in result.output
    with Store(db) as store:
        assert store.jobs() == []
    assert not (tmp_path / "events.jsonl").exists(), "a refusal emits nothing"


def test_delegate_refuses_two_kinds_of_detail(
    runner: CliRunner, write_resident: ResidentWriter, tmp_path: Path
) -> None:
    residents_dir = delegation_fleet(write_resident)
    both = runner.invoke(
        main,
        [
            "delegate", "test-agent", "--to", "receiver-agent", "--route", "inbox",
            "--title", "t", "--detail", "a", "--detail-json", "{}",
            "--residents", str(residents_dir), "--db", str(tmp_path / "d.db"),
        ],
    )  # fmt: skip
    assert both.exit_code == 1
    assert "not both" in both.output

    broken = runner.invoke(
        main,
        [
            "delegate", "test-agent", "--to", "receiver-agent", "--route", "inbox",
            "--title", "t", "--detail-json", "{oops",
            "--residents", str(residents_dir), "--db", str(tmp_path / "d.db"),
        ],
    )  # fmt: skip
    assert broken.exit_code == 1
    assert "--detail-json does not parse" in broken.output


def test_delegate_needs_a_sender_that_exists(
    runner: CliRunner, write_resident: ResidentWriter, tmp_path: Path
) -> None:
    residents_dir = delegation_fleet(write_resident)
    result = runner.invoke(
        main,
        [
            "delegate", "ghost", "--to", "receiver-agent", "--route", "inbox", "--title", "t",
            "--residents", str(residents_dir), "--db", str(tmp_path / "d.db"),
        ],
    )  # fmt: skip
    assert result.exit_code == 1
    assert "no valid resident 'ghost'" in result.output


def test_inbox_shows_what_is_waiting_and_what_is_not(
    runner: CliRunner, write_resident: ResidentWriter, tmp_path: Path
) -> None:
    residents_dir = delegation_fleet(write_resident)
    db = tmp_path / "delegation.db"
    empty = runner.invoke(
        main, ["inbox", "receiver-agent", "--residents", str(residents_dir), "--db", str(db)]
    )
    assert empty.exit_code == 0, empty.output
    assert "nothing open in receiver-agent's inbox" in empty.output
    assert "routes accepting delegated work: inbox" in empty.output

    with Store(db) as store:
        store.delegate_job(
            title="Read the background",
            assignee="receiver-agent",
            delegated_by="test-agent",
            route="inbox",
        )
    listed = runner.invoke(
        main, ["inbox", "receiver-agent", "--residents", str(residents_dir), "--db", str(db)]
    )
    assert "Read the background" in listed.output
    assert "from test-agent via inbox" in listed.output

    payload = json.loads(
        runner.invoke(
            main,
            [
                "inbox", "receiver-agent", "--status", "all", "--format", "json",
                "--residents", str(residents_dir), "--db", str(db),
            ],
        ).output
    )  # fmt: skip
    assert payload[0]["assignee"] == "receiver-agent"


def test_task_lineage_prints_the_whole_chain(
    runner: CliRunner, write_resident: ResidentWriter, tmp_path: Path
) -> None:
    residents_dir = delegation_fleet(write_resident)
    db = tmp_path / "delegation.db"
    with Store(db) as store:
        root = store.post_job(title="The root task")
        child = store.delegate_job(
            title="The handed-over half",
            assignee="receiver-agent",
            delegated_by="test-agent",
            route="inbox",
            parent_task_id=root.task_id,
            origin=f"task:{root.task_id}",
        )
    _ = residents_dir

    result = runner.invoke(main, ["task", "lineage", child.task_id, "--db", str(db)])
    assert result.exit_code == 0, result.output
    assert f"origin task:{root.task_id}" in result.output
    assert "The root task" in result.output
    assert "test-agent → receiver-agent" in result.output

    payload = json.loads(
        runner.invoke(
            main, ["task", "lineage", child.task_id, "--db", str(db), "--format", "json"]
        ).output
    )
    assert [item["task_id"] for item in payload] == [root.task_id, child.task_id]

    missing = runner.invoke(main, ["task", "lineage", "nobody", "--db", str(db)])
    assert missing.exit_code == 1


def test_board_list_marks_a_letter_as_a_letter(
    runner: CliRunner, write_resident: ResidentWriter, tmp_path: Path
) -> None:
    """A delegated item is on the same table, and the listing must not call it claimable."""
    residents_dir = delegation_fleet(write_resident)
    db = tmp_path / "delegation.db"
    with Store(db) as store:
        store.delegate_job(
            title="Read the background",
            assignee="receiver-agent",
            delegated_by="test-agent",
            route="inbox",
        )
    result = runner.invoke(
        main, ["board", "list", "--residents", str(residents_dir), "--db", str(db)]
    )
    assert result.exit_code == 0, result.output
    assert "delegated: test-agent → receiver-agent via inbox" in result.output
    assert "claimable by" not in result.output


def test_board_dispatch_reports_a_letter_it_worked(
    runner: CliRunner, write_resident: ResidentWriter, tmp_path: Path, stub_bin: StubWriter
) -> None:
    residents_dir = delegation_fleet(write_resident)
    db = tmp_path / "delegation.db"
    with Store(db) as store:
        store.delegate_job(
            title="Read the background",
            assignee="receiver-agent",
            delegated_by="test-agent",
            route="inbox",
        )
    stub_bin("claude", 'echo \'{"result": "read it", "is_error": false}\'')

    result = runner.invoke(
        main,
        ["board", "dispatch", "--residents", str(residents_dir), "--db", str(db),
         "--workdir", str(tmp_path)],
    )  # fmt: skip
    assert result.exit_code == 0, result.output
    assert "done receiver-agent" in result.output
    assert "delegated by test-agent" in result.output


# ======================================================================================
# the nursery: `steward new-resident` and `steward retire`
# ======================================================================================

CHARTER_YAML = """mission: Keep the village's notes in order.
duties:
  - Tidy the notes each evening.
rules:
  - Never delete a note without asking.
escalation: Raise needs_human before anything irreversible.
"""


@pytest.fixture
def charter_file(tmp_path: Path) -> Path:
    path = tmp_path / "charter.yaml"
    path.write_text(CHARTER_YAML, encoding="utf-8")
    return path


@pytest.fixture
def nas(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> LocalTransport:
    """Point the CLI's default transport at a directory instead of the real NAS.

    ``transport_for`` is the documented seam: the pipeline builds the ssh transport its
    manifest addresses unless somebody hands it one. A CLI test hands it one here rather
    than growing a flag nobody would ever use in production.
    """
    host = LocalTransport(root=tmp_path / "nas")
    monkeypatch.setattr("steward.nursery.transport_for", lambda _target: host)
    monkeypatch.setenv("BURROW_URL", "http://dxp2800:8737")
    monkeypatch.setenv("BURROW_TOKEN", "cli-village-token")
    return host


def new_resident_argv(repo: ScratchRepo, charter: Path, *extra: str) -> list[str]:
    """Build the full command line, so each test varies only what it is about."""
    return [
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
        str(charter),
        "--residents",
        str(repo.residents),
        "--repo",
        str(repo.root),
        *extra,
    ]


def test_new_resident_raises_a_resident_end_to_end(
    runner: CliRunner, scratch_repo: ScratchRepo, charter_file: Path, nas: LocalTransport
) -> None:
    result = runner.invoke(main, new_resident_argv(scratch_repo, charter_file))

    assert result.exit_code == 0, result.output
    assert "note-keeper is raised" in result.output
    assert (scratch_repo.residents / "note-keeper" / "soul.md").is_file()
    assert scratch_repo.log()[0] == "feat(residents): declare note-keeper"
    assert (nas.root / "docker" / "steward-note-keeper" / "docker-compose.yaml").is_file()


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
    assert "no event was emitted on its behalf" in result.output
    assert scratch_repo.log()[0] == "chore(residents): retire note-keeper"
    assert nas.calls[-1][-2:] == ("down", "--remove-orphans")


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


@pytest.mark.usefixtures("nas")
def test_doctor_says_a_retired_resident_fires_nothing(
    runner: CliRunner,
    scratch_repo: ScratchRepo,
    charter_file: Path,
    tmp_path: Path,
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

    result = runner.invoke(
        main, ["doctor", str(scratch_repo.residents), "--db", str(tmp_path / "doctor.db")]
    )

    assert "retired — fires nothing" in result.output
    assert "no enabled routines" in result.output
