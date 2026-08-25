"""Per-resident budgets: an unattended agent that is bounded, and says when it stops.

An agent nobody is watching spends money nobody is watching. This module is the other
half of steward #8 — the watchdog keeps a resident alive, and the budget keeps it from
being alive too expensively — and it is built out of three things:

**A ledger, on disk.** Every finished session — a scheduled routine, a claimed board
task, a delegated item — appends one row to ``run_ledger`` with the tokens, the money,
and the seconds it actually cost. Append-only and never revised. A failed run and a run
steward killed at its timeout are both recorded, because a session that burned four
minutes and produced nothing still burned four minutes.

**A window computed on read, from real calendar arithmetic.** "Today" is
``[local midnight, next local midnight)`` in the resident's own primary time zone,
resolved to two UTC instants at the moment somebody asks. Nothing is zeroed, rolled over,
or reset by a process starting up: a daily cap that resets because the daemon bounced is
not a cap, it is a suggestion. Bouncing the store mid-window changes no number.

**A refusal that knocks once.** When a daily cap is exhausted the resident is *paused*: a
row in ``budget_pauses``, written with ``INSERT … ON CONFLICT DO NOTHING`` so exactly one
caller wins. That one caller raises a single structured ``needs_human`` naming the budget
and the number that tripped it, through the ordinary approvals machinery
(:mod:`steward.approvals`), and every later refusal — the next scheduled fire, the next
board sweep, a run-now — reads back the same row and stays quiet. One knock per pause,
not one per refused fire.

**The pause is lifted by a person, not by tomorrow.** Approving that ``needs_human``
resumes the resident (``POST /approvals/{id}`` with ``approve``), and so does
``steward budget unpause <id>``; both are confirmed by ``needs_human_resolved``. The next
day does *not* silently un-pause it, and that is deliberate: the window resetting is a
fact about arithmetic, but a resident that blew through the cap you set is a fact about
the resident, and the next morning does not un-know it. You looked at a number and said
carry on, or you did not.

**"Carry on" means today, not forever.** Lifting a pause records an *allowance* until the
end of the window that tripped — a fact about what a person said, with the scope they
said it in. Without it, unpausing would be theatre: the day's spend is still over the cap,
so the very next fire would re-trip and knock again, and a human answering a question
would be answering it into a loop. With it, the resident finishes its day, and tomorrow's
cap applies to tomorrow. The allowance expires by the same date arithmetic the window
does, so nothing has to remember to clear it.

``max_run_seconds`` is the one budget that is not daily. It caps a single run, enforced
as ``min(routine timeout, max_run_seconds)`` so a manifest can never declare a routine
that outlives the budget the same manifest declares. A run killed by it takes the
scheduler's existing timeout path — ``routine_failed`` / ``task_failed`` — and its
duration is ledgered like any other.

Every limit is optional and an absent one means unlimited, which this module says out
loud rather than leaving as a silence: :class:`Gauge` reports ``limit: None`` and the CLI
prints ``no limit``, so "Hob has no cap" is something somebody read.
"""

import logging
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

from steward import approvals
from steward import events as ev
from steward.approvals import NeedsHuman
from steward.manifest import CLOSE_OF_DAY, DEFAULT_SCHEDULE_TZ, ResidentManifest
from steward.runners import RunResult
from steward.store import RUN_ROUTINE, Store, new_id

if TYPE_CHECKING:  # pragma: no cover — runtime never needs the record classes by name
    from steward.store import LedgerEntry, PauseRecord

__all__ = [
    "BUDGET_ACTION",
    "COST_BUDGET",
    "PAUSED_ERROR",
    "PAUSED_MESSAGE",
    "TOKEN_BUDGET",
    "UNPAUSE_OPTIONS",
    "Allowance",
    "BudgetGuard",
    "BudgetStatus",
    "Gauge",
    "Spend",
    "Window",
    "day_window",
    "effective_timeout_s",
    "primary_tz",
]

