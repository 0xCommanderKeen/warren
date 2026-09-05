"""API behavior: delegation."""

import pytest
from fastapi.testclient import TestClient

from conftest import (
    ResidentWriter,
    valid_manifest,
)
from steward.input_bounds import (
    DETAIL_MAX_CHARS,
    IDENTIFIER_MAX_CHARS,
    TITLE_MAX_CHARS,
)
from support.api import (
    FORBIDDEN,
    HANDOFF,
    RECEIVER_SOUL,
    ApiFactory,
    receiver_manifest,
    with_receiver,
)
from support.api import (
    api as api,  # noqa: PLC0414 — pytest fixture discovery
)


def test_delegating_over_http_delivers_and_announces(
    api: ApiFactory, write_resident: ResidentWriter
) -> None:
    harness = with_receiver(api, write_resident)
    response = harness.client.post("/delegate", json={**HANDOFF, "from": "test-agent"})

    assert response.status_code == 202
    body = response.json()
    assert body["status"] == "accepted"
    assert body["to"] == "receiver-agent"
    assert body["depth"] == 1
    assert not FORBIDDEN.search(body["message"]), body["message"]

    (delivered,) = harness.store.inbox("receiver-agent")
    assert delivered.task_id == body["task_id"]
    assert delivered.delegated_by == "test-agent"

    (event,) = harness.events("task_delegated")
    assert event["agent_id"] == "claude-code:test-agent"
    assert event["payload"]["to"] == "claude-code:receiver-agent"
    assert harness.store.request(body["request_id"]) is not None


def test_a_person_may_delegate_without_being_a_resident(
    api: ApiFactory, write_resident: ResidentWriter
) -> None:
    """No ``from``: the token is the permission, and the receiver's route still rules."""
    harness = with_receiver(api, write_resident)
    response = harness.client.post("/delegate", json=HANDOFF)
    assert response.status_code == 202
    (delivered,) = harness.store.inbox("receiver-agent")
    assert delivered.delegated_by == "api"
    assert delivered.origin == "human:api"


def test_a_sender_whose_manifest_forbids_it_is_refused(
    api: ApiFactory, write_resident: ResidentWriter
) -> None:
    harness = api(manifest=valid_manifest())
    write_resident(receiver_manifest(), soul=RECEIVER_SOUL, root=harness.residents_dir)

    response = harness.client.post("/delegate", json={**HANDOFF, "from": "test-agent"})
    assert response.status_code == 409
    assert response.json()["detail"]["error"] == "not_permitted"
    assert harness.store.jobs() == []
    assert harness.events() == [], "a refusal emits nothing"


def test_an_unknown_recipient_is_404(api: ApiFactory, write_resident: ResidentWriter) -> None:
    harness = with_receiver(api, write_resident)
    response = harness.client.post(
        "/delegate", json={**HANDOFF, "to": "nobody", "from": "test-agent"}
    )
    assert response.status_code == 404
    assert response.json()["detail"]["error"] == "unknown_recipient"


def test_a_route_that_is_not_open_yet_is_refused(
    api: ApiFactory, write_resident: ResidentWriter
) -> None:
    harness = with_receiver(api, write_resident, status="pending")
    response = harness.client.post("/delegate", json={**HANDOFF, "from": "test-agent"})
    assert response.status_code == 409
    assert response.json()["detail"]["error"] == "route_inactive"


def test_delegating_from_an_unknown_resident_is_404(
    api: ApiFactory, write_resident: ResidentWriter
) -> None:
    harness = with_receiver(api, write_resident)
    response = harness.client.post("/delegate", json={**HANDOFF, "from": "ghost"})
    assert response.status_code == 404
    assert response.json()["detail"]["error"] == "unknown_resident"


def test_a_handoff_needs_a_title(api: ApiFactory, write_resident: ResidentWriter) -> None:
    harness = with_receiver(api, write_resident)
    assert harness.client.post("/delegate", json={**HANDOFF, "title": ""}).status_code == 422


