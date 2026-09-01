"""Durable append-only event log, rotation, and resumable reads."""

import dataclasses
import datetime
import fcntl
import glob
import json
import os
import re
import threading
import time
from pathlib import Path

import retention
from delivery_id_index import DeliveryIdIndex
from hooks import durable


def _reject_json_constant(value):
    raise json.JSONDecodeError("non-standard JSON constant", value, 0)


@dataclasses.dataclass(frozen=True)
class EventCursor:
    """Validated event-log position with explicit resume policy."""

    boot_id: str | None = None
    device: int = 0
    inode: int = 0
    generation: int = 0
    offset: int = 0
    reset_only: bool = False

    MAX_ENCODED_BYTES = 160
    MAX_INTEGER = (1 << 64) - 1

    @classmethod
    def initial(cls):
        return cls()

    @classmethod
    def parse(cls, raw):
        if not isinstance(raw, str) or not raw or len(raw) > cls.MAX_ENCODED_BYTES:
            raise ValueError
        parts = raw.split(":")
        if len(parts) == 6 and parts[0] == "v1":
            boot_id = parts[1]
            if not re.fullmatch(r"[0-9a-f]{32}", boot_id):
                raise ValueError
            values = cls._parse_integers(parts[2:])
            return cls(boot_id, *values)
        if len(parts) in (3, 4):
            values = cls._parse_integers(parts)
            return cls(offset=values[-1], reset_only=True)
        if len(parts) == 1:
            return cls(offset=cls._parse_integers(parts)[0], reset_only=True)
        raise ValueError

    @classmethod
    def _parse_integers(cls, fields):
        values = []
        for field in fields:
            if (
                not field
                or len(field) > 20
                or not field.isascii()
                or not field.isdigit()
            ):
                raise ValueError
            value = int(field)
            if value > cls.MAX_INTEGER:
                raise ValueError
            values.append(value)
        return values

    @classmethod
    def issued(cls, boot_id, stat, generation, offset):
        device, inode = (stat.st_dev, stat.st_ino) if stat is not None else (0, 0)
        return cls(boot_id, device, inode, generation, offset)

    def resume(self, current, size):
        if self.boot_id is None and not self.reset_only:
            return 0, False
        identity_matches = (
            self.boot_id == current.boot_id
            and self.device == current.device
            and self.inode == current.inode
            and self.generation == current.generation
        )
        if self.reset_only or not identity_matches or self.offset > size:
            return 0, True
        return self.offset, False

    def format(self):
        if self.boot_id is None:
            raise ValueError("only server-issued cursors can be formatted")
        return ":".join(
            str(part)
            for part in (
                "v1",
                self.boot_id,
                self.device,
                self.inode,
                self.generation,
                self.offset,
            )
        )


