"""API behavior: session auth."""

import datetime as dt

from fastapi.testclient import TestClient

from conftest import (
    ResidentWriter,
)
from steward import events as ev
from steward.input_bounds import (
    APPROVAL_BODY_MAX_BYTES,
)
from steward.run_lifecycle import RUN_LEASE_GRACE_S
from steward.runs import RUN_ROUTINE, RUN_TASK
from steward.session_auth import (
    SESSION_CREDENTIAL_PREFIX,
    credential_digest,
)
from support.api import (
    HANDOFF,
    NEW_RESIDENT,
    ApiFactory,
    _pending,
    as_session,
    open_session_run,
    with_receiver,
)
from support.api import (
    api as api,  # noqa: PLC0414 — pytest fixture discovery
)


def test_a_session_credential_authenticates_a_read(api: ApiFactory) -> None:
    """The credential is a real credential: it gets a session through the door."""
    harness = api()
    credential = open_session_run(harness)

    response = harness.client.get("/approvals", headers=as_session(credential))

    assert response.status_code == 200


def test_a_session_cannot_decide_its_own_approval(api: ApiFactory) -> None:
    """The escalation boundary is the whole safety story, and this is what held it up.

    ``POST /approvals/{id}`` was gated by the same shared token the session was carrying,
    so the resident that raised an approval could also answer it — and every guarantee
    downstream of "a human decided", including expiry's deny-by-default, was only as strong
    as the session not noticing.
    """
    harness = api()
    credential = open_session_run(harness)
    request_id = _pending(harness)

    response = harness.client.post(
        f"/approvals/{request_id}",
        json={"decision": "approve"},
        headers=as_session(credential),
    )

    assert response.status_code == 403
    detail = response.json()["detail"]
    assert detail["error"] == "session_credential_forbidden"
    assert "answering its own knock" in detail["message"]
    assert harness.store.approval(request_id).pending  # ty: ignore[unresolved-attribute]
    assert harness.store.export_request_history() == [], "and nothing was logged as accepted"
    assert harness.events("needs_human_resolved") == []


def test_a_session_may_not_declare_a_resident_or_fire_a_routine(api: ApiFactory) -> None:
    """Both are human acts, and both name themselves in the refusal."""
    harness = api()
    credential = open_session_run(harness)

    declared = harness.client.post("/residents", json=NEW_RESIDENT, headers=as_session(credential))
    fired = harness.client.post(
        "/residents/test-agent/routines/daily-summary/run", headers=as_session(credential)
    )

    assert declared.status_code == 403
    assert "may not add to the fleet" in declared.json()["detail"]["message"]
    assert fired.status_code == 403
    assert "arrives through the board" in fired.json()["detail"]["message"]
    assert not (harness.residents_dir / "note-keeper").exists()
    assert harness.store.export_request_history() == []


def test_every_other_write_path_is_refused_too(api: ApiFactory) -> None:
    """An allowlist, not a denylist: a route nobody decided about is not reachable."""
    harness = api()
    credential = open_session_run(harness)

    response = harness.client.post(
        "/jobs", json={"title": "Research X"}, headers=as_session(credential)
    )

    assert response.status_code == 403
    assert response.json()["detail"]["error"] == "session_credential_forbidden"
    assert harness.store.jobs() == []


def test_a_session_delegates_as_itself_without_naming_itself(
    api: ApiFactory, write_resident: ResidentWriter
) -> None:
    """Omitting ``from`` used to mean "a person asked", which skipped the sender charter."""
    harness = with_receiver(api, write_resident)
    credential = open_session_run(harness)

    response = harness.client.post("/delegate", json=HANDOFF, headers=as_session(credential))

    assert response.status_code == 202
    (delivered,) = harness.store.inbox("receiver-agent")
    assert delivered.delegated_by == "test-agent"
    assert delivered.origin == "resident:test-agent"


def test_a_session_may_name_itself_and_nobody_else(
    api: ApiFactory, write_resident: ResidentWriter
) -> None:
    """Its own id is redundant but honest; another resident's is a forged signature."""
    harness = with_receiver(api, write_resident)
    credential = open_session_run(harness)

    honest = harness.client.post(
        "/delegate", json={**HANDOFF, "from": "test-agent"}, headers=as_session(credential)
    )
    forged = harness.client.post(
        "/delegate", json={**HANDOFF, "from": "receiver-agent"}, headers=as_session(credential)
    )

    assert honest.status_code == 202
    assert forged.status_code == 403
    assert forged.json()["detail"]["error"] == "sender_not_the_caller"
    assert len(harness.store.inbox("receiver-agent")) == 1, "only the honest one landed"


def test_binding_the_sender_makes_the_chain_follow_for_free(
    api: ApiFactory, write_resident: ResidentWriter
) -> None:
    """#41 binds who the sender is; #67 then decides the chain, and needs no help.

    ``Delegator._resolve_parent`` already derives the parent from the tasks the sender is
    actually holding and honours a supplied id only when it is one of them. So a session
    that names nothing still descends from the task it claimed, and a session that names
    somebody else's chain is put back into its own — which is why this route does not
    derive ``parent_task_id`` a second time.
    """
    harness = with_receiver(api, write_resident)
    root = harness.client.post("/jobs", json={"title": "The root task"}).json()["task_id"]
    other = harness.client.post("/jobs", json={"title": "Not this one"}).json()["task_id"]
    lease = ev.utc_now_iso(dt.datetime.now(dt.UTC) + dt.timedelta(minutes=30))
    claimed = harness.store.claim_next_job(
        claimant="claude-code:test-agent", skills=frozenset(), lease_expires_at=lease
    )
    assert claimed is not None
    assert claimed.task_id == root
    credential = open_session_run(harness, kind=RUN_TASK, trigger="", ref=root)

    silent = harness.client.post("/delegate", json=HANDOFF, headers=as_session(credential))
    forged = harness.client.post(
        "/delegate",
        json={**HANDOFF, "parent_task_id": other},
        headers=as_session(credential),
    )

    assert silent.json()["parent_task_id"] == root
    assert forged.json()["parent_task_id"] == root, "forging the parent escapes nothing"


