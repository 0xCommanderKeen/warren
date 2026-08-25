"""Budgets: what a day costs, when steward stops, and who is allowed to start it again.

The two properties worth proving here are the ones an unattended agent lives or dies by:
a daily window that resets on the calendar rather than on a process restart, and a refusal
that knocks exactly once however many times it is asked.
"""

import copy
import threading
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from conftest import ResidentWriter, valid_manifest
from steward import budgets as bg
from steward import events as ev
from steward import manifest as m
from steward import scheduler as s
from steward.board import Dispatcher
from steward.manifest import load_manifest
from steward.runners import Outcome, Runner, RunRequest, RunResult
from steward.store import Store

LJUBLJANA = "Europe/Ljubljana"

#: 12:00 UTC is 14:00 in Ljubljana in August — comfortably inside one local day.
NOON = datetime(2026, 8, 24, 12, 0, tzinfo=UTC)


@pytest.fixture
def store() -> Iterator[Store]:
    with Store(":memory:") as opened:
        yield opened


@pytest.fixture
def sink() -> ev.NullEmitter:
    return ev.NullEmitter()


def budget_manifest(**budgets: object) -> dict[str, Any]:
    """Build a manifest with a mock runner and the budgets a test wants to try."""
    data = copy.deepcopy(valid_manifest())
    data["runner"] = {"kind": "mock", "model": "pretend"}
    data["routines"][0]["schedule_tz"] = LJUBLJANA
    if budgets:
        data["budgets"] = dict(budgets)
    return data


def manifest_of(data: dict[str, Any] | None = None) -> m.ResidentManifest:
    """Validate a manifest dict into the model, without touching disk."""
    return m.ResidentManifest.model_validate(data or budget_manifest())


def spent(cost: float = 0.0, tokens: int = 0, duration: float = 0.0) -> RunResult:
    """Build a finished run that reports exactly this much consumption."""
    return RunResult(
        outcome=Outcome.OK,
        input_tokens=tokens,
        output_tokens=0,
        cost_usd=cost,
        duration_s=duration,
    )


class ScriptedRunner(Runner):
    """A runner that returns a prepared result and remembers the requests it got."""

    def __init__(self, result: RunResult | None = None) -> None:
        """Hold the result every run of this runner returns."""
        super().__init__()
        self.result = result or RunResult(outcome=Outcome.OK, output="done", exit_status=0)
        self.requests: list[RunRequest] = []

    def run(self, request: RunRequest) -> RunResult:
        """Record the request and hand back the prepared result."""
        self.requests.append(request)
        return self.result


# --------------------------------------------------------------------------------------
# where a day happens
# --------------------------------------------------------------------------------------


def test_the_closing_routine_decides_which_zone_the_day_is_counted_in() -> None:
    """A resident's budget day ends where its journal day ends, not where UTC is."""
    data = budget_manifest()
    data["routines"] = [
        {
            "id": "inbox",
            "schedule": "15 * * * *",
            "schedule_tz": "America/New_York",
            "prompt": "read",
            "timeout_s": 60,
        },
        {
            "id": "inbox-two",
            "schedule": "45 * * * *",
            "schedule_tz": "America/New_York",
            "prompt": "read",
            "timeout_s": 60,
        },
        {
            "id": "close",
            "schedule": "30 22 * * *",
            "schedule_tz": LJUBLJANA,
            "prompt": "look back",
            "timeout_s": 600,
            "journal": "close_of_day",
        },
    ]
    # Two routines vote New York and one votes Ljubljana, but the closing routine is the
    # one that decides which calendar day this resident's work belongs to.
    assert bg.primary_tz(manifest_of(data)) == LJUBLJANA


def test_without_a_closer_the_commonest_zone_wins() -> None:
    """A resident that never closes its day still has one, and it is the majority zone."""
    data = budget_manifest()
    data["routines"] = [
        {
            "id": "a",
            "schedule": "0 7 * * *",
            "schedule_tz": LJUBLJANA,
            "prompt": "x",
            "timeout_s": 5,
        },
        {
            "id": "b",
            "schedule": "0 8 * * *",
            "schedule_tz": LJUBLJANA,
            "prompt": "x",
            "timeout_s": 5,
        },
        {"id": "c", "schedule": "0 9 * * *", "schedule_tz": "UTC", "prompt": "x", "timeout_s": 5},
    ]
    assert bg.primary_tz(manifest_of(data)) == LJUBLJANA


def test_a_resident_with_no_enabled_routines_counts_its_day_in_utc() -> None:
    """A board-only resident has a day too, and steward does not invent a zone for it."""
    data = budget_manifest()
    data["routines"] = []
    assert bg.primary_tz(manifest_of(data)) == m.DEFAULT_SCHEDULE_TZ


def test_a_disabled_routine_does_not_vote() -> None:
    """A routine that is switched off is not part of the rhythm of this resident's day."""
    data = budget_manifest()
    data["routines"] = [
        {
            "id": "a",
            "schedule": "0 7 * * *",
            "schedule_tz": "America/New_York",
            "prompt": "x",
            "timeout_s": 5,
            "enabled": False,
        },
        {
            "id": "b",
            "schedule": "0 8 * * *",
            "schedule_tz": LJUBLJANA,
            "prompt": "x",
            "timeout_s": 5,
        },
    ]
    assert bg.primary_tz(manifest_of(data)) == LJUBLJANA


def test_the_window_is_local_midnight_to_local_midnight() -> None:
    """August in Ljubljana is UTC+2, so the local day starts at 22:00 UTC the day before."""
    window = bg.day_window(LJUBLJANA, NOON)
    assert window.day == "2026-08-24"
    assert window.start == datetime(2026, 8, 23, 22, 0, tzinfo=UTC)
    assert window.end == datetime(2026, 8, 24, 22, 0, tzinfo=UTC)


