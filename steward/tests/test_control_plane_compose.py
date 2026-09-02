"""The burrow's compose file, held to what the write surface needs (warren#351).

``deploy/compose.yaml`` is what the NAS runs. Three defects hid in it for weeks while every
suite was green — the residents tree baked into the image, no checkout to commit into, and
daemons reading a copy taken at container start — because nothing in the repository read the
file the way ``docker compose`` does. These tests do. They pin the wiring, not docker: the
checkout is mounted, the API alone may write it, and the daemons read the same checkout
rather than a copy of anything.
"""

from pathlib import Path
from typing import Any

import pytest
import yaml

COMPOSE = Path(__file__).resolve().parent.parent / "deploy" / "compose.yaml"

#: Where the burrow's residents checkout is mounted, in every container that reads it.
CHECKOUT = "/checkout"
#: The residents tree inside it — every process's ``--residents``.
RESIDENTS = f"{CHECKOUT}/steward/residents"
#: Where the image's ``GIT_SSH_COMMAND`` expects the deploy key that pushes the checkout.
KEY = "/run/steward/residents-key"


@pytest.fixture(scope="module")
def services() -> dict[str, Any]:
    """Read the compose file as docker would, merge keys and all."""
    return yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))["services"]


def mounts(service: dict[str, Any]) -> dict[str, str]:
    """Return the service's bind mounts keyed by their path inside the container."""
    return {spec.split(":")[1]: spec for spec in service["volumes"]}


def residents_of(service: dict[str, Any]) -> str:
    """Return the tree the service's command is pointed at."""
    command = service["command"]
    return command[command.index("--residents") + 1]


def test_the_api_serves_the_checkout_and_is_the_one_writer(services: dict[str, Any]) -> None:
    """Defects one and two: a real checkout, mounted read-write, and the API reads it."""
    api = services["api"]
    assert residents_of(api) == RESIDENTS
    assert mounts(api)[CHECKOUT] == f"./residents-repo:{CHECKOUT}", (
        "the API's mount is the writable one"
    )
    assert mounts(api)[KEY] == f"./residents-key:{KEY}:ro", "the deploy key is mounted, read-only"


def test_the_api_pushes_to_the_burrow_branch_and_never_main(services: dict[str, Any]) -> None:
    """The record leaves the burrow on its own branch: nothing lands on main without a PR."""
    environment = services["api"]["environment"]
    assert environment["STEWARD_PUSH_BRANCH"] == "burrow/residents"
    assert "STEWARD_ALLOW_UNCOMMITTED_WRITES" not in environment, (
        "the switch that would write into the container layer — warren#313's failure — stays off"
    )


@pytest.mark.parametrize("name", ["scheduler", "watchdog"])
def test_the_daemons_read_the_checkout_and_may_not_write_it(
    services: dict[str, Any], name: str
) -> None:
    """Defect three: a copy taken at start is never updated; the mounted checkout is live."""
    daemon = services[name]
    assert mounts(daemon)[CHECKOUT] == f"./residents-repo:{CHECKOUT}:ro", "only the API writes"
    assert KEY not in mounts(daemon), "the daemons never push, so they never hold the key"
    assert residents_of(daemon) == RESIDENTS
    assert "cp -r" not in " ".join(daemon["command"]), (
        "a copy at start is the tree nothing updates (warren#351)"
    )


def test_nothing_reads_the_tree_baked_into_the_image(services: dict[str, Any]) -> None:
    """The image's residents/ is the seed a new burrow is cloned from, and nothing serves it."""
    for name, service in services.items():
        assert "/app/residents" not in " ".join(service["command"]), name
        assert "/app/skills" not in " ".join(service["command"]), name


def test_the_daemons_read_one_tree(services: dict[str, Any]) -> None:
    """A watchdog reading a wider list than the scheduler fires would report ghosts."""
    assert residents_of(services["scheduler"]) == residents_of(services["watchdog"])
    assert mounts(services["scheduler"])[CHECKOUT] == mounts(services["watchdog"])[CHECKOUT]
