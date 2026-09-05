"""Panel API harness: Git is always initialized; lifespan stays off by default."""

import subprocess
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from conftest import ResidentWriter, valid_manifest
from steward import events as ev
from steward.api import ApiConfig, create_app
from steward.nursery import (
    raise_resident,
)
from steward.routes.deps import (
    NurseryPipeline,
)
from steward.runners import MockRunner
from steward.store import Store

TOKEN = "a-shared-secret"


AUTH = {"Authorization": f"Bearer {TOKEN}"}


@dataclass
class Panel:
    """One built app and the collaborators a test needs to look inside it."""

    client: TestClient
    store: Store
    residents_dir: Path


type PanelFactory = Callable[..., Panel]


@pytest.fixture
def panel(tmp_path: Path, write_resident: ResidentWriter) -> Iterator[PanelFactory]:
    """Build the API against a scratch fleet, the way a control panel meets it."""
    built: list[Panel] = []

    def _make(
        *,
        manifest: dict[str, Any] | None = None,
        residents: bool = True,
        nursery: NurseryPipeline = raise_resident,
    ) -> Panel:
        residents_dir = tmp_path / "residents"
        residents_dir.mkdir(exist_ok=True)
        if residents:
            write_resident(manifest or valid_manifest(), root=residents_dir)
        store = Store(":memory:")
        # A real checkout around the tree: since steward #214 the API commits what it
        # writes, and a harness with no git behind it would be exercising the refusal
        # rather than the write.
        subprocess.run(  # noqa: S603
            ["git", "-C", str(tmp_path), "init", "-b", "main"],  # noqa: S607
            check=True,
            capture_output=True,
        )
        app = create_app(
            ApiConfig(
                residents_dir=residents_dir,
                token=TOKEN,
                workdir=tmp_path,
            ),
            store=store,
            emitter=ev.EventEmitter(url=None, fallback=tmp_path / "events.jsonl"),
            runner_factory=MockRunner,
            nursery=nursery,
        )
        built.append(
            Panel(
                client=TestClient(app, headers=dict(AUTH)),
                store=store,
                residents_dir=residents_dir,
            )
        )
        return built[-1]

    yield _make

    for item in built:
        item.client.app.state.runs.shutdown()
        item.store.close()


def anonymous(panel: Panel) -> TestClient:
    """Return a client holding no token at all — which is what a fresh browser tab is."""
    return TestClient(panel.client.app)
