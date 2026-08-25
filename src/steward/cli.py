"""``steward`` command line: one load-and-validate path, shared with CI."""

import json
import logging
import sys
from collections.abc import Callable, Sequence
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import click
import yaml
from pydantic import ValidationError

from steward import events as ev
from steward.api import (
    DEFAULT_HOST,
    DEFAULT_PORT,
    ApiConfig,
    ApiError,
    create_app,
    origins_summary,
    run_server,
)
from steward.approvals import (
    ACTION_PATTERN,
    DEFAULT_EXPIRES_IN_S,
    ApprovalError,
    NeedsHuman,
    parse_duration,
    parse_options,
    raise_request,
)
from steward.board import BoardReport, Dispatcher, claimable_skills
from steward.budgets import BudgetGuard, BudgetStatus
from steward.delegation import DelegationError, Delegator, Handoff, max_depth
from steward.journal import (
    JournalEntry,
    journal_complaint,
    read_entries,
    resolve_journal_dir,
)
from steward.manifest import (
    Diagnostic,
    ManifestError,
    Resident,
    Severity,
    ValidationResult,
    manifest_json_schema,
    validate_paths,
)
from steward.nursery import (
    NewResident,
    NurseryError,
    NurseryReport,
    RetireReport,
    raise_resident,
    retire_resident,
)
from steward.runners import check_runner, skills_home
from steward.scheduler import (
    DEFAULT_CATCHUP_S,
    FireReport,
    ScheduledRoutine,
    Scheduler,
    SchedulerError,
    SchedulerState,
    default_state_path,
    load_scheduled,
)
from steward.skills import Skill, SkillLibrary, effective_skills, library_for
from steward.store import (
    APPROVAL_DECISIONS,
    JobRecord,
    OriginSpend,
    Store,
    default_db_path,
)
from steward.watchdog import DEFAULT_INTERVAL_S, Watchdog, WatchdogPass

DEFAULT_RESIDENTS_DIR = Path("residents")
EXIT_OK = 0
EXIT_INVALID = 1

#: The two options half the commands here share. Declared once, at the top, because a
#: subcommand that spelled ``--db`` slightly differently from its neighbour would be a
#: subcommand quietly reading a different database.
_DB_OPTION = click.option(
    "--db",
    type=click.Path(path_type=Path),
    default=None,
    help="Jobs, approvals, and the run ledger. Defaults to steward.db beside $STEWARD_STATE.",
)
_RESIDENTS_OPTION = click.option(
    "--residents",
    type=click.Path(path_type=Path),
    default=DEFAULT_RESIDENTS_DIR,
    show_default=True,
    help="Residents tree the manifests are read from.",
)


def _diagnostic_as_dict(diagnostic: Diagnostic) -> dict[str, str]:
    payload = asdict(diagnostic)
    payload["file"] = str(diagnostic.file)
    payload["severity"] = diagnostic.severity.value
    return payload


def _report_text(result: ValidationResult, targets: Sequence[Path]) -> None:
    for diagnostic in result.diagnostics:
        style = "red" if diagnostic.severity is Severity.ERROR else "yellow"
        click.secho(diagnostic.render(), fg=style, err=not result.ok)
        click.echo("", err=not result.ok)

    checked = ", ".join(str(target) for target in targets)
    counts = (
        f"{len(result.residents)} valid resident(s), "
        f"{len(result.errors)} error(s), {len(result.warnings)} warning(s)"
    )
    if result.ok:
        click.secho(f"ok: {counts} in {checked}", fg="green")
    else:
        click.secho(f"failed: {counts} in {checked}", fg="red", err=True)


def _report_json(result: ValidationResult) -> None:
    payload = {
        "ok": result.ok,
        "residents": [
            {
                "id": resident.id,
                "agent_id": resident.manifest.agent_id,
                "path": str(resident.path),
            }
            for resident in result.residents
        ],
        "diagnostics": [_diagnostic_as_dict(d) for d in result.diagnostics],
    }
    click.echo(json.dumps(payload, indent=2))


@click.group()
@click.version_option(package_name="steward")
def main() -> None:
    """Steward — the control plane for the agent fleet burrow watches."""


@main.command()
@click.argument(
    "paths",
    nargs=-1,
    type=click.Path(exists=True, path_type=Path),
)
@click.option(
    "--format",
    "output_format",
    type=click.Choice(["text", "json"]),
    default="text",
    help="How to report diagnostics.",
)
@click.option(
    "--skills",
    "skills_dir",
    type=click.Path(path_type=Path),
    default=None,
    help="The skills library granted names are checked against. Defaults to skills/ "
    "beside the residents tree.",
)
def validate(paths: tuple[Path, ...], output_format: str, skills_dir: Path | None) -> None:
    """Validate resident manifests. Defaults to the residents/ tree.

    Exits non-zero if any manifest fails, so CI can gate on it. A granted skill that
    names nothing in the library is one of the failures.
    """
    targets = list(paths) or [DEFAULT_RESIDENTS_DIR]
    result = validate_paths(targets, skills_dir)
    if output_format == "json":
        _report_json(result)
    else:
        _report_text(result, targets)
    sys.exit(EXIT_OK if result.ok else EXIT_INVALID)


@main.command()
def schema() -> None:
    """Print the resident manifest JSON Schema (burrow reads manifests from this)."""
    click.echo(json.dumps(manifest_json_schema(), indent=2))


# --------------------------------------------------------------------------------------
# skills
# --------------------------------------------------------------------------------------


@main.command("skills")
@click.option(
    "--residents",
    type=click.Path(path_type=Path),
    default=DEFAULT_RESIDENTS_DIR,
    show_default=True,
    help="Residents tree whose effective sets are listed.",
)
@click.option(
    "--skills",
    "skills_dir",
    type=click.Path(path_type=Path),
    default=None,
    help="The skills library. Defaults to the skills/ directory beside the residents tree.",
)
@click.option("--format", "output_format", type=click.Choice(["text", "json"]), default="text")
def skills_command(residents: Path, skills_dir: Path | None, output_format: str) -> None:
    """List the skills library and each resident's effective set.

    The effective set is the library's defaults plus the resident's own grants — what a
    session for that resident will actually be told, without opening a manifest.
    """
    library = library_for(residents, skills_dir)
    result = validate_paths([residents], skills_dir)
    sets = {
        resident.id: effective_skills(resident.manifest, library) for resident in result.residents
    }

    if output_format == "json":
        click.echo(
            json.dumps(
                {
                    "library": str(library.path) if library.path else None,
                    "skills": [skill.as_dict() for skill in library],
                    "residents": {
                        resident_id: [skill.name for skill in skills]
                        for resident_id, skills in sorted(sets.items())
                    },
                    "diagnostics": [_diagnostic_as_dict(d) for d in library.diagnostics],
                },
                indent=2,
            )
        )
        sys.exit(EXIT_OK if not library.errors else EXIT_INVALID)

    _render_library(library)
    for resident in result.residents:
        _render_effective_set(resident, sets[resident.id], library)
    for diagnostic in library.diagnostics:
        click.secho(diagnostic.render(), fg="red", err=True)
    sys.exit(EXIT_OK if not library.errors else EXIT_INVALID)


