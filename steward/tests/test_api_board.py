"""API behavior: board."""

from pathlib import Path

import pytest

from steward import events as ev
from steward.input_bounds import (
    DETAIL_MAX_CHARS,
    IDENTIFIER_MAX_CHARS,
    SKILLS_MAX_ITEMS,
    TITLE_MAX_CHARS,
)
from steward.store import Store
from support.api import (
    ApiFactory,
)
from support.api import (
    api as api,  # noqa: PLC0414 — pytest fixture discovery
)

# --------------------------------------------------------------------------------------
# the job board
# --------------------------------------------------------------------------------------


def test_posting_a_job_emits_task_posted(api: ApiFactory) -> None:
    harness = api()
    response = harness.client.post(
        "/jobs",
        json={"title": "Research X", "detail": "the long version", "required_skills": ["research"]},
    )
    assert response.status_code == 202
    body = response.json()
    assert body["status"] == "accepted"

    posted = harness.events("task_posted")
    assert len(posted) == 1
    assert posted[0]["payload"] == {
        "task_id": body["task_id"],
        "title": "Research X",
        "required_skills": ["research"],
        "posted_by": "api",
    }
    assert posted[0]["source"] == "steward"
    assert ev.validate_event(posted[0]) == ()


def test_the_board_lists_what_was_posted_and_survives_a_reopen(
    api: ApiFactory, tmp_path: Path
) -> None:
    db_path = tmp_path / "state" / "steward.db"
    harness = api(db_path=db_path)
    task_id = harness.client.post("/jobs", json={"title": "Outlive a restart"}).json()["task_id"]

    listed = harness.client.get("/jobs").json()["jobs"]
    assert [job["task_id"] for job in listed] == [task_id]
    assert listed[0]["status"] == "open"
    assert listed[0]["claimant"] is None

    with Store(db_path) as reopened:
        assert [job.task_id for job in reopened.jobs()] == [task_id]


def test_a_job_needs_a_title(api: ApiFactory) -> None:
    harness = api()
    assert harness.client.post("/jobs", json={"detail": "no title"}).status_code == 422
    assert harness.store.jobs() == []


@pytest.mark.parametrize(
    ("field", "at_limit", "over_limit"),
    [
        ("title", "x" * TITLE_MAX_CHARS, "x" * (TITLE_MAX_CHARS + 1)),
        ("detail", "x" * DETAIL_MAX_CHARS, "x" * (DETAIL_MAX_CHARS + 1)),
    ],
)
def test_job_text_bounds_are_exact_and_rejections_have_no_effect(
    api: ApiFactory, field: str, at_limit: str, over_limit: str
) -> None:
    refused = api()
    response = refused.client.post("/jobs", json={"title": "work", field: over_limit})
    assert response.status_code == 422
    assert response.json()["detail"][0]["type"] == "string_too_long"
    assert refused.store.jobs() == []
    assert refused.store.export_request_history() == []
    assert refused.events() == []

    accepted = api()
    assert accepted.client.post("/jobs", json={"title": "work", field: at_limit}).status_code == 202
    assert len(accepted.store.jobs()) == 1
    assert len(accepted.events("task_posted")) == 1


def test_required_skill_bounds_are_exact_and_side_effect_free(api: ApiFactory) -> None:
    skills = [f"s{i}" for i in range(SKILLS_MAX_ITEMS - 1)] + ["x" * IDENTIFIER_MAX_CHARS]
    for invalid in (["x" * (IDENTIFIER_MAX_CHARS + 1)], ["x"] * (SKILLS_MAX_ITEMS + 1)):
        refused = api()
        response = refused.client.post("/jobs", json={"title": "work", "required_skills": invalid})
        assert response.status_code == 422
        assert refused.store.jobs() == []
        assert refused.store.export_request_history() == []
        assert refused.events() == []
    accepted = api()
    assert (
        accepted.client.post("/jobs", json={"title": "work", "required_skills": skills}).status_code
        == 202
    )


def test_the_board_can_be_narrowed_to_one_status(api: ApiFactory) -> None:
    harness = api()
    claimed_id = harness.client.post("/jobs", json={"title": "Claimed"}).json()["task_id"]
    harness.client.post("/jobs", json={"title": "Still open"})
    harness.store.claim_next_job(
        claimant="claude-code:test-agent", skills=[], lease_expires_at="2026-08-24T13:00:00.000Z"
    )

    claimed = harness.client.get("/jobs", params={"status": "claimed"}).json()["jobs"]
    assert [job["task_id"] for job in claimed] == [claimed_id]
    assert claimed[0]["claimant"] == "claude-code:test-agent"
    assert claimed[0]["lease_expires_at"] == "2026-08-24T13:00:00.000Z"
    assert [
        job["title"]
        for job in harness.client.get("/jobs", params={"status": "open"}).json()["jobs"]
    ] == ["Still open"]


def test_an_unknown_board_status_is_refused_rather_than_ignored(api: ApiFactory) -> None:
    harness = api()
    response = harness.client.get("/jobs", params={"status": "nearly-done"})
    assert response.status_code == 422
    assert response.json()["detail"]["error"] == "unknown_status"
