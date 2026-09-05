# ruff: noqa: PLC0414 — explicit aliases preserve named imports beyond historical __all__.
"""Resident manifests: compatible public interface and ordered validation coordinator.

Foundational models, diagnostics, credential scanning and cross-field policies live in
lower-level modules. This module owns loading and the order in which diagnostics are
collected, and re-exports the existing manifest vocabulary for callers.
"""

import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError

from steward.credential_policy import CREDENTIAL_EXAMPLE as CREDENTIAL_EXAMPLE
from steward.credential_policy import CREDENTIAL_KEY_PATTERN as CREDENTIAL_KEY_PATTERN
from steward.credential_policy import CREDENTIAL_NAME_EXEMPT as CREDENTIAL_NAME_EXEMPT
from steward.credential_policy import REFERENCE_FIELDS as REFERENCE_FIELDS
from steward.credential_policy import SECRET_REDACTION, redact_mapping, redact_secrets
from steward.credential_policy import SECRET_VALUE_PATTERNS as SECRET_VALUE_PATTERNS
from steward.credential_policy import _looks_like_opaque_blob as _looks_like_opaque_blob
from steward.credential_policy import scan_for_credentials as scan_for_credentials
from steward.credential_policy import scan_text_for_secrets as scan_text_for_secrets
from steward.deployment_policy import UNPLACEABLE_RUNNER_KINDS as UNPLACEABLE_RUNNER_KINDS
from steward.deployment_policy import (
    _check_container_zone,
    _check_deployment_settings,
    _check_mount_collisions,
    _check_placement,
    _check_workspace_is_reachable,
)
from steward.deployment_rules import ZoneDisagreementError as ZoneDisagreementError
from steward.deployment_rules import container_zone as container_zone
from steward.deployment_rules import resolve_mount_host_path
from steward.diagnostics import FIELD_EXAMPLES as FIELD_EXAMPLES
from steward.diagnostics import (
    Diagnostic,
    ManifestError,
    Severity,
    _diagnostics_from_validation_error,
    closest_match,
)
from steward.fleet_policy import (
    _check_competing_mount_writers,
    _check_shared_journal_dirs,
    _check_unique_agent_ids,
    _check_unique_homes,
    _check_unique_uids,
)
from steward.manifest_models import ACCENT_PATTERN as ACCENT_PATTERN
from steward.manifest_models import AGENT_ID_PATTERN as AGENT_ID_PATTERN
from steward.manifest_models import BYPASS_PERMISSIONS as BYPASS_PERMISSIONS
from steward.manifest_models import CHARTER_ENTRIES_MAX as CHARTER_ENTRIES_MAX
from steward.manifest_models import CHARTER_ENTRY_MAX_CHARS as CHARTER_ENTRY_MAX_CHARS
from steward.manifest_models import CHARTER_MISSION_MAX_CHARS as CHARTER_MISSION_MAX_CHARS
from steward.manifest_models import (
    CHAT_ROUTE_KIND,
    CLOSE_OF_DAY,
    DEFAULT_BOARD_LEASE_S,
    DEFAULT_BOARD_TIMEOUT_S,
    DEFAULT_JOURNAL_DIR,
    DEFAULT_KEEP_ENTRIES,
    DELEGATION_ROUTE_KIND,
    JOB_BOARD_ROUTE_KIND,
    MANIFEST_FILENAME,
    NOTIFICATION_KINDS,
    NOTIFICATION_TRANSPORTS,
    QUIET_WORD_MAX_CHARS,
    QUIET_WORD_PATTERN,
    ROUTINE_DELIVER_CHAT,
    SCHEMA_VERSION,
    SESSION_GRANT_RESIDENTS_GRANT_SKILL,
    SESSION_GRANT_SKILLS_WRITE,
    UNRESTRICTED_TOOLS,
    VOICE_MAX_CHARS,
    AppGrant,
    Board,
    Budgets,
    Charter,
    Delegation,
    Deploy,
    Escalation,
    Memory,
    Mount,
    Notifications,
    PermissionMode,
    Resident,
    ResidentManifest,
    Route,
    Routine,
    RoutineDeliverKind,
    Runner,
    SessionGrant,
    SkillGrant,
    SoulDocument,
    SoulIdentity,
    ToolGrant,
    WorkspacePath,
    active_residents,
    chat_token_env_name,
    retired_complaint,
)
from steward.manifest_models import CHAT_TOKEN_ENV_PREFIX as CHAT_TOKEN_ENV_PREFIX
from steward.manifest_models import CONTAINER_PATTERN as CONTAINER_PATTERN
from steward.manifest_models import CRON_PATTERN as CRON_PATTERN
from steward.manifest_models import DEFAULT_SCHEDULE_TZ as DEFAULT_SCHEDULE_TZ
from steward.manifest_models import DEFAULT_SOUL_FILENAME as DEFAULT_SOUL_FILENAME
from steward.manifest_models import DISCORD_CHANNEL_NAME_MAX_CHARS as DISCORD_CHANNEL_NAME_MAX_CHARS
from steward.manifest_models import DISCORD_LISTEN_CHANNELS_MAX as DISCORD_LISTEN_CHANNELS_MAX
from steward.manifest_models import ESCALATION_HOW_MAX_CHARS as ESCALATION_HOW_MAX_CHARS
from steward.manifest_models import ESCALATION_MAX_CHARS as ESCALATION_MAX_CHARS
from steward.manifest_models import ESCALATION_NOTE_MAX_CHARS as ESCALATION_NOTE_MAX_CHARS
from steward.manifest_models import HOST_PATTERN as HOST_PATTERN
from steward.manifest_models import IMAGE_PATTERN as IMAGE_PATTERN
from steward.manifest_models import MANY_FIRES_DISPLAY_THRESHOLD as MANY_FIRES_DISPLAY_THRESHOLD
from steward.manifest_models import MAX_CLAIMS_PER_WAKE as MAX_CLAIMS_PER_WAKE
from steward.manifest_models import MAX_JOURNAL_KEEP as MAX_JOURNAL_KEEP
from steward.manifest_models import MAX_TIMEOUT_S as MAX_TIMEOUT_S
from steward.manifest_models import MCP_TOOL_PREFIX as MCP_TOOL_PREFIX
from steward.manifest_models import MEMORY_PATH_PATTERN as MEMORY_PATH_PATTERN
from steward.manifest_models import MOUNT_CONTAINER_PATH_PATTERN as MOUNT_CONTAINER_PATH_PATTERN
from steward.manifest_models import MOUNT_HOST_PATH_PATTERN as MOUNT_HOST_PATH_PATTERN
from steward.manifest_models import NO_MANIFESTS_PROBLEM as NO_MANIFESTS_PROBLEM
from steward.manifest_models import NOTIFY_TASK_DONE as NOTIFY_TASK_DONE
from steward.manifest_models import PROJECT_PATTERN as PROJECT_PATTERN
from steward.manifest_models import REMOTE_PATH_PATTERN as REMOTE_PATH_PATTERN
from steward.manifest_models import RETIRED_REASON as RETIRED_REASON
from steward.manifest_models import ROUTINE_DELIVER_PATTERN as ROUTINE_DELIVER_PATTERN
from steward.manifest_models import ROUTINE_PROMPT_MAX_CHARS as ROUTINE_PROMPT_MAX_CHARS
from steward.manifest_models import (
    SESSION_GRANT_RESIDENTS_DECLARE as SESSION_GRANT_RESIDENTS_DECLARE,
)
from steward.manifest_models import (
    SESSION_GRANT_RESIDENTS_DRY_RUN as SESSION_GRANT_RESIDENTS_DRY_RUN,
)
from steward.manifest_models import (
    SESSION_GRANT_RESIDENTS_REHEARSE as SESSION_GRANT_RESIDENTS_REHEARSE,
)
from steward.manifest_models import SLUG_PATTERN as SLUG_PATTERN
from steward.manifest_models import SOUL_FILE_PATTERN as SOUL_FILE_PATTERN
from steward.manifest_models import SOUL_NAME_MAX_CHARS as SOUL_NAME_MAX_CHARS
from steward.manifest_models import SOUL_ROLE_MAX_CHARS as SOUL_ROLE_MAX_CHARS
from steward.manifest_models import SSH_USER_PATTERN as SSH_USER_PATTERN
from steward.manifest_models import SUMMARY_MAX_CHARS as SUMMARY_MAX_CHARS
from steward.manifest_models import TOOL_NAME_PATTERN as TOOL_NAME_PATTERN
from steward.manifest_models import VILLAGE_HOME_MAX as VILLAGE_HOME_MAX
from steward.manifest_models import VILLAGE_HOME_MIN as VILLAGE_HOME_MIN
from steward.manifest_models import VOICE_HEADING as VOICE_HEADING
from steward.manifest_models import WORKSPACE_PATH_PATTERN as WORKSPACE_PATH_PATTERN
from steward.manifest_models import CharterEntry as CharterEntry
from steward.manifest_models import NotificationKind as NotificationKind
from steward.manifest_models import NotificationTransport as NotificationTransport
from steward.manifest_models import SkillGrantInput as SkillGrantInput
from steward.manifest_models import SkillGrantShorthand as SkillGrantShorthand
from steward.manifest_models import SkillId as SkillId
from steward.manifest_models import ToolName as ToolName
from steward.manifest_policy import METERED_BUDGET_FIELDS as METERED_BUDGET_FIELDS
from steward.manifest_policy import UNBOUNDABLE_RUNNER_KINDS as UNBOUNDABLE_RUNNER_KINDS
from steward.manifest_policy import UNMETERED_RUNNER_KINDS as UNMETERED_RUNNER_KINDS
from steward.manifest_policy import (
    _check_board_route,
    _check_budget_is_enforceable,
    _check_budget_runtime,
    _check_delegation,
    _check_deliveries_have_a_door,
    _check_directory_name,
    _check_duplicate_ids,
    _check_notifications_are_deliverable,
    _check_routine_requirements,
    _check_soul_agreement,
    _check_tools_are_enforceable,
)
from steward.schedule_policy import _check_close_of_day
from steward.schedule_policy import _daily_fire_range as _daily_fire_range
from steward.schedule_policy import _gregorian_cron_days as _gregorian_cron_days
from steward.skills import SkillLibrary as SkillLibrary
from steward.skills import effective_names as effective_names
from steward.skills import grant_diagnostics as grant_diagnostics
from steward.skills import library_for as library_for
from steward.soul import extract_voice, split_frontmatter
from steward.soul import parse_soul as parse_soul

