"""Declaring a resident writes two files — and they have to pass the ordinary validator."""

from pathlib import Path
from typing import Any

import pytest
import yaml

from steward.manifest import Diagnostic, ValidationResult, validate_tree
from steward.nursery import NewResident, NurseryError, declare_resident

CHARTER: dict[str, Any] = {
    "mission": "Keep the village's notes in order.",
    "duties": ["Tidy the notes each evening."],
    "rules": ["Never delete a note without asking."],
    "escalation": "Raise needs_human before anything irreversible.",
}


def spec(**overrides: Any) -> NewResident:  # noqa: ANN401 — a test factory takes anything
    """Build the minimum a caller must say to declare a resident."""
    data: dict[str, Any] = {
        "id": "note-keeper",
        "name": "Quill",
        "char": "Scribe",
        "accent": "#4f7ea6",
        "role": "note bot",
        "charter": CHARTER,
    }
    return NewResident.model_validate(data | overrides)


def test_a_declared_resident_passes_the_validator(tmp_path: Path) -> None:
    created = declare_resident(spec(), tmp_path)

    assert created.manifest_path.is_file()
    assert created.soul_path.is_file()
    result = validate_tree(tmp_path)
    assert result.ok, [d.render() for d in result.errors]
    assert [resident.id for resident in result.residents] == ["note-keeper"]


def test_the_declaration_deploys_nothing_and_schedules_nothing(tmp_path: Path) -> None:
    created = declare_resident(spec(), tmp_path)
    manifest = yaml.safe_load(created.manifest_path.read_text(encoding="utf-8"))

    assert manifest["routines"] == []
    assert sorted(path.name for path in created.directory.iterdir()) == ["manifest.yaml", "soul.md"]


def test_identity_is_derived_from_the_runner_when_nobody_says(tmp_path: Path) -> None:
    claude = declare_resident(spec(), tmp_path)
    codex = declare_resident(
        spec(id="scribe-two", runner={"kind": "codex", "model": "gpt-5"}), tmp_path
    )
    assert claude.resident.manifest.agent_id == "claude-code:note-keeper"
    assert codex.resident.manifest.agent_id == "codex:scribe-two"
    assert codex.resident.manifest.runner.model == "gpt-5"


def test_a_project_scoped_resident_keeps_its_project(tmp_path: Path) -> None:
    created = declare_resident(spec(project="burrow"), tmp_path)
    assert created.resident.manifest.agent_id is None
    assert created.resident.manifest.project == "burrow"
    assert "project: burrow" in created.soul_path.read_text(encoding="utf-8")


def test_the_soul_body_carries_the_frontmatter_and_a_voice(tmp_path: Path) -> None:
    created = declare_resident(spec(soul_body="Quill keeps the notes.", voice="Terse."), tmp_path)
    text = created.soul_path.read_text(encoding="utf-8")

    assert text.startswith("---\n")
    assert "name: Quill" in text
    assert "Quill keeps the notes." in text
    assert "## Voice" in text
    assert created.resident.soul.voice == "Terse."


def test_declaring_over_an_existing_resident_is_refused(tmp_path: Path) -> None:
    declare_resident(spec(), tmp_path)
    with pytest.raises(NurseryError, match="already exists"):
        declare_resident(spec(), tmp_path)


def test_the_declared_paths_are_reported_for_review(tmp_path: Path) -> None:
    created = declare_resident(spec(summary="Keeps the notes."), tmp_path)
    payload = created.to_dict()

    assert payload["id"] == "note-keeper"
    assert payload["manifest_path"].endswith("note-keeper/manifest.yaml")
    assert payload["soul_path"].endswith("note-keeper/soul.md")
    assert payload["agent_id"] == "claude-code:note-keeper"


def test_a_skeleton_that_does_not_validate_is_not_left_behind(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A half-valid skeleton would break `steward validate` for everyone until deleted."""
    broken = ValidationResult(
        diagnostics=(
            Diagnostic(file=tmp_path, field_path="soul", problem="pretend", example="pretend"),
        )
    )
    monkeypatch.setattr("steward.nursery.validate_manifest", lambda _path: broken)

    with pytest.raises(NurseryError, match="does not validate"):
        declare_resident(spec(), tmp_path)
    assert not (tmp_path / "note-keeper").exists()


def test_a_declaration_that_cannot_bind_to_the_schema_is_refused(tmp_path: Path) -> None:
    with pytest.raises(NurseryError, match="cannot declare"):
        declare_resident(spec(agent_id="no-colon-here"), tmp_path)
    assert not (tmp_path / "note-keeper").exists()
