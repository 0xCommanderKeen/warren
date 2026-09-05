"""API behavior: residents."""

import copy

import pytest

from conftest import (
    SECOND_RESIDENT_UID,
    VALID_RESIDENT_UID,
    VALID_SOUL,
    ResidentWriter,
    valid_manifest,
)
from support.api import (
    ApiFactory,
)
from support.api import (
    api as api,  # noqa: PLC0414 — pytest fixture discovery
)

# --------------------------------------------------------------------------------------
# residents
# --------------------------------------------------------------------------------------


def test_residents_are_listed_with_the_brain_they_run_on(api: ApiFactory) -> None:
    harness = api()
    body = harness.client.get("/residents").json()

    assert [resident["id"] for resident in body["residents"]] == ["test-agent"]
    assert body["residents"][0]["runner"] == {"kind": "claude", "model": "claude-opus-5"}
    assert body["errors"] == []


def test_a_broken_manifest_is_named_rather_than_hidden(api: ApiFactory) -> None:
    broken = copy.deepcopy(valid_manifest())
    del broken["memory"]
    harness = api(manifest=broken)
    body = harness.client.get("/residents").json()

    assert body["residents"] == []
    assert "memory" in body["errors"][0]


def test_an_invalid_utf8_soul_is_named_rather_than_crashing(api: ApiFactory) -> None:
    harness = api()
    soul_path = harness.residents_dir / "test-agent" / "soul.md"
    soul_path.write_bytes(b"\xff\xfe")

    response = harness.client.get("/residents")

    assert response.status_code == 200
    assert response.json()["residents"] == []
    assert str(soul_path) in response.json()["errors"][0]
    assert "soul file is not valid UTF-8" in response.json()["errors"][0]


def test_one_resident_is_served_whole(api: ApiFactory) -> None:
    harness = api()
    body = harness.client.get("/residents/test-agent").json()

    assert body["uid"] == VALID_RESIDENT_UID
    assert body["agent_id"] == "claude-code:test-agent"
    assert body["soul"]["name"] == "Testy"
    assert body["charter"]["mission"]
    assert [routine["id"] for routine in body["routines"]] == ["daily-summary"]
    assert body["memory"]["path"] == "/data/residents/test-agent/memory"


def test_an_unknown_resident_is_404(api: ApiFactory) -> None:
    harness = api()
    assert harness.client.get("/residents/nobody").status_code == 404


def test_a_resident_answers_to_its_uid_as_well_as_its_id(api: ApiFactory) -> None:
    """The durable name reaches the same resident the directory name does."""
    harness = api()

    by_id = harness.client.get("/residents/test-agent")
    by_uid = harness.client.get(f"/residents/{VALID_RESIDENT_UID}")

    assert by_uid.status_code == 200
    assert by_uid.json() == by_id.json()


@pytest.mark.parametrize("suffix", ["", "/budget", "/journal", "/inbox"])
def test_every_resident_read_accepts_a_uid(api: ApiFactory, suffix: str) -> None:
    """One lookup serves every resident route, so none of them is uid-blind."""
    harness = api()

    by_id = harness.client.get(f"/residents/test-agent{suffix}")
    by_uid = harness.client.get(f"/residents/{VALID_RESIDENT_UID}{suffix}")

    assert by_id.status_code == 200
    assert by_uid.status_code == 200
    assert by_uid.json() == by_id.json()


def test_run_now_accepts_a_uid(api: ApiFactory) -> None:
    """The one write path on a resident is addressable the same way the reads are."""
    harness = api()

    response = harness.client.post(f"/residents/{VALID_RESIDENT_UID}/routines/daily-summary/run")

    assert response.status_code == 202
    assert response.json()["request_id"]


def test_an_unknown_uid_is_404(api: ApiFactory) -> None:
    harness = api()
    response = harness.client.get("/residents/3a78217a-df03-4f3b-a46a-4c75b4ad929f")

    assert response.status_code == 404
    assert response.json()["detail"]["error"] == "unknown_resident"


def test_an_id_wins_over_another_residents_uid(
    api: ApiFactory, write_resident: ResidentWriter
) -> None:
    """Ids are matched first and exhaustively, so no working link can change meaning.

    A uuid is a valid resident id — lowercase hex and hyphens is a legal slug — so a
    directory could be named exactly what another resident's uid spells. Contrived, but
    it is the case that decides the precedence rule, and the rule has to be the one that
    cannot break a caller that works today.
    """
    harness = api()
    impostor = valid_manifest()
    impostor["uid"] = SECOND_RESIDENT_UID
    impostor["id"] = VALID_RESIDENT_UID  # this resident's *id* is test-agent's uid
    impostor["agent_id"] = "claude-code:impostor"
    write_resident(
        impostor,
        # The soul's frontmatter has to name the same identity the manifest does, or this
        # resident never validates and the test would pass for the wrong reason.
        soul=VALID_SOUL.replace("claude-code:test-agent", "claude-code:impostor"),
        root=harness.residents_dir,
    )

    body = harness.client.get(f"/residents/{VALID_RESIDENT_UID}").json()

    assert body["id"] == VALID_RESIDENT_UID
    assert body["uid"] == SECOND_RESIDENT_UID
    assert body["agent_id"] == "claude-code:impostor"
