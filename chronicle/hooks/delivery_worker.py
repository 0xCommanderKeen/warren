"""Supervised, independent delivery of the existing durable hook outbox.

One service owns a spool. The stable owner lock also excludes duplicate services;
no child processes are started by hooks. IDs and acknowledgements stay in the
legacy outbox format so stopping/upgrading the service never requires replay.
"""

import concurrent.futures
import fcntl
import json
import os
import random
import socket
import time
import urllib.error
import urllib.request

try:
    from hooks import emit, presence
except ImportError:
    import emit
    import presence

BATCH_RECORDS = 32
BATCH_BYTES = 60 * 1024
REQUEST_TIMEOUT = 2


def enqueue(event, *, session_id=""):
    """Persist semantic history first; replaceable observations must not endanger it."""
    event = emit.redact_event(event)
    if session_id:
        event["telemetry_managed"] = True
    primary, mirrors, later, later_mirrors = emit._target_groups()
    if not primary and not later:
        emit._append_local(event)
    else:
        delivery_id = emit.uuid.uuid4().hex
        additions = emit._stamp_enqueue_order(
            [
                dict(delivery_id=delivery_id, target=emit._target_id(url), event=event)
                for url, _ in primary + later
            ]
        )
        dropped, saved = emit._update_outbox(set(), additions)
        if not saved:
            dropped = emit._journal_outbox(additions)
            if dropped is None:
                emit._append_local(event)
                emit._diagnose("failure", reason="outbox journal failure")
        if dropped:
            emit._diagnose("drop", count=dropped, reason="outbox capacity")
    # A failure in the lower-priority channels cannot undo the semantic commit.
    try:
        if session_id:
            presence.observe(
                os.path.join(emit.LOG_DIR, "latest-presence.json"), event, session_id
            )
        mirror_records = emit._stamp_enqueue_order(
            [
                dict(
                    delivery_id=emit.uuid.uuid4().hex,
                    target=emit._target_id(url),
                    event=event,
                )
                for url, _ in mirrors + later_mirrors
            ]
        )
        if mirror_records:
            update_mirrors(set(), mirror_records)
    except OSError:
        emit._diagnose("failure", reason="presence or mirror disk failure")


def update_mirrors(delivered, additions=()):
    """Mirrors have their own bounded spool and cannot consume primary capacity."""
    with presence.transaction(
        os.path.join(emit.LOG_DIR, "mirror-outbox.json")
    ) as state:
        records = [
            record
            for record in state.get("records", [])
            if emit._record_key(record) not in delivered
        ]
        records.extend(additions)
        dropped = 0
        while (
            len(records) > emit.OUTBOX_RECORDS
            or len(json.dumps(records).encode()) > emit.OUTBOX_BYTES
        ):
            records.pop(0)
            dropped += 1
        state["records"] = records
        if dropped:
            state["drops"] = state.get("drops", 0) + dropped
            emit._diagnose("mirror_drop", count=dropped, reason="mirror capacity")
        return records


def post_json(url, path, body, token):
    """Return only bounded error classes; never retain URL, response or exception text."""
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = "Bearer " + token
    request = urllib.request.Request(
        url.rstrip("/") + path,
        data=json.dumps(body, ensure_ascii=False).encode(),
        headers=headers,
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT) as response:
            return None if 200 <= response.status < 300 else "http"
    except urllib.error.HTTPError as error:
        if error.code in (401, 403):
            return "authentication"
        if error.code in (400, 413, 422):
            return "invalid_event"
        return "http"
    except (TimeoutError, socket.timeout):
        return "timeout"
    except urllib.error.URLError as error:
        if isinstance(error.reason, socket.gaierror):
            return "dns"
        if isinstance(error.reason, (TimeoutError, socket.timeout)):
            return "timeout"
        return "connect"
    except OSError:
        return "connect"


