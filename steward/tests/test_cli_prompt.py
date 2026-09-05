"""CLI behavior: prompt."""

import json
from datetime import date
from pathlib import Path

from click.testing import CliRunner

from conftest import (
    ResidentWriter,
    SkillWriter,
)
from steward.cli import main
from steward.journal import latest_entry, write_entry
from steward.manifest import load_manifest
from steward.prompt import JOURNAL_MAX_CHARS, assemble_preamble
from steward.skills import effective_skills, library_for
from steward.store import Store
from support.cli import (
    journaling_resident,
)
from support.cli import (
    runner as runner,  # noqa: PLC0414 — pytest fixture discovery
)

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


def test_show_redacts_the_journal_before_it_caps_it(
    runner: CliRunner, write_resident: ResidentWriter, tmp_path: Path
) -> None:
    """A cap applied first destroys the shape the detector matches on (steward #209).

    `redact_secrets` finds a PEM block by its BEGIN *and* END markers. Cut the entry at
    the injection cap first and a block straddling the cut loses its END, so only its
    lone BEGIN is replaced and the key material prints intact — right after a
    `[redacted:secret]` that makes it look as though the scrub worked.
    """
    path = write_resident(journaling_resident(tmp_path))
    manifest = load_manifest(path).manifest
    key_body = "MIIEowIBAAKCAQEA" + "Zx9Kq3" * 200
    entry = (
        "x" * (JOURNAL_MAX_CHARS - 200)
        + f"-----BEGIN RSA PRIVATE KEY-----\n{key_body}\n-----END RSA PRIVATE KEY-----"
    )
    write_entry(manifest, date(2026, 8, 24), "close-of-day", entry)

    result = runner.invoke(main, ["show", "test-agent", *show_args(path, tmp_path)])

    assert result.exit_code == 0, result.output
    assert "MIIEowIBAAKCAQEA" not in result.output
    assert "Zx9Kq3" not in result.output
    assert "[redacted:secret]" in result.output


def test_a_redacted_journal_is_still_capped(
    runner: CliRunner, write_resident: ResidentWriter, tmp_path: Path
) -> None:
    """Redacting first must not be a way around the cap the preview still owes."""
    path = write_resident(journaling_resident(tmp_path))
    manifest = load_manifest(path).manifest
    write_entry(manifest, date(2026, 8, 24), "close-of-day", "y" * (JOURNAL_MAX_CHARS * 2))

    result = runner.invoke(main, ["show", "test-agent", *show_args(path, tmp_path)])

    assert result.exit_code == 0, result.output
    assert "[truncated at the injection cap]" in result.output
    assert "y" * (JOURNAL_MAX_CHARS + 1) not in result.output


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
