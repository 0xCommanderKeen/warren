"""steward — the control plane for the agent fleet that burrow watches.

Souls and resident manifests are versioned here; burrow only ever reads and renders
them. The public surface is the manifest load-and-validate path, the runner seam every
session launch goes through, the prompt assembly point, the skills library every
resident draws on, the resident journal (the one read path into a resident's durable
memory), the scheduler that fires routines and emits what really happened, and the job
board, approvals, and delegation that hang off it — how a resident picks work up
without being told to, what it does when its charter says stop, and how it hands work to
a neighbour when the work is not its own — and the watchdog and budgets that keep an
unattended resident both alive and bounded, whichever of those ways it woke up.
"""

from steward.api import ApiConfig, ApiError, create_app
from steward.approvals import NeedsHuman, extract_requests, harvest
from steward.board import BoardReport, Dispatcher, DispatchRun, claimable_skills
from steward.budgets import BudgetGuard, BudgetStatus, day_window, primary_tz
from steward.delegation import DelegationError, Delegator, Handoff, extract_handoffs
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
from steward.prompt import (
    assemble_delegated_prompt,
    assemble_preamble,
    assemble_routine_prompt,
    assemble_task_prompt,
)
from steward.runners import Outcome, Runner, RunRequest, RunResult, build_runner
from steward.scheduler import (
    ScheduledRoutine,
    Scheduler,
    SchedulerState,
    WakeHooks,
    load_scheduled,
)
from steward.sessions import (
    Admission,
    DelegatedWake,
    Refusal,
    ResidentSessions,
    RoutineWake,
    TaskWake,
)
from steward.skills import (
    Skill,
    SkillLibrary,
    default_skills,
    effective_skills,
    library_for,
    load_library,
)
from steward.store import LedgerEntry, PauseRecord, Store
from steward.watchdog import DockerSupervisor, Health, LocalProbe, ProcessSupervisor, Watchdog

__all__ = [
    "Admission",
    "ApiConfig",
    "ApiError",
    "BoardReport",
    "BudgetGuard",
    "BudgetStatus",
    "DelegatedWake",
    "DelegationError",
    "Delegator",
    "Diagnostic",
    "DispatchRun",
    "Dispatcher",
    "DockerSupervisor",
    "Event",
    "EventEmitter",
    "Handoff",
    "Health",
    "JournalEntry",
    "LedgerEntry",
    "LocalProbe",
    "ManifestError",
    "NeedsHuman",
    "NewResident",
    "NullEmitter",
    "Outcome",
    "PauseRecord",
    "ProcessSupervisor",
    "Refusal",
    "Resident",
    "ResidentManifest",
    "ResidentSessions",
    "RoutineWake",
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
    "TaskWake",
    "ValidationResult",
    "WakeHooks",
    "Watchdog",
    "assemble_delegated_prompt",
    "assemble_preamble",
    "assemble_routine_prompt",
    "assemble_task_prompt",
    "build_runner",
    "claimable_skills",
    "create_app",
    "day_window",
    "declare_resident",
    "default_skills",
    "effective_skills",
    "extract_handoffs",
    "extract_requests",
    "harvest",
    "journal_complaint",
    "latest_entry",
    "library_for",
    "load_library",
    "load_manifest",
    "load_scheduled",
    "manifest_json_schema",
    "primary_tz",
    "read_entries",
    "resolve_journal_dir",
    "validate_manifest",
    "validate_path",
    "validate_paths",
    "validate_tree",
]
