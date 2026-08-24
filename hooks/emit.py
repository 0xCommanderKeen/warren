#!/usr/bin/env python3
"""burrow v0 emitter: adapts a Claude Code hook callback (JSON on stdin) to one
burrow protocol event. See docs/protocol.md.

Transport: if BURROW_URL is set, POST the event to <BURROW_URL>/events; on any
failure fall back to appending to ~/.burrow/events.jsonl locally. A failed POST
trips a 60s circuit breaker so an unreachable server never slows hooks down.

Resident agents (services that outlive any one Claude session, like a Telegram
bot running claude -p per message) set BURROW_AGENT_ID (stable villager
identity, e.g. "life-agent") and optionally BURROW_PROJECT (label). For a
resident, SessionEnd maps to `idle` rather than `session_ended`: the session's
process is gone but the agent-as-service is still home, resting.

Must never break the hosting agent: swallow everything, always exit 0."""
import datetime
import fcntl
import json
import os
import sys
import time
import urllib.request

LOG_DIR = os.path.expanduser("~/.burrow")
LOG = os.path.join(LOG_DIR, "events.jsonl")
BREAKER = os.path.join(LOG_DIR, ".post-failed")
BREAKER_SECONDS = 60


ARTIFACT_TOOLS = ("Write", "Edit", "MultiEdit", "NotebookEdit")


def tool_detail(tool_input):
    for key in ("file_path", "notebook_path", "path", "pattern", "description",
                "command", "url", "query", "skill"):
        val = tool_input.get(key)
        if val:
            return str(val)[:120]
    return ""


def to_event(hook):
    name = hook.get("hook_event_name", "")
    if name == "UserPromptSubmit":
        prompt = " ".join(str(hook.get("prompt") or "").split())
        return "task_started", {"prompt": prompt[:140]}
    if name == "PreToolUse":
        payload = {"tool": hook.get("tool_name") or "?"}
        detail = tool_detail(hook.get("tool_input") or {})
        if detail:
            payload["detail"] = detail
        return "tool_called", payload
    if name == "PostToolUse":
        # A tool finished. Write-like tools produced something; every other tool
        # only proves the agent is still alive and working -> heartbeat.
        tool = hook.get("tool_name") or "?"
        artifact = (hook.get("tool_input") or {}).get("file_path")
        if artifact and tool in ARTIFACT_TOOLS:
            return "artifact_produced", {"artifact": str(artifact)[:200]}
        return "heartbeat", {"tool": tool}
    if name == "Notification":
        return "needs_human", {"message": str(hook.get("message") or "")[:200]}
    if name == "Stop":
        return "idle", {}
    if name == "SessionEnd":
        return "session_ended", {}
    return None, None


def post_event(url, event):
    try:
        if os.path.exists(BREAKER) and time.time() - os.path.getmtime(BREAKER) < BREAKER_SECONDS:
            return False
    except OSError:
        pass
    try:
        req = urllib.request.Request(
            url.rstrip("/") + "/events",
            data=json.dumps(event, ensure_ascii=False).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=2):
            pass
        return True
    except Exception:
        try:
            os.makedirs(LOG_DIR, exist_ok=True)
            with open(BREAKER, "w") as f:
                f.write(str(time.time()))
        except OSError:
            pass
        return False


def main():
    hook = json.loads(sys.stdin.read())
    etype, payload = to_event(hook)
    if not etype:
        return
    resident_id = os.environ.get("BURROW_AGENT_ID")
    if resident_id and etype == "session_ended":
        etype, payload = "idle", {}
    if resident_id:
        agent_id = resident_id if ":" in resident_id else "claude-code:" + resident_id
    else:
        agent_id = "claude-code:" + (hook.get("session_id") or "unknown")
    cwd = hook.get("cwd") or ""
    now = datetime.datetime.now(datetime.timezone.utc)
    event = {
        "v": 0,
        "ts": now.isoformat(timespec="milliseconds").replace("+00:00", "Z"),
        "source": "claude-code",
        "agent_id": agent_id,
        "project": os.environ.get("BURROW_PROJECT")
                   or os.path.basename(cwd.rstrip("/")) or "unknown",
        "cwd": cwd,
        "type": etype,
        "payload": payload,
    }
    url = os.environ.get("BURROW_URL")
    if url and post_event(url, event):
        return
    os.makedirs(LOG_DIR, exist_ok=True)
    with open(LOG, "a", encoding="utf-8") as f:
        # Coordinate with server-side in-place rotation. Locking the log itself
        # also works for descriptors opened before rotation because its inode
        # is deliberately retained.
        fcntl.flock(f, fcntl.LOCK_EX)
        f.write(json.dumps(event, ensure_ascii=False) + "\n")


if __name__ == "__main__":
    try:
        main()
    except Exception:
        pass
    sys.exit(0)
