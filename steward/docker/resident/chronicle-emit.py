#!/usr/bin/env python3
#
# GENERATED FILE — DO NOT EDIT.
#
# The chronicle hook emitter as one self-contained stdlib-only file: hooks/emit.py
# verbatim, with its `import durable` block replaced by hooks/durable.py's own source,
# materialized as a module. Built by hooks/build.py; the emitter is not written here.
#
# To change what this file does, edit chronicle/hooks/emit.py or chronicle/hooks/durable.py
# and rebuild:
#
#     python3 chronicle/hooks/build.py --output <path>
#
# steward vendors a copy of this artifact into docker/resident/chronicle-emit.py with
# `make vendor-emitter`, and its suite rebuilds the bundle at HEAD and compares byte for
# byte — so a hand edit here, or a source change nobody re-vendored, is a red build
# rather than a resident emitting a protocol nobody reads.
#
# Built from these bytes and nothing else:
#   hooks/emit.py     sha256:ffe5cce570801cfd6c893278b81a13b3a3386ceeac09306d751a7a9c3da34d19
#   hooks/durable.py  sha256:e30695fe62cb49dc88d283d29de4b2a7749ad3e7652c1fb44a43f3baee205e1b
#
# No commit and no date, deliberately: this header is compared byte for byte against a
# rebuild, and every git-derived value changes under a rebase while the sources do not.
# `git log -1 -- chronicle/hooks/` names the commit; the digests above name the bytes.
#
"""chronicle v0 emitter: adapts runner hook callbacks (JSON on stdin) to
chronicle protocol events. Claude Code is the default; Codex hooks pass
``--runner codex``. See docs/protocol.md.

Every setting below is read under its CHRONICLE_ name and, for one release, its
pre-rename BURROW_ name (see ``_setting``). Hook environment is captured when a
session *starts*, so at any moment there are live sessions holding either
spelling; both have to work or the rename would silence whichever fleet had not
been restarted yet.

Transport: if CHRONICLE_URL is set, POST the event to <CHRONICLE_URL>/events; if
no target takes it, fall back to appending to ~/.chronicle/events.jsonl locally. A failed
POST trips a per-target circuit breaker so an unreachable server never slows
hooks down. If CHRONICLE_TOKEN is set it is sent as `Authorization: Bearer
<token>`; a server that rejects it (401) is just another failed POST — the event
still lands in the local log, so a wrong or missing token loses no events, only
remoteness.

The same event is also POSTed to every CHRONICLE_MIRROR target (default
http://127.0.0.1:8737, the local dev server). A mirror is how you work on
chronicle against your own live fleet without deploying: run `python3 serve.py`
and your real sessions show up locally *and* in the shared village. Nothing is
listening most of the time, and a refused loopback connection costs nothing, so
this is on by default; set CHRONICLE_MIRROR= (empty) to turn it off. Mirrors get
CHRONICLE_MIRROR_TOKEN, not CHRONICLE_TOKEN — a dev server runs with ingest
open, and the shared secret has no business being handed to whatever holds port
8737.

Resident agents (services that outlive any one Claude session, like a Telegram
bot running claude -p per message) set CHRONICLE_AGENT_ID (stable villager
identity, e.g. "life-agent") and optionally CHRONICLE_PROJECT (label). For a
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

# --- hooks/durable.py, embedded ---------------------------------------------------
# emit.py imports durable as a sibling module; a one-file artifact has no siblings, so
# the source is carried here and materialized as one. The text between the delimiters is
# durable.py byte for byte, compiled under its own name so a traceback still points at
# the right line of the right file.
import types as _bundled_types

_DURABLE_SOURCE = r'''"""Crash-safe durable storage shared by hook and server.

Two layers live here:

* file-generation primitives — ``stage_lines``, ``stage_json``,
  ``publish_staged``, ``retire_files`` — which own the durable write, publish
  and retirement ordering for one file at a time;
* :class:`Spool` — one bounded, ordered, crash-safe log built from them: an
  active authority plus immutable auxiliary generations behind a stable
  sidecar lock.

``Spool`` owns exactly the crash ordering: publication, forensic evidence,
retirement, generation handoff, capacity and dedupe. It owns no process state,
no locks it is not asked for, and no delivery policy. ``docs/spool.md`` states
each guarantee it enforces, names the site each came from, and lists what
deliberately did not unify and stayed behind in a caller.

