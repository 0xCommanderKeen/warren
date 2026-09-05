"""The durable store behind steward's API: jobs, approvals, and the request log.

One SQLite file (``.steward/state/steward.db``, next to the scheduler's own state)
holds everything the API must remember across a restart. SQLite rather than a JSON
file because the two things stored here both need a *conditional* write to be atomic:
the job board's claim (steward #6) is ``UPDATE … WHERE status = 'open'``, and an
approval decision (steward #10) is ``UPDATE … WHERE status = 'pending'`` — the first
writer wins and every later one reads back what was recorded. A read-modify-write over
a JSON file cannot promise that, and "two residents both hold task 7" is exactly the
kind of lie this project refuses to tell.

Nothing here emits, renders, or decides. It records facts and hands them back:

- ``jobs`` — posted work, with the status the board reports. A row with an ``assignee``
  is work one resident handed to another (:mod:`steward.delegation`): the same table,
  because a delegated item is a task addressed to somebody rather than to the fleet, and
  everything the board already does to a task — leasing it, sweeping it, closing it —
  applies to it unchanged.
- ``approvals`` — a gated action waiting on a human, and the decision it received.
- ``requests`` — every accepted mutating API request and how it turned out, so a
  queued action that later failed is traceable rather than silently gone.
- ``run_ledger`` — one row per finished session, with the tokens, money, and seconds it
  actually cost. This is what makes a daily budget survive a daemon restart: a cap that
  resets because the process bounced is not a cap (steward #8).
- ``budget_pauses`` — the residents steward has stopped firing, and the number that
  stopped them. One row per paused resident, inserted conditionally, which is what makes
  "exactly one knock at the door" true rather than hoped for.
- ``watchdog_attempts`` / ``watchdog_passes`` / ``unbracketed_runs`` — what the watchdog
  has already done, so a restart budget and a "run never reported back" complaint are
  each spent exactly once however often the watchdog ticks.
- ``open_runs`` — one row per session steward has started and not yet closed. The
  watchdog reads this to find runs that never reported back, instead of reading the
  undelivered-event log, which can only answer for the host that fired them and only
  while burrow was unreachable (steward #39). It is also where a session's own scoped
  credential lives, as a digest: the run's lease is the credential's expiry, so
  :meth:`Store.session_principal` accepts one exactly while the watchdog could not yet
  bury the run (steward #41).
- ``resident_claims`` — one row per resident saying which process is currently running a
  session for it. The scheduler's overlap guard used to be an in-process lock, which the
  API, the board and a chat daemon could not see; this is the same guard in the one place
  every firing process can read (warren#111). :mod:`steward.claims` owns what it means.
"""

from collections.abc import Sequence
from datetime import datetime, timedelta
from typing import Any

from steward.claims import ResidentClaim
from steward.events import utc_now_iso
from steward.letter_replies import bounded_message
from steward.operator_auth import OperatorPrincipal
from steward.runs import (
    DELIVERY_STATUSES,
    RUN_DELEGATED,
    RUN_ROUTINE,
    RUN_TASK,
    DeliveryStatus,
    validate_kind_trigger,
)
from steward.session_auth import SessionPrincipal, credential_digest
from steward.store._connection import _Connection
from steward.store.records import (
    ORIGIN_UNATTRIBUTED,
    STATUS_CLAIMED,
    STATUS_OPEN,
    JobRecord,
    LedgerEntry,
    OpenRun,
    OperatorRecord,
    OriginSpend,
    PauseRecord,
    WatchdogAttempt,
    _dumps,
    _resident_claim,
    new_id,
)


class _AtomicTaskCloseLostError(Exception):
    """Rollback sentinel for a task/run close that lost either conditional write."""


