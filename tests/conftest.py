"""Shared fixtures: throwaway resident directories, and stub CLIs on PATH."""

import copy
import os
import stat
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
RESIDENTS_DIR = REPO_ROOT / "residents"

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