log = logging.getLogger("steward.budgets")

#: The two daily caps, named the way they are named in the manifest and in the payload of
#: the ``needs_human`` that trips. A person reading the knock and a person reading the
#: manifest have to be looking at the same word.
COST_BUDGET = "daily_cost_usd"
TOKEN_BUDGET = "daily_tokens"  # noqa: S105 — a budget name, not a credential

#: The action a budget pause is raised under. A human answering ``approve`` resumes the
#: resident; ``deny`` leaves it paused, which is a real answer and not a no-op.
BUDGET_ACTION = "budget_unpause"
UNPAUSE_OPTIONS = ("approve", "deny")

#: The refusal a paused resident gets, everywhere it gets refused. The API turns it into
#: a 409 with this ``error`` key, and the scheduler logs it as the skip reason.
PAUSED_ERROR = "budget_exceeded"
PAUSED_MESSAGE = "paused: budget exceeded"


# --------------------------------------------------------------------------------------
# where a day happens
# --------------------------------------------------------------------------------------


def primary_tz(manifest: ResidentManifest) -> str:
    """Return the time zone this resident's *day* is counted in.

    A resident's routines may each declare their own ``schedule_tz``, but a *daily* budget
    needs one answer to "when does today end", and steward picks it in this order:

    1. **The zone of the routine flagged ``journal: close_of_day``.** That routine already
       decides which calendar day the resident's journal entry is dated in
       (:func:`steward.journal.local_day`), and a resident whose day ends at 22:30
       Europe/Ljubljana should have its budget end there too. One resident, one day.
    2. **The most common ``schedule_tz`` among its enabled routines**, ties broken by
       declaration order, for a resident that has not flagged a closing routine.
    3. **UTC**, for a resident with no routines at all — a board-only resident, say. It is
       the same default ``schedule_tz`` itself has, so nothing here invents a zone.

    Disabled routines are ignored on purpose: a routine that is switched off is not part
    of the rhythm of this resident's day.
    """
    closer = next(
        (r for r in manifest.routines if r.enabled and r.journal == CLOSE_OF_DAY),
        None,
    )
    if closer is not None:
        return closer.schedule_tz
    zones = [routine.schedule_tz for routine in manifest.routines if routine.enabled]
    if not zones:
        return DEFAULT_SCHEDULE_TZ
    return max(zones, key=lambda zone: (zones.count(zone), -zones.index(zone)))


@dataclass(frozen=True, slots=True)
class Window:
    """One local day, as the two UTC instants a ledger query is actually run against."""

    tz: str
    start: datetime
    end: datetime

    @property
    def start_iso(self) -> str:
        """The inclusive start of the window, as a protocol timestamp."""
        return ev.utc_now_iso(self.start)

    @property
    def end_iso(self) -> str:
        """The exclusive end of the window, as a protocol timestamp."""
        return ev.utc_now_iso(self.end)

    @property
    def day(self) -> str:
        """The calendar date this window is, in its own zone: ``YYYY-MM-DD``."""
        return self.start.astimezone(ZoneInfo(self.tz)).date().isoformat()

    def to_dict(self) -> dict[str, Any]:
        """Return the JSON view a fuel gauge is labelled from."""
        return {"tz": self.tz, "day": self.day, "start": self.start_iso, "end": self.end_iso}


