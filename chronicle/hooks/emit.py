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

import collections
import datetime
import fcntl
import hashlib
import json
import os
import re
import sys
import threading
import time
import urllib.request
import uuid

try:
    from hooks import durable
except ImportError:  # standalone deployment invokes this file from hooks/
    import durable

LOG_DIR = os.path.expanduser("~/.burrow")
LOG = os.path.join(LOG_DIR, "events.jsonl")
BREAKER = os.path.join(LOG_DIR, ".post-failed")
OUTBOX = os.path.join(LOG_DIR, "primary-outbox.jsonl")
DIAGNOSTICS = os.path.join(LOG_DIR, "transport-diagnostics.json")
BREAKER_SECONDS = 60
# A loopback failure is an instant refused connection, not a timeout, so holding
# the breaker for a full minute would only mean "the dev server you just started
# stays invisible for another 50s".
LOOPBACK_BREAKER_SECONDS = 5
DEFAULT_MIRROR = "http://127.0.0.1:8737"
LOOPBACK_HOSTS = ("127.0.0.1", "localhost", "[::1]", "::1")
POST_TIMEOUT = 0.75
HOOK_BUDGET = 1.0
HOOK_REAP_BUDGET = 0.05
MAX_TARGETS = 8
REPLAY_BATCH = 16
OUTBOX_RECORDS = 1024
OUTBOX_BYTES = 5 * 1024 * 1024
SCHEDULE_RECORDS = 1024
SCHEDULE_BYTES = 64 * 1024
# Recent corrupt suffixes are forensic evidence, not an unbounded second log.
OUTBOX_TORN_FILES = 8
OUTBOX_TORN_BYTES = 256 * 1024
DEFERRED_RECORDS = 1024
DEFERRED_BYTES = 5 * 1024 * 1024
DEFERRED_TORN_FILES = 8
DEFERRED_TORN_BYTES = 256 * 1024
DIAGNOSTIC_HISTORY = 20
_OUTBOX_LOCK = threading.Lock()
_DIAGNOSTIC_LOCK = threading.Lock()
_DEFERRED_ID_FIELD = "_burrow_deferred_id"
OutboxRecordKey = collections.namedtuple("OutboxRecordKey", "target delivery_id")


def _target_id(url):
    """Stable non-sensitive identity used by all persisted target state."""
    return hashlib.sha256(url.encode("utf-8")).hexdigest()[:16]


ARTIFACT_TOOLS = ("Write", "Edit", "MultiEdit", "NotebookEdit")
CODEX_ARTIFACT_TOOLS = ARTIFACT_TOOLS + (
    "write_file",
    "edit_file",
    "notebook_edit",
)
RUNNER_SOURCES = {"claude": "claude-code", "codex": "codex"}


class _ToolDetail(str):
    """String detail carrying its source field until privacy is applied."""

    def __new__(cls, value, is_path=False):
        detail = super().__new__(cls, value)
        detail.is_path = is_path
        return detail


def agent_identity(source, identity):
    """Canonical runner-qualified identity used by events and lineage."""
    return source + ":" + str(identity)


def tool_detail(tool_input):
    if not isinstance(tool_input, dict):
        return ""
    for key in (
        "file_path",
        "notebook_path",
        "path",
        "pattern",
        "description",
        "command",
        "url",
        "query",
        "skill",
    ):
        val = tool_input.get(key)
        if val:
            return _ToolDetail(
                str(val)[:120], key in ("file_path", "notebook_path", "path")
            )
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
        tool_input = hook.get("tool_input") or {}
        artifact = tool_input.get("file_path") or tool_input.get("notebook_path")
        if artifact and tool in ARTIFACT_TOOLS:
            return "artifact_produced", {"artifact": str(artifact)[:200]}
        return "heartbeat", {"tool": tool}
    if name == "PostToolUseFailure":
        tool = str(hook.get("tool_name") or "?")[:120]
        error = hook.get("error")
        payload = {"tool": tool}
        if error:
            payload["error"] = str(error)[:200]
        return "tool_failed", payload
    if name == "Notification":
        # idle/auth notices are informational. Only callbacks that prove an
        # actual question is visible to the human may knock.
        if hook.get("notification_type") not in (
            "permission_prompt",
            "elicitation_dialog",
        ):
            return None, None
        message = str(hook.get("message") or "Attention requested")[:200]
        return "needs_human", {"message": message}
    if name == "SubagentStart" and hook.get("agent_id"):
        return "task_started", dict(lineage(hook, "claude"), prompt="delegated work")
    if name == "SubagentStop" and hook.get("agent_id"):
        return "session_ended", lineage(hook, "claude")
    if name == "Stop":
        return "idle", {}
    if name == "SessionEnd":
        return "session_ended", {}
    return None, None