class EventLog:
    """Own one durable JSONL log and its retained archive authority.

    ``delivery_store`` supplies the bounded acceleration ledger. The live and
    archived logs remain authoritative when that ledger evicts an identity.
    """

    def __init__(self, config, delivery_store, on_duplicate=lambda: None):
        self.config = config
        self.path = Path(config.events)
        self.delivery_store = delivery_store
        self.delivery_index = DeliveryIdIndex(self.path, self.archive_dir())
        self.on_duplicate = on_duplicate
        self.lock = threading.Lock()
        self._rotate_floor = 0
        self._generation = 0

    @property
    def generation(self):
        return self._generation

    def archive_dir(self):
        configured = self.config.archive_dir
        return str(configured if configured else self.path.parent.resolve() / "archive")

    def archive_path(self, now=None):
        now = now or datetime.datetime.now(datetime.timezone.utc)
        into = self.archive_dir()
        base, ext = os.path.splitext(self.path.name)
        stamp = now.strftime("%Y%m%dT%H%M%SZ")
        path = os.path.join(into, base + "-" + stamp + ext)
        suffix = 1
        while os.path.exists(path):
            path = os.path.join(into, f"{base}-{stamp}-{suffix}{ext}")
            suffix += 1
        return path

    def rotate(self):
        """Rotate while retaining the inode and the projection-required tail.

        Callers normally use the other methods, which invoke this under the
        module's lock. This method is public for explicit maintenance/testing.
        """
        path = str(self.path)
        with self.lock:
            return self._rotate_locked(path)

    def _rotate_locked(self, path):
        with open(path, "r+b") as live:
            fcntl.flock(live, fcntl.LOCK_EX)
            original = live.read()
            lines = original.decode("utf-8", errors="replace").splitlines()
            tail = retention.carry_forward(
                lines, int(time.time() * 1000), retention.POLICY
            ).lines
            data = "".join(line + "\n" for line in tail).encode()
            size = len(original)
            if len(data) > size * 9 // 10:
                self._rotate_floor = size + max(self.config.max_log_bytes // 10, 1)
                return None
            os.makedirs(self.archive_dir(), exist_ok=True)
            archive = self.archive_path()
            with open(archive, "xb") as archived:
                archived.write(original)
                archived.flush()
                os.fsync(archived.fileno())
            durable.fsync_parent(archive)
            self.delivery_index.publish_archives()
            live.seek(0)
            live.write(data)
            live.truncate()
            live.flush()
            os.fsync(live.fileno())
            self._generation += 1
        self._rotate_floor = 0
        return archive

    def _maybe_rotate_locked(self):
        if self.config.max_log_bytes <= 0:
            return
        try:
            size = self.path.stat().st_size
        except OSError:
            return
        if size <= max(self.config.max_log_bytes, self._rotate_floor):
            return
        try:
            self._rotate_locked(str(self.path))
        except OSError:
            pass

    def read(self):
        with self.lock:
            self._maybe_rotate_locked()
            try:
                return self.path.read_bytes()
            except OSError:
                return b""

    def append(self, event):
        line = json.dumps(event, ensure_ascii=False) + "\n"
        path = self.path.resolve()
        with self.lock:
            path.parent.mkdir(parents=True, exist_ok=True)
            with open(durable.lock_path(str(path)), "a+") as process_lock:
                fcntl.flock(process_lock, fcntl.LOCK_EX)
                delivery_id = event.get("delivery_id")
                remembered = self.delivery_store.load_ledger("delivery-ids")
                if delivery_id and delivery_id in remembered:
                    self.on_duplicate()
                    return False
                indexed = (
                    self.delivery_index.contains(delivery_id) if delivery_id else False
                )
                if delivery_id and (
                    indexed is True
                    or (indexed is None and self.has_delivery_id(delivery_id))
                ):
                    try:
                        self.delivery_store.remember("delivery-ids", delivery_id)
                    except OSError:
                        pass
                    self.on_duplicate()
                    return False
                with open(path, "a", encoding="utf-8") as stream:
                    stream.write(line)
                    stream.flush()
                    os.fsync(stream.fileno())
                durable.fsync_parent(str(path))
                if delivery_id:
                    self.delivery_index.remember(delivery_id)
                    self.delivery_store.remember("delivery-ids", delivery_id)
                self._maybe_rotate_locked()
                return True

    def has_delivery_id(self, delivery_id):
        path = str(self.path)
        paths = [path]
        base, ext = os.path.splitext(self.path.name)
        paths.extend(
            sorted(glob.glob(os.path.join(self.archive_dir(), base + "-*" + ext)))
        )
        for candidate in paths:
            try:
                with open(candidate, encoding="utf-8") as stream:
                    for line in stream:
                        try:
                            event = json.loads(line)
                        except ValueError:
                            continue
                        if (
                            isinstance(event, dict)
                            and event.get("delivery_id") == delivery_id
                        ):
                            return True
            except OSError:
                continue
        return False

    def projection_inputs(self, boot_id):
        with self.lock:
            self._maybe_rotate_locked()
            events = []
            try:
                with open(self.path, "rb") as stream:
                    stat = os.fstat(stream.fileno())
                    for line in stream:
                        try:
                            events.append(
                                json.loads(line, parse_constant=_reject_json_constant)
                            )
                        except (UnicodeDecodeError, json.JSONDecodeError):
                            events.append(None)
                    cursor = EventCursor.issued(
                        boot_id, stat, self._generation, stat.st_size
                    ).format()
            except FileNotFoundError:
                cursor = EventCursor.issued(boot_id, None, self._generation, 0).format()
            return events, cursor, self._generation

    def read_records(self, boot_id, cursor):
        with self.lock:
            self._maybe_rotate_locked()
            records = []
            try:
                with open(self.path, "rb") as stream:
                    stat = os.fstat(stream.fileno())
                    current = EventCursor.issued(boot_id, stat, self._generation, 0)
                    offset, reset = cursor.resume(current, stat.st_size)
                    stream.seek(offset)
                    chunk = stream.read()
                    end = chunk.rfind(b"\n") + 1
                    for line in chunk[:end].splitlines(keepends=True):
                        offset += len(line)
                        records.append((offset, line))
                    return records, dataclasses.replace(current, offset=offset), reset
            except FileNotFoundError:
                current = EventCursor.issued(boot_id, None, self._generation, 0)
                _, reset = cursor.resume(current, 0)
                return records, current, reset
