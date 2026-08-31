"""Crash-safe durable storage shared by hook and server.

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
    """Quarantine name whose lexical order is also its creation order."""
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


def json_mapping(line):
    """One JSON object per line, where a decoded non-object is simply absent."""
    record = json.loads(line)
    return record if isinstance(record, dict) else None


def encode_json(record):
    return json.dumps(record, ensure_ascii=False) + "\n"


def encode_compact_json(record):
    return json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n"


def decode_text(line):
    """One opaque key per line, where a blank line is absent."""
    return line.rstrip("\n") or None


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

    def snapshot(self, damage=STOP_AT_DAMAGE, active_damage=None):
        """Every readable generation, active first.

        ``active_damage`` exists because the active authority and its
        auxiliary generations can warrant different tolerance: an authority
        published by this spool is trusted to be whole, while a generation
        inherited from a crashed writer is not.
        """
        found = []
        for path in self.generations():
            policy = damage
            if path == self.path and active_damage is not None:
                policy = active_damage
            generation = self.read(path, policy)
            if generation is not None:
                found.append(generation)
        return found

    def collect(self, damage=STOP_AT_DAMAGE, active_damage=None):
        """Deduped, ordered records across every readable generation."""
        records = []
        for generation in self.snapshot(damage, active_damage):
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
                self.quarantine_tail(torn, source)
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