def to_event(hook):
    """Backward-compatible Claude adapter surface."""
    return claude_event(hook)


def lineage(hook, runner="codex"):
    payload = {}
    if hook.get("turn_id"):
        payload["turn_id"] = str(hook["turn_id"])[:120]
    if hook.get("agent_type"):
        payload["agent_type"] = str(hook["agent_type"])[:120]
    if hook.get("session_id"):
        payload["parent_agent_id"] = agent_identity(
            RUNNER_SOURCES[runner], hook["session_id"]
        )
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
        command,
        flags=re.MULTILINE | re.DOTALL,
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


def tool_failure(hook):
    """Return bounded explicit failure text, or ``None`` absent proof."""
    if hook.get("hook_event_name") == "PostToolUseFailure":
        return str(hook.get("error") or "tool failed")[:200]
    response = hook.get("tool_response")
    if isinstance(response, dict):
        if (
            response.get("success") is False
            or response.get("ok") is False
            or response.get("is_error") is True
            or response.get("status") in ("failed", "error")
        ):
            return str(
                response.get("error") or response.get("message") or "tool failed"
            )[:200]
        code = response.get("exit_code")
        if type(code) is int and code != 0:
            return str(response.get("error") or f"exit code {code}")[:200]
        if response.get("error"):
            return str(response["error"])[:200]
    if hook.get("tool_error"):
        return str(hook["tool_error"])[:200]
    if isinstance(response, str) and re.search(
        r"(?:^|\n)(?:Error|Failed)(?::|\b)", response
    ):
        return response[:200]
    return None


def direct_artifacts(tool, tool_input):
    if tool not in CODEX_ARTIFACT_TOOLS or not isinstance(tool_input, dict):
        return []
    path = tool_input.get("file_path") or tool_input.get("notebook_path")
    return [str(path)[:200]] if path else []


def codex_tool_succeeded(tool_response):
    """True only when a Codex response positively reports tool success."""
    if not isinstance(tool_response, dict):
        return False
    return (
        tool_response.get("success") is True
        or tool_response.get("ok") is True
        or tool_response.get("status") in ("success", "succeeded", "completed")
        or type(tool_response.get("exit_code")) is int
        and tool_response["exit_code"] == 0
    )


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
        message = str(reason or ("Approve " + bounded_tool))[:200]
        return [("needs_human", {"message": message})]
    if name in ("PostToolUse", "PostToolUseFailure"):
        failure = tool_failure(hook)
        if failure is not None:
            return [("tool_failed", {"tool": bounded_tool, "error": failure})]
        if tool == "apply_patch" and patch_succeeded(hook.get("tool_response")):
            artifacts = patch_artifacts(tool_input)
            if artifacts:
                return [("artifact_produced", {"artifact": path}) for path in artifacts]
        artifacts = direct_artifacts(tool, tool_input)
        if artifacts and codex_tool_succeeded(hook.get("tool_response")):
            return [("artifact_produced", {"artifact": path}) for path in artifacts]
        return [("heartbeat", {"tool": bounded_tool})]
    if name == "SubagentStart":
        if not hook.get("agent_id"):
            return []
        return [("task_started", dict(lineage(hook), prompt="delegated work"))]
    if name == "SubagentStop":
        if not hook.get("agent_id"):
            return []
        payload = lineage(hook)
        if isinstance(hook.get("stop_hook_active"), bool):
            payload["stop_hook_active"] = hook["stop_hook_active"]
        return [("session_ended", payload)]
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
    if hook.get("hook_event_name") in ("SubagentStart", "SubagentStop") and hook.get(
        "agent_id"
    ):
        identity = hook["agent_id"]
    elif resident_id:
        return resident_id if ":" in resident_id else source + ":" + resident_id
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
    groups = (
        (os.environ.get("BURROW_URL"), os.environ.get("BURROW_TOKEN")),
        (
            DEFAULT_MIRROR if mirror is None else mirror,
            os.environ.get("BURROW_MIRROR_TOKEN"),
        ),
    )
    for raw, token in groups:
        for url in (u.strip().rstrip("/") for u in (raw or "").split(",")):
            if url and url not in seen:
                seen.add(url)
                out.append((url, (token or "").strip()))
    return out