def _render_library(library: SkillLibrary) -> None:
    if not library.configured:
        click.secho("no skills library found; residents hold no skills", fg="yellow")
        return
    click.secho(f"library {library.path}", fg="cyan", bold=True)
    if not len(library):
        click.echo("  (empty)")
    for skill in library:
        marker = "default" if skill.default else "granted"
        click.echo(f"  {skill.name}  [{marker}]  {skill.description}")
    click.echo("")


def _render_effective_set(
    resident: Resident, skills: Sequence[Skill], library: SkillLibrary
) -> None:
    names = ", ".join(skill.name for skill in skills) or "none"
    click.secho(f"{resident.id}: {names}", fg="green")
    disk = skills_home(resident.manifest.runner)
    where = (
        f"prompt + {disk}/ in the session's working directory"
        if disk and library.configured
        else "prompt only"
    )
    click.secho(f"  runner {resident.manifest.runner.kind} — {where}", fg="bright_black")


# --------------------------------------------------------------------------------------
# doctor
# --------------------------------------------------------------------------------------


@main.command()
@click.argument("residents", type=click.Path(path_type=Path), default=DEFAULT_RESIDENTS_DIR)
@_DB_OPTION
def doctor(residents: Path, db: Path | None) -> None:
    """Check that what the manifests declare can actually run, here, now.

    Names the brain each resident runs on, whether its binary exists, what it has spent
    today, and when each enabled routine fires next — then says when the watchdog last
    made a pass. A missing binary is an error at a reasonable hour rather than a routine
    that silently never happens at 7am, and a resident paused by its budget is a resident
    that will not fire tonight however green everything else looks.
    """
    result = validate_paths([residents])
    if not result.ok:
        _report_text(result, [residents])
        sys.exit(EXIT_INVALID)

    problems = 0
    with _open_store(db) as store:
        guard = BudgetGuard(store)
        for resident in result.residents:
            if resident.retired:
                # Still valid, still in git, still readable — and doing nothing. Saying
                # "ready" about it would be the one line of this report that is untrue.
                click.secho(f"{resident.id}: retired — fires nothing", fg="bright_black")
                continue
            runner = resident.manifest.runner
            complaint = check_runner(runner)
            label = f"{resident.id}: runner {runner.kind}"
            if runner.model:
                label += f" ({runner.model})"
            if complaint:
                problems += 1
                click.secho(f"{label} — {complaint}", fg="red", err=True)
            else:
                click.secho(f"{label} — ready", fg="green")
            problems += _report_journal(resident)
            problems += _report_budget(guard.status(resident.manifest))
        problems += _report_watchdog(store.last_watchdog_pass())

    scheduled = _load_or_exit(residents)
    if scheduled:
        engine = Scheduler(
            scheduled,
            state=SchedulerState(path=default_state_path()),
            library=library_for(residents),
        )
        for item, moment in engine.upcoming(datetime.now(UTC)):
            local = moment.strftime("%Y-%m-%d %H:%M")
            click.echo(
                f"  {item.key}: '{item.routine.schedule}' {item.routine.schedule_tz} "
                f"→ next {local} {item.routine.schedule_tz}"
            )
    else:
        click.echo("  no enabled routines")

    sys.exit(EXIT_OK if problems == 0 else EXIT_INVALID)


def _report_journal(resident: Resident) -> int:
    """Print where this resident's journal lives, or why it has none. Returns problems."""
    complaint = journal_complaint(resident.manifest)
    if complaint:
        click.secho(f"{resident.id}: journal — {complaint}", fg="red", err=True)
        return 1
    directory = resolve_journal_dir(resident.manifest, source=resident.path)
    closer = next(
        (r.id for r in resident.manifest.routines if r.enabled and r.journal == "close_of_day"),
        None,
    )
    ends_with = f"closed by {closer}" if closer else "no routine closes the day"
    click.secho(f"{resident.id}: journal {directory} — {ends_with}", fg="green")
    return 0


def _report_budget(status: BudgetStatus) -> int:
    """Print what this resident has spent today. A pause is a problem worth exiting on."""
    if status.paused:
        click.secho(f"{status.resident}: budget — {status.summary()}", fg="red", err=True)
        return 1
    colour = "green" if status.declared else "bright_black"
    click.secho(f"{status.resident}: budget {status.summary()}", fg=colour)
    return 0


def _report_watchdog(last: dict[str, Any] | None) -> int:
    """Say when the watchdog last swept, or that nothing is watching. Never a guess."""
    if last is None:
        click.secho(
            "watchdog: has never made a pass — nothing is noticing a stuck run or a dead "
            "container; run `steward watchdog run`",
            fg="yellow",
        )
        return 0
    click.secho(
        f"watchdog: last pass {last['last_pass_at']} "
        f"({last['passes']} pass(es), {last['interventions']} intervention(s))",
        fg="green",
    )
    return 0


def _load_or_exit(residents: Path) -> list[ScheduledRoutine]:
    try:
        return load_scheduled(residents)
    except SchedulerError as exc:
        click.secho(str(exc), fg="red", err=True)
        sys.exit(EXIT_INVALID)


# --------------------------------------------------------------------------------------
# journal
# --------------------------------------------------------------------------------------

DEFAULT_JOURNAL_LIMIT = 5


def _render_entry(entry: JournalEntry) -> None:
    heading = entry.date.isoformat()
    if entry.routine:
        heading += f"  ({entry.routine})"
    click.secho(heading, fg="cyan", bold=True)
    click.secho(str(entry.path), fg="bright_black")
    click.echo("")
    click.echo(entry.text)
    click.echo("")


@main.command("journal")
@click.argument("resident_id")
@click.option(
    "--residents",
    type=click.Path(path_type=Path),
    default=DEFAULT_RESIDENTS_DIR,
    show_default=True,
    help="Residents tree the manifest is read from.",
)
@click.option(
    "--limit",
    type=int,
    default=DEFAULT_JOURNAL_LIMIT,
    show_default=True,
    help="How many entries to print, newest first.",
)
@click.option("--format", "output_format", type=click.Choice(["text", "json"]), default="text")
def journal_command(resident_id: str, residents: Path, limit: int, output_format: str) -> None:
    """Print a resident's journal entries, newest first.

    Read-only, and read from the location the resident's own manifest declares. This
    prints what the resident wrote; when it has written nothing it says so, because an
    empty journal is a real answer.
    """
    resident = _resident_or_exit(residents, resident_id)
    try:
        entries = read_entries(resident.manifest, limit, source=resident.path)
        directory = resolve_journal_dir(resident.manifest, source=resident.path)
    except ManifestError as exc:
        click.secho(f"{resident.path}: {exc}", fg="red", err=True)
        sys.exit(EXIT_INVALID)

    if output_format == "json":
        click.echo(json.dumps([entry.as_dict() for entry in entries], indent=2))
        return
    if not entries:
        click.echo(f"{resident_id} has not written a journal entry yet ({directory})")
        return
    for entry in entries:
        _render_entry(entry)


