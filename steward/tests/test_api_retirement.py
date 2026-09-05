"""API behavior: retirement."""

import copy
import subprocess
from pathlib import Path
from typing import Any, Unpack

import pytest

from conftest import (
    VALID_RESIDENT_UID,
    valid_manifest,
)
from steward import authoring as au
from steward.deploy import LocalTransport
from steward.manifest import validate_tree
from steward.nursery import (
    RetireReport,
    retire_resident,
)
from steward.routes.deps import (
    RetireOptions,
    RetirePipeline,
)
from support.api import (
    ApiFactory,
    as_operator,
    as_session,
    commit_tree,
    last_commit,
    mint_operator,
    open_session_run,
)
from support.api import (
    api as api,  # noqa: PLC0414 — pytest fixture discovery
)
from support.api import (
    village as village,  # noqa: PLC0414 — pytest fixture discovery
)


@pytest.mark.usefixtures("village")
def test_retiring_a_resident_with_no_container_marks_and_commits_and_says_so(
    api: ApiFactory, tmp_path: Path
) -> None:
    """`test-agent` is local-placed and was never provisioned, so there is nothing to stop.

    The report says that rather than "stopped", which is the distinction a control panel
    stands on: a resident that ships inside the daemon image is marked and committed here
    and only stops running on the burrow's next deploy (warren#332).
    """
    host = LocalTransport(root=tmp_path / "nas")
    harness = api(transport=host)
    commit_tree(tmp_path)

    rehearsal = harness.client.post("/residents/test-agent/retire", json={"dry_run": True}).json()
    response = harness.client.post(
        "/residents/test-agent/retire", json={"revision": rehearsal["revision"]}
    )

    assert response.status_code == 200
    body = response.json()
    assert body["marked"] is True
    assert body["stopped"] is False
    assert body["scrubbed"] is False
    assert body["commit"]
    assert "nothing at" in body["note"]
    assert validate_tree(harness.residents_dir).residents[0].retired


@pytest.mark.usefixtures("village")
def test_retiring_a_container_removes_the_token_only_after_the_container_is_down(
    api: ApiFactory, tmp_path: Path
) -> None:
    """`docker compose down` reads the .env: `${CHRONICLE_URL:?…}` errors once it is gone."""
    host = LocalTransport(root=tmp_path / "nas")
    harness = api(transport=host)
    harness.client.post("/residents/test-agent/provision")
    commit_tree(tmp_path)
    host.calls.clear()

    rehearsal = harness.client.post("/residents/test-agent/retire", json={"dry_run": True}).json()
    response = harness.client.post(
        "/residents/test-agent/retire", json={"revision": rehearsal["revision"]}
    )

    assert response.status_code == 200
    body = response.json()
    assert body["stopped"] is True
    assert body["scrubbed"] is True
    down = next(index for index, call in enumerate(host.calls) if "down" in call)
    scrub = next(index for index, call in enumerate(host.calls) if call[0] == "rm")
    assert down < scrub
    assert host.calls[scrub][-2:] == (
        "~/docker/warren/residents/test-agent/.env",
        "~/docker/warren/residents/test-agent/docker-compose.yaml",
    )
    assert "CHRONICLE_TOKEN is gone" in body["message"]
    assert "claude/ still holds" in body["message"]


@pytest.mark.usefixtures("village")
def test_retiring_an_already_retired_resident_is_refused(api: ApiFactory, tmp_path: Path) -> None:
    """`steward retire` reconciles a half-finished retirement; a button does not.

    The refusal names the way back rather than only the refusal, because a control panel
    that has just been told "no" is exactly where somebody is looking for the other door.
    """
    manifest = copy.deepcopy(valid_manifest())
    manifest["retired"] = True
    host = LocalTransport(root=tmp_path / "nas")
    harness = api(manifest=manifest, transport=host)
    commit_tree(tmp_path)

    response = harness.client.post("/residents/test-agent/retire")

    assert response.status_code == 409
    detail = response.json()["detail"]
    assert detail["error"] == "resident_retired"
    assert "/residents/test-agent/provision" in detail["message"]
    assert not host.touched


