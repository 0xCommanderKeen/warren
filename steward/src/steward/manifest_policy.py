"""Cross-field resident policies. The manifest loader owns their execution order."""

from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from steward.diagnostics import Diagnostic, Severity, closest_match
from steward.manifest_models import (
    BYPASS_PERMISSIONS,
    CHAT_ROUTE_KIND,
    JOB_BOARD_ROUTE_KIND,
    MCP_TOOL_PREFIX,
    NOTIFY_TASK_DONE,
    ROUTINE_DELIVER_CHAT,
    ResidentManifest,
    SoulDocument,
    chat_token_env_name,
)


def _check_duplicate_ids(manifest: ResidentManifest, source: Path) -> list[Diagnostic]:
    diagnostics: list[Diagnostic] = []
    groups: Mapping[str, Sequence[Any]] = {
        "skills": manifest.skills,
        "routes": manifest.routes,
        "app_grants": manifest.app_grants,
        "routines": manifest.routines,
    }
    for name, entries in groups.items():
        seen: set[str] = set()
        for index, entry in enumerate(entries):
            if entry.id in seen:
                diagnostics.append(
                    Diagnostic(
                        file=source,
                        field_path=f"{name}[{index}].id",
                        problem=f"duplicate id {entry.id!r} in {name}",
                        example=f"one entry per id in {name}",
                    )
                )
            seen.add(entry.id)
    seen_chat_addresses: set[str] = set()
    seen_token_slots: dict[str, str] = {}
    for index, route in enumerate(manifest.routes):
        if route.kind != CHAT_ROUTE_KIND:
            continue
        if route.address in seen_chat_addresses:
            diagnostics.append(
                Diagnostic(
                    file=source,
                    field_path=f"routes[{index}].address",
                    problem=f"duplicate chat address {route.address!r} in routes",
                    example="one chat route per <transport>:<reference> address",
                )
            )
        seen_chat_addresses.add(route.address)
        token_slot = chat_token_env_name(route.address)
        if token_slot is not None and token_slot in seen_token_slots:
            diagnostics.append(
                Diagnostic(
                    file=source,
                    field_path=f"routes[{index}].address",
                    problem=(
                        f"chat addresses {seen_token_slots[token_slot]!r} and "
                        f"{route.address!r} fold to the same token variable {token_slot}"
                    ),
                    example="choose references that remain distinct as environment names",
                )
            )
        elif token_slot is not None:
            seen_token_slots[token_slot] = route.address
    return diagnostics


def _check_routine_requirements(
    manifest: ResidentManifest, source: Path, effective: Sequence[str]
) -> list[Diagnostic]:
    """Check every ``requires`` against the resident's *effective* skills.

    Effective, not granted: the default skills every resident holds are part of the set
    a routine may require, so a manifest does not have to re-grant ``write-journal`` to
    be allowed to close its day with it.
    """
    available = set(effective)
    diagnostics: list[Diagnostic] = []
    for index, routine in enumerate(manifest.routines):
        for position, required in enumerate(routine.requires):
            if required in available:
                continue
            close = closest_match(required, available)
            hint = f"skills: [{close}]" if close else "grant it under skills: first"
            diagnostics.append(
                Diagnostic(
                    file=source,
                    field_path=f"routines[{index}].requires[{position}]",
                    problem=(
                        f"routine {routine.id!r} requires skill {required!r}, "
                        f"which this manifest does not grant"
                    ),
                    example=hint,
                )
            )
    return diagnostics


