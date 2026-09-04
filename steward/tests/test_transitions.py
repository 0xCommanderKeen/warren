"""Named transitions: one durable change, one fact, and the branches that say nothing.

Every test here asserts the row and the event *together*, because that pairing is the
whole point of the seam. The store's own tests still prove the conditional writes and the
event module's still prove delivery and fallback; what is proved here is that the right
fact reaches the emitter on the winning branch, and that no fact reaches it on any other.
"""

import ast
import json
import threading
import time
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from conftest import ResidentWriter, valid_manifest
from steward import events as ev
from steward import notify as nf
from steward import prompt as p
from steward import transitions as tr
from steward.approvals import NeedsHuman
from steward.approved_edits import GRANT_SKILL_ACTION
from steward.manifest import SECRET_REDACTION, ResidentManifest, load_manifest
from steward.run_lifecycle import RunTransitions
from steward.runners import Outcome, RunResult
from steward.store import (
    STATUS_CLAIMED,
    STATUS_DONE,
    STATUS_FAILED,
    STATUS_OPEN,
    ApprovalRecord,
    JobRecord,
    Store,
)
from steward.transitions import budget as tb
from steward.transitions import outcome as to
from steward.transitions import task as tt
from steward.transitions.approval import ApprovalOutboxWorker

NOW = datetime(2026, 8, 24, 12, 0, tzinfo=UTC)
CLAIMANT = "claude-code:test-agent"
PROJECT = "test-agent"
#: The end of the budget window a pause was tripped in. Required at the seam, because a
#: pause written without one is lifted into an allowance of nothing and re-trips on the
#: very next fire.
WINDOW_END = "2026-08-25T00:00:00.000Z"


@pytest.fixture
def store() -> Iterator[Store]:
    with Store(":memory:") as opened:
        yield opened


@pytest.fixture
def sink() -> ev.NullEmitter:
    return ev.NullEmitter()


@pytest.fixture
def manifest(write_resident: ResidentWriter) -> ResidentManifest:
    return load_manifest(write_resident()).manifest


@pytest.fixture
def tasks(store: Store, sink: ev.NullEmitter) -> tr.TaskTransitions:
    return tr.TaskTransitions(store=store, emitter=sink, project_of=lambda _agent: PROJECT)


@pytest.fixture
def approvals(store: Store, sink: ev.NullEmitter) -> tr.ApprovalTransitions:
    return tr.ApprovalTransitions(store=store, emitter=sink)


@pytest.fixture
def handoffs(store: Store, sink: ev.NullEmitter) -> tr.DelegationTransitions:
    return tr.DelegationTransitions(store=store, emitter=sink)


@pytest.fixture
def budgets(store: Store, sink: ev.NullEmitter) -> tr.BudgetTransitions:
    return tr.BudgetTransitions(store=store, emitter=sink)


def claimed(tasks: tr.TaskTransitions, title: str = "sweep the hall") -> JobRecord:
    """Post one task and claim it, returning the live claim handle."""
    tasks.post(title=title)
    outcome = tasks.claim(claimant=CLAIMANT, project=PROJECT, skills=(), now=NOW, lease_s=1800)
    assert outcome.record is not None
    return outcome.record


def bound_attempt(tasks: tr.TaskTransitions, *, run_id: str = "attempt-1") -> JobRecord:
    """Claim and durably bind one board attempt for lease-liveness tests."""
    job = claimed(tasks)
    assert job.claimed_at
    assert tasks.store.open_task_run(
        task_id=job.task_id,
        lease=job.claimed_at,
        run_id=run_id,
        kind="task",
        agent_id=CLAIMANT,
        project=PROJECT,
        ref=job.task_id,
        timeout_s=60,
        owner_token=f"owner-{run_id}",
        now=ev.utc_now_iso(NOW),
    )
    current = tasks.store.job(job.task_id)
    assert current is not None
    return current


def ok(artifacts: tuple[str, ...] = ()) -> RunResult:
    return RunResult(outcome=Outcome.OK, exit_status=0, artifacts=artifacts)


def failed(error: str) -> RunResult:
    return RunResult(outcome=Outcome.FAILED, exit_status=2, error=error)


def types(sink: ev.NullEmitter) -> list[str]:
    return [event.type for event in sink.events]


# ------------------------------------------------------------------ the outcome vocabulary


def test_only_an_applied_transition_reaches_an_emitter(sink: ev.NullEmitter) -> None:
    event = ev.task_posted_event(task_id="t1", title="a thing")
    assert to.applied(sink, "row", event).fact is event
    assert sink.events == [event]

    silent = [
        to.refused("nothing to do"),
        to.replayed("row"),
        to.expired("row"),
        to.superseded("lost"),
        to.answered("row"),
    ]
    assert all(transition.silent for transition in silent)
    assert sink.events == [event], "a non-applied transition must never emit"


def test_an_applied_transition_may_legitimately_carry_no_fact(sink: ev.NullEmitter) -> None:
    transition = to.applied(sink, "row")
    assert transition.applied
    assert transition.silent
    assert sink.events == []


def test_each_outcome_answers_to_exactly_one_name() -> None:
    named = {
        to.APPLIED: to.Transition(to.APPLIED),
        to.REFUSED: to.refused(""),
        to.REPLAYED: to.replayed(None),
        to.EXPIRED: to.expired(None),
        to.SUPERSEDED: to.superseded(),
        to.ANSWERED: to.answered(None),
    }
    assert set(named) == set(to.OUTCOMES)
    flags = ("applied", "refused", "replayed", "expired", "superseded", "answered")
    for name, transition in named.items():
        assert [flag for flag in flags if getattr(transition, flag)] == [name]


def test_requiring_a_record_a_refusal_never_produced_is_a_bug_that_surfaces() -> None:
    with pytest.raises(ValueError, match="refused transition has no durable record"):
        to.refused("nothing to claim").require()


def test_a_carried_fact_is_the_inner_ones_and_is_not_sent_twice(sink: ev.NullEmitter) -> None:
    event = ev.task_posted_event(task_id="t1", title="a thing")
    inner = to.applied(sink, "inner-row", event)
    outer = to.carried("outer-row", inner)
    assert outer.applied
    assert outer.record == "outer-row"
    assert outer.fact is event
    assert sink.events == [event]


def test_a_carried_transition_keeps_the_inner_one_whole_rather_than_only_its_fact() -> None:
    """Applied-and-silent is true of two different stories; ``via`` tells them apart."""
    already = to.replayed("inner-row", "this request was already decided")
    outer = to.carried("outer-row", already)

    assert outer.applied, "the outer durable change happened here"
    assert outer.silent, "and it said nothing, because the inner act had nothing to say"
    assert outer.via is already
    assert outer.via.replayed, "and the reason it said nothing is still answerable"
    assert outer.reason == "", "an applied transition does not report the inner act's refusal"
    assert outer.via.reason == "this request was already decided"


