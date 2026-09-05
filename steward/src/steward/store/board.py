"""Posted work, with the status the board reports.

A delegated item is a task addressed to somebody rather than to the fleet.
"""

from collections.abc import Iterable, Sequence

from steward.events import utc_now_iso
from steward.letter_replies import ANSWER_BATCH_MAX_CHARS, bounded_message, render_answer
from steward.store._connection import _Connection
from steward.store.records import (
    STATUS_CLAIMED,
    STATUS_DONE,
    STATUS_FAILED,
    STATUS_OPEN,
    JobRecord,
    _dumps,
    new_id,
)


class _BoardTables(_Connection):
    """Board table operations on the shared connection."""

    # -- jobs ------------------------------------------------------------------------

    def post_job(
        self,
        *,
        title: str,
        detail: str = "",
        required_skills: Sequence[str] = (),
        posted_by: str = "api",
        task_id: str | None = None,
    ) -> JobRecord:
        """Record a task on the board and return it, open and unclaimed."""
        record = JobRecord(
            task_id=task_id or new_id(),
            title=title,
            detail=detail,
            required_skills=tuple(required_skills),
            status=STATUS_OPEN,
            posted_by=posted_by,
            created_at=utc_now_iso(),
        )
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO jobs (task_id, title, detail, required_skills, status, "
                "posted_by, claimant, created_at) VALUES (?, ?, ?, ?, ?, ?, NULL, ?)",
                (
                    record.task_id,
                    record.title,
                    record.detail,
                    _dumps(list(record.required_skills)),
                    record.status,
                    record.posted_by,
                    record.created_at,
                ),
            )
        return record

    def delegate_job(  # noqa: PLR0913 — one keyword per column of the record
        self,
        *,
        title: str,
        assignee: str,
        delegated_by: str,
        route: str,
        detail: str = "",
        parent_task_id: str | None = None,
        origin: str | None = None,
        depth: int = 1,
        task_id: str | None = None,
    ) -> JobRecord:
        """Record work handed to one named resident, and return it, open and unclaimed.

        The same table the board uses, because a delegated item *is* a task — it is worked
        as a session, leased, and closed with the same three events. The only difference
        is the addressee: an item with an ``assignee`` is never offered to the open board
        (:meth:`claim_next_job` skips it), and only that resident may pick it up.

        Nothing is validated here. Whether the sender may delegate, whether the receiver's
        route accepts the work, how deep the chain is and whether it loops are all
        steward's questions, answered in :mod:`steward.delegation` before this is called —
        the store records facts and refuses none.
        """
        record = JobRecord(
            task_id=task_id or new_id(),
            title=title,
            detail=detail,
            required_skills=(),
            status=STATUS_OPEN,
            posted_by=delegated_by,
            created_at=utc_now_iso(),
            assignee=assignee,
            delegated_by=delegated_by,
            route=route,
            parent_task_id=parent_task_id,
            origin=origin,
            depth=depth,
        )
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO jobs (task_id, title, detail, required_skills, status, "
                "posted_by, claimant, created_at, assignee, delegated_by, route, "
                "parent_task_id, origin, depth) "
                "VALUES (?, ?, ?, '[]', ?, ?, NULL, ?, ?, ?, ?, ?, ?, ?)",
                (
                    record.task_id,
                    record.title,
                    record.detail,
                    record.status,
                    record.posted_by,
                    record.created_at,
                    record.assignee,
                    record.delegated_by,
                    record.route,
                    record.parent_task_id,
                    record.origin,
                    record.depth,
                ),
            )
        return record

    def jobs(self, status: str | None = None) -> list[JobRecord]:
        """Return the board, oldest first, optionally narrowed to one status."""
        query = "SELECT * FROM jobs"
        params: tuple[str, ...] = ()
        if status is not None:
            query += " WHERE status = ?"
            params = (status,)
        with self._lock:
            rows = self._conn.execute(f"{query} ORDER BY created_at, rowid", params).fetchall()
        return [JobRecord.from_row(row) for row in rows]

    def inbox(self, assignee: str, status: str | None = STATUS_OPEN) -> list[JobRecord]:
        """Return one resident's delegated items, oldest first.

        The default is the pending inbox — what is waiting to be picked up. ``status=None``
        is everything ever addressed to this resident, which is the audit view.
        """
        query = "SELECT * FROM jobs WHERE assignee = ?"
        params: tuple[str, ...] = (assignee,)
        if status is not None:
            query += " AND status = ?"
            params = (assignee, status)
        with self._lock:
            rows = self._conn.execute(f"{query} ORDER BY created_at, rowid", params).fetchall()
        return [JobRecord.from_row(row) for row in rows]

    def inbox_count(self, assignee: str, status: str | None = STATUS_OPEN) -> int:
        """Return how many items sit in one resident's inbox, without reading them.

        ``doctor`` and the console want the size of the pile, not the letters — a count is
        the whole answer there, and reading every row to call ``len`` on it is a page of
        work to print one number.
        """
        query = "SELECT COUNT(*) FROM jobs WHERE assignee = ?"
        params: tuple[str, ...] = (assignee,)
        if status is not None:
            query += " AND status = ?"
            params = (assignee, status)
        with self._lock:
            row = self._conn.execute(query, params).fetchone()
        return int(row[0])

    def lineage(self, task_id: str) -> list[JobRecord]:
        """Return the whole chain this task belongs to: its root and every descendant.

        Depth-first from the root, oldest sibling first, so rendering by
        :attr:`JobRecord.depth` reads as the tree it is.

        The chain is only ever *written* upwards — a row records the parent it came from —
        so the root is found by walking ``parent_task_id`` up from the named task. But the
        answer has to be the same whichever member of the chain is named, and it was not:
        walking up alone returned the root by itself, reporting "nothing was delegated" for
        work that had in fact fanned out. That is the id operators actually hold, because
        ``POST /delegate`` hands the root back, so the audit query was wrong on exactly its
        commonest input (steward #202). The walk up is therefore followed by a walk down
        over the same column.

        Both walks stop on an id already seen: a database somebody hand-edited into a loop
        is a corrupt database, not an infinite loop in a CLI. A task nobody delegated, and
        who delegated to nobody, is a chain of one — a real answer, not an error.
        """
        path = self.ancestry(task_id)
        if not path:
            return []
        root = path[0]
        chain: list[JobRecord] = []
        seen: set[str] = {root.task_id}
        stack: list[JobRecord] = [root]
        while stack:
            item = stack.pop()
            chain.append(item)
            children = [kid for kid in self._children(item.task_id) if kid.task_id not in seen]
            seen.update(kid.task_id for kid in children)
            stack.extend(reversed(children))
        return chain

    def ancestry(self, task_id: str) -> list[JobRecord]:
        """Return the path this task actually travelled: root first, ending at the task.

        The hops a piece of work has already been through, and deliberately *not*
        :meth:`lineage`. The delegation cycle guard asks which residents this task has
        already passed through, and branches delegated out of a shared parent are not on
        its path — answering that with the whole tree would refuse a manager's second
        letter to a worker its first letter had already reached (steward #202).

        A dangling parent — a row whose parent was deleted out from under it — ends the
        walk at the highest row that does exist, which is the most of the path there is
        left to tell. An empty list means the board has never heard of ``task_id``.
        """
        path: list[JobRecord] = []
        seen: set[str] = set()
        cursor: str | None = task_id
        while cursor is not None and cursor not in seen:
            seen.add(cursor)
            record = self.job(cursor)
            if record is None:
                break
            path.append(record)
            cursor = record.parent_task_id
        return list(reversed(path))

    def _children(self, task_id: str) -> list[JobRecord]:
        """Return the tasks delegated directly out of this one, oldest first.

        ``rowid`` breaks the tie rather than ``task_id``: two letters written in the same
        second share a ``created_at``, and ordering those by a random uuid would shuffle
        siblings around between one run of the audit query and the next.
        """
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM jobs WHERE parent_task_id = ? ORDER BY created_at, rowid",
                (task_id,),
            ).fetchall()
        return [JobRecord.from_row(row) for row in rows]

    def job(self, task_id: str) -> JobRecord | None:
        """Return one task, or ``None`` when the board has never heard of it."""
        with self._lock:
            row = self._conn.execute("SELECT * FROM jobs WHERE task_id = ?", (task_id,)).fetchone()
        return JobRecord.from_row(row) if row else None

    def claim_next_job(
        self,
        *,
        claimant: str,
        skills: Iterable[str],
        lease_expires_at: str,
        lease_duration_s: float | None = None,
        now: str | None = None,
    ) -> JobRecord | None:
        """Atomically claim the oldest open task this claimant is qualified for.

        The whole promise of the board lives in one statement::

            UPDATE jobs SET status='claimed', claimant=?, lease_expires_at=?
            WHERE task_id=? AND status='open'

        ``rowcount == 0`` means somebody else got there first — SQLite serialised the two
        writes and this caller lost. Losing is not an error and it is not retried against
        the same row: the loop simply moves to the next candidate, so two residents waking
        in the same millisecond end up holding two different tasks, or one task and
        nothing, but never the same task twice.

        Skill matching happens in Python because ``required_skills`` is a JSON list, but
        it happens *before* the conditional update and is re-checked against the row the
        update touched, so a task can never be claimed by a resident that lacks a skill.

        Work addressed to somebody — a delegated item, with an ``assignee`` — is not on
        the open board and is never returned here, however well the skills match. Reading
        another villager's letter off the notice board is not claiming.
        """
        held = frozenset(skills)
        moment = now or utc_now_iso()
        with self._lock, self._conn:
            candidates = self._conn.execute(
                "SELECT * FROM jobs WHERE status = ? AND assignee IS NULL "
                "ORDER BY created_at, rowid",
                (STATUS_OPEN,),
            ).fetchall()
            for row in candidates:
                record = JobRecord.from_row(row)
                if not record.claimable_by <= held:
                    continue
                cursor = self._conn.execute(
                    "UPDATE jobs SET status = ?, claimant = ?, claimed_at = ?, "
                    "lease_expires_at = ?, lease_duration_s = ?, run_id = NULL, "
                    "owner_token = NULL WHERE task_id = ? AND status = ?",
                    (
                        STATUS_CLAIMED,
                        claimant,
                        moment,
                        lease_expires_at,
                        lease_duration_s,
                        record.task_id,
                        STATUS_OPEN,
                    ),
                )
                if cursor.rowcount == 0:
                    continue  # Lost the race for this row; try the next open task.
                claimed = self._conn.execute(
                    "SELECT * FROM jobs WHERE task_id = ?", (record.task_id,)
                ).fetchone()
                return JobRecord.from_row(claimed)
        return None

    def claim_next_delegated(
        self,
        *,
        assignee: str,
        claimant: str,
        lease_expires_at: str,
        lease_duration_s: float | None = None,
        now: str | None = None,
    ) -> JobRecord | None:
        """Atomically pick up the oldest item waiting in one resident's inbox.

        The board's conditional write, narrowed by the addressee::

            UPDATE jobs SET status='claimed' … WHERE task_id=? AND status='open'
                AND assignee=?

        No skill matching: the sender named this resident and this resident's own manifest
        declares a route that accepts the work, which is the whole of the agreement. A
        second skills veto here would let steward silently drop a letter both ends said yes
        to, and a dropped letter is the one thing an inbox may not do.

        ``claimant`` is the burrow agent id the pickup is recorded and emitted under;
        ``assignee`` is the resident id the item was addressed to.
        """
        moment = now or utc_now_iso()
        with self._lock, self._conn:
            candidates = self._conn.execute(
                "SELECT * FROM jobs WHERE status = ? AND assignee = ? ORDER BY created_at, rowid",
                (STATUS_OPEN, assignee),
            ).fetchall()
            for row in candidates:
                record = JobRecord.from_row(row)
                cursor = self._conn.execute(
                    "UPDATE jobs SET status = ?, claimant = ?, claimed_at = ?, "
                    "lease_expires_at = ?, lease_duration_s = ?, run_id = NULL, "
                    "owner_token = NULL WHERE task_id = ? AND status = ? AND assignee = ?",
                    (
                        STATUS_CLAIMED,
                        claimant,
                        moment,
                        lease_expires_at,
                        lease_duration_s,
                        record.task_id,
                        STATUS_OPEN,
                        assignee,
                    ),
                )
                if cursor.rowcount == 0:
                    continue  # Two wake-ups of the same resident raced; one of them won.
                claimed = self._conn.execute(
                    "SELECT * FROM jobs WHERE task_id = ?", (record.task_id,)
                ).fetchone()
                return JobRecord.from_row(claimed)
        return None

    def finish_job(  # noqa: PLR0913 — one keyword per column this write touches
        self,
        task_id: str,
        *,
        status: str,
        claimant: str,
        outcome: str | None = None,
        reason: str | None = None,
        artifacts: Sequence[str] = (),
        final_message: str = "",
        lease: str | None = None,
        now: str | None = None,
    ) -> JobRecord | None:
        """Close out a claimed task. Only its own claimant may, and only once.

        Conditional on ``status = 'claimed' AND claimant = ?`` so a resident whose lease
        already expired — and whose task is open again, or held by somebody else — cannot
        come back and mark somebody else's work done.

        ``lease`` is the token :meth:`claim_next_job` / :meth:`claim_next_delegated` handed
        back (the ``claimed_at`` stamp of *this* claim). When given, the close is also
        conditional on ``claimed_at = lease``, so a session whose lease expired, was swept,
        and re-claimed — by itself or by anyone — cannot come back and close the *live*
        claim it no longer holds (steward #72). A dead handle carries the old stamp; the
        row now carries the new one, and the stale close matches nothing.
        """
        with self._lock, self._conn:
            cursor = self._conn.execute(
                "UPDATE jobs SET status = ?, outcome = ?, reason = ?, artifacts = ?, "
                "final_message = ?, "
                "finished_at = ?, lease_expires_at = NULL, run_id = NULL, owner_token = NULL "
                "WHERE task_id = ? AND status = ? AND claimant = ? "
                "AND (? IS NULL OR claimed_at = ?)",
                (
                    status,
                    outcome,
                    reason,
                    _dumps(list(artifacts)),
                    bounded_message(final_message),
                    now or utc_now_iso(),
                    task_id,
                    STATUS_CLAIMED,
                    claimant,
                    lease,
                    lease,
                ),
            )
            if cursor.rowcount == 0:
                return None
            row = self._conn.execute("SELECT * FROM jobs WHERE task_id = ?", (task_id,)).fetchone()
        return JobRecord.from_row(row)

    def claim_answered_letters(self, sender: str, now: str | None = None) -> list[JobRecord]:
        """Take terminal letters sent by one resident, marking their replies told once."""
        moment = now or utc_now_iso()
        claimed: list[JobRecord] = []
        rendered_chars = 0
        with self._lock, self._conn:
            rows = self._conn.execute(
                "SELECT * FROM jobs WHERE delegated_by = ? AND status IN (?, ?) "
                "AND reply_delivered_at IS NULL ORDER BY finished_at, rowid",
                (sender, STATUS_DONE, STATUS_FAILED),
            ).fetchall()
            for row in rows:
                rendered = render_answer(
                    title=row["title"],
                    receiver=row["assignee"] or "unknown receiver",
                    status=row["status"],
                    message=row["final_message"],
                )
                next_size = rendered_chars + (2 if claimed else 0) + len(rendered)
                if next_size > ANSWER_BATCH_MAX_CHARS:
                    break
                cursor = self._conn.execute(
                    "UPDATE jobs SET reply_delivered_at = ? "
                    "WHERE task_id = ? AND reply_delivered_at IS NULL",
                    (moment, row["task_id"]),
                )
                if cursor.rowcount == 1:
                    fresh = self._conn.execute(
                        "SELECT * FROM jobs WHERE task_id = ?", (row["task_id"],)
                    ).fetchone()
                    claimed.append(JobRecord.from_row(fresh))
                    rendered_chars = next_size
        return claimed
