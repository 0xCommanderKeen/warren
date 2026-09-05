"""The store is what makes the API's answers survive a restart, so it is tested alone."""

import sqlite3
import threading
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

from steward import health, scheduler
from steward.events import utc_now_iso
from steward.operator_auth import new_operator_credential
from steward.runs import TRIGGER_MANUAL, TRIGGER_SCHEDULE
from steward.session_auth import credential_digest, new_session_credential
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


def test_an_existing_database_gains_the_approval_announcement_outbox(tmp_path: Path) -> None:
    path = tmp_path / "old.db"
    connection = sqlite3.connect(path)
    connection.execute(
        "CREATE TABLE approvals (request_id TEXT PRIMARY KEY, agent_id TEXT NOT NULL, "
        "project TEXT NOT NULL, action TEXT NOT NULL, message TEXT NOT NULL, "
        "detail TEXT NOT NULL DEFAULT '{}', options TEXT NOT NULL DEFAULT '[]', "
        "status TEXT NOT NULL DEFAULT 'pending', decision TEXT, decided_by TEXT, "
        "decided_at TEXT, edit TEXT, expires_at TEXT, created_at TEXT NOT NULL)"
    )
    connection.commit()
    connection.close()

    with Store(path):
        pass

    connection = sqlite3.connect(path)
    columns = {row[1] for row in connection.execute("PRAGMA table_info(approval_announcements)")}
    connection.close()
    assert columns == {
        "request_id",
        "claimed_by",
        "claimed_until",
        "announced_at",
        "attempts",
        "next_attempt_at",
        "effects_at",
        "effects_claimed_by",
        "effects_claimed_until",
        "effects_attempts",
        "effects_next_attempt_at",
    }


def test_an_existing_approval_ledger_gains_the_consumption_marks(tmp_path: Path) -> None:
    """Every approval read goes through ``from_row``, so a missing column is a dead ledger."""
    path = tmp_path / "old-approvals.db"
    connection = sqlite3.connect(path)
    connection.execute(
        "CREATE TABLE approvals (request_id TEXT PRIMARY KEY, agent_id TEXT NOT NULL, "
        "project TEXT NOT NULL, action TEXT NOT NULL, message TEXT NOT NULL, "
        "detail TEXT NOT NULL DEFAULT '{}', options TEXT NOT NULL DEFAULT '[]', "
        "status TEXT NOT NULL DEFAULT 'pending', decision TEXT, decided_by TEXT, "
        "decided_at TEXT, edit TEXT, expires_at TEXT, created_at TEXT NOT NULL)"
    )
    connection.execute(
        "INSERT INTO approvals (request_id, agent_id, project, action, message, created_at) "
        "VALUES ('old-1', 'a:b', 'p', 'send_email', 'from before the column', "
        "'2026-01-01T00:00:00Z')"
    )
    connection.commit()
    connection.close()

    with Store(path) as store:
        carried_over = _approval(store, "old-1")
        assert carried_over.consumed_at is None
        assert carried_over.consumed_by == ""
        assert store.consume_approval("old-1", by="write-1") is True


def test_an_existing_request_log_gains_approval_correlation(tmp_path: Path) -> None:
    path = tmp_path / "old-requests.db"
    connection = sqlite3.connect(path)
    connection.execute(
        "CREATE TABLE requests (request_id TEXT PRIMARY KEY, received_at TEXT NOT NULL, "
        "method TEXT NOT NULL, path TEXT NOT NULL, outcome TEXT NOT NULL, "
        "detail TEXT NOT NULL DEFAULT '{}')"
    )
    connection.commit()
    connection.close()

    with Store(path):
        pass

    connection = sqlite3.connect(path)
    columns = {row[1] for row in connection.execute("PRAGMA table_info(requests)")}
    connection.close()
    assert "approval_id" in columns


def test_two_independent_connections_cannot_claim_one_announcement(tmp_path: Path) -> None:
    path = tmp_path / "shared.db"
    first, second = Store(path), Store(path)
    try:
        record = first.create_approval_request(
            agent_id="claude-code:hob", project="home", action="send_email", message="ask"
        )
        first.decide(record.request_id, "approve")
        barrier = threading.Barrier(3)
        claims: list[object] = []

        def claim(opened: Store) -> None:
            barrier.wait()
            claims.append(opened.claim_approval_announcement())

        threads = [threading.Thread(target=claim, args=(opened,)) for opened in (first, second)]
        for thread in threads:
            thread.start()
        barrier.wait()
        for thread in threads:
            thread.join()
        assert sum(claim is not None for claim in claims) == 1
    finally:
        first.close()
        second.close()


def test_a_closed_letter_is_claimed_for_its_sender_once(store: Store) -> None:
    letter = store.delegate_job(
        title="Catalogue the new shelf",
        assignee="worker",
        delegated_by="librarian",
        route="inbox",
    )
    claimed = store.claim_next_delegated(
        assignee="worker",
        claimant="claude-code:worker",
        lease_expires_at=LATER,
        now=EARLY,
    )
    assert claimed is not None
    store.finish_job(
        letter.task_id,
        status="done",
        claimant="claude-code:worker",
        outcome="ok",
        final_message="Shelves A-C are catalogued.",
        lease=claimed.claimed_at,
        now=LATER,
    )

    (answer,) = store.claim_answered_letters("librarian")

    assert answer.title == "Catalogue the new shelf"
    assert answer.assignee == "worker"
    assert answer.status == "done"
    assert answer.final_message == "Shelves A-C are catalogued."
    assert answer.reply_delivered_at is not None
    assert store.claim_answered_letters("librarian") == []


def test_answered_letter_storage_bounds_the_receivers_final_message(store: Store) -> None:
    letter = store.delegate_job(
        title="Write a long report",
        assignee="worker",
        delegated_by="librarian",
        route="inbox",
    )
    claimed = store.claim_next_delegated(
        assignee="worker",
        claimant="claude-code:worker",
        lease_expires_at=LATER,
        now=EARLY,
    )
    assert claimed is not None

    closed = store.finish_job(
        letter.task_id,
        status="done",
        claimant="claude-code:worker",
        final_message="x" * 20_000,
        lease=claimed.claimed_at,
        now=LATER,
    )

    assert closed is not None
    assert len(closed.final_message) == 4_000
    assert closed.final_message.endswith("…")