def _check_board_route(manifest: ResidentManifest, source: Path) -> list[Diagnostic]:
    """Require a declared job-board route from any resident that claims board work.

    ``routes`` is already this manifest's answer to "how does work reach this resident",
    and the board is a way work reaches it. Letting ``board.claim: true`` stand on its
    own would let a resident pull real work through a channel its own declaration never
    mentions — and burrow, which renders routes, would show a villager with no way in.
    The route must be ``active`` too: a channel somebody is still wiring up is not one
    tasks should be arriving through tonight.
    """
    if not manifest.board.claim:
        return []
    routes = [route for route in manifest.routes if route.kind == JOB_BOARD_ROUTE_KIND]
    example = (
        f"routes: [{{id: job-board, kind: {JOB_BOARD_ROUTE_KIND}, "
        f"address: steward:job-board, status: active}}]"
    )
    if not routes:
        return [
            Diagnostic(
                file=source,
                field_path="board.claim",
                problem=(
                    f"board.claim is true but no route of kind {JOB_BOARD_ROUTE_KIND!r} is "
                    f"declared; a resident cannot pull work through a channel its own "
                    f"manifest does not mention"
                ),
                example=example,
            )
        ]
    if any(route.status == "active" for route in routes):
        return []
    statuses = ", ".join(sorted({route.status for route in routes}))
    return [
        Diagnostic(
            file=source,
            field_path="board.claim",
            problem=(
                f"board.claim is true but every {JOB_BOARD_ROUTE_KIND!r} route is {statuses}; "
                f"claiming real work through a channel that is not open yet is a lie the "
                f"village would render"
            ),
            example=example,
        )
    ]


def _check_notifications_are_deliverable(
    manifest: ResidentManifest, source: Path
) -> list[Diagnostic]:
    """Warn about a declared tap for a fact this resident has no way to produce.

    :func:`_check_budget_is_enforceable`'s question, asked of the other capability that
    fires on its own: a manifest that declares one thing and can only do another is a
    document disagreeing with itself, and validation is the one moment somebody is reading
    both halves. Here it is ``on: [task_done]`` on a resident that neither claims from the
    board nor keeps an open ``delegation`` route — there is no path by which a task of its
    ever closes, so that tap can never fire.

    A **warning**, and the difference from the budget case is the whole argument. An
    unenforceable daily cap reads green while real money leaves, so it is refused. A tap
    that never fires spends nothing and loses nothing: the declaration is not wrong, only
    aspirational, and granting the resident ``board: {claim: true}`` tomorrow makes it true
    without touching this line. What it does risk is an operator reading the silence as a
    broken transport and going looking for a bug in ntfy — which is exactly the sentence a
    warning is for.
    """
    notifications = manifest.notifications
    if notifications.transport is None or NOTIFY_TASK_DONE not in notifications.on:
        return []
    if manifest.board.claim or any(route.accepts_delegation for route in manifest.routes):
        return []
    return [
        Diagnostic(
            file=source,
            field_path="notifications.on",
            problem=(
                "'task_done' is declared but this resident closes no tasks: board.claim is "
                "false and no active 'delegation' route is declared, so nothing will ever "
                "tap under this kind"
            ),
            example=(
                '"on": [needs_human]  (the knock this resident can actually raise), or '
                "board: {claim: true}  (give it work it can finish)"
            ),
            severity=Severity.WARNING,
        )
    ]


def _check_deliveries_have_a_door(manifest: ResidentManifest, source: Path) -> list[Diagnostic]:
    """Resolve each delivery declaration to exactly one active chat route.

    An **error**, where :func:`_check_notifications_are_deliverable` settles for a warning,
    because the two silences are not alike. A tap that never fires loses nothing. A digest
    that was asked for, written, paid for and then dropped on the floor every morning is
    work thrown away — and the manifest reads as if it arrives. ``active`` is the half that
    matters: a ``pending`` chat route is a bot nobody has put a token behind yet, so there
    is no conversation to send into.
    """
    # The same question :func:`steward.chat.chat_routes` answers at send time, asked of
    # the manifest alone: this module cannot import that one, and the address's parse is
    # the bridge's business rather than validation's (any non-empty address is legal).
    active = [route for route in manifest.routes if route.accepts_chat]
    diagnostics: list[Diagnostic] = []
    for index, routine in enumerate(manifest.routines):
        if routine.deliver is None:
            continue
        if routine.deliver == ROUTINE_DELIVER_CHAT:
            if not active:
                problem = (
                    f"routine {routine.id!r} delivers to chat but this resident has no "
                    "active chat route; there is no conversation to send its message into"
                )
                example = (
                    "routes: [{id: phone, kind: chat, address: telegram:<bot>, status: active}]"
                )
            elif len(active) > 1:
                problem = (
                    f"routine {routine.id!r} uses bare deliver: chat but this resident has "
                    "more than one active chat route; use deliver: <transport>:<reference>"
                )
                example = f"deliver: {active[0].address}"
            else:
                continue
        elif any(route.address == routine.deliver for route in active):
            continue
        else:
            problem = (
                f"routine {routine.id!r} delivers to {routine.deliver!r}, which does not "
                "name an active chat route on this resident"
            )
            example = f"deliver: {active[0].address}" if active else "deliver: chat"
        diagnostics.append(
            Diagnostic(
                file=source,
                field_path=f"routines[{index}].deliver",
                problem=problem,
                example=example,
            )
        )
    return diagnostics


