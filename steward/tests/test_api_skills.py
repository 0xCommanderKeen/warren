"""API behavior: skills."""

import copy
import json
from typing import Any

from fastapi.testclient import TestClient

from conftest import (
    SkillWriter,
    valid_manifest,
)
from steward import notify as nf
from support.api import (
    ApiFactory,
)
from support.api import (
    api as api,  # noqa: PLC0414 — pytest fixture discovery
)

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


def test_a_resident_reports_which_tools_it_may_reach(api: ApiFactory) -> None:
    """Alongside the other capability dimensions, so burrow can render it as one.

    "Which residents are unbounded" should be one read of the fleet rather than a walk over
    five manifest files, which is the same argument that put the word `unrestricted` in the
    manifest instead of letting an absent key mean it.
    """
    unbounded = api().client.get("/residents/test-agent").json()
    assert unbounded["tools"] == "unrestricted"

    assert unbounded["workspace"] == []

    bounded = valid_manifest()
    bounded["tools"] = ["Read", "Glob"]
    bounded["workspace"] = ["/data/library/books"]
    body = api(manifest=bounded).client.get("/residents/test-agent").json()
    assert body["tools"] == ["Read", "Glob"]
    assert body["workspace"] == ["/data/library/books"]


def test_a_resident_reports_its_notification_declaration_but_never_its_topic(
    api: ApiFactory,
) -> None:
    """The declaration is a capability dimension; the derived topic is a capability."""
    silent = api().client.get("/residents/test-agent").json()
    assert silent["notifications"] == {
        "transport": None,
        "on": ["needs_human"],
        "status": "active",
        "note": None,
    }

    declared = valid_manifest()
    declared["notifications"] = {"transport": "ntfy", "on": ["needs_human"]}
    body = api(manifest=declared).client.get("/residents/test-agent").json()
    assert body["notifications"]["transport"] == "ntfy"
    assert nf.ntfy_topic(body["uid"], "pytest") not in json.dumps(body)


def test_the_skills_listing_needs_the_token_like_everything_else(api: ApiFactory) -> None:
    harness = api()
    anonymous = TestClient(harness.client.app)
    assert anonymous.get("/skills").status_code == 401
