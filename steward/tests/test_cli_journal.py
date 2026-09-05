"""CLI behavior: journal."""

import json
from datetime import date
from pathlib import Path

from click.testing import CliRunner

from conftest import (
    ResidentWriter,
    valid_manifest,
)
from steward.cli import main
from steward.journal import write_entry
from steward.manifest import load_manifest
from support.cli import (
    journaling_resident,
)
from support.cli import (
    runner as runner,  # noqa: PLC0414 — pytest fixture discovery
)


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