def test_a_dst_day_is_genuinely_longer_than_twenty_four_hours() -> None:
    """The budget is a promise about a day, not about 86400 seconds."""
    # Ljubljana falls back on 2026-10-25, so that local day has twenty-five hours in it.
    window = bg.day_window(LJUBLJANA, datetime(2026, 10, 25, 12, 0, tzinfo=UTC))
    assert (window.end - window.start) == timedelta(hours=25)


def test_a_dst_fall_back_day_is_summed_in_full(store: Store) -> None:
    """A ``now`` in the repeated hour must not shrink the day and drop that hour's spend."""
    guard = bg.BudgetGuard(store)
    manifest = manifest_of(budget_manifest(daily_cost_usd=100.0))
    # 02:30 on the fall-back morning happens twice; the second pass carries fold=1. Pinning
    # fold=0 in day_window is what keeps the window a full 25 hours regardless (steward #68).
    ambiguous = datetime(2026, 10, 25, 2, 30, fold=1, tzinfo=ZoneInfo(LJUBLJANA)).astimezone(UTC)
    window = bg.day_window(LJUBLJANA, ambiguous)
    assert (window.end - window.start) == timedelta(hours=25)
    guard.record(manifest, result=spent(cost=1.0), run_id="dawn", now=window.start)
    guard.record(
        manifest, result=spent(cost=1.0), run_id="dusk", now=window.end - timedelta(seconds=1)
    )
    status = guard.status(manifest, ambiguous)
    assert status.spend.runs == 2
    assert status.spend.cost_usd == pytest.approx(2.0)


# --------------------------------------------------------------------------------------
# the ledger
# --------------------------------------------------------------------------------------


def test_the_ledger_accumulates_across_every_kind_of_run(store: Store) -> None:
    """A routine, a board task, and a delegated item all spend the same daily budget."""
    guard = bg.BudgetGuard(store)
    manifest = manifest_of()
    for kind, cost, tokens in (("routine", 1.0, 100), ("task", 2.0, 200), ("delegated", 0.5, 50)):
        guard.record(
            manifest,
            result=spent(cost=cost, tokens=tokens),
            kind=kind,
            run_id=f"run-{kind}",
            ref=kind,
            now=NOON,
        )
    status = guard.status(manifest, NOON)
    assert status.spend.runs == 3
    assert status.spend.cost_usd == pytest.approx(3.5)
    assert status.spend.tokens == 350
    assert {entry.kind for entry in store.ledger(manifest.id)} == {
        "routine",
        "task",
        "delegated",
    }


def test_usage_a_brain_did_not_report_is_counted_as_zero_and_said_out_loud(
    store: Store,
) -> None:
    """A codex run has no cost to give, and steward reports the gap instead of guessing."""
    guard = bg.BudgetGuard(store)
    manifest = manifest_of()
    guard.record(manifest, result=RunResult(outcome=Outcome.OK), run_id="quiet", now=NOON)
    guard.record(manifest, result=spent(cost=1.0), run_id="loud", now=NOON)
    status = guard.status(manifest, NOON)
    assert status.spend.cost_usd == pytest.approx(1.0)
    assert status.spend.unreported == 1


def test_a_failed_run_still_costs_what_it_cost(store: Store) -> None:
    """A session that burned four minutes and produced nothing still burned four minutes."""
    guard = bg.BudgetGuard(store)
    manifest = manifest_of()
    guard.record(
        manifest,
        result=RunResult(outcome=Outcome.TIMEOUT, duration_s=240.0, cost_usd=0.4),
        run_id="killed",
        now=NOON,
    )
    status = guard.status(manifest, NOON)
    assert status.spend.runs == 1
    assert status.spend.duration_s == pytest.approx(240.0)
    assert status.spend.cost_usd == pytest.approx(0.4)


def test_the_window_resets_by_date_and_not_by_a_restart(tmp_path: Path) -> None:
    """Bounce the store mid-window: yesterday stays yesterday, today stays today."""
    db = tmp_path / "steward.db"
    manifest = manifest_of(budget_manifest(daily_cost_usd=5.0))
    yesterday = NOON - timedelta(days=1)

    with Store(db) as first:
        guard = bg.BudgetGuard(first)
        guard.record(manifest, result=spent(cost=4.0), run_id="yesterday", now=yesterday)
        guard.record(manifest, result=spent(cost=1.0), run_id="today", now=NOON)
        before = guard.status(manifest, NOON)

    # A whole new process, a whole new connection, the same file on disk.
    with Store(db) as second:
        after = bg.BudgetGuard(second).status(manifest, NOON)

    assert before.spend.cost_usd == pytest.approx(1.0)
    assert after.spend.cost_usd == pytest.approx(1.0)
    assert after.window.day == "2026-08-24"
    # And the day before is still on the ledger, in its own window, un-erased.
    with Store(db) as third:
        assert bg.BudgetGuard(third).status(manifest, yesterday).spend.cost_usd == pytest.approx(
            4.0
        )


def test_a_run_at_local_midnight_belongs_to_the_day_it_starts(store: Store) -> None:
    """The window is half-open, so no run is ever counted against two days."""
    guard = bg.BudgetGuard(store)
    manifest = manifest_of()
    midnight = bg.day_window(LJUBLJANA, NOON).start  # 22:00 UTC on the 23rd
    guard.record(manifest, result=spent(cost=1.0), run_id="edge", now=midnight)
    assert guard.status(manifest, NOON).spend.cost_usd == pytest.approx(1.0)
    assert guard.status(manifest, NOON - timedelta(days=1)).spend.cost_usd == pytest.approx(0.0)


