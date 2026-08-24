"""steward — the control plane for the agent fleet that burrow watches.

Souls and resident manifests are versioned here; burrow only ever reads and renders
them. The public surface is the manifest load-and-validate path, the runner seam every
session launch goes through, the prompt assembly point, the skills library every
resident draws on, the resident journal (the one read path into a resident's durable
memory), the scheduler that fires routines and emits what really happened, and the job
board and approvals that hang off it — how a resident picks work up without being told
to, and what it does when its charter says stop.
"""

from steward.api import ApiConfig, ApiError, create_app
from steward.approvals import NeedsHuman, extract_requests, harvest
from steward.board import BoardReport, Dispatcher, DispatchRun, claimable_skills
from steward.events import Event, EventEmitter, NullEmitter
from steward.journal import (
    JournalEntry,
    journal_complaint,
    latest_entry,
    read_entries,
    resolve_journal_dir,
)
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
from steward.prompt import assemble_preamble, assemble_routine_prompt, assemble_task_prompt
from steward.runners import Outcome, Runner, RunRequest, RunResult, build_runner
from steward.scheduler import (
    ScheduledRoutine,
    Scheduler,
    SchedulerState,
    WakeHooks,
    load_scheduled,
)
from steward.skills import (
    Skill,
    SkillLibrary,
    default_skills,
    effective_skills,
    library_for,
    load_library,
)
from steward.store import Store

__all__ = [
    "ApiConfig",
    "ApiError",
    "BoardReport",
    "Diagnostic",
    "DispatchRun",
    "Dispatcher",
    "Event",
    "EventEmitter",
    "JournalEntry",
    "ManifestError",
    "NeedsHuman",
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
    "Skill",
    "SkillLibrary",
    "Store",
    "ValidationResult",
    "WakeHooks",
    "assemble_preamble",
    "assemble_routine_prompt",
    "assemble_task_prompt",
    "build_runner",
    "claimable_skills",
    "create_app",
    "declare_resident",
    "default_skills",
    "effective_skills",
    "extract_requests",
    "harvest",
    "journal_complaint",
    "latest_entry",
    "library_for",
    "load_library",
    "load_manifest",
    "load_scheduled",
    "manifest_json_schema",
    "read_entries",
    "resolve_journal_dir",
    "validate_manifest",
    "validate_path",
    "validate_paths",
    "validate_tree",
]
