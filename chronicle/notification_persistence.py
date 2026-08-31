"""Crash-safe persistence for server notification delivery.

The HTTP server owns scheduling and webhook policy.  This module owns the complete
durable lifecycle: stable lock names, bounded terminal ledgers, journal authority,
attempt state, recovery handoff, and replay retirement.
"""

import collections
import fcntl
import hashlib
import json
import math
import os
import struct
import threading
import uuid

from approval_protocol import structured_approval
from hooks import durable


DELIVERY_IDS = "delivery-ids"
KNOCKS = "knocks"
NOTIFIED = "notified"
DROPPED = "notify-dropped"
KINDS = frozenset((DELIVERY_IDS, KNOCKS, NOTIFIED, DROPPED))


def legacy_knock_key(event):
    """Pre-structured-request fallback identity, retained for ledger migration."""
    delivery_id = event.get("delivery_id")
    if isinstance(delivery_id, str) and delivery_id:
        return "delivery:" + delivery_id
    payload = event.get("payload") or {}
    return "\x00".join(
        str(value)
        for value in (
            event.get("agent_id"),
            event.get("ts"),
            payload.get("message") if isinstance(payload, dict) else "",
        )
    )


def _canonical_json_bytes(value):
    """Language-neutral JSON-semantic encoding used only as hash input.

    Strings use their UTF-16 code units, object keys use the same ordering as
    JavaScript's default string sort, and numbers use their IEEE-754 binary64
    value (with both spellings of zero unified). Thus key order and ``1`` versus
    ``1.0`` do not change identity, while array order and exact string whitespace
    do. Length prefixes make the stream unambiguous without retaining secrets.
    """
    if value is None:
        return b"n"
    if type(value) is bool:
        return b"b1" if value else b"b0"
    if type(value) in (int, float):
        try:
            number = float(value)
        except (OverflowError, ValueError):
            return b"x"
        if not math.isfinite(number):
            return b"x"
        if number == 0:
            number = 0.0
        return b"d" + struct.pack(">d", number)
    if isinstance(value, str):
        encoded = value.encode("utf-16-be", "surrogatepass")
        return b"s" + len(encoded).to_bytes(8, "big") + encoded
    if isinstance(value, list):
        values = [_canonical_json_bytes(item) for item in value]
        return b"a" + len(values).to_bytes(8, "big") + b"".join(values)
    if isinstance(value, dict):
        items = sorted(
            value.items(),
            key=lambda item: str(item[0]).encode("utf-16-be", "surrogatepass"),
        )
        encoded = [
            (_canonical_json_bytes(str(key)), _canonical_json_bytes(item))
            for key, item in items
        ]
        return (
            b"o"
            + len(encoded).to_bytes(8, "big")
            + b"".join(key + item for key, item in encoded)
        )
    return b"x"


def knock_key(event):
    """Stable notification identity.

    Producer ``delivery_id`` remains authoritative. Without one, valid structured
    requests use a v3 hash of their complete immutable shape and event identity.
    The hash is stable across JSON key/number spelling and never persists detail
    values or other possibly sensitive fields in a key.
    """
    delivery_id = event.get("delivery_id")
    if isinstance(delivery_id, str) and delivery_id:
        return "delivery:" + delivery_id
    approval = structured_approval(event)
    shape = approval.notification_shape() if approval is not None else None
    if shape is not None:
        identity = {
            "version": 3,
            "event_version": event.get("v"),
            "type": event.get("type"),
            "ts": event.get("ts"),
            "source": event.get("source"),
            "agent_id": event.get("agent_id"),
            "project": event.get("project"),
            "approval": shape,
        }
        digest = hashlib.sha256(_canonical_json_bytes(identity)).hexdigest()
        return "structured-v3-sha256-" + digest
    return legacy_knock_key(event)


def terminal_key(event):
    return (
        "burrow-sha256-"
        + hashlib.sha256(knock_key(event).encode("utf-8", "surrogatepass")).hexdigest()
    )


