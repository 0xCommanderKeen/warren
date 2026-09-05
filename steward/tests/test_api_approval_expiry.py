"""API behavior: approval expiry."""

import datetime as dt
import logging
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import pytest

from steward import events as ev
from steward.runners import Outcome, RunRequest, RunResult
from steward.transitions.approval import ApprovalTransitions
from support.api import (
    ApiFactory,
    _expired_pending,
)
from support.api import (
    api as api,  # noqa: PLC0414 — pytest fixture discovery
)


def test_approval_gets_are_read_only_even_for_unknown_ids(api: ApiFactory) -> None:
    """Neither polling nor an attacker-chosen missing id may sweep the ledger."""
    harness = api()
    request_id = _expired_pending(harness)

    assert harness.client.get("/approvals").json()["approvals"] == []
    assert harness.client.get("/approvals/never-existed").status_code == 404
    record = harness.store.approval(request_id)
    assert record is not None
    assert record.pending
    assert harness.events("needs_human_resolved") == []


def test_api_lifespan_alone_expires_and_stops_cleanly(api: ApiFactory) -> None:
    """Serve owns expiry even when none of steward's daemons are running (#143)."""
    moment = dt.datetime(2030, 8, 27, 12, tzinfo=dt.UTC)
    harness = api(now=lambda: moment, approval_expiry_interval_s=0.01)
    request_id = _expired_pending(harness)

    with harness.client:
        task = harness.client.app.state.approval_expiry_task
        for _ in range(100):
            record = harness.store.approval(request_id)
            if record is not None and not record.pending and harness.events("needs_human_resolved"):
                break
            threading.Event().wait(0.01)
        assert record is not None
        assert record.decision == "deny"
        assert record.decided_by == "expiry"
        assert len(harness.events("needs_human_resolved")) == 1
        assert not task.done()

    assert task.done()
    assert harness.client.app.state.approval_expiry_task is None


def test_expiry_loop_logs_a_failed_pass_and_keeps_running(
    api: ApiFactory, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    original = ApprovalTransitions.expire
    calls = 0

    def flaky(self: ApprovalTransitions, now: dt.datetime | None = None) -> list[Any]:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("temporary expiry failure")
        return original(self, now)

    monkeypatch.setattr(ApprovalTransitions, "expire", flaky)
    harness = api(
        now=lambda: dt.datetime(2030, 8, 27, 12, tzinfo=dt.UTC),
        approval_expiry_interval_s=0.01,
    )
    request_id = _expired_pending(harness)

    with caplog.at_level(logging.ERROR, logger="steward.api"), harness.client:
        for _ in range(100):
            record = harness.store.approval(request_id)
            if record is not None and not record.pending:
                break
            threading.Event().wait(0.01)

    assert record is not None
    assert record.decision == "deny"
    assert calls >= 2
    assert "approval expiry sweep failed; will retry" in caplog.text


def test_expiry_loop_never_overlaps_a_slow_pass(
    api: ApiFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    entered = threading.Event()
    release = threading.Event()
    calls = 0

    def slow(_self: ApprovalTransitions, _now: dt.datetime | None = None) -> list[Any]:
        nonlocal calls
        calls += 1
        entered.set()
        release.wait(timeout=10.0)
        return []

    monkeypatch.setattr(ApprovalTransitions, "expire", slow)
    harness = api(approval_expiry_interval_s=0.001)

    with harness.client:
        assert entered.wait(timeout=10.0)
        threading.Event().wait(0.03)
        assert calls == 1
        release.set()


def test_an_expired_request_cannot_be_decided(api: ApiFactory) -> None:
    """A click past the deadline must not slip an action through ahead of the sweep (#66)."""
    harness = api()
    request_id = _expired_pending(harness)

    response = harness.client.post(f"/approvals/{request_id}", json={"decision": "approve"})
    assert response.status_code == 409
    assert response.json()["detail"]["error"] == "approval_expired"
    # The late answer loses, but this API request also closes the overdue row.
    record = harness.store.approval(request_id)
    assert record is not None
    assert record.decision == "deny"
    assert record.decided_by == "expiry"
    assert len(harness.events("needs_human_resolved")) == 1
    assert [r.request_id for r in harness.store.undelivered_decisions("test-agent")] == [request_id]


def test_post_uses_the_app_clock_to_deny_an_expired_request_before_the_sweep(
    api: ApiFactory,
) -> None:
    """The request-time guard holds even before the lifespan worker has started (#143)."""
    moment = dt.datetime(2030, 8, 27, 12, tzinfo=dt.UTC)
    harness = api(now=lambda: moment)
    record = harness.store.create_approval_request(
        agent_id="claude-code:test-agent",
        project="test-agent",
        action="send_email",
        message="Testy wants to send an email after its deadline",
        resident="test-agent",
        expires_at=ev.utc_now_iso(moment - dt.timedelta(seconds=1)),
    )

    late = harness.client.post(f"/approvals/{record.request_id}", json={"decision": "approve"})
    replay = harness.client.post(f"/approvals/{record.request_id}", json={"decision": "approve"})

    assert late.status_code == 409
    assert late.json()["detail"]["error"] == "approval_expired"
    assert replay.status_code == 200
    assert replay.json()["decision"] == "deny"
    decided = harness.store.approval(record.request_id)
    assert decided is not None
    assert decided.decision == "deny"
    assert decided.decided_by == "expiry"
    assert decided.decided_at == ev.utc_now_iso(moment)
    resolved = harness.events("needs_human_resolved")
    assert len(resolved) == 1
    assert resolved[0]["payload"]["decision"] == "deny"
    assert resolved[0]["payload"]["decided_by"] == "expiry"


def test_expiry_and_late_decisions_race_to_one_deny_and_one_event(api: ApiFactory) -> None:
    """Concurrent API traffic cannot duplicate or replace the expiry decision (#143)."""
    harness = api()
    request_id = _expired_pending(harness)

    with ThreadPoolExecutor(max_workers=8) as pool:
        responses = list(
            pool.map(
                lambda decision: harness.client.post(
                    f"/approvals/{request_id}", json={"decision": decision}
                ),
                ["approve", "deny"] * 4,
            )
        )

    # A loser may observe the durable decision while its announcement is still queued.
    assert {response.status_code for response in responses} <= {200, 202, 409}
    record = harness.store.approval(request_id)
    assert record is not None
    assert record.decision == "deny"
    assert record.decided_by == "expiry"
    assert len(harness.events("needs_human_resolved")) == 1


def test_an_api_swept_deny_reaches_the_resident_on_its_next_wake(api: ApiFactory) -> None:
    """Serve-only expiry leaves the ordinary exactly-once delivery path intact (#143)."""
    prompts: list[str] = []

    def record_prompt(request: RunRequest) -> RunResult:
        prompts.append(request.prompt)
        return RunResult(outcome=Outcome.OK, output="understood")

    harness = api(behavior=record_prompt, approval_expiry_interval_s=0.01)
    _expired_pending(harness)
    with harness.client:
        for _ in range(100):
            if harness.events("needs_human_resolved"):
                break
            threading.Event().wait(0.01)
        harness.client.post("/residents/test-agent/routines/daily-summary/run")
        harness.settle()

    assert any("send_email: deny" in prompt for prompt in prompts)
    assert harness.store.undelivered_decisions("test-agent") == []
