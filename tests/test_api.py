"""The API is the only write path into the fleet, so every promise it makes is tested.

The tests run the real routes through FastAPI's ``TestClient`` — no network, no server
— against a mock runner, a scratch database, and an emitter whose "village" is a JSONL
file. That last piece is what lets a test assert the thing the contract actually
promises: not that a request returned 202, but that the matching protocol event landed.
"""

import copy
import datetime as dt
import json
import re
import threading
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from conftest import ResidentWriter, SkillWriter, valid_manifest
from steward import events as ev
from steward import journal
from steward.api import ApiConfig, ApiError, create_app
from steward.deploy import LocalTransport
from steward.manifest import Runner as RunnerSpec
from steward.manifest import validate_tree
from steward.nursery import raise_resident
from steward.runners import MockRunner, Outcome, RunRequest, RunResult
from steward.scheduler import FireReport, ScheduledRoutine
from steward.store import Store

TOKEN = "a-shared-secret"
AUTH = {"Authorization": f"Bearer {TOKEN}"}

NEW_RESIDENT: dict[str, Any] = {
    "id": "note-keeper",
    "name": "Quill",
    "char": "Scribe",
    "accent": "#4f7ea6",
    "role": "note bot",
    "charter": {
        "mission": "Keep the village's notes in order.",
        "duties": ["Tidy the notes each evening."],
        "rules": ["Never delete a note without asking."],
        "escalation": "Raise needs_human before anything irreversible.",
    },
}

#: Words the API is never allowed to use about work it has only accepted.
FORBIDDEN = re.compile(r"\b(done|ran|complete|completed|succeeded|success)\b", re.IGNORECASE)


@dataclass
class Harness:
    """One built app, plus the collaborators a test needs to look inside."""

    client: TestClient
    store: Store
    events_path: Path
    residents_dir: Path
    released: list[threading.Event] = field(default_factory=list)

    def events(self, event_type: str | None = None) -> list[dict[str, Any]]:
        """Read the village's log: every event the API actually emitted."""
        if not self.events_path.is_file():
            return []
        lines = self.events_path.read_text(encoding="utf-8").splitlines()
        parsed = [json.loads(line) for line in lines if line.strip()]
        return [e for e in parsed if event_type is None or e["type"] == event_type]

    def settle(self) -> None:
        """Let every queued manual run finish before looking at the log."""
        self.client.app.state.runs.wait(timeout=10.0)


type ApiFactory = Callable[..., Harness]


@pytest.fixture
def api(tmp_path: Path, write_resident: ResidentWriter) -> Iterator[ApiFactory]:
    """Build an app with a mock runner, a scratch store, and a file for a village."""
    built: list[Harness] = []

    def _make(  # noqa: PLR0913 — one keyword per thing a test wants to vary
        *,
        manifest: dict[str, Any] | None = None,
        token: str | None = TOKEN,
        allow_open: bool = False,
        cors_origins: tuple[str, ...] = (),
        behavior: Callable[[RunRequest], RunResult] | None = None,
        db_path: Path | None = None,
        residents: bool = True,
        nursery: Any = raise_resident,  # noqa: ANN401 — the pipeline seam, injected
        transport: LocalTransport | None = None,
    ) -> Harness:
        residents_dir = tmp_path / "residents"
        residents_dir.mkdir(exist_ok=True)
        if residents:
            write_resident(manifest or valid_manifest(), root=residents_dir)
        events_path = tmp_path / "events.jsonl"
        store = Store(db_path or ":memory:")
        app = create_app(
            ApiConfig(
                residents_dir=residents_dir,
                token=token,
                allow_open=allow_open,
                cors_origins=cors_origins,
                workdir=tmp_path,
            ),
            store=store,
            emitter=ev.EventEmitter(url=None, fallback=events_path),
            runner_factory=lambda spec: MockRunner(spec, behavior=behavior),
            nursery=nursery,
            transport=transport,
        )
        harness = Harness(
            client=TestClient(app, headers=dict(AUTH) if token else {}),
            store=store,
            events_path=events_path,
            residents_dir=residents_dir,
        )
        built.append(harness)
        return harness

    yield _make

    for harness in built:
        for release in harness.released:
            release.set()
        harness.client.app.state.runs.shutdown()
        harness.store.close()


