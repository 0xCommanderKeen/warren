"""The store is what makes the API's answers survive a restart, so it is tested alone."""

from collections.abc import Iterator
from pathlib import Path

import pytest

from steward.store import ApprovalRecord, JobRecord, RequestRecord, Store, default_db_path


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