def test_both_row_writing_outcomes_answer_to_wrote_and_only_one_to_applied() -> None:
    """The trap ``wrote`` closes: ``applied`` is not the test for "is there a record"."""
    wrote = (to.Transition(to.APPLIED, record="row"), to.answered("row"))
    wrote_nothing = (to.refused(""), to.superseded(), to.replayed("row"), to.expired("row"))

    assert all(transition.wrote for transition in wrote)
    assert [transition.applied for transition in wrote] == [True, False]
    assert not any(transition.wrote for transition in wrote_nothing)


# ----------------------------------------------------------------------- task transitions


def test_posting_writes_an_open_row_and_announces_it_as_steward(
    tasks: tr.TaskTransitions, store: Store, sink: ev.NullEmitter
) -> None:
    outcome = tasks.post(title="water the plants", required_skills=("research",))

    assert outcome.applied
    assert outcome.record is not None
    row = store.job(outcome.record.task_id)
    assert row is not None
    assert (row.status, row.claimant, row.assignee) == (STATUS_OPEN, None, None)

    (event,) = sink.events
    assert event is outcome.fact
    assert event.type == "task_posted"
    assert (event.agent_id, event.project) == (ev.API_AGENT_ID, ev.API_PROJECT)
    assert event.payload["task_id"] == row.task_id
    assert event.payload["required_skills"] == ["research"]


def test_direct_task_posters_cannot_bypass_work_bounds(
    tasks: tr.TaskTransitions, store: Store, sink: ev.NullEmitter
) -> None:
    with pytest.raises(ValueError, match="character limit"):
        tasks.post(title="x" * 201)
    assert store.jobs() == []
    assert sink.events == []


def test_a_claim_leases_the_row_and_walks_the_claimant_to_the_notice(
    tasks: tr.TaskTransitions, store: Store, sink: ev.NullEmitter
) -> None:
    tasks.post(title="sweep the hall")
    sink.events.clear()

    outcome = tasks.claim(claimant=CLAIMANT, project=PROJECT, skills=(), now=NOW, lease_s=1800)

    assert outcome.applied
    assert outcome.record is not None
    row = store.job(outcome.record.task_id)
    assert row is not None
    assert row.status == STATUS_CLAIMED
    assert row.claimant == CLAIMANT
    assert row.lease_expires_at == ev.utc_now_iso(NOW + timedelta(seconds=1800))

    (event,) = sink.events
    assert event.type == "task_claimed"
    assert (event.agent_id, event.project) == (CLAIMANT, PROJECT)
    assert event.payload["claimant"] == CLAIMANT
    assert "parent_task_id" not in event.payload, "an unparented claim answers no lineage"


def test_a_claim_that_finds_nothing_is_refused_and_says_nothing(
    tasks: tr.TaskTransitions, sink: ev.NullEmitter
) -> None:
    outcome = tasks.claim(claimant=CLAIMANT, project=PROJECT, skills=(), now=NOW, lease_s=1800)

    assert outcome.refused
    assert outcome.record is None
    assert sink.events == []


def test_a_claim_the_skills_do_not_match_is_refused_and_says_nothing(
    tasks: tr.TaskTransitions, sink: ev.NullEmitter
) -> None:
    tasks.post(title="fly the plane", required_skills=("aviation",))
    sink.events.clear()

    outcome = tasks.claim(
        claimant=CLAIMANT, project=PROJECT, skills=("research",), now=NOW, lease_s=1800
    )

    assert outcome.refused
    assert sink.events == []


def test_taking_delivery_claims_a_letter_and_carries_its_lineage(
    tasks: tr.TaskTransitions, store: Store, sink: ev.NullEmitter
) -> None:
    parent = store.post_job(title="the root task")
    store.delegate_job(
        title="read this",
        assignee="test-agent",
        delegated_by="other-agent",
        route="delegation",
        parent_task_id=parent.task_id,
        depth=1,
    )

    outcome = tasks.take_delivery(
        assignee="test-agent", claimant=CLAIMANT, project=PROJECT, now=NOW, lease_s=600
    )

    assert outcome.applied
    assert outcome.record is not None
    assert outcome.record.lease_expires_at == ev.utc_now_iso(NOW + timedelta(seconds=600))
    (event,) = sink.events
    assert event.type == "task_claimed"
    assert event.payload["parent_task_id"] == parent.task_id


def test_an_empty_inbox_is_refused_and_says_nothing(
    tasks: tr.TaskTransitions, sink: ev.NullEmitter
) -> None:
    outcome = tasks.take_delivery(
        assignee="test-agent", claimant=CLAIMANT, project=PROJECT, now=NOW, lease_s=600
    )

    assert outcome.refused
    assert sink.events == []


def test_finishing_a_task_closes_the_row_and_reports_what_it_left_behind(
    tasks: tr.TaskTransitions, store: Store, sink: ev.NullEmitter
) -> None:
    job = claimed(tasks)
    sink.events.clear()

    outcome = tasks.finish(
        job,
        claimant=CLAIMANT,
        project=PROJECT,
        result=ok(("notes.md",)),
        run_id="run-1",
        now=NOW,
    )

    assert outcome.applied
    assert outcome.reason == ""
    row = store.job(job.task_id)
    assert row is not None
    assert (row.status, row.artifacts) == (STATUS_DONE, ("notes.md",))

    (event,) = sink.events
    assert event.type == "task_done"
    assert (event.agent_id, event.project) == (CLAIMANT, PROJECT)
    assert event.payload["artifacts"] == ["notes.md"]
    assert event.payload["run_id"] == "run-1"


def test_a_failed_task_records_and_emits_one_derived_reason(
    tasks: tr.TaskTransitions, store: Store, sink: ev.NullEmitter
) -> None:
    job = claimed(tasks)
    sink.events.clear()

    outcome = tasks.finish(
        job,
        claimant=CLAIMANT,
        project=PROJECT,
        result=failed("the runner refused"),
        run_id="run-2",
        now=NOW,
    )

    assert outcome.applied
    row = store.job(job.task_id)
    assert row is not None
    assert row.status == STATUS_FAILED
    (event,) = sink.events
    assert event.type == "task_failed"
    assert row.reason == event.payload["reason"] == outcome.reason
    assert outcome.reason.endswith("the runner refused")
    assert event.payload["run_id"] == "run-2"


def test_a_close_on_a_lease_that_died_writes_nothing_and_says_nothing(
    tasks: tr.TaskTransitions, store: Store, sink: ev.NullEmitter
) -> None:
    job = claimed(tasks)
    # The lease dies, the task is swept back onto the board, and somebody re-claims it.
    store.expire_leases(ev.utc_now_iso(NOW + timedelta(hours=2)))
    tasks.claim(
        claimant="claude-code:someone-else",
        project="elsewhere",
        skills=(),
        now=NOW + timedelta(hours=2),
        lease_s=1800,
    )
    sink.events.clear()

    outcome = tasks.finish(
        job, claimant=CLAIMANT, project=PROJECT, result=ok(), run_id="run-3", now=NOW
    )

    assert outcome.superseded
    assert outcome.record is None
    assert outcome.reason == tt.LEASE_LOST
    assert sink.events == [], "a close that lost its lease must not overwrite the live claim"
    row = store.job(job.task_id)
    assert row is not None
    assert row.status == STATUS_CLAIMED
    assert row.claimant == "claude-code:someone-else"


