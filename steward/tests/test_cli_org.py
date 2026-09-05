"""CLI behavior: org."""

import json
from pathlib import Path

from click.testing import CliRunner

from conftest import (
    SECOND_RESIDENT_UID,
    ResidentWriter,
    valid_manifest,
)
from steward.cli import main
from support.cli import (
    runner as runner,  # noqa: PLC0414 — pytest fixture discovery
)

# --------------------------------------------------------------------------------------
# steward org (warren#441)
# --------------------------------------------------------------------------------------


ORG_SOUL = """---
agent_id: claude-code:receiver-agent
name: Recy
char: Monk
accent: "#a68a4f"
role: test bot
---
A villager that exists only inside a test.
"""


def org_tree(write_resident: ResidentWriter, tmp_path: Path) -> Path:
    """Build a sender and the receiver its manifest names, in one throwaway tree."""
    sender = valid_manifest()
    sender["delegation"] = {"send": True, "to": ["receiver-agent"]}
    write_resident(sender)
    receiver = valid_manifest()
    receiver["uid"] = SECOND_RESIDENT_UID
    receiver["id"] = "receiver-agent"
    receiver["agent_id"] = "claude-code:receiver-agent"
    receiver["home"] = 1
    receiver["soul"]["name"] = "Recy"
    receiver["budgets"] = {"daily_cost_usd": 5.0}
    receiver["deploy"] = {"mounts": [{"host": "~/Life", "container": "/vault", "mode": "rw"}]}
    receiver["routes"] = [
        *receiver["routes"],
        {"id": "inbox", "kind": "delegation", "address": "steward:delegation", "status": "active"},
    ]
    write_resident(receiver, soul=ORG_SOUL)
    return tmp_path / "residents"


def test_org_prints_the_receiver_indented_under_the_resident_that_may_send_to_it(
    runner: CliRunner, write_resident: ResidentWriter, tmp_path: Path
) -> None:
    tree = org_tree(write_resident, tmp_path)

    result = runner.invoke(main, ["org", "--residents", str(tree)])

    assert result.exit_code == 0
    lines = result.output.splitlines()
    assert lines[0].startswith("test-agent (")
    receiver = next(line for line in lines if line.strip().startswith("receiver-agent ("))
    assert receiver.startswith("  "), receiver


def test_org_says_no_cap_and_none_rather_than_going_quiet(
    runner: CliRunner, write_resident: ResidentWriter, tmp_path: Path
) -> None:
    """Unlimited must not read as unknown, and neither must "touches nothing"."""
    tree = org_tree(write_resident, tmp_path)

    output = runner.invoke(main, ["org", "--residents", str(tree)]).output

    assert "budget: no cap" in output
    assert "mounts: none" in output
    assert "budget: $5/day" in output
    assert "mounts: /vault (rw)" in output


def test_org_names_the_receivers_rather_than_leaving_them_to_the_indentation(
    runner: CliRunner, write_resident: ResidentWriter, tmp_path: Path
) -> None:
    """A resident with two managers sits under one row, so the edge has to be said."""
    tree = org_tree(write_resident, tmp_path)

    output = runner.invoke(main, ["org", "--residents", str(tree)]).output

    assert "hands work to: receiver-agent" in output
    assert "hands work to: nobody (no delegation grant)" in output


def test_org_marks_a_receiver_it_would_refuse_rather_than_listing_it_as_a_handoff(
    runner: CliRunner, write_resident: ResidentWriter, tmp_path: Path
) -> None:
    sender = valid_manifest()
    sender["delegation"] = {"send": True, "to": ["ghost"]}
    write_resident(sender)

    output = runner.invoke(main, ["org", "--residents", str(tmp_path / "residents")]).output

    assert "hands work to: ghost (refused)" in output


def test_org_json_is_the_same_projection_the_api_answers_with(
    runner: CliRunner, write_resident: ResidentWriter, tmp_path: Path
) -> None:
    tree = org_tree(write_resident, tmp_path)

    result = runner.invoke(main, ["org", "--residents", str(tree), "--format", "json"])

    body = json.loads(result.output)
    assert result.exit_code == 0
    assert body["edges"] == [
        {
            "sender": "test-agent",
            "receiver": "receiver-agent",
            "named": True,
            "deliverable": True,
            "reason": None,
        }
    ]
    assert body["errors"] == []


def test_org_names_a_declared_handoff_that_would_not_deliver(
    runner: CliRunner, write_resident: ResidentWriter, tmp_path: Path
) -> None:
    sender = valid_manifest()
    sender["delegation"] = {"send": True, "to": ["ghost"]}
    write_resident(sender)

    result = runner.invoke(main, ["org", "--residents", str(tmp_path / "residents")])

    assert result.exit_code == 0
    assert "test-agent -> ghost" in result.output


def test_org_exits_invalid_when_a_manifest_does_not_validate(
    runner: CliRunner, write_resident: ResidentWriter, tmp_path: Path
) -> None:
    """The same exit code `validate` uses: a chart drawn from half a tree is not an answer."""
    write_resident(valid_manifest())
    broken = tmp_path / "residents" / "broken"
    broken.mkdir()
    (broken / "manifest.yaml").write_text("version: 0\nid: broken\n", encoding="utf-8")

    result = runner.invoke(main, ["org", "--residents", str(tmp_path / "residents")])

    assert result.exit_code == 1
