"""The org chart projection: nodes, delegation edges, and what hangs off each node.

Everything here is a fact two manifests already state. The tests are written against a
fixture fleet rather than the shipped tree on purpose — the shipped residents change with
the household, and a chart test that moved when Hob got a colleague would be a test about
the household rather than about the projection.
"""

from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from conftest import RESIDENTS_DIR, ResidentWriter, valid_manifest
from steward.manifest import validate_path
from steward.org import (
    NO_OPEN_ROUTE,
    NO_SUCH_RESIDENT,
    RECEIVER_RETIRED,
    OrgChart,
    org_chart,
)

UIDS = {
    "boss": "11111111-1111-4111-8111-111111111111",
    "hand": "22222222-2222-4222-8222-222222222222",
    "worker": "33333333-3333-4333-8333-333333333333",
    "loner": "44444444-4444-4444-8444-444444444444",
}

DELEGATION_ROUTE = {
    "id": "inbox",
    "kind": "delegation",
    "address": "steward:board",
    "status": "active",
}

type SpecWriter = Callable[[dict[str, Any]], Path]


def soul_for(resident_id: str) -> str:
    """Render the soul beside a fixture manifest, with the identity validation cross-checks."""
    return (
        f"---\n"
        f"agent_id: claude-code:{resident_id}\n"
        f"name: {resident_id.title()}\n"
        f"char: Monk\n"
        f'accent: "#a68a4f"\n'
        f"role: {resident_id} bot\n"
        f"---\n"
        f"A villager that exists only inside a test.\n\n"
        f"## Voice\n\nFlat, factual, short.\n"
    )


def resident_spec(
    resident_id: str,
    *,
    home: int,
    delegation: dict[str, Any] | None = None,
    accepts: bool = False,
    **overrides: Any,  # noqa: ANN401 — a manifest field is whatever the schema allows
) -> dict[str, Any]:
    """Build one manifest for the fixture fleet.

    ``accepts`` is the receiving half of a handoff — an active route of kind
    ``delegation`` — and it is separate from ``delegation`` on purpose, because the two
    halves are separate declarations and most of these tests are about them disagreeing.
    """
    data = valid_manifest()
    data["id"] = resident_id
    data["uid"] = UIDS[resident_id]
    data["home"] = home
    data["agent_id"] = f"claude-code:{resident_id}"
    data["soul"] = {**data["soul"], "name": resident_id.title(), "role": f"{resident_id} bot"}
    if delegation is not None:
        data["delegation"] = delegation
    if accepts:
        data["routes"] = [*data["routes"], dict(DELEGATION_ROUTE)]
    data.update(overrides)
    return data


@pytest.fixture
def write(write_resident: ResidentWriter) -> SpecWriter:
    """Write one fixture resident, soul and all."""

    def _write(spec: dict[str, Any]) -> Path:
        return write_resident(spec, soul=soul_for(spec["id"]))

    return _write


@pytest.fixture
def fleet(write: SpecWriter, tmp_path: Path) -> Path:
    """Build a two-level chain — boss → hand → worker — plus a resident off to one side."""
    write(resident_spec("boss", home=1, delegation={"send": True, "to": ["hand"]}))
    write(resident_spec("hand", home=2, accepts=True, delegation={"send": True, "to": ["worker"]}))
    write(resident_spec("worker", home=3, accepts=True))
    write(resident_spec("loner", home=4))
    return tmp_path / "residents"


def chart_of(tree: Path) -> OrgChart:
    """Validate a tree and project it."""
    result = validate_path(tree)
    assert not result.errors, [diagnostic.render() for diagnostic in result.errors]
    return org_chart(result.residents)


def pairs(chart: OrgChart) -> list[tuple[str, str]]:
    return [(edge.sender, edge.receiver) for edge in chart.edges]


