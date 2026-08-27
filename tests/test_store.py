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
        # The run registry (#39) is a whole table too, and an old database gets it empty.
        assert migrated.open_runs() == []


def test_a_ledger_written_before_origin_existed_keeps_every_row(tmp_path: Path) -> None:
    """``run_ledger.origin`` (#45) is the same ALTER TABLE: an old ledger loses nothing.

    The rollup falls back to the task the row's ``ref`` names, which is the answer the old
    database already gave. New rows say it for themselves; old ones keep what they had.
    """
    path = tmp_path / "legacy-ledger.db"
    legacy = sqlite3.connect(path)
    with legacy:
        legacy.execute(
            "CREATE TABLE run_ledger (entry_id TEXT PRIMARY KEY, resident TEXT NOT NULL, "
            "agent_id TEXT NOT NULL, kind TEXT NOT NULL, run_id TEXT NOT NULL, "
            "ref TEXT NOT NULL DEFAULT '', outcome TEXT NOT NULL DEFAULT '', "
            "input_tokens INTEGER NOT NULL DEFAULT 0, output_tokens INTEGER NOT NULL DEFAULT 0, "
            "cost_usd REAL NOT NULL DEFAULT 0.0, duration_s REAL NOT NULL DEFAULT 0.0, "
            "usage_known INTEGER NOT NULL DEFAULT 1, recorded_at TEXT NOT NULL)"
        )
        legacy.execute(
            "INSERT INTO run_ledger (entry_id, resident, agent_id, kind, run_id, ref, cost_usd, "
            "recorded_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            ("e-1", "hob", "a:hob", "delegated", "old-task", "old-task", 2.5, EARLY),
        )
    legacy.close()

    with Store(path) as migrated:
        (entry,) = migrated.ledger("hob")
        assert (entry.kind, entry.cost_usd, entry.origin) == ("delegated", 2.5, "")
        migrated.delegate_job(
            title="the chain the old row came off",
            assignee="hob",
            delegated_by="maren",
            route="inbox",
            origin="task:root",
            task_id="old-task",
        )

        (rolled,) = migrated.spend_by_origin()

        assert (rolled.origin, rolled.cost_usd) == ("task:root", pytest.approx(2.5))


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


def _ask(store: Store, action: str = "send_email", resident: str = "life-agent") -> ApprovalRecord:
    return store.create_approval_request(
        agent_id="a:b", project="p", action=action, message="…", resident=resident
    )


def test_recent_denials_counts_every_way_a_resident_was_told_no(store: Store) -> None:
    """A human's deny and expiry's deny-by-default are the same answer to the resident."""
    store.decide(_ask(store).request_id, "deny", decided_by="api", now=LATER)
    store.create_approval_request(
        agent_id="a:b",
        project="p",
        action="send_email",
        message="…",
        resident="life-agent",
        expires_at=EARLY,
    )
    store.expire_approvals(LATER)
    assert store.recent_denials("life-agent", "send_email", EARLY) == 2


def test_recent_denials_ignores_everything_that_is_not_this_residents_no(store: Store) -> None:
    store.decide(_ask(store).request_id, "approve", decided_by="api", now=LATER)
    store.decide(_ask(store, action="spend").request_id, "deny", decided_by="api", now=LATER)
    store.decide(_ask(store, resident="burrow-builder").request_id, "deny", now=LATER)
    _ask(store)  # Still pending: nobody has answered it either way.
    assert store.recent_denials("life-agent", "send_email", EARLY) == 0


def test_recent_denials_starts_counting_at_since(store: Store) -> None:
    store.decide(_ask(store).request_id, "deny", decided_by="api", now=EARLY)
    assert store.recent_denials("life-agent", "send_email", EARLY) == 1
    assert store.recent_denials("life-agent", "send_email", LATER) == 0


def test_an_auto_denied_request_is_filed_resolved_and_not_counted_as_a_no(store: Store) -> None:
    """Steward's own repeat deny is on the record, but it must not renew its own window."""
    record = store.create_approval_request(
        agent_id="a:b",
        project="p",
        action="send_email",
        message="…",
        resident="life-agent",
        denied_by="repeat",
    )
    assert not record.pending
    assert record.decision == "deny"
    assert record.decided_at == record.created_at
    assert store.pending_approvals() == []
    assert store.recent_denials("life-agent", "send_email", EARLY) == 0
    # It is a decision like any other, so the resident is told about it on its next run.
    assert [r.request_id for r in store.undelivered_decisions("life-agent")] == [record.request_id]


def test_the_denials_lookup_has_an_index_to_read(store: Store) -> None:
    """The approvals table grows one row per ask; the guard runs on every knock."""
    indexes = {
        row["name"]
        for row in store._conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'index' AND tbl_name = 'approvals'"
        ).fetchall()
    }
    assert "approvals_denials" in indexes


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


def test_the_store_waits_for_a_competing_writer(store: Store) -> None:
    assert store._conn.execute("PRAGMA busy_timeout").fetchone()[0] == 5000