def post_event(url, event, token="", delivery_id=""):
    breaker = breaker_path(url)
    window = LOOPBACK_BREAKER_SECONDS if is_loopback(url) else BREAKER_SECONDS
    try:
        if os.path.exists(breaker) and time.time() - os.path.getmtime(breaker) < window:
            return False
    except OSError:
        pass
    headers = {"Content-Type": "application/json"}
    if delivery_id:
        headers["X-Burrow-Delivery-ID"] = delivery_id
    if token:
        headers["Authorization"] = "Bearer " + token
    try:
        req = urllib.request.Request(
            url.rstrip("/") + "/events",
            data=json.dumps(event, ensure_ascii=False).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=POST_TIMEOUT):
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


def _target_groups(pending=()):
    configured = targets()
    primary_urls = {
        u.strip().rstrip("/")
        for u in (os.environ.get("BURROW_URL") or "").split(",")
        if u.strip()
    }
    primary = [item for item in configured if item[0] in primary_urls]
    mirrors = [item for item in configured if item[0] not in primary_urls]
    by_key = {_target_id(url): (url, token) for url, token in primary}
    # Each target queue carries its last attempt generation. New records inherit
    # that generation, so fresh events cannot jump a recently attempted target
    # ahead of a target that has never had a worker.
    attempts = _read_schedule()
    for record in pending:
        key = record.get("target")
        if key in by_key:
            attempts[key] = max(attempts.get(key, 0), _attempt_generation(record))
    order = {item[0]: index for index, item in enumerate(primary)}
    ordered = sorted(
        primary,
        key=lambda item: (
            attempts.get(hashlib.sha256(item[0].encode("utf-8")).hexdigest()[:16], 0),
            order[item[0]],
        ),
    )
    primary_slots = min(len(ordered), MAX_TARGETS)
    active_primary = ordered[:primary_slots]
    remaining = max(0, MAX_TARGETS - primary_slots)
    active_mirrors = mirrors[:remaining]
    return (
        active_primary,
        active_mirrors,
        ordered[primary_slots:],
        mirrors[remaining:],
    )


def _outbox_spool():
    """The primary outbox, built per call so patched settings apply.

    Its auxiliary generations are ``.journal.*`` rather than replays, because
    they flow the other way: a writer that could not take the main lock leaves
    one behind, and the next writer that can take it folds them in. Nothing
    ever hands the active outbox off to a reader.
    """
    return durable.Spool(
        OUTBOX,
        lambda: (OUTBOX_RECORDS, OUTBOX_BYTES),
        key=_record_key,
        order=_enqueue_sort_key,
        generation_prefix=".journal.",
        torn_files=OUTBOX_TORN_FILES,
        torn_bytes=OUTBOX_TORN_BYTES,
    )


def _read_active_outbox(spool):
    """The live outbox, whose own writer already proved it whole.

    A damaged line here is skipped rather than treated as a torn tail: unlike
    a journal inherited from a crashed writer, this file was published by an
    atomic replacement, so a bad line is corruption beneath us, not a
    truncated write we should quarantine and replay.
    """
    active = spool.read(damage=durable.SKIP_DAMAGE)
    return list(active.records) if active is not None else []


def _schedule_path():
    return OUTBOX + ".schedule.json"


def _read_schedule():
    try:
        if os.path.getsize(_schedule_path()) > SCHEDULE_BYTES:
            return {}
        with open(_schedule_path(), encoding="utf-8") as stream:
            values = json.load(stream)
        if not isinstance(values, dict):
            return {}
        valid = {
            key: value
            for key, value in values.items()
            if isinstance(key, str) and type(value) is int and value >= 0
        }
        return valid if len(valid) <= SCHEDULE_RECORDS else {}
    except (OSError, ValueError, TypeError):
        return {}


def _new_enqueue_order():
    """Globally comparable order allocated before any outbox lock is taken."""
    return "%020d:%010d:%s" % (time.time_ns(), os.getpid(), uuid.uuid4().hex)


def _enqueue_sort_key(record):
    """Stable total enqueue order, including records written by older emitters."""
    order = record.get("enqueue_order")
    if isinstance(order, str) and order:
        return (1, order)
    event = record.get("event") if isinstance(record.get("event"), dict) else {}
    return (
        0,
        str(event.get("ts") or ""),
        str(record.get("delivery_id") or ""),
        str(record.get("target") or ""),
    )


def _stamp_enqueue_order(records):
    stamped = []
    for record in records:
        record = dict(record)
        record.setdefault("enqueue_order", _new_enqueue_order())
        stamped.append(record)
    return stamped