__all__ = [
    "CHAT_ROUTE_KIND",
    "CLOSE_OF_DAY",
    "DEFAULT_BOARD_LEASE_S",
    "DEFAULT_BOARD_TIMEOUT_S",
    "DEFAULT_JOURNAL_DIR",
    "DEFAULT_KEEP_ENTRIES",
    "DELEGATION_ROUTE_KIND",
    "JOB_BOARD_ROUTE_KIND",
    "MANIFEST_FILENAME",
    "NOTIFICATION_KINDS",
    "NOTIFICATION_TRANSPORTS",
    "QUIET_WORD_MAX_CHARS",
    "QUIET_WORD_PATTERN",
    "ROUTINE_DELIVER_CHAT",
    "SCHEMA_VERSION",
    "SECRET_REDACTION",
    "SESSION_GRANT_RESIDENTS_GRANT_SKILL",
    "SESSION_GRANT_SKILLS_WRITE",
    "UNRESTRICTED_TOOLS",
    "VOICE_MAX_CHARS",
    "AppGrant",
    "Board",
    "Budgets",
    "Charter",
    "Delegation",
    "Deploy",
    "Diagnostic",
    "Escalation",
    "ManifestError",
    "Memory",
    "Mount",
    "Notifications",
    "PermissionMode",
    "Resident",
    "ResidentManifest",
    "Route",
    "Routine",
    "RoutineDeliverKind",
    "Runner",
    "SessionGrant",
    "Severity",
    "SkillGrant",
    "SoulDocument",
    "SoulIdentity",
    "ToolGrant",
    "ValidationResult",
    "WorkspacePath",
    "active_residents",
    "chat_token_env_name",
    "closest_match",
    "extract_voice",
    "load_manifest",
    "manifest_json_schema",
    "redact_mapping",
    "redact_secrets",
    "residents_root",
    "resolve_mount_host_path",
    "retired_complaint",
    "split_frontmatter",
    "validate_manifest",
    "validate_path",
    "validate_paths",
    "validate_tree",
]


