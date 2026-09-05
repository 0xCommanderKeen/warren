"""API behavior: approvals."""

import json
import threading

from steward import events as ev
from steward import manifest as m
from steward.runners import Outcome, RunRequest, RunResult
from support.api import (
    ApiFactory,
    Harness,
    _expired_pending,
    _pending,
)
from support.api import (
    api as api,  # noqa: PLC0414 — pytest fixture discovery
)


def test_pending_approvals_are_listed(api: ApiFactory) -> None:
    harness = api()
    request_id = _pending(harness)

    listed = harness.client.get("/approvals").json()["approvals"]
    assert [record["request_id"] for record in listed] == [request_id]
    assert listed[0]["action"] == "send_email"
    assert listed[0]["options"] == ["approve", "deny", "edit"]


def test_a_decision_is_recorded_and_emits_needs_human_resolved(api: ApiFactory) -> None:
    harness = api()
    request_id = _pending(harness)

    response = harness.client.post(f"/approvals/{request_id}", json={"decision": "approve"})
    assert response.status_code == 202
    assert response.json()["status"] == "recorded"
    assert response.json()["decision"] == "approve"
    assert response.json()["approval_request_id"] == request_id
    ledger_id = response.json()["request_id"]
    assert ledger_id != request_id
    assert harness.client.get(f"/requests/{ledger_id}").json()["outcome"] == "recorded"

    resolved = harness.events("needs_human_resolved")
    assert len(resolved) == 1
    assert resolved[0]["payload"] == {
        "request_id": request_id,
        "decision": "approve",
        "decided_by": "api",
        "action": "send_email",
    }
    # Emitted as the villager who knocked, so burrow walks the right one from the door.
    assert resolved[0]["agent_id"] == "claude-code:test-agent"
    assert harness.client.get("/approvals").json()["approvals"] == []
    assert [row.outcome for row in harness.store.export_request_history()] == ["recorded"]


def test_a_second_decision_changes_nothing_and_emits_nothing(api: ApiFactory) -> None:
    harness = api()
    request_id = _pending(harness)
    first = harness.client.post(f"/approvals/{request_id}", json={"decision": "approve"}).json()

    replay = harness.client.post(f"/approvals/{request_id}", json={"decision": "deny"})
    assert replay.status_code == 200
    assert replay.json()["decision"] == "approve"
    assert replay.json()["approval_request_id"] == request_id
    assert replay.json()["request_id"] != first["request_id"]
    replay_ledger = harness.client.get(f"/requests/{replay.json()['request_id']}").json()
    assert replay_ledger["outcome"] == "recorded"
    assert len(harness.events("needs_human_resolved")) == 1

    record = harness.store.approval(request_id)
    assert record is not None
    assert record.decision == "approve"


def test_returned_decision_id_polls_pending_then_recorded_after_worker_recovery(
    api: ApiFactory,
) -> None:
    """The response id names the ledger row the lifecycle worker later finalizes."""

    class RecoveringEmitter:
        def __init__(self) -> None:
            self.available = threading.Event()
            self.recovered = threading.Event()

        def emit_durable(self, _event: ev.Event) -> bool:
            if not self.available.is_set():
                return False
            self.recovered.set()
            return True

    emitter = RecoveringEmitter()
    harness = api(emitter=emitter)
    approval_id = _pending(harness)

    with harness.client:
        answer = harness.client.post(
            f"/approvals/{approval_id}", json={"decision": "approve"}
        ).json()
        ledger_id = answer["request_id"]
        assert answer["approval_request_id"] == approval_id
        assert harness.client.get(f"/requests/{ledger_id}").json()["outcome"] == (
            "recorded_announcement_pending"
        )

        emitter.available.set()
        harness.client.app.state.approval_outbox.notify()
        assert emitter.recovered.wait(2.0)
        for _ in range(100):
            polled = harness.client.get(f"/requests/{ledger_id}").json()
            if polled["outcome"] == "recorded":
                break
            threading.Event().wait(0.01)
        assert polled["request_id"] == ledger_id
        assert polled["outcome"] == "recorded"


def test_an_edit_decision_carries_the_humans_version(api: ApiFactory) -> None:
    harness = api()
    request_id = _pending(harness)

    response = harness.client.post(
        f"/approvals/{request_id}", json={"decision": "edit", "edit": {"subject": "shorter"}}
    )
    assert response.status_code == 202
    record = harness.store.approval(request_id)
    assert record is not None
    assert record.edit == {"subject": "shorter"}


