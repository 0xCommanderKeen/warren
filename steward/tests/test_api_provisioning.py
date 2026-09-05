"""API behavior: provisioning."""

import copy
import dataclasses
import subprocess
from pathlib import Path
from typing import Any, Unpack

import pytest

from conftest import (
    valid_manifest,
)
from steward.deploy import LocalTransport
from steward.nursery import (
    NewResident,
    NurseryReport,
    RegisterStage,
    provision_resident,
    raise_resident,
)
from steward.routes.deps import (
    NurseryOptions,
    NurseryPipeline,
    ProvisionOptions,
    ProvisionPipeline,
)
from support.api import (
    NEW_RESIDENT,
    ApiFactory,
    as_session,
    open_session_run,
)
from support.api import (
    api as api,  # noqa: PLC0414 — pytest fixture discovery
)
from support.api import (
    village as village,  # noqa: PLC0414 — pytest fixture discovery
)


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
    assert (
        host.root / "docker" / "warren" / "residents" / "note-keeper" / "docker-compose.yaml"
    ).is_file()
    assert host.read("~/docker/warren/residents/note-keeper/.env") is not None


def test_a_deploy_with_nowhere_to_emit_is_a_refusal_not_a_traceback(
    api: ApiFactory, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A steward with no village address answers, rather than spilling a 500.

    `emitter_env` refuses before the transport is ever reached, and that refusal is a
    `TransportError` — which this endpoint did not catch, so `deploy: true` on a steward
    whose own environment is missing `CHRONICLE_URL` was an unhandled exception. The
    `village` fixture sets it, which is exactly why nothing here ever noticed.
    """
    monkeypatch.delenv("CHRONICLE_URL", raising=False)
    host = LocalTransport(root=tmp_path / "nas")
    harness = api(transport=host)

    response = harness.client.post("/residents", json=NEW_RESIDENT | {"deploy": True})

    assert response.status_code == 409
    assert response.json()["detail"]["error"] == "provision_refused"
    assert "CHRONICLE_URL" in response.json()["detail"]["message"]
    assert not host.touched


def test_a_refused_deploy_says_the_same_body_will_pick_up_where_it_stopped(
    api: ApiFactory, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """And the claim is true, not a comfort: the retry converges rather than colliding."""
    monkeypatch.delenv("CHRONICLE_URL", raising=False)
    host = LocalTransport(root=tmp_path / "nas")
    harness = api(transport=host)

    refused = harness.client.post("/residents", json=NEW_RESIDENT | {"deploy": True})
    assert "post the same body again" in refused.json()["detail"]["message"]

    monkeypatch.setenv("CHRONICLE_URL", "http://dxp2800:8737")
    monkeypatch.setenv("STEWARD_URL", "http://dxp2800:8802")
    retried = harness.client.post("/residents", json=NEW_RESIDENT | {"deploy": True})

    assert retried.status_code == 201
    assert retried.json()["declare"]["written"] is False, "the skeleton was already there"
    assert retried.json()["provision"]["sent"] is True
    assert (
        host.root / "docker" / "warren" / "residents" / "note-keeper" / "docker-compose.yaml"
    ).is_file()


@pytest.mark.usefixtures("village")
def test_the_api_calls_the_same_pipeline_the_cli_does(
    api: ApiFactory,
    tmp_path: Path,
    village: str,  # noqa: ARG001 — the fixture is the setup
) -> None:
    """Verified by injection, not by convention: the route is handed the pipeline."""
    seen: list[dict[str, Any]] = []

    def recorder(spec: NewResident, **kwargs: Unpack[NurseryOptions]) -> NurseryReport:
        seen.append({"spec": spec, **kwargs})
        return raise_resident(spec, **kwargs)

    host = LocalTransport(root=tmp_path / "nas")
    pipeline: NurseryPipeline = recorder
    harness = api(nursery=pipeline, transport=host)

    harness.client.post("/residents", json=NEW_RESIDENT | {"deploy": True})

    assert len(seen) == 1
    assert seen[0]["spec"].id == "note-keeper"
    assert seen[0]["provision"] is True
    assert seen[0]["transport"] is host
    # And the one setting the API always makes for itself, whatever the body says.
    assert seen[0]["commit"] is False


@pytest.mark.usefixtures("village")
def test_a_declaration_is_committed_by_steward_itself(api: ApiFactory, tmp_path: Path) -> None:
    """The reversal in steward #214: the newest declarations used to be the only unrecorded ones.

    This endpoint deliberately committed nothing, on the grounds that the server might not
    own its checkout. The cost was that every resident raised from a control panel had no
    history and no author, which is the one thing the repo-as-source-of-truth rule exists to
    provide. `declare.commit` stays null — that is the *nursery's* commit, which the API
    still asks it not to make — and the commit that did happen is its own key.
    """
    host = LocalTransport(root=tmp_path / "nas")
    harness = api(transport=host)

    response = harness.client.post("/residents", json=NEW_RESIDENT | {"deploy": True})

    assert response.json()["declare"]["commit"] is None
    commit = response.json()["commit"]
    assert commit["committed"]
    assert commit["sha"]
    subjects = subprocess.run(  # noqa: S603
        ["git", "-C", str(tmp_path), "log", "--format=%s"],  # noqa: S607
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert "declare note-keeper" in subjects


@pytest.mark.usefixtures("village")
def test_a_tree_with_no_git_refuses_to_declare_rather_than_writing_quietly(
    api: ApiFactory, tmp_path: Path
) -> None:
    """A fleet whose declarations have no history is a thing to choose, not to discover."""
    harness = api(transport=LocalTransport(root=tmp_path / "nas"), git=False)

    response = harness.client.post("/residents", json=NEW_RESIDENT)

    assert response.status_code == 409
    assert response.json()["detail"]["error"] == "not_a_git_checkout"


def test_a_deploy_leaks_no_secret_into_the_response(
    api: ApiFactory, tmp_path: Path, village: str
) -> None:
    harness = api(transport=LocalTransport(root=tmp_path / "nas"))

    response = harness.client.post("/residents", json=NEW_RESIDENT | {"deploy": True})

    assert village not in response.text
    assert response.json()["provision"]["env_keys"] == [
        "CHRONICLE_TOKEN",
        "CHRONICLE_URL",
        "STEWARD_URL",
    ]


# --------------------------------------------------------------------------------------
# POST /residents/{id}/provision — the declared manifest, built as it stands
# --------------------------------------------------------------------------------------


@pytest.mark.usefixtures("village")
def test_provisioning_builds_the_declared_manifest(api: ApiFactory, tmp_path: Path) -> None:
    """`test-agent` carries routes and app grants, so no `POST /residents` body can reach it."""
    host = LocalTransport(root=tmp_path / "nas")
    harness = api(transport=host)

    response = harness.client.post("/residents/test-agent/provision")

    assert response.status_code == 200
    body = response.json()
    assert body["act"] == "provision"
    assert body["declare"]["written"] is False
    assert body["provision"]["sent"] is True
    assert (
        host.root / "docker" / "warren" / "residents" / "test-agent" / "docker-compose.yaml"
    ).is_file()
    shipped = host.read("~/docker/warren/residents/test-agent/manifest.yaml") or ""
    assert "app_grants" in shipped


@pytest.mark.usefixtures("village")
def test_the_provision_route_runs_the_pipeline_the_command_runs(
    api: ApiFactory, tmp_path: Path
) -> None:
    """Verified by injection, not by convention — the same seam `POST /residents` has."""
    seen: list[dict[str, Any]] = []

    def recorder(resident_id: str, **kwargs: Unpack[ProvisionOptions]) -> NurseryReport:
        seen.append({"resident_id": resident_id, **kwargs})
        return provision_resident(resident_id, **kwargs)

    host = LocalTransport(root=tmp_path / "nas")
    pipeline: ProvisionPipeline = recorder
    harness = api(provisioner=pipeline, transport=host)

    harness.client.post("/residents/test-agent/provision")

    assert len(seen) == 1
    assert seen[0]["resident_id"] == "test-agent"
    assert seen[0]["transport"] is host
    assert seen[0]["dry_run"] is False


@pytest.mark.usefixtures("village")
def test_provisioning_commits_nothing_because_it_writes_nothing(
    api: ApiFactory, tmp_path: Path
) -> None:
    """No declaration is written here, so there is nothing for the write path to commit."""
    harness = api(transport=LocalTransport(root=tmp_path / "nas"))

    def commits() -> str:
        return subprocess.run(  # noqa: S603
            ["git", "-C", str(tmp_path), "log", "--format=%s"],  # noqa: S607
            capture_output=True,
            text=True,
            check=False,
        ).stdout

    before = commits()

    response = harness.client.post("/residents/test-agent/provision")

    assert "commit" not in response.json()
    assert commits() == before


@pytest.mark.usefixtures("village")
def test_a_provision_dry_run_reaches_no_host_at_all(api: ApiFactory, tmp_path: Path) -> None:
    """A rehearsal a control panel can press before the button that does it for real."""
    host = LocalTransport(root=tmp_path / "nas")
    harness = api(transport=host)

    response = harness.client.post("/residents/test-agent/provision", json={"dry_run": True})

    assert response.status_code == 200
    assert response.json()["dry_run"] is True
    assert response.json()["changed"] is False
    assert not host.touched


@pytest.mark.usefixtures("village")
def test_provisioning_an_unknown_resident_is_a_404(api: ApiFactory, tmp_path: Path) -> None:
    harness = api(transport=LocalTransport(root=tmp_path / "nas"))

    response = harness.client.post("/residents/nobody-here/provision")

    assert response.status_code == 404
    assert response.json()["detail"]["error"] == "unknown_resident"


@pytest.mark.usefixtures("village")
def test_provisioning_a_declaration_that_stopped_validating_is_refused(
    api: ApiFactory, tmp_path: Path
) -> None:
    """A manifest edited into invalidity is a 409 naming the field, not a deploy.

    Its own name, not `manifest_invalid`: that one is `422` and means *the bytes you sent*
    would not validate, and no bytes were sent here — the declaration on disk is the thing
    that is broken, and a caller cannot fix it by sending different ones.
    """
    host = LocalTransport(root=tmp_path / "nas")
    harness = api(transport=host)
    path = harness.residents_dir / "test-agent" / "manifest.yaml"
    path.write_text(
        path.read_text(encoding="utf-8").replace("#a68a4f", "not-a-colour"), encoding="utf-8"
    )

    response = harness.client.post("/residents/test-agent/provision")

    assert response.status_code == 409
    assert response.json()["detail"]["error"] == "declaration_invalid"
    assert "accent" in response.json()["detail"]["message"]
    assert not host.touched


@pytest.mark.usefixtures("village")
def test_provisioning_a_retired_resident_is_refused(api: ApiFactory, tmp_path: Path) -> None:
    """Coming back is a person's decision written into the manifest, not an HTTP call."""
    manifest = copy.deepcopy(valid_manifest())
    manifest["retired"] = True
    host = LocalTransport(root=tmp_path / "nas")
    harness = api(manifest=manifest, transport=host)

    response = harness.client.post("/residents/test-agent/provision")

    assert response.status_code == 409
    assert response.json()["detail"]["error"] == "resident_retired"
    assert not host.touched


@pytest.mark.usefixtures("village")
def test_a_host_that_answers_and_refuses_is_named_as_the_host(
    api: ApiFactory, tmp_path: Path
) -> None:
    """A host that said no is not a broken declaration, and must not borrow its name."""
    harness = api(transport=LocalTransport(root=tmp_path / "nas", fail_on="up"))

    response = harness.client.post("/residents/test-agent/provision")

    assert response.status_code == 409
    assert response.json()["detail"]["error"] == "provision_failed"
    assert "docker compose up failed" in response.json()["detail"]["message"]


def test_provisioning_with_nowhere_to_emit_is_a_refusal_not_a_traceback(
    api: ApiFactory, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("CHRONICLE_URL", raising=False)
    harness = api(transport=LocalTransport(root=tmp_path / "nas"))

    response = harness.client.post("/residents/test-agent/provision")

    assert response.status_code == 409
    assert response.json()["detail"]["error"] == "provision_refused"
    assert "CHRONICLE_URL" in response.json()["detail"]["message"]


def test_a_provision_leaks_no_secret_into_the_response(
    api: ApiFactory, tmp_path: Path, village: str
) -> None:
    harness = api(transport=LocalTransport(root=tmp_path / "nas"))

    response = harness.client.post("/residents/test-agent/provision")

    assert village not in response.text
    assert "CHRONICLE_TOKEN" in response.json()["provision"]["env_keys"]


@pytest.mark.usefixtures("village")
def test_a_provision_is_recorded_as_a_request_somebody_made(
    api: ApiFactory, tmp_path: Path
) -> None:
    harness = api(transport=LocalTransport(root=tmp_path / "nas"))

    response = harness.client.post("/residents/test-agent/provision")

    logged = harness.store.export_request_history()
    assert [record.outcome for record in logged] == ["provisioned"]
    assert response.json()["request_id"] == logged[0].request_id


@pytest.mark.usefixtures("village")
def test_a_session_may_not_provision_a_container(api: ApiFactory, tmp_path: Path) -> None:
    """Naming the act, not the neighbourhood: this is not declaring, it is building."""
    host = LocalTransport(root=tmp_path / "nas")
    harness = api(transport=host)
    credential = open_session_run(harness)

    response = harness.client.post(
        "/residents/test-agent/provision", headers=as_session(credential)
    )

    assert response.status_code == 403
    assert "starting a container on a machine" in response.json()["detail"]["message"]
    assert not host.touched


@pytest.mark.usefixtures("village")
def test_provisioning_the_same_manifest_twice_converges(api: ApiFactory, tmp_path: Path) -> None:
    """The bundle on the host is compared, not re-sent — and the container is reconciled."""
    host = LocalTransport(root=tmp_path / "nas")
    harness = api(transport=host)
    harness.client.post("/residents/test-agent/provision")

    response = harness.client.post("/residents/test-agent/provision")

    assert response.status_code == 200
    assert response.json()["changed"] is False
    assert response.json()["provision"]["sent"] is False
    assert host.calls[-1][-2:] == ("up", "-d")
    # Both halves, whatever the schedule check said: "nothing was sent" and the outcome of
    # the run are two facts, and this machine may or may not have a `claude` to find.
    message = response.json()["message"]
    assert message.startswith("converged: the host already had this bundle")
    assert "the container is up" in message


@pytest.mark.usefixtures("village")
def test_a_container_that_went_up_with_a_failing_check_says_both_halves(
    api: ApiFactory, tmp_path: Path
) -> None:
    """Saying only "the container is up" would be a control panel's one unforgivable sin."""

    def unschedulable(resident_id: str, **kwargs: Unpack[ProvisionOptions]) -> NurseryReport:
        report = provision_resident(resident_id, **kwargs)
        return dataclasses.replace(
            report, register=RegisterStage(problems=("claude is not on PATH",))
        )

    pipeline: ProvisionPipeline = unschedulable
    harness = api(provisioner=pipeline, transport=LocalTransport(root=tmp_path / "nas"))

    response = harness.client.post("/residents/test-agent/provision")

    assert response.status_code == 200
    assert response.json()["register"]["ok"] is False
    assert "the schedule check did not pass" in response.json()["message"]