# --------------------------------------------------------------------------------------
# scheduler
# --------------------------------------------------------------------------------------


@main.group()
def scheduler() -> None:
    """Fire residents' routines. Nothing fires unless this is running."""


def _scheduler_options[F: Callable[..., None]](function: F) -> F:
    """Apply the options every scheduler subcommand shares, in one place."""
    for option in reversed(
        [
            click.option(
                "--residents",
                type=click.Path(path_type=Path),
                default=DEFAULT_RESIDENTS_DIR,
                show_default=True,
                help="Residents tree to schedule from.",
            ),
            click.option(
                "--state",
                type=click.Path(path_type=Path),
                default=None,
                help="Where last-fire state lives. Defaults to $STEWARD_STATE.",
            ),
            click.option(
                "--workdir",
                type=click.Path(path_type=Path),
                default=None,
                help="Fallback working directory when a resident's memory dir is absent.",
            ),
            click.option(
                "--db",
                type=click.Path(path_type=Path),
                default=None,
                help="Jobs and approvals. Defaults to steward.db beside $STEWARD_STATE.",
            ),
            click.option(
                "--catchup-seconds",
                type=float,
                default=DEFAULT_CATCHUP_S,
                show_default=True,
                help="How late a fire may be before steward skips it instead of back-filling.",
            ),
            click.option(
                "--dry-run",
                is_flag=True,
                help="Print what would fire, with the assembled prompt. Emits nothing.",
            ),
        ]
    ):
        function = option(function)
    return function


def _build_scheduler(  # noqa: PLR0913 — click passes one parameter per option
    residents: Path,
    state: Path | None,
    workdir: Path | None,
    db: Path | None,
    catchup_seconds: float,
    *,
    dry_run: bool,
) -> Scheduler:
    scheduled = _load_or_exit(residents)
    # A rehearsal touches no database: it must not claim a task, deliver a decision, deny
    # one by expiry, or spend a budget. With no hooks and no guard the scheduler simply
    # fires routines, as it did before the board, approvals, and budgets existed.
    if dry_run:
        hooks, guard = None, None
    else:
        store = _open_store(db)
        guard = BudgetGuard(store, ev.EventEmitter.from_env())
        hooks = Dispatcher.from_path(residents, store, workdir=workdir, guard=guard)
    return Scheduler(
        scheduled,
        state=SchedulerState.load(state if state is not None else default_state_path()),
        workdir=workdir,
        catchup_s=catchup_seconds,
        dry_run=dry_run,
        library=library_for(residents),
        hooks=hooks,
        guard=guard,
    )


def _open_store(db: Path | None) -> Store:
    """Open the jobs-and-approvals database, defaulting beside the scheduler's state."""
    return Store(db if db is not None else default_db_path())


def _report_fires(reports: Sequence[FireReport], *, dry_run: bool) -> None:
    if not reports:
        click.echo("nothing due")
        return
    for report in reports:
        if dry_run:
            click.secho(f"would fire {report.scheduled.key} (run {report.run_id})", fg="yellow")
            click.echo(report.prompt)
            click.echo("")
        elif not report.fired:
            click.secho(f"skipped {report.scheduled.key}: {report.skipped_reason}", fg="yellow")
        elif report.result is not None and report.result.ok:
            click.secho(f"ok {report.scheduled.key} in {report.result.duration_s:.1f}s", fg="green")
        else:
            detail = report.result.summary() if report.result else "no result"
            click.secho(f"failed {report.scheduled.key}: {detail}", fg="red", err=True)


@scheduler.command("tick")
@_scheduler_options
def scheduler_tick(  # noqa: PLR0913, PLR0917 — click passes one parameter per option
    residents: Path,
    state: Path | None,
    workdir: Path | None,
    db: Path | None,
    catchup_seconds: float,
    dry_run: bool,  # noqa: FBT001 — click passes flags positionally
) -> None:
    """Fire everything due right now, sweep the board, then exit. Good under cron."""
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    engine = _build_scheduler(residents, state, workdir, db, catchup_seconds, dry_run=dry_run)
    if dry_run:
        reports = [engine.fire(item) for item in engine.scheduled]
    else:
        try:
            engine.require_ready()
        except SchedulerError as exc:
            click.secho(str(exc), fg="red", err=True)
            sys.exit(EXIT_INVALID)
        reports = engine.tick()
    _report_fires(reports, dry_run=dry_run)


@scheduler.command("run")
@_scheduler_options
@click.option("--max-ticks", type=int, default=None, help="Stop after this many loops.")
def scheduler_run(  # noqa: PLR0913, PLR0917 — click passes one parameter per option
    residents: Path,
    state: Path | None,
    workdir: Path | None,
    db: Path | None,
    catchup_seconds: float,
    dry_run: bool,  # noqa: FBT001 — click passes flags positionally
    max_ticks: int | None,
) -> None:
    """Run the scheduler daemon: sleep to the next due routine, fire, repeat."""
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    engine = _build_scheduler(residents, state, workdir, db, catchup_seconds, dry_run=dry_run)
    if dry_run:
        _report_fires([engine.fire(item) for item in engine.scheduled], dry_run=True)
        return
    try:
        reports = engine.run(max_ticks=max_ticks)
    except SchedulerError as exc:
        click.secho(str(exc), fg="red", err=True)
        sys.exit(EXIT_INVALID)
    except KeyboardInterrupt:  # pragma: no cover — a human stopping the daemon
        click.echo("stopped")
        return
    _report_fires(reports, dry_run=False)


# --------------------------------------------------------------------------------------
# the job board
# --------------------------------------------------------------------------------------


@main.group()
def board() -> None:
    """Work the job board: see what is on it, and let residents pick it up."""