def _bounded_schedule(schedule, durable_targets):
    primary_urls = [
        url.strip().rstrip("/")
        for url in (os.environ.get("BURROW_URL") or "").split(",")
        if url.strip()
    ]
    configured = {_target_id(url) for url in primary_urls}
    allowed = configured | {
        target for target in durable_targets if isinstance(target, str)
    }
    entries = sorted(
        ((key, value) for key, value in schedule.items() if key in allowed),
        key=lambda item: (item[1], item[0]),
    )
    entries = entries[:SCHEDULE_RECORDS]
    while entries:
        candidate = dict(entries)
        if (
            len(json.dumps(candidate, separators=(",", ":")).encode("utf-8"))
            <= SCHEDULE_BYTES
        ):
            return candidate
        entries.pop()
    return {}


def _journal_outbox(records):
    """Commit bounded auxiliary state without depending on the main lock.

    The one transaction lock serializes every authority snapshot/rewrite. Compaction first makes
    a replacement durable, then retires its input journals, so every accepted
    event always has at least one durable home.
    """
    records = _stamp_enqueue_order(records)  # allocation precedes lock contention
    spool = _outbox_spool()
    try:
        with spool.lock(".transaction", create=True):
            main = _read_active_outbox(spool)
            journals = spool.generation_paths()
            auxiliary = []
            for journal in journals:
                generation = spool.read(journal)
                # Unlike the main path this deliberately drops a torn suffix
                # instead of quarantining it: see docs/spool.md, G3.
                if generation is not None:
                    auxiliary.extend(generation.records)
            auxiliary.extend(records)
            auxiliary = spool.arrange(auxiliary)
            # Journals share the main authority's capacity, so a contended
            # writer can never push the pair past the documented ceiling.
            used = sum(len(spool.encode(item).encode("utf-8")) for item in main)
            kept, victims = spool.bound(
                auxiliary,
                max_records=max(0, OUTBOX_RECORDS - len(main)),
                max_bytes=max(0, OUTBOX_BYTES - used),
            )
            replacement = spool.generation_path(
                "%020d.%s" % (time.time_ns(), uuid.uuid4().hex)
            )
            spool.publish(
                kept,
                target=replacement,
                staging=OUTBOX + ".aux",
                retire=journals,
            )
            if not kept:
                spool.retire((replacement,))
            return len(victims)
    except OSError:
        return None


def _read_outbox_journal(path):
    generation = _outbox_spool().read(path)
    if generation is None:
        return None, b""
    return generation.records, generation.torn


def _quarantine_outbox_tail(torn):
    return _outbox_spool().quarantine_tail(torn)


def _read_durable_outbox_snapshot():
    """Best-effort oldest-first view of main and immutable journals."""
    spool = _outbox_spool()
    with spool.lock(".transaction", create=True):
        records = _read_active_outbox(spool)
        for journal in spool.generation_paths():
            generation = spool.read(journal)
            if generation is not None and generation.records:
                records.extend(generation.records)
        return spool.arrange(records)


def _record_key(record):
    return OutboxRecordKey(record.get("target"), record.get("delivery_id"))


def _attempt_generation(record):
    value = record.get("attempt_generation")
    return value if type(value) is int and value >= 0 else 0


def _recover_outbox():
    """Discard an orphan staging file; only OUTBOX is authoritative.

    A syntactically valid prefix (including an empty file) says nothing about
    whether the writer completed its intended generation. Atomic replacement
    commits a generation; surviving staging bytes are therefore never promoted.
    """
    _outbox_spool().discard_staging()


