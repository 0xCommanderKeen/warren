"""Crash-safe persistence for server notification delivery.

The HTTP server owns scheduling and webhook policy.  This module owns the complete
durable lifecycle: stable lock names, bounded terminal ledgers, journal authority,
attempt state, recovery handoff, and replay retirement.
"""

import collections
import hashlib
import json
import math
import os
import struct
import threading

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

    def journal_spool(self, path=None):
        """The knock journal as one bounded generational log.

        A blank line is absent rather than damage, and every read tolerates a
        torn line by skipping it and marking the generation incomplete — which
        is what keeps that generation out of ``retire_replay_if_terminal``.
        """
        return durable.Spool(
            path or self.journal_path(),
            self._limits,
            decode=durable.json_entry,
            encode=durable.encode_compact_json,
            key=lambda entry: knock_key(entry.get("event", entry)),
        )

    def ledger_spool(self, kind):
        """One terminal ledger: opaque keys, one generation, no replay."""
        return durable.Spool(
            self.ledger_path(kind),
            self._ledger_limits,
            decode=durable.decode_text,
            encode=durable.encode_text,
            key=lambda item: item,
        )

    @staticmethod
    def journal_paths(path):
        return durable.Spool(path).generations()

    def load_ledger(self, kind):
        cache = self._caches[kind]
        spool = self.ledger_spool(kind)
        remembered = set()
        try:
            with self._ledger_lock, spool.lock(exclusive=False) as held:
                generation = spool.read() if held is not None else None
                if generation is not None:
                    remembered.update(generation.records)
        except OSError:
            pass
        cache[spool.path] = remembered
        return remembered

    def remember_batch(self, kind, keys, preserve_existing=()):
        """Remember terminal keys, refusing rather than dropping under pressure.

        This is the one site that inverts the usual capacity rule. Everywhere
        else a capacity victim is durably reported and then dropped; here a
        terminal outcome that will not fit is a refusal, because forgetting one
        would let an already-answered notification knock again forever. The
        ledger is left byte-identical when that happens.

        Re-remembering a key promotes it to newest: eviction order *is* the
        retention policy, so this spool is LRU where the others are FIFO.
        """
        cache = self._caches[kind]
        spool = self.ledger_spool(kind)
        with self._ledger_lock, spool.lock(create=True):
            generation = spool.read()
            remembered = collections.OrderedDict(
                (key, None) for key in (generation.records if generation else ())
            )
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
                cache[spool.path] = set(remembered)
                return cache[spool.path]
            kept, _ = spool.bound(list(remembered))
            retained = set(kept)
            if not (requested | required) <= retained:
                cache[spool.path] = prior
                raise OSError("terminal batch exceeds durable ledger capacity")
            spool.publish(kept)
            cache[spool.path] = retained
            return retained

    def remember(self, kind, key):
        self.remember_batch(kind, (key,))

    def contains(self, kind, key):
        try:
            spool = self.ledger_spool(kind)
            with self._ledger_lock, spool.lock(exclusive=False) as held:
                if held is None:
                    return False
                generation = spool.read()
                return generation is not None and key in generation.records
        except OSError:
            return False

    def read_journal_keys(self, path=None):
        spool = self.journal_spool(path)
        return {
            spool.key(entry)
            for generation in spool.snapshot(damage=durable.SKIP_DAMAGE)
            for entry in generation.records
        }

    def compact_locked(self, path=None, addition=None):
        spool = self.journal_spool(path)
        terminal = self.load_ledger(NOTIFIED) | self.load_ledger(DROPPED)
        self.prune_terminal_generations(spool.path, terminal)

        live = [
            entry
            for generation in spool.snapshot(damage=durable.SKIP_DAMAGE)
            for entry in generation.records
            if not is_terminal(entry.get("event", entry), terminal)
        ]
        if addition is not None and not is_terminal(
            addition.get("event", addition), terminal
        ):
            live.append(addition)
        latest = spool.dedupe(live)
        kept, evicted = spool.bound(latest)

        victims = []
        for entry in evicted:
            victim = terminal_key(entry.get("event", entry))
            if victim not in victims:
                victims.append(victim)
        retained = [terminal_key(entry.get("event", entry)) for entry in kept]
        # A victim is fsynced into the drop ledger before the compaction that
        # omits it is published, so a restart can never resurrect it.
        durable_drops = self.remember_batch(
            DROPPED, victims, preserve_existing=retained
        )
        if not set(victims) <= durable_drops:
            raise OSError("knock victims exceed durable terminal capacity")
        lines = [(spool.key(entry), entry) for entry in kept]
        self.publish_compaction(spool.path, lines)
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

    def publish_generation_prune(self, path, generation, lines):
        """Rewrite one generation in place, without folding the others in.

        ``recover`` accounts for completeness per generation, so pruning must
        not combine them. The already-encoded lines are passed through, and
        staging uses one stable name per target chosen to sit outside the
        replay glob so a pending prune is never mistaken for a generation.
        """
        identity = hashlib.sha256(os.path.abspath(generation).encode()).hexdigest()
        self.journal_spool(path).publish(
            lines,
            target=generation,
            staging=path + ".prune-" + identity,
            encode=durable.encode_raw,
        )

    def publish_compaction(self, path, lines):
        """Publish one journal generation, then retire every replay it absorbed."""
        spool = self.journal_spool(path)
        spool.publish(
            [entry for _, entry in lines], retire=spool.generation_paths()
        )

    def commit_terminal(self, event, kind):
        if kind not in (NOTIFIED, DROPPED):
            raise ValueError("invalid knock terminal ledger")
        spool = self.journal_spool()
        key = terminal_key(event)
        try:
            with self._journal_lock, spool.lock(create=True):
                # Already-terminal sources are compacted away before the new
                # outcome can evict their suppression, and again afterwards so
                # the source of this outcome is retired under the same lock.
                self.compact_locked(spool.path)
                self.remember(kind, key)
                self.compact_locked(spool.path)
            return True
        except OSError:
            return self.contains(kind, key)

    def journal(self, event):
        spool = self.journal_spool()
        try:
            with self._journal_lock, spool.lock(create=True):
                known = self.read_journal_keys(spool.path)
                if knock_key(event) not in known:
                    known = self.compact_locked(
                        spool.path, {"event": event, "attempts": 0}
                    )
                self._caches[KNOCKS][spool.path] = known
            return True
        except OSError:
            return False

    def record_attempt(self, event, attempts):
        spool = self.journal_spool()
        try:
            with self._journal_lock, spool.lock(create=True):
                self.compact_locked(
                    spool.path, {"event": event, "attempts": attempts}
                )
            return True
        except OSError:
            return False

    def recover(self):
        """Hand off journal authority and return parsed replay generations.

        Collapsing a surviving generation before handing off another is what
        bounds the physical footprint: without it every recovery would add a
        generation to the ones a previous crash left behind.
        """
        spool = self.journal_spool()
        try:
            with self._journal_lock, spool.lock(create=True):
                generations = spool.generation_paths()
                try:
                    has_active = os.path.getsize(spool.path) > 0
                except OSError:
                    has_active = False
                if (generations and has_active) or len(generations) > 1:
                    self.compact_locked(spool.path)
                spool.handoff()
                generations = spool.generation_paths()
        except OSError:
            return []
        recovered = []
        for path in generations:
            parsed = spool.read(path, damage=durable.SKIP_DAMAGE)
            if parsed is None:
                continue
            events = collections.OrderedDict()
            for entry in parsed.records:
                journaled = isinstance(entry.get("event"), dict)
                attempts = entry.get("attempts", 0) if journaled else 0
                event = dict(entry["event"]) if journaled else entry
                if type(attempts) is not int or attempts < 0:
                    attempts = 0
                key = knock_key(event)
                if key not in events or attempts >= events[key][0]:
                    events[key] = (attempts, event)
            with self._attempts_lock:
                for attempts, event in events.values():
                    key = terminal_key(event)
                    self._attempts[key] = max(self._attempts.get(key, 0), attempts)
            recovered.append(
                (path, parsed.complete, [item[1] for item in events.values()])
            )
        return recovered

    def retire_replay_if_terminal(self, generation, complete, events):
        """Retire a drained generation only once nothing in it can be lost.

        An incomplete generation is never retired: a torn line may hide a knock
        this process never saw, so the bytes stay until a compaction folds them
        back in. That is this site's answer to torn-tail quarantine.
        """
        if not complete:
            return False
        terminal = self.load_ledger(NOTIFIED) | self.load_ledger(DROPPED)
        if not all(is_terminal(event, terminal) for event in events):
            return False
        self.journal_spool().retire((generation,))
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
