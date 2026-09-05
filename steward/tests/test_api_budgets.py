"""API behavior: budgets."""

import copy
from typing import Any

import pytest

from conftest import (
    valid_manifest,
)
from steward.runners import Outcome, RunResult
from support.api import (
    ApiFactory,
    Harness,
)
from support.api import (
    api as api,  # noqa: PLC0414 — pytest fixture discovery
)

# --------------------------------------------------------------------------------------
# budgets (steward #8)
# --------------------------------------------------------------------------------------


def budgeted(**budgets: object) -> dict[str, Any]:
    """Build a manifest with the budgets a test wants to try."""
    data = copy.deepcopy(valid_manifest())
    data["budgets"] = dict(budgets)
    return data


def spend(harness: Harness, cost: float, *, resident: str = "test-agent") -> None:
    """Put one finished run on the ledger, as a scheduler would."""
    harness.store.record_run(
        resident=resident,
        agent_id="claude-code:test-agent",
        kind="routine",
        trigger="schedule",
        run_id="already-ran",
        cost_usd=cost,
        input_tokens=10,
        output_tokens=10,
    )


def test_the_budget_endpoint_reports_spent_against_limit_and_the_window(
    api: ApiFactory,
) -> None:
    """The read burrow's fleet-ops view draws its fuel gauges from."""
    harness = api(manifest=budgeted(daily_cost_usd=5.0, max_run_seconds=600))
    spend(harness, 1.25)

    payload = harness.client.get("/residents/test-agent/budget").json()

    assert payload["resident"] == "test-agent"
    assert payload["paused"] is False
    assert payload["pause"] is None
    assert payload["max_run_seconds"] == 600
    assert payload["spent"]["cost_usd"] == pytest.approx(1.25)
    assert payload["spent"]["tokens"] == 20
    cost = next(b for b in payload["budgets"] if b["budget"] == "daily_cost_usd")
    assert cost["limit"] == 5.0
    assert cost["remaining"] == pytest.approx(3.75)
    assert payload["window"]["start"] < payload["window"]["end"]


def test_an_unlimited_resident_reports_no_limit_rather_than_nothing(api: ApiFactory) -> None:
    """A panel that simply omitted the gauge would let unlimited read as unknown."""
    payload = api().client.get("/residents/test-agent/budget").json()
    assert payload["summary"] == "no limit"
    assert all(b["limit"] is None for b in payload["budgets"])


def test_the_budget_endpoint_refuses_an_unknown_resident(api: ApiFactory) -> None:
    """Same refusal as every other resident-scoped read."""
    response = api().client.get("/residents/nobody/budget")
    assert response.status_code == 404
    assert response.json()["detail"]["error"] == "unknown_resident"


def test_the_residents_list_carries_a_budget_summary(api: ApiFactory) -> None:
    """A stopped resident should not need a second round trip to look stopped."""
    harness = api(manifest=budgeted(daily_cost_usd=1.0))
    spend(harness, 4.0)
    harness.client.post("/residents/test-agent/routines/daily-summary/run")

    listed = harness.client.get("/residents").json()["residents"][0]

    assert listed["budget"]["paused"] is True
    assert listed["budget"]["declared"] is True
    assert listed["budget"]["spent_usd"] == pytest.approx(4.0)
    assert listed["budget"]["summary"] == "paused: budget exceeded"


def test_run_now_on_a_paused_resident_is_409(api: ApiFactory) -> None:
    """A human asking for a run now is not a way around a budget the same human set."""
    harness = api(manifest=budgeted(daily_cost_usd=2.0))
    spend(harness, 2.5)

    response = harness.client.post("/residents/test-agent/routines/daily-summary/run")

    assert response.status_code == 409
    detail = response.json()["detail"]
    assert detail["error"] == "budget_exceeded"
    assert detail["message"].startswith("paused: budget exceeded")
    assert "daily_cost_usd" in detail["message"]
    # Refused before anything is written: no request row, no routine_started.
    assert harness.store.export_request_history() == []
    assert harness.events("routine_started") == []


