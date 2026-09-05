"""Shared API harnesses and behavioral setup.

Git is initialized by default and can be explicitly disabled with ``git=False``.
The client does not enter lifespan: tests start background workers with an explicit
``with harness.client:`` block. Fixture cleanup releases blocked mock runs, waits
for the run executor, and closes the store, just as the original API harness did.
"""

import copy
import datetime as dt
import json
import re
import subprocess
import threading
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from conftest import (
    SECOND_RESIDENT_UID,
    ResidentWriter,
    SkillWriter,
    valid_manifest,
)
from steward import authoring as au
from steward import events as ev
from steward.api import (
    ApiConfig,
    create_app,
)
from steward.deploy import LocalTransport
from steward.nursery import (
    provision_resident,
    raise_resident,
    retire_resident,
)
from steward.operator_auth import new_operator_credential, operator_email
from steward.routes.deps import (
    NurseryPipeline,
    ProvisionPipeline,
    RetirePipeline,
)
from steward.runners import MockRunner, RunRequest, RunResult
from steward.runs import RUN_ROUTINE
from steward.session_auth import (
    new_session_credential,
)
from steward.store import Store

TOKEN = "a-shared-secret"


AUTH = {"Authorization": f"Bearer {TOKEN}"}


NEW_RESIDENT: dict[str, Any] = {
    "id": "note-keeper",
    "name": "Quill",
    "char": "Scribe",
    "accent": "#4f7ea6",
    "role": "note bot",
    "charter": {
        "mission": "Keep the village's notes in order.",
        "duties": ["Tidy the notes each evening."],
        "rules": ["Never delete a note without asking."],
        "escalation": "Raise needs_human before anything irreversible.",
    },
}


#: Words the API is never allowed to use about work it has only accepted.
FORBIDDEN = re.compile(r"\b(done|ran|complete|completed|succeeded|success)\b", re.IGNORECASE)


@dataclass
class Harness:
    """One built app, plus the collaborators a test needs to look inside."""

    client: TestClient
    store: Store
    events_path: Path
    residents_dir: Path
    released: list[threading.Event] = field(default_factory=list)

    def events(self, event_type: str | None = None) -> list[dict[str, Any]]:
        """Read the village's log: every event the API actually emitted."""
        if not self.events_path.is_file():
            return []
        lines = self.events_path.read_text(encoding="utf-8").splitlines()
        parsed = [json.loads(line) for line in lines if line.strip()]
        return [e for e in parsed if event_type is None or e["type"] == event_type]

    def settle(self) -> None:
        """Let every queued manual run finish before looking at the log."""
        self.client.app.state.runs.wait(timeout=10.0)


type ApiFactory = Callable[..., Harness]


@pytest.fixture
def api(tmp_path: Path, write_resident: ResidentWriter) -> Iterator[ApiFactory]:
    """Build an app with a mock runner, a scratch store, and a file for a village."""
    built: list[Harness] = []

    def _make(  # noqa: PLR0913 — one keyword per thing a test wants to vary
        *,
        manifest: dict[str, Any] | None = None,
        token: str | None = TOKEN,
        allow_open: bool = False,
        token_previous: str | None = None,
        token_previous_until: str | None = None,
        cors_origins: tuple[str, ...] = (),
        behavior: Callable[[RunRequest], RunResult] | None = None,
        db_path: Path | None = None,
        residents: bool = True,
        nursery: NurseryPipeline = raise_resident,
        provisioner: ProvisionPipeline = provision_resident,
        retirer: RetirePipeline = retire_resident,
        git: bool = True,
        push: au.PushTarget | None = None,
        transport: LocalTransport | None = None,
        emitter: ev.Emitter | None = None,
        approval_expiry_interval_s: float = 30.0,
        now: Callable[[], dt.datetime] = lambda: dt.datetime.now(dt.UTC),
    ) -> Harness:
        residents_dir = tmp_path / "residents"
        residents_dir.mkdir(exist_ok=True)
        if residents:
            write_resident(manifest or valid_manifest(), root=residents_dir)
        if git:
            # A real checkout around the tree, because since steward #214 every accepted
            # write is committed and a harness with no git would be testing the fallback
            # rather than the behaviour. Nothing here configures a git identity: steward
            # passes its own, which is what makes committing work on a server that has none.
            init_repo(tmp_path)
        events_path = tmp_path / "events.jsonl"
        store = Store(db_path or ":memory:")
        app = create_app(
            ApiConfig(
                residents_dir=residents_dir,
                token=token,
                allow_open=allow_open,
                token_previous=token_previous,
                token_previous_until=token_previous_until,
                cors_origins=cors_origins,
                workdir=tmp_path,
                push=push,
            ),
            store=store,
            emitter=(
                emitter if emitter is not None else ev.EventEmitter(url=None, fallback=events_path)
            ),
            runner_factory=lambda spec, placement: MockRunner(spec, placement, behavior=behavior),
            nursery=nursery,
            provisioner=provisioner,
            retirer=retirer,
            transport=transport,
            approval_expiry_interval_s=approval_expiry_interval_s,
            now=now,
        )
        harness = Harness(
            client=TestClient(app, headers=dict(AUTH) if token else {}),
            store=store,
            events_path=events_path,
            residents_dir=residents_dir,
        )
        built.append(harness)
        return harness

    yield _make

    for harness in built:
        for release in harness.released:
            release.set()
        harness.client.app.state.runs.shutdown()
        harness.store.close()


