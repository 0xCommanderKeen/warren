"""API behavior: operator auth."""

import copy
from collections.abc import Callable
from pathlib import Path

from conftest import (
    ResidentWriter,
    valid_manifest,
)
from steward import manifest as m
from steward.input_bounds import (
    APPROVAL_BODY_MAX_BYTES,
)
from steward.manifest import validate_tree
from steward.operator_auth import new_operator_credential
from steward.session_auth import (
    credential_digest,
)
from support.api import (
    ApiFactory,
    Harness,
    _pending,
    as_operator,
    declaration,
    last_commit,
    mint_operator,
)
from support.api import (
    api as api,  # noqa: PLC0414 — pytest fixture discovery
)
from support.api import (
    writable as writable,  # noqa: PLC0414 — pytest fixture discovery
)


def test_an_operator_credential_authenticates_a_read(api: ApiFactory) -> None:
    """The whole point: a browser gets through the door without the master token."""
    harness = api()
    credential = mint_operator(harness)

    response = harness.client.get("/approvals", headers=as_operator(credential))

    assert response.status_code == 200


def test_an_operator_may_decide_an_approval_where_a_session_may_not(api: ApiFactory) -> None:
    """An operator is a *human* principal, so the session allowlist does not touch it.

    This is the line the feature exists on. A session is refused here because deciding an
    approval is the human end of the escalation boundary; an operator is that human.
    """
    harness = api()
    credential = mint_operator(harness)
    request_id = _pending(harness)

    response = harness.client.post(
        f"/approvals/{request_id}",
        json={"decision": "approve"},
        headers=as_operator(credential),
    )

    assert response.status_code == 202
    assert harness.store.approval(request_id).decision == "approve"  # ty: ignore[unresolved-attribute]


def test_an_approval_records_the_operator_who_decided_it(api: ApiFactory) -> None:
    """warren#225's payoff: the audit view names a person rather than saying "api"."""
    harness = api()
    credential = mint_operator(harness, name="Miha")
    request_id = _pending(harness)

    harness.client.post(
        f"/approvals/{request_id}", json={"decision": "deny"}, headers=as_operator(credential)
    )

    assert harness.store.approval(request_id).decided_by == "Miha"  # ty: ignore[unresolved-attribute]


def test_the_master_token_still_decides_as_the_nameless_api(api: ApiFactory) -> None:
    """The shared secret names nobody, and steward does not invent a name for it."""
    harness = api()
    request_id = _pending(harness)

    harness.client.post(f"/approvals/{request_id}", json={"decision": "approve"})

    assert harness.store.approval(request_id).decided_by == "api"  # ty: ignore[unresolved-attribute]


def test_a_job_an_operator_posts_carries_their_name(api: ApiFactory) -> None:
    """The same substitution on the board: `posted_by` is a who, so it says who."""
    harness = api()
    credential = mint_operator(harness, name="Miha")

    harness.client.post("/jobs", json={"title": "Research X"}, headers=as_operator(credential))

    assert harness.store.jobs()[0].posted_by == "Miha"


def test_a_revoked_credential_stops_working_on_the_next_request(api: ApiFactory) -> None:
    """Revocation is the difference from the master token, so it had better be immediate."""
    harness = api()
    credential = mint_operator(harness, name="Miha")
    assert harness.client.get("/approvals", headers=as_operator(credential)).status_code == 200

    harness.store.revoke_operator("Miha")

    response = harness.client.get("/approvals", headers=as_operator(credential))
    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"


def test_a_credential_nobody_minted_is_refused(api: ApiFactory) -> None:
    """Shaped like one is not the same as being one."""
    harness = api()

    response = harness.client.get("/approvals", headers=as_operator(new_operator_credential()))

    assert response.status_code == 401
    detail = response.json()["detail"]
    assert detail["error"] == "unauthorized"
    assert "steward operator mint" in detail["message"]


def test_the_operator_credential_is_stored_as_a_digest_and_never_in_the_clear(
    api: ApiFactory,
) -> None:
    """A copy of steward.db must not yield live credentials."""
    harness = api()
    credential = mint_operator(harness)

    (row,) = harness.store._conn.execute("SELECT * FROM operator_credentials").fetchall()

    stored = set(dict(row).values())
    assert credential not in stored
    assert credential_digest(credential) in stored


def test_an_oversized_approval_body_is_still_bounded_for_an_operator(api: ApiFactory) -> None:
    """The depth guard runs on shape alone, so it must recognise this credential too.

    A middleware that only knew two of the three credential kinds would leave the newest
    one — the one a browser holds — as the unbounded path.
    """
    harness = api()
    credential = mint_operator(harness)
    request_id = _pending(harness)

    response = harness.client.post(
        f"/approvals/{request_id}",
        content=b'{"decision": "approve", "edit": ' + b"x" * APPROVAL_BODY_MAX_BYTES + b"}",
        headers={**as_operator(credential), "Content-Type": "application/json"},
    )

    assert response.status_code == 413


def test_an_operator_commits_under_their_own_name(writable: Callable[..., Harness]) -> None:
    """The generic `steward (api)` author exists because a shared secret names nobody.

    An operator credential does name somebody, so the commit says so — which is the whole
    reason to prefer one over the token for a write surface.
    """
    harness = writable()
    credential = mint_operator(harness, name="Miha", email="miha@example.invalid")
    body = declaration(harness)

    response = harness.client.put(
        "/residents/test-agent/declaration",
        json={"manifest": body["manifest"], "soul": body["soul"], "revision": body["revision"]},
        headers=as_operator(credential),
    )

    assert response.status_code == 200
    author = last_commit(harness)
    assert "Miha <miha@example.invalid>" in author
    assert "Miha, over the steward API with an operator credential" in author


def test_a_write_with_the_master_token_is_still_authored_by_steward(
    writable: Callable[..., Harness],
) -> None:
    """Unchanged for everybody who has not minted one: no name is invented for a secret."""
    harness = writable()
    body = declaration(harness)

    harness.client.put(
        "/residents/test-agent/declaration",
        json={"manifest": body["manifest"], "soul": body["soul"], "revision": body["revision"]},
    )

    author = last_commit(harness)
    assert "steward (api) <steward-api@localhost>" in author
    assert "a holder of STEWARD_TOKEN" in author


def test_an_operator_credential_is_refused_on_the_way_into_the_repo(
    api: ApiFactory, write_resident: ResidentWriter, tmp_path: Path
) -> None:
    """It is a credential, so a manifest carrying one must not validate (steward #144).

    The credential a *browser* holds is the one most likely to be pasted somewhere by
    hand, which is precisely why its shape belongs in the scanner rather than only in the
    session prefix's.
    """
    api()
    leaky = copy.deepcopy(valid_manifest())
    leaky["id"] = "leaky"
    leaky["summary"] = f"paste {new_operator_credential()} here"
    write_resident(leaky, root=tmp_path / "residents")

    result = validate_tree(tmp_path / "residents")

    assert any("operator credential" in problem.render() for problem in result.errors)


def test_an_operator_credential_is_scrubbed_on_the_way_out_to_the_village(
    api: ApiFactory,
) -> None:
    """And the egress half: a session that echoes one must not have it survive into an event."""
    api()
    credential = new_operator_credential()

    scrubbed = m.redact_secrets(f"I found {credential} in the logs")

    assert credential not in scrubbed
    assert m.SECRET_REDACTION in scrubbed