@board.command("dispatch")
@_RESIDENTS_OPTION
@_DB_OPTION
@click.option(
    "--workdir",
    type=click.Path(path_type=Path),
    default=None,
    help="Fallback working directory when a resident's memory dir is absent.",
)
@click.option(
    "--sweep-only",
    is_flag=True,
    help="Reopen dead leases and deny stale approvals, but claim nothing and run nothing.",
)
def board_dispatch(
    residents: Path,
    db: Path | None,
    workdir: Path | None,
    sweep_only: bool,  # noqa: FBT001 — click passes flags positionally
) -> None:
    """Sweep deadlines, then let board-enabled residents claim and work a task.

    The same call the scheduler makes on every tick, available on its own so a poke from
    a human or an external cron can pick work up without waiting for the next routine.

    ``--sweep-only`` is not a dry run and does not pretend to be one: it writes, because
    reopening a dead lease and denying a stale approval are exactly the writes that keep
    the board honest. It just stops before claiming anything.
    """
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    with _open_store(db) as store:
        dispatcher = Dispatcher.from_path(
            residents,
            store,
            workdir=workdir,
            guard=BudgetGuard(store, ev.EventEmitter.from_env()),
            sweep_only=sweep_only,
        )
        run = dispatcher.dispatch()
    for job in run.reopened:
        click.secho(f"lease expired: {job.task_id} ({job.title}) is back on the board", fg="yellow")
    for record in run.expired_approvals:
        click.secho(f"approval expired: {record.action} denied by default", fg="yellow")
    if not run.reports:
        click.echo("nothing claimed")
        return
    for report in run.reports:
        _report_board(report)


def _report_board(report: BoardReport) -> None:
    kind = f"delegated by {report.task.delegated_by}" if report.delegated else "claimed"
    label = f"{report.resident_id} → {report.task.task_id} ({report.task.title}, {kind})"
    if report.done:
        click.secho(f"done {label}", fg="green")
    else:
        click.secho(f"failed {label}: {report.reason}", fg="red", err=True)
    for record in report.raised:
        click.secho(f"  needs human: {record.message} [{record.request_id}]", fg="yellow")
    for delivery in report.handed_over:
        if delivery.task is not None:
            click.secho(
                f"  delegated: {delivery.task.title} → {delivery.task.assignee} "
                f"[{delivery.task.task_id}]",
                fg="cyan",
            )
        else:
            click.secho(f"  delegation refused ({delivery.reason}): {delivery.message}", fg="red")


@board.command("list")
@_DB_OPTION
@_RESIDENTS_OPTION
@click.option("--format", "output_format", type=click.Choice(["text", "json"]), default="text")
def board_list(db: Path | None, residents: Path, output_format: str) -> None:
    """Show the board, oldest first, and who could claim what is still open."""
    with _open_store(db) as store:
        jobs = store.jobs()
    if output_format == "json":
        click.echo(json.dumps([job.to_dict() for job in jobs], indent=2))
        return
    if not jobs:
        click.echo("the board is empty")
        return
    library = library_for(residents)
    holders = {
        resident.id: claimable_skills(resident.manifest, library)
        for resident in validate_paths([residents]).residents
        if resident.manifest.board.claim
    }
    for job in jobs:
        colour = {"open": "cyan", "claimed": "yellow", "done": "green"}.get(job.status, "red")
        click.secho(f"{job.status:<8} {job.task_id}  {job.title}", fg=colour)
        if job.required_skills:
            click.echo(f"         skills: {', '.join(job.required_skills)}")
        if job.delegated:
            click.echo(
                f"         delegated: {job.delegated_by} → {job.assignee} via {job.route} "
                f"(depth {job.depth})"
            )
        if job.claimant:
            click.echo(f"         claimant: {job.claimant} (lease {job.lease_expires_at})")
        elif job.status == "open" and not job.delegated:
            eligible = [name for name, skills in holders.items() if job.claimable_by <= skills]
            click.echo(f"         claimable by: {', '.join(eligible) or 'nobody on this tree'}")


# --------------------------------------------------------------------------------------
# delegation
# --------------------------------------------------------------------------------------


def _resident_or_exit(residents: Path, resident_id: str) -> Resident:
    """Find one valid resident in a tree, or refuse with the ones that are there."""
    result = validate_paths([residents])
    resident = next((r for r in result.residents if r.id == resident_id), None)
    if resident is None:
        known = ", ".join(sorted(r.id for r in result.residents)) or "none"
        click.secho(f"no valid resident {resident_id!r} in {residents} (found: {known})", fg="red")
        sys.exit(EXIT_INVALID)
    return resident


@main.command("delegate")
@click.argument("resident_id")
@click.option("--to", "recipient", required=True, help="The resident id receiving the work.")
@click.option("--route", required=True, help="A delegation route that resident declares.")
@click.option("--title", required=True, help="One line naming the work.")
@click.option("--detail", default=None, help="Everything the receiver needs, as prose.")
@click.option("--detail-json", default=None, help="The same, as a JSON object.")
@click.option(
    "--parent-task-id",
    default=None,
    help="The task this descends from. Carries lineage, depth, and budget attribution.",
)
@_RESIDENTS_OPTION
@_DB_OPTION
def delegate_command(  # noqa: PLR0913, PLR0917 — click passes one parameter per option
    resident_id: str,
    recipient: str,
    route: str,
    title: str,
    detail: str | None,
    detail_json: str | None,
    parent_task_id: str | None,
    residents: Path,
    db: Path | None,
) -> None:
    """Hand work from one resident to another, from inside a session.

    Token-free and local, exactly like `steward approval raise`: a headless session with
    shell access calls this directly rather than holding steward's API token. Steward is
    still the arbiter — both manifests have to permit the handoff, the chain may not run
    too deep, and it may never revisit a resident — and a refusal exits non-zero with the
    reason, having written and emitted nothing.

    Nothing waits: the receiver picks the work up on its own next wake-up.
    """
    sender = _resident_or_exit(residents, resident_id)
    if detail and detail_json:
        click.secho("pass --detail or --detail-json, not both", fg="red", err=True)
        sys.exit(EXIT_INVALID)
    body = detail or ""
    if detail_json:
        try:
            loaded = json.loads(detail_json)
        except ValueError as exc:
            click.secho(f"--detail-json does not parse: {exc}", fg="red", err=True)
            sys.exit(EXIT_INVALID)
        body = json.dumps(loaded, ensure_ascii=False, indent=2)

    all_residents = validate_paths([residents]).residents
    with _open_store(db) as store:
        delegator = Delegator(
            residents=all_residents,
            store=store,
            emitter=ev.EventEmitter.from_env(),
            max_depth=max_depth(),
        )
        try:
            task = delegator.delegate(
                sender=sender,
                handoff=Handoff(
                    raw=f"steward delegate {resident_id} --to {recipient}",
                    to=recipient,
                    route=route,
                    title=title,
                    detail=body,
                ),
                parent_task_id=parent_task_id,
            )
        except DelegationError as exc:
            click.secho(f"refused ({exc.reason}): {exc}", fg="red", err=True)
            sys.exit(EXIT_INVALID)
    click.secho(f"{sender.id} → {task.assignee} via {task.route}: {task.title}", fg="green")
    click.echo(task.task_id)