def _update_outbox(delivered_keys, additions, attempted_targets=()):
    """Transact all authorities; main lock deliberately remains nonblocking.

    Lock order is process thread lock, stable transaction lock, then stable main
    lock. Auxiliary writers never take the main lock, so this order cannot cycle.
    """
    additions = _stamp_enqueue_order(additions)  # older contenders retain priority
    spool = _outbox_spool()
    with _OUTBOX_LOCK, spool.lock(".transaction", create=True):
        with spool.lock(blocking=False) as main:
            if main is None:
                return 0, False
            _recover_outbox()
            records = _read_active_outbox(spool)
            journals = []
            for journal in spool.generation_paths():
                generation = spool.read(journal)
                if generation is None:
                    continue
                journals.append((journal, generation.torn))
                records.extend(generation.records)
            records = spool.arrange(records)
            records = [
                record
                for record in records
                if _record_key(record) not in delivered_keys
            ]
            known = {_record_key(record) for record in records}
            known_events = {
                (
                    record.get("target"),
                    json.dumps(record.get("event"), sort_keys=True, ensure_ascii=False),
                )
                for record in records
            }
            target_generations = {}
            for record in records:
                target = record.get("target")
                target_generations[target] = max(
                    target_generations.get(target, 0), _attempt_generation(record)
                )
            for addition in additions:
                if (
                    _record_key(addition) in known
                    or (
                        addition.get("target"),
                        json.dumps(
                            addition.get("event"), sort_keys=True, ensure_ascii=False
                        ),
                    )
                    in known_events
                ):
                    continue
                addition = dict(addition)
                addition["attempt_generation"] = target_generations.get(
                    addition.get("target"), 0
                )
                records.append(addition)
            attempted_targets = set(attempted_targets)
            if attempted_targets:
                generation = (
                    max((_attempt_generation(record) for record in records), default=0)
                    + 1
                )
                for record in records:
                    if record.get("target") in attempted_targets:
                        record["attempt_generation"] = generation
            records = spool.arrange(records)
            kept, victims = spool.bound(records)
            try:
                schedule = _read_schedule()
                if attempted_targets:
                    generation = max(schedule.values(), default=0) + 1
                    for target in attempted_targets:
                        schedule[target] = generation
                schedule = _bounded_schedule(
                    schedule, (record.get("target") for record in records)
                )
                # The outbox and its fairness schedule are replaced in one
                # publish, outbox first. A crash between them leaves a stale
                # schedule, which is safe only because _read_schedule treats
                # anything it cannot trust as empty.
                spool.publish(
                    kept,
                    extra=(
                        (durable.stage_json(_schedule_path(), schedule),
                         _schedule_path()),
                    ),
                    quarantine=journals,
                    retire=[journal for journal, _ in journals],
                )
                return len(victims), True
            except OSError:
                return 0, False


def _diagnose(kind, **details):
    """Persist counters and a bounded, payload-free recent diagnostic list."""
    with _DIAGNOSTIC_LOCK:
        try:
            os.makedirs(os.path.dirname(os.path.abspath(DIAGNOSTICS)), exist_ok=True)
            with open(durable.lock_path(DIAGNOSTICS), "a+") as lock:
                # Helper-side persistence may wait: the parent enforces the
                # aggregate one-second budget and kills a stalled helper.
                fcntl.flock(lock, fcntl.LOCK_EX)
                try:
                    with open(DIAGNOSTICS, encoding="utf-8") as stream:
                        report = json.load(stream)
                    if not isinstance(report, dict):
                        raise ValueError("diagnostic root")
                    recent = report.get("recent")
                    if not isinstance(recent, list):
                        raise ValueError("diagnostic history")
                    for counter in ("failures", "retries", "drops"):
                        if type(report.get(counter, 0)) is not int:
                            raise ValueError("diagnostic counter")
                    repaired = False
                except (ValueError, OSError, TypeError):
                    report = {"failures": 0, "retries": 0, "drops": 0, "recent": []}
                    repaired = True
                if repaired:
                    report["recent"].append(
                        {"kind": "repair", "reason": "invalid diagnostics"}
                    )
                if kind in ("failure", "retry", "drop"):
                    key = {"failure": "failures", "retry": "retries", "drop": "drops"}[
                        kind
                    ]
                    report[key] = int(report.get(key, 0)) + int(details.pop("count", 1))
                report["updated_at"] = (
                    datetime.datetime.now(datetime.timezone.utc)
                    .isoformat(timespec="milliseconds")
                    .replace("+00:00", "Z")
                )
                report.setdefault("recent", []).append(dict(kind=kind, **details))
                report["recent"] = report["recent"][-DIAGNOSTIC_HISTORY:]
                pending = durable.stage_json(DIAGNOSTICS, report, ensure_ascii=False)
                durable.publish_staged(((pending, DIAGNOSTICS),))
                return True
        except (OSError, ValueError, TypeError):
            return False


def _deferred_record(line):
    """One deferred event per line, carrying a stable replay identity.

    A record written by an older emitter has no ID field. Deriving one from
    the line's own bytes gives it a stable identity anyway, so it deduplicates
    against its own replayed copies exactly like a modern record.
    """
    record = json.loads(line)
    if not isinstance(record, dict):
        return None
    record.setdefault(
        _DEFERRED_ID_FIELD, hashlib.sha256(line.encode("utf-8")).hexdigest()
    )
    return record