@dataclass(frozen=True, slots=True)
class ValidationResult:
    """The structured outcome of validating one or more manifests."""

    residents: tuple[Resident, ...] = ()
    diagnostics: tuple[Diagnostic, ...] = ()

    @property
    def errors(self) -> tuple[Diagnostic, ...]:
        """Diagnostics that fail validation."""
        return tuple(d for d in self.diagnostics if d.severity is Severity.ERROR)

    @property
    def warnings(self) -> tuple[Diagnostic, ...]:
        """Diagnostics worth printing that do not fail validation."""
        return tuple(d for d in self.diagnostics if d.severity is Severity.WARNING)

    @property
    def ok(self) -> bool:
        """True when nothing failed."""
        return not self.errors

    def merged_with(self, other: ValidationResult) -> ValidationResult:
        """Combine two results, preserving order."""
        return ValidationResult(
            residents=self.residents + other.residents,
            diagnostics=self.diagnostics + other.diagnostics,
        )


def _read_yaml(path: Path) -> tuple[object, list[Diagnostic]]:
    try:
        text = path.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        return None, [
            Diagnostic(
                file=path,
                field_path="<file>",
                problem=f"manifest is not valid UTF-8: {exc}",
                example=f"a UTF-8 encoded {MANIFEST_FILENAME}",
            )
        ]
    except OSError as exc:
        return None, [
            Diagnostic(
                file=path,
                field_path="<file>",
                problem=f"cannot read manifest: {exc.strerror or exc}",
                example=f"a readable {MANIFEST_FILENAME}",
            )
        ]
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        return None, [
            Diagnostic(
                file=path,
                field_path="<file>",
                problem=f"manifest is not valid YAML: {exc}",
                example="version: 0\nid: hob\n…",
            )
        ]
    if data is None:
        return None, [
            Diagnostic(
                file=path,
                field_path="<file>",
                problem="manifest is empty",
                example="version: 0\nid: hob\n…",
            )
        ]
    if not isinstance(data, Mapping):
        return None, [
            Diagnostic(
                file=path,
                field_path="<root>",
                problem="manifest must be a mapping of fields at the top level",
                example="version: 0\nid: hob\n…",
            )
        ]
    return data, []