class DeliveryWorker:
    """A bounded delivery turn; clock/transport are the testable external boundary."""

    def __init__(self, *, clock=time.time, transport=post_json, jitter=random.random):
        self.clock = clock
        self.transport = transport
        self.jitter = jitter
        self.status_path = os.path.join(emit.LOG_DIR, "delivery-status.json")
        saved = presence.read(self.status_path)
        self.retries = saved.get("retries", {})
        self.last_success = saved.get("last_success", {})
        self.errors = saved.get("errors", {})

    def tick(self):
        pending = emit._read_durable_outbox_snapshot()
        primary, mirrors, _, _ = emit._target_groups(pending)
        mirrored = update_mirrors(set())
        mirror_targets = {emit._target_id(url) for url, _ in mirrors}
        primary += mirrors
        pending += mirrored
        if len(primary) == 1:
            url, token = primary[0]
            self._target_tick(
                url, token, pending, emit._target_id(url) in mirror_targets
            )
        elif primary:
            with concurrent.futures.ThreadPoolExecutor(
                max_workers=emit.MAX_TARGETS
            ) as pool:
                futures = [
                    pool.submit(
                        self._target_tick,
                        url,
                        token,
                        pending,
                        emit._target_id(url) in mirror_targets,
                    )
                    for url, token in primary
                ]
                for future in futures:
                    future.result()
        with presence.transaction(self.status_path) as saved:
            saved.update(
                observed_at=self.clock(),
                worker="running",
                queue_depth=len(emit._read_durable_outbox_snapshot()),
                retries=self.retries,
                last_success=self.last_success,
                errors=self.errors,
            )

    def _target_tick(self, url, token, pending, mirror=False):
        target = emit._target_id(url)
        attempts, retry_at = self.retries.get(target, (0, 0))
        # Rotate even backoff targets so they cannot monopolize all eight slots.
        emit._update_outbox(set(), [], {target})
        if self.clock() < retry_at:
            return
        latest = presence.report(os.path.join(emit.LOG_DIR, "latest-presence.json"))
        target_pending = [
            record for record in pending if record.get("target") == target
        ]

        def queued_at(record):
            created = emit._enqueue_time(record)
            if created is not None:
                return created
            try:
                return emit.datetime.datetime.fromisoformat(
                    record["event"]["ts"].replace("Z", "+00:00")
                ).timestamp()
            except (ValueError, KeyError, TypeError):
                return 0.0  # unknown legacy age must never claim freshness

        oldest = min((queued_at(record) for record in target_pending), default=None)
        diagnostics = presence.read(emit.DIAGNOSTICS)
        health = dict(
            producer=latest["producer"],
            target=target,
            observed_at=self.clock(),
            queue_depth=len(target_pending),
            oldest_at=oldest,
            last_success=self.last_success.get(target),
            error=self.errors.get(target),
            retry_at=retry_at,
            failures=attempts,
            worker="running",
            overflow=(
                presence.read(os.path.join(emit.LOG_DIR, "mirror-outbox.json")).get(
                    "drops", 0
                )
                if mirror
                else diagnostics.get("drops", 0)
            ),
            presence_overflow=latest.get("presence_overflow", 0),
        )
        error = self.transport(
            url,
            "/telemetry",
            dict(health=health, presence=list(latest.get("presence", {}).values())),
            token,
        )
        if error:
            self._failed(target, attempts, error)
            return
        self.retries[target] = (0, 0)
        self.last_success[target] = self.clock()
        self.errors[target] = None
        records = []
        for record in target_pending:
            candidate = dict(delivery_id=record["delivery_id"], event=record["event"])
            if (
                records
                and len(json.dumps({"records": records + [candidate]}).encode())
                > BATCH_BYTES
            ):
                break
            records.append(candidate)
            if len(records) == BATCH_RECORDS:
                break
        if not records:
            return
        error = self.transport(url, "/events/batch", {"records": records}, token)
        if error:
            self._failed(target, attempts, error)
            return
        self.last_success[target] = self.clock()
        keys = {(target, record["delivery_id"]) for record in records}
        if mirror:
            update_mirrors(keys)
            saved = True
        else:
            _, saved = emit._update_outbox(keys, [])
        if saved:
            emit._diagnose_outbox(emit._read_durable_outbox_snapshot(), keys)
        else:
            self._failed(target, 0, "disk")

    def _failed(self, target, attempts, error):
        attempts += 1
        self.retries[target] = (
            attempts,
            self.clock() + min(30, 2 ** min(attempts, 5)) * (0.5 + self.jitter() / 2),
        )
        self.errors[target] = error
        emit._diagnose("failure", target=target, reason=error)

    def run(self, stop):
        os.makedirs(emit.LOG_DIR, mode=0o700, exist_ok=True)
        with open(os.path.join(emit.LOG_DIR, "delivery-owner.lock"), "a") as owner:
            try:
                fcntl.flock(owner, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return
            while not stop.is_set():
                try:
                    self.tick()
                except Exception as error:
                    # A disk failure must neither kill supervision nor expose event content.
                    emit._diagnose(
                        "failure",
                        reason="disk" if isinstance(error, OSError) else "worker",
                    )
                stop.wait(0.25)


def run_service():
    """Foreground entry point for a one-file bundle under an external supervisor."""
    import signal
    import threading

    os.umask(0o077)
    config = os.path.join(emit.LOG_DIR, "delivery-config.json")
    if not os.path.exists(config):
        if not emit._setting("URL"):
            raise RuntimeError("CHRONICLE_URL is required for the delivery worker")
        with presence.transaction(config) as settings:
            settings.update(
                {
                    name: emit._setting(name, "")
                    for name in ("URL", "TOKEN", "MIRROR", "MIRROR_TOKEN")
                }
            )
    # Container credentials arrive through its current environment; the marker is
    # not an authority overriding a newly provisioned credential on restart.
    stop = threading.Event()
    for signum in (signal.SIGINT, signal.SIGTERM):
        signal.signal(signum, lambda *_: stop.set())
    DeliveryWorker().run(stop)