def day_window(tz: str, now: datetime | None = None) -> Window:
    """Return the local day ``now`` falls in, half-open, as UTC instants.

    Computed here and nowhere else, at the moment of the question. That is the whole
    reason a restart cannot move a budget: there is no stored counter to survive, only
    rows with timestamps and a boundary derived from the calendar every time it is asked
    for. Half-open — ``[midnight, next midnight)`` — so a run at exactly midnight belongs
    to the day it starts and to no other.

    Across a DST seam the window is genuinely 23 or 25 hours long, because the wall clock
    is what the household lives by and the budget is a promise about a day, not about
    86400 seconds.
    """
    zone = ZoneInfo(tz)
    local = (now or datetime.now(UTC)).astimezone(zone)
    # ``fold=0`` pins the *earlier* of a repeated wall-clock reading. On an autumn
    # fall-back day the local midnight (and the next one) is otherwise ambiguous, and
    # letting the fold ride in from ``now`` can convert the boundary against the wrong UTC
    # offset — shrinking the day to 24 hours and dropping the repeated hour's spend, so a
    # resident that overran during it reads as under its cap (steward #68).
    start = local.replace(hour=0, minute=0, second=0, microsecond=0, fold=0)
    end = (start + timedelta(days=1)).replace(fold=0)
    return Window(tz=tz, start=start.astimezone(UTC), end=end.astimezone(UTC))


# --------------------------------------------------------------------------------------
# what has been spent
# --------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Spend:
    """What a set of ledger entries adds up to. Sums of recorded facts, never estimates."""

    runs: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    duration_s: float = 0.0
    #: Runs whose brain did not report usage at all. ``codex`` and ``command`` runners
    #: have no usage to give, and steward counts them as zero while saying how many they
    #: were — "0.00 spent across 4 runs, 3 of which did not report" is true; "0.00 spent"
    #: on its own is a comfortable lie.
    unreported: int = 0

    @property
    def tokens(self) -> int:
        """Input plus output — what a ``daily_tokens`` budget is counted against."""
        return self.input_tokens + self.output_tokens

    def to_dict(self) -> dict[str, Any]:
        """Return the JSON view of a window's consumption."""
        return {
            "runs": self.runs,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "tokens": self.tokens,
            "cost_usd": round(self.cost_usd, 6),
            "duration_s": round(self.duration_s, 3),
            "unreported_runs": self.unreported,
        }


def total(entries: Sequence[LedgerEntry]) -> Spend:
    """Add up ledger entries into one :class:`Spend`."""
    return Spend(
        runs=len(entries),
        input_tokens=sum(entry.input_tokens for entry in entries),
        output_tokens=sum(entry.output_tokens for entry in entries),
        cost_usd=sum(entry.cost_usd for entry in entries),
        duration_s=sum(entry.duration_s for entry in entries),
        unreported=sum(1 for entry in entries if not entry.usage_known),
    )


@dataclass(frozen=True, slots=True)
class Gauge:
    """One budget, what it allows, and what has gone through it today."""

    name: str
    spent: float
    limit: float | None = None

    @property
    def unlimited(self) -> bool:
        """True when the manifest declares no cap for this budget."""
        return self.limit is None

    @property
    def exhausted(self) -> bool:
        """True when this budget has nothing left. At the limit *is* exhausted."""
        return self.limit is not None and self.spent >= self.limit

    @property
    def remaining(self) -> float | None:
        """What is left, or ``None`` when there is no cap to be left of."""
        return None if self.limit is None else max(0.0, self.limit - self.spent)

    def describe(self) -> str:
        """One line a CLI prints and a ``needs_human`` message is built from."""
        if self.limit is None:
            return f"{self.name}: {_number(self.spent)} spent, no limit"
        return f"{self.name}: {_number(self.spent)} of {_number(self.limit)}"

    def to_dict(self) -> dict[str, Any]:
        """Return the JSON view burrow's fleet-ops fuel gauge is drawn from."""
        return {
            "budget": self.name,
            "spent": self.spent,
            "limit": self.limit,
            "remaining": self.remaining,
            "exhausted": self.exhausted,
        }


def _number(value: float) -> str:
    """Render a gauge number without lying about its precision."""
    return f"{value:.2f}" if value % 1 else f"{value:.0f}"


@dataclass(frozen=True, slots=True)
class Allowance:
    """A standing "carry on" a person granted, and the moment it runs out."""

    until: datetime
    granted_by: str = ""
    granted_at: str = ""

    def covers(self, now: datetime) -> bool:
        """Report whether this allowance still stands at ``now``."""
        return now < self.until

    def to_dict(self) -> dict[str, Any]:
        """Return the JSON view of an allowance a panel shows next to a full gauge."""
        return {
            "until": ev.utc_now_iso(self.until),
            "granted_by": self.granted_by,
            "granted_at": self.granted_at,
        }