def disabled_routine_manifest() -> dict[str, Any]:
    """Build a manifest whose only routine is declared but switched off."""
    data = copy.deepcopy(valid_manifest())
    data["routines"][0]["enabled"] = False
    return data


# --------------------------------------------------------------------------------------
# auth
# --------------------------------------------------------------------------------------


def test_an_unset_token_refuses_to_start(tmp_path: Path) -> None:
    with pytest.raises(ApiError, match="--allow-open"):
        create_app(ApiConfig(residents_dir=tmp_path, token=None))


def test_a_blank_token_counts_as_unset(tmp_path: Path) -> None:
    with pytest.raises(ApiError, match="STEWARD_TOKEN"):
        create_app(ApiConfig(residents_dir=tmp_path, token="   "))


def test_open_mode_is_the_only_way_to_serve_without_a_token(api: ApiFactory) -> None:
    harness = api(token=None, allow_open=True)
    assert harness.client.get("/residents").status_code == 200


def test_a_missing_token_is_401(api: ApiFactory) -> None:
    harness = api()
    anonymous = TestClient(harness.client.app)
    response = anonymous.get("/residents")
    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"
    assert response.json()["detail"]["error"] == "unauthorized"


def test_a_wrong_token_is_401_and_queues_nothing(api: ApiFactory) -> None:
    harness = api()
    response = harness.client.post(
        "/jobs", json={"title": "Research X"}, headers={"Authorization": "Bearer wrong-secret"}
    )
    assert response.status_code == 401
    assert harness.store.jobs() == []
    assert harness.store.requests() == []
    assert harness.events() == []


def test_a_token_in_the_wrong_scheme_is_401(api: ApiFactory) -> None:
    harness = api()
    response = harness.client.get("/residents", headers={"Authorization": f"Basic {TOKEN}"})
    assert response.status_code == 401