def _deferred_spool(path):
    """The local deferred log, built per call so patched settings apply."""
    return durable.Spool(
        path,
        lambda: (DEFERRED_RECORDS, DEFERRED_BYTES),
        decode=_deferred_record,
        key=lambda record: record[_DEFERRED_ID_FIELD],
        torn_files=DEFERRED_TORN_FILES,
        torn_bytes=DEFERRED_TORN_BYTES,
        torn_at_source=True,
    )


def _retire_quietly(spool, paths):
    """Retire what is already redundant, never failing the commit over it.

    These removals are pure housekeeping: every record in them is provably
    also somewhere else. Letting a permission or I/O error here abort the
    caller would turn tidying into event loss.
    """
    try:
        spool.retire(paths)
    except OSError:
        pass


def _compact_deferred_locked(path, addition=None):
    """Publish one bounded authority while the stable deferred lock is held."""
    spool = _deferred_spool(path)
    # A crash after replacement but before source retirement leaves replay IDs
    # wholly represented by active. Retire those redundant copies before
    # allocating another pending generation, preserving one-copy headroom.
    active = spool.read()
    if active is not None and active.records and not active.torn:
        active_ids = {record[_DEFERRED_ID_FIELD] for record in active.records}
        redundant = []
        for generation in spool.generation_paths():
            replay = spool.read(generation)
            if replay is None or replay.torn:
                continue
            if {record[_DEFERRED_ID_FIELD] for record in replay.records} <= active_ids:
                redundant.append(generation)
        if redundant:
            _retire_quietly(spool, redundant)

    records = []
    quarantine = []
    for generation in spool.snapshot():
        records.extend(generation.records)
        if generation.torn:
            quarantine.append((generation.path, generation.torn))
    if addition is not None:
        records.append(addition)
    kept, victims = spool.bound(spool.dedupe(records))
    dropped = len(victims)
    # Report victims before the authority that omits them is published. A crash
    # may conservatively over-report a drop, but can never create a silent one.
    if dropped and not _diagnose(
        "drop", count=dropped, reason="local deferred capacity"
    ):
        raise OSError("local deferred drop diagnostic was not durable")
    spool.publish(kept, quarantine=quarantine, retire=spool.generation_paths())
    return dropped


def _defer_local(event):
    """Commit into the bounded active-plus-replay deferred authority."""
    path = LOG + ".deferred"
    record = dict(event)
    record[_DEFERRED_ID_FIELD] = uuid.uuid4().hex
    with _deferred_spool(path).lock():
        _compact_deferred_locked(path, record)


def _replay_deferred(live):
    """Hand off and idempotently replay immutable deferred generations."""
    path = LOG + ".deferred"
    spool = _deferred_spool(path)
    with spool.lock():
        # Active-only authority was already bounded by its committing writer.
        # Consolidation is needed only after a crash leaves replay authority.
        if spool.generation_paths():
            _compact_deferred_locked(path)
        spool.handoff()

        known = set()
        live.flush()
        live.seek(0)
        for line in live:
            try:
                existing = json.loads(line)
            except ValueError:
                continue
            if isinstance(existing, dict) and existing.get(_DEFERRED_ID_FIELD):
                known.add(existing[_DEFERRED_ID_FIELD])
        for generation in spool.generation_paths():
            parsed = spool.read(generation)
            for record in parsed.records if parsed is not None else ():
                record_id = record[_DEFERRED_ID_FIELD]
                if record_id not in known:
                    live.write(json.dumps(record, ensure_ascii=False) + "\n")
                    known.add(record_id)
            # The acknowledgement is a durable write into somebody else's file,
            # so it must be fsynced before this generation may be retired. A
            # crash in between replays these records once more and the live log
            # then recognises their IDs, which is why that is idempotent.
            live.flush()
            os.fsync(live.fileno())
            _retire_quietly(spool, (generation,))


def _append_local(event):
    os.makedirs(LOG_DIR, exist_ok=True)
    with open(LOG, "a+", encoding="utf-8") as f:
        # Coordinate with server-side in-place rotation. Locking the log itself
        # also works for descriptors opened before rotation because its inode
        # is deliberately retained.
        line = json.dumps(event, ensure_ascii=False) + "\n"
        try:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            # Rotation currently owns the live inode. Persist independently;
            # a later owner atomically hands off and replays this journal.
            _defer_local(event)
            _diagnose("failure", reason="local log lock contention")
            return
        _replay_deferred(f)
        f.write(line)
        f.flush()
        os.fsync(f.fileno())


