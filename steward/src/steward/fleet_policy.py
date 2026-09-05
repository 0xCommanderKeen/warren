"""Policies that require comparing residents across a validated fleet."""

import posixpath
from collections.abc import Sequence
from pathlib import PurePosixPath

from steward.deployment_rules import (
    DeploymentSettings,
    burrow_home_for,
    resolve_mount_host_path,
)
from steward.diagnostics import Diagnostic, Severity
from steward.manifest_models import Memory, Resident


def _resolved_journal_dir(memory: Memory) -> str:
    """Return where a resident's entries land: ``memory.path`` joined with ``memory.journal``.

    Normalised so ``/a/b`` and ``/a//b/`` are one place, which is what "the same journal
    directory" has to mean for two manifests to be caught sharing one.
    """
    return posixpath.normpath(str(PurePosixPath(memory.path) / memory.journal))


def _check_shared_journal_dirs(residents: Sequence[Resident]) -> list[Diagnostic]:
    """Warn when two residents resolve to one journal directory (#77, the manifest side).

    A shared ``<memory.path>/<memory.journal>`` means one resident's close-of-day entry
    lands where another reads its own from, so the two cross-feed: alice would wake up to
    bob's private note. It is a **warning**, not an error — a tree can be arranged this way
    on purpose in a test or a migration — but it is never what a production village wants,
    and validation is the one moment anybody reads the two paths side by side. The
    read-time filtering that keeps entries apart even so lives in :mod:`steward.journal`.
    """
    by_dir: dict[str, list[Resident]] = {}
    for resident in residents:
        memory = resident.manifest.memory
        if memory.kind != "directory":
            continue
        by_dir.setdefault(_resolved_journal_dir(memory), []).append(resident)
    diagnostics: list[Diagnostic] = []
    for journal_dir, group in by_dir.items():
        if len(group) <= 1:
            continue
        ids = sorted(resident.id for resident in group)
        for resident in group:
            others = [rid for rid in ids if rid != resident.id]
            diagnostics.append(
                Diagnostic(
                    file=resident.path,
                    field_path="memory.path",
                    problem=(
                        f"resident {resident.id!r} resolves its journal directory to "
                        f"{journal_dir}, which {others} also use; a shared journal lets one "
                        f"resident read another's entries"
                    ),
                    example="give each resident its own memory.path",
                    severity=Severity.WARNING,
                )
            )
    return diagnostics


def _check_competing_mount_writers(residents: Sequence[Resident]) -> list[Diagnostic]:
    """Refuse a tree where one shared host path has more than one declared writer.

    One writer per resource is a rule of the org, not advice (warren#440): two residents
    holding the same clone open for writing race each other, and no manifest can see the
    conflict on its own. Read-only mounts are free — it is the second *writer* that is
    refused.

    Two residents contend only if they share a filesystem, so the grouping key is the
    burrow *and* the resolved path. Matching is exact after ``~/`` resolution and
    :func:`posixpath.normpath`: a symlink to the same directory, or a mount nested inside
    another resident's, is not seen.
    """
    # (burrow, path): two residents on different burrows do not share a filesystem, so
    # the same spelling there is two resources. Defaults and home resolution are shared
    # with deployment rendering through deployment_rules.
    by_host: dict[tuple[str, str], dict[str, Resident]] = {}
    defaults = DeploymentSettings.from_env()
    for resident in residents:
        for mount in resident.manifest.deploy.mounts:
            if mount.mode == "rw":
                host = mount.host
                if host.startswith("~/"):
                    user = defaults.resolve_user(resident.manifest.deploy.user)
                    home = burrow_home_for(user)
                    host = resolve_mount_host_path(host, home)
                burrow = defaults.resolve_host(resident.manifest.deploy.host)
                by_host.setdefault((burrow, posixpath.normpath(host)), {})[resident.id] = resident
    diagnostics: list[Diagnostic] = []
    for (burrow, host_path), residents_by_id in by_host.items():
        group = list(residents_by_id.values())
        if len(group) <= 1:
            continue
        ids = sorted(resident.id for resident in group)
        diagnostics.extend(
            Diagnostic(
                file=resident.path,
                field_path="deploy.mounts",
                problem=(
                    f"host path {host_path!r} on {burrow} has read-write mounts for "
                    f"{ids}; the one writer per shared clone rule permits at most one"
                ),
                example="change every mount but one to mode: ro",
            )
            for resident in group
        )
    return diagnostics


def _check_unique_uids(residents: Sequence[Resident]) -> list[Diagnostic]:
    """Reject a durable identity declared by more than one resident."""
    by_uid: dict[str, list[Resident]] = {}
    for resident in residents:
        by_uid.setdefault(str(resident.manifest.uid), []).append(resident)

    diagnostics: list[Diagnostic] = []
    for uid, group in by_uid.items():
        if len(group) <= 1:
            continue
        ids = sorted(resident.id for resident in group)
        for resident in group:
            others = [resident_id for resident_id in ids if resident_id != resident.id]
            diagnostics.append(
                Diagnostic(
                    file=resident.path,
                    field_path="uid",
                    problem=(
                        f"uid {uid} also belongs to {others}; a durable identity must name "
                        "exactly one resident"
                    ),
                    example="uid: 3a78217a-df03-4f3b-a46a-4c75b4ad929f",
                )
            )
    return diagnostics


def _check_unique_agent_ids(residents: Sequence[Resident]) -> list[Diagnostic]:
    """Reject an effective Chronicle join key used by more than one resident."""
    by_agent_id: dict[str, list[Resident]] = {}
    for resident in residents:
        by_agent_id.setdefault(resident.agent_id, []).append(resident)

    diagnostics: list[Diagnostic] = []
    for agent_id, group in by_agent_id.items():
        if len(group) <= 1:
            continue
        ids = sorted(resident.id for resident in group)
        for resident in group:
            others = [resident_id for resident_id in ids if resident_id != resident.id]
            diagnostics.append(
                Diagnostic(
                    file=resident.path,
                    field_path="agent_id",
                    problem=(
                        f"agent_id {agent_id!r} also belongs to {others}; a Chronicle join "
                        "key must name exactly one resident"
                    ),
                    example=f"agent_id: resident:{resident.manifest.uid}",
                )
            )
    return diagnostics


def _check_unique_homes(residents: Sequence[Resident]) -> list[Diagnostic]:
    """Reject two residents claiming the same stable village plot."""
    by_home: dict[int, list[Resident]] = {}
    for resident in residents:
        by_home.setdefault(resident.manifest.home, []).append(resident)
    diagnostics: list[Diagnostic] = []
    for home, group in by_home.items():
        if len(group) <= 1:
            continue
        ids = sorted(resident.id for resident in group)
        for resident in group:
            others = [resident_id for resident_id in ids if resident_id != resident.id]
            diagnostics.append(
                Diagnostic(
                    file=resident.path,
                    field_path="home",
                    problem=f"home {home} also belongs to {others}; a plot has one resident",
                    example="home: 0",
                )
            )
    return diagnostics
