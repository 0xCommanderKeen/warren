"""``steward`` command line: one load-and-validate path, shared with CI."""

import json
import sys
from collections.abc import Sequence
from dataclasses import asdict
from pathlib import Path

import click

from steward.manifest import (
    Diagnostic,
    Severity,
    ValidationResult,
    manifest_json_schema,
    validate_paths,
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


if __name__ == "__main__":  # pragma: no cover
    main()