@dataclass(frozen=True, slots=True)
class BudgetStatus:
    """Everything ``GET /residents/{id}/budget`` answers, in one object."""

    resident: str
    agent_id: str
    window: Window
    spend: Spend
    gauges: tuple[Gauge, ...]
    max_run_seconds: int | None = None
    pause: PauseRecord | None = None
    #: A person's standing "carry on", when there is one covering this window.
    allowance: Allowance | None = None

    @property
    def paused(self) -> bool:
        """True while steward is refusing to fire anything for this resident."""
        return self.pause is not None

    @property
    def declared(self) -> bool:
        """True when this resident declares any daily cap at all."""
        return any(not gauge.unlimited for gauge in self.gauges)

    @property
    def tripped(self) -> Gauge | None:
        """Return the first exhausted budget, or ``None`` while there is room."""
        return next((gauge for gauge in self.gauges if gauge.exhausted), None)

    def summary(self) -> str:
        """One line for a list view: the caps, and whether the resident is stopped."""
        if self.paused:
            return PAUSED_MESSAGE
        if not self.declared:
            return "no limit"
        gauges = "; ".join(gauge.describe() for gauge in self.gauges if not gauge.unlimited)
        if self.tripped is not None and self.allowance is not None:
            # The window's own zone, not UTC: a Ljubljana resident whose day ends at local
            # midnight should read "until 00:00", not the "22:00" its UTC instant would
            # print in summer (steward #82).
            local_until = self.allowance.until.astimezone(ZoneInfo(self.window.tz))
            return f"{gauges} — over, and allowed to carry on until {local_until:%H:%M}"
        return gauges

    def to_dict(self) -> dict[str, Any]:
        """Return the JSON view. Every field is a fact, including the absent limits."""
        return {
            "resident": self.resident,
            "agent_id": self.agent_id,
            "window": self.window.to_dict(),
            "spent": self.spend.to_dict(),
            "budgets": [gauge.to_dict() for gauge in self.gauges],
            "max_run_seconds": self.max_run_seconds,
            "paused": self.paused,
            "pause": self.pause.to_dict() if self.pause is not None else None,
            "allowance": self.allowance.to_dict() if self.allowance is not None else None,
            "summary": self.summary(),
        }


def effective_timeout_s(manifest: ResidentManifest, declared_s: int) -> int:
    """Return the timeout a run actually gets: ``min(declared, max_run_seconds)``.

    The manifest may declare a fifteen-minute routine under a five-minute budget — the
    validator warns about it — and when it does, the budget wins. A cap that a routine
    could opt out of by declaring a longer timeout would not be a cap.
    """
    cap = manifest.budgets.max_run_seconds
    return declared_s if cap is None else min(declared_s, cap)


# --------------------------------------------------------------------------------------
# the guard
# --------------------------------------------------------------------------------------