def test_an_expired_lease_goes_back_to_the_board_loudly_and_names_no_session(
    tasks: tr.TaskTransitions, store: Store, sink: ev.NullEmitter
) -> None:
    job = claimed(tasks)
    sink.events.clear()

    expired = tasks.expire_leases(now=NOW + timedelta(hours=2))

    assert [swept.require().task_id for swept in expired] == [job.task_id]
    assert all(swept.applied for swept in expired), "every swept lease was written and mourned"
    assert expired[0].require().claimant == CLAIMANT, "the row is reported as it was when it died"
    row = store.job(job.task_id)
    assert row is not None
    assert (row.status, row.claimant) == (STATUS_OPEN, None)

    (event,) = sink.events
    assert event.type == "task_failed"
    assert (event.agent_id, event.project) == (CLAIMANT, PROJECT)
    assert event.payload["reason"] == tt.LEASE_EXPIRED
    assert "run_id" not in event.payload, "the board mourns a claim, it does not answer a run"


def test_live_bound_attempt_renews_job_and_survives_original_lease(
    tasks: tr.TaskTransitions,
) -> None:
    job = bound_attempt(tasks)
    later = NOW + timedelta(hours=1)
    assert tasks.store.renew_task_run(
        "attempt-1", owner_token="owner-attempt-1", now=ev.utc_now_iso(later)
    )

    assert tasks.expire_leases(now=later) == []
    current = tasks.store.job(job.task_id)
    assert current is not None
    assert current.status == STATUS_CLAIMED


def test_dead_bound_attempt_reopens_and_closes_its_exact_run(
    tasks: tr.TaskTransitions, sink: ev.NullEmitter
) -> None:
    job = bound_attempt(tasks)
    expired = tasks.expire_leases(now=NOW + timedelta(hours=1))

    assert [item.require().task_id for item in expired] == [job.task_id]
    current = tasks.store.job(job.task_id)
    assert current is not None
    assert current.status == STATUS_OPEN
    assert tasks.store.open_runs() == []
    terminal = sink.events[-1]
    assert terminal.type == ev.TASK_FAILED
    assert terminal.payload["run_id"] == "attempt-1"


def test_concurrent_task_sweeps_choose_one_terminal(
    tasks: tr.TaskTransitions, sink: ev.NullEmitter
) -> None:
    bound_attempt(tasks)
    later = NOW + timedelta(hours=1)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: tasks.expire_leases(now=later), range(2)))

    assert sum(len(result) for result in results) == 1
    failures = [event for event in sink.events if event.type == ev.TASK_FAILED]
    assert {event.payload["event_id"] for event in failures} == {"run-terminal:attempt-1"}


def test_late_finish_is_suppressed_and_retry_gets_new_run(tasks: tr.TaskTransitions) -> None:
    old = bound_attempt(tasks)
    tasks.expire_leases(now=NOW + timedelta(hours=1))
    retry = tasks.claim(
        claimant=CLAIMANT,
        project=PROJECT,
        skills=(),
        now=NOW + timedelta(hours=1),
        lease_s=1800,
    ).require()
    assert retry.claimed_at
    assert retry.claimed_at != old.claimed_at
    assert tasks.store.open_task_run(
        task_id=retry.task_id,
        lease=retry.claimed_at,
        run_id="attempt-2",
        kind="task",
        agent_id=CLAIMANT,
        project=PROJECT,
        ref=retry.task_id,
        owner_token="owner-attempt-2",
        now=ev.utc_now_iso(NOW + timedelta(hours=1)),
    )

    late = ev.task_done_event(
        task_id=old.task_id,
        title=old.title,
        claimant=CLAIMANT,
        project=PROJECT,
        run_id="attempt-1",
    )
    assert (
        RunTransitions(tasks.store).task_session_claim(
            old, late, result=ok(), claimant=CLAIMANT, owner_token="owner-attempt-1", now=NOW
        )
        is None
    )
    current = tasks.store.job(old.task_id)
    assert current is not None
    assert current.run_id == "attempt-2"


def test_crash_after_atomic_task_expiry_is_replayed_by_next_sweep(
    tasks: tr.TaskTransitions, sink: ev.NullEmitter
) -> None:
    job = bound_attempt(tasks)
    assert job.claimed_at
    assert job.run_id
    assert job.owner_token
    event_id = f"run-terminal:{job.run_id}"
    fact = ev.task_failed_event(
        task_id=job.task_id,
        title=job.title,
        claimant=CLAIMANT,
        project=PROJECT,
        reason=tt.LEASE_EXPIRED,
        run_id=job.run_id,
    )
    fact = ev.Event(
        type=fact.type,
        agent_id=fact.agent_id,
        project=fact.project,
        ts=fact.ts,
        payload={**fact.payload, "event_id": event_id},
    )
    assert tasks.store.expire_task_attempt_and_claim_terminal(
        job.task_id,
        lease=job.claimed_at,
        run_id=job.run_id,
        owner_token=job.owner_token,
        event=fact.to_json(),
        event_id=event_id,
        now=ev.utc_now_iso(NOW + timedelta(hours=1)),
    )
    sink.events.clear()  # the simulated process died before emitting

    assert tasks.expire_leases(now=NOW + timedelta(hours=1)) == []
    assert [event.payload["run_id"] for event in sink.events] == [job.run_id]
    assert tasks.store.open_runs() == []


def test_a_sweep_with_nothing_to_reopen_says_nothing(
    tasks: tr.TaskTransitions, sink: ev.NullEmitter
) -> None:
    assert tasks.expire_leases(now=NOW) == []
    assert sink.events == []


def test_a_swept_lease_is_mourned_under_stewards_project_when_the_fleet_is_unknown(
    store: Store, sink: ev.NullEmitter
) -> None:
    unmapped = tr.TaskTransitions(store=store, emitter=sink)
    unmapped.post(title="sweep the hall")
    unmapped.claim(claimant=CLAIMANT, project=PROJECT, skills=(), now=NOW, lease_s=1)
    sink.events.clear()

    unmapped.expire_leases(now=NOW + timedelta(hours=2))

    (event,) = sink.events
    assert event.project == ev.API_PROJECT


# ------------------------------------------------------------------- approval transitions


def test_raising_a_request_files_it_pending_and_knocks(
    approvals: tr.ApprovalTransitions,
    store: Store,
    sink: ev.NullEmitter,
    manifest: ResidentManifest,
) -> None:
    outcome = approvals.raise_request(
        manifest=manifest,
        request=NeedsHuman(raw="", action="send_email", detail={"to": "a@example.com"}),
        now=NOW,
    )

    assert outcome.applied
    assert outcome.record is not None
    row = store.approval(outcome.record.request_id)
    assert row is not None
    assert row.pending

    (event,) = sink.events
    assert event.type == "needs_human"
    assert event.agent_id == manifest.agent_id
    assert event.payload["action"] == "send_email"
    assert event.payload["expires_at"] == row.expires_at