# --------------------------------------------------------------------------------------
# refusal, and the one knock
# --------------------------------------------------------------------------------------


def test_an_unlimited_resident_is_never_refused(store: Store) -> None:
    """A manifest with no budgets block spends what it spends, and says so out loud."""
    guard = bg.BudgetGuard(store)
    manifest = manifest_of()
    guard.record(manifest, result=spent(cost=999.0), run_id="a", now=NOON)
    status = guard.status(manifest, NOON)
    assert guard.allow(manifest, NOON) is None
    assert not status.declared
    assert status.summary() == "no limit"
    assert all(gauge.limit is None for gauge in status.gauges)


def test_an_exhausted_budget_refuses_pauses_and_knocks_exactly_once(
    store: Store, sink: ev.NullEmitter
) -> None:
    """One pause, one needs_human — not one knock per refused fire."""
    guard = bg.BudgetGuard(store, sink)
    manifest = manifest_of(budget_manifest(daily_cost_usd=1.0))
    guard.record(manifest, result=spent(cost=1.5), run_id="expensive", now=NOON)

    refusals = [guard.allow(manifest, NOON) or "" for _ in range(4)]

    assert all(r.startswith(bg.PAUSED_MESSAGE) for r in refusals)
    assert "daily_cost_usd" in refusals[0]
    assert "1.50" in refusals[0]
    knocks = [e for e in sink.events if e.type == ev.NEEDS_HUMAN]
    assert len(knocks) == 1
    assert knocks[0].payload["action"] == bg.BUDGET_ACTION
    assert knocks[0].payload["detail"]["spent"] == pytest.approx(1.5)
    assert knocks[0].payload["detail"]["limit"] == pytest.approx(1.0)
    assert "1.50" in knocks[0].payload["message"]
    assert "is paused" in knocks[0].payload["message"]
    assert len(store.approvals()) == 1
    assert store.budget_pause(manifest.id) is not None


def test_a_budget_knock_never_denies_itself(store: Store, sink: ev.NullEmitter) -> None:
    """Deny-by-default protects nothing here: the pause is already the safe state."""
    guard = bg.BudgetGuard(store, sink)
    manifest = manifest_of(budget_manifest(daily_tokens=100))
    guard.record(manifest, result=spent(tokens=500), run_id="chatty", now=NOON)
    guard.allow(manifest, NOON)

    record = store.approvals()[0]
    assert record.expires_at is None
    # A year later it is still waiting for a person, because only a person can answer it.
    assert store.expire_approvals(ev.utc_now_iso(NOON + timedelta(days=365))) == []


def test_the_token_budget_counts_input_plus_output(store: Store, sink: ev.NullEmitter) -> None:
    """Both halves of a conversation are tokens somebody paid for."""
    guard = bg.BudgetGuard(store, sink)
    manifest = manifest_of(budget_manifest(daily_tokens=100))
    guard.record(
        manifest,
        result=RunResult(outcome=Outcome.OK, input_tokens=60, output_tokens=60),
        run_id="a",
        now=NOON,
    )
    refusal = guard.allow(manifest, NOON)
    assert refusal is not None
    assert "daily_tokens" in refusal


def test_a_pause_does_not_lift_itself_when_the_window_rolls_over(
    store: Store, sink: ev.NullEmitter
) -> None:
    """Tomorrow does not un-know that this resident blew through the cap you set."""
    guard = bg.BudgetGuard(store, sink)
    manifest = manifest_of(budget_manifest(daily_cost_usd=1.0))
    guard.record(manifest, result=spent(cost=2.0), run_id="a", now=NOON)
    assert guard.allow(manifest, NOON) is not None

    tomorrow = NOON + timedelta(days=1)
    assert guard.status(manifest, tomorrow).spend.cost_usd == pytest.approx(0.0)
    assert guard.allow(manifest, tomorrow) is not None  # still paused, and still says why
    assert len([e for e in sink.events if e.type == ev.NEEDS_HUMAN]) == 1


def test_approving_the_request_resumes_the_resident(store: Store, sink: ev.NullEmitter) -> None:
    """The unpause path: the same approval machinery every other gated action uses."""
    guard = bg.BudgetGuard(store, sink)
    manifest = manifest_of(budget_manifest(daily_cost_usd=1.0))
    guard.record(manifest, result=spent(cost=2.0), run_id="a", now=NOON)
    guard.allow(manifest, NOON)
    request_id = store.approvals()[0].request_id
    assert store.pause_for_request(request_id) is not None

    lifted = guard.resume(manifest.id, decided_by="cli")

    assert lifted is not None
    assert store.budget_pause(manifest.id) is None
    decided = store.approval(request_id)
    assert decided is not None
    assert decided.decision == "approve"
    resolved = [e for e in sink.events if e.type == ev.NEEDS_HUMAN_RESOLVED]
    assert len(resolved) == 1
    assert resolved[0].payload["decision"] == "approve"
    # The resident really does run again, and it is not asked to knock a second time in
    # the window the human just answered about.
    assert guard.allow(manifest, NOON) is None
    assert len([e for e in sink.events if e.type == ev.NEEDS_HUMAN]) == 1