@main.command("inbox")
@click.argument("resident_id")
@_RESIDENTS_OPTION
@_DB_OPTION
@click.option(
    "--status",
    default="open",
    show_default=True,
    help="Which items to show: open, claimed, done, failed, or all.",
)
@click.option("--format", "output_format", type=click.Choice(["text", "json"]), default="text")
def inbox_command(
    resident_id: str,
    residents: Path,
    db: Path | None,
    status: str,
    output_format: str,
) -> None:
    """Show the work other residents have handed to this one.

    ``open`` is the pending inbox: delivered and not yet picked up. Nothing here wakes a
    resident — items are worked on its own next wake-up, and this is only the reading.
    """
    resident = _resident_or_exit(residents, resident_id)
    with _open_store(db) as store:
        items = store.inbox(resident.id, None if status == "all" else status)
    if output_format == "json":
        click.echo(json.dumps([item.to_dict() for item in items], indent=2))
        return
    routes = ", ".join(resident.delegation_routes) or "none"
    click.secho(f"{resident.id}: routes accepting delegated work: {routes}", fg="bright_black")
    if not items:
        click.echo(f"nothing {status} in {resident.id}'s inbox")
        return
    for item in items:
        colour = {"open": "cyan", "claimed": "yellow", "done": "green"}.get(item.status, "red")
        click.secho(f"{item.status:<8} {item.task_id}  {item.title}", fg=colour)
        click.echo(
            f"         from {item.delegated_by} via {item.route} "
            f"(depth {item.depth}, origin {item.origin})"
        )


@main.group("task")
def task_group() -> None:
    """Follow one piece of work: who asked for it, and who it passed through."""


@task_group.command("lineage")
@click.argument("task_id")
@_DB_OPTION
@click.option("--format", "output_format", type=click.Choice(["text", "json"]), default="text")
def task_lineage(task_id: str, db: Path | None, output_format: str) -> None:
    """Print the whole chain a task belongs to, root first. The audit query.

    A task nobody delegated is a chain of one, which is a real answer and not an error.
    """
    with _open_store(db) as store:
        chain = store.lineage(task_id)
    if not chain:
        click.secho(f"no task {task_id!r}", fg="red", err=True)
        sys.exit(EXIT_INVALID)
    if output_format == "json":
        click.echo(json.dumps([item.to_dict() for item in chain], indent=2))
        return
    click.secho(f"origin {chain[-1].origin or 'unrecorded'}", fg="cyan", bold=True)
    for item in chain:
        _render_lineage_hop(item)


def _render_lineage_hop(item: JobRecord) -> None:
    """Print one hop of a lineage chain: who held it, and what became of it."""
    indent = "  " * item.depth
    who = (
        f"{item.delegated_by} → {item.assignee}"
        if item.delegated
        else f"posted by {item.posted_by}"
    )
    click.secho(f"{indent}{item.task_id}  {item.title}", bold=True)
    click.echo(f"{indent}  {who} — {item.status}{f' ({item.outcome})' if item.outcome else ''}")


# --------------------------------------------------------------------------------------
# approvals
# --------------------------------------------------------------------------------------


@main.group()
def approval() -> None:
    """Raise and inspect approval requests: the gated half of unattended work."""


@approval.command("raise")
@click.argument("resident_id")
@click.option("--action", required=True, help="Short slug naming the gated action.")
@click.option(
    "--detail-json",
    default=None,
    help="JSON object with everything a person needs to decide.",
)
@click.option("--note", default=None, help="One plain sentence, instead of --detail-json.")
@click.option("--expires-in", default=None, help="How long before it denies itself, e.g. 4h.")
@click.option("--options", default=None, help="Comma-separated: approve, deny, edit.")
@_RESIDENTS_OPTION
@_DB_OPTION
def approval_raise(  # noqa: PLR0913, PLR0917 — click passes one parameter per option
    resident_id: str,
    action: str,
    detail_json: str | None,
    note: str | None,
    expires_in: str | None,
    options: str | None,
    residents: Path,
    db: Path | None,
) -> None:
    """Raise an approval request for a resident, from inside its own session.

    Token-free and local on purpose: a headless session with shell access can call this
    directly rather than needing steward's API token, which is a credential no session
    should be holding. It writes to the same database the API answers from, so the
    request appears in `GET /approvals` and knocks in burrow exactly as an output-block
    request does. Prints the ``request_id``; the session then finishes its turn and
    stops. Nothing here waits for a decision.
    """
    resident = _resident_or_exit(residents, resident_id)
    if detail_json and note:
        click.secho("pass --detail-json or --note, not both", fg="red", err=True)
        sys.exit(EXIT_INVALID)

    try:
        request = _build_needs_human(action, detail_json, note, expires_in, options)
    except ApprovalError as exc:
        click.secho(str(exc), fg="red", err=True)
        sys.exit(EXIT_INVALID)

    with _open_store(db) as store:
        record = raise_request(
            store, ev.EventEmitter.from_env(), manifest=resident.manifest, request=request
        )
    click.secho(record.message, fg="yellow")
    click.echo(record.request_id)


def _build_needs_human(
    action: str,
    detail_json: str | None,
    note: str | None,
    expires_in: str | None,
    options: str | None,
) -> NeedsHuman:
    """Build a request from CLI flags, complaining loudly rather than guessing."""
    if not ACTION_PATTERN.match(action.strip()):
        raise ApprovalError(
            f"action {action!r} is not a slug; use lowercase letters, digits, '_' and '-'"
        )
    detail: dict[str, Any] = {}
    if detail_json:
        try:
            loaded = json.loads(detail_json)
        except ValueError as exc:
            raise ApprovalError(f"--detail-json does not parse: {exc}") from None
        if not isinstance(loaded, dict):
            raise ApprovalError("--detail-json must be a JSON object, so a panel can render it")
        detail = loaded
    elif note:
        detail = {"note": note}
    return NeedsHuman(
        raw=f"steward approval raise --action {action}",
        action=action.strip(),
        detail=detail,
        options=parse_options(options) if options else APPROVAL_DECISIONS,
        expires_in_s=parse_duration(expires_in) if expires_in else DEFAULT_EXPIRES_IN_S,
    )


