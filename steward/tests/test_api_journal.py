"""API behavior: journal."""

import copy
import datetime as dt
from pathlib import Path

from conftest import (
    valid_manifest,
)
from steward import journal
from steward.manifest import validate_tree
from support.api import (
    ApiFactory,
)
from support.api import (
    api as api,  # noqa: PLC0414 — pytest fixture discovery
)


def test_the_journal_endpoint_reads_what_the_resident_wrote(
    api: ApiFactory, tmp_path: Path
) -> None:
    """GET /residents/{id}/journal returns real entries, newest first."""
    manifest = copy.deepcopy(valid_manifest())
    manifest["memory"]["path"] = str(tmp_path / "memory")
    harness = api(manifest=manifest)
    resident = validate_tree(harness.residents_dir).residents[0]
    journal.write_entry(resident.manifest, dt.date(2026, 8, 23), "close-of-day", "A quiet day.")
    journal.write_entry(resident.manifest, dt.date(2026, 8, 24), "close-of-day", "A loud day.")

    body = harness.client.get("/residents/test-agent/journal").json()

    assert body["resident"] == "test-agent"
    assert [entry["text"].strip() for entry in body["entries"]] == ["A loud day.", "A quiet day."]
    limited = harness.client.get("/residents/test-agent/journal", params={"limit": 1}).json()
    assert len(limited["entries"]) == 1


def test_an_empty_journal_is_an_empty_list_and_unknown_residents_are_404(
    api: ApiFactory, tmp_path: Path
) -> None:
    """A resident that never wrote renders as one that never wrote; a stranger is a 404."""
    manifest = copy.deepcopy(valid_manifest())
    manifest["memory"]["path"] = str(tmp_path / "memory")
    harness = api(manifest=manifest)

    assert harness.client.get("/residents/test-agent/journal").json()["entries"] == []
    assert harness.client.get("/residents/nobody/journal").status_code == 404
