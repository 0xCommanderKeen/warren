"""Sessions steward has started, and what the watchdog has already done.

The run lease is the credential expiry. Task leases and sessions share one
transaction here, so neither half can outlive a lost conditional write.
"""

from collections.abc import Sequence
from datetime import datetime, timedelta
from typing import Any

from steward.events import utc_now_iso
from steward.letter_replies import bounded_message
from steward.runs import (
    DELIVERY_STATUSES,
    RUN_DELEGATED,
    RUN_TASK,
    DeliveryStatus,
    validate_kind_trigger,
)
from steward.session_auth import SessionPrincipal, credential_digest
from steward.store._connection import _Connection
from steward.store.records import (
    STATUS_CLAIMED,
    STATUS_OPEN,
    JobRecord,
    OpenRun,
    WatchdogAttempt,
    _dumps,
)


class _AtomicTaskCloseLostError(Exception):
    """Rollback sentinel for a task/run close that lost either conditional write."""


class _RunTables(_Connection):
    """Runs table operations on the shared connection."""

    def finish_job_and_claim_run_terminal(  # noqa: PLR0913
        self,
        task_id: str,
        *,
        run_id: str,
        event: str,
        event_id: str,
        status: str,
        claimant: str,
        outcome: str | None = None,
        reason: str | None = None,
        artifacts: Sequence[str] = (),
        final_message: str = "",
        lease: str | None = None,
        owner_token: str | None = None,
        stale_before: str | None = None,
        now: str | None = None,
    ) -> JobRecord | None:
        """Close one claim and choose its immutable terminal fact in one transaction."""
        moment = now or utc_now_iso()
        try:
            with self._lock, self._conn:
                if owner_token is not None:
                    run = self._conn.execute(
                        "UPDATE open_runs SET terminal_event = ?, terminal_event_id = ?, "
                        "terminal_claimed_at = ? WHERE run_id = ? AND closed_at IS NULL "
                        "AND terminal_event IS NULL AND owner_token = ?",
                        (event, event_id, moment, run_id, owner_token),
                    )
                else:
                    run = self._conn.execute(
                        "UPDATE open_runs SET terminal_event = ?, terminal_event_id = ?, "
                        "terminal_claimed_at = ? WHERE run_id = ? AND closed_at IS NULL "
                        "AND terminal_event IS NULL AND heartbeat_at <= ?",
                        (event, event_id, moment, run_id, stale_before),
                    )
                if run.rowcount == 0:
                    raise _AtomicTaskCloseLostError  # noqa: TRY301
                job = self._conn.execute(
                    "UPDATE jobs SET status = ?, outcome = ?, reason = ?, artifacts = ?, "
                    "final_message = ?, "
                    "finished_at = ?, lease_expires_at = NULL, run_id = NULL, owner_token = NULL "
                    "WHERE task_id = ? AND status = ? AND claimant = ? "
                    "AND (? IS NULL OR claimed_at = ?) AND run_id = ? AND owner_token = ?",
                    (
                        status,
                        outcome,
                        reason,
                        _dumps(list(artifacts)),
                        bounded_message(final_message),
                        moment,
                        task_id,
                        STATUS_CLAIMED,
                        claimant,
                        lease,
                        lease,
                        run_id,
                        owner_token,
                    ),
                )
                if job.rowcount == 0:
                    raise _AtomicTaskCloseLostError  # noqa: TRY301
                row = self._conn.execute(
                    "SELECT * FROM jobs WHERE task_id = ?", (task_id,)
                ).fetchone()
        except _AtomicTaskCloseLostError:
            return None
        return JobRecord.from_row(row)

    def expire_leases(self, now: str | None = None) -> list[JobRecord]:
        """Reopen every claimed task whose lease has run out, and say who dropped it.

        Returns the rows *as they were when the lease died* — status ``claimed``, with the
        claimant still named — because that is what a ``task_failed`` event has to carry.
        The row itself is back to ``open`` by the time this returns, so the next dispatch
        can hand it to somebody who will actually finish it.
        """
        moment = now or utc_now_iso()
        expired: list[JobRecord] = []
        with self._lock, self._conn:
            rows = self._conn.execute(
                "SELECT * FROM jobs WHERE status = ? AND lease_expires_at IS NOT NULL "
                "AND lease_expires_at <= ? ORDER BY created_at, rowid",
                (STATUS_CLAIMED, moment),
            ).fetchall()
            for row in rows:
                record = JobRecord.from_row(row)
                if record.run_id is not None:
                    # Bound attempts need a run-specific terminal choice; callers using
                    # the legacy API must not split those two durable facts.
                    continue
                # An upgraded database may contain a claimed job and its still-open run
                # from before the association columns existed.  Silence is safer than
                # reopening underneath a possibly live owner; a later operator/sweep can
                # resolve the legacy row once the run is answered.
                legacy = self._conn.execute(
                    "SELECT 1 FROM open_runs WHERE kind IN (?, ?) AND ref = ? "
                    "AND closed_at IS NULL LIMIT 1",
                    (RUN_TASK, RUN_DELEGATED, record.task_id),
                ).fetchone()
                if legacy is not None:
                    continue
                cursor = self._conn.execute(
                    "UPDATE jobs SET status = ?, claimant = NULL, claimed_at = NULL, "
                    "lease_expires_at = NULL, lease_duration_s = NULL WHERE task_id = ? "
                    "AND status = ? AND claimed_at = ?",
                    (STATUS_OPEN, record.task_id, STATUS_CLAIMED, record.claimed_at),
                )
                if cursor.rowcount == 1:
                    expired.append(record)
        return expired

    def expire_task_attempt_and_claim_terminal(  # noqa: PLR0913
        self,
        task_id: str,
        *,
        lease: str,
        run_id: str,
        owner_token: str,
        event: str,
        event_id: str,
        now: str | None = None,
    ) -> JobRecord | None:
        """Atomically reopen one dead attempt and choose its exact run failure."""
        moment = now or utc_now_iso()
        try:
            with self._lock, self._conn:
                row = self._conn.execute(
                    "SELECT * FROM jobs WHERE task_id = ? AND status = ? AND claimed_at = ? "
                    "AND run_id = ? AND owner_token = ? AND lease_expires_at <= ?",
                    (task_id, STATUS_CLAIMED, lease, run_id, owner_token, moment),
                ).fetchone()
                if row is None:
                    raise _AtomicTaskCloseLostError  # noqa: TRY301
                run = self._conn.execute(
                    "UPDATE open_runs SET terminal_event = ?, terminal_event_id = ?, "
                    "terminal_claimed_at = ? WHERE run_id = ? AND owner_token = ? "
                    "AND closed_at IS NULL AND terminal_event IS NULL",
                    (event, event_id, moment, run_id, owner_token),
                )
                job = self._conn.execute(
                    "UPDATE jobs SET status = ?, claimant = NULL, claimed_at = NULL, "
                    "lease_expires_at = NULL, lease_duration_s = NULL, run_id = NULL, "
                    "owner_token = NULL WHERE task_id = ? AND status = ? AND claimed_at = ? "
                    "AND run_id = ? AND owner_token = ? AND lease_expires_at <= ?",
                    (
                        STATUS_OPEN,
                        task_id,
                        STATUS_CLAIMED,
                        lease,
                        run_id,
                        owner_token,
                        moment,
                    ),
                )
                if run.rowcount != 1 or job.rowcount != 1:
                    raise _AtomicTaskCloseLostError  # noqa: TRY301
        except _AtomicTaskCloseLostError:
            return None
        return JobRecord.from_row(row)

    # -- the watchdog's own memory -------------------------------------------------------

    def watchdog_attempt(self, resident: str) -> WatchdogAttempt:
        """Return this resident's restart budget. An untouched resident is a fresh one."""
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM watchdog_attempts WHERE resident = ?", (resident,)
            ).fetchone()
        return WatchdogAttempt.from_row(row) if row else WatchdogAttempt(resident=resident)

    def record_watchdog_attempt(
        self,
        resident: str,
        *,
        reason: str,
        next_attempt_at: str | None,
        now: str | None = None,
    ) -> WatchdogAttempt:
        """Count one restart against this resident's budget and say when the next may be."""
        moment = now or utc_now_iso()
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO watchdog_attempts (resident, attempts, reason, last_attempt_at, "
                "next_attempt_at) VALUES (?, 1, ?, ?, ?) ON CONFLICT(resident) DO UPDATE SET "
                "attempts = attempts + 1, reason = excluded.reason, "
                "last_attempt_at = excluded.last_attempt_at, "
                "next_attempt_at = excluded.next_attempt_at",
                (resident, reason, moment, next_attempt_at),
            )
            row = self._conn.execute(
                "SELECT * FROM watchdog_attempts WHERE resident = ?", (resident,)
            ).fetchone()
        return WatchdogAttempt.from_row(row)

    def give_up_on(self, resident: str, *, reason: str, now: str | None = None) -> bool:
        """Stop restarting this resident. Returns whether *this* call gave up.

        Conditional on ``gave_up_at IS NULL``, for the same reason a budget pause is
        conditional: the crash-loop knock is one knock, not one per pass for as long as
        the container stays down.
        """
        moment = now or utc_now_iso()
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO watchdog_attempts (resident, attempts, reason) "
                "VALUES (?, 0, ?) ON CONFLICT(resident) DO NOTHING",
                (resident, reason),
            )
            cursor = self._conn.execute(
                "UPDATE watchdog_attempts SET gave_up_at = ?, reason = ? "
                "WHERE resident = ? AND gave_up_at IS NULL",
                (moment, reason, resident),
            )
            return cursor.rowcount == 1

    def clear_watchdog_attempts(self, resident: str) -> None:
        """Forget a resident's restart history, because it came back healthy."""
        with self._lock, self._conn:
            self._conn.execute("DELETE FROM watchdog_attempts WHERE resident = ?", (resident,))

    def close_unbracketed_run(
        self, *, run_id: str, agent_id: str, routine: str, started_at: str, now: str | None = None
    ) -> bool:
        """Mark a run that never reported back as closed. Returns whether this call did it.

        The row is the receipt for a ``routine_failed`` steward emitted on the session's
        behalf. Conditional on the primary key, so a run that vanished is buried once even
        if the watchdog passes over it every minute for a week.
        """
        with self._lock, self._conn:
            cursor = self._conn.execute(
                "INSERT INTO unbracketed_runs (run_id, agent_id, routine, started_at, closed_at) "
                "VALUES (?, ?, ?, ?, ?) ON CONFLICT(run_id) DO NOTHING",
                (run_id, agent_id, routine, started_at, now or utc_now_iso()),
            )
            return cursor.rowcount == 1

    def closed_unbracketed_runs(self) -> set[str]:
        """Return every run steward has already buried, so nobody mourns one twice."""
        with self._lock:
            rows = self._conn.execute("SELECT run_id FROM unbracketed_runs").fetchall()
        return {row["run_id"] for row in rows}

    # -- the run registry ----------------------------------------------------------------

    def open_run(  # noqa: PLR0913 — one parameter per fact about the session that opened
        self,
        *,
        run_id: str,
        kind: str,
        agent_id: str,
        trigger: str = "",
        project: str = "",
        ref: str = "",
        timeout_s: float = 0.0,
        event_log_path: str = "",
        owner_token: str = "",
        resident_id: str = "",
        session_credential: str = "",
        now: str | None = None,
    ) -> bool:
        """Record that a session has started. Returns whether this call opened the row.

        Written where the opening event is emitted, so steward's own knowledge that a run
        exists does not depend on where that event ended up. Conditional on the primary
        key like its neighbours: a run id is one *session*, and a second open of the same
        id is a repeat rather than a second session.

        Which puts the burden on callers, and it is worth naming: whatever a session is
        about — a routine, a task — the id has to be the session's own. The board learnt
        this the hard way by keying rows on the task id, so a task claimed, dropped on a
        dead lease and re-claimed opened a row the first attempt had already closed, the
        insert was quietly dropped, and the retry ran unwatched. ``ref`` is where the
        thing the session was about goes; several rows may share one.

        ``session_credential`` is the plaintext, and only its digest is written: a copy of
        this database must not yield live credentials. Hashing here rather than in the
        caller means the mint and the check share one definition of what is hashed.
        """
        validate_kind_trigger(kind, trigger)
        with self._lock, self._conn:
            opened_at = now or utc_now_iso()
            cursor = self._conn.execute(
                "INSERT INTO open_runs (run_id, kind, trigger, agent_id, project, ref, timeout_s, "
                "started_at, heartbeat_at, event_log_path, evidence_version, owner_token, "
                "resident_id, session_credential_sha256) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?) "
                "ON CONFLICT(run_id) DO NOTHING",
                (
                    run_id,
                    kind,
                    trigger,
                    agent_id,
                    project,
                    ref,
                    float(timeout_s),
                    opened_at,
                    opened_at,
                    event_log_path,
                    owner_token,
                    resident_id,
                    credential_digest(session_credential),
                ),
            )
            return cursor.rowcount == 1

    def open_task_run(  # noqa: PLR0913
        self,
        *,
        task_id: str,
        lease: str,
        run_id: str,
        kind: str,
        agent_id: str,
        project: str = "",
        ref: str = "",
        timeout_s: float = 0.0,
        event_log_path: str = "",
        owner_token: str,
        resident_id: str = "",
        session_credential: str = "",
        now: str | None = None,
    ) -> bool:
        """Open and bind a task attempt in one transaction.

        The claim stamp fences the binding.  A swept/retried job cannot accidentally be
        attached to the late predecessor, and an inserted run is rolled back when the
        binding loses that race.

        ``session_credential`` is stored as a digest, exactly as in :meth:`open_run`, and
        rolled back with the row when the binding loses that race — a credential for a run
        that never opened would authenticate a session nobody is watching.
        """
        moment = now or utc_now_iso()
        try:
            with self._lock, self._conn:
                opened = self._conn.execute(
                    "INSERT INTO open_runs (run_id, kind, agent_id, project, ref, timeout_s, "
                    "started_at, heartbeat_at, event_log_path, evidence_version, owner_token, "
                    "resident_id, session_credential_sha256) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?) "
                    "ON CONFLICT(run_id) DO NOTHING",
                    (
                        run_id,
                        kind,
                        agent_id,
                        project,
                        ref,
                        float(timeout_s),
                        moment,
                        moment,
                        event_log_path,
                        owner_token,
                        resident_id,
                        credential_digest(session_credential),
                    ),
                )
                if opened.rowcount != 1:
                    raise _AtomicTaskCloseLostError  # noqa: TRY301
                bound = self._conn.execute(
                    "UPDATE jobs SET run_id = ?, owner_token = ? WHERE task_id = ? "
                    "AND status = ? AND claimed_at = ? AND run_id IS NULL",
                    (run_id, owner_token, task_id, STATUS_CLAIMED, lease),
                )
                if bound.rowcount != 1:
                    raise _AtomicTaskCloseLostError  # noqa: TRY301
        except _AtomicTaskCloseLostError:
            return False
        return True

    def renew_task_run(
        self,
        run_id: str,
        *,
        owner_token: str,
        now: str | None = None,
    ) -> bool:
        """Atomically renew the run heartbeat and its exact job attempt lease."""
        moment = now or utc_now_iso()
        try:
            with self._lock, self._conn:
                row = self._conn.execute(
                    "SELECT task_id, claimed_at, lease_duration_s FROM jobs WHERE run_id = ? "
                    "AND owner_token = ? AND status = ?",
                    (run_id, owner_token, STATUS_CLAIMED),
                ).fetchone()
                if row is None or row["lease_duration_s"] is None:
                    raise _AtomicTaskCloseLostError  # noqa: TRY301
                lease_end = utc_now_iso(
                    datetime.fromisoformat(moment)
                    + timedelta(seconds=float(row["lease_duration_s"]))
                )
                run = self._conn.execute(
                    "UPDATE open_runs SET heartbeat_at = ? WHERE run_id = ? AND closed_at IS NULL "
                    "AND terminal_event IS NULL AND owner_token = ?",
                    (moment, run_id, owner_token),
                )
                job = self._conn.execute(
                    "UPDATE jobs SET lease_expires_at = ? WHERE task_id = ? AND status = ? "
                    "AND claimed_at = ? AND run_id = ? AND owner_token = ?",
                    (
                        lease_end,
                        row["task_id"],
                        STATUS_CLAIMED,
                        row["claimed_at"],
                        run_id,
                        owner_token,
                    ),
                )
                if run.rowcount != 1 or job.rowcount != 1:
                    raise _AtomicTaskCloseLostError  # noqa: TRY301
        except _AtomicTaskCloseLostError:
            return False
        return True

    def renew_run(self, run_id: str, *, owner_token: str = "", now: str | None = None) -> bool:
        """Renew an open run's ownership lease; a terminal run stays terminal."""
        with self._lock, self._conn:
            cursor = self._conn.execute(
                "UPDATE open_runs SET heartbeat_at = ? WHERE run_id = ? AND closed_at IS NULL "
                "AND terminal_event IS NULL AND owner_token = ?",
                (now or utc_now_iso(), run_id, owner_token),
            )
            return cursor.rowcount == 1

    def close_stale_run(self, run_id: str, *, stale_before: str, now: str | None = None) -> bool:
        """Atomically close a run only if its ownership lease remains expired."""
        with self._lock, self._conn:
            cursor = self._conn.execute(
                "UPDATE open_runs SET closed_at = ? WHERE run_id = ? AND closed_at IS NULL "
                "AND heartbeat_at <= ?",
                (now or utc_now_iso(), run_id, stale_before),
            )
            return cursor.rowcount == 1

    def claim_run_terminal(  # noqa: PLR0913 - authority is owner token or stale cutoff
        self,
        run_id: str,
        *,
        event: str,
        event_id: str,
        owner_token: str | None = None,
        stale_before: str | None = None,
        now: str | None = None,
    ) -> bool:
        """Choose one immutable terminal fact, by the live owner or an expired watchdog."""
        with self._lock, self._conn:
            prefix = (
                "UPDATE open_runs SET terminal_event = ?, terminal_event_id = ?, "
                "terminal_claimed_at = ? WHERE run_id = ? AND closed_at IS NULL "
                "AND terminal_event IS NULL AND "
            )
            if owner_token is not None:
                cursor = self._conn.execute(
                    prefix + "owner_token = ?",
                    (event, event_id, now or utc_now_iso(), run_id, owner_token),
                )
            else:
                cursor = self._conn.execute(
                    prefix + "heartbeat_at <= ?",
                    (event, event_id, now or utc_now_iso(), run_id, stale_before),
                )
            return cursor.rowcount == 1

    def terminal_runs(self) -> list[OpenRun]:
        """Return chosen terminal facts that still need durable publication/finalization."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM open_runs WHERE terminal_event IS NOT NULL AND closed_at IS NULL "
                "ORDER BY terminal_claimed_at, rowid"
            ).fetchall()
        return [OpenRun.from_row(row) for row in rows]

    def mark_run_terminal_published(
        self, run_id: str, event_id: str, *, now: str | None = None
    ) -> bool:
        """Finalize only the immutable chosen event identified by ``event_id``."""
        moment = now or utc_now_iso()
        with self._lock, self._conn:
            cursor = self._conn.execute(
                "UPDATE open_runs SET terminal_published_at = COALESCE(terminal_published_at, ?), "
                "closed_at = ? WHERE run_id = ? AND terminal_event_id = ? AND closed_at IS NULL",
                (moment, moment, run_id, event_id),
            )
            return cursor.rowcount == 1

    def session_principal(self, credential: str, *, fresh_since: str) -> SessionPrincipal | None:
        """Return who a session credential is, or ``None`` if it is not a live one.

        The condition is the exact negation of the watchdog's burial condition — the run is
        open, no terminal fact has been chosen for it, and its heartbeat is *not* stale by
        ``fresh_since``, which is what :meth:`close_stale_run` and :meth:`claim_run_terminal`
        require in order to bury it. So a credential is accepted exactly while the watchdog
        could not yet call this run dead, and it stops the moment the session closes, times
        out, or the lease goes stale — whether or not a watchdog has actually swept.

        Not :meth:`renew_run`'s condition, which is a near miss worth naming: that one has no
        freshness clause at all (an owner whose heartbeat thread was starved for three
        minutes may still renew and carry on) and it does check ``owner_token``. The owner
        token fences steward's own writes about a run; it is not the session's to present,
        and a session that had it could renew its own credential. Burial is the right clock
        here because it is the one that answers "is anybody still entitled to act as this
        session" — and it is deny-by-default, which is the house rule (steward #41).

        Looked up by digest, so the plaintext is never compared against anything on disk.
        An empty credential is refused before the query: every row that never got one
        stores the empty digest, and matching those would make "no credential" a master
        key.
        """
        digest = credential_digest(credential)
        if not digest:
            return None
        with self._lock:
            row = self._conn.execute(
                "SELECT run_id, resident_id FROM open_runs "
                "WHERE session_credential_sha256 = ? AND closed_at IS NULL "
                "AND terminal_event IS NULL AND heartbeat_at > ?",
                (digest, fresh_since),
            ).fetchone()
        if row is None:
            return None
        return SessionPrincipal(run_id=row["run_id"], resident_id=row["resident_id"])

    def close_run(self, run_id: str, *, now: str | None = None) -> bool:
        """Record that a session reported back. Returns whether this call closed the row.

        Conditional on ``closed_at IS NULL``, so the answer is about *this* call: a run
        the watchdog already buried does not get closed a second time by a session that
        turned up late, and the row keeps the earlier moment it was answered.
        """
        with self._lock, self._conn:
            cursor = self._conn.execute(
                "UPDATE open_runs SET closed_at = ? WHERE run_id = ? AND closed_at IS NULL",
                (now or utc_now_iso(), run_id),
            )
            return cursor.rowcount == 1

    def record_delivery(self, run_id: str, status: DeliveryStatus, reason: str = "") -> bool:
        """Write down what became of a run's final message. Returns whether a row took it.

        Unconditional on ``closed_at``: delivery happens after the terminal transition, so
        the row it lands on is normally already closed, and that is the row that should say
        where the message went. The membership check stays although the type is a
        ``Literal``: this is the database boundary, and a row must never hold a word the
        API would not know how to read back.
        """
        if status not in DELIVERY_STATUSES:
            raise ValueError(f"invalid delivery status: {status!r}")
        with self._lock, self._conn:
            cursor = self._conn.execute(
                "UPDATE open_runs SET delivery = ?, delivery_reason = ? WHERE run_id = ?",
                (status, reason, run_id),
            )
            return cursor.rowcount == 1

    def run_record(self, run_id: str) -> OpenRun | None:
        """Return one run's row, open or closed, or ``None`` when steward never opened it.

        The read seam for a run *by id* — what a test, and a future ``GET /runs/{id}``,
        asks; :meth:`open_runs` and :meth:`terminal_runs` answer the watchdog's questions.
        """
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM open_runs WHERE run_id = ?", (run_id,)
            ).fetchone()
        return OpenRun.from_row(row) if row is not None else None

    def open_runs(self) -> list[OpenRun]:
        """Return every run steward started and has heard nothing back about, oldest first."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM open_runs WHERE closed_at IS NULL ORDER BY started_at, rowid"
            ).fetchall()
        return [OpenRun.from_row(row) for row in rows]

    def record_watchdog_pass(self, *, interventions: int = 0, now: str | None = None) -> str:
        """Record that the watchdog made a pass, and return when. ``doctor`` reads this."""
        moment = now or utc_now_iso()
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO watchdog_passes (id, last_pass_at, passes, interventions) "
                "VALUES (1, ?, 1, ?) ON CONFLICT(id) DO UPDATE SET last_pass_at = excluded."
                "last_pass_at, passes = passes + 1, interventions = interventions + ?",
                (moment, interventions, interventions),
            )
        return moment

    def last_watchdog_pass(self) -> dict[str, Any] | None:
        """Return when the watchdog last swept, or ``None`` if it never has.

        ``None`` is the important answer: it means nothing is watching, which is exactly
        what ``steward doctor`` has to be able to say out loud.
        """
        with self._lock:
            row = self._conn.execute("SELECT * FROM watchdog_passes WHERE id = 1").fetchone()
        if row is None:
            return None
        return {
            "last_pass_at": row["last_pass_at"],
            "passes": row["passes"],
            "interventions": row["interventions"],
        }
