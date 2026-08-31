#!/usr/bin/env python3
"""NUL-safe dispatcher behind the repository's public tests/run.sh command."""

import os
import pathlib
import subprocess
import sys


ROOT = pathlib.Path(__file__).resolve().parents[1]


def discover_tests():
    output = subprocess.run(
        ["git", "ls-files", "-z"], cwd=ROOT, check=True, capture_output=True
    ).stdout
    tests = []
    for raw_path in output.split(b"\0"):
        if not raw_path:
            continue
        path = os.fsdecode(raw_path)
        # A worktree implementation may intentionally remove obsolete tests
        # before its commit is staged; discovery follows the current tree.
        if not (ROOT / path).is_file():
            continue
        name = pathlib.PurePosixPath(path).name
        if name.startswith("test_") and name.endswith(".py"):
            tests.append((raw_path, path))
    return sorted(tests, key=lambda item: item[0])


def main(argv):
    if argv not in ([], ["--list"]):
        print("usage: sh tests/run.sh [--list]", file=sys.stderr)
        return 2

    tests = discover_tests()
    for _, path in tests:
        print(f"== {path}", flush=True)
        if argv == ["--list"]:
            continue
        command = [os.environ.get("PYTHON", "python3"), path]
        subprocess.run(command, cwd=ROOT, check=True)

    if not argv:
        print("all green")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