def _load_soul(manifest: ResidentManifest, source: Path) -> tuple[SoulDocument, list[Diagnostic]]:
    soul_path = source.parent / manifest.soul.file
    if not soul_path.is_file():
        return SoulDocument(path=soul_path), [
            Diagnostic(
                file=source,
                field_path="soul.file",
                problem=f"soul file {manifest.soul.file!r} does not exist next to the manifest",
                example=f"create {soul_path.name} with frontmatter and a short body",
            )
        ]
    try:
        text = soul_path.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        return SoulDocument(path=soul_path), [
            Diagnostic(
                file=soul_path,
                field_path="soul.file",
                problem=f"soul file is not valid UTF-8: {exc}",
                example=f"save {soul_path.name} as UTF-8",
            )
        ]
    except OSError as exc:
        return SoulDocument(path=soul_path), [
            Diagnostic(
                file=soul_path,
                field_path="soul.file",
                problem=f"cannot read soul file: {exc.strerror or exc}",
                example=f"a readable {soul_path.name} next to the manifest",
            )
        ]
    return parse_soul(text, soul_path)


def validate_manifest(path: Path | str, skills_dir: Path | str | None = None) -> ValidationResult:
    """Validate one manifest file and return structured diagnostics.

    This never raises for manifest problems: the caller decides what to do with the
    diagnostics. Use :func:`load_manifest` when you want the model or an exception.

    ``skills_dir`` names the skills library granted names are checked against. It
    defaults to ``<residents_dir>/../skills`` when that exists, and to no library at
    all when it does not — a tree with no library validates exactly as it did before
    the library landed.
    """
    source = Path(path)
    library = library_for(source.parent.parent, skills_dir)
    result = _validate_manifest(source, library)
    if not library.diagnostics:
        return result
    return ValidationResult(diagnostics=library.diagnostics).merged_with(result)