def test_ledger_failures_are_durable_and_counted(tmp_path: Path) -> None:
    path = tmp_path / "steward.db"
    with Store(path) as store:
        store.record_ledger_failure(resident="hob", run_id="run-1", error="locked", now=EARLY)
        store.record_ledger_failure(resident="pip", run_id="run-2", error="disk full", now=LATER)
    with Store(path) as reopened:
        assert reopened.ledger_failures() == {
            "id": 1,
            "failures": 2,
            "last_resident": "pip",
            "last_run_id": "run-2",
            "last_error": "disk full",
            "last_failed_at": LATER,
        }


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
    for resident in ("hob", "pip"):
        store.record_run(
            resident=resident,
            agent_id=f"a:{resident}",
            kind="delegated",
            run_id=f"{resident}-hop",
            ref=f"{resident}-hop",
            origin="task:root",
            cost_usd=1.5,
            input_tokens=10,
            output_tokens=5,
        )

    (rolled,) = store.spend_by_origin()

    assert rolled.origin == "task:root"
    assert rolled.runs == 2
    assert rolled.cost_usd == pytest.approx(3.0)
    assert rolled.tokens == 30


def test_a_run_recorded_without_an_origin_is_named_rather_than_dropped(store: Store) -> None:
    """Nothing said where it came from, and money steward cannot attribute is still money."""
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


def test_a_ref_that_collides_with_a_task_id_does_not_inherit_that_task(store: Store) -> None:
    """The regression the denormalized column exists for (#45).

    A routine is ledgered under its own id, and nothing stops a resident naming a routine
    what some task's id happens to be. Rolling spend up by joining ``ref`` to
    ``jobs.task_id`` handed that routine somebody else's bill; the row now says what it
    descends from, so the join has nothing left to guess at.
    """
    task = store.delegate_job(
        title="the real chain", assignee="hob", delegated_by="maren", route="inbox", origin="task:x"
    )
    store.record_run(
        resident="hob",
        agent_id="a:hob",
        kind="routine",
        run_id="r",
        ref=task.task_id,  # a routine whose id collides with a real task's
        origin="resident:hob",
        cost_usd=2.0,
    )

    rollup = {spend.origin: spend.cost_usd for spend in store.spend_by_origin()}

    assert rollup == {"resident:hob": pytest.approx(2.0)}


def test_a_row_written_before_the_column_existed_still_rolls_up_by_its_task(store: Store) -> None:
    """The fallback the migration leaves behind: an old row keeps the answer it had."""
    task = store.delegate_job(
        title="claimed before #45",
        assignee="hob",
        delegated_by="maren",
        route="inbox",
        origin="task:root",
    )
    store.record_run(
        resident="hob",
        agent_id="a:hob",
        kind="delegated",
        run_id=task.task_id,
        ref=task.task_id,
        cost_usd=4.0,
    )  # no origin — exactly the shape ALTER TABLE left every pre-migration row in

    (rolled,) = store.spend_by_origin()

    assert (rolled.origin, rolled.cost_usd) == ("task:root", pytest.approx(4.0))


def test_the_rollup_is_ordered_by_what_each_origin_cost(store: Store) -> None:
    """Whoever is spending the most is the first line somebody reads."""
    for origin, cost in (("task:cheap", 1.0), ("task:dear", 9.0)):
        store.record_run(
            resident="hob",
            agent_id="a:hob",
            kind="delegated",
            run_id=origin,
            ref=origin,
            origin=origin,
            cost_usd=cost,
        )

    assert [spend.origin for spend in store.spend_by_origin()] == ["task:dear", "task:cheap"]


# ------------------------------------------------------------------- the run registry


def test_an_opened_run_is_open_until_it_is_closed(store: Store) -> None:
    """Steward's own record that a session exists, independent of where its events went."""
    assert store.open_run(
        run_id="r1",
        kind="routine",
        agent_id="claude-code:hob",
        project="household",
        ref="daily-summary",
        timeout_s=900.0,
        now=EARLY,
    )

    (opened,) = store.open_runs()
    assert (opened.run_id, opened.kind, opened.ref) == ("r1", "routine", "daily-summary")
    assert opened.timeout_s == pytest.approx(900.0)
    assert opened.open

    assert store.close_run("r1", now=LATER)
    assert store.open_runs() == []


def test_one_run_id_is_one_run(store: Store) -> None:
    """A second open of the same id is a repeat, not a second session."""
    assert store.open_run(run_id="r1", kind="routine", agent_id="a:hob", now=EARLY)
    assert not store.open_run(run_id="r1", kind="routine", agent_id="a:hob", now=LATER)
    assert [run.started_at for run in store.open_runs()] == [EARLY]


def test_a_run_is_closed_once_however_late_its_session_reports(store: Store) -> None:
    """A run the watchdog already buried is not re-answered by a session that turned up."""
    store.open_run(run_id="r1", kind="routine", agent_id="a:hob", now=EARLY)

    assert store.close_run("r1", now=EARLY)
    assert not store.close_run("r1", now=LATER)


def test_closing_a_run_nobody_opened_changes_nothing(store: Store) -> None:
    """A close with no row is a no-op, not a row invented to close."""
    assert not store.close_run("never-started")
    assert store.open_runs() == []