def test_carrying_on_means_today_and_not_forever(store: Store, sink: ev.NullEmitter) -> None:
    """The allowance is scoped to the window it was granted in, and expires by date."""
    guard = bg.BudgetGuard(store, sink)
    manifest = manifest_of(budget_manifest(daily_cost_usd=1.0))
    guard.record(manifest, result=spent(cost=2.0), run_id="a", now=NOON)
    guard.allow(manifest, NOON)
    guard.resume(manifest.id, decided_by="cli")

    status = guard.status(manifest, NOON)
    assert status.allowance is not None
    assert status.allowance.until == status.window.end
    assert status.allowance.granted_by == "cli"
    assert "allowed to carry on" in status.summary()

    # Tomorrow the allowance is simply absent — nothing had to sweep it away — and
    # tomorrow's cap applies to tomorrow's spend.
    tomorrow = NOON + timedelta(days=1)
    assert guard.status(manifest, tomorrow).allowance is None
    guard.record(manifest, result=spent(cost=5.0), run_id="b", now=tomorrow)
    assert guard.allow(manifest, tomorrow) is not None
    assert len([e for e in sink.events if e.type == ev.NEEDS_HUMAN]) == 2


def test_resuming_a_resident_that_is_not_paused_changes_nothing(store: Store) -> None:
    """An unpause is not a way to invent state that was never there."""
    assert bg.BudgetGuard(store).resume("test-agent") is None


def test_a_second_caller_in_the_same_instant_does_not_knock_twice(
    store: Store, sink: ev.NullEmitter
) -> None:
    """The conditional insert decides who knocks; the loser adds nothing."""
    guard = bg.BudgetGuard(store, sink)
    manifest = manifest_of(budget_manifest(daily_cost_usd=1.0))
    guard.record(manifest, result=spent(cost=2.0), run_id="a", now=NOON)
    status = guard.status(manifest, NOON)
    tripped = status.tripped
    assert tripped is not None

    first = guard._pause(manifest, status, tripped, NOON)
    second = guard._pause(manifest, status, tripped, NOON)

    assert first.request_id == second.request_id
    assert len([e for e in sink.events if e.type == ev.NEEDS_HUMAN]) == 1


# --------------------------------------------------------------------------------------
# the post-run kill-switch (steward #68)
# --------------------------------------------------------------------------------------


def test_a_once_daily_over_cap_routine_pauses_after_night_one(
    store: Store, sink: ev.NullEmitter
) -> None:
    """Seven nights of a 10.00 run under a 5.00 cap → one run and a pause, not seven runs.

    The pre-fire check alone never trips here: the first fire of each day reads an empty
    window, so a once-daily run whose single cost exceeds the cap would spend forever. The
    post-run check pauses the resident after night one, and the pause persists across the
    date boundary until a person lifts it, so every later night is refused before it fires.
    """
    guard = bg.BudgetGuard(store, sink)
    manifest = manifest_of(budget_manifest(daily_cost_usd=5.0))

    fired = 0
    for night in range(7):
        moment = NOON + timedelta(days=night)
        if guard.allow(manifest, moment) is not None:
            continue  # refused before it fires — the kill-switch is engaged
        guard.record(manifest, result=spent(cost=10.0), run_id=f"night-{night}", now=moment)
        fired += 1

    assert fired == 1
    assert len(store.ledger(manifest.id)) == 1
    assert store.budget_pause(manifest.id) is not None
    assert len([e for e in sink.events if e.type == ev.NEEDS_HUMAN]) == 1


def test_a_single_run_that_exceeds_the_cap_pauses_the_resident(
    store: Store, sink: ev.NullEmitter
) -> None:
    """A run whose own cost blows the whole cap can't be un-spent, but it does stop the next."""
    guard = bg.BudgetGuard(store, sink)
    manifest = manifest_of(budget_manifest(daily_cost_usd=5.0))

    # Nothing refuses the very first fire — the window is empty when it is asked.
    assert guard.allow(manifest, NOON) is None
    guard.record(manifest, result=spent(cost=10.0), run_id="one-big-run", now=NOON)

    assert store.budget_pause(manifest.id) is not None
    refusal = guard.allow(manifest, NOON)
    assert refusal is not None
    assert refusal.startswith(bg.PAUSED_MESSAGE)
    assert len([e for e in sink.events if e.type == ev.NEEDS_HUMAN]) == 1


def test_recording_over_an_already_paused_resident_does_not_double_knock(
    store: Store, sink: ev.NullEmitter
) -> None:
    """The over-budget run finishes and is ledgered, but the door is only knocked on once."""
    guard = bg.BudgetGuard(store, sink)
    manifest = manifest_of(budget_manifest(daily_cost_usd=5.0))
    guard.record(manifest, result=spent(cost=10.0), run_id="a", now=NOON)  # pauses + knocks
    guard.record(manifest, result=spent(cost=10.0), run_id="b", now=NOON)  # already paused

    assert len(store.ledger(manifest.id)) == 2
    assert len(store.approvals()) == 1
    assert len([e for e in sink.events if e.type == ev.NEEDS_HUMAN]) == 1


def test_a_token_run_over_the_cap_pauses_after_it_is_recorded(
    store: Store, sink: ev.NullEmitter
) -> None:
    """The kill-switch watches the token cap too, not only the money one."""
    guard = bg.BudgetGuard(store, sink)
    manifest = manifest_of(budget_manifest(daily_tokens=100))
    guard.record(manifest, result=spent(tokens=500), run_id="chatty", now=NOON)

    refusal = guard.allow(manifest, NOON)
    assert refusal is not None
    assert "daily_tokens" in refusal


def test_the_pre_fire_check_still_permits_intra_day_runs_under_the_cap(
    store: Store, sink: ev.NullEmitter
) -> None:
    """Under the cap the pre-fire check lets a resident keep going; it stops it at the cap."""
    guard = bg.BudgetGuard(store, sink)
    manifest = manifest_of(budget_manifest(daily_cost_usd=5.0))

    fired = 0
    for run in range(5):
        if guard.allow(manifest, NOON) is not None:
            break
        guard.record(manifest, result=spent(cost=2.0), run_id=f"run-{run}", now=NOON)
        fired += 1

    # 2 + 2 + 2 = 6 crosses 5 on the third run, which the post-run check then pauses on.
    assert fired == 3
    assert guard.allow(manifest, NOON) is not None
    assert len([e for e in sink.events if e.type == ev.NEEDS_HUMAN]) == 1