def test_a_knock_is_scrubbed_before_it_reaches_the_village(
    approvals: tr.ApprovalTransitions, sink: ev.NullEmitter, manifest: ResidentManifest
) -> None:
    approvals.raise_request(
        manifest=manifest,
        request=NeedsHuman(
            raw="", action="rotate_token", detail={"key": "sk-ant-api03-DEADBEEFDEADBEEFDEADBEEF"}
        ),
        now=NOW,
    )

    (event,) = sink.events
    assert event.payload["detail"]["key"] == SECRET_REDACTION


def test_a_repeat_inside_the_window_is_answered_and_nobody_is_knocked_on(
    approvals: tr.ApprovalTransitions,
    store: Store,
    sink: ev.NullEmitter,
    manifest: ResidentManifest,
) -> None:
    first = approvals.raise_request(
        manifest=manifest, request=NeedsHuman(raw="", action="send_email"), now=NOW
    )
    assert first.record is not None
    approvals.decide(first.record.request_id, "deny", now=NOW)
    sink.events.clear()

    outcome = approvals.raise_request(
        manifest=manifest,
        request=NeedsHuman(raw="", action="send_email"),
        now=NOW + timedelta(hours=1),
    )

    assert outcome.answered
    assert outcome.silent
    assert outcome.record is not None
    row = store.approval(outcome.record.request_id)
    assert row is not None
    assert (row.decision, row.decided_by) == ("deny", "repeat")
    assert sink.events == [], "a looping resident must not knock on every wake-up"


def test_a_second_skill_grant_still_reaches_a_person_after_the_first_was_refused(
    approvals: tr.ApprovalTransitions,
    store: Store,
    sink: ev.NullEmitter,
    manifest: ResidentManifest,
) -> None:
    """Every grant asks under one action name, so the guard would answer for all of them.

    Two different questions — a different skill, a different resident — and a no to the
    first must not silently answer the second (warren#437). The action steward's write door
    opens against is the one where a swallowed ask costs a write nobody refused.
    """
    first = approvals.raise_request(
        manifest=manifest,
        request=NeedsHuman(
            raw="",
            action=GRANT_SKILL_ACTION,
            detail={"resident": "shelf-worker", "skill": "series-detection"},
        ),
        now=NOW,
    )
    approvals.decide(first.require().request_id, "deny", now=NOW)
    sink.events.clear()

    outcome = approvals.raise_request(
        manifest=manifest,
        request=NeedsHuman(
            raw="",
            action=GRANT_SKILL_ACTION,
            detail={"resident": "hob", "skill": "read-invoices"},
        ),
        now=NOW + timedelta(hours=1),
    )

    assert outcome.applied
    assert not outcome.answered, "the second question was asked, not answered for"
    row = store.approval(outcome.require().request_id)
    assert row is not None
    assert row.pending
    assert types(sink) == ["needs_human"], "and a person heard about it"


def test_an_unreadable_escalation_still_reaches_a_person(
    approvals: tr.ApprovalTransitions, sink: ev.NullEmitter, manifest: ResidentManifest
) -> None:
    outcome = approvals.raise_request(
        manifest=manifest,
        request=NeedsHuman(raw="<needs-human oops>", action="x", problem="no action"),
        now=NOW,
    )

    assert outcome.applied
    assert outcome.record is not None
    assert outcome.record.detail["problem"] == "no action"
    assert types(sink) == ["needs_human"]


def test_harvesting_a_session_raises_every_block_it_wrote(
    approvals: tr.ApprovalTransitions, sink: ev.NullEmitter, manifest: ResidentManifest
) -> None:
    output = (
        f"{p.ACTIONS_OPEN}\n"
        '<needs-human action="send_email">{"to": "a@example.com"}</needs-human>\n'
        '<needs-human action="spend_money">{"amount": 5}</needs-human>\n'
        f"{p.ACTIONS_CLOSE}"
    )

    raised = approvals.harvest(manifest=manifest, output=output, now=NOW)

    assert [ask.require().action for ask in raised] == ["send_email", "spend_money"]
    assert all(ask.applied for ask in raised), "both were knocked about"
    assert types(sink) == ["needs_human", "needs_human"]


def test_harvesting_says_which_asks_the_repeat_guard_swallowed(
    approvals: tr.ApprovalTransitions, sink: ev.NullEmitter, manifest: ResidentManifest
) -> None:
    """A batch of raises is a batch of transitions: two rows, two different stories."""
    denied = approvals.raise_request(
        manifest=manifest, request=NeedsHuman(raw="", action="send_email"), now=NOW
    )
    approvals.decide(denied.require().request_id, "deny", now=NOW)
    sink.events.clear()
    output = (
        f"{p.ACTIONS_OPEN}\n"
        '<needs-human action="send_email">{"to": "a@example.com"}</needs-human>\n'
        '<needs-human action="spend_money">{"amount": 5}</needs-human>\n'
        f"{p.ACTIONS_CLOSE}"
    )

    swallowed, knocked = approvals.harvest(
        manifest=manifest, output=output, now=NOW + timedelta(hours=1)
    )

    assert swallowed.answered, "asked again inside the window, auto-denied, nobody woken"
    assert knocked.applied, "a different action is a different question"
    assert [ask.require().action for ask in (swallowed, knocked)] == ["send_email", "spend_money"]
    assert types(sink) == ["needs_human"], "one knock, for the ask that was not swallowed"


def test_deciding_resolves_the_row_and_closes_the_loop_in_the_log(
    approvals: tr.ApprovalTransitions,
    store: Store,
    sink: ev.NullEmitter,
    manifest: ResidentManifest,
) -> None:
    raised = approvals.raise_request(
        manifest=manifest, request=NeedsHuman(raw="", action="send_email"), now=NOW
    )
    assert raised.record is not None
    sink.events.clear()

    outcome = approvals.decide(raised.record.request_id, "approve", decided_by="api", now=NOW)

    assert outcome.applied
    row = store.approval(raised.record.request_id)
    assert row is not None
    assert (row.decision, row.decided_by) == ("approve", "api")
    (event,) = sink.events
    assert event.type == "needs_human_resolved"
    assert event.agent_id == manifest.agent_id, "the villager who knocked walks away"
    assert event.payload["decision"] == "approve"


def test_a_second_decision_is_a_replay_that_changes_and_says_nothing(
    approvals: tr.ApprovalTransitions,
    store: Store,
    sink: ev.NullEmitter,
    manifest: ResidentManifest,
) -> None:
    raised = approvals.raise_request(
        manifest=manifest, request=NeedsHuman(raw="", action="send_email"), now=NOW
    )
    assert raised.record is not None
    approvals.decide(raised.record.request_id, "approve", now=NOW)
    sink.events.clear()

    outcome = approvals.decide(raised.record.request_id, "deny", now=NOW)

    assert outcome.replayed
    assert outcome.record is not None
    assert outcome.record.decision == "approve", "the first decision wins"
    assert store.approval(raised.record.request_id).decision == "approve"  # ty: ignore
    assert sink.events == []


