"""Shared fixtures: throwaway resident directories, and stub CLIs on PATH."""

import copy
import json
import os
import stat
import subprocess
import sys
import time
from collections.abc import Callable, Iterator, Mapping
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
    """Build a minimal manifest that declares every capability dimension."""
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
        "tools": "unrestricted",
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


@pytest.fixture(autouse=True)
def isolated_notifications(monkeypatch: pytest.MonkeyPatch) -> str:
    """Point every derived ntfy topic at a server that is not there.

    Autouse and unconditional, for the reason :func:`isolated_events` is. The ntfy default
    is the *public* ``https://ntfy.sh``, and a manifest fixture that declares
    ``notifications: {transport: ntfy}`` would otherwise push a real message into a real
    public topic every time the suite ran — reachable by anyone who computed the same topic
    from a uid this repo commits in plain text. Loopback port 1 is refused instantly and
    reaches no network at all.

    A test that wants a *working* transport injects a fake one; a test that wants to check
    the default target passes an explicit env mapping to ``from_env`` rather than the
    process environment. The namespace is set for the same reason: a suite must not derive
    the topics a real installation derives.
    """
    monkeypatch.setenv("STEWARD_NTFY_URL", "http://127.0.0.1:1")
    monkeypatch.setenv("STEWARD_NOTIFY_NAMESPACE", "pytest")
    monkeypatch.delenv("STEWARD_NTFY_TOKEN", raising=False)
    monkeypatch.delenv("STEWARD_NTFY_TIMEOUT_S", raising=False)
    return "http://127.0.0.1:1"


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


# --------------------------------------------------------------------------------------
# a second real process holding a resident's session claim (warren#111)
# --------------------------------------------------------------------------------------

#: What the other process runs. Deliberately a *real* process rather than a second
#: ``ResidentClaims`` in this one: the whole point of warren#111 is that in-process locks
#: are invisible across processes, and a test that proves the guard with two objects in one
#: interpreter would prove nothing about the bug it exists for.
CLAIM_HOLDER_SCRIPT = '''
"""Take one resident's claim in a second real process and sit on it until killed."""

import json
import os
import sys
import time

from steward.claims import ClaimRefused, ResidentClaims
from steward.store import Store

db, ready, resident, kind, ref, run_id, grace, beat = sys.argv[1:9]
claims = ResidentClaims(Store(db), grace_s=float(grace), heartbeat_every_s=float(beat))
with claims.hold(resident, kind=kind, ref=ref, run_id=run_id) as held:
    payload = {"held": not isinstance(held, ClaimRefused), "pid": os.getpid()}
    with open(ready + ".tmp", "w") as handle:
        handle.write(json.dumps(payload))
    os.replace(ready + ".tmp", ready)
    time.sleep(600)
'''


@dataclass
class ClaimHolder:
    """A second real process sitting on one resident's session claim."""

    process: subprocess.Popen[str]

    @property
    def pid(self) -> int:
        """The operating-system process holding the claim."""
        return self.process.pid

    def kill(self) -> None:
        """Stop the holder the way a crash does: no release, no finally block."""
        if self.process.poll() is None:
            self.process.kill()
            self.process.wait(timeout=30)


type ClaimHolderSpawner = Callable[..., ClaimHolder]


@pytest.fixture
def claim_holder(tmp_path: Path) -> Iterator[ClaimHolderSpawner]:
    """Return a factory that spawns another process holding a resident's claim.

    The factory blocks until that process has actually taken the claim, so a test can say
    "somebody else is running this resident" as a fact rather than as a hope, and every
    holder is killed when the test ends however it ended.
    """
    started: list[ClaimHolder] = []
    script = tmp_path / "claim-holder.py"
    script.write_text(CLAIM_HOLDER_SCRIPT, encoding="utf-8")

    def _spawn(  # noqa: PLR0913 — one keyword per fact the held claim records
        db: Path | str,
        resident_id: str = "test-agent",
        *,
        kind: str = "routine",
        ref: str = "held-routine",
        run_id: str = "held-run",
        grace_s: float = 120.0,
        beat_s: float = 1.0,
    ) -> ClaimHolder:
        ready = tmp_path / f"claim-holder-{len(started)}.json"
        process = subprocess.Popen(  # noqa: S603 — a fixed interpreter and a generated script
            [
                sys.executable,
                str(script),
                str(db),
                str(ready),
                resident_id,
                kind,
                ref,
                run_id,
                str(grace_s),
                str(beat_s),
            ],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        holder = ClaimHolder(process=process)
        started.append(holder)
        deadline = time.monotonic() + 60.0
        while time.monotonic() < deadline:
            if ready.exists():
                assert json.loads(ready.read_text(encoding="utf-8"))["held"]
                return holder
            if process.poll() is not None:
                raise AssertionError(f"the claim holder died: {process.communicate()}")
            time.sleep(0.05)
        raise AssertionError("the claim holder never took the claim")

    try:
        yield _spawn
    finally:
        for holder in started:
            holder.kill()


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
