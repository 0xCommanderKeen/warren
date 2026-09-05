"""The endpoints steward grew for a control panel, which outlive the one that asked.

Three of these exist because the retired ``/ui`` console genuinely could not answer three
questions without them, and every one of those questions is still asked — now by townhall
(warren#225):

*The fleet-wide routine ledger.* ``GET /routines``: what standing work every valid resident
declares, when it fires next, and — this is the part a declaration cannot tell you —
whether any scheduler is alive to fire it.

*The request log.* ``GET /requests`` and ``GET /requests/{id}``: the only honest way an
``accepted`` ever becomes a ``ran``. Every mutating route here answers with a request id and
refuses to claim an effect; this is where the effect turns up.

*What the resident view carries.* The voice a soul declares and the delegation flags a
manifest sets, so a panel can show who a resident is without re-deriving it.

*The nursery, over HTTP.* ``POST /residents`` declares — and, when asked, provisions — and
reports its own stages rather than leaving a caller to describe what a deploy usually does.

This file was ``test_ui.py``. What it lost with the console was the half that asserted the
shell was served unauthenticated and that ``ui/app.js``'s ``ROUTES`` map named real routes.
Both were tests about a client, and that client is gone; these are tests about the API, and
the API is not.
"""

import copy
import json
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Unpack

import pytest
from fastapi.testclient import TestClient
from jsonschema import Draft202012Validator

from conftest import REPO_ROOT, ResidentWriter, valid_manifest
from steward.deploy import DeployTarget
from steward.nursery import (
    DeclareStage,
    NewResident,
    NurseryReport,
    ProvisionStage,
    RegisterStage,
)
from steward.routes.deps import (
    NurseryOptions,
    NurseryPipeline,
)
from steward.scheduler import STALE_TICK_AFTER_S, SchedulerState, load_scheduled
from steward.session_auth import new_session_credential
from steward.store import RequestRecord
from support.panel import PanelFactory, anonymous
from support.panel import panel as panel  # noqa: PLC0414 — pytest fixture discovery

# --------------------------------------------------------------------------------------
# the fleet-wide routine ledger
# --------------------------------------------------------------------------------------


def test_the_ledger_carries_every_routine_of_every_resident(panel: PanelFactory) -> None:
    body = panel().client.get("/routines").json()
    assert [row["key"] for row in body["routines"]] == ["test-agent/daily-summary"]
    row = body["routines"][0]
    assert row["resident_name"] == "Testy"
    assert row["accent"] == "#a68a4f"
    assert row["schedule"] == "0 7 * * *"
    assert row["schedule_tz"] == "UTC"
    assert row["enabled"] is True


def test_an_unfired_routine_reports_no_anchor_rather_than_a_last_run(
    panel: PanelFactory,
) -> None:
    # The distinction the field name exists for: never fired is not the same as fired
    # long ago, and a ledger that blurred the two would let a dead routine look alive.
    row = panel().client.get("/routines").json()["routines"][0]
    assert row["anchor"] is None
    assert row["last_request"] is None


def test_an_enabled_routine_promises_a_next_fire_and_a_disabled_one_does_not(
    panel: PanelFactory,
) -> None:
    data = copy.deepcopy(valid_manifest())
    data["routines"][0]["enabled"] = False
    rows = panel(manifest=data).client.get("/routines").json()["routines"]
    assert rows[0]["enabled"] is False
    assert rows[0]["next_fire"] is None

    live = panel().client.get("/routines").json()["routines"][0]
    assert live["next_fire"] is not None
    assert live["next_fire"] > "2020"


def test_the_ledger_carries_what_became_of_the_last_asked_for_run(
    panel: PanelFactory,
) -> None:
    built = panel()
    accepted = built.client.post("/residents/test-agent/routines/daily-summary/run")
    assert accepted.status_code == 202
    built.client.app.state.runs.wait(timeout=10.0)

    row = built.client.get("/routines").json()["routines"][0]
    assert row["last_request"]["request_id"] == accepted.json()["request_id"]
    assert row["last_request"]["outcome"] == "ran"