def deliver(event):
    """Deliver one redacted event under one bounded hook budget.

    Primary queues replay oldest-first; mirrors see only the current event and
    can never acknowledge a primary. Independent targets receive one daemon
    worker each, capped by ``MAX_TARGETS``.
    """
    event = redact_event(event)
    deadline = time.monotonic() + HOOK_BUDGET
    current_id = uuid.uuid4().hex
    initial_primary, initial_mirrors, later_primary, later_mirrors = _target_groups()
    configured_primary = initial_primary + later_primary
    configured_mirrors = initial_mirrors + later_mirrors
    local_written = False
    if not configured_primary:
        _append_local(event)
        local_written = True
        mirrors = configured_mirrors[:MAX_TARGETS]
        deferred_mirrors = configured_mirrors[MAX_TARGETS:]
        results = []
        result_lock = threading.Lock()

        def mirror_worker(url, token):
            delivered = post_event(url, event, token, "")
            with result_lock:
                results.append(delivered)

        workers = []
        for url, token in mirrors:
            worker = threading.Thread(
                target=mirror_worker, args=(url, token), daemon=True
            )
            worker.start()
            workers.append(worker)
        for worker in workers:
            worker.join(max(0, deadline - time.monotonic()))
        if deferred_mirrors:
            _diagnose("failure", reason="target limit", count=len(deferred_mirrors))
        return

    additions = _stamp_enqueue_order(
        [
            {"delivery_id": current_id, "target": _target_id(url), "event": event}
            for url, _ in configured_primary
        ]
    )
    dropped, queued = _update_outbox(set(), additions)
    if dropped:
        _diagnose("drop", count=dropped, reason="outbox capacity")
    if not queued:
        journal_dropped = _journal_outbox(additions)
        journaled = journal_dropped is not None
        _append_local(event)
        local_written = True
        if journal_dropped:
            _diagnose(
                "drop", count=journal_dropped, reason="outbox capacity under contention"
            )
        _diagnose(
            "failure",
            reason=(
                "outbox lock contention"
                if journaled
                else "outbox contention journal failure"
            ),
        )
    pending = _read_durable_outbox_snapshot()
    primary, mirrors, deferred_primary, deferred_mirrors = _target_groups(pending)
    results = {}

    # Reserve the selected turn before network work. Fairness therefore
    # survives success (which removes queue records), failure, and restart.
    attempted = {_target_id(url) for url, _ in primary}
    _, reserved = _update_outbox(set(), [], attempted)
    if not reserved:
        _diagnose("failure", reason="schedule reservation contention")

    def primary_worker(url, token):
        target_key = _target_id(url)
        queued = [record for record in pending if record.get("target") == target_key]
        delivered_keys = []
        for record in queued[:REPLAY_BATCH]:
            if not post_event(
                url,
                record.get("event") or {},
                token,
                str(record.get("delivery_id") or ""),
            ):
                results[url] = (delivered_keys, False, target_key)
                return
            delivered_keys.append(_record_key(record))
            _diagnose("retry", target=target_key)
        results[url] = (delivered_keys, len(delivered_keys) == len(queued), target_key)

    workers = []
    for url, token in primary:
        worker = threading.Thread(target=primary_worker, args=(url, token), daemon=True)
        worker.start()
        workers.append(worker)
    for url, token in mirrors:
        worker = threading.Thread(
            target=post_event, args=(url, event, token, ""), daemon=True
        )
        worker.start()
        workers.append(worker)
    for worker in workers:
        worker.join(max(0, deadline - time.monotonic()))

    delivered_keys, primary_failed = set(), False
    for url, _ in primary:
        target_key = _target_id(url)
        replayed, current_ok, _ = results.get(url, ([], False, target_key))
        delivered_keys.update(replayed)
        if not current_ok:
            primary_failed = True
            _diagnose("failure", target=target_key)
    if deferred_primary:
        primary_failed = True
    if deferred_primary or deferred_mirrors:
        _diagnose(
            "failure",
            reason="target limit",
            count=len(deferred_primary) + len(deferred_mirrors),
        )
    if time.monotonic() < deadline:
        _, updated = _update_outbox(delivered_keys, [])
        if not updated:
            _diagnose("failure", reason="outbox lock contention")
    else:
        _diagnose("failure", reason="hook budget deferred outbox acknowledgement")
    if primary_failed and not local_written and time.monotonic() < deadline:
        _append_local(event)


def detail_policy():
    value = os.environ.get("BURROW_DETAIL", "full").strip().lower()
    return value if value in ("full", "safe", "off") else "safe"