def test_answered_letter_overflow_waits_for_the_next_wake(store: Store) -> None:
    for number in range(4):
        letter = store.delegate_job(
            title=f"Report {number}",
            assignee="worker",
            delegated_by="librarian",
            route="inbox",
        )
        claimed = store.claim_next_delegated(
            assignee="worker",
            claimant="claude-code:worker",
            lease_expires_at=LATER,
            now=EARLY,
        )
        assert claimed is not None
        store.finish_job(
            letter.task_id,
            status="done",
            claimant="claude-code:worker",
            final_message=str(number) * 4_000,
            lease=claimed.claimed_at,
            now=LATER,
        )

    first_wake = store.claim_answered_letters("librarian")
    second_wake = store.claim_answered_letters("librarian")

    assert 0 < len(first_wake) < 4
    assert len(first_wake) + len(second_wake) == 4
    assert store.claim_answered_letters("librarian") == []


def test_two_wake_ups_cannot_claim_the_same_answered_letter(tmp_path: Path) -> None:
    path = tmp_path / "shared.db"
    with Store(path) as setup:
        letter = setup.delegate_job(
            title="Count the books",
            assignee="worker",
            delegated_by="librarian",
            route="inbox",
        )
        claimed = setup.claim_next_delegated(
            assignee="worker",
            claimant="claude-code:worker",
            lease_expires_at=LATER,
            now=EARLY,
        )
        assert claimed is not None
        setup.finish_job(
            letter.task_id,
            status="done",
            claimant="claude-code:worker",
            final_message="Forty-two.",
            lease=claimed.claimed_at,
            now=LATER,
        )

    first, second = Store(path), Store(path)
    try:
        barrier = threading.Barrier(3)
        results: list[list[JobRecord]] = []

        def wake(opened: Store) -> None:
            barrier.wait()
            results.append(opened.claim_answered_letters("librarian"))

        threads = [threading.Thread(target=wake, args=(opened,)) for opened in (first, second)]
        for thread in threads:
            thread.start()
        barrier.wait()
        for thread in threads:
            thread.join()

        assert sum(len(answered) for answered in results) == 1
    finally:
        first.close()
        second.close()


def _acknowledged_budget_decision(store: Store) -> ApprovalRecord:
    request = store.create_approval_request(
        agent_id="claude-code:hob",
        project="home",
        action="budget_unpause",
        message="carry on?",
        resident="hob",
    )
    store.pause_resident(
        resident="hob",
        agent_id="claude-code:hob",
        budget="daily_cost_usd",
        spent=2,
        cap=1,
        reason="over",
        request_id=request.request_id,
        window_end=LATER,
    )
    decided, recorded = store.decide(request.request_id, "approve", decided_by="miha")
    assert recorded
    assert decided is not None
    announcement = store.claim_approval_announcement(request.request_id)
    assert announcement is not None
    _, token = announcement
    assert store.finish_approval_announcement(request.request_id, token, accepted=True)
    return decided


def test_budget_completion_and_marker_are_one_atomic_idempotent_act(store: Store) -> None:
    record = _acknowledged_budget_decision(store)
    claimed = store.claim_approval_effects(record.request_id)
    assert claimed is not None
    _, token = claimed

    completed, resumed = store.complete_approval_effects(record, token)

    assert completed
    assert resumed == "hob"
    assert store.budget_pause("hob") is None
    assert store.budget_allowance("hob")["until"] == LATER  # ty: ignore
    assert store.approval_announcement_state(record.request_id) == "complete"
    assert store.complete_approval_effects(record, token) == (False, None)


def test_approval_completion_finalizes_every_correlated_pending_request(store: Store) -> None:
    request = store.create_approval_request(
        agent_id="claude-code:hob", project="home", action="send_email", message="ask"
    )
    record, recorded = store.decide(
        request.request_id,
        "approve",
        request_log=("api-1", "POST", f"/approvals/{request.request_id}"),
    )
    assert recorded
    assert record is not None
    replay, recorded = store.decide(
        request.request_id,
        "deny",
        request_log=("api-2", "POST", f"/approvals/{request.request_id}"),
    )
    assert not recorded
    assert replay is not None
    assert [store.request(log_id).outcome for log_id in ("api-1", "api-2")] == [  # ty: ignore
        "recorded_announcement_pending",
        "recorded_announcement_pending",
    ]
    announcement = store.claim_approval_announcement(request.request_id)
    assert announcement is not None
    _, token = announcement
    assert store.finish_approval_announcement(request.request_id, token, accepted=True)
    effects = store.claim_approval_effects(request.request_id)
    assert effects is not None
    effect_record, token = effects
    assert store.complete_approval_effects(effect_record, token)[0]
    assert [store.request(log_id).outcome for log_id in ("api-1", "api-2")] == [  # ty: ignore
        "recorded",
        "recorded",
    ]


def test_two_store_workers_cannot_apply_the_same_effects(tmp_path: Path) -> None:
    path = tmp_path / "effects.db"
    first, second = Store(path), Store(path)
    try:
        record = _acknowledged_budget_decision(first)
        barrier = threading.Barrier(3)
        claims: list[tuple[ApprovalRecord, str] | None] = []

        def claim(opened: Store) -> None:
            barrier.wait()
            claims.append(opened.claim_approval_effects(record.request_id))

        threads = [threading.Thread(target=claim, args=(opened,)) for opened in (first, second)]
        for thread in threads:
            thread.start()
        barrier.wait()
        for thread in threads:
            thread.join()
        assert sum(claimed is not None for claimed in claims) == 1
    finally:
        first.close()
        second.close()


