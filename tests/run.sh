#!/bin/sh
# Run every burrow test. No framework, no deps: python3 for the emitter/server,
# node for the viewer's projection logic.
#
#     sh tests/run.sh
#     sh tests/run.sh --list
set -e
root=$(cd "$(dirname "$0")/.." && pwd)
cd "$root"
export PYTHONPATH="$root:${PYTHONPATH:-}"
PYTHON=${PYTHON:-python3}
export PYTHON
exec "$PYTHON" tests/runner.py "$@"
