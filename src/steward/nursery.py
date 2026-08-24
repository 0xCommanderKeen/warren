"""The nursery: declaring a new resident, in the one place that will ever do it.

Steward #4 turns raising a resident into three stages — **declare** (write the soul and
manifest into this repo, which is the source of truth), **provision** (materialize the
container on the NAS), **register** (hand its routines to the scheduler). This module
is the declare stage, and only the declare stage.

Writing files is deliberately all it does. ``POST /residents`` creates a resident the
way a human would: a manifest and a soul body, on disk, for review. Nothing is
deployed, nothing is scheduled, and no event is emitted on the new resident's behalf —
a villager appears in burrow when it genuinely exists and emits, never because steward
wrote a YAML file about it.

The declaration is written and then re-read through the ordinary validator, so
"``POST /residents`` produces something ``steward validate`` accepts" is a fact this
module checks rather than a claim it makes. A skeleton that fails validation is
removed instead of left in the tree.
"""

import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from steward.manifest import (
    MANIFEST_FILENAME,
    AppGrant,
    Charter,
    Diagnostic,
    Memory,
    Resident,
    ResidentManifest,
    Route,
    Runner,
    SkillGrant,
    SoulIdentity,
    validate_manifest,
)

__all__ = [
    "CreatedResident",
    "NewResident",
    "NurseryError",
    "declare_resident",
]

#: How a runner kind becomes the ``<source>`` half of a burrow agent id, when the
#: caller does not name one. Anything else is steward's own doing, and says so.
AGENT_ID_SOURCES = {"claude": "claude-code", "codex": "codex"}

DEFAULT_MEMORY_ROOT = "/data/residents"

DEFAULT_SOUL_BODY = (
    "A new resident of the village. This soul body is a skeleton written by steward's "
    "nursery — replace it with who this resident actually is before deploying them."
)

DEFAULT_VOICE = (
    "Plain and specific. Says what happened and what it does not know, and never "
    "dresses an assumption up as a fact."
)


class NurseryError(Exception):
    """Raised when a resident cannot be declared. Carries the diagnostics, when there are any."""

    def __init__(self, message: str, diagnostics: tuple[Diagnostic, ...] = ()) -> None:
        """Explain the refusal, and name the fields that caused it when known."""
        self.diagnostics = diagnostics
        super().__init__(message)