@approval.command("show")
@click.argument("request_id")
@_DB_OPTION
@click.option("--format", "output_format", type=click.Choice(["text", "json"]), default="text")
def approval_show(request_id: str, db: Path | None, output_format: str) -> None:
    """Print one request with its decision, decider, and timestamps. The audit query."""
    with _open_store(db) as store:
        record = store.approval(request_id)
    if record is None:
        click.secho(f"no approval request {request_id!r}", fg="red", err=True)
        sys.exit(EXIT_INVALID)
    if output_format == "json":
        click.echo(json.dumps(record.to_dict(), indent=2))
        return
    click.secho(record.message, fg="cyan", bold=True)
    click.echo(f"request:   {record.request_id}")
    click.echo(f"resident:  {record.resident or record.agent_id}")
    click.echo(f"action:    {record.action}")
    click.echo(f"detail:    {json.dumps(dict(record.detail), ensure_ascii=False)}")
    click.echo(f"options:   {', '.join(record.options)}")
    click.echo(f"raised:    {record.created_at}")
    click.echo(f"expires:   {record.expires_at or 'never'}")
    if record.pending:
        click.secho("decision:  still waiting", fg="yellow")
        return
    click.secho(f"decision:  {record.decision} by {record.decided_by}", fg="green")
    click.echo(f"decided:   {record.decided_at}")
    if record.edit:
        click.echo(f"edited to: {json.dumps(dict(record.edit), ensure_ascii=False)}")
    click.echo(f"delivered: {record.delivered_at or 'not yet told to the resident'}")


# --------------------------------------------------------------------------------------
# budgets
# --------------------------------------------------------------------------------------


@main.group()
def budget() -> None:
    """See what residents have spent today, and lift a pause a budget caused."""


def _render_budget(status: BudgetStatus) -> None:
    """Print one resident's gauges, its window, and whether it is stopped."""
    colour = "red" if status.paused else "green"
    click.secho(f"{status.resident}: {status.summary()}", fg=colour, bold=True)
    click.secho(
        f"  window {status.window.day} ({status.window.tz}) — {status.spend.runs} run(s)",
        fg="bright_black",
    )
    for gauge in status.gauges:
        click.echo(f"  {gauge.describe()}")
    if status.max_run_seconds is not None:
        click.echo(f"  max_run_seconds: {status.max_run_seconds}")
    if status.spend.unreported:
        click.secho(
            f"  {status.spend.unreported} of today's runs did not report what they cost; "
            f"steward counts them as zero rather than guessing",
            fg="yellow",
        )
    if status.pause is not None:
        click.secho(f"  paused at {status.pause.paused_at}: {status.pause.reason}", fg="red")
        if status.pause.request_id:
            click.secho(f"  approve {status.pause.request_id} to resume", fg="yellow")


@budget.command("show")
@click.argument("resident_id", required=False)
@_RESIDENTS_OPTION
@_DB_OPTION
@click.option("--format", "output_format", type=click.Choice(["text", "json"]), default="text")
@click.option(
    "--by-origin",
    is_flag=True,
    help="Also roll today's spend up by the origin each chain descends from.",
)
def budget_show(
    resident_id: str | None,
    residents: Path,
    db: Path | None,
    output_format: str,
    by_origin: bool,  # noqa: FBT001 — click passes flags positionally
) -> None:
    """Show today's spend against each declared budget. All residents, or just one.

    "Today" is the resident's own primary time zone — the zone of the routine that closes
    its day, else the zone most of its routines run in — and the window is computed from
    the calendar right now, not from a counter some daemon has been holding since it
    started. A steward that restarted a minute ago prints the same numbers.

    ``--by-origin`` answers the other question delegation made askable: not "what did Hob
    spend" but "what did *this* question cost", summed across every resident the chain
    passed through. A rollup is fleet-wide by nature — that is the point of it — so it
    spans from the earliest window shown to the latest, and says which span it used
    rather than picking one resident's day and calling it everybody's.
    """
    result = validate_paths([residents])
    wanted = [r for r in result.residents if resident_id is None or r.id == resident_id]
    if resident_id is not None and not wanted:
        known = ", ".join(sorted(r.id for r in result.residents)) or "none"
        click.secho(f"no valid resident {resident_id!r} in {residents} (found: {known})", fg="red")
        sys.exit(EXIT_INVALID)

    with _open_store(db) as store:
        guard = BudgetGuard(store)
        statuses = [guard.status(resident.manifest) for resident in wanted]
        origins = _origin_rollup(store, statuses) if by_origin else []

    if output_format == "json":
        # The bare list is the shape this command has always printed, and something is
        # already parsing it. Asking for the rollup is what wraps it, so a caller that
        # never asks never has to learn a new shape.
        rendered = [status.to_dict() for status in statuses]
        payload: list[dict[str, Any]] | dict[str, Any] = (
            {"residents": rendered, "by_origin": [spend.to_dict() for spend in origins]}
            if by_origin
            else rendered
        )
        click.echo(json.dumps(payload, indent=2))
        return
    if not statuses:
        click.echo(f"no valid residents in {residents}")
        return
    for status in statuses:
        _render_budget(status)
    if by_origin:
        _render_origins(statuses, origins)


def _origin_rollup(store: Store, statuses: Sequence[BudgetStatus]) -> list[OriginSpend]:
    """Roll the ledger up by origin across the widest window any shown resident is in."""
    if not statuses:
        return []
    return store.spend_by_origin(
        since=min(status.window.start_iso for status in statuses),
        until=max(status.window.end_iso for status in statuses),
    )


def _render_origins(statuses: Sequence[BudgetStatus], origins: Sequence[OriginSpend]) -> None:
    """Print the origin rollup under the per-resident gauges, naming its span."""
    span = f"{min(s.window.day for s in statuses)}..{max(s.window.day for s in statuses)}"
    click.secho(f"\nby origin ({span}, all residents shown)", bold=True)
    if not origins:
        click.secho("  nothing on the ledger in this window", fg="bright_black")
        return
    for spend in origins:
        click.echo(
            f"  {spend.origin}: ${spend.cost_usd:.4f}, {spend.tokens} token(s), {spend.runs} run(s)"
        )


@budget.command("unpause")
@click.argument("resident_id")
@_DB_OPTION
def budget_unpause(resident_id: str, db: Path | None) -> None:
    """Lift a budget pause and let this resident run again.

    The same act as approving the ``needs_human`` the pause raised, from a terminal
    instead of a panel: the request is resolved as ``approve``, ``needs_human_resolved``
    is emitted, and the village sees one answer to one question either way.
    """
    with _open_store(db) as store:
        pause = BudgetGuard(store, ev.EventEmitter.from_env()).resume(resident_id, decided_by="cli")
    if pause is None:
        click.secho(f"{resident_id} is not paused by a budget", fg="yellow")
        return
    click.secho(f"{resident_id} resumed — it was paused by {pause.reason}", fg="green")
    click.secho(
        "the day's spend is still on the ledger; the next run adds to it", fg="bright_black"
    )


# --------------------------------------------------------------------------------------
# the watchdog
# --------------------------------------------------------------------------------------


@main.group()
def watchdog() -> None:
    """Keep unattended residents alive, and never let a stuck run look like work."""


