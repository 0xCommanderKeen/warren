"""API behavior: rehearsal."""

import copy
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
import yaml

from conftest import (
    SECOND_RESIDENT_UID,
    SKILL_BODY,
    valid_manifest,
)
from steward.runners import Outcome, RunRequest, RunResult
from support.api import (
    Harness,
    as_session,
    open_session_run,
)
from support.api import (
    api as api,  # noqa: PLC0414 — pytest fixture discovery
)
from support.api import (
    writable as writable,  # noqa: PLC0414 — pytest fixture discovery
)

# --------------------------------- rehearsing a declaration (warren#446)


COLLEAGUE_SOUL = """---
agent_id: claude-code:new-colleague
name: Pip
char: Monk
accent: "#a68a4f"
role: errand bot
---
A resident that has been declared and never built.

## Voice

Brisk.
"""


def declare_colleague(harness: Harness) -> dict[str, Any]:
    """Write a second resident into the tree: declared, never provisioned."""
    manifest = copy.deepcopy(valid_manifest())
    manifest |= {
        "uid": SECOND_RESIDENT_UID,
        "id": "new-colleague",
        "agent_id": "claude-code:new-colleague",
        "soul": {"name": "Pip", "char": "Monk", "accent": "#a68a4f", "role": "errand bot"},
        "home": 1,
        "memory": {
            "kind": "directory",
            "path": str(harness.residents_dir / "never-built" / "memory"),
            "journal": "journal",
        },
        "routines": [],
    }
    manifest["charter"] |= {"rules": ["Never leave the burrow unasked."]}
    colleague = harness.residents_dir / "new-colleague"
    colleague.mkdir(parents=True, exist_ok=True)
    (colleague / "manifest.yaml").write_text(yaml.safe_dump(manifest, sort_keys=False), "utf-8")
    (colleague / "soul.md").write_text(COLLEAGUE_SOUL, encoding="utf-8")
    return manifest


def recording_runner() -> tuple[list[RunRequest], Callable[[RunRequest], RunResult]]:
    """Build a runner behavior that keeps every request and answers in the voice."""
    seen: list[RunRequest] = []

    def behavior(request: RunRequest) -> RunResult:
        seen.append(request)
        return RunResult(
            outcome=Outcome.OK,
            output="Morning. Nothing to report yet.",
            exit_status=0,
            duration_s=1.5,
            cost_usd=0.02,
            input_tokens=1200,
            output_tokens=40,
        )

    return seen, behavior