class FailingEmitter:
    """Emitter seam that simulates a process-visible transport failure."""

    def emit(self, event: ev.Event) -> bool:
        """Fail before accepting the event."""
        del event
        raise OSError("injected emitter failure")


def test_a_committed_decision_retries_its_announcement_after_emitter_failure(
    store: Store, manifest: ResidentManifest
) -> None:
    sink = ev.NullEmitter()
    transitions = tr.ApprovalTransitions(store, sink)
    raised = transitions.raise_request(
        manifest=manifest, request=NeedsHuman(raw="", action="send_email"), now=NOW
    ).require()

    with pytest.raises(OSError, match="injected"):
        tr.ApprovalTransitions(store, FailingEmitter()).decide(
            raised.request_id, "approve", now=NOW
        )

    assert store.approval(raised.request_id).decision == "approve"  # ty: ignore
    sink.events.clear()
    replay = transitions.decide(raised.request_id, "deny", now=NOW)
    assert replay.replayed
    assert sink.events == [], "a False legacy receipt leaves the announcement pending"


def test_an_abandoned_post_emit_claim_is_recovered_once_after_its_lease(
    store: Store, manifest: ResidentManifest, tmp_path: Path
) -> None:
    transitions = tr.ApprovalTransitions(store, ev.NullEmitter())
    raised = transitions.raise_request(
        manifest=manifest, request=NeedsHuman(raw="", action="send_email"), now=NOW
    ).require()
    store.decide(raised.request_id, "approve", now=ev.utc_now_iso(NOW))
    abandoned = store.claim_approval_announcement(raised.request_id, lease_s=0.05)
    assert abandoned is not None, "the simulated dead process emitted but never acknowledged"

    time.sleep(0.06)
    fallback = tmp_path / "approval-outbox-test.jsonl"
    sink = ev.EventEmitter(fallback=fallback)
    assert tr.ApprovalTransitions(store, sink).reconcile_announcements() == 1
    events = [json.loads(line) for line in fallback.read_text().splitlines()]
    assert [event["payload"]["request_id"] for event in events] == [raised.request_id]
    assert tr.ApprovalTransitions(store, sink).reconcile_announcements() == 0


def test_concurrent_reconcilers_claim_one_announcement_once(
    store: Store, manifest: ResidentManifest, tmp_path: Path
) -> None:
    raised = (
        tr.ApprovalTransitions(store, ev.NullEmitter())
        .raise_request(manifest=manifest, request=NeedsHuman(raw="", action="send_email"), now=NOW)
        .require()
    )
    store.decide(raised.request_id, "approve", now=ev.utc_now_iso(NOW))
    fallback = tmp_path / "race-events.jsonl"
    sink = ev.EventEmitter(fallback=fallback)
    barrier = threading.Barrier(3)

    def reconcile() -> None:
        barrier.wait()
        tr.ApprovalTransitions(store, sink).reconcile_announcements()

    threads = [threading.Thread(target=reconcile) for _ in range(2)]
    for thread in threads:
        thread.start()
    barrier.wait()
    for thread in threads:
        thread.join()
    events = [json.loads(line) for line in fallback.read_text().splitlines()]
    assert [event["payload"]["request_id"] for event in events] == [raised.request_id]


def test_worker_recovers_a_transient_failure_without_replay(
    store: Store, manifest: ResidentManifest
) -> None:
    class TransientEmitter:
        def __init__(self) -> None:
            self.attempts = 0
            self.accepted = threading.Event()

        def emit(self, event: ev.Event) -> bool:
            return self.emit_durable(event)

        def emit_durable(self, event: ev.Event) -> bool:
            del event
            self.attempts += 1
            if self.attempts == 1:
                return False
            self.accepted.set()
            return True

    raised = (
        tr.ApprovalTransitions(store, ev.NullEmitter())
        .raise_request(manifest=manifest, request=NeedsHuman(raw="", action="send_email"), now=NOW)
        .require()
    )
    store.decide(raised.request_id, "approve", now=ev.utc_now_iso(NOW))
    sink = TransientEmitter()
    worker = ApprovalOutboxWorker(
        tr.ApprovalTransitions(store, sink),
        lambda record, token: store.complete_approval_effects(record, token)[0],
    )
    worker.start()
    try:
        assert sink.accepted.wait(2.0)
        assert sink.attempts == 2
    finally:
        worker.close()
    assert not worker.alive


def test_worker_retries_an_injected_error_before_atomic_effect_completion(
    store: Store, manifest: ResidentManifest
) -> None:
    raised = (
        tr.ApprovalTransitions(store, ev.NullEmitter())
        .raise_request(manifest=manifest, request=NeedsHuman(raw="", action="send_email"), now=NOW)
        .require()
    )
    record, recorded = store.decide(raised.request_id, "approve", now=ev.utc_now_iso(NOW))
    assert recorded
    assert record is not None
    announcement = store.claim_approval_announcement(raised.request_id)
    assert announcement is not None
    _, announcement_token = announcement
    assert store.finish_approval_announcement(raised.request_id, announcement_token, accepted=True)
    attempted = 0
    completed = threading.Event()

    def crash_then_complete(effect: ApprovalRecord, token: str) -> bool:
        nonlocal attempted
        attempted += 1
        if attempted == 1:
            raise RuntimeError("injected crash before atomic effect transaction")
        result, _resumed = store.complete_approval_effects(effect, token)
        completed.set()
        return result

    worker = ApprovalOutboxWorker(
        tr.ApprovalTransitions(store, ev.NullEmitter()), crash_then_complete
    )
    worker.start()
    try:
        assert completed.wait(2)
    finally:
        worker.close()
    assert attempted == 2
    assert store.approval_announcement_state(raised.request_id) == "complete"


def test_idle_worker_polls_for_expired_work_created_by_another_store(
    tmp_path: Path, manifest: ResidentManifest
) -> None:
    path = tmp_path / "shared-worker.db"
    worker_store, producer = Store(path), Store(path)

    class FailOnceEmitter:
        def __init__(self) -> None:
            self.attempts = 0
            self.accepted = threading.Event()

        def emit(self, event: ev.Event) -> bool:
            return self.emit_durable(event)

        def emit_durable(self, event: ev.Event) -> bool:
            del event
            self.attempts += 1
            if self.attempts == 1:
                return False
            self.accepted.set()
            return True

    sink = FailOnceEmitter()
    worker = ApprovalOutboxWorker(
        tr.ApprovalTransitions(worker_store, sink),
        lambda record, token: worker_store.complete_approval_effects(record, token)[0],
        poll_interval=0.02,
    )
    worker.start()
    try:
        time.sleep(0.04)  # worker is idle before the independent producer commits
        raised = producer.create_approval_request(
            agent_id=manifest.chronicle_agent_id,
            project=manifest.chronicle_project,
            action="send_email",
            message="ask",
            expires_at=ev.utc_now_iso(datetime.now(UTC) - timedelta(seconds=1)),
        )
        assert [row.request_id for row in producer.expire_approvals()] == [raised.request_id]
        assert sink.accepted.wait(1.0)
    finally:
        worker.close()
        worker_store.close()
        producer.close()
    assert sink.attempts == 2


