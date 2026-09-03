#!/bin/sh
set -eu

origin=${1:-http://127.0.0.1:8737}

curl -fsS "$origin/" | grep -q '<div id="root"></div>'
curl -fsS "$origin/deep/link" | grep -q '<div id="root"></div>'

# The module script each index.html loads must come back as JavaScript. A 200 is not
# enough: served as text/plain the browser refuses the module and the page is blank,
# which no check on index.html can see (2026-09-02).
for page in "" "observatory/"; do
  module=$(curl -fsS "$origin/$page" | sed -n 's/.*<script[^>]*type="module"[^>]*src="\([^"]*\)".*/\1/p' | head -n 1)
  test -n "$module"
  case "$module" in /*) module_url="$origin$module" ;; *) module_url="$origin/$page$module" ;; esac
  curl -fsSI "$module_url" | grep -qi '^content-type: .*javascript' || {
    printf '%s\n' "module $module_url is not served as JavaScript"
    exit 1
  }
done
printf '%s\n' "module-type=javascript"
curl -fsS "$origin/chronicle/state" >/dev/null
# The pre-warren#361 prefix still answers, as a 301 to the new one. Checked here rather
# than trusted, because an unclaimed path on this origin is not a 404 — it is the SPA's
# index.html under a 200, so a redirect block that silently stopped matching would look
# exactly like one that works until somebody parsed the JSON (warren#242).
redirect=$(curl -fsS -o /dev/null -w '%{redirect_url}' "$origin/burrow/state")
case "$redirect" in
  */chronicle/state) : ;;
  *) printf '%s\n' "/burrow/state redirects to '$redirect', not /chronicle/state"; exit 1 ;;
esac
# Chronicle's manifest-validation report, on the prefixed path Steward's `/residents` left
# free (warren#242). The `grep` is the real check, not `-f`: an unproxied path is not a 404
# here, it is the SPA's index.html under a 200, which `-f` is happy with.
curl -fsS "$origin/chronicle/residents" | grep -q '"residents"'

headers=$(mktemp)
body=$(mktemp)
trap 'rm -f "$headers" "$body"' EXIT
curl -fsS --max-time 18 -D "$headers" -o "$body" "$origin/chronicle/state/stream?generation=0" || status=$?
test "${status:-0}" -eq 28
grep -qi '^X-Accel-Buffering: no' "$headers"
grep -Eq '^(event: (snapshot|reset)|: keepalive)' "$body"

steward_status=$(curl -sS -o /dev/null -w '%{http_code}' \
  -X POST -H 'Content-Type: application/json' \
  --data '{"decision":"deny"}' "$origin/approvals/arcadia-smoke-missing")
test "$steward_status" -eq 401

# Steward's read routes reach Steward rather than the SPA. Lineage is the check because it
# was one of the two paths the origin did not proxy: unproxied, this is a 200 carrying
# index.html; proxied, an unauthenticated caller is refused before the task is looked up.
lineage_status=$(curl -sS -o /dev/null -w '%{http_code}' "$origin/tasks/arcadia-smoke-missing/lineage")
test "$lineage_status" -eq 401

printf '%s\n' "steward-preflight=401"
printf '%s\n' "lineage-preflight=401"
printf '%s\n' "Arcadia HTTP, deep-link, state, and SSE smoke checks passed."
