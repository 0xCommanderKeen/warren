"""Immutable Chronicle configuration and its process-entry parsing seam."""

from __future__ import annotations

import functools
import os
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path


ROOT = Path(__file__).resolve().parent


def state_dir() -> Path:
    """The local state directory: ``~/.chronicle``, or ``~/.burrow`` if that is
    what this machine already has.

    An existing ``~/.burrow`` is live state — the emitter's offline fallback log
    and its durable outbox — belonging to sessions that are running right now.
    Preferring the new name unconditionally would silently strand a spool that
    still has events in it, so the old directory keeps being used where it exists
    and only a machine with neither gets the new name. Renaming the directory is
    therefore an operator step, safe to take whenever the spool is drained, and
    not something a deploy does underneath a running fleet.
    """
    home = Path("~").expanduser()
    new = home / ".chronicle"
    if new.is_dir():
        return new
    old = home / ".burrow"
    if old.is_dir():
        return old
    return new


DEFAULT_EVENTS = state_dir() / "events.jsonl"


def setting(environ: Mapping[str, str], name: str) -> str | None:
    """Read ``CHRONICLE_<name>``, falling back to the pre-rename ``BURROW_<name>``.

    Both spellings are accepted for one release: the compose file and ``.env`` on
    the NAS were written before the rename, and a redeploy that flipped the names
    in the same breath as the code would leave no window in which either could be
    wrong. The new spelling wins wherever both are set, so a half-migrated
    environment resolves one way rather than per setting.

    Presence — not truthiness — selects the spelling. ``CHRONICLE_TOKEN=`` means
    "open ingest", and must override a stale ``BURROW_TOKEN`` rather than fall
    through to it.
    """
    new = "CHRONICLE_" + name
    if new in environ:
        return environ[new]
    return environ.get("BURROW_" + name)


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
