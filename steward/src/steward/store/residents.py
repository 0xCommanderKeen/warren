"""One row per resident saying which process is currently running a session.

The overlap guard lives where every firing process can read it, alongside
operator credentials and the resident addressed by each chat conversation.
"""

from steward.claims import ResidentClaim
from steward.events import utc_now_iso
from steward.operator_auth import OperatorPrincipal
from steward.session_auth import credential_digest
from steward.store._connection import _Connection
from steward.store.records import (
    OperatorRecord,
    _resident_claim,
)


class _ResidentTables(_Connection):
    """Residents table operations on the shared connection."""

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