def _check_budget_runtime(manifest: ResidentManifest, source: Path) -> list[Diagnostic]:
    """Warn when a declared timeout is longer than the budget that will cut it short.

    A warning rather than an error, because the manifest is not *wrong*: steward enforces
    ``min(timeout_s, max_run_seconds)`` and the run really does get killed at the budget.
    But a routine declaring a fifteen-minute timeout under a five-minute budget will never
    once get fifteen minutes, and reading the two numbers side by side is the only moment
    anybody is going to notice that. Silence here would make the manifest a document that
    disagrees with itself.
    """
    cap = manifest.budgets.max_run_seconds
    if cap is None:
        return []
    example = f"timeout_s: {cap}  (at most budgets.max_run_seconds)"
    diagnostics = [
        Diagnostic(
            file=source,
            field_path=f"routines[{index}].timeout_s",
            problem=(
                f"routine {routine.id!r} declares timeout_s {routine.timeout_s} but "
                f"budgets.max_run_seconds is {cap}; steward runs it for {cap}s and the "
                f"declared timeout is never reached"
            ),
            example=example,
            severity=Severity.WARNING,
        )
        for index, routine in enumerate(manifest.routines)
        if routine.timeout_s > cap
    ]
    if manifest.board.claim and manifest.board.timeout_s > cap:
        diagnostics.append(
            Diagnostic(
                file=source,
                field_path="board.timeout_s",
                problem=(
                    f"board sessions declare timeout_s {manifest.board.timeout_s} but "
                    f"budgets.max_run_seconds is {cap}; a claimed task is killed at {cap}s"
                ),
                example=example,
                severity=Severity.WARNING,
            )
        )
    return diagnostics


#: Runner kinds that spawn a real brain and cannot say what it cost. ``codex`` prints
#: plain text — :mod:`steward.runners` says so out loud — and a ``command`` is whatever
#: argv the manifest supplied. Only ``claude`` parses ``--output-format json`` for usage.
#:
#: ``mock`` is missing from this set deliberately: it reports no usage either, but it
#: spawns nothing and spends nothing, so a cap over it is inert without being untruthful.
#: This set is about caps that read green while real money goes out.
UNMETERED_RUNNER_KINDS = frozenset({"codex", "command"})

#: The budget fields computed from what a runner reported. ``max_run_seconds`` is not one
#: of them — steward times the run itself — so it stays legal under any runner.
METERED_BUDGET_FIELDS = ("daily_cost_usd", "daily_tokens")


