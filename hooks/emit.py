#!/usr/bin/env python3
"""burrow v0 emitter: adapts runner hook callbacks (JSON on stdin) to burrow
protocol events. Claude Code is the default; Codex hooks pass ``--runner codex``.
See docs/protocol.md.

Transport: if BURROW_URL is set, POST the event to <BURROW_URL>/events; if no
target takes it, fall back to appending to ~/.burrow/events.jsonl locally. A
failed POST trips a per-target circuit breaker so an unreachable server never
slows hooks down. If BURROW_TOKEN is set it is sent as `Authorization: Bearer
<token>`; a server that rejects it (401) is just another failed POST — the event
still lands in the local log, so a wrong or missing token loses no events, only
remoteness.

The same event is also POSTed to every BURROW_MIRROR target (default
http://127.0.0.1:8737, the local dev server). A mirror is how you work on burrow
against your own live fleet without deploying: run `python3 serve.py` and your
real sessions show up locally *and* in the shared village. Nothing is listening
most of the time, and a refused loopback connection costs nothing, so this is on
by default; set BURROW_MIRROR= (empty) to turn it off. Mirrors get
BURROW_MIRROR_TOKEN, not BURROW_TOKEN — a dev server runs with ingest open, and
the shared secret has no business being handed to whatever holds port 8737.

Resident agents (services that outlive any one Claude session, like a Telegram
bot running claude -p per message) set BURROW_AGENT_ID (stable villager
identity, e.g. "life-agent") and optionally BURROW_PROJECT (label). For a
resident, SessionEnd maps to `idle` rather than `session_ended`: the session's
process is gone but the agent-as-service is still home, resting.

Must never break the hosting agent: swallow everything, always exit 0."""
import datetime
import fcntl
import hashlib
import json
import os
import re
import sys
import time
import urllib.request

LOG_DIR = os.path.expanduser("~/.burrow")
LOG = os.path.join(LOG_DIR, "events.jsonl")
BREAKER = os.path.join(LOG_DIR, ".post-failed")
BREAKER_SECONDS = 60
# A loopback failure is an instant refused connection, not a timeout, so holding
# the breaker for a full minute would only mean "the dev server you just started
# stays invisible for another 50s".
LOOPBACK_BREAKER_SECONDS = 5
DEFAULT_MIRROR = "http://127.0.0.1:8737"
LOOPBACK_HOSTS = ("127.0.0.1", "localhost", "[::1]", "::1")


ARTIFACT_TOOLS = ("Write", "Edit", "MultiEdit", "NotebookEdit")
RUNNER_SOURCES = {"claude": "claude-code", "codex": "codex"}


def agent_identity(source, identity):
    """Canonical runner-qualified identity used by events and lineage."""
    return source + ":" + str(identity)


def tool_detail(tool_input):
    if not isinstance(tool_input, dict):
        return ""
    for key in ("file_path", "notebook_path", "path", "pattern", "description",
                "command", "url", "query", "skill"):
        val = tool_input.get(key)
        if val:
            return str(val)[:120]
    return ""


def claude_event(hook):
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


def to_event(hook):
    """Backward-compatible Claude adapter surface."""
    return claude_event(hook)


def lineage(hook):
    payload = {}
    if hook.get("turn_id"):
        payload["turn_id"] = str(hook["turn_id"])[:120]
    if hook.get("agent_type"):
        payload["agent_type"] = str(hook["agent_type"])[:120]
    if hook.get("session_id"):
        payload["parent_agent_id"] = agent_identity(
            RUNNER_SOURCES["codex"], hook["session_id"])
    return payload


def lifecycle_payload(hook, phase, include_lineage=False):
    payload = lineage(hook) if include_lineage else {}
    payload["phase"] = phase
    if not include_lineage and hook.get("turn_id"):
        payload["turn_id"] = str(hook["turn_id"])[:120]
    if isinstance(hook.get("stop_hook_active"), bool):
        payload["stop_hook_active"] = hook["stop_hook_active"]
    return payload


def patch_artifacts(tool_input):
    """Resulting paths that a completed Codex apply_patch says it produced."""
    if not isinstance(tool_input, dict):
        return []
    command = tool_input.get("command")
    if not isinstance(command, str):
        return []
    paths = []
    blocks = re.finditer(
        r"^\*\*\* (Add|Update|Delete) File: (.+?)$"
        r"(.*?)(?=^\*\*\* (?:Add|Update|Delete) File: |^\*\*\* End Patch)",
        command, flags=re.MULTILINE | re.DOTALL,
    )
    for block in blocks:
        operation, path, body = block.groups()
        if operation == "Delete":
            continue
        if operation == "Update":
            move = re.search(r"^\*\*\* Move to: (.+)$", body, flags=re.MULTILINE)
            if move:
                path = move.group(1)
        path = path.strip()
        if path and path not in paths:
            paths.append(path[:200])
    return paths


def patch_succeeded(tool_response):
    """True only for the apply_patch response that positively proves success."""
    return tool_response == "Done!"