@pytest.mark.parametrize(
    ("field", "limit"),
    [
        ("title", TITLE_MAX_CHARS),
        ("detail", DETAIL_MAX_CHARS),
        ("to", IDENTIFIER_MAX_CHARS),
        ("route", IDENTIFIER_MAX_CHARS),
        ("from", IDENTIFIER_MAX_CHARS),
        ("parent_task_id", IDENTIFIER_MAX_CHARS),
    ],
)
def test_handoff_fields_have_exact_422_bounds(
    api: ApiFactory, write_resident: ResidentWriter, field: str, limit: int
) -> None:
    refused = with_receiver(api, write_resident)
    response = refused.client.post("/delegate", json={**HANDOFF, field: "x" * (limit + 1)})
    assert response.status_code == 422
    assert refused.store.jobs() == []
    assert refused.store.export_request_history() == []
    assert refused.events() == []

    at_limit = with_receiver(api, write_resident)
    accepted_by_validation = at_limit.client.post("/delegate", json={**HANDOFF, field: "x" * limit})
    assert accepted_by_validation.status_code != 422


def test_the_inbox_lists_what_is_waiting(api: ApiFactory, write_resident: ResidentWriter) -> None:
    harness = with_receiver(api, write_resident)
    harness.client.post("/delegate", json={**HANDOFF, "from": "test-agent"})

    body = harness.client.get("/residents/receiver-agent/inbox").json()
    assert body["status"] == "open"
    assert body["routes"] == [{"id": "inbox", "status": "active", "accepts": True}]
    assert body["pending"] == 1
    assert [item["title"] for item in body["inbox"]] == ["Read the background"]
    assert body["inbox"][0]["depth"] == 1

    everything = harness.client.get("/residents/receiver-agent/inbox?status=all").json()
    assert len(everything["inbox"]) == 1
    # `pending` is the open count whatever was asked for, so the audit view still says
    # how much post is actually waiting.
    assert everything["pending"] == 1
    assert harness.client.get("/residents/test-agent/inbox").json()["inbox"] == []


def test_the_inbox_names_a_route_somebody_shut(
    api: ApiFactory, write_resident: ResidentWriter
) -> None:
    """The console cannot show letters piling up behind a closed door it cannot see (#46)."""
    harness = with_receiver(api, write_resident, status="disabled")
    harness.store.delegate_job(
        title="Read the background",
        assignee="receiver-agent",
        delegated_by="test-agent",
        route="inbox",
    )

    body = harness.client.get("/residents/receiver-agent/inbox").json()

    assert body["routes"] == [{"id": "inbox", "status": "disabled", "accepts": False}]
    assert body["pending"] == 1


def test_an_unknown_inbox_status_is_refused_rather_than_ignored(api: ApiFactory) -> None:
    harness = api()
    response = harness.client.get("/residents/test-agent/inbox?status=whenever")
    assert response.status_code == 422
    assert response.json()["detail"]["error"] == "unknown_status"


def test_the_inbox_of_an_unknown_resident_is_404(api: ApiFactory) -> None:
    assert api().client.get("/residents/ghost/inbox").status_code == 404


def test_lineage_answers_where_a_task_came_from(
    api: ApiFactory, write_resident: ResidentWriter
) -> None:
    harness = with_receiver(api, write_resident)
    root = harness.client.post("/jobs", json={"title": "The root task"}).json()["task_id"]
    child = harness.client.post(
        "/delegate", json={**HANDOFF, "from": "test-agent", "parent_task_id": root}
    ).json()["task_id"]

    body = harness.client.get(f"/tasks/{child}/lineage").json()
    assert [item["task_id"] for item in body["chain"]] == [root, child]
    assert body["origin"] == f"task:{root}"
    assert body["depth"] == 1
    assert harness.client.get("/tasks/nobody/lineage").status_code == 404

    from_root = harness.client.get(f"/tasks/{root}/lineage").json()
    assert [item["task_id"] for item in from_root["chain"]] == [root, child], (
        "the root is the id POST /delegate hands back; it must see its descendants (#202)"
    )
    assert from_root["depth"] == 0, "origin and depth still describe the task asked about"


def test_delegation_needs_the_token_like_everything_else(api: ApiFactory) -> None:
    harness = api()
    anonymous = TestClient(harness.client.app)
    assert anonymous.post("/delegate", json=HANDOFF).status_code == 401
    assert anonymous.get("/residents/test-agent/inbox").status_code == 401
    # Refused before the task is looked up, so an unknown id is 401 and not 404. Arcadia's
    # deploy smoke script reads exactly that to tell "the origin proxies /tasks" apart from
    # "the origin fell through to the SPA and served index.html" (warren#242).
    assert anonymous.get("/tasks/no-such-task/lineage").status_code == 401
    assert harness.store.jobs() == []