def _validate_manifest(source: Path, library: SkillLibrary) -> ValidationResult:
    """Validate one manifest against an already-loaded library."""
    data, diagnostics = _read_yaml(source)
    if data is None:
        return ValidationResult(diagnostics=tuple(diagnostics))

    credential_diagnostics = scan_for_credentials(data, source)
    if credential_diagnostics:
        # Stop before binding a secret into a model.
        return ValidationResult(diagnostics=tuple(credential_diagnostics))

    try:
        manifest = ResidentManifest.model_validate(data)
    except ValidationError as exc:
        return ValidationResult(diagnostics=tuple(_diagnostics_from_validation_error(exc, source)))

    soul, soul_diagnostics = _load_soul(manifest, source)
    diagnostics.extend(soul_diagnostics)
    diagnostics.extend(_check_directory_name(manifest, source))
    diagnostics.extend(_check_duplicate_ids(manifest, source))
    diagnostics.extend(grant_diagnostics(manifest, library, source))
    diagnostics.extend(
        _check_routine_requirements(manifest, source, effective_names(manifest, library))
    )
    diagnostics.extend(_check_close_of_day(manifest, source))
    diagnostics.extend(_check_board_route(manifest, source))
    diagnostics.extend(_check_notifications_are_deliverable(manifest, source))
    diagnostics.extend(_check_deliveries_have_a_door(manifest, source))
    diagnostics.extend(_check_budget_runtime(manifest, source))
    diagnostics.extend(_check_budget_is_enforceable(manifest, source))
    diagnostics.extend(_check_tools_are_enforceable(manifest, source))
    diagnostics.extend(_check_workspace_is_reachable(manifest, source))
    diagnostics.extend(_check_mount_collisions(manifest, source))
    diagnostics.extend(_check_deployment_settings(manifest, source))
    diagnostics.extend(_check_container_zone(manifest, source))
    diagnostics.extend(_check_placement(manifest, source))
    diagnostics.extend(_check_delegation(manifest, source))
    diagnostics.extend(_check_soul_agreement(manifest, soul, source))

    resident = Resident(path=source, manifest=manifest, soul=soul)
    residents = (resident,) if not any(d.severity is Severity.ERROR for d in diagnostics) else ()
    return ValidationResult(residents=residents, diagnostics=tuple(diagnostics))


def load_manifest(path: Path | str, skills_dir: Path | str | None = None) -> Resident:
    """Load and validate one manifest, or raise :class:`ManifestError`.

    The single load-and-validate path shared by the scheduler, the API, and CI.
    """
    result = validate_manifest(path, skills_dir)
    if not result.ok or not result.residents:
        raise ManifestError(result.diagnostics)
    return result.residents[0]