def _safe_path(value):
    value = str(value)
    return re.split(r"[/\\]", value.rstrip("/\\"))[-1] or "[redacted]"


def redact_event(event):
    """Apply producer privacy policy before any transport or local write."""
    policy = detail_policy()
    out = dict(event)
    out["payload"] = dict(event.get("payload") or {})
    payload = out["payload"]
    if policy == "full":
        if isinstance(payload.get("detail"), _ToolDetail):
            payload["detail"] = str(payload["detail"])
        return out
    out["cwd"] = ""
    if "artifact" in payload:
        payload["artifact"] = (
            _safe_path(payload["artifact"]) if policy == "safe" else "[redacted]"
        )
    for key in ("prompt", "message", "detail", "error"):
        if key in payload:
            if (
                policy == "safe"
                and key == "detail"
                and getattr(payload[key], "is_path", False)
            ):
                payload[key] = _safe_path(payload[key])
            else:
                payload[key] = "[redacted]"
    return out


def main(runner="claude"):
    hook = json.loads(sys.stdin.read())
    specs = adapt_hook(runner, hook)
    if not specs:
        return
    resident_id = os.environ.get("BURROW_AGENT_ID")
    agent_id = hook_agent_id(runner, hook, resident_id)
    resident_parent = None
    if resident_id:
        source = RUNNER_SOURCES[runner]
        resident_parent = (
            resident_id if ":" in resident_id else source + ":" + resident_id
        )
    cwd = hook.get("cwd") or ""
    now = datetime.datetime.now(datetime.timezone.utc)
    for etype, payload in specs:
        if resident_parent and payload.get("parent_agent_id"):
            payload = dict(payload, parent_agent_id=resident_parent)
        if resident_id and etype == "session_ended":
            # A child lifecycle still ends that child. Only the stable resident
            # parent rests between backing sessions.
            if agent_id == resident_parent:
                etype, payload = "idle", {}
        deliver(
            {
                "v": 0,
                "ts": now.isoformat(timespec="milliseconds").replace("+00:00", "Z"),
                "source": RUNNER_SOURCES[runner],
                "agent_id": agent_id,
                "project": os.environ.get("BURROW_PROJECT")
                or (_safe_path(cwd) if cwd else "unknown"),
                "cwd": cwd,
                "type": etype,
                "payload": payload,
            }
        )


def run_hook_bounded(runner="claude"):
    """Run the whole transport path in a killable helper process.

    Filesystem calls can block below Python, so deadline checks cannot bound a
    hook. The hosting hook reserves bounded time within HOOK_BUDGET to terminate
    the child and poll WNOHANG for reaping; it never performs a blocking wait.
    """
    deadline = time.monotonic() + HOOK_BUDGET
    work_deadline = deadline - min(HOOK_REAP_BUDGET, HOOK_BUDGET)
    try:
        pid = os.fork()
    except OSError:
        # Starting an unbounded fallback would violate the hook contract.
        sys.stderr.write("burrow transport failure: ProcessStartError\n")
        return
    if pid == 0:
        try:
            main(runner)
        except Exception as error:
            sys.stderr.write(
                "burrow transport failure: " + type(error).__name__[:80] + "\n"
            )
        finally:
            os._exit(0)
    while time.monotonic() < work_deadline:
        waited, _ = os.waitpid(pid, os.WNOHANG)
        if waited == pid:
            return
        time.sleep(min(0.005, max(0, work_deadline - time.monotonic())))
    sys.stderr.write("burrow transport timeout\n")
    try:
        os.kill(pid, 9)
    except ProcessLookupError:
        pass
    while time.monotonic() < deadline:
        try:
            waited, _ = os.waitpid(pid, os.WNOHANG)
        except ChildProcessError:
            return
        if waited == pid:
            return
        time.sleep(min(0.005, max(0, deadline - time.monotonic())))


if __name__ == "__main__":
    runner = runner_name(sys.argv[1:])
    if runner:
        try:
            run_hook_bounded(runner)
        except Exception as error:
            # Last-resort diagnostic when even the durable state directory is
            # unavailable. Never echo exception text: it may contain a path,
            # URL, credential, or event detail.
            sys.stderr.write(
                "burrow transport failure: " + type(error).__name__[:80] + "\n"
            )
    if runner == "codex":
        # Stop/SubagentStop require JSON on stdout; an empty object is advisory
        # and deliberately never approves, denies, blocks, or continues Codex.
        print("{}")
    sys.exit(0)