def test_a_run_under_the_cap_records_without_pausing(store: Store, sink: ev.NullEmitter) -> None:
    """A run that stays under the cap is ledgered and leaves the door unknocked."""
    guard = bg.BudgetGuard(store, sink)
    manifest = manifest_of(budget_manifest(daily_cost_usd=5.0))
    guard.record(manifest, result=spent(cost=1.0), run_id="cheap", now=NOON)

    assert store.budget_pause(manifest.id) is None
    assert guard.allow(manifest, NOON) is None
    assert [e for e in sink.events if e.type == ev.NEEDS_HUMAN] == []


def test_a_carry_on_allowance_survives_a_later_over_cap_run(
    store: Store, sink: ev.NullEmitter
) -> None:
    """A person who said 'carry on' for today is not re-paused by the next over-cap run."""
    guard = bg.BudgetGuard(store, sink)
    manifest = manifest_of(budget_manifest(daily_cost_usd=5.0))
    guard.record(manifest, result=spent(cost=10.0), run_id="a", now=NOON)  # pauses
    guard.resume(manifest.id, decided_by="human")  # grants the allowance for the window

    # A further over-cap run inside the same window finishes and is ledgered, and the
    # post-run check leaves the resident running because a person said so.
    guard.record(manifest, result=spent(cost=10.0), run_id="b", now=NOON)

    assert store.budget_pause(manifest.id) is None
    assert guard.allow(manifest, NOON) is None
    assert len([e for e in sink.events if e.type == ev.NEEDS_HUMAN]) == 1


# --------------------------------------------------------------------------------------
# max_run_seconds
# --------------------------------------------------------------------------------------


def test_the_effective_timeout_is_the_smaller_of_the_two() -> None:
    """A routine cannot opt out of a budget by declaring a longer timeout."""
    capped = manifest_of(budget_manifest(max_run_seconds=300))
    assert bg.effective_timeout_s(capped, 900) == 300
    assert bg.effective_timeout_s(capped, 60) == 60
    assert bg.effective_timeout_s(manifest_of(), 900) == 900


def test_the_scheduler_runs_a_session_under_the_capped_timeout(
    write_resident: ResidentWriter, store: Store, tmp_path: Path
) -> None:
    """The cap reaches the process, not just the arithmetic."""
    data = budget_manifest(max_run_seconds=120)
    data["routines"][0]["timeout_s"] = 900
    path = write_resident(data)
    resident = load_manifest(path)
    runner = ScriptedRunner()
    engine = s.Scheduler(
        [s.ScheduledRoutine(resident=resident, routine=resident.manifest.routines[0])],
        emitter=ev.NullEmitter(),
        state=s.SchedulerState(path=tmp_path / "state.json"),
        workdir=tmp_path,
        runner_factory=lambda _spec: runner,
        guard=bg.BudgetGuard(store),
    )
    engine.fire(engine.scheduled[0], now=NOON)
    assert runner.requests[0].timeout_s == 120


def test_a_run_killed_by_the_cap_is_a_routine_failed_and_is_still_ledgered(
    write_resident: ResidentWriter, store: Store, tmp_path: Path
) -> None:
    """The existing timeout path carries it, and the ledger keeps the seconds anyway."""
    path = write_resident(budget_manifest(max_run_seconds=60))
    resident = load_manifest(path)
    sink = ev.NullEmitter()
    killed = RunResult(outcome=Outcome.TIMEOUT, duration_s=60.0, error="exceeded its 60s timeout")
    engine = s.Scheduler(
        [s.ScheduledRoutine(resident=resident, routine=resident.manifest.routines[0])],
        emitter=sink,
        state=s.SchedulerState(path=tmp_path / "state.json"),
        workdir=tmp_path,
        runner_factory=lambda _spec: ScriptedRunner(killed),
        guard=bg.BudgetGuard(store),
    )
    engine.fire(engine.scheduled[0], now=NOON)

    assert [e.type for e in sink.events] == [ev.ROUTINE_STARTED, ev.ROUTINE_FAILED]
    entries = store.ledger(resident.id)
    assert len(entries) == 1
    assert entries[0].duration_s == pytest.approx(60.0)
    assert entries[0].outcome == "timeout"
    # A routine descends from nobody but the resident whose day it is (#45).
    assert entries[0].origin == f"resident:{resident.id}"


# --------------------------------------------------------------------------------------
# the fire paths
# --------------------------------------------------------------------------------------


def test_a_paused_resident_does_not_fire_and_does_not_eat_a_decision(
    write_resident: ResidentWriter, store: Store, tmp_path: Path
) -> None:
    """A refused fire emits no bracket, launches nothing, and delivers no answer."""
    path = write_resident(budget_manifest(daily_cost_usd=1.0))
    resident = load_manifest(path)
    sink = ev.NullEmitter()
    guard = bg.BudgetGuard(store, sink)
    guard.record(resident.manifest, result=spent(cost=5.0), run_id="a", now=NOON)

    delivered: list[str] = []

    class Hooks:
        def decisions_for(self, resident_id: str) -> str | None:
            delivered.append(resident_id)
            return "you may send that email"

        def harvest(self, manifest: m.ResidentManifest, output: str) -> object:  # noqa: ARG002
            return []

        def dispatch(self, now: datetime) -> object:  # noqa: ARG002
            return None

    runner = ScriptedRunner()
    engine = s.Scheduler(
        [s.ScheduledRoutine(resident=resident, routine=resident.manifest.routines[0])],
        emitter=sink,
        state=s.SchedulerState(path=tmp_path / "state.json"),
        workdir=tmp_path,
        runner_factory=lambda _spec: runner,
        hooks=Hooks(),
        guard=guard,
    )
    report = engine.fire(engine.scheduled[0], now=NOON)

    assert not report.fired
    assert report.skipped_reason is not None
    assert report.skipped_reason.startswith(bg.PAUSED_MESSAGE)
    assert runner.requests == []
    assert delivered == []
    assert [e.type for e in sink.events] == [ev.NEEDS_HUMAN]


