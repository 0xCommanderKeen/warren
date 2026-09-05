# ruff: noqa: ANN401 — fixture factories deliberately accept malformed external values
"""A note's run receipt, repository and file authority must agree."""

import json
from pathlib import Path
from typing import Any

import pytest

from steward.queue_report import read_note
from steward.store import Store


def note_file(root, **fields: Any):
    note = {
        "run_id": "run1",
        "repository": "owner/repo",
        "commit": "a" * 40,
        "recommendations": [
            {
                "number": 466,
                "reason": "It is unblocked.",
                "evidence": [{"source": "gh issue view 441", "quote": "CLOSED"}],
            }
        ],
        **fields,
    }
    (root / "queue-review.json").write_text(json.dumps(note))


def receipt(db, **fields: Any):
    return db.record_run(
        resident="karen",
        agent_id="resident:karen",
        kind="routine",
        trigger="schedule",
        ref="queue-review",
        run_id="run1",
        **fields,
    )


def test_note_is_only_published_after_a_matching_successful_run(tmp_path: Path):
    note_file(tmp_path)
    assert read_note(tmp_path, None, "owner/repo")["note"] is None
    with Store(":memory:") as db:
        result = read_note(tmp_path, receipt(db, outcome="ok"), "owner/repo")
    assert result["note"]["recommendations"][0]["number"] == 466
    assert result["run"]["run_id"] == "run1"


@pytest.mark.parametrize(
    "fields",
    [
        {"run_id": "forged"},
        {"repository": "other/repo"},
        {"recommendations": [{"number": 1, "reason": "guess", "evidence": []}]},
    ],
)
def test_untraceable_or_mismatched_notes_are_not_published(tmp_path: Path, fields):
    note_file(tmp_path, **fields)
    with Store(":memory:") as db:
        assert read_note(tmp_path, receipt(db, outcome="ok"), "owner/repo")["note"] is None


def test_failed_run_and_symlink_notes_are_not_published(tmp_path: Path):
    note_file(tmp_path)
    with Store(":memory:") as db:
        assert read_note(tmp_path, receipt(db, outcome="failed"), "owner/repo")["note"] is None
        original = tmp_path / "queue-review.json"
        original.rename(tmp_path / "target.json")
        original.symlink_to(tmp_path / "target.json")
        assert read_note(tmp_path, receipt(db, outcome="ok"), "owner/repo")["note"] is None