def test_a_decision_the_request_did_not_offer_is_a_truthful_conflict(api: ApiFactory) -> None:
    harness = api()
    record = harness.store.create_approval_request(
        agent_id="claude-code:test-agent",
        project="test-agent",
        action="send_email",
        message="Testy wants to send an email",
        options=("approve", "deny"),
    )

    response = harness.client.post(
        f"/approvals/{record.request_id}",
        json={"decision": "edit", "edit": {"subject": "shorter"}},
    )

    assert response.status_code == 409
    assert response.json()["detail"] == {
        "error": "approval_decision_not_offered",
        "message": "decision 'edit' was not offered for this approval; use one of: approve, deny",
        "offered": ["approve", "deny"],
    }
    pending = harness.store.approval(record.request_id)
    assert pending is not None
    assert pending.pending
    assert pending.decision is None
    assert pending.edit is None
    assert harness.events("needs_human_resolved") == []


def test_a_replay_wins_over_whether_the_retried_decision_was_offered(api: ApiFactory) -> None:
    harness = api()
    record = harness.store.create_approval_request(
        agent_id="claude-code:test-agent",
        project="test-agent",
        action="send_email",
        message="Testy wants to send an email",
        options=("approve",),
    )
    harness.client.post(f"/approvals/{record.request_id}", json={"decision": "approve"})

    replay = harness.client.post(
        f"/approvals/{record.request_id}",
        json={"decision": "edit", "edit": {"subject": "shorter"}},
    )

    assert replay.status_code == 200
    assert replay.json()["decision"] == "approve"
    assert len(harness.events("needs_human_resolved")) == 1


def test_expiry_wins_over_whether_the_late_decision_was_offered(api: ApiFactory) -> None:
    harness = api()
    request_id = _expired_pending(harness)

    response = harness.client.post(
        f"/approvals/{request_id}",
        json={"decision": "edit", "edit": {"subject": "shorter"}},
    )

    assert response.status_code == 409
    assert response.json()["detail"]["error"] == "approval_expired"


def test_deciding_an_unknown_request_is_404(api: ApiFactory) -> None:
    harness = api()
    response = harness.client.post("/approvals/no-such-request", json={"decision": "approve"})
    assert response.status_code == 404
    assert response.json()["detail"]["error"] == "unknown_approval"
    assert harness.events() == []


def test_an_unknown_decision_is_refused(api: ApiFactory) -> None:
    harness = api()
    request_id = _pending(harness)
    assert (
        harness.client.post(f"/approvals/{request_id}", json={"decision": "maybe"}).status_code
        == 422
    )
    assert harness.store.pending_approvals()[0].request_id == request_id


def test_run_now_harvests_an_approval_block(api: ApiFactory) -> None:
    """A manual fire is a fire: its <needs-human> region is harvested, not dropped (#W1)."""
    block = (
        "drafted it\n===STEWARD-ACTIONS===\n"
        '<needs-human action="send_email">\n{"to": "a@example.com"}\n</needs-human>\n'
        "===END-STEWARD-ACTIONS==="
    )
    harness = api(behavior=lambda _request: RunResult(outcome=Outcome.OK, output=block))
    harness.client.post("/residents/test-agent/routines/daily-summary/run")
    harness.settle()

    assert [record.action for record in harness.store.pending_approvals()] == ["send_email"]
    assert len(harness.events("needs_human")) == 1


def test_run_now_delivers_a_pending_decision(api: ApiFactory) -> None:
    """A decision answered while the resident slept reaches it on a manual wake-up too (#W1)."""
    prompts: list[str] = []

    def record_prompt(request: RunRequest) -> RunResult:
        prompts.append(request.prompt)
        return RunResult(outcome=Outcome.OK, output="did the thing")

    harness = api(behavior=record_prompt)
    request = harness.store.create_approval_request(
        agent_id="claude-code:test-agent",
        project="test-agent",
        action="send_email",
        message="Testy wants to send an email",
        resident="test-agent",
    )
    harness.store.decide(request.request_id, "approve", decided_by="api")

    harness.client.post("/residents/test-agent/routines/daily-summary/run")
    harness.settle()

    # Delivered exactly once, into the session's own preamble, and marked told.
    assert harness.store.undelivered_decisions("test-agent") == []
    assert any("send_email: approve" in prompt for prompt in prompts)


def test_the_approval_list_defaults_to_pending_and_filters_on_request(api: ApiFactory) -> None:
    harness = api()
    decided = _pending(harness)
    waiting = _pending(harness)
    harness.client.post(f"/approvals/{decided}", json={"decision": "approve"})

    default = harness.client.get("/approvals").json()
    assert default["status"] == "pending"
    assert [r["request_id"] for r in default["approvals"]] == [waiting]

    resolved = harness.client.get("/approvals", params={"status": "resolved"}).json()
    assert [r["request_id"] for r in resolved["approvals"]] == [decided]

    every = harness.client.get("/approvals", params={"status": "all"}).json()
    assert {r["request_id"] for r in every["approvals"]} == {decided, waiting}


def _pending_with_a_secret(harness: Harness) -> str:
    record = harness.store.create_approval_request(
        agent_id="claude-code:test-agent",
        project="test-agent",
        action="send_email",
        message="Testy needs ghp_abcdefghijklmnopqrstuvwxyz0123456789 to send it",
        detail={"to": "plumber@example.com", "auth": "Bearer ghp_zyxwvutsrqponmlkjihgfe98765"},
    )
    return record.request_id


