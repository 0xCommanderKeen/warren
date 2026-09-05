"""One row per finished session, with the tokens, money, and seconds it cost.

This is what makes a daily budget survive a daemon restart.
"""

from steward.events import utc_now_iso
from steward.runs import (
    RUN_ROUTINE,
    validate_kind_trigger,
)
from steward.store._connection import _Connection
from steward.store.records import (
    ORIGIN_UNATTRIBUTED,
    LedgerEntry,
    OriginSpend,
    new_id,
)


class _LedgerTables(_Connection):
    """Ledger table operations on the shared connection."""

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