class BudgetGuard:
    """Reads the ledger before a run, writes to it after, and pauses when a cap trips.

    Structural, not inherited: the resident session lifecycle holds this through its
    :class:`steward.sessions.RunGuard` protocol, so a steward built without budgets fires
    routines exactly as it did before this module existed. Every method is safe to call on
    a manifest that declares no budgets at all — :meth:`allow` returns ``None``,
    :meth:`timeout_for` returns what it was given, and :meth:`record` still ledgers, so
    "what has Hob cost me this week" is answerable before anybody sets a limit.
    """

    def __init__(self, store: Store, emitter: ev.Emitter | None = None) -> None:
        """Hold the durable ledger and the emitter a pause knocks through."""
        self.store = store
        self.emitter: ev.Emitter = emitter if emitter is not None else ev.NullEmitter()

    # -- reading -----------------------------------------------------------------------

    def status(self, manifest: ResidentManifest, now: datetime | None = None) -> BudgetStatus:
        """Return this resident's spend against its caps, for the window ``now`` is in."""
        window = day_window(primary_tz(manifest), now)
        spend = total(self.store.ledger(manifest.id, since=window.start_iso, until=window.end_iso))
        budgets = manifest.budgets
        return BudgetStatus(
            resident=manifest.id,
            agent_id=_agent_id(manifest),
            window=window,
            spend=spend,
            gauges=(
                Gauge(name=COST_BUDGET, spent=spend.cost_usd, limit=budgets.daily_cost_usd),
                Gauge(name=TOKEN_BUDGET, spent=float(spend.tokens), limit=budgets.daily_tokens),
            ),
            max_run_seconds=budgets.max_run_seconds,
            pause=self.store.budget_pause(manifest.id),
            allowance=self._allowance(manifest.id, window),
        )

    def _allowance(self, resident_id: str, window: Window) -> Allowance | None:
        """Return the standing "carry on" for this window, if one still covers it.

        Read against the *window*, not against the wall clock, so an allowance granted
        yesterday is simply absent from today's answer rather than something a sweep has
        to remember to delete.
        """
        raw = self.store.budget_allowance(resident_id)
        if raw is None:
            return None
        try:
            until = datetime.fromisoformat(raw["until"])
        except ValueError:  # pragma: no cover — only a hand-edited database gets here
            return None
        if until <= window.start:
            return None
        return Allowance(
            until=until.astimezone(UTC),
            granted_by=raw["granted_by"],
            granted_at=raw["granted_at"],
        )

    # -- enforcing ---------------------------------------------------------------------

    def allow(self, manifest: ResidentManifest, now: datetime | None = None) -> str | None:
        """Return why this resident may not run, or ``None`` when it may.

        Called before *every* fire — a scheduled routine, a board claim, a run-now — and
        it is the only place a budget refusal is decided. Two ways to be refused, and both
        read the same way to the caller:

        - the resident is **already paused**, in which case nothing is emitted and nothing
          is written; the pause is simply reported;
        - a daily cap is **exhausted right now**, in which case this call pauses the
          resident, raises exactly one ``needs_human``, and reports the same refusal.
        """
        pause = self.store.budget_pause(manifest.id)
        if pause is not None:
            return refusal(pause, request_open=self._request_open(pause))
        status = self.status(manifest, now)
        tripped = status.tripped
        if tripped is None:
            return None
        moment = now or datetime.now(UTC)
        if status.allowance is not None and status.allowance.covers(moment):
            # A person looked at this number and said carry on. That answer holds for the
            # window they answered in, and steward does not ask them again inside it.
            log.info(
                "%s: over %s but allowed to carry on until %s",
                manifest.id,
                tripped.name,
                status.allowance.until.isoformat(),
            )
            return None
        return refusal(self._pause(manifest, status, tripped, now))

    def _request_open(self, pause: PauseRecord) -> bool:
        """Report whether the pause's approval request can still be approved.

        A denied ``budget_unpause`` is resolved, so re-approving it does nothing
        (``recorded=False``). Telling a human to "approve request <id>" when that can never
        work again is a dead end (steward #82); this is what lets the refusal drop that
        advice and point at ``steward budget unpause``, which lifts the pause regardless of
        how the request was answered.
        """
        if not pause.request_id:
            return False
        record = self.store.approval(pause.request_id)
        return record is not None and record.pending

    def _pause(
        self,
        manifest: ResidentManifest,
        status: BudgetStatus,
        tripped: Gauge,
        now: datetime | None,
    ) -> PauseRecord:
        """Stop this resident and knock once. The conditional insert decides who knocks."""
        request_id = new_id()
        record, created = self.store.pause_resident(
            resident=manifest.id,
            agent_id=status.agent_id,
            budget=tripped.name,
            spent=tripped.spent,
            cap=float(tripped.limit if tripped.limit is not None else 0.0),
            reason=tripped.describe(),
            request_id=request_id,
            window_end=status.window.end_iso,
            now=ev.utc_now_iso(now) if now is not None else None,
        )
        if not created:
            # Somebody else tripped the same budget in the same instant and already
            # knocked. Their row and their request stand; this call adds nothing.
            return record
        log.warning(
            "%s: %s — pausing this resident and knocking at the door",
            manifest.id,
            tripped.describe(),
        )
        approvals.raise_request(
            self.store,
            self.emitter,
            manifest=manifest,
            request=NeedsHuman(
                raw=f"budget {tripped.name} exhausted",
                action=BUDGET_ACTION,
                detail={
                    "resident": manifest.id,
                    "budget": tripped.name,
                    "spent": tripped.spent,
                    "limit": tripped.limit,
                    "window": status.window.to_dict(),
                    "runs_today": status.spend.runs,
                    "runs_without_usage": status.spend.unreported,
                },
                options=UNPAUSE_OPTIONS,
                # A budget pause does not deny itself. Deny-by-default exists so a gated
                # action never happens because nobody answered — here the *safe* state is
                # already the current one, and expiring the request would only throw away
                # the one thing that can lift the pause.
                expires_in_s=None,
            ),
            message=knock_message(manifest, tripped),
            now=now,
            request_id=request_id,
            # The pause row above already makes this exactly one knock per tripped budget.
            # A human denying yesterday's unpause must not swallow today's pause: that
            # deny answered "may this resident run again", not "has it stopped again".
            repeat_guard=False,
        )
        return record

    def timeout_for(self, manifest: ResidentManifest, declared_s: int) -> int:
        """Return the timeout one run of this resident actually gets."""
        return effective_timeout_s(manifest, declared_s)

    # -- writing -----------------------------------------------------------------------

    def record(  # noqa: PLR0913 — the run, named by its kind, its id, and what it cost
        self,
        manifest: ResidentManifest,
        *,
        result: RunResult,
        kind: str = RUN_ROUTINE,
        run_id: str = "",
        ref: str = "",
        origin: str = "",
        now: datetime | None = None,
    ) -> LedgerEntry:
        """Append what one finished session cost. Called once per run, whatever happened.

        Usage that the brain did not report is written as zero and flagged, never guessed:
        a ``codex`` run has no cost to give, and a ledger that invented one would make the
        fuel gauge a decoration.

        ``origin`` is what the run descends from; the caller knows the chain, so it says
        so here rather than leaving the rollup to reconstruct it from a join.
        """
        known = (
            result.cost_usd is not None
            or result.input_tokens is not None
            or result.output_tokens is not None
        )
        entry = self.store.record_run(
            resident=manifest.id,
            agent_id=_agent_id(manifest),
            kind=kind,
            run_id=run_id,
            ref=ref,
            origin=origin,
            outcome=str(result.outcome),
            input_tokens=result.input_tokens or 0,
            output_tokens=result.output_tokens or 0,
            cost_usd=result.cost_usd or 0.0,
            duration_s=result.duration_s,
            usage_known=known,
            now=ev.utc_now_iso(now) if now is not None else None,
        )
        self._pause_if_over(manifest, now)
        return entry

    def _pause_if_over(self, manifest: ResidentManifest, now: datetime | None) -> None:
        """Pause this resident, once, if the run just ledgered pushed its day over a cap.

        The other half of the kill-switch (steward #68). :meth:`allow` reads the ledger
        *before* a run and cannot stop a run whose own single cost crosses the cap: the
        first fire of a day always reads an empty window, so a once-daily 10.00 routine
        under a 5.00 cap fired seven nights and spent 70.00 without ever pausing. The
        over-budget run has already spent and cannot be un-spent — but once its cost is on
        the ledger the *next* fire, board claim, or delegated pickup must be refused, and
        that refusal is a pause, written here.

        Knocks exactly once. It reuses :meth:`_pause`, whose conditional insert means an
        already-paused resident (its pre-fire refusal, or an earlier over-cap run) is read
        back rather than knocked on again — and a resident a person told to carry on for
        this window is left alone, the same standing "carry on" :meth:`allow` honours.
        """
        status = self.status(manifest, now)
        if status.paused:
            return
        tripped = status.tripped
        if tripped is None:
            return
        moment = now or datetime.now(UTC)
        if status.allowance is not None and status.allowance.covers(moment):
            return
        self._pause(manifest, status, tripped, now)

    # -- lifting a pause ---------------------------------------------------------------

    def resume(
        self,
        resident_id: str,
        *,
        decided_by: str = "cli",
        decide: bool = True,
    ) -> PauseRecord | None:
        """Lift a budget pause. Returns what was lifted, or ``None`` if nothing was.

        ``decide=False`` is the API's path: ``POST /approvals/{id}`` has already recorded
        the decision and emitted ``needs_human_resolved``, and recording it twice would
        put two answers in the log for one question. ``decide=True`` is the CLI's path,
        where nobody has answered anything yet, so the same request is resolved here and
        the same event is emitted — an unpause from a terminal and an unpause from a panel
        leave the village looking identical, because they are the same act.
        """
        pause = self.store.unpause_resident(resident_id)
        if pause is None:
            return None
        if pause.window_end:
            # "Carry on" scoped to the day it was said about. Without this the next fire
            # would re-trip the same cap and knock again, and answering a question would
            # be answering it into a loop.
            self.store.grant_budget_allowance(
                resident_id,
                until=pause.window_end,
                granted_by=decided_by,
                reason=pause.reason,
            )
        log.info("%s: budget pause lifted by %s (%s)", resident_id, decided_by, pause.reason)
        if decide and pause.request_id:
            record, recorded = self.store.decide(pause.request_id, "approve", decided_by=decided_by)
            if recorded and record is not None:
                self.emitter.emit(
                    ev.needs_human_resolved_event(
                        request_id=record.request_id,
                        decision="approve",
                        action=record.action,
                        agent_id=record.agent_id,
                        project=record.project,
                        decided_by=decided_by,
                    )
                )
        return pause