def _check_budget_is_enforceable(manifest: ResidentManifest, source: Path) -> list[Diagnostic]:
    """Refuse a daily cap the declared runner can never report enough usage to trip.

    ``Gauge.exhausted`` is ``spent >= limit``, and ``spent`` is summed from ``run_ledger``
    rows that :meth:`steward.budgets.BudgetGuard.record` writes as **zeros** whenever the
    runner reported nothing. A resident on ``runner.kind: codex`` or ``command`` therefore
    accumulates ``cost_usd = 0.0`` for ever: the cap never trips, the pause machinery never
    fires, and ``GET /residents/{id}/budget`` reports a green gauge with real money being
    spent behind it (steward #125).

    An error rather than a warning, and at validation time rather than at run time, because
    the failure is silent by construction: nothing at run time is going to notice that a
    number stayed zero. The declared cap and the declared runner contradict each other, and
    this is the one moment somebody is reading both.

    ``max_run_seconds`` is untouched — steward measures a run's duration itself, so that
    cap is enforceable whatever the brain is.
    """
    if manifest.runner.kind not in UNMETERED_RUNNER_KINDS:
        return []
    return [
        Diagnostic(
            file=source,
            field_path=f"budgets.{name}",
            problem=(
                f"runner kind {manifest.runner.kind!r} does not report usage, so this cap "
                f"can never trip: every run is ledgered as costing zero and the budget "
                f"gauge reads green while the resident spends"
            ),
            example=(
                "runner: {kind: claude}  (the only kind that reports usage), "
                "or drop the cap and cap the session instead: "
                "budgets: {max_run_seconds: 900}"
            ),
        )
        for name in METERED_BUDGET_FIELDS
        if getattr(manifest.budgets, name) is not None
    ]


#: Runner kinds steward has no way to bound. ``codex exec`` takes no tool flag at all, and
#: a ``command`` is whatever argv the manifest supplied — so a list under either reads as a
#: boundary in the file and holds nothing at run time.
#:
#: ``mock`` is absent for the same reason it is absent from :data:`UNMETERED_RUNNER_KINDS`:
#: it bounds nothing either, but it spawns nothing, so a bound over it is inert without
#: being untruthful. This set is about boundaries that read green while a real session runs
#: past them.
UNBOUNDABLE_RUNNER_KINDS = frozenset({"codex", "command"})


def _check_tools_are_enforceable(manifest: ResidentManifest, source: Path) -> list[Diagnostic]:
    """Refuse a declared tool list steward would not actually be able to hold.

    Errors rather than warnings, and at validation time, because every one of these fails
    *silently*: the manifest reads as a bound, the session runs, and nothing at run time is
    going to notice that the bound was never applied. It is the same argument
    :func:`_check_budget_is_enforceable` makes about a daily cap under a runner that reports
    no usage — a boundary steward cannot hold is worse than none, because somebody read it.

    Three ways to write one:

    - **A list under a runner steward cannot bound.** ``codex`` and ``command`` take no tool
      flag; only ``claude`` compiles one (:meth:`steward.runners.ClaudeRunner.argv`).
    - **A list beside ``permission_mode: bypassPermissions``.** The list itself is *not*
      made inert by the bypass — measured against CLI 2.1.247, ``--tools`` removes a tool
      whatever the mode, so ``--tools Read --permission-mode acceptEdits`` still has no
      Bash. The contradiction is a different one, and it is real: this manifest went to the
      trouble of naming which tools may exist and then waived approval on every call to the
      ones that survive. One boundary drawn, the other dropped, in one file — and this is
      the one moment somebody is reading both.
    - **An ``mcp__…`` name inside a list.** Steward pairs a bound with
      ``--strict-mcp-config``, which loads no MCP servers at all, so the name resolves to a
      tool the session does not have. The CLI accepts the argument without complaint, which
      is exactly what makes it worth refusing here.
    """
    bound = manifest.tools.bound
    if bound is None:
        return []
    diagnostics: list[Diagnostic] = []
    if manifest.runner.kind in UNBOUNDABLE_RUNNER_KINDS:
        diagnostics.append(
            Diagnostic(
                file=source,
                field_path="tools",
                problem=(
                    f"runner kind {manifest.runner.kind!r} takes no tool flag, so this list "
                    f"bounds nothing: the session reaches every tool its brain has while the "
                    f"manifest reads as if it were held to {len(bound)}"
                ),
                example=(
                    "tools: unrestricted  (the truth, said out loud), or "
                    "runner: {kind: claude}  (the only kind steward can bound)"
                ),
            )
        )
    if manifest.runner.permission_mode == BYPASS_PERMISSIONS:
        diagnostics.append(
            Diagnostic(
                file=source,
                field_path="runner.permission_mode",
                problem=(
                    f"{BYPASS_PERMISSIONS!r} auto-approves every call to every tool that "
                    f"survives the list above, so this manifest names which tools may exist "
                    f"and then waives the approval on all of them"
                ),
                example=(
                    "permission_mode: acceptEdits  (approves the edits a bounded session "
                    "makes, and nothing else), or drop the list: tools: unrestricted"
                ),
            )
        )
    diagnostics.extend(
        Diagnostic(
            file=source,
            field_path=f"tools[{index}]",
            problem=(
                f"{name!r} is an MCP tool, and steward pairs a bounded list with "
                f"--strict-mcp-config, which loads no MCP servers at all; the session will "
                f"not have it"
            ),
            example=(
                "drop the mcp__ name, or tools: unrestricted if this resident really does "
                "need the MCP servers configured on the machine it runs on"
            ),
        )
        for index, name in enumerate(bound)
        if name.startswith(MCP_TOOL_PREFIX)
    )
    return diagnostics


