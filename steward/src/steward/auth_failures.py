"""Bounded, process-local failed-auth buckets and aggregate request-log summaries."""

import math
import threading
import time
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass, field
from ipaddress import ip_address

from steward.store import Store, new_id


@dataclass(frozen=True)
class FailurePolicy:
    """Bounds for failed requests; valid credentials never spend these allowances."""

    burst: int = 5
    refill_seconds: float = 12.0
    capacity: int = 1024
    idle_seconds: float = 300.0
    audit_seconds: float = 60.0

    def __post_init__(self) -> None:
        """Refuse policies that cannot bound storage or calculate retry times."""
        values = (
            self.burst,
            self.refill_seconds,
            self.capacity,
            self.idle_seconds,
            self.audit_seconds,
        )
        if any(not math.isfinite(value) or value <= 0 for value in values):
            raise ValueError("auth failure limits must be finite and positive")


@dataclass
class _Bucket:
    tokens: float
    updated: float


def source_key(host: str | None) -> str:
    """Store only a normalized peer address; never retain attacker-supplied header text."""
    try:
        return str(ip_address(host or ""))
    except ValueError:
        return "unknown"


@dataclass
class AuthFailures:
    """Limit failure responses and audit writes with bounded, synchronized state."""

    store: Store
    policy: FailurePolicy = field(default_factory=FailurePolicy)
    now: Callable[[], float] = time.monotonic
    _buckets: OrderedDict[str, _Bucket] = field(default_factory=OrderedDict, init=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, init=False)
    _last_audit: float | None = field(default=None, init=False)
    _count: int = field(default=0, init=False)
    _throttled: int = field(default=0, init=False)
    _sources: set[str] = field(default_factory=set, init=False)

    def refuse(self, host: str | None) -> int:
        """Return zero for 401, or Retry-After seconds for 429; summarize without secrets."""
        source = source_key(host)
        with self._lock:
            now = self.now()
            while self._buckets:
                oldest = next(iter(self._buckets))
                if now - self._buckets[oldest].updated < self.policy.idle_seconds:
                    break
                self._buckets.popitem(last=False)
            bucket = self._buckets.pop(source, _Bucket(float(self.policy.burst), now))
            bucket.tokens = min(
                self.policy.burst,
                bucket.tokens + max(0.0, now - bucket.updated) / self.policy.refill_seconds,
            )
            bucket.updated = now
            retry = 0
            if bucket.tokens >= 1:
                bucket.tokens -= 1
            else:
                retry = math.ceil((1 - bucket.tokens) * self.policy.refill_seconds)
            self._buckets[source] = bucket
            if len(self._buckets) > self.policy.capacity:
                self._buckets.popitem(last=False)
            self._count += 1
            self._throttled += bool(retry)
            if len(self._sources) < 32:  # noqa: PLR2004 — bounded diagnostic sample
                self._sources.add(source)
            # First failure is visible immediately; the whole process writes at most one
            # summary per interval afterwards, even if a caller changes addresses.
            if self._last_audit is None or now - self._last_audit >= self.policy.audit_seconds:
                self._flush(now)
            return retry

    def _flush(self, now: float) -> None:
        self.store.log_request(
            request_id=new_id(),
            method="AUTH",
            path="/auth",
            outcome="auth_failures",
            detail={
                "failed": self._count,
                "throttled": self._throttled,
                "sources_sample": sorted(self._sources),
                "window_seconds": 0
                if self._last_audit is None
                else max(0.0, now - self._last_audit),
            },
        )
        self._last_audit = now
        self._count = self._throttled = 0
        self._sources.clear()

    def flush(self) -> None:
        """Persist the final aggregate at graceful API shutdown."""
        with self._lock:
            if self._count:
                self._flush(self.now())
