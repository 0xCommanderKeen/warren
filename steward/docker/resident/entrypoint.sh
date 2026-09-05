#!/bin/sh
# Seed the claude config volume, then get out of the way.
#
# /root/.claude is a bind mount from the host (./claude beside the compose file), and a
# bind mount hides whatever the image baked at that path. So the image carries the
# canonical copies under /opt/steward and this script puts them into the volume at start.
# That is the same shape hob's container has today — the emitter living in the
# claude-config volume — except that here the copy comes from the image instead of from
# somebody's afternoon with scp.
#
# What it will and will not overwrite:
#   chronicle-emit.py  always replaced. It is a generated artifact — chronicle's emitter
#                    bundle, vendored into this repo and compared against a live rebuild by
#                    steward's suite — so the image is its source of truth and a stale copy
#                    in the volume is a bug, not a local edit worth keeping.
#   settings.json    written only when absent. A resident may legitimately have grown a
#                    permissions block or an extra hook; clobbering that every restart
#                    would make the container hostile to the person maintaining it. When a
#                    settings.json is present but does not wire the emitter, this says so
#                    loudly and carries on — refusing to start would take a resident down
#                    over telemetry, which is the wrong trade.
#
# The one exception to "written only when absent" is the emitter's *path*, which warren#361
# renamed from burrow-emit.py. The volume outlives the image, so a resident provisioned
# before the rename comes back up with a settings.json naming a file the new image no
# longer ships. That path is a generated reference this script already owns, not a local
# edit, so it is repointed in place — one string, everything else in the file untouched —
# and the pre-rename copy is removed once nothing names it any more. Left alone, the
# resident would look perfectly healthy and emit nothing.
#
# Who reads what this file writes: since steward #206 every claude session is launched with
# `--setting-sources ""`, and $CONFIG_DIR/settings.json is the `user` source. So the hooks
# seeded below fire for a *person* running `claude` in this container by hand, and not for a
# routine steward starts. Steward declares the same six hooks itself, on argv, naming the
# baked /opt/steward/chronicle-emit.py rather than the copy this script puts in the mount
# (steward #264, docs/manifest.md) — deliberately, because $CONFIG_DIR is a bind mount from
# the host and a hook command pointing into a mount is arbitrary code from outside the
# image. Two readers, two copies, one channel; docs/settings-sources.md has the measurement.
set -eu

CONFIG_DIR=/root/.claude
BAKED_DIR=/opt/steward

# CODEX_HOME is declared only for Codex residents. Never seed or replace credentials.
if [ -n "${CODEX_HOME:-}" ]; then
    mkdir -p "$CODEX_HOME"
    chmod 0700 "$CODEX_HOME"
fi

mkdir -p "$CONFIG_DIR"
cp "$BAKED_DIR/chronicle-emit.py" "$CONFIG_DIR/chronicle-emit.py"

if [ ! -f "$CONFIG_DIR/settings.json" ]; then
    cp "$BAKED_DIR/settings.json" "$CONFIG_DIR/settings.json"
    echo "steward: wrote $CONFIG_DIR/settings.json (chronicle hooks wired)"
else
    python3 - "$CONFIG_DIR/settings.json" <<'PY' || true
import json
import sys

WANTED = ("UserPromptSubmit", "PreToolUse", "PostToolUse", "Notification", "Stop", "SessionEnd")
EMITTER = "chronicle-emit.py"
LEGACY = "burrow-emit.py"
path = sys.argv[1]
try:
    with open(path, encoding="utf-8") as handle:
        text = handle.read()
    document = json.loads(text) or {}
except (OSError, ValueError) as exc:
    print("steward: WARNING %s is unreadable (%s); this resident will emit nothing" % (path, exc))
    sys.exit(0)

# warren#361, in place and announced: the old name in here refers to a file this image no
# longer ships. Parsed first, so a settings.json that is already broken is reported rather
# than rewritten, and replaced as text so an operator's own additions keep their formatting.
if LEGACY in text:
    repointed = text.replace(LEGACY, EMITTER)
    try:
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(repointed)
    except OSError as exc:
        print("steward: WARNING could not repoint %s at %s (%s); this resident will emit nothing" % (path, EMITTER, exc))
        sys.exit(0)
    document = json.loads(repointed)
    print("steward: repointed %s from %s to %s (warren#361)" % (path, LEGACY, EMITTER))

hooks = document.get("hooks") or {}
missing = [
    name for name in WANTED
    if EMITTER not in json.dumps(hooks.get(name) or [])
]
if missing:
    print("steward: WARNING %s does not wire %s into %s;" % (path, EMITTER, ", ".join(missing)))
    print("steward:          the village will not see those events. /opt/steward/settings.json is the template.")
PY
fi

# The pre-rename copy goes only once nothing names it: a repoint that failed above leaves
# the reference in place, and removing the file it points at would be exactly the silent
# failure this block exists to avoid.
if [ -f "$CONFIG_DIR/burrow-emit.py" ] && ! grep -q "burrow-emit\.py" "$CONFIG_DIR/settings.json" 2>/dev/null; then
    rm -f "$CONFIG_DIR/burrow-emit.py"
    echo "steward: removed the pre-rename $CONFIG_DIR/burrow-emit.py (warren#361)"
fi

# The village address arrives from the compose .env; say whether it is there, and never
# say what the token is.
village_url="${CHRONICLE_URL:-}"
village_agent="${CHRONICLE_AGENT_ID:-}"
if [ -n "$village_url" ]; then
    echo "steward: emitting to $village_url as ${village_agent:-<no agent id set>}"
else
    echo "steward: WARNING no CHRONICLE_URL; events fall back to ~/.chronicle/ in this container"
fi

# Where the emitter keeps its durable outbox, said out loud at start (warren#234). The
# emitter vendored here journals undelivered events and replays them when the village comes
# back, which is what an unattended container needs — but it does that under $HOME, and the
# only volume this container mounts for claude is /root/.claude. So the queue survives a
# restart and does NOT survive `docker compose down` or an image upgrade. Mounting it is an
# operator decision, not a default this script should make; docs/manifest.md states the
# options. Printing the path is how a human finds the queue when it matters.
echo "steward: durable outbox under $HOME/.chronicle — container-local unless the"
echo "steward:          compose file mounts it"

exec "$@"
