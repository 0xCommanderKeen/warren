#!/bin/sh
# steward-smoke — prove, from inside a resident's container, that it can reach the village.
#
# This is issue #51's acceptance criterion made executable: "a test event from inside the
# container reaches POST /events with a 204". Run it right after provisioning:
#
#   docker exec steward-<id> steward-smoke
#
# Four checks, in the order they can fail:
#   1. the claude CLI is installed and answers --version
#   2. python3 and the vendored chronicle emitter are where settings.json says they are
#   3. a direct POST to the village's /events comes back 204
#   4. the emitter itself, fed a real hook payload on stdin, delivers the same way
#
# Exit 0 only when the village answered 204. When it did not, check 4's fallback line is
# printed instead — a resident off the tailnet still logs to ~/.burrow/events.jsonl, and
# seeing that line is the difference between "the emitter is broken" and "the NAS is away".
#
# On honesty: both events this posts are `heartbeat`, under a `steward-smoke:<host>` agent
# id rather than the resident's own. A heartbeat is liveness-only in chronicle's projection —
# it never claims a task, an artifact, or a knock — and a probe identity means running the
# smoke test cannot conjure a villager for a resident that has not done any work yet. The
# village never lies, including when steward is the one testing it.
#
# And on the same honesty: check 2 says this container *could* emit, not that a
# steward-launched session *will*. Since steward #206 every claude session is launched with
# `--setting-sources ""`, and settings.json here is the `user` source — so its hooks are
# not loaded by a session steward starts. They are loaded by a person running `claude`
# in here by hand. Closing that channel is what #206 is for; re-establishing the telemetry
# through something steward declares (`--settings <file>`, measured to survive the flag —
# see docs/settings-sources.md) is separate work and has not been done.
set -u

CONFIG_DIR=/root/.claude
EMITTER="$CONFIG_DIR/burrow-emit.py"
SETTINGS="$CONFIG_DIR/settings.json"
AGENT_ID="${SMOKE_AGENT_ID:-steward-smoke:$(hostname)}"
PROJECT="${CHRONICLE_PROJECT:-${BURROW_PROJECT:-steward}}"
FALLBACK="$HOME/.burrow/events.jsonl"
failed=0

say() { echo "smoke: $*"; }
fail() { echo "smoke: FAIL $*"; failed=1; }

# ---------------------------------------------------------------- 1. the brain is here
if version=$(claude --version 2>&1); then
    say "ok   claude $version"
else
    fail "the claude CLI is not runnable in this container ($version)"
fi

# ------------------------------------------------------- 2. the emitter is where it says
if command -v python3 >/dev/null 2>&1; then
    say "ok   $(python3 --version 2>&1)"
else
    fail "python3 is missing, so no hook can emit anything"
fi

if [ -f "$EMITTER" ]; then
    say "ok   emitter at $EMITTER"
else
    fail "no emitter at $EMITTER — the entrypoint did not seed the claude volume"
fi

if [ -f "$SETTINGS" ]; then
    if python3 -c 'import json,sys; json.load(open(sys.argv[1]))' "$SETTINGS" 2>/dev/null; then
        say "ok   $SETTINGS parses"
    else
        fail "$SETTINGS is not valid JSON, so claude will start with no hooks at all"
    fi
else
    fail "no $SETTINGS — a hand-run claude in here would have no hooks and emit nothing"
fi

# ------------------------------------------------------------------- 3. the village answers
VILLAGE_URL="${CHRONICLE_URL:-${BURROW_URL:-}}"
VILLAGE_TOKEN="${CHRONICLE_TOKEN:-${BURROW_TOKEN:-}}"
if [ -z "$VILLAGE_URL" ]; then
    fail "no CHRONICLE_URL/BURROW_URL; there is no village to post to"
    posted=""
else
    url=$(echo "$VILLAGE_URL" | sed 's:/*$::')
    ts=$(date -u +%Y-%m-%dT%H:%M:%S.000Z)
    body=$(printf '{"v":0,"ts":"%s","source":"steward","agent_id":"%s","project":"%s","cwd":"%s","type":"heartbeat","payload":{"tool":"steward-smoke"}}' \
        "$ts" "$AGENT_ID" "$PROJECT" "$(pwd)")
    if [ -n "$VILLAGE_TOKEN" ]; then
        posted=$(printf '%s' "$body" | curl -sS -o /dev/null -w '%{http_code}' \
            --max-time 10 \
            -X POST "$url/events" \
            -H 'Content-Type: application/json' \
            -H "Authorization: Bearer $VILLAGE_TOKEN" \
            --data-binary @- 2>/dev/null)
    else
        posted=$(printf '%s' "$body" | curl -sS -o /dev/null -w '%{http_code}' \
            --max-time 10 \
            -X POST "$url/events" \
            -H 'Content-Type: application/json' \
            --data-binary @- 2>/dev/null)
    fi
    case "$posted" in
        204) say "ok   POST $url/events -> 204" ;;
        401) fail "POST $url/events -> 401: the village token is wrong or missing" ;;
        ""|000) fail "POST $url/events -> no answer at all: $url is unreachable from this container" ;;
        *)   fail "POST $url/events -> $posted (expected 204)" ;;
    esac
fi

# ------------------------------------------------- 4. the wired path, end to end, as a hook
if [ -f "$EMITTER" ]; then
    before=0
    [ -f "$FALLBACK" ] && before=$(wc -l < "$FALLBACK" | tr -d ' ')
    printf '{"hook_event_name":"PostToolUse","tool_name":"SmokeTest","session_id":"steward-smoke","cwd":"%s"}' "$(pwd)" \
        | BURROW_AGENT_ID="$AGENT_ID" BURROW_PROJECT="$PROJECT" \
          CHRONICLE_AGENT_ID="$AGENT_ID" CHRONICLE_PROJECT="$PROJECT" python3 "$EMITTER"
    status=$?
    after=0
    [ -f "$FALLBACK" ] && after=$(wc -l < "$FALLBACK" | tr -d ' ')
    if [ "$status" -ne 0 ]; then
        fail "the emitter exited $status; a hook must never fail the session it runs in"
    elif [ "$after" -gt "$before" ]; then
        say "note the emitter fell back to $FALLBACK — nothing is lost, but the village did not take it:"
        echo "      $(tail -n 1 "$FALLBACK")"
        [ "${posted:-}" = "204" ] && fail "the direct POST worked but the emitter did not; check the village URL inside the container"
    else
        say "ok   the emitter delivered without falling back"
    fi
fi

if [ "$failed" -eq 0 ]; then
    say "PASS this container can reach the village"
else
    say "FAILED — see the lines above"
fi
exit "$failed"
