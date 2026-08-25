"""The store is what makes the API's answers survive a restart, so it is tested alone."""

import sqlite3
import threading
from collections.abc import Iterator
from pathlib import Path

import pytest

from steward.events import utc_now_iso
from steward.store import (
    ORIGIN_UNATTRIBUTED,
    ApprovalRecord,
    JobRecord,
    RequestRecord,
    Store,
    default_db_path,
)

EARLY = "2026-08-24T09:00:00.000Z"
LATER = "2026-08-24T10:00:00.000Z"


@pytest.fixture
def store() -> Iterator[Store]:
    with Store(":memory:") as opened:
        yield opened


def _job(store: Store, task_id: str) -> JobRecord:
    found = store.job(task_id)
    assert found is not None
    return found


def _approval(store: Store, request_id: str) -> ApprovalRecord:
    found = store.approval(request_id)
    assert found is not None
    return found


def _request(store: Store, request_id: str) -> RequestRecord:
    found = store.request(request_id)
    assert found is not None
    return found


def test_a_posted_job_comes_back_open_and_unclaimed(store: Store) -> None:
    job = store.post_job(
        title="Research X", detail="the long version", required_skills=["research"]
    )
    assert job.status == "open"
    assert job.claimant is None
    assert store.jobs()[0].to_dict() == job.to_dict()
    assert store.job("nobody") is None


def test_required_skills_round_trip_as_a_list(store: Store) -> None:
    job = store.post_job(title="Draft a post", required_skills=["writing", "research"])
    assert _job(store, job.task_id).required_skills == ("writing", "research")


def test_the_board_survives_reopening_the_database(tmp_path: Path) -> None:
    path = tmp_path / "state" / "steward.db"
    with Store(path) as first:
        task_id = first.post_job(title="Outlive a restart").task_id
    with Store(path) as second:
        assert [job.task_id for job in second.jobs()] == [task_id]
        assert _job(second, task_id).title == "Outlive a restart"


# ------------------------------------------------------------------------ claiming


def test_claiming_takes_the_oldest_open_task_the_skills_cover(store: Store) -> None:
    old = store.post_job(title="Older", required_skills=["research"])
    store.post_job(title="Newer")
    claimed = store.claim_next_job(
        claimant="claude-code:life-agent", skills=["research"], lease_expires_at=LATER
    )
    assert claimed is not None
    assert claimed.task_id == old.task_id
    assert claimed.status == "claimed"
    assert claimed.claimant == "claude-code:life-agent"
    assert claimed.lease_expires_at == LATER


def test_a_task_is_never_claimed_by_a_resident_missing_a_skill(store: Store) -> None:
    store.post_job(title="Needs a skill", required_skills=["surgery"])
    assert store.claim_next_job(claimant="a:b", skills=["research"], lease_expires_at=LATER) is None


def test_an_untagged_task_is_claimable_by_anybody(store: Store) -> None:
    store.post_job(title="Anyone can do this")
    claimed = store.claim_next_job(claimant="a:b", skills=[], lease_expires_at=LATER)
    assert claimed is not None
    assert claimed.required_skills == ()


def test_a_claimed_task_is_skipped_and_the_next_open_one_taken(store: Store) -> None:
    first = store.post_job(title="First")
    second = store.post_job(title="Second")
    assert store.claim_next_job(claimant="a:b", skills=[], lease_expires_at=LATER) is not None
    mine = store.claim_next_job(claimant="c:d", skills=[], lease_expires_at=LATER)
    assert mine is not None
    assert mine.task_id == second.task_id
    assert _job(store, first.task_id).claimant == "a:b"