def test_abandoned_live_effects_lease_recovers_only_at_its_deadline(store: Store) -> None:
    record = _acknowledged_budget_decision(store)
    assert store.claim_approval_effects(record.request_id, lease_s=0.05) is not None
    assert store.claim_approval_effects(record.request_id) is None
    time.sleep(0.06)
    recovered = store.claim_approval_effects(record.request_id)
    assert recovered is not None
    _, token = recovered
    assert store.complete_approval_effects(record, token)[0]


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
        claimant="claude-code:hob", skills=["research"], lease_expires_at=LATER
    )
    assert claimed is not None
    assert claimed.task_id == old.task_id
    assert claimed.status == "claimed"
    assert claimed.claimant == "claude-code:hob"
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
        threading.Thread(target=contend, args=("claude-code:other",)),
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
            title="Read the background", assignee="hob", delegated_by="sender", route="inbox"
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


def test_a_run_registry_written_before_credentials_existed_still_opens(tmp_path: Path) -> None:
    """``open_runs`` gained two columns (#41): an existing registry keeps its rows.

    A live steward has a ``steward.db`` full of real runs, so the migration has to be an
    ``ALTER TABLE`` and the index over the new column has to be created *after* it —
    which is exactly what breaks if the index is put in the base schema by mistake. The
    old row reads back with an empty digest, and the credential lookup must not match it.
    """
    path = tmp_path / "legacy.db"
    legacy = sqlite3.connect(path)
    with legacy:
        legacy.execute(
            "CREATE TABLE open_runs (run_id TEXT PRIMARY KEY, kind TEXT NOT NULL "
            "DEFAULT 'routine', agent_id TEXT NOT NULL, project TEXT NOT NULL DEFAULT '', "
            "ref TEXT NOT NULL DEFAULT '', timeout_s REAL NOT NULL DEFAULT 0.0, "
            "started_at TEXT NOT NULL, closed_at TEXT)"
        )
        legacy.execute(
            "INSERT INTO open_runs (run_id, kind, agent_id, started_at) VALUES (?, ?, ?, ?)",
            ("old-run", "routine", "claude-code:hob", EARLY),
        )
    legacy.close()

    with Store(path) as migrated:
        (run,) = migrated.open_runs()
        assert run.run_id == "old-run"
        assert run.resident_id == ""
        assert migrated.session_principal("", fresh_since=EARLY) is None
        credential = credentialed(migrated, run_id="new-run")
        principal = migrated.session_principal(credential, fresh_since=EARLY)
        assert principal is not None
        assert principal.run_id == "new-run"


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
        assert (entry.kind, entry.cost_usd, entry.origin, entry.trigger) == (
            "delegated",
            2.5,
            "",
            "",
        )
        migrated.delegate_job(
            title="the chain the old row came off",
            assignee="hob",
            delegated_by="sender",
            route="inbox",
            origin="task:root",
            task_id="old-task",
        )

        (rolled,) = migrated.spend_by_origin()

        assert (rolled.origin, rolled.cost_usd) == ("task:root", pytest.approx(2.5))


def test_a_registry_written_before_trigger_existed_keeps_the_run_unknown(tmp_path: Path) -> None:
    path = tmp_path / "legacy-registry.db"
    legacy = sqlite3.connect(path)
    with legacy:
        legacy.execute(
            "CREATE TABLE open_runs (run_id TEXT PRIMARY KEY, kind TEXT NOT NULL, "
            "agent_id TEXT NOT NULL, project TEXT NOT NULL DEFAULT '', "
            "ref TEXT NOT NULL DEFAULT '', timeout_s REAL NOT NULL DEFAULT 0.0, "
            "started_at TEXT NOT NULL, closed_at TEXT)"
        )
        legacy.execute(
            "INSERT INTO open_runs (run_id, kind, agent_id, started_at) VALUES (?, ?, ?, ?)",
            ("legacy", "routine", "a:hob", EARLY),
        )
    legacy.close()

    with Store(path) as migrated:
        (run,) = migrated.open_runs()
        assert run.trigger == ""


@pytest.mark.parametrize(
    ("kind", "trigger"),
    [
        ("routine", TRIGGER_SCHEDULE),
        ("routine", TRIGGER_MANUAL),
        ("task", ""),
        ("delegated", ""),
    ],
)
@pytest.mark.parametrize("write", ["record_run", "open_run"])
def test_every_valid_kind_trigger_pair_can_be_written(
    store: Store, kind: str, trigger: str, write: str
) -> None:
    if write == "record_run":
        store.record_run(
            resident="hob", agent_id="a:hob", kind=kind, run_id="valid", trigger=trigger
        )
        assert [(row.kind, row.trigger) for row in store.ledger()] == [(kind, trigger)]
    else:
        assert store.open_run(run_id="valid", kind=kind, agent_id="a:hob", trigger=trigger)
        assert [(row.kind, row.trigger) for row in store.open_runs()] == [(kind, trigger)]


@pytest.mark.parametrize(
    ("kind", "trigger"),
    [
        ("routine", ""),
        ("routine", "button-ish"),
        ("task", TRIGGER_SCHEDULE),
        ("task", TRIGGER_MANUAL),
        ("delegated", TRIGGER_SCHEDULE),
        ("delegated", TRIGGER_MANUAL),
        ("unknown", ""),
    ],
)
@pytest.mark.parametrize("write", ["record_run", "open_run"])
def test_invalid_kind_trigger_pairs_are_rejected_before_writing(
    store: Store, kind: str, trigger: str, write: str
) -> None:
    kwargs = {"agent_id": "a:hob", "kind": kind, "run_id": "bad", "trigger": trigger}
    if write == "record_run":
        kwargs["resident"] = "hob"
    with pytest.raises(ValueError, match=r"invalid (run kind|trigger)"):
        getattr(store, write)(**kwargs)
    assert store.ledger() == []
    assert store.open_runs() == []


def test_scheduler_exports_the_shared_trigger_vocabulary() -> None:
    assert scheduler.TRIGGER_SCHEDULE is TRIGGER_SCHEDULE
    assert scheduler.TRIGGER_MANUAL is TRIGGER_MANUAL


