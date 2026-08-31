#!/bin/sh
set -eu

origin=${1:-http://127.0.0.1:8737}

curl -fsS "$origin/" | grep -q '<div id="root"></div>'
curl -fsS "$origin/deep/link" | grep -q '<div id="root"></div>'
curl -fsS "$origin/burrow/state" >/dev/null

headers=$(mktemp)
body=$(mktemp)
trap 'rm -f "$headers" "$body"' EXIT
curl -fsS --max-time 18 -D "$headers" -o "$body" "$origin/burrow/state/stream?generation=0" || status=$?
test "${status:-0}" -eq 28
grep -qi '^X-Accel-Buffering: no' "$headers"
grep -Eq '^(event: (snapshot|reset)|: keepalive)' "$body"

steward_status=$(curl -sS -o /dev/null -w '%{http_code}' \
  -X POST -H 'Content-Type: application/json' \
  --data '{"decision":"deny"}' "$origin/approvals/arcadia-smoke-missing")
test "$steward_status" -eq 401

printf '%s\n' "steward-preflight=401"
printf '%s\n' "Arcadia HTTP, deep-link, state, and SSE smoke checks passed."