def test_worker_close_surfaces_a_slow_active_pass(store: Store) -> None:
    entered = threading.Event()
    release = threading.Event()

    class SlowTransitions:
        def __init__(self) -> None:
            self.store = store

        def reconcile_announcements(self) -> None:
            entered.set()
            release.wait()

    worker = ApprovalOutboxWorker(
        SlowTransitions(),  # ty: ignore[invalid-argument-type]
        lambda _record, _token: True,
        close_timeout=0.01,
    )
    worker.start()
    assert entered.wait(1.0)
    with pytest.raises(TimeoutError, match="did not stop"):
        worker.close()
    assert worker.alive
    release.set()
    worker.close(timeout=1.0)
    assert not worker.alive


def test_worker_rejects_a_concurrent_second_start(store: Store) -> None:
    worker = ApprovalOutboxWorker(
        tr.ApprovalTransitions(store, ev.NullEmitter()), lambda _record, _token: True
    )
    worker.start()
    try:
        with pytest.raises(RuntimeError, match="already running"):
            worker.start()
    finally:
        worker.close()


def test_worker_can_close_before_its_first_start(store: Store) -> None:
    worker = ApprovalOutboxWorker(
        tr.ApprovalTransitions(store, ev.NullEmitter()), lambda _record, _token: True
    )

    worker.close()
    worker.start()
    worker.close()

    assert not worker.alive


def test_an_expired_request_can_never_be_approved(
    approvals: tr.ApprovalTransitions,
    store: Store,
    sink: ev.NullEmitter,
    manifest: ResidentManifest,
) -> None:
    raised = approvals.raise_request(
        manifest=manifest,
        request=NeedsHuman(raw="", action="send_email", expires_in_s=60),
        now=NOW,
    )
    assert raised.record is not None
    sink.events.clear()

    outcome = approvals.decide(raised.record.request_id, "approve", now=NOW + timedelta(hours=1))

    assert outcome.expired
    assert outcome.record is not None
    assert outcome.record.pending, "deny-by-default keeps the last word until the sweep"
    assert sink.events == []
    row = store.approval(raised.record.request_id)
    assert row is not None
    assert row.decision is None


def test_deciding_a_request_nobody_raised_is_refused(
    approvals: tr.ApprovalTransitions, sink: ev.NullEmitter
) -> None:
    outcome = approvals.decide("no-such-request", "approve")

    assert outcome.refused
    assert outcome.record is None
    assert sink.events == []


@pytest.mark.parametrize(
    "case",
    [
        (("deny", "edit"), "approve", None),
        (("approve", "edit"), "deny", None),
        (("approve", "deny"), "edit", {"subject": "shorter"}),
    ],
)
def test_a_decision_the_request_did_not_offer_is_refused_without_a_write(
    approvals: tr.ApprovalTransitions,
    store: Store,
    sink: ev.NullEmitter,
    manifest: ResidentManifest,
    case: tuple[tuple[str, ...], str, dict[str, str] | None],
) -> None:
    offered, decision, edit = case
    raised = approvals.raise_request(
        manifest=manifest,
        request=NeedsHuman(raw="", action="send_email", options=offered),
        now=NOW,
    )
    request_id = raised.require().request_id
    sink.events.clear()

    outcome = approvals.decide(request_id, decision, edit=edit, now=NOW)

    assert outcome.refused
    assert outcome.record is not None
    assert outcome.record.options == offered
    assert store.approval(request_id).pending  # ty: ignore
    assert sink.events == []


@pytest.mark.parametrize("decision", ["approve", "deny", "edit"])
def test_each_offered_decision_is_recorded(
    approvals: tr.ApprovalTransitions,
    manifest: ResidentManifest,
    decision: str,
) -> None:
    raised = approvals.raise_request(
        manifest=manifest,
        request=NeedsHuman(raw="", action="send_email", options=(decision,)),
        now=NOW,
    )

    outcome = approvals.decide(
        raised.require().request_id,
        decision,
        edit={"subject": "shorter"} if decision == "edit" else None,
        now=NOW,
    )

    assert outcome.applied
    assert outcome.require().decision == decision


def test_the_sweep_denies_a_passed_deadline_and_says_who_decided(
    approvals: tr.ApprovalTransitions,
    store: Store,
    sink: ev.NullEmitter,
    manifest: ResidentManifest,
) -> None:
    raised = approvals.raise_request(
        manifest=manifest,
        request=NeedsHuman(raw="", action="send_email", expires_in_s=60),
        now=NOW,
    )
    assert raised.record is not None
    sink.events.clear()

    swept = approvals.expire(NOW + timedelta(hours=1))

    assert [denied.require().request_id for denied in swept] == [raised.record.request_id]
    assert all(denied.applied for denied in swept), "every swept request was denied and announced"
    row = store.approval(raised.record.request_id)
    assert row is not None
    assert (row.decision, row.decided_by) == ("deny", "expiry")
    (event,) = sink.events
    assert event.type == "needs_human_resolved"
    assert event.payload["decided_by"] == "expiry"


def test_the_sweep_leaves_an_answered_request_alone(
    approvals: tr.ApprovalTransitions, sink: ev.NullEmitter, manifest: ResidentManifest
) -> None:
    raised = approvals.raise_request(
        manifest=manifest,
        request=NeedsHuman(raw="", action="send_email", expires_in_s=60),
        now=NOW,
    )
    assert raised.record is not None
    approvals.decide(raised.record.request_id, "approve", now=NOW)
    sink.events.clear()

    assert approvals.expire(NOW + timedelta(hours=1)) == []
    assert sink.events == []


# ----------------------------------------------------------------- delegation transitions


def test_delivering_a_handoff_writes_the_letter_and_names_both_ends(
    handoffs: tr.DelegationTransitions, store: Store, sink: ev.NullEmitter
) -> None:
    outcome = handoffs.deliver(
        title="read the shelf",
        detail="everything you need",
        assignee="shelf-worker",
        delegated_by="librarian",
        route="delegation",
        parent_task_id=None,
        origin="resident:librarian",
        depth=1,
        sender_agent_id="claude-code:librarian",
        sender_project="library",
        recipient_agent_id="claude-code:shelf-worker",
    )

    assert outcome.applied
    assert outcome.record is not None
    row = store.job(outcome.record.task_id)
    assert row is not None
    assert (row.status, row.assignee, row.delegated_by) == (
        STATUS_OPEN,
        "shelf-worker",
        "librarian",
    )
    assert store.inbox("shelf-worker") == [row]

    (event,) = sink.events
    assert event.type == "task_delegated"
    assert event.agent_id == "claude-code:librarian", "the villager carrying the letter walks"
    assert event.project == "library"
    assert event.payload["to"] == "claude-code:shelf-worker"
    assert event.payload["route"] == "delegation"
    assert event.payload["depth"] == 1
    assert event.payload["parent_task_id"] is None, "the delegation payload is explicit"


