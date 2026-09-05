"""Shared CLI harnesses and behavioral setup."""

from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner

from conftest import (
    ScratchRepo,
    valid_manifest,
)
from steward.deploy import LocalTransport
from steward.store import Store

#: A `claude` current enough for the flag every session carries (steward #206). Doctor
#: probes `--setting-sources` for *every* claude resident, declarations or not, so a stub
#: that answers `--help` with nothing is now a red doctor rather than a quiet one.
CURRENT_CLAUDE = (
    'echo "  --setting-sources <sources>"; echo "  --settings <file-or-json>"; '
    'echo "  --add-dir <directories...>"'
)


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


# ------------------------------------------------------------------------------ journal


def journaling_resident(tmp_path: Path) -> dict:
    data = valid_manifest()
    data["memory"] = {"kind": "directory", "path": str(tmp_path / "memory")}
    return data


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
            trigger="schedule",
            run_id="already-ran",
            ref="daily-summary",
            origin=f"resident:{resident}",
            cost_usd=cost,
            input_tokens=50,
            output_tokens=50,
        )


# ------------------------------------------------------------- supervision topology (#59)


def supervised_manifest(**deploy: object) -> dict[str, Any]:
    """Build a manifest that names a container, so there is something to supervise."""
    data = valid_manifest()
    data["runner"] = {"kind": "claude"}
    data["deploy"] = {"container": "steward-test-agent", **deploy}
    return data


def docker_naming_itself(name: str) -> str:
    """Return a `docker` stub that answers `info` with a name and refuses everything else.

    Only `info`, deliberately: a stub that answered every subcommand the same way would
    have `docker inspect --format {{.State.Running}}` print a daemon name, which the
    watchdog reads as "not running" and dutifully restarts — turning a topology test into
    a restart test.
    """
    return f"case \"$1\" in info) printf '{name}\\t27.3.1\\n' ;; *) exit 1 ;; esac"


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
    monkeypatch.setattr("steward.nursery.transport_for", lambda _target, _env=None: host)
    monkeypatch.setenv("CHRONICLE_URL", "http://dxp2800:8737")
    monkeypatch.setenv("CHRONICLE_TOKEN", "cli-village-token")
    monkeypatch.setenv("STEWARD_URL", "http://dxp2800:8802")
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
