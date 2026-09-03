"""The vendoring command must produce the artifact it claims to, from bytes that exist.

``make vendor-emitter`` is the one command that writes ``docker/resident/chronicle-emit.py``,
the emitter every deployed resident runs. It builds chronicle's bundle (warren#234) rather
than copying a source file, so what it has to get right is: refuse inputs that are in no
commit, need nothing on ``PATH`` that a fresh checkout lacks, and never leave a
half-written artifact in a committed path.

These run the real target against isolated throwaway repositories, never against
``../chronicle``.
"""

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from conftest import REPO_ROOT

#: The three files chronicle's bundle is a pure function of. The target guards every one:
#: build.py decides the artifact's shape as much as the two sources it flattens.
SOURCES = ("emit.py", "durable.py", "build.py")

#: A stand-in emit.py: the shebang the bundle keeps first, and the exact import block the
#: build replaces. Copying chronicle's real emitter here would make these tests fail
#: whenever it changed, which is the sibling suite's job, not this one's.
FAKE_EMIT = """\
#!/usr/bin/env python3
try:
    from hooks import durable
except ImportError:  # standalone deployment invokes this file from hooks/
    import durable

print(durable.VALUE)
"""

FAKE_DURABLE = "VALUE = 'committed bytes'\n"


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


def chronicle_repo(tmp_path: Path, *, omit: str = "") -> Path:
    """Make a throwaway chronicle: the two fake sources, and the build that flattens them."""
    repo = tmp_path / "chronicle"
    (repo / "hooks").mkdir(parents=True)
    (repo / "hooks" / "emit.py").write_text(FAKE_EMIT, encoding="utf-8")
    (repo / "hooks" / "durable.py").write_text(FAKE_DURABLE, encoding="utf-8")
    shutil.copy(REPO_ROOT.parent / "chronicle" / "hooks" / "build.py", repo / "hooks" / "build.py")
    run("git", "init", "-q", cwd=repo)
    run("git", "config", "user.email", "test@example.com", cwd=repo)
    run("git", "config", "user.name", "Test", cwd=repo)
    for name in SOURCES:
        if name != omit:
            run("git", "add", f"hooks/{name}", cwd=repo)
    run("git", "commit", "-qm", "add the emitter", cwd=repo)
    return repo


def steward_copy(tmp_path: Path) -> Path:
    repo = tmp_path / "steward"
    (repo / "docker" / "resident").mkdir(parents=True)
    shutil.copy(REPO_ROOT / "Makefile", repo / "Makefile")
    return repo


def vendor(
    steward: Path,
    chronicle: Path,
    *,
    env: dict[str, str] | None = None,
    python: str = sys.executable,
    variable: str = "CHRONICLE",
):
    return subprocess.run(  # noqa: S603 - fixed make target against an isolated copy
        (
            MAKE,
            "-s",
            "vendor-emitter",
            f"{variable}={chronicle}",
            f"PYTHON={python}",
        ),
        cwd=steward,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


def vendored(steward: Path) -> Path:
    return steward / "docker" / "resident" / "chronicle-emit.py"


@pytest.mark.parametrize("source", SOURCES)
@pytest.mark.parametrize("state", ["unstaged", "staged"])
def test_vendor_emitter_refuses_uncommitted_upstream_bytes(
    tmp_path: Path, state: str, source: str
) -> None:
    """A copy built from bytes that are in no commit is a copy nobody can trace back."""
    chronicle = chronicle_repo(tmp_path)
    steward = steward_copy(tmp_path)
    (chronicle / "hooks" / source).write_text("# uncommitted\n", encoding="utf-8")
    if state == "staged":
        run("git", "add", f"hooks/{source}", cwd=chronicle)

    result = vendor(steward, chronicle)

    assert result.returncode != 0
    assert "uncommitted changes" in result.stdout
    assert not vendored(steward).exists()


@pytest.mark.parametrize("source", SOURCES)
def test_vendor_emitter_refuses_a_file_absent_from_head(tmp_path: Path, source: str) -> None:
    chronicle = chronicle_repo(tmp_path, omit=source)
    steward = steward_copy(tmp_path)

    result = vendor(steward, chronicle)

    assert result.returncode != 0
    assert "not committed at HEAD" in result.stdout
    assert not vendored(steward).exists()


def test_vendor_emitter_writes_the_bundle_the_build_produces(tmp_path: Path) -> None:
    """And needs nothing on PATH but git and make — no checksum utility, no python3."""
    chronicle = chronicle_repo(tmp_path)
    steward = steward_copy(tmp_path)
    tools = tmp_path / "tools"
    tools.mkdir()
    for command in ("git", "make"):
        (tools / command).symlink_to(executable(command))
    env = os.environ | {"PATH": str(tools)}

    result = vendor(steward, chronicle, env=env)

    assert result.returncode == 0, result.stderr
    expected = subprocess.run(  # noqa: S603 - the same build, invoked directly
        (sys.executable, str(chronicle / "hooks" / "build.py")),
        capture_output=True,
        text=True,
        check=True,
    )
    assert vendored(steward).read_text(encoding="utf-8") == expected.stdout
    assert "committed bytes" in vendored(steward).read_text(encoding="utf-8")
    commit = run("git", "rev-parse", "HEAD", cwd=chronicle).stdout.strip()
    assert commit in result.stdout, "the human running this is told which commit it built"


def test_vendor_emitter_still_answers_to_the_pre_rename_variable(tmp_path: Path) -> None:
    """`BURROW=` is muscle memory and lives in scripts written before warren#216."""
    chronicle = chronicle_repo(tmp_path)
    steward = steward_copy(tmp_path)

    result = vendor(steward, chronicle, variable="BURROW")

    assert result.returncode == 0, result.stderr
    assert vendored(steward).is_file()


def test_vendor_emitter_leaves_no_artifact_behind_when_the_build_fails(tmp_path: Path) -> None:
    """It writes into a committed path, so a torn write would be committed as the emitter."""
    chronicle = chronicle_repo(tmp_path)
    steward = steward_copy(tmp_path)

    result = vendor(steward, chronicle, python="false")

    assert result.returncode != 0
    assert not vendored(steward).exists()
    assert list((steward / "docker" / "resident").iterdir()) == []


def test_vendor_emitter_records_no_checksum_beside_the_copy(tmp_path: Path) -> None:
    """The pinned checksum is gone: it could only catch tampering, never staleness.

    Its replacement is tests/test_resident_image.py, which rebuilds the bundle from
    ../chronicle at HEAD and compares byte for byte — a check with nothing recorded in it
    to go stale (warren#234).
    """
    chronicle = chronicle_repo(tmp_path)
    steward = steward_copy(tmp_path)

    assert vendor(steward, chronicle).returncode == 0
    assert [path.name for path in (steward / "docker" / "resident").iterdir()] == [
        "chronicle-emit.py"
    ]