def _check_delegation(manifest: ResidentManifest, source: Path) -> list[Diagnostic]:
    """Check that the delegation block says something, and does not say it about itself.

    Two ways to write a block that reads like a grant and is not one, both caught here
    rather than at the moment a session tries to hand work over and is refused:

    - **An allowlist with the switch off.** ``to:`` names recipients while ``send`` is
      false. Nothing is permitted, and a reader would swear otherwise.
    - **Naming yourself.** A resident cannot delegate to itself: that is not a handoff,
      it is the same session pretending to be two, and steward rejects it at enqueue. A
      manifest that declares it is declaring something that can never happen.
    """
    delegation = manifest.delegation
    diagnostics: list[Diagnostic] = []
    if delegation.to and not delegation.send:
        diagnostics.append(
            Diagnostic(
                file=source,
                field_path="delegation.send",
                problem=(
                    f"delegation.to names {sorted(delegation.to)} but delegation.send is "
                    f"false, so this resident may not delegate to anybody; an allowlist "
                    f"that grants nothing reads like a grant"
                ),
                example="delegation: {send: true, to: [hob]}",
            )
        )
    if manifest.id in delegation.to:
        diagnostics.append(
            Diagnostic(
                file=source,
                field_path="delegation.to",
                problem=(
                    f"{manifest.id!r} lists itself as a recipient; a resident handing work "
                    f"to itself is one session pretending to be two, and steward rejects it"
                ),
                example="to: [hob]  (somebody else)",
            )
        )
    return diagnostics


def _check_soul_agreement(
    manifest: ResidentManifest, soul: SoulDocument, source: Path
) -> list[Diagnostic]:
    """Check the soul frontmatter against the manifest, which is the source of truth."""
    diagnostics: list[Diagnostic] = []
    frontmatter = soul.frontmatter
    identity = {
        "uid": str(manifest.uid),
        "name": manifest.soul.name,
        "char": manifest.soul.char,
        "accent": manifest.soul.accent,
        "role": manifest.soul.role,
        "agent_id": manifest.agent_id,
        "project": manifest.project,
    }
    for key, expected in identity.items():
        if key not in frontmatter:
            continue
        actual = str(frontmatter[key]).strip()
        if expected is None or actual.lower() != str(expected).lower():
            diagnostics.append(
                Diagnostic(
                    file=soul.path,
                    field_path=f"frontmatter.{key}",
                    problem=(
                        f"soul frontmatter says {actual!r} but {source.name} says "
                        f"{expected!r}; the manifest is the source of truth"
                    ),
                    example=f"{key}: {expected}" if expected is not None else f"remove {key}",
                )
            )
    return diagnostics


def _check_directory_name(manifest: ResidentManifest, source: Path) -> list[Diagnostic]:
    directory = source.parent.name
    if directory and manifest.id != directory:
        return [
            Diagnostic(
                file=source,
                field_path="id",
                problem=f"id {manifest.id!r} does not match directory {directory!r}",
                example=f"id: {directory}",
            )
        ]
    return []