@pytest.mark.usefixtures("village")
def test_a_retire_dry_run_marks_nothing_commits_nothing_and_reaches_no_host(
    api: ApiFactory, tmp_path: Path
) -> None:
    """The rehearsal the console shows before the button that does it for real."""
    host = LocalTransport(root=tmp_path / "nas")
    harness = api(transport=host)
    commit_tree(tmp_path)

    response = harness.client.post("/residents/test-agent/retire", json={"dry_run": True})

    assert response.status_code == 200
    body = response.json()
    assert body["dry_run"] is True
    assert body["commit"] is None
    assert body["commands"], "a rehearsal that showed no argv would be a drawing of the plan"
    assert not host.touched
    assert not validate_tree(harness.residents_dir).residents[0].retired


@pytest.mark.usefixtures("village")
def test_the_retire_route_runs_the_pipeline_the_command_runs(
    api: ApiFactory, tmp_path: Path
) -> None:
    """Verified by injection, not by convention — the seam both other doors have."""
    seen: list[dict[str, Any]] = []

    def recorder(resident_id: str, **kwargs: Unpack[RetireOptions]) -> RetireReport:
        seen.append({"resident_id": resident_id, **kwargs})
        return retire_resident(resident_id, **kwargs)

    host = LocalTransport(root=tmp_path / "nas")
    pipeline: RetirePipeline = recorder
    harness = api(retirer=pipeline, transport=host)
    commit_tree(tmp_path)

    rehearsal = harness.client.post("/residents/test-agent/retire", json={"dry_run": True}).json()
    harness.client.post("/residents/test-agent/retire", json={"revision": rehearsal["revision"]})

    assert [call["dry_run"] for call in seen] == [True, False]
    assert all(call["resident_id"] == "test-agent" for call in seen)
    assert all(call["transport"] is host for call in seen)
    assert seen[-1]["expected_revision"] == rehearsal["revision"]


@pytest.mark.usefixtures("village")
def test_a_uid_retires_the_resident_the_id_would_have(api: ApiFactory, tmp_path: Path) -> None:
    """Townhall addresses a resident by uid, because an id is a directory name (#112)."""
    harness = api(transport=LocalTransport(root=tmp_path / "nas"))
    commit_tree(tmp_path)

    path = f"/residents/{VALID_RESIDENT_UID}/retire"
    rehearsal = harness.client.post(path, json={"dry_run": True}).json()
    response = harness.client.post(path, json={"revision": rehearsal["revision"]})

    assert response.status_code == 200
    assert response.json()["resident"] == "test-agent"


@pytest.mark.usefixtures("village")
def test_retiring_into_a_dirty_worktree_is_refused_by_name(api: ApiFactory, tmp_path: Path) -> None:
    """The `api` fixture never commits its tree, which is exactly the state being refused.

    Named rather than described: a control panel needs to tell "somebody is mid-way through
    an edit in this checkout" apart from "the host said no", and only the pipeline that
    looked knows which.
    """
    host = LocalTransport(root=tmp_path / "nas")
    harness = api(transport=host)

    rehearsal = harness.client.post("/residents/test-agent/retire", json={"dry_run": True}).json()
    response = harness.client.post(
        "/residents/test-agent/retire", json={"revision": rehearsal["revision"]}
    )

    assert response.status_code == 409
    assert response.json()["detail"]["error"] == "worktree_refused"
    assert not host.touched
    assert not validate_tree(harness.residents_dir).residents[0].retired


@pytest.mark.usefixtures("village")
def test_retirement_ignores_unrelated_dirt_but_binds_the_target_revision(
    api: ApiFactory, tmp_path: Path
) -> None:
    harness = api(transport=LocalTransport(root=tmp_path / "nas"))
    commit_tree(tmp_path)
    rehearsal = harness.client.post("/residents/test-agent/retire", json={"dry_run": True}).json()
    (tmp_path / "unrelated.txt").write_text("somebody else's work", encoding="utf-8")

    response = harness.client.post(
        "/residents/test-agent/retire", json={"revision": rehearsal["revision"]}
    )

    assert response.status_code == 200


