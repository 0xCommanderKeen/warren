"""API behavior: declarations."""

import copy
import subprocess
import threading
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import pytest

from conftest import (
    SECOND_RESIDENT_UID,
    ResidentWriter,
    bare_origin,
    branch_head,
    valid_manifest,
)
from steward import authoring as au
from steward.deploy import LocalTransport
from steward.manifest import validate_tree
from support.api import (
    GRANTED_SKILLS,
    NEW_RESIDENT,
    ApiFactory,
    Harness,
    commit_tree,
    declaration,
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


def test_a_declaration_is_readable_as_text_and_as_data(writable: Callable[..., Harness]) -> None:
    """A form needs the fields; a diff needs the bytes. Both, from one call."""
    body = declaration(writable())

    assert body["manifest"]["id"] == "test-agent"
    assert "id: test-agent" in body["text"]
    assert body["soul"].startswith("---")
    assert body["revision"].startswith("sha256:")


def test_an_edited_declaration_is_written_validated_and_committed(
    writable: Callable[..., Harness],
) -> None:
    """The endpoint the whole UI write track is built on."""
    harness = writable()
    body = declaration(harness)
    body["manifest"]["summary"] = "A resident with a tidier summary."

    response = harness.client.put(
        "/residents/test-agent/declaration",
        json={"manifest": body["manifest"], "revision": body["revision"]},
    )

    assert response.status_code == 200
    assert response.json()["commit"]["committed"]
    assert declaration(harness)["manifest"]["summary"] == "A resident with a tidier summary."


BURROW_PUSH = au.PushTarget(remote="origin", branch="burrow/residents")


def harness_origin(tmp_path: Path) -> Path:
    """Give the harness's checkout — ``tmp_path`` itself — an ``origin`` beside it."""
    return bare_origin(tmp_path, tmp_path.parent / f"{tmp_path.name}-origin.git")


def test_an_accepted_write_is_pushed_to_the_burrow_branch(
    writable: Callable[..., Harness], tmp_path: Path
) -> None:
    """The commit is the record; the push keeps it off a burrow with no backup (warren#351)."""
    harness = writable(push=BURROW_PUSH)
    remote = harness_origin(tmp_path)
    body = declaration(harness)
    body["manifest"]["summary"] = "Pushed as well as committed."

    response = harness.client.put(
        "/residents/test-agent/declaration", json={"manifest": body["manifest"]}
    )

    assert response.status_code == 200
    commit = response.json()["commit"]
    assert commit["committed"]
    assert commit["pushed"] is True
    assert branch_head(remote, "burrow/residents") == commit["sha"]
    assert "pushed to origin burrow/residents" in response.json()["message"]


def test_a_write_whose_push_fails_is_still_accepted(
    writable: Callable[..., Harness], tmp_path: Path
) -> None:
    """The push is a record, not a gate: it can never fail a save that is already on disk."""
    harness = writable(push=BURROW_PUSH)
    subprocess.run(  # noqa: S603
        ["git", "-C", str(tmp_path), "remote", "add", "origin", str(tmp_path / "nowhere.git")],  # noqa: S607
        check=True,
        capture_output=True,
    )
    body = declaration(harness)
    body["manifest"]["summary"] = "Committed here, and only here."

    response = harness.client.put(
        "/residents/test-agent/declaration", json={"manifest": body["manifest"]}
    )

    assert response.status_code == 200
    commit = response.json()["commit"]
    assert commit["committed"]
    assert commit["pushed"] is False
    assert "NOT pushed to origin burrow/residents" in response.json()["message"]
    assert declaration(harness)["manifest"]["summary"] == "Committed here, and only here."


def test_a_write_with_no_push_configured_says_nothing_about_pushing(
    writable: Callable[..., Harness],
) -> None:
    """``pushed: null`` — a steward with no remote is not a steward whose push failed."""
    harness = writable()
    body = declaration(harness)
    body["manifest"]["summary"] = "Local checkout only."

    response = harness.client.put(
        "/residents/test-agent/declaration", json={"manifest": body["manifest"]}
    )

    assert response.status_code == 200
    assert response.json()["commit"]["pushed"] is None
    assert "pushed" not in response.json()["message"]


def test_a_declared_resident_is_pushed_like_any_other_write(
    api: ApiFactory, tmp_path: Path
) -> None:
    """``POST /residents`` commits through authoring, so its commit is pushed the same way."""
    harness = api(transport=LocalTransport(root=tmp_path / "nas"), push=BURROW_PUSH)
    remote = harness_origin(tmp_path)

    response = harness.client.post("/residents", json=NEW_RESIDENT)

    assert response.status_code == 201, response.text
    commit = response.json()["commit"]
    assert commit["committed"]
    assert commit["pushed"] is True
    assert branch_head(remote, "burrow/residents") == commit["sha"]


@pytest.mark.usefixtures("village")
def test_a_retirement_is_pushed_after_its_commit(api: ApiFactory, tmp_path: Path) -> None:
    """The nursery commits the mark; the API then pushes it, and the response says so."""
    harness = api(transport=LocalTransport(root=tmp_path / "nas"), push=BURROW_PUSH)
    commit_tree(tmp_path)
    remote = harness_origin(tmp_path)

    rehearsal = harness.client.post("/residents/test-agent/retire", json={"dry_run": True}).json()
    response = harness.client.post(
        "/residents/test-agent/retire", json={"revision": rehearsal["revision"]}
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["commit"]
    assert body["push"] == {
        "pushed": True,
        "remote": "origin",
        "branch": "burrow/residents",
        "note": "pushed to origin burrow/residents",
    }
    assert branch_head(remote, "burrow/residents") == body["commit"]
    assert "pushed to origin burrow/residents" in body["message"]


@pytest.mark.usefixtures("village")
def test_a_retirement_with_nothing_to_push_carries_no_push(api: ApiFactory, tmp_path: Path) -> None:
    """A dry run commits nothing, so there is nothing to push and ``push`` says so with null."""
    harness = api(transport=LocalTransport(root=tmp_path / "nas"), push=BURROW_PUSH)
    commit_tree(tmp_path)
    harness_origin(tmp_path)

    response = harness.client.post("/residents/test-agent/retire", json={"dry_run": True})

    assert response.status_code == 200, response.text
    assert response.json()["commit"] is None
    assert response.json()["push"] is None


def test_a_declaration_can_be_written_as_text_so_comments_survive(
    writable: Callable[..., Harness],
) -> None:
    """A manifest people edit carries comments; a round trip through a model destroys them."""
    harness = writable()
    body = declaration(harness)
    commented = "# why this resident exists\n" + body["text"]

    response = harness.client.put("/residents/test-agent/declaration", json={"text": commented})

    assert response.status_code == 200
    assert "# why this resident exists" in declaration(harness)["text"]


def test_an_invalid_edit_is_refused_with_the_field_that_was_wrong(
    writable: Callable[..., Harness],
) -> None:
    """A UI has to be able to put a red border round one input."""
    harness = writable()
    body = declaration(harness)
    body["manifest"]["charter"]["mission"] = "m" * 3_000

    response = harness.client.put(
        "/residents/test-agent/declaration", json={"manifest": body["manifest"]}
    )

    assert response.status_code == 422
    detail = response.json()["detail"]
    assert detail["error"] == "manifest_invalid"
    assert "charter.mission" in {d["field"] for d in detail["diagnostics"]}
    assert declaration(harness)["manifest"]["charter"]["mission"] != "m" * 3_000


def test_a_refused_edit_commits_nothing_and_logs_the_refusal(
    writable: Callable[..., Harness],
) -> None:
    """Refusals are immediate and specific, and leave the resident exactly as it was."""
    harness = writable()
    body = declaration(harness)
    before = body["text"]
    body["manifest"]["summary"] = "s" * 5_000

    harness.client.put("/residents/test-agent/declaration", json={"manifest": body["manifest"]})

    assert declaration(harness)["text"] == before
    assert [r.outcome for r in harness.store.export_request_history()] == [
        "refused: manifest_invalid"
    ]


def test_two_editors_are_told_rather_than_one_silently_winning(
    writable: Callable[..., Harness],
) -> None:
    """The revision a form loaded is what makes a lost update visible instead of invisible."""
    harness = writable()
    first = declaration(harness)
    second = declaration(harness)
    first["manifest"]["summary"] = "The first editor's summary."
    harness.client.put(
        "/residents/test-agent/declaration",
        json={"manifest": first["manifest"], "revision": first["revision"]},
    )

    second["manifest"]["summary"] = "The second editor's summary."
    response = harness.client.put(
        "/residents/test-agent/declaration",
        json={"manifest": second["manifest"], "revision": second["revision"]},
    )

    assert response.status_code == 409
    assert response.json()["detail"]["error"] == "stale_revision"
    assert declaration(harness)["manifest"]["summary"] == "The first editor's summary."


def test_declaration_successor_waits_for_failed_writer_rollback(
    writable: Callable[..., Harness], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A same-revision successor never preflights a failed writer's transient declaration."""
    harness = writable()
    loaded = declaration(harness)
    first = copy.deepcopy(loaded["manifest"])
    second = copy.deepcopy(loaded["manifest"])
    first["summary"] = "Transient bytes from the failed writer."
    second["summary"] = "The successor's committed declaration."
    installed = threading.Event()
    release = threading.Event()
    original = au.write_declaration

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

    monkeypatch.setattr(au, "write_declaration", injected_write)
    with ThreadPoolExecutor(max_workers=2) as pool:
        failed = pool.submit(
            harness.client.put,
            "/residents/test-agent/declaration",
            json={"manifest": first, "soul": loaded["soul"], "revision": loaded["revision"]},
        )
        assert installed.wait(5)
        successor = pool.submit(
            harness.client.put,
            "/residents/test-agent/declaration",
            json={"manifest": second, "soul": loaded["soul"], "revision": loaded["revision"]},
        )
        assert not successor.done()
        release.set()
        assert failed.result().status_code == 409
        accepted = successor.result()

    assert accepted.status_code == 200
    assert declaration(harness)["manifest"]["summary"] == second["summary"]


def test_a_declaration_needs_exactly_one_spelling(writable: Callable[..., Harness]) -> None:
    """`manifest` and `text` are two ways to say one thing; neither may silently win."""
    harness = writable()
    body = declaration(harness)

    both = harness.client.put(
        "/residents/test-agent/declaration",
        json={"manifest": body["manifest"], "text": body["text"]},
    )
    neither = harness.client.put("/residents/test-agent/declaration", json={})

    assert both.status_code == 422
    assert neither.status_code == 422


def test_editing_an_unknown_resident_is_404(writable: Callable[..., Harness]) -> None:
    """PUT updates; it does not create. POST /residents is how a resident is declared."""
    response = writable().client.put(
        "/residents/nobody/declaration", json={"manifest": {"id": "nobody"}}
    )

    assert response.status_code == 404


def test_renaming_the_soul_file_is_refused_rather_than_orphaning_it(
    writable: Callable[..., Harness],
) -> None:
    """Steward will not leave a file behind in the tree that nothing reads and nothing owns."""
    harness = writable()
    body = declaration(harness)
    body["manifest"]["soul"]["file"] = "identity.md"

    response = harness.client.put(
        "/residents/test-agent/declaration", json={"manifest": body["manifest"]}
    )

    assert response.status_code == 409
    assert response.json()["detail"]["error"] == "soul_file_changed"


@pytest.mark.parametrize("field", ["uid", "agent_id"])
def test_declaration_put_cannot_replace_resident_identity(
    writable: Callable[..., Harness], field: str
) -> None:
    harness = writable()
    body = declaration(harness)
    body["manifest"][field] = SECOND_RESIDENT_UID if field == "uid" else "resident:replacement"

    response = harness.client.put(
        "/residents/test-agent/declaration", json={"manifest": body["manifest"]}
    )

    assert response.status_code == 409
    assert response.json()["detail"]["error"] == "resident_identity_changed"


# -- reload ----------------------------------------------------------------------------


def test_reload_re_reads_the_tree_into_this_process(writable: Callable[..., Harness]) -> None:
    """The API's own long-lived collaborators, refreshed without a restart."""
    harness = writable()

    response = harness.client.post("/reload")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "reloaded"
    assert body["residents"] == 1
    assert sorted(body["skills"]) == list(GRANTED_SKILLS)


def test_reload_picks_up_a_routine_added_since_startup(
    writable: Callable[..., Harness], write_resident: ResidentWriter, tmp_path: Path
) -> None:
    """The run-now scheduler was assembled at startup and would fire the old manifest."""
    harness = writable()
    data = copy.deepcopy(valid_manifest())
    data["routines"].append(
        {
            "id": "second-routine",
            "schedule": "0 9 * * *",
            "prompt": "Do the other thing.",
            "timeout_s": 600,
        }
    )
    write_resident(data, root=tmp_path / "residents")

    assert harness.client.post("/reload").status_code == 200
    assert harness.client.app.state.runs.scheduler.scheduled
    assert "second-routine" in {
        item.routine.id for item in harness.client.app.state.runs.scheduler.scheduled
    }


def test_reload_refuses_a_tree_that_does_not_validate(
    writable: Callable[..., Harness], write_resident: ResidentWriter, tmp_path: Path
) -> None:
    """The daemon's rule, in this process too: a broken manifest must not retire the fleet."""
    harness = writable()
    before = list(harness.client.app.state.runs.scheduler.scheduled)
    broken = copy.deepcopy(valid_manifest())
    broken["charter"]["mission"] = "m" * 3_000
    write_resident(broken, root=tmp_path / "residents")

    response = harness.client.post("/reload")

    assert response.status_code == 409
    assert response.json()["detail"]["error"] == "tree_invalid"
    assert harness.client.app.state.runs.scheduler.scheduled == before