def terminal_keys(event):
    """Return only aliases that prove the same notification identity.

    Old structured v1/v2 ledgers lack the immutable shape needed to prove an
    alias. Trusting them can let a plain or differently shaped event suppress a
    real approval forever, so an upgrade may re-notify one old structured event
    once. Plain events and producer delivery IDs retain their exact old keys.
    """
    return (terminal_key(event),)


def is_terminal(event, terminal):
    return any(key in terminal for key in terminal_keys(event))


class NotificationPersistence:
    """One cohesive durable authority, configured by live server settings."""

    def __init__(self, events_path, limits, lock_shards=32, ledger_limits=None):
        self._events_path = events_path
        self._limits = limits
        self._ledger_limits = ledger_limits or limits
        self.lock_shards = lock_shards
        self._journal_lock = threading.RLock()
        self._ledger_lock = threading.RLock()
        self._attempts = {}
        self._attempts_lock = threading.Lock()
        self._caches = {kind: {} for kind in KINDS}

    def ledger_path(self, kind):
        if kind not in KINDS:
            raise ValueError("invalid durable ledger kind: %r" % (kind,))
        return os.path.abspath(self._events_path()) + "." + kind

    def notification_lock_path(self, shard):
        if not 0 <= shard < self.lock_shards:
            raise ValueError("invalid notification lock shard: %r" % (shard,))
        return os.path.abspath(self._events_path()) + ".notify-lock-%02d" % shard

    def delivery_lock_path(self, key):
        shard = int.from_bytes(key.encode("utf-8")[:64], "little") % self.lock_shards
        return self.notification_lock_path(shard)

    def journal_path(self):
        return self.ledger_path(KNOCKS)

    @staticmethod
    def journal_paths(path):
        return [path] + durable.replay_paths(path)

    def load_ledger(self, kind):
        cache = self._caches[kind]
        path = self.ledger_path(kind)
        remembered = set()
        try:
            with self._ledger_lock, open(durable.lock_path(path), "a+") as lock:
                fcntl.flock(lock, fcntl.LOCK_SH)
                try:
                    with open(path, encoding="utf-8") as stream:
                        remembered.update(
                            line.rstrip("\n") for line in stream if line.strip()
                        )
                except OSError:
                    pass
        except OSError:
            pass
        cache[path] = remembered
        return remembered

    def remember_batch(self, kind, keys, preserve_existing=()):
        cache = self._caches[kind]
        path = self.ledger_path(kind)
        records, byte_limit = self._ledger_limits()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with self._ledger_lock, open(durable.lock_path(path), "a+") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            remembered = collections.OrderedDict()
            try:
                with open(path, encoding="utf-8") as stream:
                    for line in stream:
                        prior_key = line.rstrip("\n")
                        if prior_key:
                            remembered[prior_key] = None
            except OSError:
                pass
            prior = set(remembered)
            required = prior.intersection(preserve_existing)
            requested = set()
            changed = False
            for key in keys:
                requested.add(key)
                if key in remembered:
                    if next(reversed(remembered)) == key:
                        continue
                    remembered.pop(key)
                remembered[key] = None
                changed = True
            if not changed:
                cache[path] = set(remembered)
                return cache[path]
            lines = [(item, item + "\n") for item in remembered]
            total = sum(len(line.encode("utf-8")) for _, line in lines)
            while lines and (len(lines) > records or total > byte_limit):
                _, line = lines.pop(0)
                total -= len(line.encode("utf-8"))
            retained = {item for item, _ in lines}
            if not (requested | required) <= retained:
                cache[path] = prior
                raise OSError("terminal batch exceeds durable ledger capacity")
            durable.publish_lines(path, (line for _, line in lines))
            cache[path] = retained
            return retained

    def remember(self, kind, key):
        self.remember_batch(kind, (key,))

    def contains(self, kind, key):
        try:
            path = self.ledger_path(kind)
            with self._ledger_lock, open(durable.lock_path(path), "a+") as lock:
                fcntl.flock(lock, fcntl.LOCK_SH)
                try:
                    with open(path, encoding="utf-8") as stream:
                        return any(line.rstrip("\n") == key for line in stream)
                except OSError:
                    return False
        except OSError:
            return False

    def read_journal_keys(self, path=None):
        path = path or self.journal_path()
        known = set()
        for generation in self.journal_paths(path):
            try:
                with open(generation, encoding="utf-8") as stream:
                    for line in stream:
                        try:
                            prior = json.loads(line)
                        except ValueError:
                            continue
                        if isinstance(prior, dict):
                            event = prior.get("event", prior)
                            if isinstance(event, dict):
                                known.add(knock_key(event))
            except OSError:
                continue
        return known

    def compact_locked(self, path=None, addition=None):
        path = path or self.journal_path()
        terminal = self.load_ledger(NOTIFIED) | self.load_ledger(DROPPED)
        self.prune_terminal_generations(path, terminal)

        latest = collections.OrderedDict()
        for generation in self.journal_paths(path):
            try:
                with open(generation, encoding="utf-8") as stream:
                    for line in stream:
                        try:
                            entry = json.loads(line)
                        except ValueError:
                            continue
                        event = (
                            entry.get("event", entry)
                            if isinstance(entry, dict)
                            else None
                        )
                        if isinstance(event, dict):
                            if not is_terminal(event, terminal):
                                latest[knock_key(event)] = entry
            except OSError:
                pass
        if addition is not None:
            event = addition.get("event", addition)
            if not is_terminal(event, terminal):
                latest[knock_key(event)] = addition
        lines = [
            (key, json.dumps(entry, ensure_ascii=False, separators=(",", ":")) + "\n")
            for key, entry in latest.items()
        ]
        records, byte_limit = self._limits()
        total = sum(len(line.encode("utf-8")) for _, line in lines)
        victims = []
        while lines and (len(lines) > records or total > byte_limit):
            key, line = lines.pop(0)
            total -= len(line.encode("utf-8"))
            victim = terminal_key(latest[key].get("event", latest[key]))
            if victim not in victims:
                victims.append(victim)
        retained = [
            terminal_key(latest[key].get("event", latest[key])) for key, _ in lines
        ]
        durable_drops = self.remember_batch(
            DROPPED, victims, preserve_existing=retained
        )
        if not set(victims) <= durable_drops:
            raise OSError("knock victims exceed durable terminal capacity")
        self.publish_compaction(path, lines)
        return {key for key, _ in lines}

    def prune_terminal_generations(self, path, terminal):
        """Remove terminal sources without combining journal generations."""
        for generation in NotificationPersistence.journal_paths(path):
            try:
                with open(generation, encoding="utf-8", newline="") as stream:
                    original = stream.readlines()
            except FileNotFoundError:
                continue
            retained = []
            changed = False
            for line in original:
                try:
                    entry = json.loads(line)
                except ValueError:
                    retained.append(line)
                    continue
                event = entry.get("event", entry) if isinstance(entry, dict) else None
                if isinstance(event, dict) and is_terminal(event, terminal):
                    changed = True
                else:
                    retained.append(line)
            if changed:
                self.publish_generation_prune(path, generation, retained)

    @staticmethod
    def publish_generation_prune(path, generation, lines):
        identity = hashlib.sha256(os.path.abspath(generation).encode()).hexdigest()
        # Keep staging outside the replay glob, with one stable name per target.
        pending = durable.stage_lines(path + ".prune-" + identity, lines)
        durable.publish_staged(((pending, generation),))

    @staticmethod
    def publish_compaction(path, lines):
        durable.publish_lines(
            path, (line for _, line in lines), retire=durable.replay_paths(path)
        )

    def commit_terminal(self, event, kind):
        if kind not in (NOTIFIED, DROPPED):
            raise ValueError("invalid knock terminal ledger")
        path = self.journal_path()
        key = terminal_key(event)
        try:
            with self._journal_lock:
                os.makedirs(os.path.dirname(path), exist_ok=True)
                with open(durable.lock_path(path), "a+") as lock:
                    fcntl.flock(lock, fcntl.LOCK_EX)
                    self.compact_locked(path)
                    self.remember(kind, key)
                    self.compact_locked(path)
            return True
        except OSError:
            return self.contains(kind, key)

    def journal(self, event):
        path = self.journal_path()
        try:
            with self._journal_lock:
                os.makedirs(os.path.dirname(path), exist_ok=True)
                with open(durable.lock_path(path), "a+") as lock:
                    fcntl.flock(lock, fcntl.LOCK_EX)
                    known = self.read_journal_keys(path)
                    if knock_key(event) not in known:
                        known = self.compact_locked(
                            path, {"event": event, "attempts": 0}
                        )
                    self._caches[KNOCKS][path] = known
            return True
        except OSError:
            return False

    def record_attempt(self, event, attempts):
        path = self.journal_path()
        try:
            with self._journal_lock:
                os.makedirs(os.path.dirname(path), exist_ok=True)
                with open(durable.lock_path(path), "a+") as lock:
                    fcntl.flock(lock, fcntl.LOCK_EX)
                    self.compact_locked(path, {"event": event, "attempts": attempts})
            return True
        except OSError:
            return False

    def recover(self):
        """Hand off journal authority and return parsed replay generations."""
        path = self.journal_path()
        try:
            with self._journal_lock:
                os.makedirs(os.path.dirname(path), exist_ok=True)
                with open(durable.lock_path(path), "a+") as lock:
                    fcntl.flock(lock, fcntl.LOCK_EX)
                    try:
                        has_active = os.path.getsize(path) > 0
                    except OSError:
                        has_active = False
                    generations = durable.replay_paths(path)
                    if (generations and has_active) or len(generations) > 1:
                        self.compact_locked(path)
                        has_active = os.path.getsize(path) > 0
                    if has_active:
                        os.replace(path, durable.replay_path(path, uuid.uuid4().hex))
                        durable.fsync_parent(path)
                    generations = durable.replay_paths(path)
        except OSError:
            return []
        recovered = []
        for generation in generations:
            complete = True
            events = collections.OrderedDict()
            try:
                with open(generation, encoding="utf-8") as stream:
                    for line in stream:
                        if not line.strip():
                            continue
                        try:
                            entry = json.loads(line)
                        except ValueError:
                            complete = False
                            continue
                        if not isinstance(entry, dict):
                            complete = False
                            continue
                        attempts = (
                            entry.get("attempts", 0)
                            if isinstance(entry.get("event"), dict)
                            else 0
                        )
                        event = (
                            dict(entry["event"])
                            if isinstance(entry.get("event"), dict)
                            else entry
                        )
                        if type(attempts) is not int or attempts < 0:
                            attempts = 0
                        key = knock_key(event)
                        if key not in events or attempts >= events[key][0]:
                            events[key] = (attempts, event)
            except OSError:
                continue
            with self._attempts_lock:
                for attempts, event in events.values():
                    key = terminal_key(event)
                    self._attempts[key] = max(self._attempts.get(key, 0), attempts)
            recovered.append(
                (generation, complete, [item[1] for item in events.values()])
            )
        return recovered

    def retire_replay_if_terminal(self, generation, complete, events):
        if not complete:
            return False
        terminal = self.load_ledger(NOTIFIED) | self.load_ledger(DROPPED)
        if not all(is_terminal(event, terminal) for event in events):
            return False
        durable.retire_files((generation,))
        return True

    def terminal_counts(self):
        return len(self.load_ledger(NOTIFIED)), len(self.load_ledger(DROPPED))

    def next_attempt(self, event):
        """Advance and return this notification's process-local attempt count."""
        key = terminal_key(event)
        with self._attempts_lock:
            attempts = self._attempts.get(key, 0) + 1
            self._attempts[key] = attempts
            return attempts

    def attempts_exhausted(self, event, limit=3):
        """Return whether retry policy has reached its terminal attempt limit."""
        key = terminal_key(event)
        with self._attempts_lock:
            return self._attempts.get(key, 0) >= limit

    def clear_attempts(self, event):
        """Forget process-local retry state after a terminal outcome."""
        key = terminal_key(event)
        with self._attempts_lock:
            self._attempts.pop(key, None)

    def reset_process_state(self):
        """Discard non-authoritative caches and attempts, as on process restart."""
        with self._attempts_lock:
            self._attempts.clear()
        with self._ledger_lock:
            for cache in self._caches.values():
                cache.clear()