def test_a_guard_that_cannot_be_read_stops_the_run(
    write_resident: ResidentWriter, tmp_path: Path
) -> None:
    """An unreadable budget refuses rather than waving a run through."""

    class Broken:
        def allow(
            self,
            manifest: m.ResidentManifest,  # noqa: ARG002
            now: datetime | None = None,  # noqa: ARG002
        ) -> str | None:
            raise RuntimeError("the ledger is on fire")

        def timeout_for(self, manifest: m.ResidentManifest, declared_s: int) -> int:  # noqa: ARG002
            return declared_s

        def record(self, manifest: m.ResidentManifest, **_: object) -> object:  # noqa: ARG002
            return None

    resident = load_manifest(write_resident(budget_manifest()))
    engine = s.Scheduler(
        [s.ScheduledRoutine(resident=resident, routine=resident.manifest.routines[0])],
        emitter=ev.NullEmitter(),
        state=s.SchedulerState(path=tmp_path / "state.json"),
        workdir=tmp_path,
        guard=Broken(),
    )
    report = engine.fire(engine.scheduled[0], now=NOON)
    assert not report.fired
    assert "budget unreadable" in (report.skipped_reason or "")


def test_a_broken_ledger_does_not_fail_the_routine(
    write_resident: ResidentWriter, tmp_path: Path
) -> None:
    """Losing a ledger row is a warning; the run really did happen."""

    class HalfBroken:
        def allow(
            self,
            manifest: m.ResidentManifest,  # noqa: ARG002
            now: datetime | None = None,  # noqa: ARG002
        ) -> str | None:
            return None

        def timeout_for(self, manifest: m.ResidentManifest, declared_s: int) -> int:  # noqa: ARG002
            return declared_s

        def record(self, manifest: m.ResidentManifest, **_: object) -> object:  # noqa: ARG002
            raise RuntimeError("disk full")

    resident = load_manifest(write_resident(budget_manifest()))
    sink = ev.NullEmitter()
    engine = s.Scheduler(
        [s.ScheduledRoutine(resident=resident, routine=resident.manifest.routines[0])],
        emitter=sink,
        state=s.SchedulerState(path=tmp_path / "state.json"),
        workdir=tmp_path,
        runner_factory=lambda _spec: ScriptedRunner(),
        guard=HalfBroken(),
    )
    report = engine.fire(engine.scheduled[0], now=NOON)
    assert report.fired
    assert [e.type for e in sink.events] == [ev.ROUTINE_STARTED, ev.ROUTINE_FINISHED]


def board_manifest(**budgets: object) -> dict[str, Any]:
    """Build a board-claiming manifest, with whatever budgets the test wants."""
    data = budget_manifest(**budgets)
    data["routes"] = [
        *data["routes"],
        {"id": "job-board", "kind": "job-board", "address": "steward:job-board"},
    ]
    data["board"] = {"claim": True, "lease_s": 1800, "timeout_s": 900}
    return data


def test_a_paused_resident_claims_nothing_off_the_board(
    write_resident: ResidentWriter, store: Store, tmp_path: Path
) -> None:
    """A board session spends the same money out of the same cap, so it is refused too."""
    resident = load_manifest(write_resident(board_manifest(daily_cost_usd=1.0)))
    sink = ev.NullEmitter()
    guard = bg.BudgetGuard(store, sink)
    guard.record(resident.manifest, result=spent(cost=9.0), run_id="a", now=NOON)
    store.post_job(title="tidy the notes")

    run = Dispatcher(
        residents=[resident],
        store=store,
        emitter=sink,
        workdir=tmp_path,
        runner_factory=lambda _spec: ScriptedRunner(),
        guard=guard,
    ).dispatch(NOON)

    assert run.reports == ()
    assert store.jobs("open")  # the task is still there for somebody who can afford it
    assert ev.TASK_CLAIMED not in [e.type for e in sink.events]


def test_a_board_task_lands_in_the_ledger(
    write_resident: ResidentWriter, store: Store, tmp_path: Path
) -> None:
    """Claimed work is recorded under its own kind, so the board's cost is answerable."""
    resident = load_manifest(write_resident(board_manifest()))
    guard = bg.BudgetGuard(store, ev.NullEmitter())
    task = store.post_job(title="tidy the notes")

    Dispatcher(
        residents=[resident],
        store=store,
        emitter=ev.NullEmitter(),
        workdir=tmp_path,
        runner_factory=lambda _spec: ScriptedRunner(spent(cost=0.25, tokens=40)),
        guard=guard,
    ).dispatch(NOON)

    entries = store.ledger(resident.id)
    assert [entry.kind for entry in entries] == ["task"]
    assert entries[0].cost_usd == pytest.approx(0.25)
    # The row says what it descends from (#45), rather than leaving a join to work it out.
    assert entries[0].origin == f"task:{task.task_id}"


