"""The vendoring command must make its provenance claim true."""

import hashlib
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from conftest import REPO_ROOT


def executable(name: str) -> str:
    path = shutil.which(name)
    if path is None:
        raise RuntimeError(f"required test executable is missing: {name}")
    return path


MAKE = executable("make")


def run(*args: str, cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603 - fixed test commands and isolated repositories
        args, cwd=cwd, check=True, capture_output=True, text=True
    )


def burrow_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "burrow"
    (repo / "hooks").mkdir(parents=True)
    (repo / "hooks" / "emit.py").write_text("committed bytes\n", encoding="utf-8")
    run("git", "init", "-q", cwd=repo)
    run("git", "config", "user.email", "test@example.com", cwd=repo)
    run("git", "config", "user.name", "Test", cwd=repo)
    run("git", "add", "hooks/emit.py", cwd=repo)
    run("git", "commit", "-qm", "add emitter", cwd=repo)
    return repo


def steward_copy(tmp_path: Path) -> Path:
    repo = tmp_path / "steward"
    (repo / "docker" / "resident").mkdir(parents=True)
    shutil.copy(REPO_ROOT / "Makefile", repo / "Makefile")
    return repo


def vendor(
    steward: Path,
    burrow: Path,
    *,
    env: dict[str, str] | None = None,
    python: str = sys.executable,
):
    return subprocess.run(  # noqa: S603 - fixed make target against an isolated copy
        (
            MAKE,
            "-s",
            "vendor-emitter",
            f"BURROW={burrow}",
            f"PYTHON={python}",
        ),
        cwd=steward,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


@pytest.mark.parametrize("state", ["unstaged", "staged"])
def test_vendor_emitter_refuses_uncommitted_upstream_bytes(tmp_path: Path, state: str) -> None:
    burrow = burrow_repo(tmp_path)
    steward = steward_copy(tmp_path)
    (burrow / "hooks" / "emit.py").write_text("uncommitted bytes\n", encoding="utf-8")
    if state == "staged":
        run("git", "add", "hooks/emit.py", cwd=burrow)

    result = vendor(steward, burrow)

    assert result.returncode != 0
    assert "uncommitted changes" in result.stdout
    assert not (steward / "docker" / "resident" / "burrow-emit.py").exists()


def test_vendor_emitter_refuses_a_file_absent_from_head(tmp_path: Path) -> None:
    burrow = tmp_path / "burrow"
    (burrow / "hooks").mkdir(parents=True)
    (burrow / "README.md").write_text("burrow\n", encoding="utf-8")
    run("git", "init", "-q", cwd=burrow)
    run("git", "config", "user.email", "test@example.com", cwd=burrow)
    run("git", "config", "user.name", "Test", cwd=burrow)
    run("git", "add", "README.md", cwd=burrow)
    run("git", "commit", "-qm", "initial", cwd=burrow)
    (burrow / "hooks" / "emit.py").write_text("untracked bytes\n", encoding="utf-8")
    steward = steward_copy(tmp_path)

    result = vendor(steward, burrow)

    assert result.returncode != 0
    assert not (steward / "docker" / "resident" / "burrow-emit.sha256").exists()


def test_vendor_emitter_records_committed_bytes_without_a_checksum_utility(tmp_path: Path) -> None:
    burrow = burrow_repo(tmp_path)
    steward = steward_copy(tmp_path)
    tools = tmp_path / "tools"
    tools.mkdir()
    for command in ("git", "make", "cat"):
        (tools / command).symlink_to(executable(command))
    env = os.environ | {"PATH": str(tools)}

    result = vendor(steward, burrow, env=env)

    assert result.returncode == 0, result.stderr
    commit = run("git", "rev-parse", "HEAD", cwd=burrow).stdout.strip()
    upstream = (burrow / "hooks" / "emit.py").read_bytes()
    metadata = (steward / "docker" / "resident" / "burrow-emit.sha256").read_text()
    assert f"commit: {commit}\n" in metadata
    assert f"sha256: {hashlib.sha256(upstream).hexdigest()}\n" in metadata


def test_vendor_emitter_stops_when_checksum_calculation_fails(tmp_path: Path) -> None:
    burrow = burrow_repo(tmp_path)
    steward = steward_copy(tmp_path)

    result = vendor(steward, burrow, python="false")

    assert result.returncode != 0
    assert not (steward / "docker" / "resident" / "burrow-emit.py").exists()
    assert not (steward / "docker" / "resident" / "burrow-emit.sha256").exists()