def test_the_two_level_chain_is_the_edges_the_manifests_declare(fleet: Path) -> None:
    chart = chart_of(fleet)
    assert pairs(chart) == [("boss", "hand"), ("hand", "worker")]
    assert all(edge.deliverable for edge in chart.edges)
    assert all(edge.named for edge in chart.edges)


def test_every_resident_is_a_node_including_the_one_nobody_delegates_to(fleet: Path) -> None:
    chart = chart_of(fleet)
    assert [node.id for node in chart.nodes] == ["boss", "hand", "loner", "worker"]


def test_the_chain_ranks_managers_above_the_residents_they_hand_work_to(fleet: Path) -> None:
    chart = chart_of(fleet)
    assert {node.id: node.rank for node in chart.nodes} == {
        "boss": 0,
        "hand": 1,
        "worker": 2,
        "loner": 0,
    }


def test_two_residents_that_may_delegate_to_each_other_stay_on_the_top_row(
    write: SpecWriter, tmp_path: Path
) -> None:
    """A cycle has no top, so nobody in it is placed under anybody (see ``_ranks``)."""
    write(resident_spec("boss", home=1, accepts=True, delegation={"send": True, "to": ["hand"]}))
    write(resident_spec("hand", home=2, accepts=True, delegation={"send": True, "to": ["boss"]}))
    chart = chart_of(tmp_path / "residents")
    assert {node.id: node.rank for node in chart.nodes} == {"boss": 0, "hand": 0}
    assert sorted(pairs(chart)) == [("boss", "hand"), ("hand", "boss")]


def test_a_resident_sits_below_its_deepest_manager_not_its_nearest(
    write: SpecWriter, tmp_path: Path
) -> None:
    """Two routes to worker: boss → worker direct, and boss → hand → worker.

    Worker belongs under hand. The shortest path from a root is one hop, so a
    breadth-first walk would put worker on hand's own row while worker's card says it
    takes work from hand. Fails against a shortest-path rank; see ``_ranks``.
    """
    write(resident_spec("boss", home=1, delegation={"send": True, "to": ["hand", "worker"]}))
    write(resident_spec("hand", home=2, accepts=True, delegation={"send": True, "to": ["worker"]}))
    write(resident_spec("worker", home=3, accepts=True))

    chart = chart_of(tmp_path / "residents")

    assert {node.id: node.rank for node in chart.nodes} == {"boss": 0, "hand": 1, "worker": 2}


def test_a_named_receiver_with_no_open_door_is_drawn_and_says_why(
    write: SpecWriter, tmp_path: Path
) -> None:
    write(resident_spec("boss", home=1, delegation={"send": True, "to": ["worker"]}))
    write(resident_spec("worker", home=3))
    chart = chart_of(tmp_path / "residents")
    edge = chart.edges[0]
    assert (edge.sender, edge.receiver) == ("boss", "worker")
    assert edge.deliverable is False
    assert edge.reason == NO_OPEN_ROUTE
    # And it does not make anybody a manager: an edge that carries nothing is not a rank.
    assert {node.id: node.rank for node in chart.nodes} == {"boss": 0, "worker": 0}


def test_a_named_receiver_that_does_not_exist_is_drawn_and_says_why(
    write: SpecWriter, tmp_path: Path
) -> None:
    write(resident_spec("boss", home=1, delegation={"send": True, "to": ["ghost"]}))
    chart = chart_of(tmp_path / "residents")
    assert chart.edges[0].reason == NO_SUCH_RESIDENT
    assert chart.edges[0].deliverable is False


def test_a_retired_receiver_is_named_as_retired_rather_than_as_a_shut_door(
    write: SpecWriter, tmp_path: Path
) -> None:
    write(resident_spec("boss", home=1, delegation={"send": True, "to": ["worker"]}))
    write(resident_spec("worker", home=3, accepts=True, retired=True))
    chart = chart_of(tmp_path / "residents")
    assert chart.edges[0].reason == RECEIVER_RETIRED
    node = chart.node("worker")
    assert node is not None
    assert node.retired is True


