"""Actionable declaration diagnostics shared by manifests and the skills library."""

import difflib
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from pydantic import ValidationError


class Severity(StrEnum):
    """How badly a diagnostic hurts. Errors fail validation; warnings do not."""

    ERROR = "error"
    WARNING = "warning"


@dataclass(frozen=True, slots=True)
class Diagnostic:
    """One actionable complaint about one field of one file.

    Every diagnostic names the file, the field path, what is wrong, and what a valid
    value looks like — a bad manifest must never silently imply access it does not have.
    """

    file: Path
    field_path: str
    problem: str
    example: str
    severity: Severity = Severity.ERROR

    def render(self) -> str:
        """Return the human-readable, terminal-friendly form of this diagnostic."""
        return (
            f"{self.file}: {self.severity.value}: {self.field_path}\n"
            f"    problem: {self.problem}\n"
            f"    example: {self.example}"
        )

    def __str__(self) -> str:
        """Render the diagnostic for humans."""
        return self.render()


def closest_match(value: str, candidates: Iterable[str]) -> str | None:
    """Return the candidate a misspelling most likely meant, or ``None``.

    One near-miss finder, shared by every "you named something that does not exist"
    diagnostic — a typo should be answered with the fix, not with a list to read.
    """
    matches = difflib.get_close_matches(value, sorted(candidates), n=1)
    return matches[0] if matches else None


class ManifestError(Exception):
    """Raised by :func:`load_manifest` when a manifest does not validate."""

    def __init__(self, diagnostics: Sequence[Diagnostic]) -> None:
        """Carry the diagnostics that explain the refusal."""
        self.diagnostics = tuple(diagnostics)
        summary = "; ".join(f"{d.field_path}: {d.problem}" for d in self.diagnostics[:3])
        super().__init__(summary or "manifest is invalid")


FIELD_EXAMPLES: Mapping[str, str] = {
    "version": "version: 0",
    "uid": "uid: 7e36d76a-1ad8-4d65-a619-8c6e7fb93ed9",
    "id": "id: hob  (lowercase, dashes)",
    "agent_id": "agent_id: claude-code:hob",
    "project": "project: burrow",
    "summary": "summary: Keeps the household running.",
    "soul": "soul: {name: Hob, char: Monk, accent: '#a68a4f', role: life bot}",
    "soul.name": "name: Hob",
    "soul.char": "char: Monk",
    "soul.accent": "accent: '#a68a4f'",
    "soul.role": "role: life bot",
    "soul.file": "file: soul.md",
    "charter": (
        "charter: {mission: …, duties: [...], rules: [...], escalation: raise needs_human}"
    ),
    "charter.mission": "mission: Keep the household running day to day.",
    "charter.duties": "duties: ['Post a daily summary each morning']",
    "charter.rules": "rules: ['Never send email without explicit approval']",
    "charter.escalation": (
        "escalation: Raise needs_human before any irreversible action.  "
        "(or: {when: [...], how: needs_human})"
    ),
    "charter.escalation.when": "when: ['A message needs a reply I was not told to send']",
    "charter.escalation.how": "how: needs_human",
    "skills": "skills: [read-inbox, read-calendar]",
    "skills.id": "id: read-inbox  (a name in skills/)",
    "skills.source": "source: library",
    "memory": ("memory: {kind: directory, path: /data/residents/hob/memory, journal: journal}"),
    "memory.kind": "kind: directory",
    "memory.path": "path: /data/residents/hob/memory",
    "memory.journal": "journal: journal  (a directory under path; one file per local day)",
    "memory.journal_keep": "journal_keep: 30  (entries kept, newest first)",
    "routes": "routes: [{id: cron, kind: cron, address: steward-scheduler, status: active}]",
    "routes.id": "id: inbox",
    "routes.kind": "kind: email  (delegation makes the route deliverable)",
    "routes.address": "address: mailbox:household  (a reference, not a credential)",
    "routes.status": "status: active",
    "routes.posts_to": "posts_to: [household]  (Discord channel names this resident may post to)",
    "routes.listens_in": (
        "listens_in: [household]  (Discord channels where @mentions may start a session)"
    ),
    "notifications": 'notifications: {transport: ntfy, "on": [needs_human]}',
    "notifications.transport": "transport: ntfy  (omit the block entirely to tap nobody)",
    "notifications.on": '"on": [needs_human, task_done]',
    "notifications.status": "status: active  (active | pending | disabled)",
    "notifications.note": "note: Miha's phone  (a label, never an address)",
    "app_grants": "app_grants: [{id: gmail, name: Gmail, status: granted}]",
    "app_grants.id": "id: gmail",
    "app_grants.name": "name: Gmail",
    "app_grants.status": "status: granted  (granted | pending | revoked)",
    "app_grants.scopes": "scopes: [channels.manage, members.read]  (only for id: discord)",
    "app_grants.status_ref": "status_ref: https://myaccount.google.com/permissions",
    "tools": "tools: [Read, Glob, Grep]  (or: tools: unrestricted)",
    "workspace": "workspace: [/data/library/books]  (absolute paths)",
    "runner": "runner: {kind: claude, model: claude-opus-5}",
    "runner.kind": "kind: claude  (claude | codex | command | mock)",
    "runner.placement": "placement: local  (local | container)",
    "runner.model": "model: claude-opus-5",
    "runner.permission_mode": "permission_mode: acceptEdits  (a mode the CLI accepts)",
    "runner.command": "command: ['my-agent', '--prompt', '{prompt}', '--cwd', '{workdir}']",
    "routines": (
        "routines: [{id: daily-summary, schedule: '0 7 * * *', prompt: …, timeout_s: 900}]"
    ),
    "routines.id": "id: daily-summary",
    "routines.schedule": "schedule: '0 7 * * *'",
    "routines.schedule_tz": "schedule_tz: Europe/Ljubljana  (IANA name; defaults to UTC)",
    "deploy.tz": "tz: Europe/Ljubljana  (IANA name; defaults to the routines' schedule_tz)",
    "routines.prompt": "prompt: Write today's household summary.",
    "routines.requires": "requires: [read-inbox]  (a default skill, or one granted above)",
    "routines.timeout_s": "timeout_s: 900",
    "routines.enabled": "enabled: true",
    "routines.journal": "journal: close_of_day  (on exactly one routine, or omit it)",
    "routines.deliver": (
        "deliver: chat  (one active route), or deliver: discord:hob  (an exact route)"
    ),
    "routines.quiet_word": "quiet_word: NOTHING  (one short token; only with deliver)",
    "delegation": "delegation: {send: true, to: [receiver-resident]}",
    "delegation.send": "send: true  (omit the block entirely to never delegate)",
    "delegation.to": "to: [receiver-resident]  (resident ids; omit it to allow any receiver)",
    "delegation.note": "note: Project work may be handed to a household agent.",
    "board": "board: {claim: true, max_claims_per_wake: 1, lease_s: 1800, timeout_s: 900}",
    "board.claim": "claim: true  (omit the block entirely to never claim)",
    "board.max_claims_per_wake": "max_claims_per_wake: 1",
    "board.lease_s": "lease_s: 1800  (must outlive timeout_s)",
    "board.timeout_s": "timeout_s: 900",
    "budgets": "budgets: {daily_cost_usd: 5.0, daily_tokens: 2000000, max_run_seconds: 900}",
    "budgets.daily_cost_usd": "daily_cost_usd: 5.0  (omit the field for no cap)",
    "budgets.daily_tokens": "daily_tokens: 2000000  (input + output, per local day)",
    "budgets.max_run_seconds": "max_run_seconds: 900  (one run, not a day)",
    "deploy": "deploy: {host: dxp2800, user: Miha, container: steward-hob}",
    "deploy.container": "container: steward-hob  (the docker name, or omit the block)",
    "deploy.host": "host: dxp2800  (a hostname, no spaces: it reaches a remote shell)",
    "deploy.user": "user: Miha  (the ssh user on that host)",
    "deploy.path": "path: ~/docker/warren/residents/hob  (compose directory on the host)",
    "deploy.image": "image: steward-resident:latest",
    "deploy.command": "command: [sleep, infinity]",
    "retired": "retired: false  (true retires the resident; the files stay in git)",
}

