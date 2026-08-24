"""The CLI is what CI gates on, so its exit codes are part of the contract."""

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from conftest import REPO_ROOT, ResidentWriter, StubWriter, valid_manifest
from steward.cli import main


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