def _report_interventions(report: WatchdogPass) -> None:
    """Print everything the pass actually changed, loudest first."""
    for health in report.gave_up:
        click.secho(f"gave up on {health.resident_id}: {health.detail}", fg="red", err=True)
    for resident_id in report.paused:
        click.secho(f"paused {resident_id}: budget exceeded", fg="red", err=True)
    for health in report.restarted:
        click.secho(f"restarted {health.resident_id}: {health.detail}", fg="yellow")
    for run in report.buried:
        click.secho(
            f"closed run {run.run_id} of {run.routine}: it never reported back", fg="yellow"
        )
    for job in report.reopened:
        click.secho(f"lease expired: {job.task_id} ({job.title}) is back on the board", fg="yellow")
    for record in report.expired_approvals:
        click.secho(f"approval expired: {record.action} denied by default", fg="yellow")


def _report_pass(report: WatchdogPass) -> None:
    """Print what one pass observed and did, interventions before observations."""
    _report_interventions(report)
    for health in report.health:
        if not health.known:
            click.secho(f"{health.resident_id}: unsupervised — {health.detail}", fg="bright_black")
        elif health.alive:
            click.secho(f"{health.resident_id}: ok", fg="green")
    if not report:
        click.echo("nothing to intervene in")


@watchdog.command("tick")
@_RESIDENTS_OPTION
@_DB_OPTION
@click.option("--format", "output_format", type=click.Choice(["text", "json"]), default="text")
def watchdog_tick(residents: Path, db: Path | None, output_format: str) -> None:
    """Make one pass: probe, sweep deadlines, bury stale runs, check budgets. Then exit."""
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    with _open_store(db) as store:
        report = Watchdog.from_path(residents, store).tick()
    if output_format == "json":
        click.echo(json.dumps(report.to_dict(), indent=2))
        return
    _report_pass(report)


@watchdog.command("run")
@_RESIDENTS_OPTION
@_DB_OPTION
@click.option(
    "--interval",
    type=float,
    default=DEFAULT_INTERVAL_S,
    show_default=True,
    help="Seconds between passes.",
)
@click.option("--max-passes", type=int, default=None, help="Stop after this many passes.")
def watchdog_run(residents: Path, db: Path | None, interval: float, max_passes: int | None) -> None:
    """Run the watchdog daemon: one pass, sleep, repeat.

    Sits alongside ``steward scheduler run`` rather than inside it, on purpose: the thing
    that notices a dead scheduler must not be a part of the scheduler.
    """
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    with _open_store(db) as store:
        dog = Watchdog.from_path(residents, store)
        try:
            passes = dog.run(interval_s=interval, max_passes=max_passes)
        except KeyboardInterrupt:  # pragma: no cover — a human stopping the daemon
            click.echo("stopped")
            return
    for report in passes:
        _report_pass(report)


# --------------------------------------------------------------------------------------
# the API
# --------------------------------------------------------------------------------------


@main.command()
@click.option("--host", default=DEFAULT_HOST, show_default=True, help="Interface to bind.")
@click.option("--port", default=DEFAULT_PORT, show_default=True, help="Port to bind.")
@click.option(
    "--residents",
    type=click.Path(path_type=Path),
    default=DEFAULT_RESIDENTS_DIR,
    show_default=True,
    help="Residents tree the API validates and creates into.",
)
@click.option(
    "--db",
    type=click.Path(path_type=Path),
    default=None,
    help="Jobs, approvals, and the request log. Defaults to steward.db beside $STEWARD_STATE.",
)
@click.option(
    "--allow-open",
    is_flag=True,
    help="Serve without a token. Local development only: every endpoint is a write path.",
)
def serve(
    host: str,
    port: int,
    residents: Path,
    db: Path | None,
    allow_open: bool,  # noqa: FBT001 — click passes flags positionally
) -> None:
    """Serve the token-gated HTTP API: the write path burrow's viewer calls.

    Tailnet only. The default bind is loopback, and steward must never be exposed to
    the public internet: one shared token is the whole of its auth.
    """
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    config = ApiConfig.from_env(residents_dir=residents, db_path=db, allow_open=allow_open)
    try:
        app = create_app(config)
    except ApiError as exc:
        click.secho(str(exc), fg="red", err=True)
        sys.exit(EXIT_INVALID)
    if allow_open and not (config.token or "").strip():
        click.secho("serving without a token — local development only", fg="yellow", err=True)
    click.echo(
        f"steward api on http://{host}:{port} (cors: {origins_summary(config.cors_origins)})"
    )
    if app.state.ui_dir is not None:
        click.echo(f"management console on http://{host}:{port}/ui/ (from {app.state.ui_dir})")
    run_server(app, host=host, port=port)


# --------------------------------------------------------------------------------------
# the nursery
# --------------------------------------------------------------------------------------

#: The charter file `--charter` reads: the same four fields the manifest declares, as
#: YAML. A charter is prose somebody thought about, and prose belongs in a file that can
#: be reviewed in a diff rather than in four shell arguments that cannot.
CHARTER_EXAMPLE = """mission: One paragraph of purpose.
duties:
  - Standing responsibilities, one per line.
rules:
  - Hard constraints, e.g. "Never send email without explicit approval."
escalation: When and how to raise needs_human instead of acting.
"""


def _read_charter(path: Path) -> dict[str, Any]:
    """Load a charter file, or exit naming what a valid one looks like."""
    try:
        loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        click.secho(f"cannot read the charter at {path}: {exc}", fg="red", err=True)
        sys.exit(EXIT_INVALID)
    if not isinstance(loaded, dict):
        click.secho(
            f"{path} is not a charter: it must be a YAML mapping with mission, duties, "
            f"rules and escalation\n\n{CHARTER_EXAMPLE}",
            fg="red",
            err=True,
        )
        sys.exit(EXIT_INVALID)
    return loaded


def _build_spec(  # noqa: PLR0913, PLR0917 — click passes one parameter per option
    resident_id: str,
    name: str,
    char: str,
    accent: str,
    role: str,
    charter: Path | None,
    skills: tuple[str, ...],
    runner: str,
    model: str | None,
    project: str | None,
    summary: str | None,
) -> NewResident:
    """Bind the command line into the same request body the API takes. Exits on refusal."""
    if charter is None:
        click.secho(
            "--charter is required: a resident without a charter is a resident whose "
            f"sessions have nothing to be told\n\n{CHARTER_EXAMPLE}",
            fg="red",
            err=True,
        )
        sys.exit(EXIT_INVALID)
    payload: dict[str, Any] = {
        "id": resident_id,
        "name": name,
        "char": char,
        "accent": accent,
        "role": role,
        "charter": _read_charter(charter),
        "skills": list(skills),
        "runner": {"kind": runner, "model": model},
    }
    if project:
        payload["project"] = project
    if summary:
        payload["summary"] = summary
    try:
        return NewResident.model_validate(payload)
    except ValidationError as exc:
        for error in exc.errors():
            where = ".".join(str(part) for part in error["loc"]) or "<root>"
            click.secho(f"{where}: {error['msg']}", fg="red", err=True)
        sys.exit(EXIT_INVALID)


