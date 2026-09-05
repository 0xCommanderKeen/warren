"""Shared state-path configuration for persistence and execution."""

import os
from pathlib import Path

DEFAULT_STATE_PATH = Path(".steward/state/scheduler.json")
STATE_ENV = "STEWARD_STATE"


def default_state_path(env: dict[str, str] | None = None) -> Path:
    """Return ``$STEWARD_STATE``, or ``.steward/state/scheduler.json`` under the cwd."""
    source = os.environ if env is None else env
    configured = (source.get(STATE_ENV) or "").strip()
    return Path(configured).expanduser() if configured else DEFAULT_STATE_PATH
