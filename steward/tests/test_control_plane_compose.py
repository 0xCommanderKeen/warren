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
#: Where every process finds the residents' deploy directories (warren#356, warren#358):
#: the burrow's directory, mounted at its own path, and ``STEWARD_BURROW_HOME`` telling
#: ``deploy.py`` to resolve ``~/docker/warren/residents/<id>/memory`` against that home
#: rather than the image's ``/root``. That directory and no wider: the control plane's own
#: directory beside it holds the ``.env`` and the deploy key, which the daemons have no
#: business with.
#: Where the burrow's credential directory is mounted (warren#462). One file per secret,
#: written by the API and read by the daemons that carry a conversation.
SECRETS = "/secrets"
HOST_RESIDENTS = "/home/Miha/docker/warren/residents"
BURROW_HOME = "/home/Miha"


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


def test_every_daemon_keeps_its_event_queue_on_the_data_volume(
    services: dict[str, Any],
) -> None:
    """A deploy recreates containers, so undelivered events must survive under /data."""
    paths = {
        name: service["environment"]["STEWARD_EVENTS_FALLBACK"]
        for name, service in services.items()
    }

    assert paths == {
        "api": "/data/events/api.jsonl",
        "scheduler": "/data/events/scheduler.jsonl",
        "watchdog": "/data/events/watchdog.jsonl",
        "chat": "/data/events/chat.jsonl",
    }


def test_the_daemons_read_one_tree(services: dict[str, Any]) -> None:
    """A watchdog reading a wider list than the scheduler fires would report ghosts."""
    assert residents_of(services["scheduler"]) == residents_of(services["watchdog"])
    assert mounts(services["scheduler"])[CHECKOUT] == mounts(services["watchdog"])[CHECKOUT]


@pytest.mark.parametrize("name", ["scheduler", "watchdog"])
def test_the_daemons_see_the_residents_deploy_directories(
    services: dict[str, Any], name: str
) -> None:
    """warren#356: the scheduler looked for /root/docker/steward-pip/memory and found nothing.

    A container-placed resident's memory lives at ``<deploy.path>/memory`` on the burrow.
    ``memory_host_dir`` resolves the ``~`` in that path against STEWARD_BURROW_HOME, so the
    daemons compute the host's own path — and the host's residents directory is mounted at
    exactly that path, read-write, because the scheduler journals into and materializes
    skills onto it. Since warren#358 that is ``~/docker/warren/residents`` and nothing
    above it: mounting all of ``~/docker`` handed the daemons steward's ``.env`` and deploy
    key next door.
    """
    daemon = services[name]
    assert mounts(daemon)[HOST_RESIDENTS] == f"{HOST_RESIDENTS}:{HOST_RESIDENTS}", (
        "the daemons compute /home/Miha/docker/warren/residents/<id>/memory; the mount must "
        "sit at that path"
    )
    assert daemon["environment"]["STEWARD_BURROW_HOME"] == BURROW_HOME
    assert not any(spec.split(":")[1].startswith("/root/docker") for spec in daemon["volumes"]), (
        "no second view of the directory under the image's /root"
    )


def test_the_api_does_not_hold_the_residents_directories(services: dict[str, Any]) -> None:
    """The API writes declarations into the checkout, never into a resident's memory."""
    assert not any(
        spec.split(":")[1].startswith("/root/docker") for spec in services["api"]["volumes"]
    )


#: Where this burrow's residents are on the host — and, for the API, in the container too.
HOST_RESIDENTS_SAME_PATH = f"{HOST_RESIDENTS}:{HOST_RESIDENTS}"
SOCKET = "/var/run/docker.sock"


def test_the_api_provisions_this_burrows_residents_itself(services: dict[str, Any]) -> None:
    """warren#356 gap two: the API cannot ssh to the machine it is on, so it does not.

    It writes a resident's bundle into the residents directory and runs docker compose
    against the socket — through deploy.BurrowTransport, which resolves every ``~/`` path
    against STEWARD_BURROW_HOME. The directory is mounted at its *host* path because the
    compose CLI in the API's process hands the daemon the paths it resolves ``./memory`` to.
    """
    api = services["api"]
    assert mounts(api)[HOST_RESIDENTS] == HOST_RESIDENTS_SAME_PATH, (
        "the residents directory must have the same path in the API as on the host"
    )
    assert mounts(api)[SOCKET] == f"{SOCKET}:{SOCKET}"
    assert api["environment"]["STEWARD_BURROW_HOME"] == BURROW_HOME
    assert api["environment"]["STEWARD_BURROW"] == "dxp2800", (
        "the name deploy.host is matched against"
    )


def test_the_api_alone_may_write_the_credential_directory(services: dict[str, Any]) -> None:
    """warren#462: the write path is an API call, so exactly one container may write.

    The chat daemon and the scheduler both *read* tokens — one to answer a message, one to
    deliver a routine's final line — and neither has any business setting one. Mounting
    their side read-only is what makes "a credential is written by a human through the API"
    a property of the deployment rather than a convention in the code.
    """
    assert mounts(services["api"])[SECRETS] == f"./secrets:{SECRETS}"
    for name in ("chat", "scheduler"):
        assert mounts(services[name])[SECRETS] == f"./secrets:{SECRETS}:ro", name


def test_every_container_that_mounts_secrets_is_told_where_they_are(
    services: dict[str, Any],
) -> None:
    """The mount point and ``STEWARD_SECRETS_DIR`` are one fact, so they cannot drift."""
    for name in ("api", "chat", "scheduler"):
        assert services[name]["environment"]["STEWARD_SECRETS_DIR"] == SECRETS, name


def test_the_watchdog_holds_no_credentials(services: dict[str, Any]) -> None:
    """It restarts containers and reads no route; a mount it never uses is only exposure."""
    assert SECRETS not in mounts(services["watchdog"])