def init_repo(root: Path) -> None:
    """Make a directory a git checkout, the way a burrow holding the tree actually is."""
    subprocess.run(["git", "-C", str(root), "init", "-b", "main"], check=True, capture_output=True)  # noqa: S603, S607


# --------------------------------------------------------------------------------------
# approvals
# --------------------------------------------------------------------------------------


def _pending(harness: Harness) -> str:
    record = harness.store.create_approval_request(
        agent_id="claude-code:test-agent",
        project="test-agent",
        action="send_email",
        message="Testy wants to send an email to the plumber",
        detail={"to": "plumber@example.com"},
    )
    return record.request_id


def _expired_pending(harness: Harness) -> str:
    """Seed a request whose deadline has already passed but the sweep has not run."""
    past = ev.utc_now_iso(dt.datetime.now(dt.UTC) - dt.timedelta(hours=1))
    record = harness.store.create_approval_request(
        agent_id="claude-code:test-agent",
        project="test-agent",
        action="send_email",
        message="Testy wanted to send an email, a while ago",
        resident="test-agent",
        expires_at=past,
    )
    return record.request_id


# --------------------------------------------------------------------------------------
# delegation
# --------------------------------------------------------------------------------------

RECEIVER_SOUL = """---
agent_id: claude-code:receiver-agent
name: Recy
char: Monk
accent: "#a68a4f"
role: test bot
---
A villager that exists only inside a test.
"""


def receiver_manifest(*, status: str = "active") -> dict[str, Any]:
    """Build a resident that accepts delegated work through one named route."""
    data = copy.deepcopy(valid_manifest())
    data["uid"] = SECOND_RESIDENT_UID
    data["id"] = "receiver-agent"
    data["agent_id"] = "claude-code:receiver-agent"
    data["soul"]["name"] = "Recy"
    data["routes"] = [
        *data["routes"],
        {"id": "inbox", "kind": "delegation", "address": "steward:delegation", "status": status},
    ]
    return data


def sending_manifest() -> dict[str, Any]:
    """Build the test resident, permitted to hand work to the receiver."""
    data = copy.deepcopy(valid_manifest())
    data["delegation"] = {"send": True, "to": ["receiver-agent"]}
    return data


def with_receiver(
    api: ApiFactory, write_resident: ResidentWriter, *, status: str = "active"
) -> Harness:
    """Build an app whose tree holds a permitted sender and a declared receiver."""
    harness = api(manifest=sending_manifest())
    write_resident(receiver_manifest(status=status), soul=RECEIVER_SOUL, root=harness.residents_dir)
    return harness


HANDOFF = {"to": "receiver-agent", "route": "inbox", "title": "Read the background"}


# --------------------------------------------------------------------------------------
# scoped per-session credentials (steward #41)
# --------------------------------------------------------------------------------------


def open_session_run(  # noqa: PLR0913 — one keyword per fact about the run being opened
    harness: Harness,
    *,
    resident_id: str = "test-agent",
    kind: str = RUN_ROUTINE,
    trigger: str = "schedule",
    ref: str = "daily-summary",
    run_id: str = "run-1",
    heartbeat_at: str | None = None,
) -> str:
    """Register a live run the way the scheduler does, and return its credential."""
    credential = new_session_credential()
    assert harness.store.open_run(
        run_id=run_id,
        kind=kind,
        trigger=trigger,
        agent_id=f"claude-code:{resident_id}",
        project=resident_id,
        ref=ref,
        resident_id=resident_id,
        session_credential=credential,
        timeout_s=900.0,
        now=heartbeat_at,
    )
    return credential


