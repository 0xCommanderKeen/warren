"""``GET /secrets`` and ``PUT /secrets/{name}`` — the write path that replaces an ssh paste.

The rule the whole endpoint is built around: a value goes **in** and never comes back out.
There is no read route for one, no value in the listing, no value in an event, and no value
in the request log — so the tests here mostly assert on what is *absent*.
"""

from __future__ import annotations

import stat
import subprocess
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from conftest import ResidentWriter, valid_manifest
from steward import events as ev
from steward import secrets as sec
from steward.api import ApiConfig, create_app
from steward.runners import MockRunner
from steward.runs import RUN_ROUTINE
from steward.session_auth import new_session_credential
from steward.store import Store

TOKEN = "a-shared-secret"
AUTH = {"Authorization": f"Bearer {TOKEN}"}
FAKE_BOT_TOKEN = "123456789:AAHfake-token-for-tests-only-nothing-real"


def chat_manifest(memory: Path) -> dict[str, Any]:
    """Build a manifest with one live Telegram route, so a token has a slot to claim."""
    data = valid_manifest()
    memory.mkdir(parents=True, exist_ok=True)
    data["memory"] = {"kind": "directory", "path": str(memory), "journal": "journal"}
    data["routes"] = [
        *data["routes"],
        {"id": "chat", "kind": "chat", "address": "telegram:testy", "status": "active"},
    ]
    return data


@dataclass
class Vault:
    """One built app, the store behind it, and the village it emits into."""

    client: TestClient
    store: Store
    events_path: Path
    secrets_dir: Path

    def events(self, event_type: str) -> list[dict[str, Any]]:
        """Return the events of one type this app actually emitted."""
        import json  # noqa: PLC0415 — one reader, kept beside the only thing that parses

        if not self.events_path.is_file():
            return []
        lines = self.events_path.read_text(encoding="utf-8").splitlines()
        return [
            event
            for event in (json.loads(line) for line in lines if line.strip())
            if event["type"] == event_type
        ]

    def session(self, resident_id: str = "test-agent") -> dict[str, str]:
        """Register a live run the way the scheduler does and return its auth header."""
        credential = new_session_credential()
        assert self.store.open_run(
            run_id="run-1",
            kind=RUN_ROUTINE,
            trigger="schedule",
            agent_id=f"claude-code:{resident_id}",
            project=resident_id,
            ref="daily-summary",
            resident_id=resident_id,
            session_credential=credential,
            timeout_s=900.0,
        )
        return {"Authorization": f"Bearer {credential}"}


type VaultFactory = Any


@pytest.fixture
def vault(
    tmp_path: Path, write_resident: ResidentWriter, monkeypatch: pytest.MonkeyPatch
) -> Iterator[VaultFactory]:
    """Build an app whose secrets directory is a scratch one, never the burrow's mount."""
    built: list[Vault] = []
    directory = tmp_path / "secrets-mount"
    monkeypatch.setenv(sec.SECRETS_DIR_ENV, str(directory))

    def _make(*, chat: bool = False) -> Vault:
        residents_dir = tmp_path / "residents"
        residents_dir.mkdir(exist_ok=True)
        declared = chat_manifest(tmp_path / "memory") if chat else valid_manifest()
        write_resident(declared, root=residents_dir)
        # A real checkout around the tree: since steward #214 every accepted write is
        # committed, and a harness with no git would test the fallback rather than this.
        subprocess.run(  # noqa: S603
            ["git", "-C", str(tmp_path), "init", "-b", "main"],  # noqa: S607
            check=True,
            capture_output=True,
        )
        events_path = tmp_path / "events.jsonl"
        store = Store(":memory:")
        app = create_app(
            ApiConfig(residents_dir=residents_dir, token=TOKEN, workdir=tmp_path),
            store=store,
            emitter=ev.EventEmitter(url=None, fallback=events_path),
            runner_factory=MockRunner,
        )
        made = Vault(
            client=TestClient(app, headers=dict(AUTH)),
            store=store,
            events_path=events_path,
            secrets_dir=directory,
        )
        built.append(made)
        return made

    yield _make

    for made in built:
        made.client.app.state.runs.shutdown()
        made.store.close()


# -- writing ---------------------------------------------------------------------------


