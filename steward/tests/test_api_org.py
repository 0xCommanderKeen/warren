"""API behavior: org."""

from fastapi.testclient import TestClient

from conftest import (
    ResidentWriter,
)
from steward.org import NO_OPEN_ROUTE
from support.api import (
    ApiFactory,
    with_receiver,
)
from support.api import (
    api as api,  # noqa: PLC0414 — pytest fixture discovery
)

# --------------------------------------------------------------------------------------
# GET /org — the chart computed from the manifests (warren#441)
# --------------------------------------------------------------------------------------


def test_the_org_chart_draws_the_edge_two_manifests_declare(
    api: ApiFactory, write_resident: ResidentWriter
) -> None:
    harness = with_receiver(api, write_resident)

    body = harness.client.get("/org").json()

    assert [node["id"] for node in body["nodes"]] == ["receiver-agent", "test-agent"]
    assert body["edges"] == [
        {
            "sender": "test-agent",
            "receiver": "receiver-agent",
            "named": True,
            "deliverable": True,
            "reason": None,
        }
    ]


def test_the_org_chart_ranks_the_receiver_under_the_sender(
    api: ApiFactory, write_resident: ResidentWriter
) -> None:
    """What a panel draws rows from, so the two surfaces cannot disagree about who is above."""
    harness = with_receiver(api, write_resident)

    ranks = {node["id"]: node["rank"] for node in harness.client.get("/org").json()["nodes"]}

    assert ranks == {"test-agent": 0, "receiver-agent": 1}


def test_a_shut_door_leaves_the_edge_drawn_and_says_it_will_not_deliver(
    api: ApiFactory, write_resident: ResidentWriter
) -> None:
    harness = with_receiver(api, write_resident, status="pending")

    (edge,) = harness.client.get("/org").json()["edges"]

    assert edge["deliverable"] is False
    assert edge["reason"] == NO_OPEN_ROUTE


def test_the_org_chart_names_the_manifests_it_could_not_read(api: ApiFactory) -> None:
    """A fleet that has gone quiet says why, rather than answering an empty chart."""
    harness = api()
    broken = harness.residents_dir / "broken"
    broken.mkdir()
    (broken / "manifest.yaml").write_text("version: 0\nid: broken\n", encoding="utf-8")

    body = harness.client.get("/org").json()

    assert [node["id"] for node in body["nodes"]] == ["test-agent"]
    assert any("broken" in error for error in body["errors"])


def test_the_org_chart_is_shut_to_an_unauthenticated_reader(api: ApiFactory) -> None:
    """Every door steward has is behind the token, and a chart of the grants most of all."""
    harness = api()

    anonymous = TestClient(harness.client.app)

    assert anonymous.get("/org").status_code == 401