# ------------------------------------------------------------------------ approvals


def test_an_approval_request_starts_pending(store: Store) -> None:
    record = store.create_approval_request(
        agent_id="claude-code:hob",
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
        agent_id="claude-code:hob",
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


def test_a_decision_is_spent_by_one_write_and_no_other(store: Store) -> None:
    """One approval is one edit (warren#437), and the claim is what makes that true."""
    record = store.create_approval_request(
        agent_id="claude-code:karen", project="karen", action="grant_skill", message="…"
    )
    store.decide(record.request_id, "approve", decided_by="miha")

    assert store.consume_approval(record.request_id, by="write-1") is True
    assert store.consume_approval(record.request_id, by="write-2") is False

    spent = _approval(store, record.request_id)
    assert spent.consumed_at
    assert spent.consumed_by == "write-1"
    assert spent.to_dict()["consumed_by"] == "write-1"


def test_only_the_write_that_claimed_a_decision_can_give_it_back(store: Store) -> None:
    """A refused write releases its own claim; a stale caller may not re-open somebody's."""
    record = store.create_approval_request(
        agent_id="claude-code:karen", project="karen", action="grant_skill", message="…"
    )
    assert store.consume_approval(record.request_id, by="write-1") is True

    assert store.release_approval(record.request_id, by="write-2") is False
    assert _approval(store, record.request_id).consumed_at, "somebody else's claim stands"

    assert store.release_approval(record.request_id, by="write-1") is True
    released = _approval(store, record.request_id)
    assert released.consumed_at is None
    assert released.consumed_by == ""
    assert store.consume_approval(record.request_id, by="write-3") is True


def test_releasing_a_decision_nobody_claimed_changes_nothing(store: Store) -> None:
    record = store.create_approval_request(
        agent_id="a:b", project="p", action="grant_skill", message="…"
    )

    assert store.release_approval(record.request_id, by="") is False
    assert _approval(store, record.request_id).consumed_at is None


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
        agent_id="a:b", project="p", action="send_email", message="…", resident="hob"
    )
    assert store.undelivered_decisions("hob") == [], "pending decides nothing yet"
    store.decide(record.request_id, "approve")

    waiting = store.undelivered_decisions("hob")
    assert [r.request_id for r in waiting] == [record.request_id]
    assert store.mark_delivered([record.request_id]) == 1
    assert store.undelivered_decisions("hob") == []
    assert store.mark_delivered([record.request_id]) == 0, "delivering twice marks nothing"
    assert _approval(store, record.request_id).delivered_at


def test_claiming_decisions_marks_them_delivered_in_one_pass(store: Store) -> None:
    record = store.create_approval_request(
        agent_id="a:b", project="p", action="send_email", message="…", resident="hob"
    )
    store.decide(record.request_id, "approve")
    claimed = store.claim_undelivered_decisions("hob")
    assert [r.request_id for r in claimed] == [record.request_id]
    # The read and the mark were one pass: a second wake-up finds nothing.
    assert store.claim_undelivered_decisions("hob") == []
    assert _approval(store, record.request_id).delivered_at


def test_two_concurrent_wake_ups_claim_a_decision_exactly_once(tmp_path: Path) -> None:
    """The read-then-mark is atomic, so a decision reaches exactly one of two racing sessions."""
    with Store(tmp_path / "steward.db") as store:
        record = store.create_approval_request(
            agent_id="a:b", project="p", action="send_email", message="…", resident="hob"
        )
        store.decide(record.request_id, "approve")

        barrier = threading.Barrier(2)
        results: list[list[ApprovalRecord]] = []
        lock = threading.Lock()

        def grab() -> None:
            barrier.wait()
            claimed = store.claim_undelivered_decisions("hob")
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
        agent_id="a:b", project="p", action="spend", message="…", resident="hob"
    )
    store.decide(mine.request_id, "deny")
    assert store.undelivered_decisions("other-resident") == []


def test_the_audit_view_holds_the_request_and_its_decision(store: Store) -> None:
    record = store.create_approval_request(
        agent_id="a:b", project="p", action="send_email", message="…", resident="hob"
    )
    store.decide(record.request_id, "edit", decided_by="api", edit={"subject": "shorter"})
    audited = store.approvals()
    assert len(audited) == 1
    assert audited[0].to_dict()["decision"] == "edit"
    assert audited[0].to_dict()["resident"] == "hob"
    assert store.approvals("pending") == []
    assert [r.request_id for r in store.approvals("resolved")] == [record.request_id]


def _ask(store: Store, action: str = "send_email", resident: str = "hob") -> ApprovalRecord:
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
        resident="hob",
        expires_at=EARLY,
    )
    store.expire_approvals(LATER)
    assert store.recent_denials("hob", "send_email", EARLY) == 2


def test_recent_denials_ignores_everything_that_is_not_this_residents_no(store: Store) -> None:
    store.decide(_ask(store).request_id, "approve", decided_by="api", now=LATER)
    store.decide(_ask(store, action="spend").request_id, "deny", decided_by="api", now=LATER)
    store.decide(_ask(store, resident="other-resident").request_id, "deny", now=LATER)
    _ask(store)  # Still pending: nobody has answered it either way.
    assert store.recent_denials("hob", "send_email", EARLY) == 0


def test_recent_denials_starts_counting_at_since(store: Store) -> None:
    store.decide(_ask(store).request_id, "deny", decided_by="api", now=EARLY)
    assert store.recent_denials("hob", "send_email", EARLY) == 1
    assert store.recent_denials("hob", "send_email", LATER) == 0