def as_session(credential: str) -> dict[str, str]:
    """Build the header a session presents. Never steward's own token."""
    return {"Authorization": f"Bearer {credential}"}


# --------------------------------------------------------------------------------------
# POST /residents with deploy: true — the same pipeline the CLI runs
# --------------------------------------------------------------------------------------


@pytest.fixture
def village(monkeypatch: pytest.MonkeyPatch) -> str:
    """Give the API's environment a village to point new containers at."""
    monkeypatch.setenv("CHRONICLE_URL", "http://dxp2800:8737")
    monkeypatch.setenv("CHRONICLE_TOKEN", "api-village-token")
    monkeypatch.setenv("STEWARD_URL", "http://dxp2800:8802")
    return "api-village-token"


# --------------------------------------------------------------------------------------
# POST /residents/{id}/retire — the door back out (warren#331)
# --------------------------------------------------------------------------------------


def commit_tree(root: Path, subject: str = "chore: the tree as it stands") -> None:
    """Commit everything in the harness's checkout, so a retirement has a clean worktree.

    The ``api`` fixture inits a repo and commits nothing into it, which is the right default
    for every other route — none of them refuse over a worktree. Retirement's commit belongs
    between the mark and the stop, so it is the nursery's, and the nursery's dirty-worktree
    refusal comes with it. A test that wants the happy path has to be honest about that.
    """
    # Identity and signing passed as command-line config, the way steward passes its own: a
    # temp checkout has no `git config`, and a developer's global `commit.gpgsign` is not
    # this test's business.
    author = (
        "-c",
        "user.name=Test",
        "-c",
        "user.email=test@example.invalid",
        "-c",
        "commit.gpgsign=false",
    )
    subprocess.run(["git", "-C", str(root), "add", "-A"], check=True, capture_output=True)  # noqa: S603, S607
    subprocess.run(  # noqa: S603
        ["git", "-C", str(root), *author, "commit", "-m", subject],  # noqa: S607
        check=True,
        capture_output=True,
    )


# --------------------------------------------------------------------------------------
# the write API: declarations and skills (steward #214)
# --------------------------------------------------------------------------------------

GRANTED_SKILLS = ("daily-summary", "write-journal")


@pytest.fixture
def writable(api: ApiFactory, write_skill: SkillWriter, tmp_path: Path) -> Callable[..., Harness]:
    """Build an API whose library holds the skills the test resident is granted.

    Without it the tree has no library at all, every grant goes unchecked, and the first
    skill written would configure a library that suddenly makes those grants errors — which
    is correct behaviour and a terrible way to set up a test about something else.
    """

    def _make(**kwargs: object) -> Harness:
        for name in GRANTED_SKILLS:
            write_skill(name, root=tmp_path / "skills")
        return api(**kwargs)

    return _make


def declaration(harness: Harness, resident_id: str = "test-agent") -> dict[str, Any]:
    """Read a resident's declaration the way a form loads it."""
    response = harness.client.get(f"/residents/{resident_id}/declaration")
    assert response.status_code == 200
    return response.json()


# -- skills ----------------------------------------------------------------------------


NEW_SKILL = {
    "name": "triage",
    "description": "Sort the inbox before anything else.",
    "body": "Read every message. Answer what you can. Escalate what you cannot.",
}


# --------------------------------------------------------------------------------------
# named operator credentials (warren#225)
# --------------------------------------------------------------------------------------


def mint_operator(
    harness: Harness, name: str = "Miha", email: str | None = None, note: str = ""
) -> str:
    """Mint one operator credential the way the terminal does, and return the plaintext."""
    credential = new_operator_credential()
    harness.store.mint_operator(
        name=name,
        email=email or operator_email(name),
        credential=credential,
        note=note,
    )
    return credential


def last_commit(harness: Harness) -> str:
    """Return the newest commit's author line and body — who wrote it, and on whose behalf."""
    return subprocess.run(
        ["git", "log", "-1", "--format=%an <%ae>%n%b"],  # noqa: S607
        cwd=harness.residents_dir.parent,
        capture_output=True,
        text=True,
        check=True,
    ).stdout


def as_operator(credential: str) -> dict[str, str]:
    """Build the header an operator's browser presents. Never steward's own token."""
    return {"Authorization": f"Bearer {credential}"}
