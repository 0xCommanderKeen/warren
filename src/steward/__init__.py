"""steward — the control plane for the agent fleet that burrow watches.

Souls and resident manifests are versioned here; burrow only ever reads and renders
them. The public surface is the manifest load-and-validate path, the runner seam every
session launch goes through, the prompt assembly point, and the scheduler that fires
routines and emits what really happened.
"""

from steward.api import ApiConfig, ApiError, create_app
from steward.events import Event, EventEmitter, NullEmitter
from steward.manifest import (
    Diagnostic,
    ManifestError,
    Resident,
    ResidentManifest,
    Severity,
    ValidationResult,
    load_manifest,
    manifest_json_schema,
    validate_manifest,
    validate_path,
    validate_paths,
    validate_tree,
)
from steward.nursery import NewResident, declare_resident
from steward.prompt import assemble_preamble, assemble_routine_prompt
from steward.runners import Outcome, Runner, RunRequest, RunResult, build_runner
from steward.scheduler import ScheduledRoutine, Scheduler, SchedulerState, load_scheduled
from steward.store import Store

__all__ = [
    "ApiConfig",
    "ApiError",
    "Diagnostic",
    "Event",
    "EventEmitter",
    "ManifestError",
    "NewResident",
    "NullEmitter",
    "Outcome",
    "Resident",
    "ResidentManifest",
    "RunRequest",
    "RunResult",
    "Runner",
    "ScheduledRoutine",
    "Scheduler",
    "SchedulerState",
    "Severity",
    "Store",
    "ValidationResult",
    "assemble_preamble",
    "assemble_routine_prompt",
    "build_runner",
    "create_app",
    "declare_resident",
    "load_manifest",
    "load_scheduled",
    "manifest_json_schema",
    "validate_manifest",
    "validate_path",
    "validate_paths",
    "validate_tree",
]