def test_an_auto_denied_request_is_filed_resolved_and_not_counted_as_a_no(store: Store) -> None:
    """Steward's own repeat deny is on the record, but it must not renew its own window."""
    record = store.create_approval_request(
        agent_id="a:b",
        project="p",
        action="send_email",
        message="…",
        resident="hob",
        denied_by="repeat",
    )
    assert not record.pending
    assert record.decision == "deny"
    assert record.decided_at == record.created_at
    assert store.pending_approvals() == []
    assert store.recent_denials("hob", "send_email", EARLY) == 0
    # It is a decision like any other, so the resident is told about it on its next run.
    assert [r.request_id for r in store.undelivered_decisions("hob")] == [record.request_id]


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
            agent_id="a:b", project="p", action="spend", message="…", resident="hob"
        ).request_id
        first.decide(request_id, "deny")
    with Store(path) as second:
        assert [r.request_id for r in second.undelivered_decisions("hob")] == [request_id]
        second.mark_delivered([request_id])
    with Store(path) as third:
        assert third.undelivered_decisions("hob") == []


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
    assert [record.request_id for record in store.export_request_history()] == ["r1"]


def test_updating_an_unknown_request_is_ignored(store: Store) -> None:
    store.set_request_outcome("never-logged", "ran")
    assert store.export_request_history() == []


def test_outcome_can_be_updated_without_touching_the_detail(store: Store) -> None:
    store.log_request(request_id="r2", method="POST", path="/jobs", outcome="queued")
    store.set_request_outcome("r2", "failed")
    assert _request(store, "r2").outcome == "failed"


