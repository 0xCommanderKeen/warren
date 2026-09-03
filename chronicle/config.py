"""Immutable Chronicle configuration and its process-entry parsing seam."""

from __future__ import annotations

import functools
import os
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path


ROOT = Path(__file__).resolve().parent


def state_dir() -> Path:
    """The local state directory: ``~/.chronicle``.

    It used to be ``~/.burrow``, and until warren#361 this function preferred an
    existing one so that a machine mid-rename would not strand a spool with
    undelivered events in it. That fallback is gone: every deployed container is
    now on an image that has only ever written ``~/.chronicle``, and the dev
    machines were renamed by hand with their spools drained. A machine that still
    has a ``~/.burrow`` keeps it as a static archive — nothing appends to it, and
    ``mv ~/.burrow ~/.chronicle`` is the one operator step that adopts it.
    """
    return Path("~").expanduser() / ".chronicle"


DEFAULT_EVENTS = state_dir() / "events.jsonl"


def setting(environ: Mapping[str, str], name: str) -> str | None:
    """Read ``CHRONICLE_<name>``.

    It also read the pre-rename ``BURROW_<name>`` until warren#361 finished the
    rename. That fallback existed so a redeploy did not have to flip the names in
    the same breath as the code — and it is gone because nothing writes the old
    spelling any more and the burrow's own environment was checked to be free of
    it before this landed. An environment still spelled the old way now gets the
    *default* rather than the value it names, which is why that check was made
    against the running fleet instead of assumed.
    """
    return environ.get("CHRONICLE_" + name)


def _integer(value: str | None, default: int) -> int:
    if not value:
        return default
    try:
        return int(value)
    except ValueError:
        return default


def _floating(value: str | None, default: float) -> float:
    if not value:
        return default
    try:
        return float(value)
    except ValueError:
        return default


@dataclass(frozen=True, slots=True)
class Config:
    """Complete settings value consumed by one Chronicle application runtime."""

    host: str = "127.0.0.1"
    port: int = 8737
    root: Path = ROOT
    events: Path = DEFAULT_EVENTS
    villagers_dir: Path = ROOT / "villagers"
    token: str = ""
    archive_dir: Path | None = None
    max_event_bytes: int = 64 * 1024
    max_log_bytes: int = 5 * 1024 * 1024
    notify_url: str = ""
    notify_token: str = ""
    notify_timeout: float = 5.0
    notify_memory: int = 512
    notify_workers: int = 2
    notify_queue: int = 64
    knock_records: int = 1024
    knock_bytes: int = 5 * 1024 * 1024
    ledger_records: int = 1024
    ledger_bytes: int = 5 * 1024 * 1024
    knock_lock_shards: int = 32
    drop_seconds: int = 12 * 60 * 60

    @classmethod
    def from_env(
        cls,
        environ: Mapping[str, str] | None = None,
        argv: Sequence[str] = (),
    ) -> Config:
        """Parse settings explicitly at the process entry point.

        ``argv`` contains arguments after the executable name. Unknown or invalid
        values retain the documented defaults, matching the historical CLI.
        """
        environ = os.environ if environ is None else environ
        defaults = cls()
        read = functools.partial(setting, environ)
        port = int(argv[0]) if argv and argv[0].isdigit() else defaults.port
        events = Path(read("EVENTS") or str(defaults.events)).expanduser()
        archive = (read("ARCHIVE") or "").strip()
        knock_records = _integer(read("KNOCK_RECORDS"), defaults.knock_records)
        knock_bytes = _integer(read("KNOCK_BYTES"), defaults.knock_bytes)
        host = read("HOST")
        return cls(
            host=defaults.host if host is None else host,
            port=port,
            events=events,
            villagers_dir=Path(
                read("VILLAGERS") or str(defaults.villagers_dir)
            ).expanduser(),
            token=(read("TOKEN") or "").strip(),
            archive_dir=Path(archive).expanduser() if archive else None,
            max_log_bytes=_integer(read("MAX_LOG"), defaults.max_log_bytes),
            notify_url=(read("NOTIFY_URL") or "").strip(),
            notify_token=(read("NOTIFY_TOKEN") or "").strip(),
            notify_timeout=_floating(read("NOTIFY_TIMEOUT"), defaults.notify_timeout),
            knock_records=knock_records,
            knock_bytes=knock_bytes,
            ledger_records=knock_records,
            ledger_bytes=knock_bytes,
        )