def _report_nursery(report: NurseryReport) -> None:
    """Print the plan or the result, and colour the one line that matters most."""
    for line in report.render():
        if line.startswith("warning: "):
            click.secho(line, fg="yellow", err=True)
        elif line.startswith(("declare", "provision", "register", "plan for", "raised")):
            click.secho(line, fg="cyan", bold=True)
        else:
            click.echo(line)
    if report.register is not None and report.register.problems:
        click.secho(
            "the resident is declared and deployed, but the scheduler cannot run it yet",
            fg="yellow",
            err=True,
        )
    elif report.dry_run:
        click.secho("nothing was written, sent, or committed", fg="bright_black")
    elif not report.changed:
        click.secho("converged: nothing needed changing", fg="green")
    else:
        click.secho(f"{report.resident_id} is raised", fg="green")


@main.command("new-resident")
@click.option("--id", "resident_id", required=True, help="Slug; the directory under residents/.")
@click.option("--name", required=True, help="Display name, e.g. Quill.")
@click.option("--char", required=True, help="Burrow sprite key, e.g. Scribe.")
@click.option("--accent", required=True, help="Hex accent colour, e.g. '#4f7ea6'.")
@click.option("--role", required=True, help="One-line role, e.g. note bot.")
@click.option(
    "--charter",
    type=click.Path(path_type=Path, exists=True, dir_okay=False),
    default=None,
    help="YAML file with mission, duties, rules and escalation.",
)
@click.option("--skills", default="", help="Comma-separated skill names to grant.")
@click.option("--runner", default="claude", show_default=True, help="Which brain: claude, codex…")
@click.option("--model", default=None, help="Model for that runner, e.g. claude-opus-5.")
@click.option("--project", default=None, help="Project label, for a project-scoped soul.")
@click.option("--summary", default=None, help="One line burrow can display.")
@_RESIDENTS_OPTION
@click.option(
    "--repo",
    type=click.Path(path_type=Path),
    default=None,
    help="The checkout to commit into. Defaults to the parent of the residents tree.",
)
@click.option("--dry-run", is_flag=True, help="Print the whole plan and touch nothing.")
@click.option("--allow-dirty", is_flag=True, help="Commit even though the worktree is dirty.")
@click.option("--no-commit", is_flag=True, help="Write the declaration but do not commit it.")
@click.option("--no-deploy", is_flag=True, help="Declare and check only; build no container.")
@click.option("--format", "output_format", type=click.Choice(["text", "json"]), default="text")
def new_resident(  # noqa: PLR0913, PLR0917 — click passes one parameter per option
    resident_id: str,
    name: str,
    char: str,
    accent: str,
    role: str,
    charter: Path | None,
    skills: str,
    runner: str,
    model: str | None,
    project: str | None,
    summary: str | None,
    residents: Path,
    repo: Path | None,
    dry_run: bool,  # noqa: FBT001 — click passes flags positionally
    allow_dirty: bool,  # noqa: FBT001
    no_commit: bool,  # noqa: FBT001
    no_deploy: bool,  # noqa: FBT001
    output_format: str,
) -> None:
    """Raise a resident: declare it, commit it, build it, and check its schedule.

    The replacement for the ssh ritual — hand-written soul, hand-written compose service,
    tar to the NAS, wire the emitter by hand, restart, hope. One command, three stages,
    and every one of them idempotent: run it again after a failure and it picks up where
    it stopped rather than duplicating anything.

    `--dry-run` prints the files, the compose fragment, the exact ssh commands and the
    next fire of every routine, and provably touches nothing — no commit, no ssh, no
    scheduler state.
    """
    granted = tuple(part.strip() for part in skills.split(",") if part.strip())
    spec = _build_spec(
        resident_id, name, char, accent, role, charter, granted, runner, model, project, summary
    )
    try:
        report = raise_resident(
            spec,
            residents_dir=residents,
            repo=repo,
            provision=not no_deploy,
            commit=not no_commit,
            allow_dirty=allow_dirty,
            dry_run=dry_run,
        )
    except NurseryError as exc:
        click.secho(str(exc), fg="red", err=True)
        for diagnostic in exc.diagnostics:
            click.secho(diagnostic.render(), fg="red", err=True)
        sys.exit(EXIT_INVALID)
    if output_format == "json":
        click.echo(json.dumps(report.to_dict(), indent=2))
        return
    _report_nursery(report)


def _report_retire(report: RetireReport) -> None:
    """Print what retiring came to, and what it deliberately did not do."""
    for line in report.render():
        click.echo(line)
    if report.dry_run:
        click.secho("nothing was stopped, marked, or committed", fg="bright_black")
        return
    click.secho(
        "no event was emitted on its behalf: a retired resident leaves the village by "
        "going quiet, which is the only honest way to leave it",
        fg="bright_black",
    )
    click.secho(f"{report.resident_id} is retired", fg="green")


@main.command("retire")
@click.argument("resident_id")
@_RESIDENTS_OPTION
@click.option(
    "--repo",
    type=click.Path(path_type=Path),
    default=None,
    help="The checkout to commit into. Defaults to the parent of the residents tree.",
)
@click.option("--dry-run", is_flag=True, help="Print what would happen and touch nothing.")
@click.option("--allow-dirty", is_flag=True, help="Commit even though the worktree is dirty.")
@click.option("--no-commit", is_flag=True, help="Mark the manifest but do not commit it.")
@click.option("--format", "output_format", type=click.Choice(["text", "json"]), default="text")
def retire_command(  # noqa: PLR0913, PLR0917 — click passes one parameter per option
    resident_id: str,
    residents: Path,
    repo: Path | None,
    dry_run: bool,  # noqa: FBT001 — click passes flags positionally
    allow_dirty: bool,  # noqa: FBT001
    no_commit: bool,  # noqa: FBT001
    output_format: str,
) -> None:
    """Retire a resident: mark it retired in git, then stop and remove its container.

    Retirement is a lifecycle state, not a deletion. The manifest and the soul stay in
    this repo and in its history, `steward validate` still reads them, and the resident
    simply stops: no routines, no board claims, no letters, no run-now. It drops out of
    the village the honest way — it stops emitting — and steward forges nothing on its
    behalf on the way out.
    """
    try:
        report = retire_resident(
            resident_id,
            residents_dir=residents,
            repo=repo,
            commit=not no_commit,
            allow_dirty=allow_dirty,
            dry_run=dry_run,
        )
    except NurseryError as exc:
        click.secho(str(exc), fg="red", err=True)
        for diagnostic in exc.diagnostics:
            click.secho(diagnostic.render(), fg="red", err=True)
        sys.exit(EXIT_INVALID)
    if output_format == "json":
        click.echo(json.dumps(report.to_dict(), indent=2))
        return
    _report_retire(report)


if __name__ == "__main__":  # pragma: no cover
    main()