@pytest.mark.usefixtures("village")
def test_retirement_requires_and_binds_a_rehearsed_revision(
    api: ApiFactory, tmp_path: Path
) -> None:
    harness = api(transport=LocalTransport(root=tmp_path / "nas"))
    commit_tree(tmp_path)

    missing = harness.client.post("/residents/test-agent/retire")
    rehearsal = harness.client.post("/residents/test-agent/retire", json={"dry_run": True}).json()
    manifest = harness.residents_dir / "test-agent" / "manifest.yaml"
    manifest.write_text(manifest.read_text() + "\n# changed\n", encoding="utf-8")
    stale = harness.client.post(
        "/residents/test-agent/retire", json={"revision": rehearsal["revision"]}
    )

    assert missing.json()["detail"]["error"] == "retirement_rehearsal_required"
    assert stale.json()["detail"]["error"] == "stale_retirement_plan"
    assert not validate_tree(harness.residents_dir).residents[0].retired


@pytest.mark.usefixtures("village")
def test_a_host_that_refuses_the_stop_is_named_as_the_host(api: ApiFactory, tmp_path: Path) -> None:
    """A `docker compose down` that failed is not a broken declaration and must not say so."""
    harness = api(transport=LocalTransport(root=tmp_path / "nas", fail_on="down"))
    commit_tree(tmp_path)
    harness.client.post("/residents/test-agent/provision")
    commit_tree(tmp_path)

    rehearsal = harness.client.post("/residents/test-agent/retire", json={"dry_run": True}).json()
    response = harness.client.post(
        "/residents/test-agent/retire", json={"revision": rehearsal["revision"]}
    )

    assert response.status_code == 409
    assert response.json()["detail"]["error"] == "retire_failed"
    assert "docker compose down failed" in response.json()["detail"]["message"]
    # The mark landed and was committed before the host was reached, and the message says
    # so: unwinding it would put the resident back in the watchdog's care with a container
    # that may already be half down.
    assert validate_tree(harness.residents_dir).residents[0].retired


@pytest.mark.usefixtures("village")
def test_retiring_an_unknown_resident_is_a_404(api: ApiFactory, tmp_path: Path) -> None:
    harness = api(transport=LocalTransport(root=tmp_path / "nas"))
    commit_tree(tmp_path)

    response = harness.client.post("/residents/nobody-here/retire")

    assert response.status_code == 404
    assert response.json()["detail"]["error"] == "unknown_resident"


@pytest.mark.usefixtures("village")
def test_a_session_may_not_retire_a_resident(api: ApiFactory, tmp_path: Path) -> None:
    """Naming the act, not the neighbourhood: this is not declaring, it is ending."""
    host = LocalTransport(root=tmp_path / "nas")
    harness = api(transport=host)
    commit_tree(tmp_path)
    credential = open_session_run(harness)

    response = harness.client.post("/residents/test-agent/retire", headers=as_session(credential))

    assert response.status_code == 403
    assert "dismissing itself" in response.json()["detail"]["message"]
    assert not host.touched
    assert not validate_tree(harness.residents_dir).residents[0].retired


@pytest.mark.usefixtures("village")
def test_a_retirement_is_recorded_as_a_request_somebody_made(
    api: ApiFactory, tmp_path: Path
) -> None:
    harness = api(transport=LocalTransport(root=tmp_path / "nas"))
    commit_tree(tmp_path)

    rehearsal = harness.client.post("/residents/test-agent/retire", json={"dry_run": True}).json()
    response = harness.client.post(
        "/residents/test-agent/retire", json={"revision": rehearsal["revision"]}
    )

    logged = harness.store.export_request_history()
    assert [record.outcome for record in logged] == ["rehearsed", "retired"]
    assert response.json()["request_id"] == logged[-1].request_id


@pytest.mark.usefixtures("village")
def test_a_refused_retirement_says_so_in_the_request_log(api: ApiFactory, tmp_path: Path) -> None:
    """The row is written before the pipeline runs, because the pipeline has side effects.

    A retirement that failed part-way marked a manifest and may have stopped a container, so
    the request has to exist in the log whatever happens next — corrected to say it was
    refused rather than never recorded at all.
    """
    harness = api(transport=LocalTransport(root=tmp_path / "nas"))

    rehearsal = harness.client.post("/residents/test-agent/retire", json={"dry_run": True}).json()
    harness.client.post("/residents/test-agent/retire", json={"revision": rehearsal["revision"]})

    logged = harness.store.export_request_history()
    assert [record.outcome for record in logged] == [
        "rehearsed",
        "refused: worktree_refused",
    ]