# --------------------------------------------------------------------- budget transitions


def knock(resident: str = "test-agent") -> NeedsHuman:
    return NeedsHuman(
        raw="budget daily_cost_usd exhausted",
        action="budget_unpause",
        detail={"resident": resident},
        options=("approve", "deny"),
        expires_in_s=None,
    )


def test_pausing_stops_the_resident_and_knocks_once(
    budgets: tr.BudgetTransitions, store: Store, sink: ev.NullEmitter, manifest: ResidentManifest
) -> None:
    outcome = budgets.pause(
        manifest=manifest,
        agent_id=CLAIMANT,
        budget="daily_cost_usd",
        spent=5.2,
        cap=5.0,
        reason="spent 5.20 of 5.00",
        knock=knock(),
        message="Testy has spent 5.20 of 5.00 daily_cost_usd today and is paused",
        window_end=WINDOW_END,
        now=NOW,
    )

    assert outcome.applied
    assert outcome.record is not None
    pause = store.budget_pause("test-agent")
    assert pause is not None
    assert pause.request_id is not None
    assert pause.request_id == outcome.record.request_id

    (event,) = sink.events
    assert event.type == "needs_human"
    assert event.payload["request_id"] == pause.request_id
    assert event.payload["expires_at"] is None, "a pause is already the safe state"
    request = store.approval(pause.request_id)
    assert request is not None
    assert request.pending


def test_a_second_pause_of_the_same_budget_adds_nothing_and_knocks_on_nobody(
    budgets: tr.BudgetTransitions, sink: ev.NullEmitter, manifest: ResidentManifest
) -> None:
    common: dict[str, Any] = {
        "manifest": manifest,
        "agent_id": CLAIMANT,
        "budget": "daily_cost_usd",
        "spent": 5.2,
        "cap": 5.0,
        "reason": "spent 5.20 of 5.00",
        "knock": knock(),
        "message": "paused",
        "window_end": WINDOW_END,
        "now": NOW,
    }
    first = budgets.pause(**common)
    sink.events.clear()

    outcome = budgets.pause(**common)

    assert outcome.superseded
    assert outcome.reason == tb.ALREADY_PAUSED
    assert first.record is not None
    assert outcome.record is not None
    assert outcome.record.request_id == first.record.request_id
    assert sink.events == []


def test_resuming_from_a_terminal_answers_the_request_that_was_waiting(
    budgets: tr.BudgetTransitions, store: Store, sink: ev.NullEmitter, manifest: ResidentManifest
) -> None:
    paused = budgets.pause(
        manifest=manifest,
        agent_id=CLAIMANT,
        budget="daily_cost_usd",
        spent=5.2,
        cap=5.0,
        reason="spent 5.20 of 5.00",
        knock=knock(),
        message="paused",
        window_end=WINDOW_END,
        now=NOW,
    )
    assert paused.record is not None
    assert paused.record.request_id is not None
    sink.events.clear()

    outcome = budgets.resume("test-agent", decided_by="cli")

    assert outcome.applied
    assert store.budget_pause("test-agent") is None
    assert store.budget_allowance("test-agent") is not None, "carry on, for today only"
    (event,) = sink.events
    assert event.type == "needs_human_resolved"
    assert event.payload["decision"] == "approve"
    assert event.payload["decided_by"] == "cli"
    request = store.approval(paused.record.request_id)
    assert request is not None
    assert request.decision == "approve"


def test_resuming_from_the_api_lifts_the_pause_without_answering_twice(
    budgets: tr.BudgetTransitions, store: Store, sink: ev.NullEmitter, manifest: ResidentManifest
) -> None:
    budgets.pause(
        manifest=manifest,
        agent_id=CLAIMANT,
        budget="daily_cost_usd",
        spent=5.2,
        cap=5.0,
        reason="spent 5.20 of 5.00",
        knock=knock(),
        message="paused",
        window_end=WINDOW_END,
        now=NOW,
    )
    sink.events.clear()

    outcome = budgets.resume("test-agent", decided_by="api", decide=False)

    assert outcome.applied
    assert outcome.silent, "the decision endpoint already said this"
    assert store.budget_pause("test-agent") is None
    assert sink.events == []


def test_resuming_against_a_request_a_person_already_denied_lifts_the_pause_and_keeps_the_deny(
    budgets: tr.BudgetTransitions, store: Store, sink: ev.NullEmitter, manifest: ResidentManifest
) -> None:
    """A terminal is a person too, but the first decision still wins in the log."""
    paused = budgets.pause(
        manifest=manifest,
        agent_id=CLAIMANT,
        budget="daily_cost_usd",
        spent=5.2,
        cap=5.0,
        reason="spent 5.20 of 5.00",
        knock=knock(),
        message="paused",
        window_end=WINDOW_END,
        now=NOW,
    )
    request_id = paused.require().request_id
    assert request_id is not None
    budgets.approvals.decide(request_id, "deny", decided_by="api", now=NOW)
    sink.events.clear()

    outcome = budgets.resume("test-agent", decided_by="cli")

    assert outcome.applied, "the unpause is this call's own durable change"
    assert store.budget_pause("test-agent") is None
    assert outcome.silent, "the deny was already answered; two answers is one too many"
    assert sink.events == []
    assert outcome.via is not None, "the inner decision is carried, not erased"
    assert outcome.via.replayed, "and the caller can see why nothing was emitted"
    request = store.approval(request_id)
    assert request is not None
    assert request.decision == "deny", "the first decision keeps the last word"


def test_resuming_a_pause_that_names_no_window_grants_no_allowance(
    budgets: tr.BudgetTransitions, store: Store, sink: ev.NullEmitter
) -> None:
    """The other side of the carry-on branch: a pause row written before windows existed.

    ``window_end`` defaults to empty in the store, so rows predating it reach ``resume``
    with nothing to scope an allowance to. The pause still lifts — that is this call's own
    durable act — but no "carry on" is granted, and the next fire re-trips the same cap.
    That is the shape the required ``window_end`` at the seam exists to keep new pauses out
    of; this asserts what happens to the old ones rather than leaving the branch untested.
    """
    store.pause_resident(
        resident="test-agent",
        agent_id=CLAIMANT,
        budget="daily_cost_usd",
        spent=5.2,
        cap=5.0,
        reason="spent 5.20 of 5.00",
        now=ev.utc_now_iso(NOW),
    )

    outcome = budgets.resume("test-agent", decided_by="cli")

    assert outcome.applied, "the pause lifts either way"
    assert store.budget_pause("test-agent") is None
    assert store.budget_allowance("test-agent") is None, "no window to scope a carry-on to"
    assert sink.events == [], "nobody asked, so there is nothing to answer"


