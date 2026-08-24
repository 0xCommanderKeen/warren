"""``steward`` command line: one load-and-validate path, shared with CI."""

import json
import logging
import sys
from collections.abc import Callable, Sequence
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path

import click

from steward.api import (
    DEFAULT_HOST,
    DEFAULT_PORT,
    ApiConfig,
    ApiError,
    create_app,
    origins_summary,
    run_server,
)
from steward.manifest import (
    Diagnostic,
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


def _load_or_exit(residents: Path) -> list[ScheduledRoutine]:
    try:
        return load_scheduled(residents)
    except SchedulerError as exc:
        click.secho(str(exc), fg="red", err=True)
        sys.exit(EXIT_INVALID)


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


def _build_scheduler(
    residents: Path,
    state: Path | None,
    workdir: Path | None,
    catchup_seconds: float,
    *,
    dry_run: bool,
) -> Scheduler:
    scheduled = _load_or_exit(residents)
    return Scheduler(
        scheduled,
        state=SchedulerState.load(state if state is not None else default_state_path()),
        workdir=workdir,
        catchup_s=catchup_seconds,
        dry_run=dry_run,
    )


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
def scheduler_tick(
    residents: Path,
    state: Path | None,
    workdir: Path | None,
    catchup_seconds: float,
    dry_run: bool,  # noqa: FBT001 — click passes flags positionally
) -> None:
    """Fire everything due right now, then exit. Good under an external cron."""
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    engine = _build_scheduler(residents, state, workdir, catchup_seconds, dry_run=dry_run)
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
    catchup_seconds: float,
    dry_run: bool,  # noqa: FBT001 — click passes flags positionally
    max_ticks: int | None,
) -> None:
    """Run the scheduler daemon: sleep to the next due routine, fire, repeat."""
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    engine = _build_scheduler(residents, state, workdir, catchup_seconds, dry_run=dry_run)
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
