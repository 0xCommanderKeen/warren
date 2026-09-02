#!/bin/sh
# Run every Chronicle test with the interpreter and dependencies locked by uv.
#
#     sh tests/run.sh
#     sh tests/run.sh --list
set -e
root=$(cd "$(dirname "$0")/.." && pwd)
cd "$root"
export PYTHONPATH="$root:${PYTHONPATH:-}"
exec uv run --frozen python tests/runner.py "$@"