def test_two_threads_racing_one_open_task_yield_exactly_one_claimant(tmp_path: Path) -> None:
    """The whole promise of the board: SQLite serialises, and one caller loses."""
    path = tmp_path / "race.db"
    with Store(path) as seed:
        seed.post_job(title="The only task")

    gate = threading.Barrier(2)
    winners: list[JobRecord | None] = []
    lock = threading.Lock()

    def contend(claimant: str) -> None:
        with Store(path) as contender:
            gate.wait(timeout=5)
            try:
                claimed = contender.claim_next_job(
                    claimant=claimant, skills=[], lease_expires_at=LATER
                )
            except sqlite3.OperationalError:  # pragma: no cover — a busy file, still not two claims
                claimed = None
            with lock:
                winners.append(claimed)

    threads = [
        threading.Thread(target=contend, args=("claude-code:hob",)),
        threading.Thread(target=contend, args=("claude-code:maren",)),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    claimed = [record for record in winners if record is not None]
    assert len(claimed) == 1, "two residents must never both hold one task"
    with Store(path) as after:
        assert after.jobs("claimed")[0].claimant == claimed[0].claimant


def test_finishing_records_the_outcome_and_the_artifacts(store: Store) -> None:
    posted = store.post_job(title="Write it up")
    store.claim_next_job(claimant="a:b", skills=[], lease_expires_at=LATER)
    closed = store.finish_job(
        posted.task_id,
        status="done",
        claimant="a:b",
        outcome="ok",
        artifacts=["notes/report.md"],
    )
    assert closed is not None
    assert closed.status == "done"
    assert closed.artifacts == ("notes/report.md",)
    assert closed.lease_expires_at is None
    assert closed.finished_at
    assert store.jobs("done") == [closed]


def test_only_the_holder_of_the_claim_may_finish_it(store: Store) -> None:
    posted = store.post_job(title="Not yours")
    store.claim_next_job(claimant="a:b", skills=[], lease_expires_at=LATER)
    assert store.finish_job(posted.task_id, status="done", claimant="c:d") is None
    assert _job(store, posted.task_id).status == "claimed"


def test_a_stale_session_cannot_close_the_live_re_claim(store: Store) -> None:
    """A dead lease's old handle carries the old stamp; the live re-claim carries a new one."""
    posted = store.post_job(title="Handed back and forth")
    first = store.claim_next_job(
        claimant="a:b", skills=[], lease_expires_at=EARLY, now="2026-08-24T08:00:00.000Z"
    )
    assert first is not None
    # The lease dies and the sweep reopens the task; the same claimant picks it back up.
    store.expire_leases(LATER)
    second = store.claim_next_job(
        claimant="a:b",
        skills=[],
        lease_expires_at="2026-08-24T12:00:00.000Z",
        now="2026-08-24T09:30:00.000Z",
    )
    assert second is not None
    assert first.claimed_at != second.claimed_at

    # The dead handle's stamp no longer matches the live row: its close is rejected.
    assert (
        store.finish_job(posted.task_id, status="done", claimant="a:b", lease=first.claimed_at)
        is None
    )
    assert _job(store, posted.task_id).status == "claimed"
    # The live handle closes it.
    closed = store.finish_job(
        posted.task_id, status="done", claimant="a:b", lease=second.claimed_at
    )
    assert closed is not None
    assert closed.status == "done"


def test_an_expired_lease_reopens_the_task_and_names_who_dropped_it(store: Store) -> None:
    posted = store.post_job(title="Abandoned")
    store.claim_next_job(claimant="claude-code:hob", skills=[], lease_expires_at=EARLY)
    expired = store.expire_leases(LATER)
    assert [record.task_id for record in expired] == [posted.task_id]
    assert expired[0].claimant == "claude-code:hob", "the event has to name the claimant"
    reopened = _job(store, posted.task_id)
    assert reopened.status == "open"
    assert reopened.claimant is None
    assert reopened.lease_expires_at is None


def test_a_live_lease_is_left_alone(store: Store) -> None:
    store.post_job(title="Still working")
    store.claim_next_job(claimant="a:b", skills=[], lease_expires_at=LATER)
    assert store.expire_leases(EARLY) == []
    assert store.jobs("claimed")[0].claimant == "a:b"


def test_the_board_filters_by_status(store: Store) -> None:
    store.post_job(title="To be claimed")
    store.post_job(title="Still open")
    store.claim_next_job(claimant="a:b", skills=[], lease_expires_at=LATER)
    assert [job.title for job in store.jobs("open")] == ["Still open"]
    assert [job.title for job in store.jobs("claimed")] == ["To be claimed"]
    assert len(store.jobs()) == 2


def test_a_claim_survives_reopening_the_database(tmp_path: Path) -> None:
    path = tmp_path / "steward.db"
    with Store(path) as first:
        task_id = first.post_job(title="Held across a restart").task_id
        first.claim_next_job(claimant="a:b", skills=[], lease_expires_at=LATER)
    with Store(path) as second:
        held = second.job(task_id)
        assert held is not None
        assert (held.status, held.claimant, held.lease_expires_at) == ("claimed", "a:b", LATER)


def test_a_database_written_before_claiming_existed_still_opens(tmp_path: Path) -> None:
    """The migration is ALTER TABLE, not DROP: an old board keeps every row it had."""
    path = tmp_path / "legacy.db"
    legacy = sqlite3.connect(path)
    with legacy:
        legacy.execute(
            "CREATE TABLE jobs (task_id TEXT PRIMARY KEY, title TEXT NOT NULL, "
            "detail TEXT NOT NULL DEFAULT '', required_skills TEXT NOT NULL DEFAULT '[]', "
            "status TEXT NOT NULL DEFAULT 'open', posted_by TEXT NOT NULL, claimant TEXT, "
            "created_at TEXT NOT NULL)"
        )
        legacy.execute(
            "INSERT INTO jobs (task_id, title, posted_by, created_at) VALUES (?, ?, ?, ?)",
            ("old-1", "Posted before leases existed", "api", EARLY),
        )
    legacy.close()

    with Store(path) as migrated:
        job = migrated.job("old-1")
        assert job is not None
        assert job.title == "Posted before leases existed"
        assert job.artifacts == ()
        assert job.lease_expires_at is None
        claimed = migrated.claim_next_job(claimant="a:b", skills=[], lease_expires_at=LATER)
        assert claimed is not None

        # Delegation (#7) added columns to this same table; the old row reads back with
        # the defaults rather than blowing up, and it is not somebody's post.
        assert (job.assignee, job.delegated_by, job.route, job.origin) == (None, None, None, None)
        assert job.depth == 0
        assert not job.delegated
        assert migrated.inbox("a") == []

        # The watchdog and budgets (#8) added whole tables. Both evolutions have to land
        # on the same database — a steward upgraded across two releases at once is the
        # ordinary case, not an exotic one — so the combined schema is exercised here in
        # one open, on a file that predates both.
        letter = migrated.delegate_job(
            title="Read the background", assignee="hob", delegated_by="maren", route="inbox"
        )
        assert letter.origin is None
        assert [item.task_id for item in migrated.inbox("hob")] == [letter.task_id]
        migrated.record_run(
            resident="hob", agent_id="a:b", kind="delegated", run_id="r", ref=letter.task_id
        )
        assert [entry.kind for entry in migrated.ledger("hob")] == ["delegated"]
        assert migrated.budget_pause("hob") is None
        assert migrated.last_watchdog_pass() is None


# ------------------------------------------------------------------------ approvals


def test_an_approval_request_starts_pending(store: Store) -> None:
    record = store.create_approval_request(
        agent_id="claude-code:life-agent",
        project="household",
        action="send_email",
        message="Hob wants to send an email to the plumber",
        detail={"to": "plumber@example.com"},
    )
    assert record.pending
    assert [pending.request_id for pending in store.pending_approvals()] == [record.request_id]
    assert _approval(store, record.request_id).detail == {"to": "plumber@example.com"}


def test_the_first_decision_wins_and_later_ones_read_it_back(store: Store) -> None:
    record = store.create_approval_request(
        agent_id="claude-code:life-agent",
        project="household",
        action="send_email",
        message="Hob wants to send an email",
    )
    decided, recorded = store.decide(record.request_id, "approve", decided_by="api")
    assert recorded is True
    assert decided is not None
    assert decided.decision == "approve"
    assert decided.decided_at

    again, recorded_again = store.decide(record.request_id, "deny", decided_by="api")
    assert recorded_again is False
    assert again is not None
    assert again.decision == "approve"
    assert store.pending_approvals() == []


def test_an_edit_decision_keeps_the_humans_version(store: Store) -> None:
    record = store.create_approval_request(
        agent_id="a:b", project="p", action="send_email", message="…"
    )
    decided, _ = store.decide(record.request_id, "edit", edit={"subject": "shorter"})
    assert decided is not None
    assert decided.edit == {"subject": "shorter"}
    assert decided.to_dict()["edit"] == {"subject": "shorter"}


def test_deciding_an_unknown_request_records_nothing(store: Store) -> None:
    record, recorded = store.decide("no-such-request", "approve")
    assert record is None
    assert recorded is False


def test_a_pending_request_is_still_pending_after_a_restart(tmp_path: Path) -> None:
    path = tmp_path / "steward.db"
    with Store(path) as first:
        request_id = first.create_approval_request(
            agent_id="a:b", project="p", action="spend", message="…"
        ).request_id
    with Store(path) as second:
        assert [r.request_id for r in second.pending_approvals()] == [request_id]


def test_an_expired_request_is_denied_by_default_and_says_who_by(store: Store) -> None:
    record = store.create_approval_request(
        agent_id="a:b", project="p", action="send_email", message="…", expires_at=EARLY
    )
    expired = store.expire_approvals(LATER)
    assert [r.request_id for r in expired] == [record.request_id]
    assert expired[0].decision == "deny"
    assert expired[0].decided_by == "expiry"
    assert store.pending_approvals() == []


def test_a_request_that_a_human_already_answered_is_not_re_expired(store: Store) -> None:
    record = store.create_approval_request(
        agent_id="a:b", project="p", action="spend", message="…", expires_at=LATER
    )
    # Answered before the deadline, so it is recorded; the later sweep leaves it alone.
    _, recorded = store.decide(record.request_id, "approve", decided_by="api", now=EARLY)
    assert recorded
    assert store.expire_approvals(LATER) == []
    assert _approval(store, record.request_id).decision == "approve"


def test_an_expired_request_is_refused_as_expired_not_approved(store: Store) -> None:
    """A human clicking approve after the deadline cannot slip an expired action through."""
    record = store.create_approval_request(
        agent_id="a:b", project="p", action="spend", message="…", expires_at=EARLY
    )
    decided, recorded = store.decide(record.request_id, "approve", decided_by="api", now=LATER)
    assert recorded is False, "an expired request is not decided"
    # Still pending — distinct from an already-decided replay, which reads back resolved —
    # so the deny-by-default sweep can close the loop in the log.
    assert decided is not None
    assert decided.pending
    assert decided.decision is None
    (denied,) = store.expire_approvals(LATER)
    assert denied.decision == "deny"
    assert denied.decided_by == "expiry"


def test_a_request_with_no_deadline_never_expires(store: Store) -> None:
    store.create_approval_request(agent_id="a:b", project="p", action="spend", message="…")
    assert store.expire_approvals(LATER) == []
    assert len(store.pending_approvals()) == 1


def test_a_decision_is_delivered_to_its_resident_exactly_once(store: Store) -> None:
    record = store.create_approval_request(
        agent_id="a:b", project="p", action="send_email", message="…", resident="life-agent"
    )
    assert store.undelivered_decisions("life-agent") == [], "pending decides nothing yet"
    store.decide(record.request_id, "approve")

    waiting = store.undelivered_decisions("life-agent")
    assert [r.request_id for r in waiting] == [record.request_id]
    assert store.mark_delivered([record.request_id]) == 1
    assert store.undelivered_decisions("life-agent") == []
    assert store.mark_delivered([record.request_id]) == 0, "delivering twice marks nothing"
    assert _approval(store, record.request_id).delivered_at


def test_claiming_decisions_marks_them_delivered_in_one_pass(store: Store) -> None:
    record = store.create_approval_request(
        agent_id="a:b", project="p", action="send_email", message="…", resident="life-agent"
    )
    store.decide(record.request_id, "approve")
    claimed = store.claim_undelivered_decisions("life-agent")
    assert [r.request_id for r in claimed] == [record.request_id]
    # The read and the mark were one pass: a second wake-up finds nothing.
    assert store.claim_undelivered_decisions("life-agent") == []
    assert _approval(store, record.request_id).delivered_at


def test_two_concurrent_wake_ups_claim_a_decision_exactly_once(tmp_path: Path) -> None:
    """The read-then-mark is atomic, so a decision reaches exactly one of two racing sessions."""
    with Store(tmp_path / "steward.db") as store:
        record = store.create_approval_request(
            agent_id="a:b", project="p", action="send_email", message="…", resident="life-agent"
        )
        store.decide(record.request_id, "approve")

        barrier = threading.Barrier(2)
        results: list[list[ApprovalRecord]] = []
        lock = threading.Lock()

        def grab() -> None:
            barrier.wait()
            claimed = store.claim_undelivered_decisions("life-agent")
            with lock:
                results.append(claimed)

        threads = [threading.Thread(target=grab) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        assert sum(len(claimed) for claimed in results) == 1, "delivered exactly once"


def test_one_resident_never_reads_another_residents_decisions(store: Store) -> None:
    mine = store.create_approval_request(
        agent_id="a:b", project="p", action="spend", message="…", resident="life-agent"
    )
    store.decide(mine.request_id, "deny")
    assert store.undelivered_decisions("burrow-builder") == []


def test_the_audit_view_holds_the_request_and_its_decision(store: Store) -> None:
    record = store.create_approval_request(
        agent_id="a:b", project="p", action="send_email", message="…", resident="life-agent"
    )
    store.decide(record.request_id, "edit", decided_by="api", edit={"subject": "shorter"})
    audited = store.approvals()
    assert len(audited) == 1
    assert audited[0].to_dict()["decision"] == "edit"
    assert audited[0].to_dict()["resident"] == "life-agent"
    assert store.approvals("pending") == []
    assert [r.request_id for r in store.approvals("resolved")] == [record.request_id]


def test_a_decision_and_its_delivery_survive_a_restart(tmp_path: Path) -> None:
    path = tmp_path / "steward.db"
    with Store(path) as first:
        request_id = first.create_approval_request(
            agent_id="a:b", project="p", action="spend", message="…", resident="life-agent"
        ).request_id
        first.decide(request_id, "deny")
    with Store(path) as second:
        assert [r.request_id for r in second.undelivered_decisions("life-agent")] == [request_id]
        second.mark_delivered([request_id])
    with Store(path) as third:
        assert third.undelivered_decisions("life-agent") == []


def test_now_defaults_to_the_wall_clock(store: Store) -> None:
    """The sweeps take a moment for tests; left alone they use the real one."""
    store.post_job(title="Now")
    claimed = store.claim_next_job(claimant="a:b", skills=[], lease_expires_at=utc_now_iso())
    assert claimed is not None
    assert claimed.claimed_at
    assert [record.task_id for record in store.expire_leases()] == [claimed.task_id]


def test_the_request_log_records_what_became_of_a_request(store: Store) -> None:
    store.log_request(
        request_id="r1", method="POST", path="/jobs", outcome="queued", detail={"task_id": "t"}
    )
    assert _request(store, "r1").outcome == "queued"
    store.set_request_outcome("r1", "ran", {"run_id": "abc"})
    logged = _request(store, "r1")
    assert logged.outcome == "ran"
    assert logged.detail == {"run_id": "abc"}
    assert logged.to_dict()["path"] == "/jobs"
    assert [record.request_id for record in store.requests()] == ["r1"]


def test_updating_an_unknown_request_is_ignored(store: Store) -> None:
    store.set_request_outcome("never-logged", "ran")
    assert store.requests() == []


def test_outcome_can_be_updated_without_touching_the_detail(store: Store) -> None:
    store.log_request(request_id="r2", method="POST", path="/jobs", outcome="queued")
    store.set_request_outcome("r2", "failed")
    assert _request(store, "r2").outcome == "failed"


def test_the_database_lives_beside_the_scheduler_state(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("STEWARD_STATE", str(tmp_path / "state" / "scheduler.json"))
    assert default_db_path() == tmp_path / "state" / "steward.db"


# -------------------------------------------------------------------------- the inbox


def test_the_inbox_can_be_counted_without_being_read(store: Store) -> None:
    """``doctor`` prints one number, so it asks for one number."""
    store.delegate_job(
        title="Read the background", assignee="hob", delegated_by="maren", route="inbox"
    )
    store.delegate_job(title="And this", assignee="hob", delegated_by="maren", route="inbox")
    store.delegate_job(title="Not yours", assignee="pip", delegated_by="maren", route="inbox")

    assert store.inbox_count("hob") == 2
    assert store.inbox_count("nobody") == 0

    store.claim_next_delegated(assignee="hob", claimant="claude-code:hob", lease_expires_at=LATER)

    assert store.inbox_count("hob") == 1, "a claimed letter has left the pending pile"
    assert store.inbox_count("hob", None) == 2, "…but it is still somebody's post"


# -------------------------------------------------------------- the ledger, by origin


def test_spend_rolls_up_by_the_origin_a_task_carries(store: Store) -> None:
    """Two hops of one chain are one bill, because both rows carry the same origin."""
    first = store.delegate_job(
        title="first hop", assignee="hob", delegated_by="maren", route="inbox", origin="task:root"
    )
    second = store.delegate_job(
        title="second hop", assignee="pip", delegated_by="hob", route="inbox", origin="task:root"
    )
    for resident, task in (("hob", first), ("pip", second)):
        store.record_run(
            resident=resident,
            agent_id=f"a:{resident}",
            kind="delegated",
            run_id=task.task_id,
            ref=task.task_id,
            cost_usd=1.5,
            input_tokens=10,
            output_tokens=5,
        )

    (rolled,) = store.spend_by_origin()

    assert rolled.origin == "task:root"
    assert rolled.runs == 2
    assert rolled.cost_usd == pytest.approx(3.0)
    assert rolled.tokens == 30


def test_a_run_behind_no_task_is_named_rather_than_dropped(store: Store) -> None:
    """A routine has no chain above it, and money steward cannot attribute is still money."""
    store.record_run(
        resident="hob",
        agent_id="a:hob",
        kind="routine",
        run_id="r",
        ref="daily-summary",
        cost_usd=2.0,
    )

    (rolled,) = store.spend_by_origin()

    assert rolled.origin == ORIGIN_UNATTRIBUTED
    assert rolled.cost_usd == pytest.approx(2.0)


def test_the_rollup_is_ordered_by_what_each_origin_cost(store: Store) -> None:
    """Whoever is spending the most is the first line somebody reads."""
    for origin, cost in (("task:cheap", 1.0), ("task:dear", 9.0)):
        task = store.delegate_job(
            title=origin, assignee="hob", delegated_by="maren", route="inbox", origin=origin
        )
        store.record_run(
            resident="hob",
            agent_id="a:hob",
            kind="delegated",
            run_id=task.task_id,
            ref=task.task_id,
            cost_usd=cost,
        )

    assert [spend.origin for spend in store.spend_by_origin()] == ["task:dear", "task:cheap"]
