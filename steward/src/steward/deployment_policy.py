"""Resident placement and mount policies, using the same rules as deployment rendering."""

from pathlib import Path

from steward.deployment_rules import (
    DeploymentSettings,
    DeploymentSettingsError,
    ZoneDisagreementError,
    _path_provides,
    container_zone,
)
from steward.diagnostics import Diagnostic
from steward.manifest_models import ResidentManifest
from steward.manifest_policy import UNBOUNDABLE_RUNNER_KINDS


def _check_workspace_is_reachable(manifest: ResidentManifest, source: Path) -> list[Diagnostic]:
    """Refuse a directory grant steward has no way to make.

    ``workspace`` is the mirror image of ``tools``: ``tools`` narrows *what exists* in a
    session, ``workspace`` widens *where it may act*. That is why one is required and the
    other is not — an absent ``tools`` would have meant every tool, while an absent
    ``workspace`` means no directory beyond the resident's own, which is a silence that
    grants nothing and so is a silence this schema can live with.

    It still has to be a grant steward can actually make. Only :meth:`ClaudeRunner.argv`
    compiles ``--add-dir``; under ``codex`` or ``command`` the list would sit in the
    manifest reading like access somebody granted while the session could not reach a byte
    of it. Unlike the tools refusals this one fails loudly at run time — the resident simply
    cannot open the files — but it fails at the resident's next fire, over a manifest that
    read as if the access was there, and the manifest is where it should have been caught.

    ``mock`` is exempt for the reason it is always exempt: it opens nothing.
    """
    if not manifest.workspace:
        return []
    if manifest.runner.container_placed:
        provided = [manifest.memory.path, *(mount.container for mount in manifest.deploy.mounts)]
        diagnostics = []
        for index, workspace in enumerate(manifest.workspace):
            if any(_path_provides(root, workspace) for root in provided):
                continue
            diagnostics.append(
                Diagnostic(
                    file=source,
                    field_path=f"workspace[{index}]",
                    problem=(
                        f"workspace path {workspace!r} is inside a container, but no declared "
                        "mount provides it; the grant would be silently unreachable"
                    ),
                    example="add deploy.mounts with a container path that contains this workspace",
                )
            )
        return diagnostics
    if manifest.runner.kind not in UNBOUNDABLE_RUNNER_KINDS:
        return []
    return [
        Diagnostic(
            file=source,
            field_path="workspace",
            problem=(
                f"runner kind {manifest.runner.kind!r} takes no directory flag, so this "
                f"grant reaches nothing: the session's access is whatever that brain "
                f"allows, and the manifest reads as if {len(manifest.workspace)} "
                f"director{'y' if len(manifest.workspace) == 1 else 'ies'} had been opened to it"
            ),
            example=(
                "runner: {kind: claude}  (the only kind steward can widen), or drop the "
                "grant and let the session work inside its own memory directory"
            ),
        )
    ]


def _check_mount_collisions(manifest: ResidentManifest, source: Path) -> list[Diagnostic]:
    """Refuse extra mounts that mask Steward-managed container directories."""
    managed = (manifest.memory.path, "/root/.claude")
    if manifest.runner.kind == "codex":
        managed += ("/root/.codex",)
    diagnostics = []
    for index, mount in enumerate(manifest.deploy.mounts):
        collides = any(
            _path_provides(path, mount.container) or _path_provides(mount.container, path)
            for path in managed
        )
        if collides:
            diagnostics.append(
                Diagnostic(
                    file=source,
                    field_path=f"deploy.mounts[{index}].container",
                    problem=(
                        f"container path {mount.container!r} collides with a Steward-managed "
                        "memory or runner configuration mount"
                    ),
                    example="choose a separate container path such as /vault",
                )
            )
    return diagnostics


def _check_container_zone(manifest: ResidentManifest, source: Path) -> list[Diagnostic]:
    """Refuse a manifest whose container clock has no single answer.

    ``TZ`` is rendered into every container so ``date`` inside a session agrees with the
    routines' wall clock; a resident stamping "today" from ``date`` is otherwise one
    late-evening edge away from a wrong journal filename. When the routines disagree
    among themselves the manifest has to say which zone the container follows.
    """
    try:
        container_zone(manifest)
    except ZoneDisagreementError as exc:
        return [
            Diagnostic(
                file=source,
                field_path="deploy.tz",
                problem=str(exc),
                example="deploy: {tz: Europe/Ljubljana}",
            )
        ]
    return []


#: Runner kinds a session cannot be placed in a container under. ``mock`` spawns nothing,
#: so the declaration would read as containment while the "session" ran in-process; a
#: ``command`` template substitutes ``{workdir}`` on the control plane, so its argv would
#: run inside the container carrying a host path baked in. Both are the same failure the
#: tools refusals exist for: a declaration somebody reads that holds nothing.
UNPLACEABLE_RUNNER_KINDS = frozenset({"mock", "command"})


def _check_placement(manifest: ResidentManifest, source: Path) -> list[Diagnostic]:
    """Refuse a container placement steward could not honestly hold (steward #58).

    Two refusals, both at validation time because both fail silently at run time:

    - ``placement: container`` under a kind in :data:`UNPLACEABLE_RUNNER_KINDS` — the
      manifest would read as "sessions happen inside the container" while they did not,
      or did with the wrong working directory substituted in.
    - ``placement: container`` with no ``deploy.container``. The address must be written
      down: the nursery's default name is what it *would* create, and relocating a
      resident's execution should never hang off a name nobody wrote. Refusing here is a
      diagnostic in daylight rather than a 7am ``docker exec`` against a guess.
    """
    runner = manifest.runner
    if not runner.container_placed:
        return []
    if runner.kind in UNPLACEABLE_RUNNER_KINDS:
        reason = (
            "spawns no process for a container to hold"
            if runner.kind == "mock"
            else "substitutes {workdir} with a control-plane path the container does not have"
        )
        return [
            Diagnostic(
                file=source,
                field_path="runner.placement",
                problem=(
                    f"placement 'container' under runner kind {runner.kind!r} declares a "
                    f"containment that cannot hold: {runner.kind!r} {reason}"
                ),
                example="runner: {kind: claude, placement: container}",
            )
        ]
    if not manifest.deploy.container:
        return [
            Diagnostic(
                file=source,
                field_path="runner.placement",
                problem=(
                    "placement 'container' needs deploy.container to name the container "
                    "sessions run in; steward will not exec into a defaulted name"
                ),
                example="deploy: {container: steward-" + manifest.id + "}",
            )
        ]
    return []


def _check_deployment_settings(manifest: ResidentManifest, source: Path) -> list[Diagnostic]:
    """Report missing placement before any target is rendered or provisioning begins."""
    defaults = DeploymentSettings.from_env()
    try:
        defaults.resolve_host(manifest.deploy.host)
        defaults.resolve_user(manifest.deploy.user)
    except DeploymentSettingsError as exc:
        return [
            Diagnostic(
                file=source,
                field_path="deploy",
                problem=str(exc),
                example="configure STEWARD_DEPLOY_HOST and STEWARD_DEPLOY_USER",
            )
        ]
    return []
