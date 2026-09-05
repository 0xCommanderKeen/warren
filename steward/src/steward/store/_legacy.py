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

from typing import Any

from steward.claims import ResidentClaim
from steward.events import utc_now_iso
from steward.operator_auth import OperatorPrincipal
from steward.session_auth import credential_digest
from steward.store._connection import _Connection
from steward.store.records import (
    OperatorRecord,
    PauseRecord,
    _resident_claim,
)


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
