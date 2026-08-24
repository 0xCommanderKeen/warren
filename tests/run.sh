#!/bin/sh
# Run every burrow test. No framework, no deps: python3 for the emitter/server,
# node for the viewer's projection logic.
#
#     sh tests/run.sh
set -e
root=$(cd "$(dirname "$0")/.." && pwd)
cd "$root"
export PYTHONPATH="$root:${PYTHONPATH:-}"
PYTHON=${PYTHON:-python3}
for t in tests/test_*.py; do
  echo "== $t"
  "$PYTHON" "$t"
done
for t in tests/test_*.js; do
  echo "== $t"
  node "$t"
done
echo "all green"
