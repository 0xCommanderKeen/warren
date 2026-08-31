#!/bin/sh
# Seed the claude config volume, then get out of the way.
#
# /root/.claude is a bind mount from the host (./claude beside the compose file), and a
# bind mount hides whatever the image baked at that path. So the image carries the
# canonical copies under /opt/steward and this script puts them into the volume at start.
# That is the same shape life-agent's container has today — burrow-emit.py living in the
# claude-config volume — except that here the copy comes from the image instead of from
# somebody's afternoon with scp.
#
# What it will and will not overwrite:
#   burrow-emit.py   always replaced. It is vendored from burrow and pinned by checksum in
#                    this repo, so the image is its source of truth and a stale copy in the
#                    volume is a bug, not a local edit worth keeping.
#   settings.json    written only when absent. A resident may legitimately have grown a
#                    permissions block or an extra hook; clobbering that every restart
#                    would make the container hostile to the person maintaining it. When a
#                    settings.json is present but does not wire the emitter, this says so
#                    loudly and carries on — refusing to start would take a resident down
#                    over telemetry, which is the wrong trade.
#
# One thing this file wires that a steward-launched session no longer reads: since steward
# #206 every claude session is launched with `--setting-sources ""`, and this is the `user`
# source. The hooks below fire for a person running `claude` in this container by hand;
# they do not fire for a routine steward starts. See docs/settings-sources.md — the
# channel was closed on purpose, and giving steward its own declared settings file
# (`--settings`, measured to survive the flag) is the separate work that would bring the
# telemetry back.
set -eu

CONFIG_DIR=/root/.claude
BAKED_DIR=/opt/steward

mkdir -p "$CONFIG_DIR"
cp "$BAKED_DIR/burrow-emit.py" "$CONFIG_DIR/burrow-emit.py"

if [ ! -f "$CONFIG_DIR/settings.json" ]; then
    cp "$BAKED_DIR/settings.json" "$CONFIG_DIR/settings.json"
    echo "steward: wrote $CONFIG_DIR/settings.json (burrow hooks wired)"
else
    python3 - "$CONFIG_DIR/settings.json" <<'PY' || true
import json
import sys

WANTED = ("UserPromptSubmit", "PreToolUse", "PostToolUse", "Notification", "Stop", "SessionEnd")
try:
    with open(sys.argv[1], encoding="utf-8") as handle:
        hooks = (json.load(handle) or {}).get("hooks") or {}
except (OSError, ValueError) as exc:
    print("steward: WARNING %s is unreadable (%s); this resident will emit nothing" % (sys.argv[1], exc))
    sys.exit(0)

missing = [
    name for name in WANTED
    if "burrow-emit.py" not in json.dumps(hooks.get(name) or [])
]
if missing:
    print("steward: WARNING %s does not wire burrow-emit.py into %s;" % (sys.argv[1], ", ".join(missing)))
    print("steward:          the village will not see those events. /opt/steward/settings.json is the template.")
PY
fi

# The village address arrives from the compose .env; say whether it is there, and never
# say what the token is.
if [ -n "${BURROW_URL:-}" ]; then
    echo "steward: emitting to $BURROW_URL as ${BURROW_AGENT_ID:-<no BURROW_AGENT_ID set>}"
else
    echo "steward: WARNING BURROW_URL is unset; events fall back to ~/.burrow/events.jsonl in this container"
fi

exec "$@"
