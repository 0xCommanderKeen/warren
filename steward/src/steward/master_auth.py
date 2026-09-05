"""Master-token overlap policy shared by the route and body guards."""

import logging
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from hmac import compare_digest

PREVIOUS_ENV = "STEWARD_TOKEN_PREVIOUS"
UNTIL_ENV = "STEWARD_TOKEN_PREVIOUS_UNTIL"
log = logging.getLogger(__name__)


@dataclass
class MasterTokens:
    """Accept an old credential only until its explicit deadline; never expose secrets."""

    current: str | None = field(repr=False)
    previous: str | None = field(default=None, repr=False)
    until: str | None = None
    now: Callable[[], datetime] = lambda: datetime.now(UTC)
    compare: Callable[[bytes, bytes], bool] = compare_digest
    deadline: datetime | None = field(init=False, default=None)
    _last: dict[str, datetime] = field(init=False, default_factory=dict, repr=False)
    _lock: threading.Lock = field(init=False, default_factory=threading.Lock, repr=False)

    def __post_init__(self) -> None:
        """Validate rotation settings before serving any requests."""
        self.current = (self.current or "").strip() or None
        if self.previous is not None:
            self.previous = self.previous.strip()
        if self.previous is None and self.until is None:
            return
        if not self.current or not self.previous or not self.previous.strip() or not self.until:
            raise ValueError(
                "rotation requires STEWARD_TOKEN, STEWARD_TOKEN_PREVIOUS "
                "and STEWARD_TOKEN_PREVIOUS_UNTIL"
            )
        try:
            self.deadline = datetime.fromisoformat(self.until)
        except ValueError:
            raise ValueError(
                "STEWARD_TOKEN_PREVIOUS_UNTIL must be an explicit UTC timestamp"
            ) from None
        if self.deadline.utcoffset() != timedelta(0):
            raise ValueError("STEWARD_TOKEN_PREVIOUS_UNTIL must be an explicit UTC timestamp")
        if self.current == self.previous:
            raise ValueError("current and previous master tokens must differ")

    def match(self, presented: bytes) -> str | None:
        """Return the non-secret slot name, or refuse the credential."""
        if self.current is None:
            return "open"
        current = self.compare(presented, self.current.encode())
        previous = self.compare(presented, self.previous.encode()) if self.previous else False
        if presented and current:
            return "current"
        if presented and previous and self.deadline is not None and self.now() < self.deadline:
            return "previous"
        return None

    def audit(self, slot: str) -> None:
        """Log at most once per slot per minute, independent of request volume."""
        if slot == "open":
            return
        now = self.now()
        with self._lock:
            last = self._last.get(slot)
            if last is not None and now - last < timedelta(minutes=1):
                return
            self._last[slot] = now
        log.info("master token authenticated: slot=%s", slot, extra={"master_token_slot": slot})


def rotation_status(env: Mapping[str, str], now: datetime) -> str:
    """Describe only local rotation configuration, without printing its values."""
    try:
        policy = MasterTokens(env.get("STEWARD_TOKEN"), env.get(PREVIOUS_ENV), env.get(UNTIL_ENV))
    except ValueError as exc:
        return f"invalid rotation configuration: {exc}"
    if policy.deadline is None:
        return "clean: no previous master token configured (local configuration only)"
    state = "active" if now < policy.deadline else "expired"
    return (
        f"{state}: previous master token remains configured; remove after migration "
        "(local configuration only)"
    )