def test_a_human_still_delegates_on_behalf_of_anybody(
    api: ApiFactory, write_resident: ResidentWriter
) -> None:
    """The human path is untouched: this issue removes reach, it does not add refusals."""
    harness = with_receiver(api, write_resident)

    response = harness.client.post("/delegate", json={**HANDOFF, "from": "test-agent"})

    assert response.status_code == 202


def test_a_credential_stops_working_when_its_run_closes(api: ApiFactory) -> None:
    """Expiry rides the run lifecycle, so a leaked credential is worthless by morning."""
    harness = api()
    credential = open_session_run(harness)
    assert harness.client.get("/approvals", headers=as_session(credential)).status_code == 200

    assert harness.store.close_run("run-1")

    response = harness.client.get("/approvals", headers=as_session(credential))
    assert response.status_code == 401
    assert response.json()["detail"]["error"] == "unauthorized"


def test_a_credential_stops_working_when_its_lease_goes_stale(api: ApiFactory) -> None:
    """Before the watchdog sweeps, too: the bound is when a run *could* be buried."""
    harness = api()
    stale = ev.utc_now_iso(dt.datetime.now(dt.UTC) - dt.timedelta(seconds=RUN_LEASE_GRACE_S + 60))
    credential = open_session_run(harness, heartbeat_at=stale)

    response = harness.client.get("/approvals", headers=as_session(credential))

    assert response.status_code == 401
    assert harness.store.open_runs(), "and the row is still open — nobody swept it yet"


def test_a_credential_stops_working_once_a_terminal_fact_is_chosen(api: ApiFactory) -> None:
    """A run whose end is decided is over, whether or not the event has been published."""
    harness = api()
    credential = open_session_run(harness)

    assert harness.store.claim_run_terminal(
        "run-1", event="{}", event_id="run-terminal:run-1", owner_token=""
    )

    assert harness.client.get("/approvals", headers=as_session(credential)).status_code == 401


def test_an_invented_credential_is_401(api: ApiFactory) -> None:
    harness = api()
    open_session_run(harness)

    invented = f"{SESSION_CREDENTIAL_PREFIX}{'a' * 43}"
    response = harness.client.get("/approvals", headers=as_session(invented))

    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"


def test_a_run_opened_without_a_credential_cannot_be_impersonated_by_an_empty_one(
    api: ApiFactory,
) -> None:
    """Every legacy row stores the empty digest; matching it would be a master key."""
    harness = api()
    assert harness.store.open_run(
        run_id="unregistered", kind=RUN_ROUTINE, trigger="schedule", agent_id="claude-code:x"
    )

    anonymous = TestClient(harness.client.app)
    assert anonymous.get("/approvals", headers={"Authorization": "Bearer "}).status_code == 401
    assert anonymous.get("/approvals").status_code == 401


def test_the_credential_is_stored_as_a_digest_and_never_in_the_clear(api: ApiFactory) -> None:
    """A copy of steward.db must not yield live credentials."""
    harness = api()
    credential = open_session_run(harness)

    (row,) = harness.store._conn.execute("SELECT * FROM open_runs").fetchall()

    stored = set(dict(row).values())
    assert credential not in stored
    assert credential_digest(credential) in stored


def test_an_oversized_approval_body_is_still_bounded_for_a_session(api: ApiFactory) -> None:
    """The depth guard must not hold for one credential kind and not the other.

    A session credential authenticates and is *then* refused by the route policy, so it
    reaches the same body handling the human token does. The middleware therefore bounds it
    on the credential's shape, before anything recursive is materialised.
    """
    harness = api()
    credential = open_session_run(harness)
    request_id = _pending(harness)
    raw = b'{"decision":"edit","edit":' + b"[" * 70_000 + b"]" * 70_000 + b"}"
    assert len(raw) > APPROVAL_BODY_MAX_BYTES

    response = harness.client.post(
        f"/approvals/{request_id}",
        content=raw,
        headers={"content-type": "application/json", **as_session(credential)},
    )

    assert response.status_code == 413
    assert harness.store.approval(request_id).pending  # ty: ignore[unresolved-attribute]


def test_open_mode_has_no_boundary_and_does_not_pretend_to(api: ApiFactory) -> None:
    """``--allow-open`` has no token to scope, so it is stated rather than simulated.

    Refusing a presented session credential here would be theatre: the same session can
    reach the same route with no header at all. The credential is still minted, so nothing
    about a run changes shape between modes — only what the API can enforce does.
    """
    harness = api(token=None, allow_open=True)
    credential = open_session_run(harness)
    request_id = _pending(harness)

    decided = harness.client.post(
        f"/approvals/{request_id}", json={"decision": "approve"}, headers=as_session(credential)
    )
    headerless = harness.client.post("/jobs", json={"title": "Anything"})

    assert decided.status_code == 202
    assert headerless.status_code == 202
