#!/usr/bin/env python3
"""burrow v0 emitter: adapts a Claude Code hook callback (JSON on stdin) to one
burrow protocol event appended to ~/.burrow/events.jsonl. See docs/protocol.md.

Must never break the hosting agent: swallow everything, always exit 0."""
import datetime
import json
import os
import sys

LOG_DIR = os.path.expanduser("~/.burrow")
LOG = os.path.join(LOG_DIR, "events.jsonl")


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
        artifact = (hook.get("tool_input") or {}).get("file_path")
        if not artifact:
            return None, None
        return "artifact_produced", {"artifact": str(artifact)[:200]}
    if name == "Notification":
        return "needs_human", {"message": str(hook.get("message") or "")[:200]}
    if name == "Stop":
        return "idle", {}
    if name == "SessionEnd":
        return "session_ended", {}
    return None, None


def main():
    hook = json.loads(sys.stdin.read())
    etype, payload = to_event(hook)
    if not etype:
        return
    cwd = hook.get("cwd") or ""
    now = datetime.datetime.now(datetime.timezone.utc)
    event = {
        "v": 0,
        "ts": now.isoformat(timespec="milliseconds").replace("+00:00", "Z"),
        "source": "claude-code",
        "agent_id": "claude-code:" + (hook.get("session_id") or "unknown"),
        "project": os.path.basename(cwd.rstrip("/")) or "unknown",
        "cwd": cwd,
        "type": etype,
        "payload": payload,
    }
    os.makedirs(LOG_DIR, exist_ok=True)
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(json.dumps(event, ensure_ascii=False) + "\n")


if __name__ == "__main__":
    try:
        main()
    except Exception:
        pass
    sys.exit(0)
