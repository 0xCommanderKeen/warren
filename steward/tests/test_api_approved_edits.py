"""API behavior: approved edits."""

import copy
import datetime as dt
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
import pytest
import yaml

from conftest import (
    VALID_SOUL,
    SkillWriter,
    valid_manifest,
)
from steward import authoring as au
from steward import events as ev
from steward.approved_edits import GRANT_SKILL_ACTION
from steward.store import ApprovalRecord
from support.api import (
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
    writable as writable,  # noqa: PLC0414 — pytest fixture discovery
)

# -- the one door an approval opens (warren#437) ----------------------------------------

#: A skill the library holds and the test resident does not, so granting it is a real edit.
UNGRANTED_SKILL = "series-detection"


#: A clock far enough ahead that a deadline set today has passed by the time the route
#: reads it, without any test having to sleep or hand-edit a decided row.
LONG_AFTERWARDS = dt.datetime(2030, 1, 1, tzinfo=dt.UTC)


@pytest.fixture
def grantor(
    writable: Callable[..., Harness], write_skill: SkillWriter, tmp_path: Path
) -> Callable[..., Harness]:
    """Build a resident holding ``residents.grant_skill``, with a skill in the library."""

    def _make(**kwargs: object) -> Harness:
        write_skill(UNGRANTED_SKILL, root=tmp_path / "skills")
        manifest = copy.deepcopy(valid_manifest())
        manifest["session_grants"] = ["residents.grant_skill"]
        return writable(manifest=manifest, **kwargs)

    return _make


def approved_grant(  # noqa: PLR0913 — one keyword per thing a refusal test varies
    harness: Harness,
    *,
    resident: str = "test-agent",
    skill: str = UNGRANTED_SKILL,
    raised_by: str = "test-agent",
    action: str = GRANT_SKILL_ACTION,
    decision: str | None = "approve",
    expires_at: str | None = None,
) -> str:
    """Raise the knock Karen's skill writes, and answer it the way Miha would."""
    record = harness.store.create_approval_request(
        agent_id=f"claude-code:{raised_by}",
        project=raised_by,
        action=action,
        message=f"Grant {skill} to {resident}?",
        detail={"resident": resident, "skill": skill, "note": "Reads the series index."},
        options=("approve", "deny"),
        resident=raised_by,
        expires_at=expires_at,
    )
    if decision is not None:
        harness.store.decide(record.request_id, decision, decided_by="miha")
    return record.request_id


def manifest_granting(harness: Harness, skill: str = UNGRANTED_SKILL) -> dict[str, Any]:
    """Build the candidate that adds exactly one skill to what is already declared."""
    data = yaml.safe_load(declaration(harness)["text"])
    data["skills"] = [*data["skills"], {"id": skill, "note": "Reads the series index."}]
    return data


def put_declaration(
    harness: Harness,
    credential: str,
    manifest: dict[str, Any],
    **body: Any,  # noqa: ANN401 — the request body's own fields, passed through
) -> httpx.Response:
    """PUT a candidate declaration as a session, the way the skill's curl does."""
    return harness.client.put(
        "/residents/test-agent/declaration",
        json={"text": yaml.safe_dump(manifest, sort_keys=False), **body},
        headers=as_session(credential),
    )


def decision_row(harness: Harness, request_id: str) -> ApprovalRecord:
    """Read one decision back out of the ledger, insisting it is there."""
    record = harness.store.approval(request_id)
    assert record is not None
    return record


def consumed_at(harness: Harness, request_id: str) -> str | None:
    """Read back whether the decision has been spent on a write."""
    return decision_row(harness, request_id).consumed_at