def codex_events(hook):
    """Adapt one documented Codex lifecycle callback to zero or more v0 events."""
    name = hook.get("hook_event_name", "")
    tool_input = hook.get("tool_input")
    if not isinstance(tool_input, dict):
        tool_input = {}
    tool = str(hook.get("tool_name") or "?")
    bounded_tool = tool[:120]
    if name == "UserPromptSubmit":
        prompt = " ".join(str(hook.get("prompt") or "").split())
        return [("task_started", {"prompt": prompt[:140]})]
    if name == "PreToolUse":
        payload = {"tool": bounded_tool}
        detail = tool_detail(tool_input)
        if detail:
            payload["detail"] = detail
        return [("tool_called", payload)]
    if name == "PermissionRequest":
        reason = tool_input.get("description") or tool_detail(tool_input)
        payload = {"phase": "approval_requested", "tool": bounded_tool}
        if reason:
            payload["detail"] = str(reason)[:120]
        return [("heartbeat", payload)]
    if name == "PostToolUse":
        if tool == "apply_patch" and patch_succeeded(hook.get("tool_response")):
            artifacts = patch_artifacts(tool_input)
            if artifacts:
                return [("artifact_produced", {"artifact": path}) for path in artifacts]
        return [("heartbeat", {"tool": bounded_tool})]
    if name == "SubagentStart":
        if not hook.get("agent_id"):
            return []
        return [("task_started", lineage(hook))]
    if name == "SubagentStop":
        if not hook.get("agent_id"):
            return []
        return [("heartbeat", lifecycle_payload(hook, "subagent_stop", True))]
    if name == "Stop":
        return [("heartbeat", lifecycle_payload(hook, "stop"))]
    if name == "SessionEnd":
        return [("session_ended", {})]
    return []


def adapt_hook(runner, hook):
    if runner == "codex":
        return codex_events(hook)
    etype, payload = claude_event(hook)
    return [(etype, payload)] if etype else []


def runner_name(argv):
    if not argv:
        return "claude"
    if len(argv) != 2 or argv[0] != "--runner":
        return None
    runner = argv[1]
    return runner if runner in RUNNER_SOURCES else None


def hook_agent_id(runner, hook, resident_id=None):
    source = RUNNER_SOURCES[runner]
    if resident_id:
        return resident_id if ":" in resident_id else source + ":" + resident_id
    if runner == "codex" and hook.get("hook_event_name") in (
            "SubagentStart", "SubagentStop") and hook.get("agent_id"):
        identity = hook["agent_id"]
    else:
        identity = hook.get("session_id") or "unknown"
    return agent_identity(source, identity)


def is_loopback(url):
    host = url.split("//", 1)[-1].split("/")[0].rsplit(":", 1)[0]
    return host in LOOPBACK_HOSTS


def breaker_path(url):
    """One breaker per target: a village that is down must not silence the dev
    server running next to it (that pair is exactly the off-tailnet case)."""
    return BREAKER + "-" + hashlib.sha1(url.encode("utf-8")).hexdigest()[:8]


def targets():
    """Where this event goes, in order, as (url, token) — BURROW_URL first, then
    the mirrors. Both vars take a comma-separated list; duplicates collapse so a
    URL named twice never doubles the event."""
    out = []
    seen = set()
    mirror = os.environ.get("BURROW_MIRROR")
    groups = ((os.environ.get("BURROW_URL"), os.environ.get("BURROW_TOKEN")),
              (DEFAULT_MIRROR if mirror is None else mirror,
               os.environ.get("BURROW_MIRROR_TOKEN")))
    for raw, token in groups:
        for url in (u.strip().rstrip("/") for u in (raw or "").split(",")):
            if url and url not in seen:
                seen.add(url)
                out.append((url, (token or "").strip()))
    return out


def post_event(url, event, token=""):
    breaker = breaker_path(url)
    window = LOOPBACK_BREAKER_SECONDS if is_loopback(url) else BREAKER_SECONDS
    try:
        if os.path.exists(breaker) and time.time() - os.path.getmtime(breaker) < window:
            return False
    except OSError:
        pass
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = "Bearer " + token
    try:
        req = urllib.request.Request(
            url.rstrip("/") + "/events",
            data=json.dumps(event, ensure_ascii=False).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=2):
            pass
        return True
    except Exception:
        try:
            os.makedirs(LOG_DIR, exist_ok=True)
            with open(breaker, "w") as f:
                f.write(str(time.time()))
        except OSError:
            pass
        return False


def deliver(event):
    """Shared delivery interface for every runner adapter."""
    delivered = False
    for url, token in targets():
        # No short-circuit: a mirror exists to see the same stream the village
        # sees, so every target gets the event, not just the first one that answers.
        delivered = post_event(url, event, token) or delivered
    if delivered:
        return
    os.makedirs(LOG_DIR, exist_ok=True)
    with open(LOG, "a", encoding="utf-8") as f:
        # Coordinate with server-side in-place rotation. Locking the log itself
        # also works for descriptors opened before rotation because its inode
        # is deliberately retained.
        fcntl.flock(f, fcntl.LOCK_EX)
        f.write(json.dumps(event, ensure_ascii=False) + "\n")


def main(runner="claude"):
    hook = json.loads(sys.stdin.read())
    specs = adapt_hook(runner, hook)
    if not specs:
        return
    resident_id = os.environ.get("BURROW_AGENT_ID")
    agent_id = hook_agent_id(runner, hook, resident_id)
    cwd = hook.get("cwd") or ""
    now = datetime.datetime.now(datetime.timezone.utc)
    for etype, payload in specs:
        if resident_id and etype == "session_ended":
            etype, payload = "idle", {}
        deliver({
            "v": 0,
            "ts": now.isoformat(timespec="milliseconds").replace("+00:00", "Z"),
            "source": RUNNER_SOURCES[runner],
            "agent_id": agent_id,
            "project": os.environ.get("BURROW_PROJECT")
                       or os.path.basename(cwd.rstrip("/")) or "unknown",
            "cwd": cwd,
            "type": etype,
            "payload": payload,
        })


if __name__ == "__main__":
    runner = runner_name(sys.argv[1:])
    if runner:
        try:
            main(runner)
        except Exception:
            pass
    if runner == "codex":
        # Stop/SubagentStop require JSON on stdout; an empty object is advisory
        # and deliberately never approves, denies, blocks, or continues Codex.
        print("{}")
    sys.exit(0)