Callers hold their protocol's stable lock while collecting authority.
"""

import collections
import contextlib
import fcntl
import glob
import errno
import json
import os
import time
import uuid


PENDING_SUFFIX = ".pending"
LOCK_SUFFIX = ".lock"
REPLAY_PREFIX = ".replay."
TORN_PREFIX = ".torn."

# How one generation reacts to a line it cannot decode. The choice is a
# durability decision, never a convenience: see docs/spool.md.
STOP_AT_DAMAGE = "stop"  # the rest of the file is a torn tail to quarantine
SKIP_DAMAGE = "skip"  # the line is lost, and the generation is not retirable


def pending_path(path):
    return path + PENDING_SUFFIX


def lock_path(path):
    return path + LOCK_SUFFIX


def replay_paths(path):
    return sorted(glob.glob(path + REPLAY_PREFIX + "*"))


def replay_path(path, generation):
    if not generation or os.sep in generation:
        raise ValueError("invalid replay generation: %r" % (generation,))
    return path + REPLAY_PREFIX + generation


def fsync_parent(path):
    descriptor = os.open(os.path.dirname(os.path.abspath(path)), os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def stage_lines(path, lines):
    pending = pending_path(path)
    with open(pending, "w", encoding="utf-8") as stream:
        stream.writelines(lines)
        stream.flush()
        os.fsync(stream.fileno())
    return pending


def stage_json(path, value, ensure_ascii=True):
    pending = pending_path(path)
    with open(pending, "w", encoding="utf-8") as stream:
        json.dump(value, stream, ensure_ascii=ensure_ascii, separators=(",", ":"))
        stream.flush()
        os.fsync(stream.fileno())
    return pending


def publish_staged(replacements):
    targets = []
    for pending, target in replacements:
        os.replace(pending, target)
        targets.append(target)
    for directory in {os.path.dirname(os.path.abspath(path)) for path in targets}:
        fsync_parent(os.path.join(directory, "."))


def retire_files(paths):
    changed_parents = []
    failure = None
    try:
        for path in paths:
            try:
                os.unlink(path)
                parent = os.path.dirname(os.path.abspath(path))
                if parent not in changed_parents:
                    changed_parents.append(parent)
            except OSError as error:
                if isinstance(error, FileNotFoundError) or error.errno == errno.ENOENT:
                    continue
                failure = error
                break
    finally:
        # A later unlink failure must not leave earlier successful removals only
        # in the directory cache.  Sync those removals before reporting failure.
        for directory in changed_parents:
            try:
                fsync_parent(os.path.join(directory, "."))
            except OSError as error:
                if failure is None:
                    failure = error
    if failure is not None:
        raise failure
    return bool(changed_parents)


def publish_lines(path, lines, retire=()):
    """Durably publish one generation, then durably retire its inputs."""
    publish_staged(((stage_lines(path, lines), path),))
    retire_files(retire)


def torn_path(source):
    """Quarantine name that sorts by creation time within one root.

    Retention orders by mtime and breaks ties on this name, because samples
    torn from different generations have different roots and their names are
    not comparable across them.
    """
    return source + TORN_PREFIX + "%020d.%s" % (time.time_ns(), uuid.uuid4().hex)


def json_record(line):
    """One JSON object per line. Anything else is damage."""
    record = json.loads(line)
    if not isinstance(record, dict):
        raise ValueError("spool record is not a JSON object")
    return record


def json_entry(line):
    """One JSON object per line, where a blank line is absent, not damage."""
    if not line.strip():
        return None
    return json_record(line)


def encode_json(record):
    return json.dumps(record, ensure_ascii=False) + "\n"


def encode_compact_json(record):
    return json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n"


def decode_text(line):
    """One opaque key per line, where a blank or blank-looking line is absent."""
    return line.rstrip("\n") if line.strip() else None


def encode_text(value):
    return value + "\n"


def encode_raw(line):
    """Pass an already-encoded line through unchanged."""
    return line


Generation = collections.namedtuple("Generation", "path records torn complete")


class Spool:
    """One bounded, ordered, crash-safe log.

    An active authority at ``path`` plus immutable auxiliary generations
    (``path`` + ``generation_prefix`` + name). Reading tolerates damage under
    an explicit policy; writing always publishes before it retires, and always
    keeps forensic evidence before it erases the file that evidence came from.

    ``limits`` is a callable returning ``(records, bytes)`` so live settings
    are re-read on every operation rather than frozen at construction.
    Construction performs no I/O, so a caller whose paths move (tests, a
    reconfigured server) can build a spool per call.
    """

    def __init__(
        self,
        path,
        limits=None,
        decode=json_record,
        encode=encode_json,
        key=None,
        order=None,
        generation_prefix=REPLAY_PREFIX,
        torn_files=8,
        torn_bytes=256 * 1024,
        torn_at_source=False,
    ):
        self.path = path
        # A spool used only to name, read, publish or retire needs no capacity.
        self.limits = limits if limits is None or callable(limits) else (lambda: limits)
        self.decode = decode
        self.encode = encode
        self.key = key
        self.order = order
        self.generation_prefix = generation_prefix
        self.torn_files = torn_files
        self.torn_bytes = torn_bytes
        # Where a quarantine file is named: beside the spool, or beside the
        # generation the bytes came from. Both roots share one budget, but the
        # choice is visible on disk, so it is configuration rather than taste.
        self.torn_at_source = torn_at_source

    # -- names ---------------------------------------------------------

    def pending_path(self, target=None):
        return pending_path(self.path if target is None else target)

    def lock_path(self, suffix=""):
        """This spool's stable sidecar lock, optionally a named second one."""
        return lock_path(self.path + suffix)

    def generation_path(self, generation):
        if not generation or os.sep in generation:
            raise ValueError("invalid replay generation: %r" % (generation,))
        return self.path + self.generation_prefix + generation

    def generation_paths(self):
        """Immutable auxiliary generations, oldest name first.

        A quarantine file can match the generation glob (it is named after the
        generation it came from). It is evidence, never authority, so it is
        excluded here rather than at every call site.
        """
        prefix = len(self.path)
        return [
            candidate
            for candidate in sorted(glob.glob(self.path + self.generation_prefix + "*"))
            if TORN_PREFIX not in candidate[prefix:]
        ]

    def generations(self):
        """The active authority first, then its auxiliary generations."""
        return [self.path] + self.generation_paths()

    # -- locking -------------------------------------------------------

    @contextlib.contextmanager
    def lock(self, suffix="", exclusive=True, blocking=True, create=False):
        """Hold this spool's stable sidecar lock.

        Yields the open lock file, or ``None`` when a non-blocking acquisition
        found it already held — contention is a result, never an exception.
        """
        if create:
            os.makedirs(os.path.dirname(os.path.abspath(self.path)), exist_ok=True)
        handle = open(self.lock_path(suffix), "a+")
        try:
            mode = fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH
            try:
                fcntl.flock(handle, mode | (0 if blocking else fcntl.LOCK_NB))
            except BlockingIOError:
                yield None
                return
            yield handle
        finally:
            handle.close()

    # -- reading -------------------------------------------------------

    def read(self, path=None, damage=STOP_AT_DAMAGE):
        """Decode one generation, or ``None`` when it cannot be read at all.

        ``None`` and an empty generation are deliberately distinguishable: a
        file that could not be opened must not be mistaken for one that was
        legitimately published empty, because the second is retirable and the
        first is not.
        """
        path = self.path if path is None else path
        try:
            with open(path, "rb") as stream:
                data = stream.read()
        except OSError:
            return None
        records = []
        complete = True
        offset = 0
        for line in data.splitlines(keepends=True):
            try:
                record = self.decode(line.decode("utf-8"))
            except (UnicodeDecodeError, ValueError):
                complete = False
                if damage == STOP_AT_DAMAGE:
                    return Generation(path, records, data[offset:], False)
                offset += len(line)
                continue
            offset += len(line)
            if record is not None:
                records.append(record)
        return Generation(path, records, b"", complete)

    def snapshot(self, damage=STOP_AT_DAMAGE):
        """Every readable generation, active first.

        A caller wanting different tolerance for the active authority than for
        generations inherited from a crashed writer reads them separately;
        ``emit.py``'s outbox is the one place that needs to.
        """
        found = []
        for path in self.generations():
            generation = self.read(path, damage)
            if generation is not None:
                found.append(generation)
        return found

    def collect(self, damage=STOP_AT_DAMAGE):
        """Deduped, ordered records across every readable generation."""
        records = []
        for generation in self.snapshot(damage):
            records.extend(generation.records)
        return self.arrange(records)

    # -- policy --------------------------------------------------------

    def dedupe(self, records):
        """Collapse repeats: last value wins, in the first occurrence's slot.

        This is what makes the publish-then-retire crash window harmless. The
        surviving duplicate carries whatever the newer writer knew, without
        letting it jump ahead of work that was queued earlier.
        """
        if self.key is None:
            return list(records)
        positions = {}
        unique = []
        for record in records:
            identity = self.key(record)
            if identity in positions:
                unique[positions[identity]] = record
            else:
                positions[identity] = len(unique)
                unique.append(record)
        return unique

    def arrange(self, records):
        unique = self.dedupe(records)
        return sorted(unique, key=self.order) if self.order else unique

    def bound(self, records, max_records=None, max_bytes=None):
        """Evict oldest-first until both caps hold; return ``(kept, victims)``.

        Capacity is measured in encoded UTF-8 bytes, never in-memory size, so
        the bound describes what the file will actually cost.
        """
        if self.limits is None and (max_records is None or max_bytes is None):
            raise ValueError("spool has no capacity to bound records against")
        limit_records, limit_bytes = self.limits() if self.limits else (0, 0)
        if max_records is not None:
            limit_records = max_records
        if max_bytes is not None:
            limit_bytes = max_bytes
        kept = list(records)
        sizes = [len(self.encode(record).encode("utf-8")) for record in kept]
        total = sum(sizes)
        victims = []
        while kept and (len(kept) > limit_records or total > limit_bytes):
            victims.append(kept.pop(0))
            total -= sizes.pop(0)
        return kept, victims

    # -- writing -------------------------------------------------------

    def publish(
        self,
        records,
        target=None,
        staging=None,
        retire=(),
        quarantine=(),
        extra=(),
        encode=None,
    ):
        """Publish one generation, keep the evidence, then retire the sources.

        The order is the guarantee, not an implementation detail:

        1. the replacement is written, flushed and fsynced to a staging path;
        2. it — and every already-staged ``extra`` target — is atomically
           renamed into place, and the directory is fsynced;
        3. torn suffixes found in the sources are quarantined and fsynced;
        4. only then are the sources retired.

        A crash anywhere in that sequence leaves every accepted record with at
        least one durable home, and leaves corrupt bytes readable for longer
        than the file they came from. Staging never becomes authority
        (:meth:`discard_staging`), so a crash during step 1 changes nothing.
        """
        target = self.path if target is None else target
        encode = self.encode if encode is None else encode
        pending = stage_lines(
            target if staging is None else staging,
            (encode(record) for record in records),
        )
        publish_staged([(pending, target)] + list(extra))
        for source, torn in quarantine:
            if torn:
                self.quarantine_tail(torn, source if self.torn_at_source else None)
        retire_files(retire)

    def handoff(self, generation=None):
        """Hand a non-empty active authority to an immutable generation.

        Returns the new generation path, or ``None`` when there was nothing to
        hand off. After the rename the active path is absent, so a concurrent
        appender opens a fresh authority instead of writing into a generation
        somebody is already draining.
        """
        try:
            if os.path.getsize(self.path) <= 0:
                return None
        except OSError:
            return None
        target = self.generation_path(generation or uuid.uuid4().hex)
        os.replace(self.path, target)
        fsync_parent(self.path)
        return target

    def retire(self, paths):
        return retire_files(paths)

    def discard_staging(self, target=None):
        """Discard an orphan staging file; it is never promoted.

        A syntactically valid prefix — including an empty file — says nothing
        about whether the writer completed the generation it intended. Only
        atomic replacement commits one.
        """
        pending = self.pending_path(target)
        if not os.path.exists(pending):
            return False
        try:
            os.unlink(pending)
            fsync_parent(pending)
        except OSError:
            return False
        return True

    def quarantine_tail(self, torn, source=None):
        """Durably retain a bounded forensic sample outside every authority."""
        path = torn_path(self.path if source is None else source)
        with open(path, "xb") as damaged:
            damaged.write(torn)
            damaged.flush()
            os.fsync(damaged.fileno())
        self._reclaim_quarantine()
        fsync_parent(self.path)
        return path

    def _reclaim_quarantine(self):
        """Keep the newest evidence within both caps, deterministically.

        Evidence torn from the active authority and from any generation draws
        on one shared allowance: corruption is forensic, not a second log.
        """
        candidates = glob.glob(self.path + TORN_PREFIX + "*") + glob.glob(
            self.path + self.generation_prefix + "*" + TORN_PREFIX + "*"
        )

        def recency(candidate):
            try:
                return (os.path.getmtime(candidate), candidate)
            except OSError:
                return (0.0, candidate)

        candidates.sort(key=recency, reverse=True)
        retained = 0
        for index, candidate in enumerate(candidates):
            try:
                size = os.path.getsize(candidate)
            except OSError:
                continue
            if index >= self.torn_files or retained + size > self.torn_bytes:
                try:
                    os.unlink(candidate)
                except OSError:
                    pass
            else:
                retained += size