def test_a_board_session_is_capped_by_max_run_seconds(
    write_resident: ResidentWriter, store: Store, tmp_path: Path
) -> None:
    """The per-run cap applies to a claimed task exactly as it does to a routine."""
    resident = load_manifest(write_resident(board_manifest(max_run_seconds=90)))
    runner = ScriptedRunner()
    store.post_job(title="tidy the notes")
    Dispatcher(
        residents=[resident],
        store=store,
        emitter=ev.NullEmitter(),
        workdir=tmp_path,
        runner_factory=lambda _spec: runner,
        guard=bg.BudgetGuard(store),
    ).dispatch(NOON)
    assert runner.requests[0].timeout_s == 90


# --------------------------------------------------------------------------------------
# the manifest
# --------------------------------------------------------------------------------------


def test_budgets_must_be_positive(write_resident: ResidentWriter) -> None:
    """A zero or negative cap is not a cap; it is a manifest nobody meant to write."""
    for field, value in (("daily_cost_usd", 0), ("daily_tokens", -1), ("max_run_seconds", 0)):
        path = write_resident(budget_manifest(**{field: value}))
        result = m.validate_manifest(path)
        assert not result.ok
        assert any(field in d.field_path for d in result.errors)


def test_a_timeout_over_the_cap_is_a_warning_not_an_error(
    write_resident: ResidentWriter,
) -> None:
    """The manifest is not wrong, but a routine that never gets its time is worth saying."""
    data = budget_manifest(max_run_seconds=300)
    data["routines"][0]["timeout_s"] = 900
    result = m.validate_manifest(write_resident(data))
    assert result.ok
    warnings = [d for d in result.warnings if d.field_path == "routines[0].timeout_s"]
    assert len(warnings) == 1
    assert "300" in warnings[0].problem


def test_a_board_timeout_over_the_cap_warns_too(write_resident: ResidentWriter) -> None:
    """The same warning, for the other kind of session."""
    data = board_manifest(max_run_seconds=60)
    data["routines"][0]["timeout_s"] = 30
    result = m.validate_manifest(write_resident(data))
    assert result.ok
    assert [d.field_path for d in result.warnings] == ["board.timeout_s"]


def test_a_manifest_without_a_budgets_block_is_unlimited(
    write_resident: ResidentWriter,
) -> None:
    """Absent means unlimited, and the model says so rather than defaulting to a number."""
    resident = load_manifest(write_resident(budget_manifest()))
    assert not resident.manifest.budgets.declared
    assert resident.manifest.budgets.daily_cost_usd is None
    assert resident.manifest.deploy.container is None


def test_the_deploy_block_names_a_container(write_resident: ResidentWriter) -> None:
    """One field, and it is a declaration a supervisor can act on."""
    data = budget_manifest()
    data["deploy"] = {"container": "steward-life-agent"}
    resident = load_manifest(write_resident(data))
    assert resident.manifest.deploy.container == "steward-life-agent"


def test_a_container_name_has_to_look_like_one(write_resident: ResidentWriter) -> None:
    """A name docker would refuse is a name steward refuses first, in daylight."""
    data = budget_manifest()
    data["deploy"] = {"container": "not a container name"}
    result = m.validate_manifest(write_resident(data))
    assert not result.ok
    assert any("deploy.container" in d.field_path for d in result.errors)


def test_budgets_and_deploy_are_in_the_published_schema() -> None:
    """Burrow reads manifests from this schema, so a new block has to appear in it."""
    schema = m.manifest_json_schema()
    assert "budgets" in schema["properties"]
    assert "deploy" in schema["properties"]
    assert "Budgets" in schema["$defs"]


# --------------------------------------------------------------------------------------
# the shapes a panel reads
# --------------------------------------------------------------------------------------


def test_the_status_view_carries_the_window_the_gauges_and_the_pause(
    store: Store, sink: ev.NullEmitter
) -> None:
    """Everything a fuel gauge needs, and nothing it would have to re-derive."""
    guard = bg.BudgetGuard(store, sink)
    manifest = manifest_of(budget_manifest(daily_cost_usd=2.0, max_run_seconds=300))
    guard.record(manifest, result=spent(cost=3.0, tokens=10), run_id="a", now=NOON)
    guard.allow(manifest, NOON)

    payload = guard.status(manifest, NOON).to_dict()

    assert payload["resident"] == "test-agent"
    assert payload["paused"] is True
    assert payload["max_run_seconds"] == 300
    assert payload["window"]["tz"] == LJUBLJANA
    assert payload["window"]["day"] == "2026-08-24"
    cost = next(b for b in payload["budgets"] if b["budget"] == bg.COST_BUDGET)
    assert cost == {
        "budget": "daily_cost_usd",
        "spent": pytest.approx(3.0),
        "limit": 2.0,
        "remaining": pytest.approx(0.0),
        "exhausted": True,
    }
    tokens = next(b for b in payload["budgets"] if b["budget"] == bg.TOKEN_BUDGET)
    assert tokens["limit"] is None
    assert payload["pause"]["budget"] == bg.COST_BUDGET


def test_a_gauge_with_room_left_reports_it(store: Store) -> None:
    """The unremarkable case still has to be reportable."""
    guard = bg.BudgetGuard(store)
    manifest = manifest_of(budget_manifest(daily_cost_usd=10.0))
    guard.record(manifest, result=spent(cost=2.5), run_id="a", now=NOON)
    gauge = next(g for g in guard.status(manifest, NOON).gauges if g.name == bg.COST_BUDGET)
    assert not gauge.exhausted
    assert gauge.remaining == pytest.approx(7.5)
    assert gauge.describe() == "daily_cost_usd: 2.50 of 10"


