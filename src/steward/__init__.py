"""steward — the control plane for the agent fleet that burrow watches.

Souls and resident manifests are versioned here; burrow only ever reads and renders
them. The public surface is the manifest load-and-validate path, the runner seam every
session launch goes through, the prompt assembly point, and the scheduler that fires
routines and emits what really happened.
"""

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
from steward.prompt import assemble_preamble, assemble_routine_prompt
from steward.runners import Outcome, Runner, RunRequest, RunResult, build_runner
from steward.scheduler import ScheduledRoutine, Scheduler, SchedulerState, load_scheduled

__all__ = [
    "Diagnostic",
    "Event",
    "EventEmitter",
    "ManifestError",
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
    "ValidationResult",
    "assemble_preamble",
    "assemble_routine_prompt",
    "build_runner",
    "load_manifest",
    "load_scheduled",
    "manifest_json_schema",
    "validate_manifest",
    "validate_path",
    "validate_paths",
    "validate_tree",
]
