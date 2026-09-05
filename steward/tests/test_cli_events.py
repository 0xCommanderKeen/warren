"""CLI behavior: events."""

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from steward import cli
from steward.cli import main
from support.cli import (
    runner as runner,  # noqa: PLC0414 — pytest fixture discovery
)


def test_events_flush_reports_delivery_and_exits_cleanly(
    runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fallback = tmp_path / "events.jsonl"
    monkeypatch.setenv("CHRONICLE_URL", "https://village.example")
    emitter = cli.ev.EventEmitter.from_env(
        {
            "CHRONICLE_URL": "https://village.example",
            "STEWARD_EVENTS_FALLBACK": str(fallback),
        }
    )
    event = cli.ev.Event(type="routine_started", agent_id="a", project="p")
    assert emitter._queue_record(event, "delivery-cli-0001")
    monkeypatch.setattr(cli.ev.EventEmitter, "_post", lambda *_args: True)

    result = runner.invoke(main, ["events", "flush", "--fallback", str(fallback)])
    assert result.exit_code == 0, result.output
    assert "delivered 1; retired-records 1; pending 0; corrupt 0" in result.output


def test_events_flush_failure_is_visible_and_nonzero(
    runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fallback = tmp_path / "events.jsonl"
    monkeypatch.setenv("CHRONICLE_URL", "https://village.example")
    emitter = cli.ev.EventEmitter(url="https://village.example", fallback=fallback)
    assert emitter._queue_record(
        cli.ev.Event(type="routine_started", agent_id="a", project="p"),
        "delivery-cli-0002",
    )
    monkeypatch.setattr(cli.ev.EventEmitter, "_post", lambda *_args: False)

    result = runner.invoke(main, ["events", "flush", "--fallback", str(fallback)])
    assert result.exit_code == 1
    assert "delivered 0; retired-records 0; pending 1" in result.output


def test_events_flush_still_drains_pending_when_legacy_read_fails(
    runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fallback = tmp_path / "events.jsonl"
    monkeypatch.setenv("CHRONICLE_URL", "https://village.example")
    emitter = cli.ev.EventEmitter(url="https://village.example", fallback=fallback)
    assert emitter._queue_record(
        cli.ev.Event(type="routine_started", agent_id="a", project="p"),
        "delivery-cli-legacy-error",
    )
    monkeypatch.setattr(
        cli.ev.EventEmitter,
        "import_legacy",
        lambda _self: cli.ev.ImportReport(errors=1, unknown=1),
    )
    monkeypatch.setattr(cli.ev.EventEmitter, "_post", lambda *_args: True)

    result = runner.invoke(
        main,
        ["events", "flush", "--fallback", str(fallback), "--include-legacy", "--format", "json"],
    )
    payload = json.loads(result.output)
    assert result.exit_code == 1
    assert payload["legacy_errors"] == 1
    assert payload["legacy_unknown"] == 1
    assert payload["delivered"] == 1
    assert payload["pending"] == 0