def test_being_exactly_at_the_limit_is_exhausted(store: Store) -> None:
    """A cap you have reached is a cap you have no more of."""
    guard = bg.BudgetGuard(store)
    manifest = manifest_of(budget_manifest(daily_cost_usd=1.0))
    guard.record(manifest, result=spent(cost=1.0), run_id="a", now=NOON)
    assert guard.status(manifest, NOON).tripped is not None


# --------------------------------------------------------------------------------------
# stamping at completion, and one lock across allow → run → record (steward #68)
# --------------------------------------------------------------------------------------


def _routine(rid: str, hour: int) -> dict[str, Any]:
    return {
        "id": rid,
        "schedule": f"0 {hour} * * *",
        "schedule_tz": LJUBLJANA,
        "prompt": "do it",
        "timeout_s": 60,
        "enabled": True,
    }


def test_a_nightly_over_cap_run_that_crosses_midnight_still_pauses(
    write_resident: ResidentWriter, store: Store, tmp_path: Path
) -> None:
    """Stamped at completion, last night's 23:50 spend lands in the window tonight re-reads."""
    data = budget_manifest(daily_cost_usd=5.0)
    data["routines"] = [_routine("nightly", 23)]
    resident = load_manifest(write_resident(data))
    over_and_long = RunResult(outcome=Outcome.OK, cost_usd=10.0, duration_s=1200.0)
    engine = s.Scheduler(
        [s.ScheduledRoutine(resident=resident, routine=resident.manifest.routines[0])],
        emitter=ev.NullEmitter(),
        state=s.SchedulerState(path=tmp_path / "state.json"),
        workdir=tmp_path,
        runner_factory=lambda _spec: ScriptedRunner(over_and_long),
        guard=bg.BudgetGuard(store, ev.NullEmitter()),
    )
    item = engine.scheduled[0]
    # 23:50 Ljubljana is 21:50 UTC in August; a 20-minute run finishes at 00:10 local.
    night_one = datetime(2026, 8, 24, 21, 50, tzinfo=UTC)
    night_two = datetime(2026, 8, 25, 21, 50, tzinfo=UTC)

    assert engine.fire(item, now=night_one).fired
    second = engine.fire(item, now=night_two)
    assert not second.fired
    assert second.skipped_reason is not None
    assert second.skipped_reason.startswith(bg.PAUSED_MESSAGE)
    assert store.budget_pause(resident.id) is not None


def test_concurrent_due_routines_of_one_resident_respect_the_cap(
    write_resident: ResidentWriter, store: Store, tmp_path: Path
) -> None:
    """The per-resident lock stops two due routines both passing one pre-ledger read."""
    data = budget_manifest(daily_cost_usd=1.0)
    data["routines"] = [_routine("one", 7), _routine("two", 8)]
    resident = load_manifest(write_resident(data))
    engine = s.Scheduler(
        [
            s.ScheduledRoutine(resident=resident, routine=routine)
            for routine in resident.manifest.routines
        ],
        emitter=ev.NullEmitter(),
        state=s.SchedulerState(path=tmp_path / "state.json"),
        workdir=tmp_path,
        runner_factory=lambda _spec: ScriptedRunner(spent(cost=2.0)),
        guard=bg.BudgetGuard(store, ev.NullEmitter()),
    )
    reports: list[s.FireReport] = []
    lock = threading.Lock()
    barrier = threading.Barrier(2)

    def go(item: s.ScheduledRoutine) -> None:
        barrier.wait()
        report = engine.fire(item, now=NOON)
        with lock:
            reports.append(report)

    threads = [threading.Thread(target=go, args=(item,)) for item in engine.scheduled]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert sum(1 for report in reports if report.fired) == 1, "only one run spends the cap"
    assert len(store.ledger(resident.id)) == 1


# --------------------------------------------------------------------------------------
# a denied pause, and a clock read in the resident's own zone (steward #82)
# --------------------------------------------------------------------------------------


def test_a_denied_pause_points_at_unpause_not_a_dead_request(
    store: Store, sink: ev.NullEmitter
) -> None:
    """After a deny, 'approve request <id>' can never work again — the refusal drops it."""
    guard = bg.BudgetGuard(store, sink)
    manifest = manifest_of(budget_manifest(daily_cost_usd=1.0))
    guard.record(manifest, result=spent(cost=2.0), run_id="a", now=NOON)
    first = guard.allow(manifest, NOON)
    assert first is not None
    assert "approve request" in first

    pause = store.budget_pause(manifest.id)
    assert pause is not None
    assert pause.request_id is not None
    decided, recorded = store.decide(pause.request_id, "deny", decided_by="human")
    assert recorded
    assert decided is not None

    after = guard.allow(manifest, NOON)
    assert after is not None
    assert "approve request" not in after
    assert f"steward budget unpause {manifest.id}" in after
    # And that path still lifts the pause, however the request was answered.
    assert guard.resume(manifest.id, decide=False) is not None


def test_the_carry_on_summary_reads_in_the_local_zone_not_utc(
    store: Store, sink: ev.NullEmitter
) -> None:
    """A Ljubljana resident's day ends at local 00:00, not the 22:00 its UTC instant prints."""
    guard = bg.BudgetGuard(store, sink)
    manifest = manifest_of(budget_manifest(daily_cost_usd=1.0))
    guard.record(manifest, result=spent(cost=2.0), run_id="a", now=NOON)
    guard.allow(manifest, NOON)  # pauses
    guard.resume(manifest.id, decided_by="human")  # grants "carry on" until the local day's end

    summary = guard.status(manifest, NOON).summary()
    assert "until 00:00" in summary
    assert "until 22:00" not in summary