def test_a_rehearsal_runs_one_turn_from_the_declaration_and_keeps_nothing(
    writable: Callable[..., Harness],
) -> None:
    """The whole of warren#446 in one call: the charter answers, and nothing else moves.

    Charter, soul and skills reach the prompt exactly as a real launch would put them
    there — and the environment, the workspace and the working directory carry none of
    what a *provisioned* resident would have, because none of it exists yet.
    """
    seen, behavior = recording_runner()
    harness = writable(behavior=behavior)
    manifest = declare_colleague(harness)
    memory = Path(manifest["memory"]["path"])

    response = harness.client.post(
        "/residents/new-colleague/rehearse", json={"message": "Say good morning."}
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["reply"] == "Morning. Nothing to report yet."
    assert body["ok"] is True
    assert body["rehearsal"] is True

    assert len(seen) == 1
    request = seen[0]
    # the declaration, injected the way a launch injects it
    assert "Exercise the schema without touching the real world." in request.prompt
    assert "Never leave the burrow unasked." in request.prompt
    # the soul: the identity block and the ``## Voice`` section a launch extracts from it
    assert "Pip" in request.prompt
    assert "Brisk." in request.prompt
    assert SKILL_BODY.strip() in request.prompt
    assert "Say good morning." in request.prompt
    # and none of what a provisioned resident would have
    assert dict(request.env) == {}
    assert request.workspace == ()
    # `tools: unrestricted` in the declaration, and nothing at all in the rehearsal: the
    # turn is not placed in a container, so an unbounded grant would be a shell on the
    # control plane's own host for anybody who can write a declaration
    assert manifest["tools"] == "unrestricted"
    assert request.tools.bound == ()
    assert request.workdir != memory
    assert not memory.exists()
    # nothing was recorded anywhere
    assert harness.store.ledger("new-colleague") == []
    assert harness.events() == []


def test_a_rehearsal_leaves_the_declaration_exactly_as_unprovisioned_as_it_was(
    writable: Callable[..., Harness],
) -> None:
    """The scratch directory the turn ran in does not outlive the request."""
    seen, behavior = recording_runner()
    harness = writable(behavior=behavior)
    declare_colleague(harness)

    assert (
        harness.client.post(
            "/residents/new-colleague/rehearse", json={"message": "Hello."}
        ).status_code
        == 200
    )

    assert not seen[0].workdir.exists()
    assert sorted(p.name for p in (harness.residents_dir / "new-colleague").iterdir()) == [
        "manifest.yaml",
        "soul.md",
    ]


def test_a_session_granted_residents_rehearse_pays_for_the_turn_itself(
    writable: Callable[..., Harness],
) -> None:
    """A rehearsal spends the *caller's* budget line, and says so (warren#446).

    The resident being rehearsed has no line to spend: it has never run, and a ledger row
    against it would say it did work it has no container to have done.
    """
    manifest = copy.deepcopy(valid_manifest())
    manifest["session_grants"] = ["residents.rehearse"]
    _seen, behavior = recording_runner()
    harness = writable(manifest=manifest, behavior=behavior)
    declare_colleague(harness)
    credential = open_session_run(harness)

    response = harness.client.post(
        "/residents/new-colleague/rehearse",
        json={"message": "Say good morning."},
        headers=as_session(credential),
    )

    assert response.status_code == 200, response.text
    assert response.json()["charged_to"] == "test-agent"
    assert harness.store.ledger("new-colleague") == []
    (entry,) = harness.store.ledger("test-agent")
    assert entry.kind == "rehearsal"
    assert entry.ref == "new-colleague"
    assert entry.origin == "rehearsal:new-colleague"
    assert entry.cost_usd == 0.02


def test_a_human_rehearsal_is_charged_to_no_resident(
    writable: Callable[..., Harness],
) -> None:
    """An operator has no ledger line, and steward says that rather than inventing one."""
    _seen, behavior = recording_runner()
    harness = writable(behavior=behavior)
    declare_colleague(harness)

    body = harness.client.post(
        "/residents/new-colleague/rehearse", json={"message": "Hello."}
    ).json()

    assert body["charged_to"] == ""
    assert "no resident's budget line" in body["message"]
    assert harness.store.ledger() == []


@pytest.mark.parametrize("grants", [[], ["residents.declare", "residents.dry_run"]])
def test_rehearsing_is_refused_without_the_rehearse_grant(
    writable: Callable[..., Harness], grants: list[str]
) -> None:
    """The expensive door is never implied by the free one (warren#410 decision 9)."""
    manifest = copy.deepcopy(valid_manifest())
    manifest["session_grants"] = grants
    seen, behavior = recording_runner()
    harness = writable(manifest=manifest, behavior=behavior)
    declare_colleague(harness)
    credential = open_session_run(harness)

    response = harness.client.post(
        "/residents/new-colleague/rehearse",
        json={"message": "Say good morning."},
        headers=as_session(credential),
    )

    assert response.status_code == 403
    message = response.json()["detail"]["message"]
    assert "spends the caller's budget" in message
    assert "residents.rehearse" in message
    assert seen == []
    assert harness.store.export_request_history() == []
    assert harness.store.ledger() == []


def test_a_rehearsal_is_refused_when_the_caller_that_pays_is_out_of_budget(
    writable: Callable[..., Harness],
) -> None:
    """The payer's cap is a real cap: a rehearsal cannot be the way around it."""
    manifest = copy.deepcopy(valid_manifest())
    manifest["session_grants"] = ["residents.rehearse"]
    manifest["budgets"] = {"daily_cost_usd": 0.01}
    seen, behavior = recording_runner()
    harness = writable(manifest=manifest, behavior=behavior)
    declare_colleague(harness)
    credential = open_session_run(harness)

    spent = harness.client.post(
        "/residents/new-colleague/rehearse",
        json={"message": "First."},
        headers=as_session(credential),
    )
    refused = harness.client.post(
        "/residents/new-colleague/rehearse",
        json={"message": "Second."},
        headers=as_session(credential),
    )

    assert spent.status_code == 200, spent.text
    assert refused.status_code == 409
    assert refused.json()["detail"]["error"] == "rehearsal_refused"
    assert "test-agent pays for this rehearsal" in refused.json()["detail"]["message"]
    assert len(seen) == 1


def test_a_rehearsal_refuses_an_unknown_resident_and_an_empty_message(
    writable: Callable[..., Harness],
) -> None:
    """Two refusals that must not reach a runner: no such declaration, and nothing to say."""
    seen, behavior = recording_runner()
    harness = writable(behavior=behavior)

    missing = harness.client.post("/residents/nobody/rehearse", json={"message": "Hello."})
    empty = harness.client.post("/residents/test-agent/rehearse", json={"message": ""})

    assert missing.status_code == 404
    assert empty.status_code == 422
    assert seen == []
