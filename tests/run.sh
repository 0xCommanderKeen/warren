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
# Both naming conventions run: test_*.js and *.test.js. The library trip shipped
# broken because tests/projection.test.js and tests/routes.test.js matched
# neither glob and so never ran.
for t in tests/test_*.js tests/*.test.js; do
  echo "== $t"
  node "$t"
done
echo "all green"