def test_the_token_is_compared_in_constant_time(
    api: ApiFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A naive == leaks the token one byte at a time to anyone who can time a request."""
    calls: list[tuple[bytes, bytes]] = []

    def spy(left: bytes, right: bytes) -> bool:
        calls.append((left, right))
        return left == right

    monkeypatch.setattr("steward.api.compare_digest", spy)
    harness = api()

    assert harness.client.get("/residents").status_code == 200
    assert calls == [(TOKEN.encode(), TOKEN.encode())]

    calls.clear()
    same_length = "a-shared-secreT"
    assert (
        harness.client.get(
            "/residents", headers={"Authorization": f"Bearer {same_length}"}
        ).status_code
        == 401
    )
    assert calls == [(same_length.encode(), TOKEN.encode())]


# --------------------------------------------------------------------------------------
# run now
# --------------------------------------------------------------------------------------


def test_run_now_lands_routine_started_with_trigger_manual(api: ApiFactory) -> None:
    harness = api()
    response = harness.client.post("/residents/test-agent/routines/daily-summary/run")

    assert response.status_code == 202
    body = response.json()
    assert body["status"] == "accepted"
    assert body["request_id"]
    assert body["trigger"] == "manual"

    harness.settle()
    started = harness.events("routine_started")
    assert len(started) == 1
    assert started[0]["payload"]["trigger"] == "manual"
    assert started[0]["payload"]["routine"] == "daily-summary"
    assert started[0]["agent_id"] == "claude-code:test-agent"
    assert [e["type"] for e in harness.events()] == ["routine_started", "routine_finished"]
    assert ev.validate_event(started[0]) == ()


def test_run_now_records_the_outcome_against_the_request_id(api: ApiFactory) -> None:
    harness = api()
    request_id = harness.client.post("/residents/test-agent/routines/daily-summary/run").json()[
        "request_id"
    ]
    harness.settle()

    logged = harness.store.request(request_id)
    assert logged is not None
    assert logged.outcome == "ran"
    assert logged.path == "/residents/test-agent/routines/daily-summary/run"


def test_a_failed_run_is_logged_as_failed_and_emitted_as_failed(api: ApiFactory) -> None:
    harness = api(
        behavior=lambda _request: RunResult(outcome=Outcome.FAILED, exit_status=2, error="boom")
    )
    request_id = harness.client.post("/residents/test-agent/routines/daily-summary/run").json()[
        "request_id"
    ]
    harness.settle()

    logged = harness.store.request(request_id)
    assert logged is not None
    assert logged.outcome == "failed"
    assert [e["type"] for e in harness.events()] == ["routine_started", "routine_failed"]


def test_run_now_against_an_unknown_resident_is_404(api: ApiFactory) -> None:
    harness = api()
    response = harness.client.post("/residents/nobody/routines/daily-summary/run")
    assert response.status_code == 404
    assert response.json()["detail"]["error"] == "unknown_resident"
    assert harness.store.requests() == []


def test_run_now_against_an_unknown_routine_is_404(api: ApiFactory) -> None:
    harness = api()
    response = harness.client.post("/residents/test-agent/routines/no-such-routine/run")
    assert response.status_code == 404
    detail = response.json()["detail"]
    assert detail["error"] == "unknown_routine"
    assert "daily-summary" in detail["message"]
    assert harness.events() == []


def test_run_now_against_a_disabled_routine_is_409(api: ApiFactory) -> None:
    harness = api(manifest=disabled_routine_manifest())
    response = harness.client.post("/residents/test-agent/routines/daily-summary/run")
    assert response.status_code == 409
    assert response.json()["detail"]["error"] == "routine_disabled"
    assert harness.events() == []


def test_run_now_against_a_broken_manifest_says_so(api: ApiFactory) -> None:
    broken = copy.deepcopy(valid_manifest())
    del broken["memory"]
    harness = api(manifest=broken)
    response = harness.client.post("/residents/test-agent/routines/daily-summary/run")
    assert response.status_code == 409
    assert response.json()["detail"]["error"] == "resident_invalid"


def test_an_overlapping_run_is_refused_rather_than_queued(api: ApiFactory) -> None:
    release = threading.Event()
    entered = threading.Event()

    def block(_request: RunRequest) -> RunResult:
        entered.set()
        release.wait(timeout=10.0)
        return RunResult(outcome=Outcome.OK, exit_status=0)

    harness = api(behavior=block)
    harness.released.append(release)

    first = harness.client.post("/residents/test-agent/routines/daily-summary/run")
    assert first.status_code == 202
    assert entered.wait(timeout=10.0)

    second = harness.client.post("/residents/test-agent/routines/daily-summary/run")
    assert second.status_code == 409
    assert second.json()["detail"]["error"] == "already_running"

    release.set()
    harness.settle()
    assert len(harness.events("routine_started")) == 1

    refused = [r for r in harness.store.requests() if r.outcome.startswith("refused")]
    assert len(refused) == 1


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


# --------------------------------------------------------------------------------------
# approvals
# --------------------------------------------------------------------------------------


def _pending(harness: Harness) -> str:
    record = harness.store.create_approval_request(
        agent_id="claude-code:test-agent",
        project="test-agent",
        action="send_email",
        message="Testy wants to send an email to the plumber",
        detail={"to": "plumber@example.com"},
    )
    return record.request_id


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


def test_a_second_decision_changes_nothing_and_emits_nothing(api: ApiFactory) -> None:
    harness = api()
    request_id = _pending(harness)
    harness.client.post(f"/approvals/{request_id}", json={"decision": "approve"})

    replay = harness.client.post(f"/approvals/{request_id}", json={"decision": "deny"})
    assert replay.status_code == 200
    assert replay.json()["decision"] == "approve"
    assert len(harness.events("needs_human_resolved")) == 1

    record = harness.store.approval(request_id)
    assert record is not None
    assert record.decision == "approve"


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


def _expired_pending(harness: Harness) -> str:
    """Seed a request whose deadline has already passed but the sweep has not run."""
    past = ev.utc_now_iso(dt.datetime.now(dt.UTC) - dt.timedelta(hours=1))
    record = harness.store.create_approval_request(
        agent_id="claude-code:test-agent",
        project="test-agent",
        action="send_email",
        message="Testy wanted to send an email, a while ago",
        resident="test-agent",
        expires_at=past,
    )
    return record.request_id


def test_an_expired_request_is_not_listed_as_pending(api: ApiFactory) -> None:
    """A deadline that has passed denies by default; pending must not offer it to a human (#66)."""
    harness = api()
    request_id = _expired_pending(harness)

    assert harness.client.get("/approvals").json()["approvals"] == []
    assert harness.client.get("/approvals?status=pending").json()["approvals"] == []
    # It is still in the ledger — the audit view sees everything, decided or not.
    listed_all = harness.client.get("/approvals?status=all").json()["approvals"]
    assert [record["request_id"] for record in listed_all] == [request_id]


def test_an_expired_request_cannot_be_decided(api: ApiFactory) -> None:
    """A click past the deadline must not slip an action through ahead of the sweep (#66)."""
    harness = api()
    request_id = _expired_pending(harness)

    response = harness.client.post(f"/approvals/{request_id}", json={"decision": "approve"})
    assert response.status_code == 409
    assert response.json()["detail"]["error"] == "approval_expired"
    # Nothing was recorded and nothing emitted: it is still pending, for the sweep to deny.
    record = harness.store.approval(request_id)
    assert record is not None
    assert record.pending
    assert harness.events("needs_human_resolved") == []


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


# --------------------------------------------------------------------------------------
# residents
# --------------------------------------------------------------------------------------


def test_residents_are_listed_with_the_brain_they_run_on(api: ApiFactory) -> None:
    harness = api()
    body = harness.client.get("/residents").json()

    assert [resident["id"] for resident in body["residents"]] == ["test-agent"]
    assert body["residents"][0]["runner"] == {"kind": "claude", "model": "claude-opus-5"}
    assert body["errors"] == []


def test_a_broken_manifest_is_named_rather_than_hidden(api: ApiFactory) -> None:
    broken = copy.deepcopy(valid_manifest())
    del broken["memory"]
    harness = api(manifest=broken)
    body = harness.client.get("/residents").json()

    assert body["residents"] == []
    assert "memory" in body["errors"][0]


def test_one_resident_is_served_whole(api: ApiFactory) -> None:
    harness = api()
    body = harness.client.get("/residents/test-agent").json()

    assert body["agent_id"] == "claude-code:test-agent"
    assert body["soul"]["name"] == "Testy"
    assert body["charter"]["mission"]
    assert [routine["id"] for routine in body["routines"]] == ["daily-summary"]
    assert body["memory"]["path"] == "/data/residents/test-agent/memory"


def test_an_unknown_resident_is_404(api: ApiFactory) -> None:
    harness = api()
    assert harness.client.get("/residents/nobody").status_code == 404


def test_creating_a_resident_writes_a_tree_the_validator_accepts(api: ApiFactory) -> None:
    harness = api()
    response = harness.client.post("/residents", json=NEW_RESIDENT)

    assert response.status_code == 201
    body = response.json()
    assert body["status"] == "accepted"
    assert body["request_id"]
    assert Path(body["manifest_path"]).is_file()
    assert Path(body["soul_path"]).is_file()

    result = validate_tree(harness.residents_dir)
    assert result.ok, [d.render() for d in result.errors]
    assert "note-keeper" in {resident.id for resident in result.residents}

    # It exists as a declaration and nothing else: no deploy, no schedule, no event.
    assert harness.events() == []
    listed = harness.client.get("/residents").json()["residents"]
    assert {resident["id"] for resident in listed} == {"test-agent", "note-keeper"}


def test_creating_the_same_resident_twice_converges(api: ApiFactory) -> None:
    """The same body twice is one resident, not an error and not a second write."""
    harness = api()
    harness.client.post("/residents", json=NEW_RESIDENT)
    response = harness.client.post("/residents", json=NEW_RESIDENT)

    assert response.status_code == 201
    assert response.json()["declare"]["written"] is False
    assert response.json()["changed"] is False


def test_creating_a_resident_that_exists_differently_is_409(api: ApiFactory) -> None:
    """A collision is a *different* declaration under a name somebody already used."""
    harness = api()
    harness.client.post("/residents", json=NEW_RESIDENT)
    response = harness.client.post("/residents", json=NEW_RESIDENT | {"role": "something else"})

    assert response.status_code == 409
    detail = response.json()["detail"]
    assert detail["error"] == "resident_not_declared"
    assert "soul" in detail["message"]


def test_a_resident_that_cannot_be_declared_is_400(api: ApiFactory) -> None:
    harness = api()
    response = harness.client.post("/residents", json=NEW_RESIDENT | {"agent_id": "no-colon"})

    assert response.status_code == 400
    assert not (harness.residents_dir / "note-keeper").exists()


def test_a_credential_shaped_field_never_reaches_a_manifest(api: ApiFactory) -> None:
    harness = api()
    response = harness.client.post("/residents", json=NEW_RESIDENT | {"api_key": "sk-not-today"})

    assert response.status_code == 422
    assert not (harness.residents_dir / "note-keeper").exists()


# --------------------------------------------------------------------------------------
# CORS, and the language of the answers
# --------------------------------------------------------------------------------------


def test_cors_headers_appear_only_for_a_configured_origin(api: ApiFactory) -> None:
    village = "http://village.local:8080"
    harness = api(cors_origins=(village,))

    allowed = harness.client.get("/residents", headers={"Origin": village})
    assert allowed.headers["access-control-allow-origin"] == village

    other = harness.client.get("/residents", headers={"Origin": "https://evil.example"})
    assert "access-control-allow-origin" not in other.headers


def test_a_preflight_from_the_village_is_answered_without_a_token(api: ApiFactory) -> None:
    """A browser cannot put the bearer token on a preflight, so the gate cannot see it."""
    village = "http://village.local:8080"
    harness = api(cors_origins=(village,))
    anonymous = TestClient(harness.client.app)

    response = anonymous.options(
        "/residents/test-agent/routines/daily-summary/run",
        headers={
            "Origin": village,
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "authorization",
        },
    )
    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == village
    assert harness.events() == []


def test_without_configured_origins_no_browser_is_invited(api: ApiFactory) -> None:
    harness = api()
    response = harness.client.get("/residents", headers={"Origin": "http://village.local"})
    assert "access-control-allow-origin" not in response.headers


def test_no_endpoint_claims_the_work_is_done(api: ApiFactory) -> None:
    """Acknowledgement, not effect: the API may say accepted, queued, recorded."""
    harness = api()
    request_id = _pending(harness)
    bodies = [
        harness.client.post("/residents/test-agent/routines/daily-summary/run").json(),
        harness.client.post("/jobs", json={"title": "Research X"}).json(),
        harness.client.post(f"/approvals/{request_id}", json={"decision": "deny"}).json(),
        harness.client.post("/residents", json=NEW_RESIDENT).json(),
    ]
    harness.settle()

    for body in bodies:
        assert body["status"] in {"accepted", "queued", "recorded"}
        assert not FORBIDDEN.search(body["message"]), body["message"]


def test_the_schema_is_not_served_unauthenticated(api: ApiFactory) -> None:
    harness = api()
    assert harness.client.get("/openapi.json").status_code == 404
    assert harness.client.get("/docs").status_code == 404


def test_config_reads_the_environment(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("STEWARD_TOKEN", "from-env")
    monkeypatch.setenv("STEWARD_CORS_ORIGINS", "http://a.local, http://b.local ,")
    monkeypatch.setenv("STEWARD_RESIDENTS", str(tmp_path / "elsewhere"))

    config = ApiConfig.from_env()
    assert config.token == "from-env"
    assert config.cors_origins == ("http://a.local", "http://b.local")
    assert config.residents_dir == tmp_path / "elsewhere"


def test_config_defaults_to_the_residents_tree(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("STEWARD_RESIDENTS", raising=False)
    monkeypatch.delenv("STEWARD_CORS_ORIGINS", raising=False)
    config = ApiConfig.from_env({})
    assert config.residents_dir == Path("residents")
    assert config.cors_origins == ()


def test_a_manual_run_uses_the_declared_runner_and_prompt(api: ApiFactory) -> None:
    seen: list[RunRequest] = []

    def record(request: RunRequest) -> RunResult:
        seen.append(request)
        return RunResult(outcome=Outcome.OK, exit_status=0)

    harness = api(behavior=record)
    harness.client.post("/residents/test-agent/routines/daily-summary/run")
    harness.settle()

    assert len(seen) == 1
    assert "Write the summary." in seen[0].prompt
    assert seen[0].model == "claude-opus-5"
    assert seen[0].timeout_s == 900
    assert seen[0].env["STEWARD_ROUTINE"] == "daily-summary"


def test_a_manual_run_that_blows_up_is_a_logged_failure_not_a_500(
    api: ApiFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness = api()
    runs = harness.client.app.state.runs

    def boom(_item: object, *, trigger: str) -> None:
        raise RuntimeError(f"no brain for a {trigger} run")

    monkeypatch.setattr(runs.scheduler, "fire", boom)
    request_id = harness.client.post("/residents/test-agent/routines/daily-summary/run").json()[
        "request_id"
    ]
    harness.settle()

    logged = harness.store.request(request_id)
    assert logged is not None
    assert logged.outcome == "failed"
    assert "RuntimeError" in logged.detail["error"]


def test_a_skipped_fire_is_logged_as_skipped(
    api: ApiFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness = api()
    runs = harness.client.app.state.runs

    def skip(item: ScheduledRoutine, *, trigger: str) -> FireReport:
        assert trigger == "manual"
        return FireReport(scheduled=item, run_id="r", fired=False, skipped_reason="already running")

    monkeypatch.setattr(runs.scheduler, "fire", skip)
    request_id = harness.client.post("/residents/test-agent/routines/daily-summary/run").json()[
        "request_id"
    ]
    harness.settle()

    logged = harness.store.request(request_id)
    assert logged is not None
    assert logged.outcome == "skipped: already running"


def test_the_runner_seam_is_what_the_manifest_declares() -> None:
    """The API builds runners through the same factory the scheduler does."""
    assert MockRunner(RunnerSpec(kind="mock", model="m")).describe() == "mock (m)"


def test_the_journal_endpoint_reads_what_the_resident_wrote(
    api: ApiFactory, tmp_path: Path
) -> None:
    """GET /residents/{id}/journal returns real entries, newest first."""
    manifest = copy.deepcopy(valid_manifest())
    manifest["memory"]["path"] = str(tmp_path / "memory")
    harness = api(manifest=manifest)
    resident = validate_tree(harness.residents_dir).residents[0]
    journal.write_entry(resident.manifest, dt.date(2026, 8, 23), "close-of-day", "A quiet day.")
    journal.write_entry(resident.manifest, dt.date(2026, 8, 24), "close-of-day", "A loud day.")

    body = harness.client.get("/residents/test-agent/journal").json()

    assert body["resident"] == "test-agent"
    assert [entry["text"].strip() for entry in body["entries"]] == ["A loud day.", "A quiet day."]
    limited = harness.client.get("/residents/test-agent/journal", params={"limit": 1}).json()
    assert len(limited["entries"]) == 1


def test_an_empty_journal_is_an_empty_list_and_unknown_residents_are_404(
    api: ApiFactory, tmp_path: Path
) -> None:
    """A resident that never wrote renders as one that never wrote; a stranger is a 404."""
    manifest = copy.deepcopy(valid_manifest())
    manifest["memory"]["path"] = str(tmp_path / "memory")
    harness = api(manifest=manifest)

    assert harness.client.get("/residents/test-agent/journal").json()["entries"] == []
    assert harness.client.get("/residents/nobody/journal").status_code == 404


# --------------------------------------------------------------------------------------
# skills
# --------------------------------------------------------------------------------------


def granting(*names: str) -> dict[str, Any]:
    data = copy.deepcopy(valid_manifest())
    data["skills"] = list(names)
    data["routines"][0]["requires"] = ["daily-summary"]
    return data


def test_the_library_is_listed_with_who_holds_each_skill(
    api: ApiFactory, write_skill: SkillWriter
) -> None:
    write_skill("daily-summary", defaults=True)
    write_skill("write-journal", defaults=True)
    write_skill("read-inbox", description="Triage the mail.")
    write_skill("write-blog-post")
    harness = api(manifest=granting("read-inbox"))

    body = harness.client.get("/skills").json()
    by_name = {skill["name"]: skill for skill in body["skills"]}

    assert sorted(by_name) == ["daily-summary", "read-inbox", "write-blog-post", "write-journal"]
    assert by_name["daily-summary"]["default"] is True
    assert by_name["daily-summary"]["holders"] == ["test-agent"]
    assert by_name["read-inbox"]["holders"] == ["test-agent"]
    assert by_name["read-inbox"]["description"] == "Triage the mail."
    assert by_name["write-blog-post"]["holders"] == [], "a skill nobody holds says so"
    assert body["errors"] == []
    assert body["library"].endswith("skills")


def test_a_broken_skill_is_named_in_the_listing(api: ApiFactory, write_skill: SkillWriter) -> None:
    write_skill("daily-summary", defaults=True)
    write_skill("write-journal", defaults=True)
    write_skill("broken", text="---\nname: broken\n---\n\nNo description.\n")
    harness = api(manifest=granting())

    body = harness.client.get("/skills").json()
    assert [skill["name"] for skill in body["skills"]] == ["daily-summary", "write-journal"]
    assert "description" in body["errors"][0]


def test_with_no_library_the_listing_is_empty_rather_than_missing(api: ApiFactory) -> None:
    harness = api()
    body = harness.client.get("/skills").json()
    assert body == {"library": None, "skills": [], "errors": []}


def test_a_resident_carries_the_set_a_session_would_actually_get(
    api: ApiFactory, write_skill: SkillWriter
) -> None:
    write_skill("daily-summary", defaults=True)
    write_skill("write-journal", defaults=True)
    write_skill("read-inbox")
    harness = api(manifest=granting("read-inbox"))

    body = harness.client.get("/residents/test-agent").json()
    assert body["effective_skills"] == ["daily-summary", "write-journal", "read-inbox"]
    assert [grant["id"] for grant in body["skills"]] == ["read-inbox"]

    listed = harness.client.get("/residents").json()["residents"][0]
    assert listed["effective_skills"] == body["effective_skills"]


def test_the_skills_listing_needs_the_token_like_everything_else(api: ApiFactory) -> None:
    harness = api()
    anonymous = TestClient(harness.client.app)
    assert anonymous.get("/skills").status_code == 401


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
    assert harness.store.requests() == []
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
    assert entries[0].ref == "daily-summary"
    assert entries[0].cost_usd == pytest.approx(0.5)
    assert entries[0].tokens == 10


# --------------------------------------------------------------------------------------
# delegation
# --------------------------------------------------------------------------------------

RECEIVER_SOUL = """---
agent_id: claude-code:receiver-agent
name: Recy
char: Monk
accent: "#a68a4f"
role: test bot
---
A villager that exists only inside a test.
"""


def receiver_manifest(*, status: str = "active") -> dict[str, Any]:
    """Build a resident that accepts delegated work through one named route."""
    data = copy.deepcopy(valid_manifest())
    data["id"] = "receiver-agent"
    data["agent_id"] = "claude-code:receiver-agent"
    data["soul"]["name"] = "Recy"
    data["routes"] = [
        *data["routes"],
        {"id": "inbox", "kind": "delegation", "address": "steward:delegation", "status": status},
    ]
    return data


def sending_manifest() -> dict[str, Any]:
    """Build the test resident, permitted to hand work to the receiver."""
    data = copy.deepcopy(valid_manifest())
    data["delegation"] = {"send": True, "to": ["receiver-agent"]}
    return data


def with_receiver(
    api: ApiFactory, write_resident: ResidentWriter, *, status: str = "active"
) -> Harness:
    """Build an app whose tree holds a permitted sender and a declared receiver."""
    harness = api(manifest=sending_manifest())
    write_resident(receiver_manifest(status=status), soul=RECEIVER_SOUL, root=harness.residents_dir)
    return harness


HANDOFF = {"to": "receiver-agent", "route": "inbox", "title": "Read the background"}


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


def test_delegation_needs_the_token_like_everything_else(api: ApiFactory) -> None:
    harness = api()
    anonymous = TestClient(harness.client.app)
    assert anonymous.post("/delegate", json=HANDOFF).status_code == 401
    assert anonymous.get("/residents/test-agent/inbox").status_code == 401
    assert harness.store.jobs() == []


# --------------------------------------------------------------------------------------
# POST /residents with deploy: true — the same pipeline the CLI runs
# --------------------------------------------------------------------------------------


@pytest.fixture
def village(monkeypatch: pytest.MonkeyPatch) -> str:
    """Give the API's environment a village to point new containers at."""
    monkeypatch.setenv("BURROW_URL", "http://dxp2800:8737")
    monkeypatch.setenv("BURROW_TOKEN", "api-village-token")
    return "api-village-token"


@pytest.mark.usefixtures("village")
def test_declaring_without_the_flag_still_deploys_nothing(
    api: ApiFactory,
    tmp_path: Path,
    village: str,  # noqa: ARG001 — the fixture is the setup
) -> None:
    """The default is the endpoint's old behaviour, exactly."""
    host = LocalTransport(root=tmp_path / "nas")
    harness = api(transport=host)

    response = harness.client.post("/residents", json=NEW_RESIDENT)

    assert response.status_code == 201
    assert response.json()["provision"] is None
    assert not host.touched
    assert "nothing is deployed" in response.json()["message"]


@pytest.mark.usefixtures("village")
def test_deploy_true_runs_the_whole_pipeline(api: ApiFactory, tmp_path: Path) -> None:
    host = LocalTransport(root=tmp_path / "nas")
    harness = api(transport=host)

    response = harness.client.post("/residents", json=NEW_RESIDENT | {"deploy": True})

    assert response.status_code == 201
    body = response.json()
    assert body["provision"]["target"]["container"] == "steward-note-keeper"
    assert body["provision"]["sent"] is True
    assert body["register"]["ok"] is True
    assert (host.root / "docker" / "steward-note-keeper" / "docker-compose.yaml").is_file()
    assert host.read("~/docker/steward-note-keeper/.env") is not None


@pytest.mark.usefixtures("village")
def test_the_api_calls_the_same_pipeline_the_cli_does(
    api: ApiFactory,
    tmp_path: Path,
    village: str,  # noqa: ARG001 — the fixture is the setup
) -> None:
    """Verified by injection, not by convention: the route is handed the pipeline."""
    seen: list[dict[str, Any]] = []

    def recorder(spec: Any, **kwargs: Any) -> Any:  # noqa: ANN401 — a recorder takes anything
        seen.append({"spec": spec, **kwargs})
        return raise_resident(spec, **kwargs)

    host = LocalTransport(root=tmp_path / "nas")
    harness = api(nursery=recorder, transport=host)

    harness.client.post("/residents", json=NEW_RESIDENT | {"deploy": True})

    assert len(seen) == 1
    assert seen[0]["spec"].id == "note-keeper"
    assert seen[0]["provision"] is True
    assert seen[0]["transport"] is host
    # And the one setting the API always makes for itself, whatever the body says.
    assert seen[0]["commit"] is False


@pytest.mark.usefixtures("village")
def test_the_api_never_commits_and_says_so(api: ApiFactory, tmp_path: Path) -> None:
    """The server may not own this checkout, so a commit here would surprise somebody."""
    host = LocalTransport(root=tmp_path / "nas")
    harness = api(transport=host)

    response = harness.client.post("/residents", json=NEW_RESIDENT | {"deploy": True})

    assert response.json()["declare"]["commit"] is None
    assert "NOT committed" in response.json()["message"]


def test_a_deploy_leaks_no_secret_into_the_response(
    api: ApiFactory, tmp_path: Path, village: str
) -> None:
    harness = api(transport=LocalTransport(root=tmp_path / "nas"))

    response = harness.client.post("/residents", json=NEW_RESIDENT | {"deploy": True})

    assert village not in response.text
    assert response.json()["provision"]["env_keys"] == ["BURROW_TOKEN", "BURROW_URL"]


def test_a_retired_resident_is_listed_and_refuses_a_run_now(api: ApiFactory) -> None:
    """Listed, because a fleet view that hid it could not say what used to run here."""
    manifest = copy.deepcopy(valid_manifest())
    manifest["retired"] = True
    harness = api(manifest=manifest)

    listing = harness.client.get("/residents").json()["residents"]
    assert [resident["id"] for resident in listing] == ["test-agent"]
    assert listing[0]["retired"] is True

    response = harness.client.post("/residents/test-agent/routines/daily-summary/run")
    assert response.status_code == 409
    assert response.json()["detail"]["error"] == "resident_retired"


def test_a_retired_resident_takes_no_letters(api: ApiFactory) -> None:
    manifest = copy.deepcopy(valid_manifest())
    manifest["retired"] = True
    manifest["routes"].append(
        {"id": "handoff", "kind": "delegation", "address": "steward:delegation"}
    )
    harness = api(manifest=manifest)

    response = harness.client.post(
        "/delegate", json={"to": "test-agent", "route": "handoff", "title": "Something"}
    )

    assert response.status_code == 404
    # A retired recipient is still a 404 from the sender's side, but it now carries its own
    # reason code so a panel can tell a resident that left the village from a typo (#W21).
    assert response.json()["detail"]["error"] == "retired_recipient"
    assert "is retired" in response.json()["detail"]["message"]