@pytest.mark.usefixtures("village")
def test_a_retirement_that_stopped_part_way_is_not_logged_as_refused(
    api: ApiFactory, tmp_path: Path
) -> None:
    """The mark is committed by the time the host is reached, and the log has to say so.

    "Refused" over a request that left a commit in git is the one row an audit trail cannot
    recover from: it says nothing happened, while the resident has already stopped taking
    work.
    """
    harness = api(transport=LocalTransport(root=tmp_path / "nas", fail_on="down"))
    commit_tree(tmp_path)
    harness.client.post("/residents/test-agent/provision")
    commit_tree(tmp_path)

    rehearsal = harness.client.post("/residents/test-agent/retire", json={"dry_run": True}).json()
    harness.client.post("/residents/test-agent/retire", json={"revision": rehearsal["revision"]})

    outcomes = [record.outcome for record in harness.store.export_request_history()]
    assert outcomes[-1] == "stopped part-way: retire_failed"


@pytest.mark.usefixtures("village")
def test_a_retirement_whose_commit_git_refused_says_which_side_it_stopped_on(
    api: ApiFactory, tmp_path: Path
) -> None:
    """The mark is on disk and in no commit — a different situation from never starting.

    `retire_failed` would say the host refused, which is the wrong place to send somebody:
    the host was never reached. The refusal names the file, says the resident has already
    stopped taking work, and gives both ways out.
    """
    host = LocalTransport(root=tmp_path / "nas")

    def unable_to_commit(resident_id: str, **kwargs: Unpack[RetireOptions]) -> RetireReport:
        def git(argv: list[str]) -> au.CommandOutcome:
            if "commit" in argv:
                return au.CommandOutcome(argv=tuple(argv), exit_status=1, stderr="gpg failed")
            return au.run_argv(argv)

        return retire_resident(resident_id, git=git, **kwargs)

    pipeline: RetirePipeline = unable_to_commit
    harness = api(retirer=pipeline, transport=host)
    commit_tree(tmp_path)

    rehearsal = harness.client.post("/residents/test-agent/retire", json={"dry_run": True}).json()
    response = harness.client.post(
        "/residents/test-agent/retire", json={"revision": rehearsal["revision"]}
    )

    assert response.status_code == 409
    detail = response.json()["detail"]
    assert detail["error"] == "commit_failed"
    assert "nothing has committed it" in detail["message"]
    assert "the container was not touched" in detail["message"]
    # The mark is on disk, the host was never reached, and the log says part-way rather
    # than refused.
    assert validate_tree(harness.residents_dir).residents[0].retired
    assert not host.touched
    assert harness.store.export_request_history()[-1].outcome == "stopped part-way: commit_failed"


@pytest.mark.usefixtures("village")
def test_an_operator_retirement_is_committed_by_the_operator(
    api: ApiFactory, tmp_path: Path
) -> None:
    """A server has nobody at the keyboard, so the identity has to be passed, not inherited.

    Without one the commit is made with whatever `git config` the process happens to see —
    which on a control-plane host is nothing at all, and steward would mark the manifest
    perfectly and then fail with "Please tell me who you are".
    """
    harness = api(transport=LocalTransport(root=tmp_path / "nas"))
    commit_tree(tmp_path)
    credential = mint_operator(harness, name="Miha")

    headers = as_operator(credential)
    rehearsal = harness.client.post(
        "/residents/test-agent/retire", json={"dry_run": True}, headers=headers
    ).json()
    response = harness.client.post(
        "/residents/test-agent/retire",
        json={"revision": rehearsal["revision"]},
        headers=headers,
    )

    assert response.status_code == 200
    assert "Miha <" in last_commit(harness)
    assert (
        "chore(residents): retire test-agent"
        in subprocess.run(
            ["git", "log", "-1", "--format=%s"],  # noqa: S607
            cwd=harness.residents_dir.parent,
            capture_output=True,
            text=True,
            check=True,
        ).stdout
    )


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