'''

durable = _bundled_types.ModuleType("durable")
durable.__file__ = "durable.py"
exec(compile(_DURABLE_SOURCE, "durable.py", "exec"), durable.__dict__)
del _bundled_types
# --- end of hooks/durable.py ------------------------------------------------------


def _setting(name, default=None):
    """Read CHRONICLE_<name>, falling back to the pre-rename BURROW_<name>.

    Both spellings are live at once during the rollout: a hook's environment is
    fixed when its session starts, so sessions already running when this file is
    updated keep handing it BURROW_URL for as long as they last, while new ones
    get CHRONICLE_URL. Dropping the old spelling would make those sessions go
    quiet — events would still be logged locally, but the village would stop
    seeing them — which is exactly the silent failure the emitter exists to
    avoid.

    Presence, not truthiness, selects the spelling. CHRONICLE_MIRROR= means "no
    mirrors" and must not fall through to a leftover BURROW_MIRROR; the caller
    also relies on ``None`` meaning neither spelling was set at all, which is
    what turns the default mirror on.
    """
    key = "CHRONICLE_" + name
    value = os.environ[key] if key in os.environ else os.environ.get("BURROW_" + name)
    return default if value is None else value


def _state_dir():
    """~/.chronicle — the offline fallback log and the durable primary outbox.

    It used to be ~/.burrow, and until warren#361 an existing one was preferred so
    that a machine mid-rename would not strand a spool with undelivered events in
    it. That fallback is gone: every deployed container is now on an image that has
    only ever written ~/.chronicle, and the dev machines were renamed by hand with
    their spools drained. A machine that still has a ~/.burrow keeps it as a static
    archive — nothing appends to it, and `mv ~/.burrow ~/.chronicle` is the one
    operator step that adopts it.
    """
    return os.path.join(os.path.expanduser("~"), ".chronicle")


LOG_DIR = _state_dir()
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
ACK_RESERVE = 0.1
MAX_TARGETS = 8
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
STUCK_OUTBOX_HOOKS = 10
STUCK_OUTBOX_AGE_SECONDS = 24 * 60 * 60
_OUTBOX_LOCK = threading.Lock()
_DIAGNOSTIC_LOCK = threading.Lock()
# Persisted inside spool records on disk. Deliberately NOT renamed: a record
# written before an upgrade must still be replayable after it, and this name is
# part of that stored shape rather than part of the service's vocabulary.
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


def breaker_path(url, base=None):
    """One breaker per target: a village that is down must not silence the dev
    server running next to it (that pair is exactly the off-tailnet case)."""
    base = BREAKER if base is None else base
    return base + "-" + hashlib.sha1(url.encode("utf-8")).hexdigest()[:8]


def targets():
    """Where this event goes, in order, as (url, token) — CHRONICLE_URL first, then
    the mirrors. Both vars take a comma-separated list; duplicates collapse so a
    URL named twice never doubles the event."""
    out = []
    seen = set()
    mirror = _setting("MIRROR")
    groups = (
        (_setting("URL"), _setting("TOKEN")),
        (
            DEFAULT_MIRROR if mirror is None else mirror,
            _setting("MIRROR_TOKEN"),
        ),
    )
    for raw, token in groups:
        for url in (u.strip().rstrip("/") for u in (raw or "").split(",")):
            if url and url not in seen:
                seen.add(url)
                out.append((url, (token or "").strip()))
    return out


def post_event(
    url,
    event,
    token="",
    delivery_id="",
    *,
    timeout=None,
    breaker_base=None,
    log_dir=None,
    opener=None,
):
    breaker = breaker_path(url, breaker_base)
    timeout = POST_TIMEOUT if timeout is None else timeout
    log_dir = LOG_DIR if log_dir is None else log_dir
    opener = urllib.request.urlopen if opener is None else opener
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
        with opener(req, timeout=timeout):
            pass
        return True
    except Exception:
        try:
            os.makedirs(log_dir, exist_ok=True)
            with open(breaker, "w") as f:
                f.write(str(time.time()))
        except OSError:
            pass
        return False


_DEFAULT_POST_EVENT = post_event


def _target_groups(pending=()):
    configured = targets()
    primary_urls = {
        u.strip().rstrip("/")
        for u in (_setting("URL") or "").split(",")
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


def _enqueue_time(record):
    """UTC timestamp encoded by a modern enqueue order, or None for legacy records."""
    order = record.get("enqueue_order")
    try:
        return int(str(order).split(":", 1)[0]) / 1_000_000_000
    except (TypeError, ValueError, OverflowError):
        return None


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
        for url in (_setting("URL") or "").split(",")
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
    a replacement durable, quarantines any torn suffix it read, then retires
    its input journals, so every accepted event always has at least one durable
    home and corrupt bytes outlive the file that carried them.
    """
    records = _stamp_enqueue_order(records)  # allocation precedes lock contention
    spool = _outbox_spool()
    try:
        with spool.lock(".transaction", create=True):
            main = _read_active_outbox(spool)
            journals = spool.generation_paths()
            auxiliary = []
            quarantine = []
            for journal in journals:
                generation = spool.read(journal)
                if generation is not None:
                    # Evidence before erasure, as on the main path: the torn
                    # bytes outlive the journal they came from (spool.md, G3).
                    quarantine.append((journal, generation.torn))
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
                quarantine=quarantine,
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

    The schedule is staged in the same transaction, so its orphan is swept
    here too rather than being left for the next writer to overwrite.
    """
    spool = _outbox_spool()
    spool.discard_staging()
    spool.discard_staging(_schedule_path())


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


def _diagnose(kind, *, diagnostics=None, **details):
    """Persist counters and a bounded, payload-free recent diagnostic list."""
    diagnostics = DIAGNOSTICS if diagnostics is None else diagnostics
    with _DIAGNOSTIC_LOCK:
        try:
            os.makedirs(os.path.dirname(os.path.abspath(diagnostics)), exist_ok=True)
            with open(durable.lock_path(diagnostics), "a+") as lock:
                # Helper-side persistence may wait: the parent enforces the
                # aggregate one-second budget and kills a stalled helper.
                fcntl.flock(lock, fcntl.LOCK_EX)
                try:
                    with open(diagnostics, encoding="utf-8") as stream:
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
                if kind == "outbox":
                    previous = report.get("outbox")
                    previous = previous if isinstance(previous, dict) else {}
                    acknowledged = int(details.pop("acknowledged", 0))
                    hooks_without_ack = (
                        0
                        if acknowledged
                        else int(previous.get("hooks_without_ack", 0)) + 1
                    )
                    details["hooks_without_ack"] = hooks_without_ack
                    details["last_ack_at"] = (
                        report["updated_at"]
                        if acknowledged
                        else previous.get("last_ack_at")
                    )
                    details["status"] = (
                        "stuck"
                        if (
                            details.get("records", 0) >= details.get("capacity", 1)
                            or details.get("oldest_age_seconds", 0)
                            >= STUCK_OUTBOX_AGE_SECONDS
                        )
                        and hooks_without_ack >= STUCK_OUTBOX_HOOKS
                        else "healthy"
                    )
                    report["outbox"] = details
                    if details["status"] == "stuck" and previous.get("status") != "stuck":
                        report.setdefault("recent", []).append(
                            {
                                "kind": "stuck_outbox",
                                "reason": "old or full without acknowledgements",
                            }
                        )
                else:
                    report.setdefault("recent", []).append(dict(kind=kind, **details))
                report["recent"] = report["recent"][-DIAGNOSTIC_HISTORY:]
                pending = durable.stage_json(diagnostics, report, ensure_ascii=False)
                durable.publish_staged(((pending, diagnostics),))
                return True
        except (OSError, ValueError, TypeError):
            return False


_DEFAULT_DIAGNOSE = _diagnose


def _diagnose_outbox(records, acknowledged):
    """Publish bounded queue health without exposing event payloads."""
    oldest_at = None
    oldest_age = 0
    if records:
        created = _enqueue_time(records[0])
        if created is not None:
            oldest_age = max(0, int(time.time() - created))
            oldest_at = (
                datetime.datetime.fromtimestamp(created, datetime.timezone.utc)
                .isoformat(timespec="milliseconds")
                .replace("+00:00", "Z")
            )
    _diagnose(
        "outbox",
        records=len(records),
        capacity=OUTBOX_RECORDS,
        oldest_queued_at=oldest_at,
        oldest_age_seconds=oldest_age,
        acknowledged=len(acknowledged),
    )


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
    caller would turn tidying into event loss. One at a time, so a failure on
    one path does not strand the ones behind it.
    """
    for path in paths:
        try:
            spool.retire((path,))
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


def deliver(event, deadline=None):
    """Deliver one redacted event under one bounded hook budget.

    Primary queues replay oldest-first; mirrors see only the current event and
    can never acknowledge a primary. Independent targets receive one daemon
    worker each, capped by ``MAX_TARGETS``.
    """
    event = redact_event(event)
    deadline = deadline if deadline is not None else time.monotonic() + HOOK_BUDGET
    post_deadline = deadline - min(ACK_RESERVE, HOOK_BUDGET)
    current_id = uuid.uuid4().hex
    initial_primary, initial_mirrors, later_primary, later_mirrors = _target_groups()
    configured_primary = initial_primary + later_primary
    configured_mirrors = initial_mirrors + later_mirrors
    local_written = False
    transport = post_event
    transport_timeout = POST_TIMEOUT
    transport_breaker = BREAKER
    transport_log_dir = LOG_DIR
    transport_opener = urllib.request.urlopen
    diagnostic = _diagnose
    diagnostic_path = DIAGNOSTICS

    def post(url, posted_event, token, delivery_id):
        if transport is not _DEFAULT_POST_EVENT:
            return transport(url, posted_event, token, delivery_id)
        return transport(
            url,
            posted_event,
            token,
            delivery_id,
            timeout=transport_timeout,
            breaker_base=transport_breaker,
            log_dir=transport_log_dir,
            opener=transport_opener,
        )

    def diagnose(kind, **details):
        if diagnostic is not _DEFAULT_DIAGNOSE:
            return diagnostic(kind, **details)
        return diagnostic(kind, diagnostics=diagnostic_path, **details)

    if not configured_primary:
        _append_local(event)
        local_written = True
        mirrors = configured_mirrors[:MAX_TARGETS]
        deferred_mirrors = configured_mirrors[MAX_TARGETS:]
        results = []
        result_lock = threading.Lock()

        def mirror_worker(url, token):
            delivered = post(url, event, token, "")
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
    result_lock = threading.Lock()
    initial_rtt = transport_timeout

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
        estimated_rtt = initial_rtt
        for record in queued:
            if time.monotonic() + estimated_rtt > post_deadline:
                return
            started = time.monotonic()
            if not post(
                url,
                record.get("event") or {},
                token,
                str(record.get("delivery_id") or ""),
            ):
                with result_lock:
                    results[url] = (list(delivered_keys), False, target_key)
                return
            elapsed = time.monotonic() - started
            estimated_rtt = max(elapsed * 1.25, 0.001)
            delivered_keys.append(_record_key(record))
            with result_lock:
                results[url] = (
                    list(delivered_keys),
                    len(delivered_keys) == len(queued),
                    target_key,
                )
            diagnose("retry", target=target_key)
        with result_lock:
            results[url] = (
                list(delivered_keys),
                len(delivered_keys) == len(queued),
                target_key,
            )

    workers = []
    for url, token in primary:
        worker = threading.Thread(target=primary_worker, args=(url, token), daemon=True)
        worker.start()
        workers.append(worker)
    for url, token in mirrors:
        worker = threading.Thread(
            target=post, args=(url, event, token, ""), daemon=True
        )
        worker.start()
        workers.append(worker)
    for worker in workers:
        worker.join(max(0, post_deadline - time.monotonic()))

    # A worker can receive the HTTP response just before ``post_deadline`` yet
    # publish its result just after it because the process was descheduled. Keep
    # harvesting successes during the reserved acknowledgement tail and retire
    # them durably as soon as they become visible. A single snapshot here loses
    # accepted deliveries precisely when the host is busiest.
    acknowledged_keys = set()
    update_attempted = False

    def observed_keys():
        observed = set()
        for target_url, _ in primary:
            target_key = _target_id(target_url)
            with result_lock:
                replayed, _, _ = results.get(
                    target_url, ([], False, target_key)
                )
            observed.update(replayed)
        return observed

    def acknowledge_observed():
        nonlocal update_attempted
        pending_ack = observed_keys() - acknowledged_keys
        if not pending_ack:
            return
        update_attempted = True
        _, updated = _update_outbox(pending_ack, [])
        if updated:
            acknowledged_keys.update(pending_ack)

    while True:
        acknowledge_observed()
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            # One last snapshot closes the publish-before-deadline race. The
            # outer hook process remains the hard bound if this durable write
            # itself stalls below Python.
            acknowledge_observed()
            break
        if not any(worker.is_alive() for worker in workers):
            # A worker can publish and exit between the first snapshot and the
            # liveness check. Once it is dead, this snapshot is authoritative.
            acknowledge_observed()
            break
        for worker in workers:
            if worker.is_alive():
                worker.join(min(0.005, remaining))
                break
    if not update_attempted:
        _, updated = _update_outbox(set(), [])
        update_attempted = updated

    primary_failed = False
    failed_targets = []
    for url, _ in primary:
        target_key = _target_id(url)
        with result_lock:
            replayed, current_ok, _ = results.get(url, ([], False, target_key))
        if not current_ok:
            primary_failed = True
            failed_targets.append(target_key)
    if deferred_primary:
        primary_failed = True
    # Diagnostics are deliberately after acknowledgement: their lock or fsync
    # may stall, but a host timeout must never erase accepted progress.
    unacknowledged_keys = observed_keys() - acknowledged_keys
    if unacknowledged_keys or not update_attempted:
        _diagnose("failure", reason="outbox lock contention")
    else:
        _diagnose_outbox(_read_durable_outbox_snapshot(), acknowledged_keys)
    for target_key in failed_targets:
        _diagnose("failure", target=target_key)
    if deferred_primary or deferred_mirrors:
        _diagnose(
            "failure",
            reason="target limit",
            count=len(deferred_primary) + len(deferred_mirrors),
        )
    if primary_failed and not local_written and time.monotonic() < deadline:
        _append_local(event)


def detail_policy():
    value = _setting("DETAIL", "full").strip().lower()
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


def main(runner="claude", deadline=None):
    hook = json.loads(sys.stdin.read())
    specs = adapt_hook(runner, hook)
    if not specs:
        return
    resident_id = _setting("AGENT_ID")
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
        event = {
            "v": 0,
            "ts": now.isoformat(timespec="milliseconds").replace("+00:00", "Z"),
            "source": RUNNER_SOURCES[runner],
            "agent_id": agent_id,
            "project": _setting("PROJECT")
            or (_safe_path(cwd) if cwd else "unknown"),
            "cwd": cwd,
            "type": etype,
            "payload": payload,
        }
        if deadline is None:
            deliver(event)
        else:
            deliver(event, deadline)


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
        sys.stderr.write("chronicle transport failure: ProcessStartError\n")
        return
    if pid == 0:
        try:
            main(runner, work_deadline)
        except Exception as error:
            sys.stderr.write(
                "chronicle transport failure: " + type(error).__name__[:80] + "\n"
            )
        finally:
            os._exit(0)
    while time.monotonic() < work_deadline:
        waited, _ = os.waitpid(pid, os.WNOHANG)
        if waited == pid:
            return
        time.sleep(min(0.005, max(0, work_deadline - time.monotonic())))
    sys.stderr.write("chronicle transport timeout\n")
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


def print_emitter_status():
    """Render local outbox health for an operator; hook execution never calls this."""
    try:
        with open(DIAGNOSTICS, encoding="utf-8") as stream:
            outbox = json.load(stream).get("outbox")
        if not isinstance(outbox, dict):
            raise ValueError("missing outbox status")
    except (OSError, ValueError, TypeError):
        print("chronicle emitter outbox: no delivery status recorded")
        return
    oldest = outbox.get("oldest_queued_at") or "none"
    last_ack = outbox.get("last_ack_at") or "never"
    print(
        "chronicle emitter outbox: %s; %s/%s queued; oldest %s (%ss); "
        "%s hooks without ack; last ack %s"
        % (
            outbox.get("status", "unknown"),
            outbox.get("records", "?"),
            outbox.get("capacity", "?"),
            oldest,
            outbox.get("oldest_age_seconds", "?"),
            outbox.get("hooks_without_ack", "?"),
            last_ack,
        )
    )


if __name__ == "__main__":
    if sys.argv[1:] == ["--status"]:
        print_emitter_status()
    else:
        runner = runner_name(sys.argv[1:])
        if runner:
            try:
                run_hook_bounded(runner)
            except Exception as error:
                # Last-resort diagnostic when even the durable state directory is
                # unavailable. Never echo exception text: it may contain a path,
                # URL, credential, or event detail.
                sys.stderr.write(
                    "chronicle transport failure: " + type(error).__name__[:80] + "\n"
                )
        if runner == "codex":
            # Stop/SubagentStop require JSON on stdout; an empty object is advisory
            # and deliberately never approves, denies, blocks, or continues Codex.
            print("{}")
    sys.exit(0)
