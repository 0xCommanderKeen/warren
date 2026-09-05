"""API behavior: session grants."""

import copy
from collections.abc import Callable
from pathlib import Path

import pytest

from conftest import (
    valid_manifest,
)
from steward.deploy import LocalTransport
from support.api import (
    NEW_RESIDENT,
    NEW_SKILL,
    ApiFactory,
    Harness,
    as_session,
    declaration,
    last_commit,
    open_session_run,
)
from support.api import (
    api as api,  # noqa: PLC0414 — pytest fixture discovery
)
from support.api import (
    village as village,  # noqa: PLC0414 — pytest fixture discovery
)
from support.api import (
    writable as writable,  # noqa: PLC0414 — pytest fixture discovery
)

# -- human-only ------------------------------------------------------------------------


def test_a_session_may_not_write_anything_the_fleet_is_declared_by(
    writable: Callable[..., Harness],
) -> None:
    """Without named grants, the allowlist holds by construction, route by route.

    A resident editing its own charter would be choosing the rules it is held to, and a
    resident writing a skill would be handing itself instructions nobody approved. Both are
    refused unless the manifest opens the corresponding narrow door — and the declaration
    door needs a human's yes on top of the grant (warren#437).
    """
    harness = writable()
    credential = open_session_run(harness)
    before = declaration(harness)["text"]

    refusals = {
        "declaration": harness.client.put(
            "/residents/test-agent/declaration",
            json={"manifest": {"id": "test-agent"}},
            headers=as_session(credential),
        ),
        "skill_create": harness.client.post(
            "/skills", json=NEW_SKILL, headers=as_session(credential)
        ),
        "skill_update": harness.client.put(
            "/skills/daily-summary",
            json={"description": "d", "body": "b"},
            headers=as_session(credential),
        ),
        "reload": harness.client.post("/reload", headers=as_session(credential)),
    }

    for route, response in refusals.items():
        assert response.status_code == 403, route
        assert response.json()["detail"]["error"] == "session_credential_forbidden", route
    assert declaration(harness)["text"] == before
    assert harness.store.export_request_history() == [], "and nothing was logged as accepted"


def test_a_granted_session_can_create_and_update_an_ungranted_skill(
    writable: Callable[..., Harness],
) -> None:
    manifest = copy.deepcopy(valid_manifest())
    manifest["session_grants"] = ["skills.write"]
    harness = writable(manifest=manifest)
    credential = open_session_run(harness)

    created = harness.client.post("/skills", json=NEW_SKILL, headers=as_session(credential))
    assert created.status_code == 201, created.text
    assert created.json()["commit"]["committed"]
    assert created.json()["commit"]["sha"]
    assert "test-agent (session) <test-agent-session@localhost>" in last_commit(harness)

    updated = harness.client.put(
        "/skills/triage",
        json={"description": "Sort the inbox, gently.", "body": NEW_SKILL["body"]},
        headers=as_session(credential),
    )
    assert updated.status_code == 200, updated.text
    assert updated.json()["commit"]["committed"]


def test_a_granted_session_cannot_grant_everyone_or_rewrite_a_granted_skill(
    writable: Callable[..., Harness],
) -> None:
    manifest = copy.deepcopy(valid_manifest())
    manifest["session_grants"] = ["skills.write"]
    harness = writable(manifest=manifest)
    credential = open_session_run(harness)

    defaulted = harness.client.post(
        "/skills", json=NEW_SKILL | {"defaults": True}, headers=as_session(credential)
    )
    granted = harness.client.put(
        "/skills/daily-summary",
        json={"description": "Rewritten", "body": "Do something else."},
        headers=as_session(credential),
    )

    assert defaulted.status_code == 403
    assert "grant" in defaulted.json()["detail"]["message"]
    assert granted.status_code == 403
    assert "rewriting" in granted.json()["detail"]["message"]
    assert harness.store.export_request_history() == []

    assert harness.client.post("/skills", json=NEW_SKILL | {"defaults": True}).status_code == 201
    default_skill = harness.client.put(
        "/skills/triage",
        json={"description": "No longer default", "body": "Changed by the session."},
        headers=as_session(credential),
    )
    assert default_skill.status_code == 403
    assert "rewriting" in default_skill.json()["detail"]["message"]
    assert (
        harness.client.put(
            "/skills/daily-summary", json={"description": "Rewritten", "body": "Human edit."}
        ).status_code
        == 200
    )


def test_a_session_granted_residents_declare_can_only_declare_without_deploying(
    writable: Callable[..., Harness],
) -> None:
    manifest = copy.deepcopy(valid_manifest())
    manifest["session_grants"] = ["residents.declare"]
    harness = writable(manifest=manifest)
    credential = open_session_run(harness)

    declared = harness.client.post(
        "/residents", json=NEW_RESIDENT | {"deploy": False}, headers=as_session(credential)
    )
    deploying = harness.client.post(
        "/residents",
        json=NEW_RESIDENT | {"id": "deployed-colleague", "deploy": True},
        headers=as_session(credential),
    )

    assert declared.status_code == 201, declared.text
    assert declared.json()["commit"]["committed"]
    assert "test-agent (session) <test-agent-session@localhost>" in last_commit(harness)
    assert deploying.status_code == 403
    assert "deploying" in deploying.json()["detail"]["message"]
    assert not (harness.residents_dir / "deployed-colleague").exists()


@pytest.mark.usefixtures("village")
def test_a_session_granted_residents_dry_run_gets_the_plan_without_reaching_the_host(
    api: ApiFactory, tmp_path: Path
) -> None:
    manifest = copy.deepcopy(valid_manifest())
    manifest["session_grants"] = ["residents.dry_run"]
    host = LocalTransport(root=tmp_path / "nas")
    harness = api(manifest=manifest, transport=host)
    credential = open_session_run(harness)

    response = harness.client.post(
        "/residents/test-agent/provision",
        json={"dry_run": True},
        headers=as_session(credential),
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["dry_run"] is True
    assert {"files", "compose", "commands", "env_keys"} <= set(body["provision"])
    assert "next_fires" in body["register"]
    assert not host.touched


@pytest.mark.parametrize("body", [None, {"dry_run": False}])
def test_a_session_granted_residents_dry_run_cannot_provision_for_real(
    api: ApiFactory, body: dict[str, bool] | None
) -> None:
    manifest = copy.deepcopy(valid_manifest())
    manifest["session_grants"] = ["residents.dry_run"]
    harness = api(manifest=manifest)
    credential = open_session_run(harness)

    response = harness.client.post(
        "/residents/test-agent/provision", json=body, headers=as_session(credential)
    )

    assert response.status_code == 403
    assert "provisioning" in response.json()["detail"]["message"]


def test_resident_session_grants_never_open_declaration_edits_or_retirement(
    writable: Callable[..., Harness],
) -> None:
    """Neither resident grant reaches the declaration; only `residents.grant_skill` does."""
    manifest = copy.deepcopy(valid_manifest())
    manifest["session_grants"] = ["residents.declare", "residents.dry_run"]
    harness = writable(manifest=manifest)
    credential = open_session_run(harness)

    declaration_edit = harness.client.put(
        "/residents/test-agent/declaration",
        json={"manifest": manifest},
        headers=as_session(credential),
    )
    retirement = harness.client.post(
        "/residents/test-agent/retire",
        json={"dry_run": True},
        headers=as_session(credential),
    )

    assert declaration_edit.status_code == 403
    assert retirement.status_code == 403
    assert harness.store.export_request_history() == []