def test_a_put_writes_a_private_file_and_says_only_that_it_did(vault: VaultFactory):
    harness = vault(chat=True)

    response = harness.client.put(
        "/secrets/STEWARD_CHAT_TOKEN_TESTY", json={"value": FAKE_BOT_TOKEN}
    )

    assert response.status_code == 200
    body = response.json()
    assert body["name"] == "STEWARD_CHAT_TOKEN_TESTY"
    assert body["set"] is True
    assert FAKE_BOT_TOKEN not in response.text
    path = harness.secrets_dir / "STEWARD_CHAT_TOKEN_TESTY"
    assert path.read_text(encoding="utf-8") == FAKE_BOT_TOKEN
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_a_put_records_the_name_and_never_the_value(vault: VaultFactory):
    """The event and the request log both exist so an operator can audit a rotation."""
    harness = vault(chat=True)

    harness.client.put("/secrets/STEWARD_CHAT_TOKEN_TESTY", json={"value": FAKE_BOT_TOKEN})

    [event] = harness.events("secret_written")
    assert event["payload"] == {"secret": "STEWARD_CHAT_TOKEN_TESTY"}
    assert FAKE_BOT_TOKEN not in harness.events_path.read_text(encoding="utf-8")
    [logged] = harness.store.requests()
    assert logged.outcome == "secret_written"
    assert logged.path == "/secrets/STEWARD_CHAT_TOKEN_TESTY"
    assert logged.detail == {"secret": "STEWARD_CHAT_TOKEN_TESTY"}
    assert FAKE_BOT_TOKEN not in repr(logged)


def test_a_put_replaces_an_existing_secret(vault: VaultFactory):
    harness = vault(chat=True)

    harness.client.put("/secrets/STEWARD_CHAT_TOKEN_TESTY", json={"value": "first"})
    response = harness.client.put(
        "/secrets/STEWARD_CHAT_TOKEN_TESTY", json={"value": FAKE_BOT_TOKEN}
    )

    assert response.status_code == 200
    assert (harness.secrets_dir / "STEWARD_CHAT_TOKEN_TESTY").read_text(
        encoding="utf-8"
    ) == FAKE_BOT_TOKEN


@pytest.mark.parametrize("name", ["lower_case", "WITH-DASH", "TRAILING_", "A" * 200])
def test_a_name_that_is_not_a_slot_is_refused(vault: VaultFactory, name: str):
    harness = vault()

    response = harness.client.put(f"/secrets/{name}", json={"value": FAKE_BOT_TOKEN})

    assert response.status_code == 422
    assert response.json()["detail"]["error"] == "invalid_secret_name"
    assert not harness.secrets_dir.exists()


def test_a_traversal_in_the_name_never_reaches_the_filesystem(vault: VaultFactory):
    harness = vault()

    escaped = harness.client.put("/secrets/..%2F..%2Fescaped", json={"value": FAKE_BOT_TOKEN})
    dotted = harness.client.put("/secrets/..", json={"value": FAKE_BOT_TOKEN})

    assert escaped.status_code in {404, 422}
    assert dotted.status_code in {404, 422}
    assert not (harness.secrets_dir.parent / "escaped").exists()
    assert harness.store.requests() == []


@pytest.mark.parametrize(
    ("value", "error"),
    [
        ("   ", "invalid_secret_value"),
        ("one\ntwo", "invalid_secret_value"),
    ],
)
def test_a_value_that_is_not_one_credential_is_refused(vault: VaultFactory, value: str, error: str):
    harness = vault()

    response = harness.client.put("/secrets/STEWARD_CHAT_TOKEN_TESTY", json={"value": value})

    assert response.status_code == 422
    detail = response.json()["detail"]
    assert detail == {"error": error, "message": detail["message"]} or "value" in str(detail)
    assert not (harness.secrets_dir / "STEWARD_CHAT_TOKEN_TESTY").exists()


def test_a_value_longer_than_any_credential_is_refused(vault: VaultFactory):
    harness = vault()

    response = harness.client.put(
        "/secrets/STEWARD_CHAT_TOKEN_TESTY", json={"value": "x" * (sec.VALUE_MAX_CHARS + 1)}
    )

    assert response.status_code == 422
    assert not (harness.secrets_dir / "STEWARD_CHAT_TOKEN_TESTY").exists()


def test_a_refusal_never_quotes_the_value_it_refused(vault: VaultFactory):
    """A 422 that echoed the input would put the credential in a response body."""
    harness = vault()
    oversized = FAKE_BOT_TOKEN * 300

    response = harness.client.put("/secrets/STEWARD_CHAT_TOKEN_TESTY", json={"value": oversized})

    assert response.status_code == 422
    assert FAKE_BOT_TOKEN not in response.text
    assert harness.store.requests() == []


