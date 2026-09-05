"""CLI behavior: notify."""

import json
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner

from conftest import (
    VALID_RESIDENT_UID,
    VALID_SOUL,
    ResidentWriter,
    StubWriter,
    valid_manifest,
)
from steward import notify as nf
from steward.cli import main
from support.cli import (
    CURRENT_CLAUDE,
)
from support.cli import (
    runner as runner,  # noqa: PLC0414 — pytest fixture discovery
)

# ------------------------------------------------------------- notifications (warren#114)


def tapping_manifest() -> dict[str, Any]:
    data = valid_manifest()
    data["notifications"] = {"transport": "ntfy", "on": ["needs_human"], "note": "Miha's phone"}
    return data


@pytest.mark.parametrize("explicit_seed", [False, True])
def test_notify_list_defaults_to_live_checkout_when_seed_disagrees(
    runner: CliRunner,
    write_resident: ResidentWriter,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    explicit_seed: bool,
) -> None:
    """A bare operator command must not verify the image's seed back to them."""
    write_resident(tapping_manifest())
    checkout = tmp_path / "checkout" / "steward" / "residents"
    write_resident(root=checkout)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("STEWARD_RESIDENTS", str(checkout))

    args = ["notify", "list"] + (["--residents", "residents"] if explicit_seed else [])
    result = runner.invoke(main, args)

    assert result.exit_code == 0, result.output
    expected = "test-agent: ntfy — active" if explicit_seed else "test-agent: taps nobody"
    assert expected in result.output


@pytest.mark.parametrize("command", [["validate"], ["doctor"], ["skills"], ["journal", "live"]])
def test_other_cli_defaults_read_live_checkout(
    write_resident: ResidentWriter,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    stub_bin: StubWriter,
    command: list[str],
) -> None:
    """Positional defaults and individually declared options use the same tree."""
    write_resident()
    manifest = valid_manifest()
    manifest["id"] = "live"
    manifest["agent_id"] = "claude-code:live"
    manifest["memory"]["path"] = str(tmp_path / "memory")
    checkout = tmp_path / "checkout" / "steward" / "residents"
    write_resident(manifest, root=checkout, soul=VALID_SOUL.replace("test-agent", "live"))
    stub_bin("claude", CURRENT_CLAUDE)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("STEWARD_RESIDENTS", str(checkout))

    result = CliRunner().invoke(main, command)

    assert result.exit_code == 0, result.output
    assert (str(checkout) if command == ["validate"] else "live") in result.output
    assert "test-agent" not in result.output


def test_notify_list_prints_the_address_an_operator_has_to_subscribe_to(
    runner: CliRunner, write_resident: ResidentWriter
) -> None:
    """The derived topic is written down nowhere else, so this command is the setup path."""
    tree = write_resident(tapping_manifest()).parent.parent

    result = runner.invoke(main, ["notify", "list", "--residents", str(tree)])

    assert result.exit_code == 0
    assert "test-agent: ntfy — active" in result.output
    assert "on:      needs_human" in result.output
    assert nf.ntfy_topic(VALID_RESIDENT_UID, "pytest") in result.output
    assert "Miha's phone" in result.output


def test_notify_list_says_plainly_when_a_resident_taps_nobody(
    runner: CliRunner, write_resident: ResidentWriter
) -> None:
    tree = write_resident().parent.parent
    result = runner.invoke(main, ["notify", "list", "--residents", str(tree)])
    assert result.exit_code == 0
    assert "taps nobody" in result.output


def test_notify_list_marks_a_declaration_that_is_not_live_yet(
    runner: CliRunner, write_resident: ResidentWriter
) -> None:
    data = tapping_manifest()
    data["notifications"]["status"] = "pending"
    tree = write_resident(data).parent.parent

    result = runner.invoke(main, ["notify", "list", "--residents", str(tree)])

    assert "pending — declared, and silent" in result.output


def test_notify_list_json_is_the_machine_view(
    runner: CliRunner, write_resident: ResidentWriter
) -> None:
    tree = write_resident(tapping_manifest()).parent.parent

    result = runner.invoke(main, ["notify", "list", "--residents", str(tree), "--format", "json"])

    (row,) = json.loads(result.output)
    assert row["transport"] == "ntfy"
    assert row["enabled"] is True
    assert row["address"].endswith(nf.ntfy_topic(VALID_RESIDENT_UID, "pytest"))


def test_notify_list_over_an_empty_tree_says_so(runner: CliRunner, tmp_path: Path) -> None:
    empty = tmp_path / "residents"
    empty.mkdir()
    result = runner.invoke(main, ["notify", "list", "--residents", str(empty)])
    assert result.exit_code == 0
    assert "no valid residents" in result.output


def test_notify_test_refuses_a_resident_that_never_opted_in(
    runner: CliRunner, write_resident: ResidentWriter
) -> None:
    """This command proves a declaration; it does not stand in for one."""
    tree = write_resident().parent.parent

    result = runner.invoke(main, ["notify", "test", "test-agent", "--residents", str(tree)])

    assert result.exit_code == 1
    assert "declares no notifications block" in result.output


def test_notify_test_refuses_a_declaration_that_is_not_active(
    runner: CliRunner, write_resident: ResidentWriter
) -> None:
    data = tapping_manifest()
    data["notifications"]["status"] = "disabled"
    tree = write_resident(data).parent.parent

    result = runner.invoke(main, ["notify", "test", "test-agent", "--residents", str(tree)])

    assert result.exit_code == 1
    assert "status is 'disabled'" in result.output


def test_notify_test_reports_a_transport_it_could_not_reach(
    runner: CliRunner, write_resident: ResidentWriter
) -> None:
    """The suite points ntfy at a closed loopback port, which is exactly this case."""
    tree = write_resident(tapping_manifest()).parent.parent

    result = runner.invoke(main, ["notify", "test", "test-agent", "--residents", str(tree)])

    assert result.exit_code == 1
    assert "not sent" in result.output


def test_notify_test_says_where_it_landed(
    runner: CliRunner, write_resident: ResidentWriter, monkeypatch: pytest.MonkeyPatch
) -> None:
    sent: list[nf.Tap] = []
    monkeypatch.setattr(
        nf.NtfyTransport, "send", lambda _self, _manifest, tap: bool(sent.append(tap)) or True
    )
    tree = write_resident(tapping_manifest()).parent.parent

    result = runner.invoke(main, ["notify", "test", "test-agent", "--residents", str(tree)])

    assert result.exit_code == 0
    assert "sent —" in result.output
    assert [tap.kind for tap in sent] == ["test"]
