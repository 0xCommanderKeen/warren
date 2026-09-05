"""CLI behavior: delegation."""

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from conftest import (
    SECOND_RESIDENT_UID,
    ResidentWriter,
    StubWriter,
    valid_manifest,
)
from steward.cli import main
from steward.store import Store
from support.cli import (
    runner as runner,  # noqa: PLC0414 — pytest fixture discovery
)

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
    receiver["uid"] = SECOND_RESIDENT_UID
    receiver["id"] = "receiver-agent"
    receiver["agent_id"] = "claude-code:receiver-agent"
    receiver["home"] = 1
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
    monkeypatch.delenv("CHRONICLE_URL", raising=False)
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


def test_task_lineage_from_the_root_still_shows_the_descendants(
    runner: CliRunner, write_resident: ResidentWriter, tmp_path: Path
) -> None:
    """The root is the only id POST /delegate hands back, so it must answer too (#202)."""
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

    from_root = runner.invoke(main, ["task", "lineage", root.task_id, "--db", str(db)])
    assert from_root.exit_code == 0, from_root.output
    assert "The handed-over half" in from_root.output
    assert "test-agent → receiver-agent" in from_root.output

    def ids(task_id: str) -> list[str]:
        raw = runner.invoke(
            main, ["task", "lineage", task_id, "--db", str(db), "--format", "json"]
        ).output
        return [item["task_id"] for item in json.loads(raw)]

    assert ids(root.task_id) == [root.task_id, child.task_id]
    assert ids(child.task_id) == ids(root.task_id)


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