def test_a_fleet_nothing_has_ever_ticked_says_so_rather_than_dead(
    panel: PanelFactory, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The reason `alive` is a tri-state: a fresh install where no daemon was ever started
    # and a daemon that died an hour ago are different problems with different fixes.
    monkeypatch.setenv("STEWARD_STATE", str(tmp_path / "never.json"))
    scheduler = panel().client.get("/routines").json()["scheduler"]

    assert scheduler["alive"] is None
    assert scheduler["last_tick"] is None
    assert scheduler["stale_after_s"] == STALE_TICK_AFTER_S


def test_the_ledger_reads_back_whether_a_scheduler_is_still_ticking(
    panel: PanelFactory, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state_path = tmp_path / "heartbeat.json"
    monkeypatch.setenv("STEWARD_STATE", str(state_path))
    built = panel()
    state = SchedulerState(path=state_path)

    state.record_tick(datetime.now(UTC))
    state.save()
    live = built.client.get("/routines").json()["scheduler"]
    assert live["alive"] is True
    assert live["last_tick"] is not None

    # The daemon stopped. The next fires below are still promised and nothing keeps them.
    state.record_tick(datetime.now(UTC) - timedelta(seconds=STALE_TICK_AFTER_S + 1))
    state.save()
    assert built.client.get("/routines").json()["scheduler"]["alive"] is False


def test_the_ledger_names_manifests_that_did_not_validate(panel: PanelFactory) -> None:
    built = panel()
    broken = built.residents_dir / "broken"
    broken.mkdir()
    (broken / "manifest.yaml").write_text("id: broken\n", encoding="utf-8")
    body = built.client.get("/routines").json()
    assert body["errors"], "a broken manifest must be named, never quietly omitted"


def test_an_empty_tree_is_an_empty_ledger(panel: PanelFactory) -> None:
    body = panel(residents=False).client.get("/routines").json()
    assert body["routines"] == []
    assert body["state_path"]


def test_the_ledger_needs_the_token(panel: PanelFactory) -> None:
    assert anonymous(panel()).get("/routines").status_code == 401


def test_a_retired_residents_routines_are_listed_but_never_firable(
    panel: PanelFactory,
) -> None:
    """The cross-check between the nursery and the panel that renders it.

    ``load_scheduled`` leaves retired residents out, so the scheduler will never fire
    these — and a ledger that promised a ``next_fire`` for one would be promising
    something no process anywhere intends to do. They stay *listed*, because "what used
    to run here" is a question this view exists to answer.
    """
    data = copy.deepcopy(valid_manifest())
    data["retired"] = True
    built = panel(manifest=data)

    rows = built.client.get("/routines").json()["routines"]
    assert [row["key"] for row in rows] == ["test-agent/daily-summary"]
    assert rows[0]["retired"] is True
    assert rows[0]["enabled"] is True, "the routine's own switch is untouched by retirement"
    assert rows[0]["next_fire"] is None

    # And the refusal a panel greys the button out with is the real one.
    refused = built.client.post("/residents/test-agent/routines/daily-summary/run")
    assert refused.status_code == 409
    assert refused.json()["detail"]["error"] == "resident_retired"


def test_a_living_residents_routines_say_they_are_not_retired(panel: PanelFactory) -> None:
    # The other half: the flag is on every row, so a panel never has to infer absence.
    row = panel().client.get("/routines").json()["routines"][0]
    assert row["retired"] is False
    assert row["next_fire"] is not None


def test_the_scheduler_and_the_ledger_agree_about_a_retired_resident(
    panel: PanelFactory,
) -> None:
    # One fact, two readers: whatever load_scheduled would hand the scheduler is exactly
    # what the ledger promises a next fire for.
    data = copy.deepcopy(valid_manifest())
    data["retired"] = True
    built = panel(manifest=data)

    scheduled = {item.key for item in load_scheduled(built.residents_dir)}
    promised = {
        row["key"] for row in built.client.get("/routines").json()["routines"] if row["next_fire"]
    }
    assert scheduled == set()
    assert promised == scheduled


# --------------------------------------------------------------------------------------
# the request log: how a 202 is ever confirmed
# --------------------------------------------------------------------------------------


def test_the_request_log_starts_empty(panel: PanelFactory) -> None:
    assert panel().client.get("/requests").json() == {"requests": []}


def test_an_accepted_request_can_be_read_back_by_its_id(panel: PanelFactory) -> None:
    built = panel()
    accepted = built.client.post("/jobs", json={"title": "Research X"}).json()
    record = built.client.get(f"/requests/{accepted['request_id']}").json()
    assert record["path"] == "/jobs"
    assert record["method"] == "POST"
    assert record["outcome"] == "posted"


def test_a_queued_run_becomes_ran_in_the_log_and_not_before(panel: PanelFactory) -> None:
    # The whole promise a control panel rests on: 202 says queued, and only this log ever
    # says it happened.
    built = panel()
    accepted = built.client.post("/residents/test-agent/routines/daily-summary/run").json()
    built.client.app.state.runs.wait(timeout=10.0)
    record = built.client.get(f"/requests/{accepted['request_id']}").json()
    assert record["outcome"] == "ran"
    assert record["detail"]["routine"] == "test-agent/daily-summary"


def test_the_log_is_newest_first(panel: PanelFactory) -> None:
    built = panel()
    first = built.client.post("/jobs", json={"title": "One"}).json()["request_id"]
    second = built.client.post("/jobs", json={"title": "Two"}).json()["request_id"]
    ids = [row["request_id"] for row in built.client.get("/requests").json()["requests"]]
    assert ids == [second, first]


def test_the_log_window_is_clamped(panel: PanelFactory) -> None:
    built = panel()
    for index in range(4):
        built.client.post("/jobs", json={"title": f"Task {index}"})
    assert len(built.client.get("/requests", params={"limit": 2}).json()["requests"]) == 2
    assert len(built.client.get("/requests", params={"limit": 0}).json()["requests"]) == 1
    assert len(built.client.get("/requests", params={"limit": 9999}).json()["requests"]) == 4


def test_an_unknown_request_id_is_a_404_that_explains_itself(panel: PanelFactory) -> None:
    response = panel().client.get("/requests/nope")
    assert response.status_code == 404
    assert response.json()["detail"]["error"] == "unknown_request"
    assert "refused" in response.json()["detail"]["message"]


def test_a_refused_request_is_never_logged(panel: PanelFactory) -> None:
    built = panel()
    assert built.client.post("/residents/test-agent/routines/nope/run").status_code == 404
    assert built.client.get("/requests").json()["requests"] == []


def test_the_request_log_needs_the_token(panel: PanelFactory) -> None:
    assert anonymous(panel()).get("/requests").status_code == 401


def test_a_resident_carries_its_voice(panel: PanelFactory) -> None:
    body = panel().client.get("/residents/test-agent").json()
    assert body["voice"] == "Flat, factual, short."


def test_a_soul_with_no_voice_says_so_rather_than_omitting_it(
    panel: PanelFactory, write_resident: ResidentWriter
) -> None:
    built = panel()
    write_resident(
        {**valid_manifest(), "id": "voiceless", "agent_id": "claude-code:voiceless"},
        root=built.residents_dir,
        soul=(
            "---\nagent_id: claude-code:voiceless\nname: Testy\nchar: Monk\n"
            'accent: "#a68a4f"\nrole: test bot\n---\nNo voice section at all.\n'
        ),
    )
    body = built.client.get("/residents/voiceless").json()
    assert body["voice"] is None


def test_a_resident_carries_its_delegation_flags(panel: PanelFactory) -> None:
    data = copy.deepcopy(valid_manifest())
    data["delegation"] = {"send": True, "to": ["hob"]}
    body = panel(manifest=data).client.get("/residents/test-agent").json()
    assert body["delegation"] == {"send": True, "to": ["hob"], "note": None}


# --------------------------------------------------------------------------------------
# the conversation window
# --------------------------------------------------------------------------------------


def test_conversations_list_and_read_the_residents_remembered_window(
    panel: PanelFactory, tmp_path: Path
) -> None:
    memory = tmp_path / "test-agent-memory"
    data = copy.deepcopy(valid_manifest())
    data["memory"]["path"] = str(memory)
    data["routes"].append(
        {"id": "chat", "kind": "chat", "address": "telegram:testy", "status": "active"}
    )
    built = panel(manifest=data)
    chat = memory / "chat"
    chat.mkdir(parents=True)
    (chat / "4242.jsonl").write_text(
        '{"at":"2026-09-04T08:00:00Z","speaker":"operator","text":"hello"}\n'
        '{"at":"2026-09-04T08:00:02Z","speaker":"hob","text":"receipt 521f54b"}\n',
        encoding="utf-8",
    )

    listed = built.client.get("/residents/test-agent/conversations")
    assert listed.status_code == 200
    assert listed.json() == {
        "resident": "test-agent",
        "conversations": [{"id": "4242", "last_turn_at": "2026-09-04T08:00:02Z", "turn_count": 2}],
    }

    read = built.client.get("/residents/test-agent/conversations/4242")
    assert read.status_code == 200
    assert read.json() == {
        "resident": "test-agent",
        "conversation": "4242",
        "turns": [
            {"at": "2026-09-04T08:00:00Z", "speaker": "operator", "text": "hello"},
            {"at": "2026-09-04T08:00:02Z", "speaker": "hob", "text": "receipt 521f54b"},
        ],
    }


def test_conversation_reads_scrub_secrets_and_stay_behind_the_operator_token(
    panel: PanelFactory, tmp_path: Path
) -> None:
    memory = tmp_path / "test-agent-memory"
    data = copy.deepcopy(valid_manifest())
    data["memory"]["path"] = str(memory)
    data["routes"].append(
        {"id": "chat", "kind": "chat", "address": "telegram:testy", "status": "active"}
    )
    built = panel(manifest=data)
    chat = memory / "chat"
    chat.mkdir(parents=True)
    (chat / "4242.jsonl").write_text(
        '{"at":"2026-09-04T08:00:00Z","speaker":"operator",'
        '"text":"token 123456789:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdef"}\n',
        encoding="utf-8",
    )

    anonymous = TestClient(built.client.app)
    assert anonymous.get("/residents/test-agent/conversations").status_code == 401
    body = built.client.get("/residents/test-agent/conversations/4242").json()
    assert body["turns"][0]["text"] == "token [redacted:secret]"


def test_a_resident_without_a_chat_route_lists_no_stale_conversation(
    panel: PanelFactory, tmp_path: Path
) -> None:
    memory = tmp_path / "test-agent-memory"
    data = copy.deepcopy(valid_manifest())
    data["memory"]["path"] = str(memory)
    built = panel(manifest=data)
    chat = memory / "chat"
    chat.mkdir(parents=True)
    (chat / "4242.jsonl").write_text(
        '{"at":"2026-09-04T08:00:00Z","speaker":"operator","text":"stale"}\n',
        encoding="utf-8",
    )

    assert built.client.get("/residents/test-agent/conversations").json() == {
        "resident": "test-agent",
        "conversations": [],
    }


# --------------------------------------------------------------------------------------
# the deploy path, as the pending ledger sees it
#
# A panel renders a deploy from two things and invents neither: the 201 body's own
# report, and the request log it then polls. Both are checked here.
# --------------------------------------------------------------------------------------

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


def canned_pipeline(*, problems: tuple[str, ...] = ()) -> NurseryPipeline:
    """Return a nursery that reaches no host, so a UI test can look at the answer's shape."""

    def pipeline(spec: NewResident, **kwargs: Unpack[NurseryOptions]) -> NurseryReport:
        directory = Path("/srv/steward/residents") / spec.id
        target = DeployTarget(
            resident_id=spec.id,
            host="dxp2800",
            user="Miha",
            path=f"~/docker/warren/residents/{spec.id}",
            container=f"steward-{spec.id}",
            image="steward-resident:latest",
            command=("sleep", "infinity"),
        )
        return NurseryReport(
            resident_id=spec.id,
            declare=DeclareStage(
                resident_id=spec.id,
                manifest_path=directory / "manifest.yaml",
                soul_path=directory / "soul.md",
                written=True,
                note="declared and validated",
            ),
            # Honoured rather than ignored: a stub that provisioned whatever it was asked
            # would let a declare-only request come back describing a container.
            provision=ProvisionStage(
                target=target,
                files=("docker-compose.yaml", ".env"),
                compose="services:\n  note-keeper: {}\n",
                compose_changed=True,
                env_keys=("CHRONICLE_TOKEN", "CHRONICLE_URL"),
                commands=(("ssh", "Miha@dxp2800", "docker compose up -d"),),
                sent=True,
            )
            if kwargs.get("provision")
            else None,
            register=RegisterStage(
                problems=problems,
                fires=() if problems else (("tidy-notes", "2026-08-26T20:00:00+02:00"),),
            ),
        )

    return pipeline


def test_a_deploy_leaves_a_request_the_ledger_can_poll(panel: PanelFactory) -> None:
    built = panel(nursery=canned_pipeline())

    accepted = built.client.post("/residents", json=NEW_RESIDENT | {"deploy": True}).json()
    record = built.client.get(f"/requests/{accepted['request_id']}").json()

    assert record["path"] == "/residents"
    assert record["outcome"] == "deployed"
    assert record["detail"] == {"resident": "note-keeper"}


def test_declaring_without_deploying_says_declared_in_the_same_log(
    panel: PanelFactory,
) -> None:
    # The two paths are told apart in the log, so a person reading it afterwards can see
    # which requests reached a machine and which only wrote a file.
    built = panel(nursery=canned_pipeline())

    accepted = built.client.post("/residents", json=NEW_RESIDENT).json()

    assert built.client.get(f"/requests/{accepted['request_id']}").json()["outcome"] == "declared"


def test_the_answer_carries_the_whole_report_for_the_panel_to_print(
    panel: PanelFactory,
) -> None:
    body = (
        panel(nursery=canned_pipeline())
        .client.post("/residents", json=NEW_RESIDENT | {"deploy": True})
        .json()
    )

    # Everything ui/app.js's declared() panel reads, in one place, so a rename on either
    # side fails here rather than rendering a column of "undefined".
    assert body["provision"]["target"]["container"] == "steward-note-keeper"
    assert body["provision"]["commands"] == ["ssh Miha@dxp2800 docker compose up -d"]
    assert body["provision"]["env_keys"] == ["CHRONICLE_TOKEN", "CHRONICLE_URL"]
    assert body["provision"]["compose"].startswith("services:")
    assert body["register"]["next_fires"] == [
        {"routine": "tidy-notes", "at": "2026-08-26T20:00:00+02:00"}
    ]
    assert body["declare"]["commit"] is None


def test_a_deploy_whose_schedule_check_failed_does_not_read_as_a_success(
    panel: PanelFactory,
) -> None:
    """The container went up and the check did not pass. The message must say both."""
    built = panel(nursery=canned_pipeline(problems=("runner binary not found: claude",)))

    body = built.client.post("/residents", json=NEW_RESIDENT | {"deploy": True}).json()

    assert body["register"]["ok"] is False
    assert "did not pass" in body["message"]
    assert "register.problems" in body["message"]


def test_raising_a_retired_resident_is_refused_by_its_own_code(panel: PanelFactory) -> None:
    # A code rather than prose, because the form has to tell this refusal apart from
    # "that name is taken by a different declaration" without matching on a sentence.
    data = copy.deepcopy(valid_manifest())
    data["retired"] = True
    built = panel(manifest=data)

    response = built.client.post(
        "/residents", json=NEW_RESIDENT | {"id": "test-agent", "deploy": True}
    )

    assert response.status_code == 409
    assert response.json()["detail"]["error"] == "resident_retired"
    assert "retired: false" in response.json()["detail"]["message"]


def test_request_window_filters_resident_and_clamps_large_ledger(panel: PanelFactory) -> None:
    built = panel()
    built.store.log_request(
        request_id="hob", method="POST", path="/residents/hob/pause", outcome="ran"
    )
    for index in range(510):
        built.store.log_request(
            request_id=f"other-{index}",
            method="POST",
            path="/residents/hobbit/pause",
            outcome="ran",
        )
    assert [
        r["request_id"]
        for r in built.client.get("/requests?resident=hob&limit=1").json()["requests"]
    ] == ["hob"]
    assert len(built.client.get("/requests").json()["requests"]) == 50
    assert len(built.client.get("/requests?limit=9999").json()["requests"]) == 500
    assert len(built.client.get("/requests?limit=-1").json()["requests"]) == 1


def test_routine_summary_hydrates_only_latest_declared_requests(
    panel: PanelFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    built = panel()
    for index in range(10):
        built.store.log_request(
            request_id=str(index),
            method="POST",
            path="/jobs",
            outcome="queued",
            detail={"routine": "test-agent/daily-summary"},
        )
    original = RequestRecord.from_row
    hydrated: list[str] = []

    def track(row: sqlite3.Row) -> RequestRecord:
        record = original(row)
        hydrated.append(record.request_id)
        return record

    monkeypatch.setattr(RequestRecord, "from_row", track)
    rows = built.client.get("/routines").json()["routines"]
    assert rows[0]["last_request"]["request_id"] == "9"
    assert hydrated == ["9"]


def test_run_receipt_and_request_ledger_validate_exported_contract(panel: PanelFactory) -> None:
    """The accepted id survives through both ledger reads, with a checkable envelope."""
    document = json.loads((REPO_ROOT / "docs/openapi.json").read_text(encoding="utf-8"))
    built = panel()
    path = "/residents/{resident_id}/routines/{routine_id}/run"
    accepted = built.client.post("/residents/test-agent/routines/daily-summary/run")
    assert accepted.status_code == 202
    request_id = accepted.json()["request_id"]
    for route, method, response in [
        (path, "post", accepted),
        ("/requests/{request_id}", "get", built.client.get(f"/requests/{request_id}")),
        ("/requests", "get", built.client.get("/requests")),
    ]:
        schema = document["paths"][route][method]["responses"][str(response.status_code)][
            "content"
        ]["application/json"]["schema"]
        assert "$ref" in schema, f"{route} still publishes an unchecked dictionary"
        validator = Draft202012Validator({**schema, "components": document["components"]})
        validator.validate(response.json())
        malformed = dict(response.json())
        del malformed["requests" if route == "/requests" else "request_id"]
        assert not validator.is_valid(malformed)


def test_routine_rows_validate_before_and_after_a_run(
    panel: PanelFactory, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("STEWARD_STATE", str(tmp_path / "never.json"))
    document = json.loads((REPO_ROOT / "docs/openapi.json").read_text(encoding="utf-8"))
    schema = document["paths"]["/routines"]["get"]["responses"]["200"]["content"][
        "application/json"
    ]["schema"]
    assert "$ref" in schema, "routine rows still have no published fields"
    validator = Draft202012Validator({**schema, "components": document["components"]})
    built = panel()
    before = built.client.get("/routines").json()
    validator.validate(before)
    assert before["scheduler"]["alive"] is None
    assert before["routines"][0]["last_request"] is None
    assert before["routines"][0]["last_run"] is None
    assert before["routines"][0]["journal"] is None
    assert built.client.post("/residents/test-agent/routines/daily-summary/run").status_code == 202
    built.client.app.state.runs.wait(timeout=10.0)
    after = built.client.get("/routines").json()
    validator.validate(after)
    assert after["routines"][0]["last_request"]["outcome"] == "ran"
    assert after["routines"][0]["last_run"]["trigger"] == "manual"
    del after["routines"][0]["last_run"]
    assert not validator.is_valid(after), "nullable must not mean absent"


@pytest.mark.parametrize(
    ("method", "path", "status", "code"),
    [
        ("get", "/requests/missing", 404, "unknown_request"),
        ("post", "/residents/test-agent/routines/missing/run", 404, "unknown_routine"),
        ("post", "/residents/missing/routines/daily-summary/run", 404, "unknown_resident"),
        ("post", "/residents/test-agent/routines/daily-summary/run", 409, "routine_disabled"),
    ],
)
def test_run_path_refusals_validate_exported_contract(
    panel: PanelFactory, method: str, path: str, status: int, code: str
) -> None:
    data = valid_manifest()
    data["routines"][0]["enabled"] = False
    built = panel(manifest=data)
    response = built.client.request(method, path)
    assert response.status_code == status
    assert response.json()["detail"]["error"] == code
    assert "request_id" not in response.json()
    route = (
        "/requests/{request_id}"
        if method == "get"
        else ("/residents/{resident_id}/routines/{routine_id}/run")
    )
    document = json.loads((REPO_ROOT / "docs/openapi.json").read_text(encoding="utf-8"))
    schema = document["paths"][route][method]["responses"][str(status)]["content"][
        "application/json"
    ]["schema"]
    validator = Draft202012Validator({**schema, "components": document["components"]})
    validator.validate(response.json())
    assert not validator.is_valid({"detail": {"error": code}})


@pytest.mark.parametrize(
    ("method", "path", "route"),
    [
        ("GET", "/requests", "/requests"),
        ("GET", "/requests/missing", "/requests/{request_id}"),
        ("GET", "/routines", "/routines"),
        (
            "POST",
            "/residents/test-agent/routines/daily-summary/run",
            "/residents/{resident_id}/routines/{routine_id}/run",
        ),
    ],
)
def test_typed_path_auth_refusals_match_the_artifact(
    panel: PanelFactory, method: str, path: str, route: str
) -> None:
    built = panel()
    response = anonymous(built).request(method, path)
    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"
    document = json.loads((REPO_ROOT / "docs/openapi.json").read_text(encoding="utf-8"))
    schema = document["paths"][route][method.lower()]["responses"]["401"]["content"][
        "application/json"
    ]["schema"]
    Draft202012Validator({**schema, "components": document["components"]}).validate(response.json())


def test_session_run_refusal_and_invalid_limit_keep_distinct_contracts(panel: PanelFactory) -> None:
    built = panel()
    credential = new_session_credential()
    assert built.store.open_run(
        run_id="live-run",
        kind="routine",
        trigger="schedule",
        agent_id="claude-code:test-agent",
        project="test-agent",
        ref="daily-summary",
        resident_id="test-agent",
        session_credential=credential,
        timeout_s=900.0,
    )
    refused = built.client.post(
        "/residents/test-agent/routines/daily-summary/run",
        headers={"Authorization": f"Bearer {credential}"},
    )
    assert refused.status_code == 403
    assert refused.json()["detail"]["error"] == "session_credential_forbidden"
    invalid = built.client.get("/requests?limit=not-a-number")
    assert invalid.status_code == 422
    assert isinstance(invalid.json()["detail"], list)
    document = json.loads((REPO_ROOT / "docs/openapi.json").read_text(encoding="utf-8"))
    for method, route, response in [
        ("post", "/residents/{resident_id}/routines/{routine_id}/run", refused),
        ("get", "/requests", invalid),
    ]:
        schema = document["paths"][route][method]["responses"][str(response.status_code)][
            "content"
        ]["application/json"]["schema"]
        Draft202012Validator({**schema, "components": document["components"]}).validate(
            response.json()
        )
