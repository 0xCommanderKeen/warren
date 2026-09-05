"""Deployment defaults and path rules shared by validation and rendering."""

import os
import posixpath
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import PurePosixPath

from steward.manifest_models import (
    DEFAULT_SCHEDULE_TZ,
    HOST_PATTERN,
    SSH_USER_PATTERN,
    ResidentManifest,
)

DEPLOY_HOST_ENV = "STEWARD_DEPLOY_HOST"
DEPLOY_USER_ENV = "STEWARD_DEPLOY_USER"


class DeploymentSettingsError(ValueError):
    """Placement is missing or unsafe; refuse before provisioning anything."""


@dataclass(frozen=True)
class DeploymentSettings:
    """One snapshot of installation defaults; explicit resident fields take precedence."""

    host: str | None = None
    user: str | None = None

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> DeploymentSettings:
        """Read at construction, never at import, so separate installations stay separate."""
        source = os.environ if env is None else env
        return cls(source.get(DEPLOY_HOST_ENV), source.get(DEPLOY_USER_ENV))

    def resolve_host(self, explicit: str | None = None) -> str:
        """Resolve a host with the manifest's existing safety grammar."""
        return self._resolve(explicit, self.host, DEPLOY_HOST_ENV, HOST_PATTERN)

    def resolve_user(self, explicit: str | None = None) -> str:
        """Resolve an SSH user without guessing the current process account."""
        return self._resolve(explicit, self.user, DEPLOY_USER_ENV, SSH_USER_PATTERN)

    @staticmethod
    def _resolve(explicit: str | None, default: str | None, name: str, pattern: str) -> str:
        value = explicit if explicit is not None else default
        if not value or not re.fullmatch(pattern, value):
            raise DeploymentSettingsError(
                f"set an explicit deploy field or {name} to a valid nonblank value"
            )
        return value


#: Where the warren lives on a burrow (warren#358): one directory under the deploy user's
#: home holding everything the warren puts there — chronicle at ``<root>/burrow``, the
#: origin at ``<root>/arcadia``, the control plane at ``<root>/steward`` — rather than
#: siblings at the top of ``~/docker`` beside stacks that are not the warren's.
DEFAULT_ROOT = "~/docker/warren"

#: Where a resident's compose directory goes when its manifest names none: named by the
#: resident, under one directory for all of them — ``~/docker/warren/residents/<id>``. The
#: control plane's daemons mount exactly this directory (deploy/compose.yaml), so a
#: resident whose manifest puts ``deploy.path`` elsewhere on the burrow is one they cannot
#: journal for.
DEFAULT_RESIDENTS_ROOT = f"{DEFAULT_ROOT}/residents"

#: What this burrow is called, when the machine's own hostname is not the name manifests
#: use for it (``topology.py`` reads the same variable to partition the residents tree).
#: The deployed control plane compares a resident's ``deploy.host`` against it: a match
#: means *this machine*, and provisioning then goes through :class:`BurrowTransport`
#: rather than ssh to itself.
BURROW_ENV = "STEWARD_BURROW"

#: The deploy user's home *as the host knows it* — ``/home/Miha`` on the NAS — for the
#: process that provisions this burrow's residents from inside a container, where its own
#: ``$HOME`` is ``/root`` and means nothing to the docker daemon. Defaults to
#: ``/home/<deploy.user>``.
BURROW_HOME_ENV = "STEWARD_BURROW_HOME"

#: What a resident's container name is when nobody says: the same prefix the watchdog has
#: been reading out of ``deploy.container`` since #8.
CONTAINER_PREFIX = "steward-"

#: The image a resident runs when its manifest names none: the one this repo builds, from
#: ``docker/resident/Dockerfile``, carrying the ``claude`` CLI and chronicle's hook emitter.
#: It is a *local* tag, because this fleet has no registry — ``make image`` builds it and
#: ``make image-ship`` pipes it to the NAS over ssh, the same way everything else here
#: travels. A host that has never been shipped the image fails at ``docker compose up``
#: with "image not found", which is a loud, fixable failure; the alternative default — a
#: bare ``python:3.12-slim`` that starts happily and cannot run a session — was a quiet one.
DEFAULT_IMAGE = "steward-resident:latest"

#: What the container runs when the manifest names nothing. A resident's container is a
#: *place for sessions to happen* — steward drives the brain from outside — so the honest
#: default is a process that stays up and does nothing, rather than a busy loop
#: pretending to be work.
DEFAULT_COMMAND: tuple[str, ...] = ("sleep", "infinity")

#: Where a resident's memory is mounted when its manifest declares a memory kind that is
#: not a directory. Nothing is lost: the volume still persists on the host, it simply is
#: not the location the manifest talks about.
FALLBACK_MEMORY_PATH = "/data/memory"


def _path_provides(root: str, path: str) -> bool:
    """Whether an absolute container path is the root or one of its descendants."""
    normal_root = PurePosixPath(posixpath.normpath(root))
    normal_path = PurePosixPath(posixpath.normpath(path))
    return normal_path == normal_root or normal_root in normal_path.parents


class ZoneDisagreementError(ValueError):
    """The routines read their schedules in different zones and ``deploy.tz`` is unset."""


def container_zone(manifest: ResidentManifest) -> str:
    """Return the IANA zone the resident's container clock is set to (warren#386).

    ``deploy.tz`` when declared; otherwise the routines' ``schedule_tz`` when every routine
    agrees (a resident with no routines reads as :data:`DEFAULT_SCHEDULE_TZ`). Routines
    that disagree with no ``deploy.tz`` to settle it raise :class:`ZoneDisagreementError`:
    validation turns that into an error, so the nursery never has to pick a clock for a
    manifest that never chose one.
    """
    if manifest.deploy.tz is not None:
        return manifest.deploy.tz
    zones = sorted({routine.schedule_tz for routine in manifest.routines})
    if not zones:
        return DEFAULT_SCHEDULE_TZ
    if len(zones) > 1:
        raise ZoneDisagreementError(
            f"routines read their schedules in {', '.join(zones)} and deploy.tz does not "
            f"say which one the container's clock follows"
        )
    return zones[0]


def resolve_mount_host_path(host: str, burrow_home: str) -> str:
    """Resolve a mount's ``~/`` spelling against the burrow user's host home."""
    return burrow_home.rstrip("/") + host[1:] if host.startswith("~/") else host


def memory_path_for(manifest: ResidentManifest) -> str:
    """Return the in-container path the resident's memory volume is mounted at."""
    memory = manifest.memory
    if memory.kind == "directory" and memory.path:
        return memory.path
    return FALLBACK_MEMORY_PATH


def burrow_home_for(user: str, env: Mapping[str, str] | None = None) -> str:
    """Resolve the deploy user's host home for rendering and mount-writer validation."""
    source = os.environ if env is None else env
    return (source.get(BURROW_HOME_ENV) or f"/home/{user}").rstrip("/")