def test_resuming_a_resident_that_was_never_paused_is_refused(
    budgets: tr.BudgetTransitions, sink: ev.NullEmitter
) -> None:
    outcome = budgets.resume("test-agent")

    assert outcome.refused
    assert outcome.record is None
    assert sink.events == []


def test_the_transitions_package_has_exactly_one_place_that_emits() -> None:
    """The structural half of the pairing invariant, asserted rather than trusted.

    An AST walk rather than a scan for ``.emit(`` in the source text, because the subject
    matter defeats a text scan in both directions. This package's own prose is *about*
    emitting, so one docstring sentence spelling the call out would fail the assertion with
    no bug present; and a glob over ``*.py`` cannot see into a subpackage somebody adds
    tomorrow, nor past a stray space in ``emitter . emit(fact)``.

    Still deliberately narrow: it cannot see through an alias (``send = emitter.emit``) or
    an emit reached by getattr. What it catches is the ordinary way a second emit site gets
    added, which is somebody writing ``emitter.emit(…)`` in a new branch. Counting first,
    and only then naming the file, so a walk that finds nothing fails with the sentence
    rather than an IndexError from the assertion trying to describe an empty list.
    """
    package = Path(__file__).resolve().parents[1] / "src" / "steward" / "transitions"
    emitting = [
        f"{path.relative_to(package)}:{node.lineno}"
        for path in sorted(package.rglob("*.py"))
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8")))
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "emit"
    ]
    assert len(emitting) == 1, (
        f"exactly one line in steward.transitions may reach an emitter; found {emitting}"
    )
    assert emitting[0].startswith("outcome.py:"), (
        f"the one line that emits must be the one in outcome.applied; found {emitting[0]}"
    )


# ------------------------------------------------------------- notifications (warren#114)


class RecordingTransport:
    """A transport that keeps every tap instead of sending it."""

    def __init__(self, *, raises: bool = False) -> None:
        """Start empty, optionally rigged to explode on the next send."""
        self.sent: list[nf.Tap] = []
        self.raises = raises

    @property
    def name(self) -> str:
        """Answer to the manifest word that selects it."""
        return nf.NTFY

    def address(self, manifest: ResidentManifest) -> str:
        """Say where it would have sent."""
        return f"fake://{manifest.id}"

    def send(self, manifest: ResidentManifest, tap: nf.Tap) -> bool:  # noqa: ARG002
        """Record the tap, or blow up if the test asked it to."""
        if self.raises:
            raise RuntimeError("no phone here")
        self.sent.append(tap)
        return True


@pytest.fixture
def taps() -> RecordingTransport:
    return RecordingTransport()


@pytest.fixture
def tapping_manifest(write_resident: ResidentWriter) -> ResidentManifest:
    data = {**valid_manifest(), "notifications": {"transport": "ntfy", "on": ["needs_human"]}}
    return load_manifest(write_resident(data)).manifest


def knocking(
    store: Store, sink: ev.NullEmitter, taps: RecordingTransport
) -> tr.ApprovalTransitions:
    """Build the approval seam over a transport that records rather than sends."""
    return tr.ApprovalTransitions(store=store, emitter=sink, notifier=nf.Notifier({nf.NTFY: taps}))


def test_a_raised_request_also_taps_the_declared_transport(
    store: Store,
    sink: ev.NullEmitter,
    taps: RecordingTransport,
    tapping_manifest: ResidentManifest,
) -> None:
    knocking(store, sink, taps).raise_request(
        manifest=tapping_manifest,
        request=NeedsHuman(raw="", action="send_email", detail={"to": "a@example.com"}),
        now=NOW,
    )
    (tap,) = taps.sent
    assert tap.kind == "needs_human"
    assert tap.title == "Testy wants to send email"
    assert "a@example.com" in tap.body


def test_a_knock_steward_raised_itself_taps_too(
    store: Store,
    sink: ev.NullEmitter,
    taps: RecordingTransport,
    tapping_manifest: ResidentManifest,
) -> None:
    """A budget pause, a watchdog give-up and a refused handoff all come through here."""
    knocking(store, sink, taps).knock(
        manifest=tapping_manifest,
        request=NeedsHuman(raw="", action="budget_exceeded"),
        message="Testy has spent $5.00 of $5.00 today",
        now=NOW,
    )
    (tap,) = taps.sent
    assert tap.title == "Testy has spent $5.00 of $5.00 today"


def test_an_undeclared_resident_writes_the_row_and_taps_nobody(
    store: Store, sink: ev.NullEmitter, taps: RecordingTransport, manifest: ResidentManifest
) -> None:
    outcome = knocking(store, sink, taps).raise_request(
        manifest=manifest, request=NeedsHuman(raw="", action="send_email"), now=NOW
    )
    assert outcome.applied
    assert len(sink.events) == 1
    assert taps.sent == []


def test_the_repeat_guard_silences_the_phone_as_well_as_the_village(
    store: Store,
    sink: ev.NullEmitter,
    taps: RecordingTransport,
    tapping_manifest: ResidentManifest,
) -> None:
    """The whole point of the guard: a looping resident must not buzz on every wake-up."""
    seam = knocking(store, sink, taps)
    first = seam.raise_request(
        manifest=tapping_manifest, request=NeedsHuman(raw="", action="send_email"), now=NOW
    )
    assert first.record is not None
    seam.decide(first.record.request_id, "deny", now=NOW)
    taps.sent.clear()

    outcome = seam.raise_request(
        manifest=tapping_manifest,
        request=NeedsHuman(raw="", action="send_email"),
        now=NOW + timedelta(hours=1),
    )
    assert outcome.answered
    assert taps.sent == []


def test_an_exploding_transport_cannot_fail_the_knock_it_reports(
    store: Store, sink: ev.NullEmitter, tapping_manifest: ResidentManifest
) -> None:
    """The load-bearing promise: a courtesy must never take the durable change with it."""
    seam = tr.ApprovalTransitions(
        store=store,
        emitter=sink,
        notifier=nf.Notifier({nf.NTFY: RecordingTransport(raises=True)}),
    )
    outcome = seam.raise_request(
        manifest=tapping_manifest, request=NeedsHuman(raw="", action="send_email"), now=NOW
    )
    assert outcome.applied
    assert store.approval(outcome.require().request_id) is not None
    assert [event.type for event in sink.events] == ["needs_human"]


def test_a_secret_is_scrubbed_on_the_way_to_the_phone_too(
    store: Store,
    sink: ev.NullEmitter,
    taps: RecordingTransport,
    tapping_manifest: ResidentManifest,
) -> None:
    knocking(store, sink, taps).raise_request(
        manifest=tapping_manifest,
        request=NeedsHuman(
            raw="", action="rotate_token", detail={"key": "sk-ant-api03-DEADBEEFDEADBEEFDEADBEEF"}
        ),
        now=NOW,
    )
    (tap,) = taps.sent
    assert "sk-ant-api03" not in tap.body
    assert SECRET_REDACTION in tap.body
