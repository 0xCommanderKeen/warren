#!/bin/sh
set -e
root=$(cd "$(dirname "$0")/.." && pwd)
cd "$root"
export PYTHONPATH="$root:${PYTHONPATH:-}"
PYTHON=${PYTHON:-python3}
"$PYTHON" tests/test_ui_contract.py
node tests/state-contract.test.js
