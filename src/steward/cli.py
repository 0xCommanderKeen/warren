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
from steward.board import BoardReport, Dispatcher, effective_skills
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
from steward.runners import check_runner
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
from steward.store import APPROVAL_DECISIONS, Store, default_db_path

DEFAULT_RESIDENTS_DIR = Path("residents")
EXIT_OK = 0
EXIT_INVALID = 1


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
def validate(paths: tuple[Path, ...], output_format: str) -> None:
    """Validate resident manifests. Defaults to the residents/ tree.

    Exits non-zero if any manifest fails, so CI can gate on it.
    """
    targets = list(paths) or [DEFAULT_RESIDENTS_DIR]
    result = validate_paths(targets)
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
# doctor
# --------------------------------------------------------------------------------------


@main.command()
@click.argument("residents", type=click.Path(path_type=Path), default=DEFAULT_RESIDENTS_DIR)
def doctor(residents: Path) -> None:
    """Check that what the manifests declare can actually run, here, now.

    Names the brain each resident runs on, whether its binary exists, and when each
    enabled routine fires next. A missing binary is an error at a reasonable hour
    rather than a routine that silently never happens at 7am.
    """
    result = validate_paths([residents])
    if not result.ok:
        _report_text(result, [residents])
        sys.exit(EXIT_INVALID)

    problems = 0
    for resident in result.residents:
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

    scheduled = _load_or_exit(residents)
    if scheduled:
        engine = Scheduler(scheduled, state=SchedulerState(path=default_state_path()))
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
    result = validate_paths([residents])
    resident = next((r for r in result.residents if r.id == resident_id), None)
    if resident is None:
        known = ", ".join(sorted(r.id for r in result.residents)) or "none"
        click.secho(f"no valid resident {resident_id!r} in {residents} (found: {known})", fg="red")
        sys.exit(EXIT_INVALID)

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
    # A rehearsal touches no database: it must not claim a task, deliver a decision, or
    # deny one by expiry. With no hooks the scheduler simply fires routines, as it did
    # before the board and approvals existed.
    hooks = None if dry_run else Dispatcher.from_path(residents, _open_store(db), workdir=workdir)
    return Scheduler(
        scheduled,
        state=SchedulerState.load(state if state is not None else default_state_path()),
        workdir=workdir,
        catchup_s=catchup_seconds,
        dry_run=dry_run,
        hooks=hooks,
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

_DB_OPTION = click.option(
    "--db",
    type=click.Path(path_type=Path),
    default=None,
    help="Jobs and approvals. Defaults to steward.db beside $STEWARD_STATE.",
)
_RESIDENTS_OPTION = click.option(
    "--residents",
    type=click.Path(path_type=Path),
    default=DEFAULT_RESIDENTS_DIR,
    show_default=True,
    help="Residents tree the manifests are read from.",
)


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
        dispatcher = Dispatcher.from_path(residents, store, workdir=workdir, sweep_only=sweep_only)
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
    label = f"{report.resident_id} → {report.task.task_id} ({report.task.title})"
    if report.done:
        click.secho(f"done {label}", fg="green")
    else:
        click.secho(f"failed {label}: {report.reason}", fg="red", err=True)
    for record in report.raised:
        click.secho(f"  needs human: {record.message} [{record.request_id}]", fg="yellow")


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
    holders = {
        resident.id: effective_skills(resident.manifest)
        for resident in validate_paths([residents]).residents
        if resident.manifest.board.claim
    }
    for job in jobs:
        colour = {"open": "cyan", "claimed": "yellow", "done": "green"}.get(job.status, "red")
        click.secho(f"{job.status:<8} {job.task_id}  {job.title}", fg=colour)
        if job.required_skills:
            click.echo(f"         skills: {', '.join(job.required_skills)}")
        if job.claimant:
            click.echo(f"         claimant: {job.claimant} (lease {job.lease_expires_at})")
        elif job.status == "open":
            eligible = [name for name, skills in holders.items() if job.claimable_by <= skills]
            click.echo(f"         claimable by: {', '.join(eligible) or 'nobody on this tree'}")


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
    result = validate_paths([residents])
    resident = next((r for r in result.residents if r.id == resident_id), None)
    if resident is None:
        known = ", ".join(sorted(r.id for r in result.residents)) or "none"
        click.secho(f"no valid resident {resident_id!r} in {residents} (found: {known})", fg="red")
        sys.exit(EXIT_INVALID)
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
    run_server(app, host=host, port=port)


if __name__ == "__main__":  # pragma: no cover
    main()