def test_a_granted_session_writes_the_one_skill_a_human_approved(
    grantor: Callable[..., Harness],
) -> None:
    """Karen's end of #410: the yes arrives, and the session performs the edit itself."""
    harness = grantor()
    credential = open_session_run(harness)
    request_id = approved_grant(harness)

    response = put_declaration(
        harness, credential, manifest_granting(harness), approval_request_id=request_id
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["commit"]["committed"]
    assert body["commit"]["sha"]
    assert body["approval"] == {
        "request_id": request_id,
        "act": f"grant skill {UNGRANTED_SKILL!r} to 'test-agent'",
    }
    assert "test-agent (session) <test-agent-session@localhost>" in last_commit(harness)
    granted = yaml.safe_load(declaration(harness)["text"])["skills"]
    assert {"id": UNGRANTED_SKILL, "note": "Reads the series index."} in granted
    assert consumed_at(harness, request_id), "the decision is consumed by the write"


def test_one_approval_is_one_edit(grantor: Callable[..., Harness]) -> None:
    harness = grantor()
    credential = open_session_run(harness)
    request_id = approved_grant(harness)
    first = put_declaration(
        harness, credential, manifest_granting(harness), approval_request_id=request_id
    )
    assert first.status_code == 200, first.text

    again = put_declaration(
        harness,
        credential,
        manifest_granting(harness, "write-blog-post"),
        approval_request_id=request_id,
    )

    assert again.status_code == 403
    assert again.json()["detail"]["error"] == "edit_not_approved"
    assert "already spent" in again.json()["detail"]["message"]


@pytest.mark.parametrize(
    ("raise_kwargs", "expected"),
    [
        pytest.param({"decision": None}, "still waiting on a human", id="undecided"),
        pytest.param({"decision": "deny"}, "answered 'deny'", id="denied"),
        pytest.param({"raised_by": "someone-else"}, "raised by someone-else", id="borrowed"),
        pytest.param({"action": "send_email"}, "'send_email'", id="a yes about another act"),
        pytest.param({"resident": "hob"}, "this edit writes 'test-agent'", id="another resident"),
    ],
)
def test_a_decision_that_is_not_a_live_yes_about_this_edit_writes_nothing(
    grantor: Callable[..., Harness], raise_kwargs: dict[str, Any], expected: str
) -> None:
    harness = grantor()
    credential = open_session_run(harness)
    before = declaration(harness)["text"]
    request_id = approved_grant(harness, **raise_kwargs)

    response = put_declaration(
        harness, credential, manifest_granting(harness), approval_request_id=request_id
    )

    assert response.status_code == 403
    assert response.json()["detail"]["error"] == "edit_not_approved"
    assert expected in response.json()["detail"]["message"]
    assert declaration(harness)["text"] == before
    assert harness.store.export_request_history() == [], "a refusal at the door records nothing"


def test_a_yes_that_has_run_out_writes_nothing(grantor: Callable[..., Harness]) -> None:
    """The deadline bounds the whole act, not only how long there was to answer."""
    harness = grantor(now=lambda: LONG_AFTERWARDS)
    # The run has to still be live at the same clock, or the credential expires first and
    # the test would be watching the wrong door shut.
    credential = open_session_run(harness, heartbeat_at=ev.utc_now_iso(LONG_AFTERWARDS))
    request_id = approved_grant(harness, expires_at="2029-01-01T00:00:00Z")
    assert decision_row(harness, request_id).decision == "approve", "answered in time"

    response = put_declaration(
        harness, credential, manifest_granting(harness), approval_request_id=request_id
    )

    assert response.status_code == 403
    assert "expired at 2029-01-01T00:00:00Z" in response.json()["detail"]["message"]
    assert consumed_at(harness, request_id) is None


def test_an_approval_nobody_raised_opens_nothing(grantor: Callable[..., Harness]) -> None:
    harness = grantor()
    credential = open_session_run(harness)

    response = put_declaration(
        harness, credential, manifest_granting(harness), approval_request_id="no-such-request"
    )

    assert response.status_code == 403
    assert "no approval request" in response.json()["detail"]["message"]


@pytest.mark.parametrize(
    ("mutate", "expected"),
    [
        pytest.param(
            lambda m: m | {"summary": "Something else entirely."},
            "also changes summary",
            id="a second change riding along",
        ),
        pytest.param(
            lambda m: m | {"session_grants": ["skills.write", "residents.grant_skill"]},
            "also changes session_grants",
            id="a door opened alongside the grant",
        ),
        pytest.param(
            lambda m: m | {"skills": [*m["skills"][:-1], "write-blog-post"]},
            "grants 'write-blog-post'",
            id="a different skill than the one approved",
        ),
        pytest.param(
            lambda m: m | {"skills": [*m["skills"], "research"]},
            "does not leave the 2 grant",
            id="two skills where one was approved",
        ),
        pytest.param(
            lambda m: m | {"skills": [m["skills"][0], m["skills"][-1]]},
            "does not leave the 2 grant",
            id="a grant quietly dropped",
        ),
    ],
)
def test_a_declaration_differing_anywhere_beyond_the_approved_line_writes_nothing(
    grantor: Callable[..., Harness],
    mutate: Callable[[dict[str, Any]], dict[str, Any]],
    expected: str,
) -> None:
    harness = grantor()
    credential = open_session_run(harness)
    before = declaration(harness)["text"]
    request_id = approved_grant(harness)

    response = put_declaration(
        harness, credential, mutate(manifest_granting(harness)), approval_request_id=request_id
    )

    assert response.status_code == 403
    assert response.json()["detail"]["error"] == "edit_not_approved"
    assert expected in response.json()["detail"]["message"]
    assert declaration(harness)["text"] == before
    assert consumed_at(harness, request_id) is None, "and the yes is still unspent"


def test_an_approved_edit_that_also_rewrites_the_soul_writes_nothing(
    grantor: Callable[..., Harness],
) -> None:
    harness = grantor()
    credential = open_session_run(harness)
    request_id = approved_grant(harness)

    response = put_declaration(
        harness,
        credential,
        manifest_granting(harness),
        soul=f"{VALID_SOUL}\n\nAnd a paragraph nobody approved.",
        approval_request_id=request_id,
    )

    assert response.status_code == 403
    assert "soul document" in response.json()["detail"]["message"]
    assert consumed_at(harness, request_id) is None


def test_a_write_that_refuses_gives_the_decision_back(
    grantor: Callable[..., Harness], write_skill: SkillWriter, tmp_path: Path
) -> None:
    """A manifest the fleet refuses must not cost a human's yes: nothing was written."""
    harness = grantor()
    credential = open_session_run(harness)
    request_id = approved_grant(harness, skill="shelf-index")

    refused = put_declaration(
        harness,
        credential,
        manifest_granting(harness, "shelf-index"),
        approval_request_id=request_id,
    )

    assert refused.status_code == 422, refused.text
    assert refused.json()["detail"]["error"] == "manifest_invalid"
    assert consumed_at(harness, request_id) is None

    write_skill("shelf-index", root=tmp_path / "skills")
    accepted = put_declaration(
        harness,
        credential,
        manifest_granting(harness, "shelf-index"),
        approval_request_id=request_id,
    )

    assert accepted.status_code == 200, accepted.text
    assert consumed_at(harness, request_id), "and the same yes still covered the same edit"


def test_losing_the_claim_between_the_match_and_the_write_writes_nothing(
    grantor: Callable[..., Harness], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The match reads the decision as unspent; the claim is what actually settles it.

    Between the two, another write of the same resident can spend it. Steward has to
    believe the claim rather than the read it made a moment earlier, or the same yes
    authorises two edits.
    """
    harness = grantor()
    credential = open_session_run(harness)
    request_id = approved_grant(harness)
    before = declaration(harness)["text"]
    monkeypatch.setattr(harness.store, "consume_approval", lambda *_a, **_k: False)

    response = put_declaration(
        harness, credential, manifest_granting(harness), approval_request_id=request_id
    )

    assert response.status_code == 403
    assert response.json()["detail"]["error"] == "edit_not_approved"
    assert "spent on another write" in response.json()["detail"]["message"]
    assert declaration(harness)["text"] == before
    assert [row.outcome for row in harness.store.export_request_history()] == [
        "refused: edit_not_approved"
    ]


def test_a_write_that_dies_on_something_unnamed_gives_the_decision_back_too(
    grantor: Callable[..., Harness], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The authoring seam rolls the tree back for any `Exception`; the claim follows it.

    Wiring the release to `AuthoringError` alone would leave a git call that died, or an
    `OSError` under the tree, spending a human's yes on a declaration nobody changed.
    """
    harness = grantor()
    credential = open_session_run(harness)
    request_id = approved_grant(harness)
    before = declaration(harness)["text"]

    def die(*_args: object, **_kwargs: object) -> None:
        raise OSError("the disk went away mid-write")

    monkeypatch.setattr(au, "write_declaration", die)
    with pytest.raises(OSError, match="disk went away"):
        put_declaration(
            harness, credential, manifest_granting(harness), approval_request_id=request_id
        )

    assert consumed_at(harness, request_id) is None
    assert declaration(harness)["text"] == before


def test_the_grant_alone_writes_nothing(grantor: Callable[..., Harness]) -> None:
    """The manifest key buys the route. A human's yes is what buys the write."""
    harness = grantor()
    credential = open_session_run(harness)
    before = declaration(harness)["text"]

    response = put_declaration(harness, credential, manifest_granting(harness))

    assert response.status_code == 403
    assert response.json()["detail"]["error"] == "session_credential_forbidden"
    assert "named no approved request" in response.json()["detail"]["message"]
    assert declaration(harness)["text"] == before
    assert harness.store.export_request_history() == []


def test_an_approval_does_not_help_a_session_that_holds_no_grant(
    writable: Callable[..., Harness],
) -> None:
    harness = writable()
    credential = open_session_run(harness)
    request_id = approved_grant(harness)
    before = declaration(harness)["text"]

    response = put_declaration(
        harness, credential, manifest_granting(harness), approval_request_id=request_id
    )

    assert response.status_code == 403
    assert response.json()["detail"]["error"] == "session_credential_forbidden"
    assert "residents.grant_skill" in response.json()["detail"]["message"]
    assert declaration(harness)["text"] == before
    assert consumed_at(harness, request_id) is None


def test_a_human_needs_no_approval_and_steward_will_not_spend_one_for_them(
    grantor: Callable[..., Harness],
) -> None:
    harness = grantor()
    request_id = approved_grant(harness)
    candidate = yaml.safe_dump(manifest_granting(harness), sort_keys=False)

    presented = harness.client.put(
        "/residents/test-agent/declaration",
        json={"text": candidate, "approval_request_id": request_id},
    )
    plain = harness.client.put("/residents/test-agent/declaration", json={"text": candidate})

    assert presented.status_code == 422
    assert presented.json()["detail"]["error"] == "approval_not_needed"
    assert plain.status_code == 200, plain.text
    assert plain.json()["approval"] is None
    assert consumed_at(harness, request_id) is None


def test_a_session_may_still_read_the_declaration_it_is_bound_by(
    writable: Callable[..., Harness],
) -> None:
    """Reads stay open: a resident that could not see its own charter could not follow it."""
    harness = writable()
    credential = open_session_run(harness)

    response = harness.client.get(
        "/residents/test-agent/declaration", headers=as_session(credential)
    )

    assert response.status_code == 200