# --------------------------------------------------------------------------------------
# the words a refusal uses
# --------------------------------------------------------------------------------------


def refusal(pause: PauseRecord, *, request_open: bool = True) -> str:
    """Render the one refusal a paused resident gets, wherever it is refused.

    ``request_open`` says whether approving the pause's request would still lift it. Once
    it has been *denied* the request is resolved and re-approving it changes nothing, so
    the refusal drops the "approve request <id>" path and names only the one action that
    still works — ``steward budget unpause`` — rather than sending a human down a dead end
    (steward #82).
    """
    cap = f" of {_number(pause.cap)}" if pause.cap else ""
    if pause.request_id and request_open:
        tail = (
            f"; approve request {pause.request_id} or run `steward budget unpause {pause.resident}`"
        )
    else:
        tail = f"; run `steward budget unpause {pause.resident}`"
    return f"{PAUSED_MESSAGE} ({pause.budget}: {_number(pause.spent)}{cap} spent){tail}"


def knock_message(manifest: ResidentManifest, tripped: Gauge) -> str:
    """Render the one line burrow renders and a notification forwards.

    Derived rather than authored, and it carries the number: "Hob is paused" tells you
    nothing you can act on, while "Hob has spent 5.20 of its 5.00 daily_cost_usd budget"
    is a sentence you can answer yes or no to.
    """
    limit = _number(tripped.limit) if tripped.limit is not None else "its"
    return (
        f"{manifest.soul.name} has spent {_number(tripped.spent)} of {limit} "
        f"{tripped.name} today and is paused"
    )


def _agent_id(manifest: ResidentManifest) -> str:
    """Name the burrow identity this resident's spend is recorded under."""
    return manifest.agent_id or f"steward:{manifest.id}"
