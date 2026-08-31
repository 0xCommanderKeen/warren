"""Immutable Burrow configuration and its process-entry parsing seam."""

from __future__ import annotations

import os
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path


ROOT = Path(__file__).resolve().parent


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
    """Complete settings value consumed by one Burrow application runtime."""

    host: str = "127.0.0.1"
    port: int = 8737
    root: Path = ROOT
    events: Path = Path("~/.burrow/events.jsonl").expanduser()
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
        port = int(argv[0]) if argv and argv[0].isdigit() else defaults.port
        events = Path(environ.get("BURROW_EVENTS") or str(defaults.events)).expanduser()
        archive = (environ.get("BURROW_ARCHIVE") or "").strip()
        knock_records = _integer(
            environ.get("BURROW_KNOCK_RECORDS"), defaults.knock_records
        )
        knock_bytes = _integer(environ.get("BURROW_KNOCK_BYTES"), defaults.knock_bytes)
        return cls(
            host=environ.get("BURROW_HOST", defaults.host),
            port=port,
            events=events,
            villagers_dir=Path(
                environ.get("BURROW_VILLAGERS") or str(defaults.villagers_dir)
            ).expanduser(),
            token=(environ.get("BURROW_TOKEN") or "").strip(),
            archive_dir=Path(archive).expanduser() if archive else None,
            max_log_bytes=_integer(
                environ.get("BURROW_MAX_LOG"), defaults.max_log_bytes
            ),
            notify_url=(environ.get("BURROW_NOTIFY_URL") or "").strip(),
            notify_token=(environ.get("BURROW_NOTIFY_TOKEN") or "").strip(),
            notify_timeout=_floating(
                environ.get("BURROW_NOTIFY_TIMEOUT"), defaults.notify_timeout
            ),
            knock_records=knock_records,
            knock_bytes=knock_bytes,
            ledger_records=knock_records,
            ledger_bytes=knock_bytes,
        )