class NewResident(BaseModel):
    """What a caller must say to declare a resident. The API body and the CLI share it."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    id: str = Field(pattern=r"^[a-z0-9][a-z0-9-]*$", description="Directory under residents/.")
    name: str = Field(min_length=1, description="Display name, e.g. Hob.")
    char: str = Field(min_length=1, description="Burrow sprite key, e.g. Monk.")
    accent: str = Field(pattern=r"^#[0-9a-fA-F]{6}$", description="Hex accent colour.")
    role: str = Field(min_length=1, description="One-line role, e.g. life bot.")
    charter: Charter = Field(description="Mission, duties, hard rules, escalation policy.")

    agent_id: str | None = Field(default=None, description="Burrow identity; derived if absent.")
    project: str | None = Field(default=None, description="Project label, for a scoped soul.")
    summary: str | None = Field(default=None, description="One line burrow can display.")
    skills: list[SkillGrant] = Field(default_factory=list, description="Granted capabilities.")
    memory: Memory | None = Field(default=None, description="Memory location; derived if absent.")
    routes: list[Route] = Field(default_factory=list, description="Declared inbound channels.")
    app_grants: list[AppGrant] = Field(default_factory=list, description="Declared app access.")
    runner: Runner = Field(default_factory=Runner, description="Which brain this resident runs on.")
    soul_body: str | None = Field(default=None, description="Opening paragraph of soul.md.")
    voice: str | None = Field(default=None, description="The soul's ## Voice section.")

    def resolved_agent_id(self) -> str | None:
        """Return the burrow identity to declare, deriving one when only an id was given."""
        if self.agent_id or self.project:
            return self.agent_id
        source = AGENT_ID_SOURCES.get(self.runner.kind, "steward")
        return f"{source}:{self.id}"

    def resolved_memory(self) -> Memory:
        """Return the declared memory location, or the conventional one for this id."""
        if self.memory is not None:
            return self.memory
        return Memory(
            kind="directory", path=f"{DEFAULT_MEMORY_ROOT}/{self.id}/memory", journal="journal.md"
        )


@dataclass(frozen=True, slots=True)
class CreatedResident:
    """What the declare stage actually wrote, and the validated resident it read back."""

    id: str
    directory: Path
    manifest_path: Path
    soul_path: Path
    resident: Resident

    def to_dict(self) -> dict[str, Any]:
        """Return the JSON view: the paths a human should go and review."""
        return {
            "id": self.id,
            "directory": str(self.directory),
            "manifest_path": str(self.manifest_path),
            "soul_path": str(self.soul_path),
            "agent_id": self.resident.manifest.agent_id,
            "project": self.resident.manifest.project,
        }


def _soul_document(spec: NewResident, agent_id: str | None) -> str:
    """Render ``soul.md``: frontmatter that agrees with the manifest, then a body."""
    frontmatter: dict[str, Any] = {}
    if agent_id:
        frontmatter["agent_id"] = agent_id
    if spec.project:
        frontmatter["project"] = spec.project
    frontmatter |= {
        "name": spec.name,
        "char": spec.char,
        "accent": spec.accent,
        "role": spec.role,
    }
    header = yaml.safe_dump(frontmatter, sort_keys=False, allow_unicode=True).strip()
    body = (spec.soul_body or DEFAULT_SOUL_BODY).strip()
    voice = (spec.voice or DEFAULT_VOICE).strip()
    return f"---\n{header}\n---\n{body}\n\n## Voice\n\n{voice}\n"


def _manifest_model(spec: NewResident) -> ResidentManifest:
    """Bind the request into a manifest model, so an invalid one never reaches disk."""
    try:
        return ResidentManifest(
            id=spec.id,
            agent_id=spec.resolved_agent_id(),
            project=spec.project,
            summary=spec.summary,
            soul=SoulIdentity(name=spec.name, char=spec.char, accent=spec.accent, role=spec.role),
            charter=spec.charter,
            skills=spec.skills,
            memory=spec.resolved_memory(),
            routes=spec.routes,
            app_grants=spec.app_grants,
            runner=spec.runner,
            routines=[],
        )
    except ValidationError as exc:
        raise NurseryError(
            f"cannot declare resident {spec.id!r}: {exc.errors()[0]['msg']}"
        ) from exc


def declare_resident(spec: NewResident, residents_dir: Path | str) -> CreatedResident:
    """Write a resident's manifest and soul body, and read them back through the validator.

    Refuses to touch an existing resident: converging an existing declaration is the
    nursery's job in steward #4, and quietly overwriting a soul someone wrote is not
    something an API call should be able to do by accident.
    """
    root = Path(residents_dir)
    directory = root / spec.id
    if directory.exists():
        raise NurseryError(f"resident {spec.id!r} already exists at {directory}")

    manifest = _manifest_model(spec)
    payload = manifest.model_dump(mode="json", exclude_none=True)

    directory.mkdir(parents=True)
    manifest_path = directory / MANIFEST_FILENAME
    soul_path = directory / manifest.soul.file
    try:
        manifest_path.write_text(
            yaml.safe_dump(payload, sort_keys=False, allow_unicode=True), encoding="utf-8"
        )
        soul_path.write_text(_soul_document(spec, manifest.agent_id), encoding="utf-8")
        result = validate_manifest(manifest_path)
        if not result.ok or not result.residents:
            raise NurseryError(  # noqa: TRY301 — the cleanup below is the point of the try
                f"the declaration written for {spec.id!r} does not validate",
                result.errors,
            )
    except NurseryError, OSError:
        # A skeleton that does not validate is worse than no skeleton: it would break
        # `steward validate` for everyone until someone deleted it by hand.
        shutil.rmtree(directory, ignore_errors=True)
        raise

    return CreatedResident(
        id=spec.id,
        directory=directory,
        manifest_path=manifest_path,
        soul_path=soul_path,
        resident=result.residents[0],
    )
