"""API behavior: skill writes."""

import threading
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import pytest

from steward import authoring as au
from support.api import (
    NEW_SKILL,
    ApiFactory,
    Harness,
)
from support.api import (
    api as api,  # noqa: PLC0414 — pytest fixture discovery
)
from support.api import (
    writable as writable,  # noqa: PLC0414 — pytest fixture discovery
)


def test_a_skill_written_over_http_is_visible_at_once(writable: Callable[..., Harness]) -> None:
    """The library used to be read once at startup, so a fresh skill would have been invisible."""
    harness = writable()

    created = harness.client.post("/skills", json=NEW_SKILL)

    assert created.status_code == 201
    assert created.json()["commit"]["committed"]
    assert "triage" in {skill["name"] for skill in harness.client.get("/skills").json()["skills"]}


def test_one_skill_is_readable_with_the_revision_to_edit_against(
    writable: Callable[..., Harness],
) -> None:
    """The GET a form loads before it lets anybody type."""
    harness = writable()
    harness.client.post("/skills", json=NEW_SKILL)

    body = harness.client.get("/skills/triage").json()

    assert body["description"] == NEW_SKILL["description"]
    assert body["body"].strip() == NEW_SKILL["body"]
    assert body["revision"].startswith("sha256:")


def test_adding_a_skill_that_exists_is_refused_rather_than_overwriting(
    writable: Callable[..., Harness],
) -> None:
    """Refuse rather than overwrite: add and rewrite must not be the same button."""
    harness = writable()
    harness.client.post("/skills", json=NEW_SKILL)

    again = harness.client.post("/skills", json=NEW_SKILL | {"body": "Something else entirely."})

    assert again.status_code == 409
    assert again.json()["detail"]["error"] == "skill_exists"
    assert harness.client.get("/skills/triage").json()["body"].startswith("Read every message")


def test_a_skill_is_replaced_by_put(writable: Callable[..., Harness]) -> None:
    """Full replacement, like the declaration: read it, change it, write it back."""
    harness = writable()
    harness.client.post("/skills", json=NEW_SKILL)

    response = harness.client.put(
        "/skills/triage",
        json={"description": "Sort the inbox, gently.", "body": "Read. Answer. Escalate."},
    )

    assert response.status_code == 200
    assert harness.client.get("/skills/triage").json()["description"] == "Sort the inbox, gently."


def test_skill_successor_waits_for_failed_writer_rollback(
    writable: Callable[..., Harness], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A same-revision successor never preflights a failed writer's transient skill bytes."""
    harness = writable()
    loaded = harness.client.get("/skills/daily-summary").json()
    installed = threading.Event()
    release = threading.Event()
    original = au.write_skill

    def failing_first_git(argv: list[str]) -> au.CommandOutcome:
        if "update-ref" in argv and not installed.is_set():
            installed.set()
            assert release.wait(5)
            return au.CommandOutcome(
                tuple(argv), exit_status=1, stderr="injected publication failure"
            )
        return au.run_argv(argv)

    def injected_write(*args: Any, **kwargs: Any) -> au.WriteResult:  # noqa: ANN401
        kwargs["git"] = failing_first_git
        return original(*args, **kwargs)

    monkeypatch.setattr(au, "write_skill", injected_write)
    with ThreadPoolExecutor(max_workers=2) as pool:
        failed = pool.submit(
            harness.client.put,
            "/skills/daily-summary",
            json={
                "description": "Transient failed skill.",
                "body": "Fail.\n",
                "revision": loaded["revision"],
            },
        )
        assert installed.wait(5)
        successor = pool.submit(
            harness.client.put,
            "/skills/daily-summary",
            json={
                "description": "The successor skill.",
                "body": "Succeed.\n",
                "revision": loaded["revision"],
            },
        )
        assert not successor.done()
        release.set()
        assert failed.result().status_code == 409
        accepted = successor.result()

    assert accepted.status_code == 200
    assert harness.client.get("/skills/daily-summary").json()["description"] == (
        "The successor skill."
    )


def test_editing_an_unknown_skill_is_404(writable: Callable[..., Harness]) -> None:
    """PUT updates a skill; POST /skills adds one."""
    response = writable().client.put(
        "/skills/nobody-wrote-this", json={"description": "d", "body": "b"}
    )

    assert response.status_code == 404


def test_an_invalid_skill_is_refused_with_diagnostics(writable: Callable[..., Harness]) -> None:
    """The library's own caps, surfaced as fields rather than swallowed."""
    harness = writable()

    response = harness.client.post("/skills", json=NEW_SKILL | {"body": "x" * 9_000})

    assert response.status_code == 422
    assert response.json()["detail"]["diagnostics"]
    assert harness.client.get("/skills/triage").status_code == 404


def test_a_default_skill_reaches_every_resident(writable: Callable[..., Harness]) -> None:
    """`defaults: true` is a grant to the whole fleet, and the fleet view has to show it."""
    harness = writable()

    harness.client.post("/skills", json=NEW_SKILL | {"defaults": True})

    resident = harness.client.get("/residents/test-agent").json()
    assert "triage" in resident["effective_skills"]


def test_a_skill_that_would_break_the_fleet_is_refused(api: ApiFactory, tmp_path: Path) -> None:
    """A first skill that turns every existing grant into an error must not be written.

    Creating the library is what makes grants checkable at all, so this is the one write
    whose blast radius is the entire tree — and it is refused before the directory exists.
    """
    harness = api()

    response = harness.client.post("/skills", json=NEW_SKILL)

    assert response.status_code == 422
    assert not (tmp_path / "skills").exists()