def test_an_unknown_body_key_is_refused_rather_than_ignored(vault: VaultFactory):
    harness = vault()

    response = harness.client.put(
        "/secrets/STEWARD_CHAT_TOKEN_TESTY", json={"value": "v", "name": "other"}
    )

    assert response.status_code == 422


# -- the listing -----------------------------------------------------------------------


def test_the_listing_names_the_slot_a_declared_route_wants(vault: VaultFactory):
    harness = vault(chat=True)

    body = harness.client.get("/secrets").json()

    assert body["directory"] == str(harness.secrets_dir)
    [entry] = body["secrets"]
    assert entry["name"] == "STEWARD_CHAT_TOKEN_TESTY"
    assert entry["set"] is False
    assert entry["source"] is None
    assert entry["route"] == {
        "resident": "test-agent",
        "route": "chat",
        "address": "telegram:testy",
    }


def test_the_listing_says_where_a_set_secret_came_from(vault: VaultFactory):
    harness = vault(chat=True)
    harness.client.put("/secrets/STEWARD_CHAT_TOKEN_TESTY", json={"value": FAKE_BOT_TOKEN})

    [entry] = harness.client.get("/secrets").json()["secrets"]

    assert entry["set"] is True
    assert entry["source"] == "file"


def test_the_listing_reports_a_secret_the_environment_still_holds(
    vault: VaultFactory, monkeypatch: pytest.MonkeyPatch
):
    """Nothing migrates by force, so an ``.env`` token has to read as set."""
    monkeypatch.setenv("STEWARD_CHAT_TOKEN_TESTY", FAKE_BOT_TOKEN)
    harness = vault(chat=True)

    [entry] = harness.client.get("/secrets").json()["secrets"]

    assert entry["set"] is True
    assert entry["source"] == "env"


def test_a_secret_no_route_claims_is_still_listed(vault: VaultFactory):
    """An unassigned token is the state a half-finished provisioning leaves behind."""
    harness = vault(chat=True)
    harness.client.put("/secrets/STEWARD_CHAT_TOKEN_GHOST", json={"value": FAKE_BOT_TOKEN})

    entries = {entry["name"]: entry for entry in harness.client.get("/secrets").json()["secrets"]}

    assert entries["STEWARD_CHAT_TOKEN_GHOST"]["route"] is None
    assert entries["STEWARD_CHAT_TOKEN_GHOST"]["set"] is True


def test_no_listing_field_can_carry_a_value(vault: VaultFactory):
    harness = vault(chat=True)
    harness.client.put("/secrets/STEWARD_CHAT_TOKEN_TESTY", json={"value": FAKE_BOT_TOKEN})

    assert FAKE_BOT_TOKEN not in harness.client.get("/secrets").text


def test_there_is_no_route_that_reads_one_secret(vault: VaultFactory):
    harness = vault()
    harness.client.put("/secrets/STEWARD_CHAT_TOKEN_TESTY", json={"value": FAKE_BOT_TOKEN})

    assert harness.client.get("/secrets/STEWARD_CHAT_TOKEN_TESTY").status_code == 405


def test_there_is_no_route_that_deletes_one(vault: VaultFactory):
    """Unsetting is an ssh step on purpose: it is rare, and it is not what #462 is for."""
    harness = vault()

    assert harness.client.delete("/secrets/STEWARD_CHAT_TOKEN_TESTY").status_code == 405


# -- who may -----------------------------------------------------------------------


def test_an_anonymous_caller_is_refused(vault: VaultFactory):
    harness = vault()
    anonymous = TestClient(harness.client.app)

    assert (
        anonymous.put("/secrets/STEWARD_CHAT_TOKEN_TESTY", json={"value": "v"}).status_code == 401
    )
    assert anonymous.get("/secrets").status_code == 401
    assert not harness.secrets_dir.exists()


def test_a_resident_session_may_not_write_a_secret(vault: VaultFactory):
    """A resident that could set a token would be able to take another bot's identity."""
    harness = vault(chat=True)
    session = harness.session()

    response = harness.client.put(
        "/secrets/STEWARD_CHAT_TOKEN_TESTY",
        json={"value": FAKE_BOT_TOKEN},
        headers=session,
    )

    assert response.status_code == 403
    assert response.json()["detail"]["error"] == "session_credential_forbidden"
    assert "identity" in response.json()["detail"]["message"]
    assert not (harness.secrets_dir / "STEWARD_CHAT_TOKEN_TESTY").exists()
    assert harness.store.requests() == [], "and nothing was logged as accepted"
