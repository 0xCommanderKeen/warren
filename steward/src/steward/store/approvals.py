"""A gated action waiting on a human, and the decision it received."""

from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any

from steward.events import utc_now_iso
from steward.store._connection import _Connection
from steward.store.records import (
    APPROVAL_DECISIONS,
    DECIDED_BY_EXPIRY,
    DECIDED_BY_REPEAT,
    STATUS_PENDING,
    STATUS_RESOLVED,
    ApprovalRecord,
    _dumps,
    new_id,
)


class _ApprovalTables(_Connection):
    """Approvals table operations on the shared connection."""

    # -- approvals -------------------------------------------------------------------

    def create_approval_request(  # noqa: PLR0913 — one keyword per column of the record
        self,
        *,
        agent_id: str,
        project: str,
        action: str,
        message: str,
        resident: str = "",
        detail: Mapping[str, Any] | None = None,
        options: Sequence[str] = APPROVAL_DECISIONS,
        expires_at: str | None = None,
        request_id: str | None = None,
        denied_by: str | None = None,
    ) -> ApprovalRecord:
        """Record a gated action that is waiting on a human.

        The internal half of steward #10: a session that reaches a gated action raises it
        — through the ``<needs-human>`` block in its output or through
        ``steward approval raise`` — steward emits the structured ``needs_human``, and the
        human answers through ``POST /approvals/{request_id}``. The API only ever
        *answers* requests; it never invents one.

        ``resident`` is the manifest id, kept alongside the burrow ``agent_id`` because
        the decision has to find its way back to a directory under ``residents/`` on the
        resident's next wake-up, and an agent id is not that.

        ``denied_by``, when given, files the request already resolved as a deny rather
        than pending — a decision steward made itself, with nobody waiting on it. It is
        how the repeat-deny guard (:mod:`steward.transitions.approval`) records an ask it
        answered instead of knocking about: the row still exists, so the ledger shows
        the resident asked and what it was told, and the resident hears the answer in the
        next preamble like any other decision.
        """
        moment = utc_now_iso()
        denied = denied_by is not None
        record = ApprovalRecord(
            request_id=request_id or new_id(),
            agent_id=agent_id,
            project=project,
            action=action,
            message=message,
            detail=dict(detail or {}),
            options=tuple(options),
            status=STATUS_RESOLVED if denied else STATUS_PENDING,
            created_at=moment,
            resident=resident,
            expires_at=expires_at,
            decision="deny" if denied else None,
            decided_by=denied_by,
            decided_at=moment if denied else None,
        )
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO approvals (request_id, agent_id, project, action, message, "
                "detail, options, status, created_at, expires_at, resident, decision, "
                "decided_by, decided_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    record.request_id,
                    record.agent_id,
                    record.project,
                    record.action,
                    record.message,
                    _dumps(dict(record.detail)),
                    _dumps(list(record.options)),
                    record.status,
                    record.created_at,
                    record.expires_at,
                    record.resident,
                    record.decision,
                    record.decided_by,
                    record.decided_at,
                ),
            )
        return record

    def recent_denials(self, resident: str, action: str, since: str) -> int:
        """Count the times this resident was told no about this action since ``since``.

        A deny is ``status='resolved'`` with ``decision='deny'``, clocked by
        ``decided_at``, so both a human's deny and expiry's deny-by-default count: from
        the resident's side, "nobody answered in time" and "a person said no" are the same
        answer, and both mean the action did not happen.

        Steward's own repeat auto-denials (``decided_by='repeat'``) are **not** counted, on
        purpose. If they were, every swallowed ask would push the window out again and one
        deny would silence an action forever — a permanent ban nobody chose. The window is
        measured from a real decision.
        """
        with self._lock:
            row = self._conn.execute(
                "SELECT COUNT(*) AS denials FROM approvals WHERE resident = ? AND action = ? "
                "AND status = ? AND decision = ? AND decided_at IS NOT NULL AND decided_at >= ? "
                "AND (decided_by IS NULL OR decided_by != ?)",
                (resident, action, STATUS_RESOLVED, "deny", since, DECIDED_BY_REPEAT),
            ).fetchone()
        return int(row["denials"])

    def approval(self, request_id: str) -> ApprovalRecord | None:
        """Return one approval request, decided or not, or ``None`` if unknown."""
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM approvals WHERE request_id = ?", (request_id,)
            ).fetchone()
        return ApprovalRecord.from_row(row) if row else None

    def pending_approvals(self) -> list[ApprovalRecord]:
        """Return every request still waiting on a human, oldest first."""
        return self.approvals(status=STATUS_PENDING)

    def approvals(self, status: str | None = None) -> list[ApprovalRecord]:
        """Return approval requests, oldest first, optionally narrowed to one status.

        ``None`` is the whole ledger — the audit view, where a request and the decision
        it received sit in one row and "what did I approve, and when" has an answer.
        """
        query = "SELECT * FROM approvals"
        params: tuple[str, ...] = ()
        if status is not None:
            query += " WHERE status = ?"
            params = (status,)
        with self._lock:
            rows = self._conn.execute(f"{query} ORDER BY created_at, rowid", params).fetchall()
        return [ApprovalRecord.from_row(row) for row in rows]

    def expire_approvals(self, now: str | None = None) -> list[ApprovalRecord]:
        """Deny every pending request whose ``expires_at`` has passed, and return them.

        Deny-by-default, and it is the whole safety property: a gated action that nobody
        answered must not become a gated action that went ahead because the human was
        asleep. The recorded ``decided_by`` is ``expiry`` rather than a person, because
        pretending a human said no would be a different kind of lie.
        """
        moment = now or utc_now_iso()
        expired: list[ApprovalRecord] = []
        with self._lock, self._conn:
            rows = self._conn.execute(
                "SELECT * FROM approvals WHERE status = ? AND expires_at IS NOT NULL "
                "AND expires_at <= ? ORDER BY created_at, rowid",
                (STATUS_PENDING, moment),
            ).fetchall()
            for row in rows:
                request_id = row["request_id"]
                cursor = self._conn.execute(
                    "UPDATE approvals SET status = ?, decision = ?, decided_by = ?, "
                    "decided_at = ? WHERE request_id = ? AND status = ?",
                    (
                        STATUS_RESOLVED,
                        "deny",
                        DECIDED_BY_EXPIRY,
                        moment,
                        request_id,
                        STATUS_PENDING,
                    ),
                )
                if cursor.rowcount != 1:
                    continue  # A human answered in the same instant; their answer wins.
                decided = self._conn.execute(
                    "SELECT * FROM approvals WHERE request_id = ?", (request_id,)
                ).fetchone()
                self._conn.execute(
                    "INSERT OR IGNORE INTO approval_announcements (request_id) VALUES (?)",
                    (request_id,),
                )
                expired.append(ApprovalRecord.from_row(decided))
        return expired

    def undelivered_decisions(self, resident: str) -> list[ApprovalRecord]:
        """Return this resident's decided-but-untold requests, oldest first."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM approvals WHERE resident = ? AND status = ? "
                "AND delivered_at IS NULL ORDER BY decided_at, rowid",
                (resident, STATUS_RESOLVED),
            ).fetchall()
        return [ApprovalRecord.from_row(row) for row in rows]

    def claim_undelivered_decisions(
        self, resident: str, now: str | None = None
    ) -> list[ApprovalRecord]:
        """Take this resident's decided-but-untold decisions, marking them delivered, atomically.

        The read and the mark are one transaction under one lock, so two sessions of the
        same resident waking at the same instant cannot both walk away believing they were
        handed the same answer (steward #74). Each ``UPDATE`` is conditional on
        ``delivered_at IS NULL``, so only the records *this* call actually flips are
        returned; a concurrent caller gets the ones it flipped, or none. Told once, to one
        session — the honest reading of "the decision reached the resident".
        """
        moment = now or utc_now_iso()
        claimed: list[ApprovalRecord] = []
        with self._lock, self._conn:
            rows = self._conn.execute(
                "SELECT * FROM approvals WHERE resident = ? AND status = ? "
                "AND delivered_at IS NULL ORDER BY decided_at, rowid",
                (resident, STATUS_RESOLVED),
            ).fetchall()
            for row in rows:
                cursor = self._conn.execute(
                    "UPDATE approvals SET delivered_at = ? "
                    "WHERE request_id = ? AND delivered_at IS NULL",
                    (moment, row["request_id"]),
                )
                if cursor.rowcount == 1:
                    fresh = self._conn.execute(
                        "SELECT * FROM approvals WHERE request_id = ?", (row["request_id"],)
                    ).fetchone()
                    claimed.append(ApprovalRecord.from_row(fresh))
        return claimed

    def mark_delivered(self, request_ids: Sequence[str], now: str | None = None) -> int:
        """Record that these decisions have been put in front of the resident.

        Conditional on ``delivered_at IS NULL``, so a decision is delivered exactly once
        even if two sessions of the same resident start at the same moment. The return
        value is how many this call actually marked.
        """
        moment = now or utc_now_iso()
        marked = 0
        with self._lock, self._conn:
            for request_id in request_ids:
                cursor = self._conn.execute(
                    "UPDATE approvals SET delivered_at = ? "
                    "WHERE request_id = ? AND delivered_at IS NULL",
                    (moment, request_id),
                )
                marked += cursor.rowcount
        return marked

    def consume_approval(self, request_id: str, *, by: str, now: str | None = None) -> bool:
        """Claim this decision for one write, and report whether *this* call got it.

        The whole of "one approval is one edit" (warren#437). Conditional on
        ``consumed_at IS NULL``, so two sessions of the same resident presenting the same
        request id at the same moment cannot both be told yes — the second reads ``False``
        and is refused before it reaches the tree.

        Claimed *before* the write rather than marked after it, because the failure that
        matters is the one where the write lands and the marker does not: that leaves a
        spent decision looking unspent, which is the one direction this must never fail in.
        A write that then refuses gives the claim back through :meth:`release_approval`.

        ``by`` is the claiming request's own id, and it is what makes the release safe:
        only the claimant can give a claim back.
        """
        moment = now or utc_now_iso()
        with self._lock, self._conn:
            cursor = self._conn.execute(
                "UPDATE approvals SET consumed_at = ?, consumed_by = ? "
                "WHERE request_id = ? AND consumed_at IS NULL",
                (moment, by, request_id),
            )
        return cursor.rowcount == 1

    def release_approval(self, request_id: str, *, by: str) -> bool:
        """Give a claimed decision back, because the write it was claimed for refused.

        Narrowed to ``consumed_by = by`` so a caller can only release its own claim: a
        stale release must never re-open a decision some other write is holding.
        """
        with self._lock, self._conn:
            cursor = self._conn.execute(
                "UPDATE approvals SET consumed_at = NULL, consumed_by = '' "
                "WHERE request_id = ? AND consumed_by = ? AND consumed_at IS NOT NULL",
                (request_id, by),
            )
        return cursor.rowcount == 1

    def decide(  # noqa: PLR0913 — optional API ledger metadata joins the same transaction
        self,
        request_id: str,
        decision: str,
        *,
        decided_by: str = "api",
        edit: Mapping[str, Any] | None = None,
        now: str | None = None,
        request_log: tuple[str, str, str] | None = None,
    ) -> tuple[ApprovalRecord | None, bool]:
        """Record a decision. Returns the record and whether *this* call recorded it.

        The first decision wins. A replay — a double-tapped notification, a retried
        request — changes nothing and reads back what was already recorded, which is
        what makes the endpoint idempotent all the way down to the disk.

        An **expired** request is never decided, however it is answered (steward #66): the
        conditional write is narrowed by ``expires_at IS NULL OR expires_at > now``, so a
        human clicking *approve* a minute after the deadline cannot slip an action through
        ahead of the deny-by-default sweep. That case is distinct and readable in the
        return: ``recorded`` is ``False`` and the record comes back *still pending* (rather
        than resolved, which is what a replay of an already-decided request reads back), so
        a caller can tell "too late, it expired" from "somebody already answered". The
        sweep (:meth:`expire_approvals`) still denies it and closes the loop in the log.
        """
        moment = now or utc_now_iso()
        with self._lock, self._conn:
            existing = self._conn.execute(
                "SELECT request_id FROM approvals WHERE request_id = ?", (request_id,)
            ).fetchone()
            if existing is None:
                return None, False
            cursor = self._conn.execute(
                "UPDATE approvals SET status = ?, decision = ?, decided_by = ?, "
                "decided_at = ?, edit = ? WHERE request_id = ? AND status = ? "
                "AND (expires_at IS NULL OR expires_at > ?)",
                (
                    STATUS_RESOLVED,
                    decision,
                    decided_by,
                    moment,
                    _dumps(dict(edit)) if edit else None,
                    request_id,
                    STATUS_PENDING,
                    moment,
                ),
            )
            recorded = cursor.rowcount == 1
            if recorded:
                self._conn.execute(
                    "INSERT OR IGNORE INTO approval_announcements (request_id) VALUES (?)",
                    (request_id,),
                )
            row = self._conn.execute(
                "SELECT * FROM approvals WHERE request_id = ?", (request_id,)
            ).fetchone()
            if request_log is not None and row["status"] == STATUS_RESOLVED:
                log_id, method, path = request_log
                self._conn.execute(
                    "INSERT OR IGNORE INTO requests (request_id, received_at, method, path, "
                    "outcome, detail, approval_id) VALUES (?, ?, ?, ?, "
                    "CASE WHEN EXISTS (SELECT 1 FROM approval_announcements "
                    "WHERE request_id = ? AND effects_at IS NOT NULL) THEN 'recorded' "
                    "ELSE 'recorded_announcement_pending' END, ?, ?)",
                    (
                        log_id,
                        moment,
                        method,
                        path,
                        request_id,
                        _dumps({"approval": request_id, "decision": row["decision"]}),
                        request_id,
                    ),
                )
        return ApprovalRecord.from_row(row), recorded

    def claim_approval_announcement(
        self, request_id: str | None = None, *, lease_s: float = 30
    ) -> tuple[ApprovalRecord, str] | None:
        """Lease one unresolved approval announcement for emission.

        The decision and queue row are committed atomically.  The lease prevents two
        processes from announcing concurrently, while its deadline makes a process death
        recoverable by the next API start or replay.
        """
        token = new_id()
        now = datetime.now(UTC)
        moment = utc_now_iso(now)
        until = utc_now_iso(now + timedelta(seconds=lease_s))
        with self._lock, self._conn:
            params: tuple[str, ...] = (token, until, moment, moment)
            if request_id is not None:
                params += (request_id,)
                requested = "AND request_id = ?"
            else:
                requested = ""
            claim_sql = (
                "UPDATE approval_announcements SET claimed_by = ?, claimed_until = ? "  # noqa: S608
                "WHERE request_id = (SELECT request_id FROM approval_announcements "
                "WHERE announced_at IS NULL AND (next_attempt_at IS NULL OR next_attempt_at <= ?) "
                "AND (claimed_until IS NULL OR claimed_until <= ?) "
                + requested
                + " ORDER BY rowid LIMIT 1) AND announced_at IS NULL RETURNING request_id"
            )
            claimed = self._conn.execute(claim_sql, params).fetchone()
            if claimed is None:
                return None
            row = self._conn.execute(
                "SELECT * FROM approvals WHERE request_id = ?", (claimed["request_id"],)
            ).fetchone()
        return ApprovalRecord.from_row(row), token

    def finish_approval_announcement(self, request_id: str, token: str, *, accepted: bool) -> bool:
        """Acknowledge a claimed announcement, or release it for immediate retry."""
        with self._lock, self._conn:
            if accepted:
                cursor = self._conn.execute(
                    "UPDATE approval_announcements SET announced_at = ?, claimed_by = NULL, "
                    "claimed_until = NULL WHERE request_id = ? AND claimed_by = ? "
                    "AND announced_at IS NULL",
                    (utc_now_iso(), request_id, token),
                )
            else:
                attempts = (
                    self._conn.execute(
                        "SELECT attempts FROM approval_announcements WHERE request_id = ?",
                        (request_id,),
                    ).fetchone()["attempts"]
                    + 1
                )
                delay = min(30.0, 0.1 * (2 ** min(attempts - 1, 8)))
                cursor = self._conn.execute(
                    "UPDATE approval_announcements SET claimed_by = NULL, claimed_until = NULL, "
                    "attempts = ?, next_attempt_at = ? "
                    "WHERE request_id = ? AND claimed_by = ? AND announced_at IS NULL",
                    (
                        attempts,
                        utc_now_iso(datetime.now(UTC) + timedelta(seconds=delay)),
                        request_id,
                        token,
                    ),
                )
        return cursor.rowcount == 1

    def approval_announcement_state(self, request_id: str) -> str | None:
        """Return ``pending``, ``announced`` or ``complete`` for a queued decision."""
        with self._lock:
            row = self._conn.execute(
                "SELECT announced_at, effects_at FROM approval_announcements WHERE request_id = ?",
                (request_id,),
            ).fetchone()
        if row is None:
            return None
        if row["announced_at"] is None:
            return "pending"
        return "complete" if row["effects_at"] is not None else "announced"

    def claim_approval_effects(
        self, request_id: str | None = None, *, lease_s: float = 30
    ) -> tuple[ApprovalRecord, str] | None:
        """Lease one acknowledged decision's completion effects across processes."""
        token = new_id()
        now = datetime.now(UTC)
        moment = utc_now_iso(now)
        until = utc_now_iso(now + timedelta(seconds=lease_s))
        with self._lock, self._conn:
            requested = "AND request_id = ?" if request_id is not None else ""
            params: tuple[str, ...] = (token, until, moment, moment)
            if request_id is not None:
                params += (request_id,)
            claim_sql = (
                "UPDATE approval_announcements SET effects_claimed_by = ?, "  # noqa: S608
                "effects_claimed_until = ? WHERE request_id = (SELECT request_id FROM "
                "approval_announcements WHERE announced_at IS NOT NULL AND effects_at IS NULL "
                "AND (effects_next_attempt_at IS NULL OR effects_next_attempt_at <= ?) "
                "AND (effects_claimed_until IS NULL OR effects_claimed_until <= ?) "
                + requested
                + " ORDER BY rowid LIMIT 1) AND effects_at IS NULL RETURNING request_id"
            )
            claimed = self._conn.execute(claim_sql, params).fetchone()
            if claimed is None:
                return None
            row = self._conn.execute(
                "SELECT * FROM approvals WHERE request_id = ?", (claimed["request_id"],)
            ).fetchone()
        return ApprovalRecord.from_row(row), token

    def complete_approval_effects(
        self, record: ApprovalRecord, token: str
    ) -> tuple[bool, str | None]:
        """Apply completion and its marker in one transaction guarded by the effects lease.

        Budget approval atomically removes the pause and grants its window allowance.
        All other decisions have no completion mutation, but still atomically acquire the
        durable marker. A stale worker cannot complete a lease another process recovered.
        """
        moment = utc_now_iso()
        with self._lock, self._conn:
            owned = self._conn.execute(
                "SELECT 1 FROM approval_announcements WHERE request_id = ? "
                "AND announced_at IS NOT NULL AND effects_at IS NULL AND effects_claimed_by = ?",
                (record.request_id, token),
            ).fetchone()
            if owned is None:
                return False, None
            resumed: str | None = None
            if record.action == "budget_unpause" and record.decision == "approve":
                pause = self._conn.execute(
                    "SELECT * FROM budget_pauses WHERE request_id = ?", (record.request_id,)
                ).fetchone()
                if pause is not None:
                    resumed = pause["resident"]
                    if pause["window_end"]:
                        self._conn.execute(
                            "INSERT INTO budget_allowances "
                            "(resident, until, granted_by, reason, granted_at) "
                            "VALUES (?, ?, ?, ?, ?) "
                            "ON CONFLICT(resident) DO UPDATE SET until=excluded.until, "
                            "granted_by=excluded.granted_by, reason=excluded.reason, "
                            "granted_at=excluded.granted_at",
                            (
                                resumed,
                                pause["window_end"],
                                record.decided_by or "api",
                                pause["reason"],
                                moment,
                            ),
                        )
                    self._conn.execute(
                        "DELETE FROM budget_pauses WHERE resident = ? AND request_id = ?",
                        (resumed, record.request_id),
                    )
            cursor = self._conn.execute(
                "UPDATE approval_announcements SET effects_at = ?, effects_claimed_by = NULL, "
                "effects_claimed_until = NULL WHERE request_id = ? AND effects_at IS NULL "
                "AND effects_claimed_by = ?",
                (moment, record.request_id, token),
            )
            if cursor.rowcount == 1:
                self._conn.execute(
                    "UPDATE requests SET outcome = 'recorded' WHERE approval_id = ? "
                    "AND outcome = 'recorded_announcement_pending'",
                    (record.request_id,),
                )
        return cursor.rowcount == 1, resumed

    def release_approval_effects(self, request_id: str, token: str) -> bool:
        """Release a failed effects claim with bounded exponential backoff."""
        with self._lock, self._conn:
            row = self._conn.execute(
                "SELECT effects_attempts FROM approval_announcements WHERE request_id = ? "
                "AND effects_claimed_by = ? AND effects_at IS NULL",
                (request_id, token),
            ).fetchone()
            if row is None:
                return False
            attempts = row["effects_attempts"] + 1
            delay = min(30.0, 0.1 * (2 ** min(attempts - 1, 8)))
            cursor = self._conn.execute(
                "UPDATE approval_announcements SET effects_claimed_by=NULL, "
                "effects_claimed_until=NULL, effects_attempts=?, effects_next_attempt_at=? "
                "WHERE request_id=? AND effects_claimed_by=? AND effects_at IS NULL",
                (
                    attempts,
                    utc_now_iso(datetime.now(UTC) + timedelta(seconds=delay)),
                    request_id,
                    token,
                ),
            )
        return cursor.rowcount == 1

    def next_approval_work_at(self) -> str | None:
        """Earliest announcement/effects retry or live-lease deadline."""
        with self._lock:
            row = self._conn.execute(
                "SELECT MIN(due) AS due FROM ("
                "SELECT CASE WHEN claimed_until IS NOT NULL THEN claimed_until "
                "WHEN next_attempt_at IS NOT NULL THEN next_attempt_at ELSE ? END AS due "
                "FROM approval_announcements WHERE announced_at IS NULL UNION ALL "
                "SELECT CASE WHEN effects_claimed_until IS NOT NULL THEN effects_claimed_until "
                "WHEN effects_next_attempt_at IS NOT NULL THEN effects_next_attempt_at ELSE ? END "
                "FROM approval_announcements WHERE announced_at IS NOT NULL "
                "AND effects_at IS NULL)",
                (utc_now_iso(), utc_now_iso()),
            ).fetchone()
        return row["due"]