def validate_tree(
    residents_dir: Path | str, skills_dir: Path | str | None = None
) -> ValidationResult:
    """Validate every ``<id>/manifest.yaml`` under a residents directory.

    The skills library is loaded once for the whole tree, so a broken ``SKILL.md`` is
    one complaint rather than one per resident.
    """
    root = Path(residents_dir)
    if not root.is_dir():
        return ValidationResult(
            diagnostics=(
                Diagnostic(
                    file=root,
                    field_path="<path>",
                    problem="residents directory does not exist",
                    example="residents/<id>/manifest.yaml",
                ),
            )
        )

    library = library_for(root, skills_dir)
    result = ValidationResult(diagnostics=library.diagnostics)
    found = False
    for directory in sorted(p for p in root.iterdir() if p.is_dir()):
        manifest_path = directory / MANIFEST_FILENAME
        if not manifest_path.is_file():
            result = result.merged_with(
                ValidationResult(
                    diagnostics=(
                        Diagnostic(
                            file=directory,
                            field_path="<directory>",
                            problem=f"resident directory has no {MANIFEST_FILENAME}",
                            example=f"{directory.name}/{MANIFEST_FILENAME}",
                        ),
                    )
                )
            )
            continue
        found = True
        result = result.merged_with(_validate_manifest(manifest_path, library))

    result = result.merged_with(
        ValidationResult(diagnostics=tuple(_check_unique_uids(result.residents)))
    )
    result = result.merged_with(
        ValidationResult(diagnostics=tuple(_check_unique_agent_ids(result.residents)))
    )
    result = result.merged_with(
        ValidationResult(diagnostics=tuple(_check_unique_homes(result.residents)))
    )
    result = result.merged_with(
        ValidationResult(diagnostics=tuple(_check_shared_journal_dirs(result.residents)))
    )
    result = result.merged_with(
        ValidationResult(diagnostics=tuple(_check_competing_mount_writers(result.residents)))
    )

    if not found and not result.diagnostics:
        result = ValidationResult(
            diagnostics=(
                Diagnostic(
                    file=root,
                    field_path="<path>",
                    problem=NO_MANIFESTS_PROBLEM,
                    example="residents/hob/manifest.yaml",
                    severity=Severity.WARNING,
                ),
            )
        )
    return result


def validate_path(path: Path | str, skills_dir: Path | str | None = None) -> ValidationResult:
    """Validate a manifest file, a resident directory, or a whole residents tree."""
    target = Path(path)
    if target.is_file():
        return validate_manifest(target, skills_dir)
    if (target / MANIFEST_FILENAME).is_file():
        return validate_manifest(target / MANIFEST_FILENAME, skills_dir)
    return validate_tree(target, skills_dir)


def residents_root(path: Path | str) -> Path:
    """Return the residents tree a :func:`validate_path` target belongs to.

    All three shapes that function accepts live *in* a tree, and the skills library is
    beside the tree — ``<residents_dir>/../skills`` — never beside the target. Validation
    already makes this reduction internally, which is why a manifest file is validated
    against ``source.parent.parent``'s library. A caller that has to name the same library
    for itself must make it too: handing :func:`steward.skills.library_for` a *resident*
    directory looks for ``residents/<id>/../skills`` and finds nothing, and an unconfigured
    library answers "no skill is missing, no run would materialize anything" to every
    question asked of it — a green line that means "there was nothing to check".
    """
    target = Path(path)
    if target.is_file():
        return target.resolve().parent.parent
    if (target / MANIFEST_FILENAME).is_file():
        return target.resolve().parent
    return target


def validate_paths(
    paths: Iterable[Path | str], skills_dir: Path | str | None = None
) -> ValidationResult:
    """Validate several targets, merging their results in order."""
    result = ValidationResult()
    for path in paths:
        result = result.merged_with(validate_path(path, skills_dir))
    return result


#: Where the generated schema is committed, relative to the repo root — the path the
#: ``$id`` below promises. ``make schema-write`` writes it; tests/test_schema_contract.py
#: fails when the committed copy drifts from what this module generates.
SCHEMA_ARTIFACT = "schema/resident-manifest-v0.json"


def manifest_json_schema() -> dict[str, Any]:
    """Return the manifest JSON Schema, so burrow can read manifests without translation."""
    schema = ResidentManifest.model_json_schema()
    schema["$id"] = f"https://github.com/0xCommanderKeen/steward/{SCHEMA_ARTIFACT}"
    schema["title"] = "steward resident manifest v0"
    return schema


def manifest_schema_json() -> str:
    """Render the schema exactly as the committed artifact holds it: indent 2, one newline.

    One function so the CLI, the make target and the drift test cannot disagree about
    whitespace — a contract test that failed over a missing trailing newline would teach
    everyone to stop reading it.
    """
    return json.dumps(manifest_json_schema(), indent=2) + "\n"