def test_the_pause_knocks_once_however_many_times_run_now_is_asked(api: ApiFactory) -> None:
    """One knock per pause, not one per refused fire."""
    harness = api(manifest=budgeted(daily_cost_usd=1.0))
    spend(harness, 9.0)
    for _ in range(3):
        harness.client.post("/residents/test-agent/routines/daily-summary/run")
    assert len(harness.events("needs_human")) == 1


def test_approving_the_budget_request_resumes_the_resident(api: ApiFactory) -> None:
    """The unpause path, end to end, through the ordinary approvals endpoint."""
    harness = api(manifest=budgeted(daily_cost_usd=1.0))
    spend(harness, 3.0)
    harness.client.post("/residents/test-agent/routines/daily-summary/run")
    request_id = harness.events("needs_human")[0]["payload"]["request_id"]

    decided = harness.client.post(f"/approvals/{request_id}", json={"decision": "approve"})

    assert decided.status_code == 202
    assert decided.json()["resumed"] == "test-agent"
    assert harness.store.budget_pause("test-agent") is None
    resolved = harness.events("needs_human_resolved")
    assert len(resolved) == 1
    assert resolved[0]["payload"]["decision"] == "approve"
    # And the resident really does fire again, once it is no longer paused.
    assert (
        harness.client.post("/residents/test-agent/routines/daily-summary/run").status_code == 202
    )
    harness.settle()
    assert harness.events("routine_started")


def test_denying_the_budget_request_leaves_the_resident_paused(api: ApiFactory) -> None:
    """A deny is a real answer: it says keep it stopped."""
    harness = api(manifest=budgeted(daily_cost_usd=1.0))
    spend(harness, 3.0)
    harness.client.post("/residents/test-agent/routines/daily-summary/run")
    request_id = harness.events("needs_human")[0]["payload"]["request_id"]

    decided = harness.client.post(f"/approvals/{request_id}", json={"decision": "deny"})

    assert decided.json()["resumed"] is None
    assert harness.store.budget_pause("test-agent") is not None
    assert (
        harness.client.post("/residents/test-agent/routines/daily-summary/run").status_code == 409
    )


def test_editing_a_budget_request_is_refused_and_leaves_the_resident_paused(
    api: ApiFactory,
) -> None:
    harness = api(manifest=budgeted(daily_cost_usd=1.0))
    spend(harness, 3.0)
    harness.client.post("/residents/test-agent/routines/daily-summary/run")
    request_id = harness.events("needs_human")[0]["payload"]["request_id"]

    decided = harness.client.post(
        f"/approvals/{request_id}", json={"decision": "edit", "edit": {"cap": 10}}
    )

    assert decided.status_code == 409
    assert decided.json()["detail"]["offered"] == ["approve", "deny"]
    assert harness.store.approval(request_id).pending  # ty: ignore
    assert harness.store.budget_pause("test-agent") is not None
    assert harness.events("needs_human_resolved") == []


def test_an_ordinary_approval_does_not_resume_anything(api: ApiFactory) -> None:
    """Only a budget request lifts a budget pause; nothing else is mistaken for one."""
    harness = api()
    record = harness.store.create_approval_request(
        agent_id="claude-code:test-agent",
        project="test-agent",
        action="send_email",
        message="Testy wants to send email",
        resident="test-agent",
    )
    decided = harness.client.post(f"/approvals/{record.request_id}", json={"decision": "approve"})
    assert decided.json()["resumed"] is None
    assert "the parked work resumes" in decided.json()["message"]


def test_a_run_now_lands_on_the_ledger(api: ApiFactory) -> None:
    """Every kind of run reports its consumption, including one a human asked for."""
    harness = api(
        behavior=lambda _request: RunResult(
            outcome=Outcome.OK, output="done", cost_usd=0.5, input_tokens=7, output_tokens=3
        )
    )
    harness.client.post("/residents/test-agent/routines/daily-summary/run")
    harness.settle()

    entries = harness.store.ledger("test-agent")
    assert len(entries) == 1
    assert entries[0].kind == "routine"
    assert entries[0].trigger == "manual"
    assert entries[0].ref == "daily-summary"
    assert entries[0].cost_usd == pytest.approx(0.5)
    assert entries[0].tokens == 10