_UNION_TAGS = frozenset({"str", "int", "bool", "list[str]", "Escalation", "function-after[_"})


def _normalize_loc(loc: Sequence[object]) -> str:
    """Turn a pydantic error location into a dotted, index-free field path."""
    parts = [
        str(part)
        for part in loc
        if isinstance(part, str)
        and part not in _UNION_TAGS
        and not part.startswith(("function-", "constrained-"))
    ]
    return ".".join(parts)


def _render_loc(loc: Sequence[object]) -> str:
    """Turn a pydantic error location into a dotted path that keeps list indices."""
    rendered = ""
    for part in loc:
        if isinstance(part, int):
            rendered += f"[{part}]"
        elif part in _UNION_TAGS or str(part).startswith(("function-", "constrained-")):
            continue
        else:
            rendered += f".{part}" if rendered else str(part)
    return rendered or "<root>"


def _example_for(loc: Sequence[object]) -> str:
    normalized = _normalize_loc(loc)
    while normalized:
        if normalized in FIELD_EXAMPLES:
            return FIELD_EXAMPLES[normalized]
        normalized = normalized.rsplit(".", 1)[0] if "." in normalized else ""
    return "see docs/manifest.md for the field reference"


def _diagnostics_from_validation_error(error: ValidationError, source: Path) -> list[Diagnostic]:
    seen: set[tuple[str, str]] = set()
    diagnostics: list[Diagnostic] = []
    for raw in error.errors():
        loc = raw["loc"]
        path = _render_loc(loc)
        problem = raw["msg"]
        if raw["type"] == "missing":
            problem = "required field is missing"
        key = (path, problem)
        if key in seen:
            continue
        seen.add(key)
        diagnostics.append(
            Diagnostic(file=source, field_path=path, problem=problem, example=_example_for(loc))
        )
    return diagnostics