def test_the_approval_list_scrubs_what_the_session_typed(api: ApiFactory) -> None:
    """The API is a rendering meant for a human, and the console puts it on a screen.

    Redaction already ran on the event path to burrow; the row served here was the copy
    that still carried the secret (steward #144).
    """
    harness = api()
    _pending_with_a_secret(harness)

    served = harness.client.get("/approvals").json()["approvals"][0]

    assert "ghp_" not in json.dumps(served)
    assert m.SECRET_REDACTION in served["message"]
    assert m.SECRET_REDACTION in served["detail"]["auth"]
    assert served["detail"]["to"] == "plumber@example.com"  # only the secret is cut


def test_auditing_one_approval_by_id_scrubs_it_too(api: ApiFactory) -> None:
    harness = api()
    request_id = _pending_with_a_secret(harness)

    served = harness.client.get(f"/approvals/{request_id}").json()

    assert "ghp_" not in json.dumps(served)
    assert m.SECRET_REDACTION in served["message"]


def test_the_stored_row_still_holds_what_the_session_typed(api: ApiFactory) -> None:
    """Redaction is egress, not storage: the resident's own copy must stay truthful."""
    harness = api()
    request_id = _pending_with_a_secret(harness)

    stored = harness.store.approval(request_id)

    assert stored is not None
    assert "ghp_abcdefghijklmnopqrstuvwxyz0123456789" in stored.message


def test_an_edit_can_change_one_key_without_seeing_the_withheld_one(api: ApiFactory) -> None:
    """The console prefills its edit box from the scrubbed detail it was served.

    An edit *replaces* the whole detail, so without restoring the withheld value the only
    ways to change one key would be to retype a live credential into a browser textarea or
    to drop the key and take the value away from the resident. Sending back exactly what
    was shown means "I did not touch this" (steward #144).
    """
    harness = api()
    request_id = _pending_with_a_secret(harness)
    shown = harness.client.get(f"/approvals/{request_id}").json()["detail"]
    assert m.SECRET_REDACTION in shown["auth"]

    edited = {**shown, "to": "roofer@example.com"}
    response = harness.client.post(
        f"/approvals/{request_id}", json={"decision": "edit", "edit": edited}
    )

    assert response.status_code == 202
    recorded = harness.store.approval(request_id)
    assert recorded is not None
    assert recorded.edit is not None
    assert recorded.edit["to"] == "roofer@example.com"
    # The value the decider never saw is the value the resident gets back, intact.
    assert recorded.edit["auth"] == "Bearer ghp_zyxwvutsrqponmlkjihgfe98765"


def test_a_marker_no_stored_value_explains_is_refused(api: ApiFactory) -> None:
    """Restoring is equality against what was shown, never "there is a marker here"."""
    harness = api()
    request_id = _pending_with_a_secret(harness)

    response = harness.client.post(
        f"/approvals/{request_id}",
        json={"decision": "edit", "edit": {"auth": f"Bearer {m.SECRET_REDACTION} and mine"}},
    )

    assert response.status_code == 422
    assert response.json()["detail"]["error"] == "edit_withheld_value"
    still_open = harness.store.approval(request_id)
    assert still_open is not None
    assert still_open.pending  # nothing was decided


def test_an_edit_that_replaces_the_withheld_value_outright_is_accepted(
    api: ApiFactory,
) -> None:
    harness = api()
    request_id = _pending_with_a_secret(harness)

    response = harness.client.post(
        f"/approvals/{request_id}",
        json={"decision": "edit", "edit": {"auth": "Bearer the-real-one"}},
    )

    assert response.status_code == 202
    recorded = harness.store.approval(request_id)
    assert recorded is not None
    assert recorded.edit == {"auth": "Bearer the-real-one"}


def test_an_unknown_approval_status_is_refused(api: ApiFactory) -> None:
    harness = api()
    response = harness.client.get("/approvals", params={"status": "ignored"})
    assert response.status_code == 422
    assert response.json()["detail"]["error"] == "unknown_status"


def test_one_request_can_be_audited_by_id(api: ApiFactory) -> None:
    """What did I approve, and when: request, decision, decider, timestamps, one call."""
    harness = api()
    request_id = _pending(harness)
    harness.client.post(f"/approvals/{request_id}", json={"decision": "deny"})

    audited = harness.client.get(f"/approvals/{request_id}").json()
    assert audited["action"] == "send_email"
    assert audited["detail"] == {"to": "plumber@example.com"}
    assert audited["decision"] == "deny"
    assert audited["decided_by"] == "api"
    assert audited["created_at"]
    assert audited["decided_at"]
    assert harness.client.get("/approvals/never-existed").status_code == 404