class _LegacyTables(_Connection):
    """Remaining table families awaiting the second extraction PR."""

    def chat_recipient(self, bot: str, conversation: str) -> str | None:
        """Read the resident last explicitly addressed in a shared bot conversation."""
        with self._lock:
            row = self._conn.execute(
                "SELECT resident_uid FROM chat_recipients WHERE bot = ? AND conversation = ?",
                (bot, conversation),
            ).fetchone()
        return row["resident_uid"] if row is not None else None

    def select_chat_recipient(self, bot: str, conversation: str, resident_uid: str) -> None:
        """Remember a shared conversation's recipient across daemon restarts."""
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO chat_recipients (bot, conversation, resident_uid) VALUES (?, ?, ?) "
                "ON CONFLICT (bot, conversation) DO UPDATE "
                "SET resident_uid = excluded.resident_uid",
                (bot, conversation, resident_uid),
            )

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

    # -- the run ledger ----------------------------------------------------------------

    def record_run(  # noqa: PLR0913 — one keyword per column of the entry
        self,
        *,
        resident: str,
        agent_id: str,
        kind: str,
        run_id: str,
        trigger: str = "",
        ref: str = "",
        origin: str = "",
        outcome: str = "",
        input_tokens: int = 0,
        output_tokens: int = 0,
        cost_usd: float = 0.0,
        duration_s: float = 0.0,
        usage_known: bool = True,
        now: str | None = None,
    ) -> LedgerEntry:
        """Append what one finished session cost. Append-only, and never revised.

        Every run gets a row, including a failed one and one steward killed at its
        timeout: a session that burned four minutes and produced nothing still burned
        four minutes, and a budget that only counted successes would be a budget that
        rewards crashing.

        ``origin`` is what the run descends from, recorded here rather than reconstructed
        later: a caller that knows the chain says so, and the row stops depending on a
        join that can only guess.
        """
        validate_kind_trigger(kind, trigger)
        entry = LedgerEntry(
            entry_id=new_id(),
            resident=resident,
            agent_id=agent_id,
            kind=kind,
            trigger=trigger,
            run_id=run_id,
            ref=ref,
            origin=origin,
            outcome=outcome,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cost_usd=cost_usd,
            duration_s=duration_s,
            usage_known=usage_known,
            recorded_at=now or utc_now_iso(),
        )
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO run_ledger (entry_id, resident, agent_id, kind, trigger, run_id, ref, "
                "origin, outcome, input_tokens, output_tokens, cost_usd, duration_s, "
                "usage_known, recorded_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    entry.entry_id,
                    entry.resident,
                    entry.agent_id,
                    entry.kind,
                    entry.trigger,
                    entry.run_id,
                    entry.ref,
                    entry.origin,
                    entry.outcome,
                    entry.input_tokens,
                    entry.output_tokens,
                    entry.cost_usd,
                    entry.duration_s,
                    int(entry.usage_known),
                    entry.recorded_at,
                ),
            )
        return entry

    def ledger(
        self,
        resident: str | None = None,
        *,
        since: str | None = None,
        until: str | None = None,
    ) -> list[LedgerEntry]:
        """Return ledger entries, oldest first, optionally within a half-open window.

        ``since``/``until`` are protocol timestamps and the window is ``[since, until)``,
        so two adjacent days can never both count the same run. The window is computed by
        the caller at *read* time from real calendar arithmetic — nothing here is reset,
        rolled over, or zeroed by a process starting up.
        """
        clauses: list[str] = []
        params: list[str] = []
        if resident is not None:
            clauses.append("resident = ?")
            params.append(resident)
        if since is not None:
            clauses.append("recorded_at >= ?")
            params.append(since)
        if until is not None:
            clauses.append("recorded_at < ?")
            params.append(until)
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        with self._lock:
            rows = self._conn.execute(
                f"SELECT * FROM run_ledger{where} ORDER BY recorded_at, rowid",  # noqa: S608
                params,
            ).fetchall()
        return [LedgerEntry.from_row(row) for row in rows]

    def spend_by_origin(
        self,
        *,
        since: str | None = None,
        until: str | None = None,
    ) -> list[OriginSpend]:
        """Roll today's ledger up by the origin each run's task descends from.

        The question delegation's ``origin`` column was recorded to answer: *what did the
        fleet spend answering one question?* A chain rolls up to its root — every hop
        inherits the same origin (:func:`steward.delegation.origin_for`) — so the cost of
        an answer stays with the answer rather than scattering across whichever residents
        happened to be in the line.

        The origin is read off the ledger row itself, written there by whoever recorded
        the run. It used to be inferred by joining ``run_ledger.ref`` to ``jobs.task_id``,
        which is only a task id for board and delegated runs — a routine whose ref
        collided with a task's id inherited that task's bill. The join survives for rows
        written before the column existed, and nothing else.

        A run that descends from nobody — one recorded without an origin, on an old
        database — is reported under :data:`ORIGIN_UNATTRIBUTED` rather than dropped:
        money steward cannot attribute is still money somebody spent.
        """
        clauses: list[str] = []
        params: list[str] = []
        if since is not None:
            clauses.append("l.recorded_at >= ?")
            params.append(since)
        if until is not None:
            clauses.append("l.recorded_at < ?")
            params.append(until)
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        with self._lock:
            rows = self._conn.execute(
                "SELECT COALESCE(NULLIF(l.origin, ''), NULLIF(j.origin, ''), ?) AS origin, "  # noqa: S608
                "COUNT(*) AS runs, "
                "SUM(l.cost_usd) AS cost_usd, "
                "SUM(l.input_tokens + l.output_tokens) AS tokens, "
                "SUM(l.duration_s) AS duration_s "
                "FROM run_ledger l LEFT JOIN jobs j ON j.task_id = l.ref"
                # Grouped by ordinal, not by name: both joined tables have an ``origin``
                # column now, so ``GROUP BY origin`` is ambiguous rather than the alias.
                f"{where} GROUP BY 1 ORDER BY cost_usd DESC, 1",
                (ORIGIN_UNATTRIBUTED, *params),
            ).fetchall()
        return [
            OriginSpend(
                origin=row["origin"],
                runs=row["runs"],
                cost_usd=row["cost_usd"] or 0.0,
                tokens=row["tokens"] or 0,
                duration_s=row["duration_s"] or 0.0,
            )
            for row in rows
        ]

    # -- budget pauses -------------------------------------------------------------------

    def pause_resident(  # noqa: PLR0913 — one keyword per column of the pause
        self,
        *,
        resident: str,
        agent_id: str,
        budget: str,
        spent: float,
        cap: float,
        reason: str = "",
        request_id: str | None = None,
        window_end: str = "",
        now: str | None = None,
    ) -> tuple[PauseRecord, bool]:
        """Stop firing this resident. Returns the pause and whether *this* call made it.

        ``INSERT … ON CONFLICT DO NOTHING`` is the whole dedupe: the first refusal writes
        the row and knocks at the door, and every later refusal — the next scheduled fire,
        the next board sweep, a run-now — reads back the same row and stays quiet. One
        knock per pause, not one per refused fire, without a flag anybody has to remember
        to clear.
        """
        moment = now or utc_now_iso()
        with self._lock, self._conn:
            cursor = self._conn.execute(
                "INSERT INTO budget_pauses (resident, agent_id, budget, spent, cap, reason, "
                "request_id, window_end, paused_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(resident) DO NOTHING",
                (resident, agent_id, budget, spent, cap, reason, request_id, window_end, moment),
            )
            created = cursor.rowcount == 1
            row = self._conn.execute(
                "SELECT * FROM budget_pauses WHERE resident = ?", (resident,)
            ).fetchone()
        return PauseRecord.from_row(row), created

    def budget_pause(self, resident: str) -> PauseRecord | None:
        """Return this resident's pause, or ``None`` when it is free to run."""
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM budget_pauses WHERE resident = ?", (resident,)
            ).fetchone()
        return PauseRecord.from_row(row) if row else None

    def budget_pauses(self) -> list[PauseRecord]:
        """Return every paused resident, oldest pause first. The 'who is stopped' view."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM budget_pauses ORDER BY paused_at, resident"
            ).fetchall()
        return [PauseRecord.from_row(row) for row in rows]

    def pause_for_request(self, request_id: str) -> PauseRecord | None:
        """Return the pause a given approval request was raised for, if there is one."""
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM budget_pauses WHERE request_id = ?", (request_id,)
            ).fetchone()
        return PauseRecord.from_row(row) if row else None

    def unpause_resident(self, resident: str) -> PauseRecord | None:
        """Lift a pause and return what it was. ``None`` when nothing was paused.

        Deleting rather than flagging, because a lifted pause is not a fact about the
        resident any more — what it *cost* is still in the ledger, which is where the
        history belongs.
        """
        with self._lock, self._conn:
            row = self._conn.execute(
                "SELECT * FROM budget_pauses WHERE resident = ?", (resident,)
            ).fetchone()
            if row is None:
                return None
            self._conn.execute("DELETE FROM budget_pauses WHERE resident = ?", (resident,))
        return PauseRecord.from_row(row)

    def grant_budget_allowance(
        self,
        resident: str,
        *,
        until: str,
        granted_by: str,
        reason: str = "",
        now: str | None = None,
    ) -> None:
        """Record that a human said carry on, and until when.

        Overwrites any earlier allowance for the same resident: a second "carry on" is a
        newer answer to the same question, not a second answer to be reconciled.
        """
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO budget_allowances (resident, until, granted_by, reason, granted_at) "
                "VALUES (?, ?, ?, ?, ?) ON CONFLICT(resident) DO UPDATE SET "
                "until = excluded.until, granted_by = excluded.granted_by, "
                "reason = excluded.reason, granted_at = excluded.granted_at",
                (resident, until, granted_by, reason, now or utc_now_iso()),
            )

    def budget_allowance(self, resident: str) -> dict[str, Any] | None:
        """Return the standing "carry on" for this resident, if a human granted one."""
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM budget_allowances WHERE resident = ?", (resident,)
            ).fetchone()
        if row is None:
            return None
        return {
            "resident": row["resident"],
            "until": row["until"],
            "granted_by": row["granted_by"],
            "reason": row["reason"],
            "granted_at": row["granted_at"],
        }

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

    # -- the resident claim ----------------------------------------------------------------

    def claim_resident(  # noqa: PLR0913 — one parameter per fact the claim records
        self,
        resident_id: str,
        *,
        token: str,
        holder: str = "",
        kind: str = "",
        ref: str = "",
        run_id: str = "",
        stale_before: str,
        now: str | None = None,
    ) -> ResidentClaim | None:
        """Take this resident's one live-session claim, or return ``None``.

        What a claim *means* is :mod:`steward.claims`; this is the write that makes it true.
        One statement, so the check and the take cannot be separated by another process.
        The upsert's ``WHERE`` is the whole guard: a claim may be taken only when the last
        one was given back, or when its holder stopped saying it was alive before
        ``stale_before``. A live claim leaves the row exactly as it is — ``rowcount`` is 0,
        nothing is overwritten, and the caller reads back who holds it.

        The row is updated rather than replaced so the resident's ``PRIMARY KEY`` is what
        makes the claim exclusive; there is no window where two rows exist for one resident.
        """
        moment = now or utc_now_iso()
        with self._lock, self._conn:
            cursor = self._conn.execute(
                "INSERT INTO resident_claims (resident_id, token, holder, kind, ref, run_id, "
                "claimed_at, heartbeat_at, released_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL) "
                "ON CONFLICT(resident_id) DO UPDATE SET token = excluded.token, "
                "holder = excluded.holder, kind = excluded.kind, ref = excluded.ref, "
                "run_id = excluded.run_id, claimed_at = excluded.claimed_at, "
                "heartbeat_at = excluded.heartbeat_at, released_at = NULL "
                "WHERE resident_claims.released_at IS NOT NULL "
                "OR resident_claims.heartbeat_at <= ?",
                (resident_id, token, holder, kind, ref, run_id, moment, moment, stale_before),
            )
            if cursor.rowcount != 1:
                return None
            row = self._conn.execute(
                "SELECT * FROM resident_claims WHERE resident_id = ?", (resident_id,)
            ).fetchone()
        return _resident_claim(row)

    def renew_resident_claim(self, resident_id: str, *, token: str, now: str | None = None) -> bool:
        """Stamp a claim's heartbeat. ``False`` means this token no longer holds it.

        Fenced on the token, like every other write here: a holder that was declared dead
        and reclaimed must not be able to keep the *new* holder's claim alive under its own
        name, which is precisely what an unfenced ``UPDATE … WHERE resident_id`` would do.
        """
        with self._lock, self._conn:
            cursor = self._conn.execute(
                "UPDATE resident_claims SET heartbeat_at = ? WHERE resident_id = ? "
                "AND token = ? AND released_at IS NULL",
                (now or utc_now_iso(), resident_id, token),
            )
            return cursor.rowcount == 1

    def release_resident_claim(
        self, resident_id: str, *, token: str, now: str | None = None
    ) -> bool:
        """Give a claim back. ``False`` means somebody else already holds this resident."""
        with self._lock, self._conn:
            cursor = self._conn.execute(
                "UPDATE resident_claims SET released_at = ? WHERE resident_id = ? "
                "AND token = ? AND released_at IS NULL",
                (now or utc_now_iso(), resident_id, token),
            )
            return cursor.rowcount == 1

    def resident_claim(self, resident_id: str) -> ResidentClaim | None:
        """Return the last claim recorded for a resident, live or not.

        Whether it still *holds* is :meth:`steward.claims.ResidentClaim.live_at`'s question,
        and it needs a cutoff this layer has no business inventing. A released or stale row
        is kept and handed back because it is the answer to "what ran here last".
        """
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM resident_claims WHERE resident_id = ?", (resident_id,)
            ).fetchone()
        return _resident_claim(row) if row is not None else None

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

    # -- operator credentials (warren#225) ---------------------------------------------

    def mint_operator(
        self,
        *,
        name: str,
        email: str,
        credential: str,
        note: str = "",
        now: str | None = None,
    ) -> OperatorRecord:
        """Record a named operator credential. Raises :class:`ValueError` for a live name.

        Only the digest is stored — the caller has just generated the plaintext and is
        about to print it once, and this method is deliberately unable to hand it back.

        A name is refused rather than silently rotated. Re-minting in place would leave the
        person holding the old credential with no way to tell it had stopped working, and
        "revoke, then mint" says the same thing in two steps that each leave a stamp.
        """
        moment = now or utc_now_iso()
        record = OperatorRecord(
            name=name,
            email=email,
            digest=credential_digest(credential),
            note=note,
            issued_at=moment,
        )
        if not record.digest:
            raise ValueError("an operator credential cannot be empty")
        with self._lock, self._conn:
            existing = self._conn.execute(
                "SELECT revoked_at FROM operator_credentials WHERE name = ?", (name,)
            ).fetchone()
            if existing is not None and existing["revoked_at"] is None:
                raise ValueError(f"operator {name!r} already holds a live credential")
            # A revoked name may be minted again: the stamp on the old row is the audit
            # trail, and REPLACE keeps the primary key honest without a second table.
            self._conn.execute(
                "INSERT OR REPLACE INTO operator_credentials "
                "(name, email, digest, note, issued_at, revoked_at) VALUES (?, ?, ?, ?, ?, NULL)",
                (record.name, record.email, record.digest, record.note, record.issued_at),
            )
        return record

    def revoke_operator(self, name: str, *, now: str | None = None) -> OperatorRecord | None:
        """Stamp an operator's credential dead. ``None`` when there was no live one.

        Conditional on ``revoked_at IS NULL``, so the answer is about *this* call and a
        second revocation does not move the moment the first one recorded.
        """
        moment = now or utc_now_iso()
        with self._lock, self._conn:
            cursor = self._conn.execute(
                "UPDATE operator_credentials SET revoked_at = ? "
                "WHERE name = ? AND revoked_at IS NULL",
                (moment, name),
            )
            if cursor.rowcount != 1:
                return None
            row = self._conn.execute(
                "SELECT * FROM operator_credentials WHERE name = ?", (name,)
            ).fetchone()
        return OperatorRecord.from_row(row)

    def operators(self, *, live_only: bool = False) -> list[OperatorRecord]:
        """List operator credentials, oldest first. Revoked ones are included by default."""
        clause = " WHERE revoked_at IS NULL" if live_only else ""
        with self._lock:
            rows = self._conn.execute(
                f"SELECT * FROM operator_credentials{clause} ORDER BY issued_at, name"  # noqa: S608
            ).fetchall()
        return [OperatorRecord.from_row(row) for row in rows]

    def operator_principal(self, credential: str) -> OperatorPrincipal | None:
        """Return who an operator credential is, or ``None`` if it is not a live one.

        Looked up by digest, so the plaintext is never compared against anything on disk,
        and gated on ``revoked_at IS NULL``, which is the whole of what revocation means:
        there is no cache, no session, and nothing to expire. An empty credential is
        refused before the query — a row can never store the empty digest, but a query
        that would match one is a query worth not writing.
        """
        digest = credential_digest(credential)
        if not digest:
            return None
        with self._lock:
            row = self._conn.execute(
                "SELECT name, email FROM operator_credentials "
                "WHERE digest = ? AND revoked_at IS NULL",
                (digest,),
            ).fetchone()
        if row is None:
            return None
        return OperatorPrincipal(name=row["name"], email=row["email"])

    # -- what actually ran (warren#104) ------------------------------------------------

    def latest_routine_runs(self) -> dict[str, LedgerEntry]:
        """Return the newest recorded run of each routine, keyed ``<resident>/<routine>``.

        The answer to warren#104. The routine ledger used to report ``last_request`` and
        nothing else, which is the *API request log*: a run somebody asked for over HTTP
        leaves a row there, and a run the scheduler fired on its own schedule does not.
        An operator reading that panel concluded a healthy resident "only runs when I
        trigger it manually", which was false.

        This reads the run ledger instead, which every finished session writes to whatever
        started it, so the answer carries the trigger (``schedule`` or ``manual``) and the
        outcome. The two facts stay side by side rather than one replacing the other: they
        are different questions, and a request that was accepted and never ran is exactly
        the case where the difference matters.

        The bare-column-with-``MAX`` form is SQLite's documented idiom for "the whole row
        at the maximum", and ``recorded_at`` is a sortable protocol timestamp, so this is
        one indexed pass rather than a full ledger read and a fold in Python.
        """
        with self._lock:
            rows = self._conn.execute(
                "SELECT *, MAX(recorded_at) FROM run_ledger "
                "WHERE kind = ? AND ref <> '' GROUP BY resident, ref",
                (RUN_ROUTINE,),
            ).fetchall()
        return {f"{row['resident']}/{row['ref']}": LedgerEntry.from_row(row) for row in rows}

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