def test_a_retired_sender_hands_work_to_nobody(write: SpecWriter, tmp_path: Path) -> None:
    write(resident_spec("boss", home=1, retired=True, delegation={"send": True, "to": ["worker"]}))
    write(resident_spec("worker", home=3, accepts=True))
    assert chart_of(tmp_path / "residents").edges == ()


def test_an_empty_allowlist_reaches_every_open_door_and_says_it_was_not_named(
    write: SpecWriter, tmp_path: Path
) -> None:
    write(resident_spec("boss", home=1, delegation={"send": True}))
    write(resident_spec("hand", home=2, accepts=True))
    write(resident_spec("worker", home=3, accepts=True))
    write(resident_spec("loner", home=4))
    chart = chart_of(tmp_path / "residents")
    assert sorted(pairs(chart)) == [("boss", "hand"), ("boss", "worker")]
    assert not any(edge.named for edge in chart.edges)
    assert all(edge.deliverable for edge in chart.edges)


def test_a_resident_that_never_delegates_produces_no_edges(fleet: Path) -> None:
    chart = chart_of(fleet)
    node = chart.node("loner")
    assert node is not None
    assert node.delegates is False
    assert "loner" not in {edge.sender for edge in chart.edges}


def test_a_node_carries_the_grants_mounts_and_declared_budget(
    write: SpecWriter, tmp_path: Path
) -> None:
    write(
        resident_spec(
            "boss",
            home=1,
            session_grants=["skills.write"],
            app_grants=[{"id": "burrow", "name": "Burrow", "status": "granted"}],
            budgets={"daily_cost_usd": 10.0, "max_run_seconds": 900},
            deploy={
                "mounts": [
                    {"host": "~/Life", "container": "/vault", "mode": "rw"},
                    {"host": "~/docker/warren", "container": "/checkout", "mode": "ro"},
                ]
            },
        )
    )
    node = chart_of(tmp_path / "residents").node("boss")
    assert node is not None
    assert node.session_grants == ("skills.write",)
    assert node.app_grants == (("burrow", "granted"),)
    assert [(m.container, m.mode) for m in node.mounts] == [("/vault", "rw"), ("/checkout", "ro")]
    assert node.budget.declared is True
    assert node.budget.daily_cost_usd == 10.0
    assert node.budget.daily_tokens is None


def test_a_resident_with_no_declared_cap_says_so_rather_than_going_quiet(fleet: Path) -> None:
    """Unlimited must not read as unknown — the same rule ``budget_summary`` follows."""
    node = chart_of(fleet).node("loner")
    assert node is not None
    assert node.budget.to_dict() == {
        "declared": False,
        "daily_cost_usd": None,
        "daily_tokens": None,
        "max_run_seconds": None,
    }


def test_the_wire_form_is_json_shaped_and_names_every_node_field(fleet: Path) -> None:
    document = chart_of(fleet).to_dict()
    assert set(document) == {"nodes", "edges"}
    assert set(document["nodes"][0]) == {
        "id",
        "uid",
        "name",
        "role",
        "accent",
        "summary",
        "retired",
        "rank",
        "session_grants",
        "app_grants",
        "mounts",
        "budget",
        "delegates",
        "accepts",
    }
    assert set(document["edges"][0]) == {"sender", "receiver", "named", "deliverable", "reason"}


def test_the_projection_is_the_same_answer_twice(fleet: Path) -> None:
    assert chart_of(fleet).to_dict() == chart_of(fleet).to_dict()


def test_an_empty_fleet_is_an_empty_chart() -> None:
    assert org_chart([]).to_dict() == {"nodes": [], "edges": []}


def test_the_shipped_fleet_projects_without_raising() -> None:
    """The chart is only useful if it survives the residents actually in the tree."""
    result = validate_path(RESIDENTS_DIR)
    chart = org_chart(result.residents)
    assert [node.id for node in chart.nodes] == [r.id for r in result.residents]
