"""Shared fixtures: throwaway resident directories, and stub CLIs on PATH."""

import copy
import os
import stat
import subprocess
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
RESIDENTS_DIR = REPO_ROOT / "residents"
VALID_RESIDENT_UID = "7e36d76a-1ad8-4d65-a619-8c6e7fb93ed9"
SECOND_RESIDENT_UID = "3a78217a-df03-4f3b-a46a-4c75b4ad929f"

VALID_SOUL = """---
agent_id: claude-code:test-agent
name: Testy
char: Monk
accent: "#a68a4f"
role: test bot
---
A villager that exists only inside a test.

## Voice

Flat, factual, short.
"""


def valid_manifest() -> dict[str, Any]:
    """Build a minimal manifest that declares all five capability dimensions."""
    return {
        "version": 0,
        "uid": VALID_RESIDENT_UID,
        "id": "test-agent",
        "agent_id": "claude-code:test-agent",
        "summary": "A resident that exists only inside a test.",
        "soul": {
            "name": "Testy",
            "char": "Monk",
            "accent": "#a68a4f",
            "role": "test bot",
        },
        "charter": {
            "mission": "Exercise the schema without touching the real world.",
            "duties": ["Answer test questions."],
            "rules": ["Never send email without explicit approval."],
            "escalation": "Raise needs_human before anything irreversible.",
        },
        "skills": ["daily-summary", {"id": "write-journal", "note": "end of run"}],
        "memory": {
            "kind": "directory",
            "path": "/data/residents/test-agent/memory",
            "journal": "journal",
        },
        "routes": [
            {"id": "schedule", "kind": "cron", "address": "steward:scheduler"},
        ],
        "app_grants": [
            {"id": "burrow", "name": "Burrow", "status": "granted", "scopes": ["events.write"]},
        ],
        "runner": {"kind": "claude", "model": "claude-opus-5"},
        "routines": [
            {
                "id": "daily-summary",
                "schedule": "0 7 * * *",
                "prompt": "Write the summary.",
                "requires": ["daily-summary"],
                "timeout_s": 900,
            },
        ],
    }


@pytest.fixture(autouse=True)
def isolated_events(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point the undelivered-event fallback at this test's own directory.

    Autouse and unconditional. Without it, anything that builds an emitter from the
    environment writes into ``~/.burrow/events.jsonl`` — a developer's real village — and
    the watchdog, which *reads* that file to find runs that never reported back, would
    happily bury somebody's actual work. A test suite must not be able to emit into a
    village it did not create. A test that wants a different location simply sets the
    variable again, and one that is checking the default deletes it.
    """
    path = tmp_path / "fallback-events.jsonl"
    monkeypatch.setenv("STEWARD_EVENTS_FALLBACK", str(path))
    return path


type ResidentWriter = Callable[..., Path]


@pytest.fixture
def write_resident(tmp_path: Path) -> ResidentWriter:
    """Write ``residents/<id>/`` into a temp dir and return the manifest path."""

    def _write(
        manifest: Mapping[str, Any] | None = None,
        *,
        soul: str | None = VALID_SOUL,
        directory: str | None = None,
        root: Path | None = None,
    ) -> Path:
        data = copy.deepcopy(dict(manifest if manifest is not None else valid_manifest()))
        base = root if root is not None else tmp_path / "residents"
        resident = base / (directory or str(data.get("id", "unnamed")))
        resident.mkdir(parents=True, exist_ok=True)
        manifest_path = resident / "manifest.yaml"
        manifest_path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
        if soul is not None:
            (resident / "soul.md").write_text(soul, encoding="utf-8")
        return manifest_path

    return _write


SKILL_BODY = "Read what is there. Write down what you did. Say what you could not do.\n"

type SkillWriter = Callable[..., Path]


@pytest.fixture
def write_skill(tmp_path: Path) -> SkillWriter:
    """Write ``skills/<name>/SKILL.md`` beside the temp residents tree.

    The library lands where :func:`steward.skills.default_skills_dir` looks for it, so
    a test that writes a skill gets the same resolution the real repo has.
    """

    def _write(  # noqa: PLR0913 — one keyword per thing a test wants to vary
        name: str,
        *,
        description: str = "One line saying what this skill is for.",
        body: str = SKILL_BODY,
        defaults: bool = False,
        text: str | None = None,
        root: Path | None = None,
    ) -> Path:
        base = root if root is not None else tmp_path / "skills"
        directory = base / name
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / "SKILL.md"
        if text is None:
            frontmatter = [f"name: {name}", f"description: {description}"]
            if defaults:
                frontmatter.append("defaults: true")
            text = "---\n" + "\n".join(frontmatter) + "\n---\n\n" + body
        path.write_text(text, encoding="utf-8")
        return path

    return _write


type StubWriter = Callable[..., Path]


@pytest.fixture
def stub_bin(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> StubWriter:
    """Write a fake executable into a temp dir that is first on PATH.

    Runner tests assert against a real process, not a mocked one: that is the only
    way to know steward passes the model, the prompt, and the cwd the way it claims.
    """
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ.get('PATH', '')}")

    def _write(name: str, body: str) -> Path:
        script = bindir / name
        script.write_text(f"#!/bin/sh\n{body}\n", encoding="utf-8")
        script.chmod(script.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
        return script

    return _write


@pytest.fixture
def empty_path(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Blank out PATH so a missing binary is missing for sure."""
    monkeypatch.setenv("PATH", str(tmp_path / "nothing-here"))


@dataclass(frozen=True, slots=True)
class ScratchRepo:
    """A throwaway git checkout shaped like this one: residents/ and skills/ inside it."""

    root: Path

    @property
    def residents(self) -> Path:
        """The residents tree inside the scratch checkout."""
        return self.root / "residents"

    @property
    def skills(self) -> Path:
        """The skills library beside it."""
        return self.root / "skills"

    def git(self, *args: str) -> subprocess.CompletedProcess[str]:
        """Run one git command in the scratch checkout, and insist that it worked."""
        return subprocess.run(  # noqa: S603 — argv list, shell=False, a temp directory
            ["git", "-C", str(self.root), *args],  # noqa: S607
            capture_output=True,
            text=True,
            check=True,
        )

    def log(self) -> list[str]:
        """Return the commit subjects, newest first."""
        out = self.git("log", "--format=%s").stdout.strip()
        return out.splitlines() if out else []

    def head(self) -> str:
        """Return the current commit."""
        return self.git("rev-parse", "HEAD").stdout.strip()


@pytest.fixture
def scratch_repo(tmp_path: Path) -> ScratchRepo:
    """Build a real git repo in a temp directory, with one commit already in it.

    Real git, because the nursery's commits are made by running ``git`` and a fake would
    only prove that the fake agrees with itself. Temp directory, emphatically: a test that
    could commit into *this* checkout would be a test suite writing history nobody asked
    for, and the fixture exists so no test is ever tempted to point at the repo root.
    """
    repo = ScratchRepo(root=tmp_path / "checkout")
    repo.residents.mkdir(parents=True)
    repo.skills.mkdir(parents=True)
    (repo.root / "README.md").write_text("scratch\n", encoding="utf-8")
    repo.git("init", "-b", "main")
    repo.git("config", "user.email", "test@example.invalid")
    repo.git("config", "user.name", "Test")
    repo.git("config", "commit.gpgsign", "false")
    repo.git("add", "-A")
    repo.git("commit", "-m", "chore: scratch repo")
    return repo
