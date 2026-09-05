"""CLI behavior: approvals."""

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from conftest import (
    ResidentWriter,
)
from steward.cli import main
from steward.store import Store
from support.cli import (
    runner as runner,  # noqa: PLC0414 — pytest fixture discovery
)

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
    monkeypatch.delenv("CHRONICLE_URL", raising=False)
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


@pytest.mark.parametrize("output_format", ["text", "json"])
def test_approval_show_scrubs_what_the_session_typed(
    runner: CliRunner, tmp_path: Path, output_format: str
) -> None:
    """The audit query is the output most likely to be pasted into an issue (steward #144).

    Both formats: `--format json` is the one more likely to be piped somewhere, not less.
    """
    db = tmp_path / "approvals.db"
    with Store(db) as store:
        record = store.create_approval_request(
            agent_id="claude-code:test-agent",
            project="p",
            action="rotate_token",
            message="need ghp_abcdefghijklmnopqrstuvwxyz0123456789 to rotate",
            detail={"to": "a@example.com", "auth": "Bearer ghp_zyxwvutsrqponmlkjihgfe98765"},
        )
        store.decide(
            record.request_id,
            "edit",
            decided_by="api",
            edit={"note": "use sk-ant-abcdef0123456789ghij"},
        )

    result = runner.invoke(
        main,
        ["approval", "show", record.request_id, "--db", str(db), "--format", output_format],
    )

    assert result.exit_code == 0, result.output
    assert "ghp_" not in result.output
    assert "sk-ant-" not in result.output
    assert "[redacted:secret]" in result.output
    # Only the secret is cut: the action still reads as itself and the address survives.
    assert "rotate_token" in result.output
    assert "a@example.com" in result.output


def test_approval_show_says_when_it_has_never_heard_of_a_request(
    runner: CliRunner, tmp_path: Path
) -> None:
    result = runner.invoke(main, ["approval", "show", "nope", "--db", str(tmp_path / "a.db")])
    assert result.exit_code == 1
    assert "no approval request 'nope'" in result.output