def test_the_database_lives_beside_the_scheduler_state(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("STEWARD_STATE", str(tmp_path / "state" / "scheduler.json"))
    assert default_db_path() == tmp_path / "state" / "steward.db"


def test_the_store_waits_longer_than_sqlites_default(store: Store) -> None:
    assert store._conn.execute("PRAGMA busy_timeout").fetchone()[0] == 15_000


def test_health_failures_are_independent_durable_counted_and_corruption_tolerant(
    tmp_path: Path,
) -> None:
    path = tmp_path / "steward.db"
    with Store(path) as store:
        store.health.record(
            kind="ledger_write", resident="hob", run_id="run-1", error="locked", now=EARLY
        )
        store.health.record(
            kind="pause_enforcement",
            resident="pip",
            run_id="run-2",
            error="disk full",
            now=LATER,
        )
    with (tmp_path / "steward.db.health.jsonl").open("ab") as journal:
        journal.write(b"not json\n")
    with Store(path) as reopened:
        failure = reopened.health.latest()
    assert failure is not None
    assert failure.count == 2
    assert failure.kind == "pause_enforcement"
    assert failure.resident == "pip"
    assert failure.run_id == "run-2"
    assert failure.error == "disk full"
    assert failure.failed_at == LATER


def test_first_health_failure_atomically_creates_and_syncs_the_journal_directory(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    journal = health.HealthJournal(tmp_path / "steward.db")
    assert journal.path is not None
    calls: list[tuple[str, object]] = []
    real_fsync = health.os.fsync
    real_replace = health.os.replace

    def fsync(fd: int) -> None:
        calls.append(("fsync", "directory" if health.os.fstat(fd).st_mode & 0o040000 else "file"))
        real_fsync(fd)

    def replace(source: str, destination: Path) -> None:
        calls.append(("replace", destination))
        real_replace(source, destination)

    monkeypatch.setattr(health.os, "fsync", fsync)
    monkeypatch.setattr(health.os, "replace", replace)

    journal.record(kind="ledger_write", resident="hob", run_id="first", error="locked", now=EARLY)

    assert calls == [
        ("fsync", "file"),
        ("replace", journal.path),
        ("fsync", "directory"),
    ]
    assert journal.path.read_text(encoding="utf-8") == (
        '{"version":1,"count":1,"kind":"ledger_write","resident":"hob",'
        '"run_id":"first","error":"locked",'
        f'"failed_at":"{EARLY}"}}\n'
    )


@pytest.mark.parametrize("failed_operation", ["write", "fsync", "replace"])
def test_failed_health_compaction_keeps_the_previous_evidence(
    failed_operation: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    path = tmp_path / "steward.db"
    journal = health.HealthJournal(path)
    journal.record(kind="ledger_write", resident="hob", run_id="old", error="locked", now=EARLY)
    assert journal.path is not None
    old_evidence = journal.path.read_bytes()
    monkeypatch.setattr(health, "COMPACT_AT_BYTES", len(old_evidence))

    def fail(*_args: object, **_kwargs: object) -> None:
        raise OSError(f"failed {failed_operation}")

    monkeypatch.setattr(health.os, failed_operation, fail)

    with pytest.raises(OSError, match=f"failed {failed_operation}"):
        journal.record(
            kind="pause_enforcement", resident="pip", run_id="new", error="disk full", now=LATER
        )

    assert journal.path.read_bytes() == old_evidence
    latest = journal.latest()
    assert latest is not None
    assert latest.run_id == "old"
    assert list(tmp_path.glob(".steward.db.health.jsonl.*.tmp")) == []


def test_health_append_atomically_repairs_a_torn_suffix(tmp_path: Path) -> None:
    path = tmp_path / "steward.db"
    journal = health.HealthJournal(path)
    journal.record(kind="ledger_write", resident="hob", run_id="one", error="locked", now=EARLY)
    journal.record(kind="ledger_write", resident="hob", run_id="two", error="locked", now=LATER)
    assert journal.path is not None
    with journal.path.open("ab") as stream:
        stream.write(b'{"version":1,"count":3,"kind":"torn"')

    journal.record(
        kind="pause_enforcement", resident="pip", run_id="three", error="disk full", now=LATER
    )

    lines = journal.path.read_bytes().splitlines()
    assert len(lines) == 3
    assert all(line.endswith(b"}") for line in lines)
    latest = journal.latest()
    assert latest is not None
    assert latest.count == 3
    assert latest.run_id == "three"


def test_health_reader_adopts_a_journal_created_before_the_lock_sidecar(tmp_path: Path) -> None:
    journal = health.HealthJournal(tmp_path / "steward.db")
    assert journal.path is not None
    journal.path.write_text(
        '{"version":1,"count":7,"kind":"ledger_write","resident":"hob",'
        '"run_id":"legacy","error":"locked","failed_at":"2026-08-24T09:00:00.000Z"}\n',
        encoding="utf-8",
    )

    latest = journal.latest()

    assert latest is not None
    assert latest.count == 7
    assert latest.run_id == "legacy"
    assert journal.path.with_name(f"{journal.path.name}.lock").exists()


def test_health_writers_keep_counting_across_atomic_replacements(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    journal = health.HealthJournal(tmp_path / "steward.db")
    monkeypatch.setattr(health, "COMPACT_AT_BYTES", 1)
    writers = [
        threading.Thread(
            target=journal.record,
            kwargs={
                "kind": "ledger_write",
                "resident": "hob",
                "run_id": f"run-{index}",
                "error": "locked",
                "now": EARLY,
            },
        )
        for index in range(20)
    ]

    for writer in writers:
        writer.start()
    for writer in writers:
        writer.join()

    latest = journal.latest()
    assert latest is not None
    assert latest.count == len(writers)


# -------------------------------------------------------------------------- the inbox


def test_the_inbox_can_be_counted_without_being_read(store: Store) -> None:
    """``doctor`` prints one number, so it asks for one number."""
    store.delegate_job(
        title="Read the background", assignee="hob", delegated_by="sender", route="inbox"
    )
    store.delegate_job(title="And this", assignee="hob", delegated_by="sender", route="inbox")
    store.delegate_job(title="Not yours", assignee="pip", delegated_by="sender", route="inbox")

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
        trigger=TRIGGER_SCHEDULE,
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
        title="the real chain",
        assignee="hob",
        delegated_by="sender",
        route="inbox",
        origin="task:x",
    )
    store.record_run(
        resident="hob",
        agent_id="a:hob",
        kind="routine",
        trigger=TRIGGER_SCHEDULE,
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
        delegated_by="sender",
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
        trigger=TRIGGER_SCHEDULE,
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
    assert store.open_run(
        run_id="r1", kind="routine", trigger=TRIGGER_SCHEDULE, agent_id="a:hob", now=EARLY
    )
    assert not store.open_run(
        run_id="r1", kind="routine", trigger=TRIGGER_SCHEDULE, agent_id="a:hob", now=LATER
    )
    assert [run.started_at for run in store.open_runs()] == [EARLY]


def test_a_run_is_closed_once_however_late_its_session_reports(store: Store) -> None:
    """A run the watchdog already buried is not re-answered by a session that turned up."""
    store.open_run(
        run_id="r1", kind="routine", trigger=TRIGGER_SCHEDULE, agent_id="a:hob", now=EARLY
    )

    assert store.close_run("r1", now=EARLY)
    assert not store.close_run("r1", now=LATER)


def test_closing_a_run_nobody_opened_changes_nothing(store: Store) -> None:
    """A close with no row is a no-op, not a row invented to close."""
    assert not store.close_run("never-started")
    assert store.open_runs() == []


# ------------------------------------------- scoped per-session credentials (steward #41)


def credentialed(store: Store, *, run_id: str = "r1", now: str = LATER) -> str:
    """Open a live run holding one freshly minted credential, and return the plaintext."""
    credential = new_session_credential()
    assert store.open_run(
        run_id=run_id,
        kind="routine",
        trigger=TRIGGER_SCHEDULE,
        agent_id="claude-code:hob",
        project="household",
        ref="daily-summary",
        resident_id="hob",
        session_credential=credential,
        now=now,
    )
    return credential


def test_a_credential_resolves_to_the_resident_whose_run_it_is(store: Store) -> None:
    """The credential *is* the identity: the resident is read from the row, not a body."""
    credential = credentialed(store)

    principal = store.session_principal(credential, fresh_since=EARLY)

    assert principal is not None
    assert principal.resident_id == "hob"
    assert principal.run_id == "r1"


def test_only_the_digest_is_written(store: Store) -> None:
    """A copy of steward.db must not yield live credentials."""
    credential = credentialed(store)

    (row,) = store._conn.execute("SELECT * FROM open_runs").fetchall()

    assert credential not in set(dict(row).values())
    assert row["session_credential_sha256"] == credential_digest(credential)


def test_a_closed_run_authenticates_nothing(store: Store) -> None:
    credential = credentialed(store)
    assert store.close_run("r1")

    assert store.session_principal(credential, fresh_since=EARLY) is None


def test_a_chosen_terminal_fact_ends_the_credential_before_the_close(store: Store) -> None:
    """A run whose end is decided is over, whether or not the event has been published."""
    credential = credentialed(store)
    assert store.claim_run_terminal("r1", event="{}", event_id="t:r1", owner_token="")

    assert store.open_runs(), "the row is still open — publication has not happened yet"
    assert store.session_principal(credential, fresh_since=EARLY) is None


def test_a_stale_lease_ends_the_credential_before_the_watchdog_sweeps(store: Store) -> None:
    """No second clock: the bound is the moment the watchdog *could* bury the run.

    Deliberately not the condition ``renew_run`` renews under — that has no freshness
    clause at all, so a live owner whose heartbeat thread was starved may still renew and
    carry on. Burial is the clock that answers "is anybody still entitled to act as this
    session", and it denies by default.
    """
    credential = credentialed(store, now=EARLY)

    assert store.session_principal(credential, fresh_since=LATER) is None
    assert store.open_runs(), "and nobody has swept the row yet"


def test_a_credential_nobody_minted_is_nobody(store: Store) -> None:
    credentialed(store)

    assert store.session_principal(new_session_credential(), fresh_since=EARLY) is None


def test_no_credential_is_not_a_master_key(store: Store) -> None:
    """Every run opened without one stores the empty digest; it must never match."""
    assert store.open_run(
        run_id="r1", kind="routine", trigger=TRIGGER_SCHEDULE, agent_id="a:hob", now=LATER
    )

    assert store.session_principal("", fresh_since=EARLY) is None


def test_a_task_attempt_credential_dies_with_the_binding_that_lost_the_race(
    store: Store,
) -> None:
    """An inserted run is rolled back when the claim stamp no longer matches.

    Its credential has to go with it: one that outlived a run nobody is watching would
    authenticate a session steward never started.
    """
    credential = new_session_credential()

    assert not store.open_task_run(
        task_id="no-such-task",
        lease=EARLY,
        run_id="r1",
        kind="task",
        agent_id="claude-code:hob",
        owner_token="owner",
        resident_id="hob",
        session_credential=credential,
    )

    assert store.open_runs() == []
    assert store.session_principal(credential, fresh_since=EARLY) is None


# ------------------------------------------------ named operator credentials (warren#225)


def test_an_operator_credential_resolves_to_the_person_it_was_minted_for(store: Store) -> None:
    """The credential *is* the identity: the name is read from the row, not from a header."""
    credential = new_operator_credential()
    store.mint_operator(name="Miha", email="miha@example.invalid", credential=credential)

    principal = store.operator_principal(credential)

    assert principal is not None
    assert principal.name == "Miha"
    assert principal.email == "miha@example.invalid"
    assert principal.identity == ("Miha", "miha@example.invalid")


def test_only_the_digest_of_an_operator_credential_is_written(store: Store) -> None:
    """A copy of steward.db must not yield live credentials."""
    credential = new_operator_credential()
    store.mint_operator(name="Miha", email="m@x.invalid", credential=credential)

    (row,) = store._conn.execute("SELECT * FROM operator_credentials").fetchall()

    assert credential not in set(dict(row).values())
    assert row["digest"] == credential_digest(credential)


def test_a_revoked_credential_resolves_to_nobody(store: Store) -> None:
    """Revocation is the whole difference from the master token, so it has to be total."""
    credential = new_operator_credential()
    store.mint_operator(name="Miha", email="m@x.invalid", credential=credential, now=EARLY)

    revoked = store.revoke_operator("Miha", now=LATER)

    assert revoked is not None
    assert revoked.revoked_at == LATER
    assert store.operator_principal(credential) is None


def test_revoking_twice_does_not_move_the_moment_it_first_stopped(store: Store) -> None:
    """The answer is about *this* call, and the audit keeps the first stamp."""
    store.mint_operator(name="Miha", email="m@x.invalid", credential=new_operator_credential())
    store.revoke_operator("Miha", now=EARLY)

    assert store.revoke_operator("Miha", now=LATER) is None
    assert store.operators()[0].revoked_at == EARLY


def test_a_revoked_row_is_kept_rather_than_deleted(store: Store) -> None:
    """Who could act as this fleet's operator, and until when, is what an audit asks."""
    store.mint_operator(name="Miha", email="m@x.invalid", credential=new_operator_credential())
    store.revoke_operator("Miha", now=LATER)

    listed = store.operators()

    assert [(record.name, record.live) for record in listed] == [("Miha", False)]
    assert store.operators(live_only=True) == []


def test_a_name_that_already_holds_a_live_credential_is_refused(store: Store) -> None:
    """Re-minting in place would leave the old holder with no way to tell it had stopped."""
    store.mint_operator(name="Miha", email="m@x.invalid", credential=new_operator_credential())

    with pytest.raises(ValueError, match="already holds a live credential"):
        store.mint_operator(name="Miha", email="m@x.invalid", credential=new_operator_credential())


def test_a_revoked_name_may_be_minted_again(store: Store) -> None:
    """Revoke-then-mint is the rotation, and it has to actually work."""
    first = new_operator_credential()
    store.mint_operator(name="Miha", email="m@x.invalid", credential=first)
    store.revoke_operator("Miha")

    second = new_operator_credential()
    store.mint_operator(name="Miha", email="m@x.invalid", credential=second)

    assert store.operator_principal(first) is None
    assert store.operator_principal(second) is not None


def test_an_empty_credential_is_not_a_master_key(store: Store) -> None:
    """No row can hold the empty digest, and a query that would match one is not written."""
    store.mint_operator(name="Miha", email="m@x.invalid", credential=new_operator_credential())

    assert store.operator_principal("") is None


def test_a_credential_nobody_minted_resolves_to_nobody(store: Store) -> None:
    """Shaped like one is not the same as being one."""
    assert store.operator_principal(new_operator_credential()) is None


# ------------------------------------------------------ the last run of a routine (#104)


def record_routine_run(  # noqa: PLR0913 — one keyword per fact about the run
    store: Store,
    *,
    resident: str = "hob",
    routine: str = "daily-summary",
    run_id: str = "r1",
    trigger: str = TRIGGER_SCHEDULE,
    outcome: str = "ok",
    now: str = LATER,
) -> None:
    """Write the ledger row a finished routine session leaves behind."""
    store.record_run(
        resident=resident,
        agent_id=f"claude-code:{resident}",
        kind="routine",
        trigger=trigger,
        run_id=run_id,
        ref=routine,
        outcome=outcome,
        now=now,
    )


def test_the_latest_run_of_each_routine_is_keyed_the_way_the_scheduler_keys_it(
    store: Store,
) -> None:
    """``<resident>/<routine>`` is the scheduler's own state key, so the ledger joins on it."""
    record_routine_run(store)

    runs = store.latest_routine_runs()

    assert list(runs) == ["hob/daily-summary"]
    assert runs["hob/daily-summary"].trigger == TRIGGER_SCHEDULE


def test_the_newest_row_wins_per_routine(store: Store) -> None:
    """One answer per routine, and it is the latest — not whichever the scan reached first."""
    record_routine_run(store, run_id="old", outcome="failed", now=EARLY)
    record_routine_run(store, run_id="new", outcome="ok", now=LATER)

    runs = store.latest_routine_runs()

    assert runs["hob/daily-summary"].run_id == "new"
    assert runs["hob/daily-summary"].outcome == "ok"


def test_two_residents_running_the_same_routine_name_do_not_collide(store: Store) -> None:
    """Routine ids are unique within a resident, not across the fleet."""
    record_routine_run(store, resident="hob", run_id="h1")
    record_routine_run(store, resident="pip", run_id="p1")

    runs = store.latest_routine_runs()

    assert {key: entry.run_id for key, entry in runs.items()} == {
        "hob/daily-summary": "h1",
        "pip/daily-summary": "p1",
    }


def test_a_run_that_is_not_a_routine_is_not_a_routines_last_run(store: Store) -> None:
    """A claimed board task is a run, and it is not this routine firing."""
    store.record_run(
        resident="hob",
        agent_id="claude-code:hob",
        kind="task",
        run_id="t1",
        ref="task-42",
        outcome="ok",
    )

    assert store.latest_routine_runs() == {}


# ---------------------------------------------------------------------------- delivery


def test_a_delivery_is_written_onto_the_run_row_open_or_closed(store: Store) -> None:
    assert store.open_run(run_id="d1", kind="routine", agent_id="a:hob", trigger="schedule")
    opened = store.run_record("d1")
    assert opened is not None
    assert opened.delivery is None
    assert store.close_run("d1")
    assert store.record_delivery("d1", "delivery_failed", "no operators")
    row = store.run_record("d1")
    assert row is not None
    assert (row.delivery, row.delivery_reason) == ("delivery_failed", "no operators")
    assert row.to_dict()["delivery"] == "delivery_failed"
    assert store.record_delivery("d1", "delivered")
    again = store.run_record("d1")
    assert again is not None
    assert again.delivery_reason == ""


def test_a_delivery_needs_a_run_and_a_known_status(store: Store) -> None:
    assert not store.record_delivery("never-opened", "delivered")
    assert store.run_record("never-opened") is None
    with pytest.raises(ValueError, match="invalid delivery status"):
        store.record_delivery("never-opened", "lost")  # ty: ignore[invalid-argument-type]


def test_losing_the_add_column_race_to_a_neighbour_is_nothing(store: Store) -> None:
    """Four daemons boot at once after a deploy; three of them find the column already there."""
    store._add_column("open_runs", "delivery", "TEXT")
    with pytest.raises(sqlite3.OperationalError, match="no such table"):
        store._add_column("no_such_table", "delivery", "TEXT")


def test_recent_requests_are_bounded_newest_first_with_stable_ties(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("steward.store.utc_now_iso", lambda: EARLY)
    for key in ("z", "a", "m"):
        store.log_request(request_id=key, method="POST", path="/jobs", outcome="queued")
    assert [r.request_id for r in store.recent_requests(limit=2)] == ["m", "a"]
    assert [r.request_id for r in store.export_request_history()] == ["z", "a", "m"]
    with pytest.raises(ValueError, match="positive"):
        store.recent_requests(limit=0)


def test_recent_requests_filter_exact_resident_before_limit(store: Store) -> None:
    for key, path, detail in (
        ("created", "/residents", {"resident": "hob"}),
        ("nested", "/residents/hob/routines/inbox/run", {"routine": "hob/inbox"}),
        ("prefix", "/residents/hobbit/pause", {}),
        ("other", "/jobs", {}),
    ):
        store.log_request(request_id=key, method="POST", path=path, outcome="ran", detail=detail)
    assert [r.request_id for r in store.recent_requests(limit=1, resident="hob")] == ["nested"]
    assert [r.request_id for r in store.recent_requests(limit=10, resident="hob")] == [
        "nested",
        "created",
    ]
    assert store.recent_requests(limit=10, resident="missing") == []


def test_latest_routine_requests_select_only_requested_keys_with_stable_ties(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("steward.store.utc_now_iso", lambda: EARLY)
    for key, routine in (("z", "hob/inbox"), ("a", "hob/inbox"), ("x", 7), ("y", "ada/check")):
        store.log_request(
            request_id=key,
            method="POST",
            path="/jobs",
            outcome="queued",
            detail={"routine": routine},
        )
    latest = store.latest_routine_requests(["hob/inbox", "absent", "7"])
    assert {key: row.request_id for key, row in latest.items()} == {"hob/inbox": "a"}
    store.set_request_outcome("a", "ran", {"routine": "ada/check"})
    assert {
        key: row.request_id for key, row in store.latest_routine_requests(["hob/inbox"]).items()
    } == {"hob/inbox": "z"}


def test_large_request_ledger_uses_bounded_index_scans(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Exercise the actual public queries, then EXPLAIN their traced SQL. This is
    # deliberate query-plan instrumentation, not a developer-machine timing budget.
    monkeypatch.setattr("steward.store.utc_now_iso", lambda: EARLY)
    for index in range(20_000):
        resident = "hob" if index % 2 == 0 else "ada"
        store.log_request(
            request_id=str(index),
            method="POST",
            path=f"/residents/{resident}/pause",
            outcome="ran",
            detail={"routine": f"{resident}/daily"},
        )
    # A late insertion with an older timestamp must not displace a newer request.
    monkeypatch.setattr("steward.store.utc_now_iso", lambda: "2020-01-01T00:00:00Z")
    store.log_request(
        request_id="older",
        method="POST",
        path="/residents/hob/pause",
        outcome="ran",
        detail={"routine": "hob/daily"},
    )
    statements: list[str] = []
    store._conn.set_trace_callback(statements.append)
    assert [r.request_id for r in store.recent_requests(limit=2)] == ["19999", "19998"]
    assert [r.request_id for r in store.recent_requests(limit=1, resident="hob")] == ["19998"]
    assert {
        key: row.request_id
        for key, row in store.latest_routine_requests(["hob/daily", "ada/daily"]).items()
    } == {"hob/daily": "19998", "ada/daily": "19999"}
    store._conn.set_trace_callback(None)
    assert len(statements) == 4
    for sql, index in zip(
        statements,
        (
            "requests_received",
            "requests_resident_received",
            "requests_routine_received",
            "requests_routine_received",
        ),
        strict=True,
    ):
        assert "LIMIT " in sql
        plan = " ".join(row[3] for row in store._conn.execute("EXPLAIN QUERY PLAN " + sql))
        assert index in plan
        assert "TEMP B-TREE" not in plan
    assert len(store.export_request_history()) == 20_001
