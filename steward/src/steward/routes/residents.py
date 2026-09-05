"""Resident HTTP routes and their local request/view vocabulary."""

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Any

import yaml
from fastapi import APIRouter, Request
from pydantic import BeforeValidator, Field, model_validator

from steward import authoring as au
from steward import events as ev
from steward.approved_edits import UnapprovedEditError, approved_edit
from steward.budgets import BudgetStatus
from steward.chat import Transcript, chat_complaint, conversation_slug, conversation_summaries
from steward.deploy import TransportError
from steward.input_bounds import IDENTIFIER_MAX_CHARS
from steward.journal import journal_complaint, read_entries
from steward.manifest import (
    Resident,
    ResidentManifest,
    ValidationResult,
    redact_secrets,
    validate_path,
)
from steward.nursery import (
    CLAUDE_LOGIN_REMAINS,
    COMMIT_FAILED,
    WORKTREE_REFUSED,
    NewResident,
    NurseryError,
    NurseryReport,
    RetireReport,
)
from steward.prompt import MESSAGE_MAX_CHARS
from steward.rehearsal import Rehearsal, RehearsalError, rehearse
from steward.routes.auth import session_of
from steward.routes.deps import DOCUMENT_MAX_CHARS, Deps, _Body, _refuse
from steward.skills import SkillLibrary, effective_skills, library_for


class ResidentPost(NewResident):
    """A resident to declare, and whether to actually build it.

    Everything a :class:`~steward.nursery.NewResident` says, plus one flag. ``deploy``
    defaults to **false**, which keeps ``POST /residents`` exactly what it has always
    been: two files written for review, no container, no schedule, no event. Asking for
    ``deploy: true`` is asking steward to reach a machine over ssh and start something
    there, and that is not a thing a request should be able to do by leaving a field out.
    """

    deploy: bool = Field(
        default=False,
        description="Provision the container and check the schedule, not just declare.",
    )


class ProvisionPost(_Body):
    """Whether to build the declared resident, or only rehearse building it.

    There is nothing else to say: the manifest is the request. Everything ``new-resident``
    takes in flags this endpoint reads off ``residents/<id>/manifest.yaml``, which is the
    whole point of the door (warren#270).
    """

    dry_run: bool = Field(
        default=False,
        description="Print the plan and reach no host. Nothing is sent, run, or written.",
    )


class RehearsePost(_Body):
    """The one message a rehearsal answers.

    Nothing else, and deliberately no knobs: the declaration in the tree is the rest of
    the request, and a rehearsal that let the caller vary the charter, the skills or the
    tools would be rehearsing something other than what would be provisioned.
    """

    message: str = Field(
        min_length=1,
        max_length=MESSAGE_MAX_CHARS,
        description="What to say to the resident. Shape it like its first real message.",
    )


class RetirePost(_Body):
    """Whether to retire the declared resident, or only rehearse retiring it.

    The mirror of :class:`ProvisionPost`, and for the same reason there is nothing else on
    it: retirement takes an id and reads the declared manifest, so the request *is* the
    resident. The knobs ``steward retire`` offers beyond this one — ``--no-commit``,
    ``--no-deploy``, ``--allow-dirty`` — are break-glass for a host that is already gone or
    a checkout somebody is mid-way through, and each of them leaves the retirement half
    done in a way only the person at the terminal can see. A control panel gets the whole
    act or a refusal naming what stopped it.
    """

    dry_run: bool = Field(
        default=False,
        description=(
            "Report the plan and touch nothing: no mark, no commit, no host. `commands` is "
            "the exact argv a real run would issue."
        ),
    )
    revision: str | None = Field(
        default=None,
        description=(
            "The declaration revision returned by rehearsal. Required for execution so "
            "the confirmed plan cannot retire changed bytes."
        ),
    )


def _unchanged_mapping(value: object) -> object:
    """Publish the typed manifest input while preserving its raw mapping at runtime."""
    return value


ManifestMapping = Annotated[
    dict[str, Any],
    BeforeValidator(
        _unchanged_mapping,
        json_schema_input_type=ResidentManifest | dict[str, Any],
    ),
]


class DeclarationPut(_Body):
    """A resident's declaration, as a form edits it.

    ``manifest`` and ``text`` are two spellings of one thing and exactly one may be given.
    ``manifest`` is the mapping a form builds from its fields, which steward serialises —
    convenient, and it rewrites the file, so comments in it do not survive. ``text`` is the
    YAML itself, written byte for byte, which is how a caller keeps the comments a person
    wrote. Neither is more validated than the other.
    """

    manifest: ManifestMapping | None = Field(
        default=None, description="The manifest as data. Steward serialises it to YAML."
    )
    text: str | None = Field(
        default=None,
        max_length=DOCUMENT_MAX_CHARS,
        description="The manifest as YAML, written exactly as given. Preserves comments.",
    )
    soul: str | None = Field(
        default=None,
        max_length=DOCUMENT_MAX_CHARS,
        description="The soul document. Omit it to leave the soul untouched.",
    )
    revision: str | None = Field(
        default=None,
        max_length=IDENTIFIER_MAX_CHARS,
        description="The revision this edit was made against. Omit it to overwrite blindly.",
    )
    approval_request_id: str | None = Field(
        default=None,
        max_length=IDENTIFIER_MAX_CHARS,
        description=(
            "The approved request this edit is made against. Required of a session holding "
            "`residents.grant_skill`, and refused from a human caller, who needs none."
        ),
    )

    @model_validator(mode="after")
    def _one_spelling(self) -> DeclarationPut:
        """Insist on exactly one manifest spelling, so neither can silently win."""
        if (self.manifest is None) == (self.text is None):
            raise ValueError("give exactly one of `manifest` (a mapping) or `text` (YAML)")
        return self


def budget_summary(status: BudgetStatus) -> dict[str, Any]:
    """Return the small budget block the list view carries on every resident.

    Deliberately smaller than :meth:`BudgetStatus.to_dict` — a fleet list wants a fuel
    gauge and a stopped flag, not a full ledger window — but never *quieter*: a resident
    with no declared cap reports ``declared: false`` and a ``summary`` of ``no limit``,
    because a panel that simply omits the gauge would let unlimited read as unknown.
    """
    return {
        "declared": status.declared,
        "paused": status.paused,
        "summary": status.summary(),
        "spent_usd": round(status.spend.cost_usd, 6),
        "tokens": status.spend.tokens,
        "runs": status.spend.runs,
        "budgets": [gauge.to_dict() for gauge in status.gauges],
        "window": status.window.to_dict(),
    }


def resident_view(resident: Resident, library: SkillLibrary | None = None) -> dict[str, Any]:
    """Return the JSON view of one validated manifest.

    Safe to serve wholesale: a manifest that contained a credential-shaped key or an
    inline secret would have failed validation and never become a ``Resident`` at all,
    so there is nothing here to redact.

    ``effective_skills`` is what a session for this resident is actually given — the
    library's defaults plus this manifest's grants — so the panel can show the set
    without re-deriving it from two places.

    ``voice`` is the soul's own ``## Voice`` section, exactly the text
    :mod:`steward.prompt` injects. It is already parsed and in memory here, and a panel
    that showed a resident's charter but not the style it writes in would be showing half
    of who it is. ``None`` means the soul declares no voice, which is a real answer.
    """
    manifest = resident.manifest
    resolved = effective_skills(manifest, library) if library is not None else ()
    return {
        "id": manifest.id,
        "uid": str(manifest.uid),
        "home": manifest.home,
        "agent_id": manifest.agent_id,
        "project": manifest.project,
        "summary": manifest.summary,
        # Retirement is a lifecycle state, so a retired resident is *listed* rather than
        # hidden — a fleet view that quietly dropped it would be a fleet view that cannot
        # answer what used to run here.
        "retired": manifest.retired,
        "path": str(resident.path),
        "soul": manifest.soul.model_dump(mode="json"),
        "voice": resident.soul.voice,
        "charter": manifest.charter.model_dump(mode="json"),
        "skills": [skill.model_dump(mode="json") for skill in manifest.skills],
        # What the session is actually given: the library's defaults plus those grants.
        "effective_skills": [skill.name for skill in resolved],
        "memory": manifest.memory.model_dump(mode="json"),
        "routes": [route.model_dump(mode="json") for route in manifest.routes],
        "app_grants": [grant.model_dump(mode="json") for grant in manifest.app_grants],
        # Which tools a session may reach: the names, or the word "unrestricted". Here
        # rather than folded into "runner" because it is a capability dimension like the
        # four above it, and because "which residents are unbounded" should be one read.
        "tools": manifest.tools.model_dump(mode="json"),
        # And where those tools may act: the directories opened to a session beyond the
        # working directory it is confined to. Empty is the common, and the safe, answer.
        "workspace": list(manifest.workspace),
        # Which brain, answerable without opening a file.
        "runner": {"kind": manifest.runner.kind, "model": manifest.runner.model},
        # Whether this resident takes work off the board, and on what terms.
        "board": manifest.board.model_dump(mode="json"),
        # And whether it may hand work to anybody else, and to whom.
        "delegation": manifest.delegation.model_dump(mode="json"),
        # Whether steward taps a human about this resident, and about what (warren#114).
        # The *declaration* only: the derived ntfy topic is deliberately not here and not
        # anywhere else a browser can reach, because on ntfy the topic is the capability —
        # `steward notify list`, at a terminal, is the one place it is printed.
        "notifications": manifest.notifications.model_dump(mode="json"),
        "routines": [
            {
                "id": routine.id,
                "schedule": routine.schedule,
                "schedule_tz": routine.schedule_tz,
                "requires": list(routine.requires),
                "timeout_s": routine.timeout_s,
                "enabled": routine.enabled,
            }
            for routine in manifest.routines
        ],
    }


#: How a refused provision is answered. A reason the nursery named maps to the status that
#: reason means; anything it did not name is the host having answered and said no, which is
#: not something the caller can fix by sending different bytes — the same reasoning
#: :data:`WRITE_STATUS` applies to a tree with no git behind it.
PROVISION_STATUS: Mapping[str, int] = {
    "unknown_resident": 404,
    "resident_retired": 409,
    "declaration_invalid": 409,
}
PROVISION_FAILED = "provision_failed"
PROVISION_REFUSED = "provision_refused"

#: How a refused retirement is answered. ``unknown_resident``, ``resident_invalid`` and
#: ``resident_retired`` are settled by the route itself before the pipeline is called — see
#: :func:`_find_resident` and the retired check in the route — so what is left here is what
#: the pipeline can still name once it is running: a checkout it will not commit into, a
#: commit git refused, and the races the pre-checks cannot close.
RETIRE_STATUS: Mapping[str, int] = {
    "unknown_resident": 404,
    "resident_retired": 409,
    "declaration_invalid": 409,
    WORKTREE_REFUSED: 409,
    COMMIT_FAILED: 409,
    "retirement_rehearsal_required": 409,
    "stale_retirement_plan": 409,
}

#: What an unnamed retirement failure is: the host answered and said no, or stopped
#: answering part-way. Never borrowed for anything the caller could fix by sending
#: different bytes — there are no bytes here to send.
RETIRE_FAILED = "retire_failed"
RETIRE_REFUSED = "retire_refused"

#: The retirement refusals that changed nothing at all. Everything else stopped *part-way*
#: — a manifest marked and not committed, or marked and committed with the container still
#: up — and the request log has to be able to tell those apart. A row reading "refused" over
#: a request that left a commit in git is the one row an audit cannot recover from.
RETIRE_UNTOUCHED: frozenset[str] = frozenset(
    {
        "unknown_resident",
        "declaration_invalid",
        WORKTREE_REFUSED,
        "retirement_rehearsal_required",
        "stale_retirement_plan",
    }
)


def _payer_for(  # noqa: RET503 — the fallthrough raises through the refusal seam
    request: Request, result: ValidationResult, residents_dir: Path
) -> ResidentManifest | None:
    """Name the manifest whose budget line pays for a rehearsal, or ``None`` for a human.

    A session pays: the credential names a resident, and that resident's declaration is
    the budget the turn comes out of. A human — the master token, a named operator, or
    open mode — has no ledger line to charge, so nothing is recorded and no budget can
    refuse the turn. That asymmetry is the same one the run ledger already has: it is a
    record of what *residents* spent, and an operator asking a question is steward
    spending its own operator's money.
    """
    session = session_of(request)
    if session is None:
        return None
    for resident in result.residents:
        if resident.id == session.resident_id:
            return resident.manifest
    # The credential authenticated against a live run, so the resident existed when the
    # run opened; its declaration has since stopped validating or been removed. Refusing
    # is the honest answer — the alternative is a free rehearsal on a budget nobody can
    # find, which is exactly the thing this door is separate from `residents.dry_run` to
    # prevent.
    _refuse(
        409,
        "payer_not_declared",
        f"{session.resident_id} holds a live session credential, but its own declaration "
        f"is not in {residents_dir}; a rehearsal is charged to the caller's budget "
        f"line and steward will not run one it cannot bill",
    )


def _charge_note(rehearsal: Rehearsal) -> str:
    """Say whose budget the turn came out of, in the sentence that reports it."""
    if rehearsal.charged_to:
        return f"was charged to {rehearsal.charged_to}'s budget line"
    return "was charged to no resident's budget line, because a human asked for it"


#: What a declaration edit that no decision authorises is answered with. One error slug
#: for every way an approval can fail to cover a write — unknown, somebody else's, still
#: pending, denied, expired, spent, or simply about a different edit — because the caller's
#: next move is the same in all of them: knock again and wait for a human. The *message*
#: says which one it was, and one code keeps a client from branching on distinctions that
#: change nothing, or from using the door as an oracle for which request ids exist.
EDIT_NOT_APPROVED = "edit_not_approved"


@dataclass(frozen=True, slots=True)
class _GatedEdit:
    """The decision one session's declaration edit is being written against.

    Built only once the edit has matched, so its existence is the statement that this write
    is allowed. ``revision`` is the fingerprint of the declaration the match was made
    against, which is what stops the write landing on bytes nobody compared.
    """

    request_id: str
    act: str
    revision: str


def _manifest_data(text: str, which: str) -> dict[str, Any]:
    """Parse one manifest for comparison, or say which of the two would not parse."""
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise UnapprovedEditError(f"the {which} manifest does not parse as YAML: {exc}") from exc
    if not isinstance(data, Mapping):
        raise UnapprovedEditError(f"the {which} manifest is not a mapping")
    return dict(data)


def _deployed_message(report: NurseryReport) -> str:
    """Say what a finished provision came to — **both** halves of it.

    The container going up and the schedule check passing are two facts, and a report that
    said only the first would be a control panel's one unforgivable sin. Shared by both
    doors onto the nursery so they cannot come to describe the same outcome differently.
    """
    if report.register is not None and not report.register.ok:
        return (
            "the container is up, but the schedule check did not pass — see "
            "register.problems; nothing fires until those are fixed"
        )
    return (
        "the container is up and the schedule was checked; the resident appears in the "
        "village when it emits its own first event, and never before"
    )


def _provision_message(report: NurseryReport) -> str:
    """Say what ``POST /residents/{id}/provision`` came to, rehearsals included.

    Convergence is said as well as the outcome, never instead of it. A second run that sent
    nothing and *also* cannot schedule is two facts, and picking one of them to print would
    be the same half-truth :func:`_deployed_message` exists to prevent — so the converged
    sentence prefixes that one rather than replacing it.
    """
    if report.dry_run:
        return (
            "nothing was sent, run, or written: this is the plan, and `commands` is the "
            "exact argv a real run would issue"
        )
    if report.changed:
        return _deployed_message(report)
    return (
        f"converged: the host already had this bundle, so nothing was sent. "
        f"{_deployed_message(report)}"
    )


def _retire_message(report: RetireReport) -> str:
    """Say what ``POST /residents/{id}/retire`` came to — all three halves of it.

    A retirement is a decision, a container and a credential, and a message that named only
    the first would let a control panel report "retired" over a resident whose ``.env`` is
    still on the NAS holding a live village token. So the mark, the host, and the login
    steward deliberately did *not* remove are all said, in that order, every time.
    """
    if report.dry_run:
        return (
            "nothing was marked, committed, stopped, or removed: this is the plan, and "
            "`commands` is the exact argv a real run would issue"
        )
    if not report.marked:
        mark = "the manifest already said retired"
    elif report.commit:
        mark = "the manifest now says retired and that decision is committed"
    else:
        mark = (
            "the manifest now says retired, but nothing committed it — there is no history "
            "of this decision"
        )
    # ``note`` is steward's own sentence about the host, and it says something specific
    # exactly when there was nothing to stop — "nothing at ~/docker/… to stop", "deploy
    # skipped". When the stop succeeded it is the word "retired", which is the outcome
    # already said above, so that case is the one this spells out rather than repeats.
    if not report.stopped:
        host = report.note
    elif report.scrubbed:
        host = "the container is down and the .env holding CHRONICLE_TOKEN is gone"
    else:
        host = "the container is down; there was no .env here to remove"
    return f"{mark}; {host}. {CLAUDE_LOGIN_REMAINS}"


def router(deps: Deps) -> APIRouter:  # noqa: C901, PLR0915 — route factory is assembly
    """Build the resident routes around one application collaborator graph."""
    routes = APIRouter()
    settings = deps.settings
    db = deps.db
    residents_dir = deps.residents_dir
    nursery = deps.nursery
    provisioner = deps.provisioner
    retirer = deps.retirer
    transport = deps.transport
    guard = deps.guard

    @routes.get("/residents")
    def list_residents() -> dict[str, Any]:
        """List the validated residents, and name the manifests that did not validate."""
        result = validate_path(residents_dir, settings.skills_dir)
        current = library_for(residents_dir, settings.skills_dir)
        return {
            "residents": [
                {
                    **resident_view(resident, current),
                    # The fuel gauge burrow's fleet-ops view draws, on the one call that
                    # already lists everybody. A stopped resident should not need a
                    # second round trip to look stopped.
                    "budget": budget_summary(guard.status(resident.manifest)),
                }
                for resident in result.residents
            ],
            "errors": [diagnostic.render() for diagnostic in result.errors],
        }

    @routes.get("/residents/{resident_id}")
    def get_resident(resident_id: str) -> dict[str, Any]:
        """Return one validated manifest, runner included, so "which brain" is answerable."""
        result = validate_path(residents_dir, settings.skills_dir)
        current = library_for(residents_dir, settings.skills_dir)
        return resident_view(deps.find_resident(result, resident_id), current)

    @routes.get("/residents/{resident_id}/budget")
    def get_resident_budget(resident_id: str) -> dict[str, Any]:
        """Return spent-against-limit for each budget, the window, and the pause state.

        The read burrow's fleet-ops view (burrow #40) draws fuel gauges from. Everything
        in it is a sum over rows steward wrote when runs finished, inside a window
        computed from the calendar at the moment of this request — so a steward that
        restarted an hour ago answers exactly what one that has been up all day answers.
        """
        result = validate_path(residents_dir, settings.skills_dir)
        resident = deps.find_resident(result, resident_id)
        return guard.status(resident.manifest).to_dict()

    @routes.get("/residents/{resident_id}/journal")
    def get_resident_journal(resident_id: str, limit: int = 14) -> dict[str, Any]:
        """Return the resident's journal, newest first; an empty journal is an empty list."""
        result = validate_path(residents_dir, settings.skills_dir)
        resident = deps.find_resident(result, resident_id)
        complaint = journal_complaint(resident.manifest)
        if complaint is not None:
            _refuse(409, "journal_unreadable", complaint)
        entries = read_entries(resident.manifest, max(0, min(limit, 100)))
        return {"resident": resident.id, "entries": [entry.as_dict() for entry in entries]}

    @routes.get("/residents/{resident_id}/conversations")
    def list_conversations(resident_id: str) -> dict[str, Any]:
        """List the rolling conversation windows held in this resident's memory."""
        result = validate_path(residents_dir, settings.skills_dir)
        resident = deps.find_resident(result, resident_id)
        if not any(route.kind == "chat" for route in resident.manifest.routes):
            return {"resident": resident.id, "conversations": []}
        complaint = chat_complaint(resident.manifest)
        if complaint is not None:
            _refuse(409, "conversations_unreadable", complaint)
        return {
            "resident": resident.id,
            "conversations": [
                {
                    "id": summary.id,
                    "last_turn_at": summary.last_turn_at,
                    "turn_count": summary.turn_count,
                }
                for summary in conversation_summaries(resident.manifest)
            ],
        }

    @routes.get("/residents/{resident_id}/conversations/{conversation}")
    def get_conversation(resident_id: str, conversation: str) -> dict[str, Any]:
        """Return one rolling conversation window, oldest turn first."""
        result = validate_path(residents_dir, settings.skills_dir)
        resident = deps.find_resident(result, resident_id)
        if not any(route.kind == "chat" for route in resident.manifest.routes):
            _refuse(404, "conversation_not_found", f"resident {resident.id!r} has no chat route")
        complaint = chat_complaint(resident.manifest)
        if complaint is not None:
            _refuse(409, "conversations_unreadable", complaint)
        transcript = Transcript(resident.manifest, conversation)
        if not transcript.path.is_file():
            _refuse(404, "conversation_not_found", f"conversation {conversation!r} not found")
        turns = transcript.turns()
        return {
            "resident": resident.id,
            "conversation": conversation_slug(conversation),
            "turns": [
                {
                    "at": turn.at,
                    "speaker": turn.speaker,
                    "text": redact_secrets(turn.text),
                }
                for turn in turns
            ],
        }

    @routes.post("/residents", status_code=201)
    def create_resident(body: ResidentPost, request: Request) -> dict[str, Any]:
        """Declare a resident, and — only when asked — provision and check it.

        The same :func:`steward.nursery.raise_resident` pipeline ``steward new-resident``
        runs, with two settings the API always makes for itself:

        ``commit=False``
            The *nursery* does not commit here, because its commit is bound up with its own
            dirty-worktree refusal, which is right for a terminal and wrong for a server.
            The declaration is committed all the same — by :mod:`steward.authoring`, after
            the pipeline returns, staging only the two files that were written.

            This reverses the endpoint's original stance, deliberately (steward #214). It
            used to commit nothing on the grounds that the server may not own its checkout,
            which left the fleet's newest declarations as the only ones with no history and
            no author. The honest version of that worry is a *configured* one:
            ``STEWARD_ALLOW_UNCOMMITTED_WRITES`` accepts a tree with no git behind it, and
            a tree that has git gets the audit trail everything else gets.
        ``provision=body.deploy``
            Default false, so the endpoint's old behaviour is its default behaviour: files
            for review and nothing else.
        """
        if session_of(request) is not None and body.deploy:
            _refuse(
                403,
                "session_credential_forbidden",
                "deploying a resident reaches a host and starts a container; the "
                "residents.declare grant only opens declaration with deploy: false",
            )
        try:
            report = nursery(
                body,
                residents_dir=residents_dir,
                skills_dir=settings.skills_dir,
                transport=transport,
                provision=body.deploy,
                commit=False,
            )
        except NurseryError as exc:
            status = 409 if (residents_dir / body.id).exists() else 400
            _refuse(status, exc.reason or "resident_not_declared", str(exc))
        except TransportError as exc:
            # `deploy: true` and there was nobody to ask — in practice a steward whose own
            # environment has no `CHRONICLE_URL` to give the container, since `emitter_env`
            # refuses before a transport is reached and every later one is already wrapped
            # as a `NurseryError`. This was an unhandled 500: a control panel got a
            # traceback where it needed a sentence (warren#270).
            #
            # The declare stage has already written its two files by now and nothing has
            # committed them, so the refusal says what the next move is. That is a promise
            # the pipeline actually keeps — declaring is idempotent, so the same body
            # converges on the skeleton rather than colliding with it — and a test holds it
            # to that rather than taking the sentence's word for it.
            _refuse(
                409,
                PROVISION_REFUSED,
                f"{exc}; nothing was deployed and this request committed nothing — post "
                f"the same body again once that is fixed and it will pick up where it "
                f"stopped rather than collide",
            )
        request_id = deps.accept(
            request, "deployed" if body.deploy else "declared", {"resident": body.id}
        )
        written = [
            path
            for path in (report.declare.manifest_path, report.declare.soul_path)
            if path.is_file()
        ]
        knobs = deps.write_settings(request)
        push = knobs.pop("push")  # commit_write takes no push; it is recorded after
        try:
            commit = au.commit_write(
                residents_dir,
                written,
                au.DECLARE_SUBJECT.format(id=body.id),
                request_id=request_id,
                principal=deps.acting_principal(request),
                **knobs,
            )
        except au.AuthoringError as exc:
            db.set_request_outcome(request_id, f"refused: {exc.reason}")
            deps.refuse_write(exc)
        commit = au.record_push(commit, au.repo_toplevel(residents_dir), push)
        recorded = commit.note
        deployed = (
            _deployed_message(report)
            if body.deploy
            else "nothing is deployed and no routine is scheduled: this is a file for review"
        )
        return {
            "request_id": request_id,
            "status": "accepted",
            "message": f"{deployed}. {recorded}",
            # The four keys this endpoint has always returned, kept at the top level so
            # the deploy flag is additive for anything already reading the response.
            "id": body.id,
            "directory": str(report.declare.manifest_path.parent),
            "manifest_path": str(report.declare.manifest_path),
            "soul_path": str(report.declare.soul_path),
            **report.to_dict(),
            # Last, and deliberately after the report: `declare.commit` is the *nursery's*
            # commit, which is always null here because the API asks it not to commit.
            # This is the one that happened.
            "commit": commit.to_dict(),
        }

    @routes.post("/residents/{resident_id}/provision")
    def provision_declared_resident(
        resident_id: str, request: Request, body: ProvisionPost | None = None
    ) -> dict[str, Any]:
        """Build a resident from the manifest already in the tree, and check its schedule.

        The other door onto the nursery (warren#270). ``POST /residents`` assembles a
        declaration from a request body and refuses to converge it onto a manifest somebody
        has since edited — which left every resident carrying a route, an app grant or a
        ``runner.placement`` with no way onto the nursery path at all, because no body can
        express those fields. This one reads ``residents/<id>/manifest.yaml`` as the source
        of truth and runs provision and register against it.

        **200, not 202.** The container is up and the schedule has been checked by the time
        this answers — there is nothing left to acknowledge later, and saying `accepted`
        about work that already finished would be the one dishonesty the request log exists
        to prevent.

        Nothing is written into the checkout, so unlike every other write here there is no
        commit: the declaration being provisioned was committed by whoever wrote it, and a
        declaration whose bytes are in no commit comes back in ``warnings`` rather than as a
        refusal this endpoint has no way to resolve.
        """
        if session_of(request) is not None and (body is None or not body.dry_run):
            _refuse(
                403,
                "session_credential_forbidden",
                "provisioning reaches a host and starts a container; the residents.dry_run "
                "grant requires an explicit dry_run: true",
            )
        asked = body or ProvisionPost()
        try:
            report = provisioner(
                resident_id,
                residents_dir=residents_dir,
                skills_dir=settings.skills_dir,
                transport=transport,
                dry_run=asked.dry_run,
            )
        except NurseryError as exc:
            # Keyed on the nursery's own ``reason``, never on the prose or on a second look
            # at the filesystem: "there is no such resident" and "its declaration does not
            # validate" are different answers and only the pipeline that looked knows which.
            # An unnamed one is the host having answered and refused — a bundle that would
            # not land, a `docker compose up` that failed — and it says that rather than
            # borrowing a name for something it is not.
            reason = exc.reason or PROVISION_FAILED
            _refuse(PROVISION_STATUS.get(reason, 409), reason, str(exc))
        except TransportError as exc:
            # Both halves of "there was nobody to ask": a host that did not answer, and a
            # steward with no village address to give the container. One refusal, because
            # the exception's own message already says which, and a traceback would say
            # neither (steward #90).
            _refuse(409, PROVISION_REFUSED, str(exc))
        request_id = deps.accept(
            request,
            "rehearsed" if asked.dry_run else "provisioned",
            {"resident": report.resident_id},
        )
        return {
            "request_id": request_id,
            "message": _provision_message(report),
            **report.to_dict(),
        }

    @routes.post("/residents/{resident_id}/rehearse")
    def rehearse_declared_resident(
        resident_id: str, request: Request, body: RehearsePost
    ) -> dict[str, Any]:
        """Answer one message from a declaration, and keep nothing (warren#446).

        The behavioural half of the dry run. ``POST /residents/{id}/provision`` with
        ``dry_run: true`` proves the *plan* — the image, the host, the mounts, the next
        fires — and says nothing about whether the charter reads right. This runs one
        throwaway turn from the same declaration, with its charter, soul and skills
        assembled into the prompt exactly as a real launch would assemble them, and
        returns what the resident said.

        Nothing else happens. No container, no mounts, no memory directory, no session
        credential, no events, and no run row against the resident being rehearsed: it is
        exactly as unprovisioned afterwards as it was before. See
        :mod:`steward.rehearsal` for what each of those absences is protecting.

        **The one thing it costs is money**, which is why the door is its own grant. A
        session pays out of its *own* budget line rather than the declaration's — the
        resident being rehearsed does not exist yet, and the caller is who chose to spend
        a model turn — so a caller whose budget is spent or paused is refused here as it
        would be refused a session of its own. That check is advisory in exactly the way
        ``guard.allow`` is at every other route: it reads the ledger before the turn and
        writes to it after, so simultaneous requests can each see an unspent budget. What
        bounds a rehearsal absolutely is the per-turn timeout and the empty tool grant,
        not the daily cap.
        """
        result = validate_path(residents_dir, settings.skills_dir)
        resident = deps.find_resident(result, resident_id)
        payer = _payer_for(request, result, residents_dir)
        try:
            rehearsal = rehearse(
                resident,
                body.message,
                library=library_for(residents_dir, settings.skills_dir),
                runner_factory=deps.runner_factory,
                guard=guard,
                payer=payer,
                now=deps.now(),
            )
        except RehearsalError as exc:
            # Every one of them is 409: steward looked at the declaration, or at the
            # budget line that would pay, and will not run this. Nothing was spent.
            _refuse(409, exc.reason, str(exc))
        # Logged after the turn, never before: a rehearsal that was refused spent nothing
        # and belongs in the log as a refusal, which `_refuse` above has already made it.
        request_id = deps.accept(request, "answered", {"resident": resident.id})
        return {
            "request_id": request_id,
            "message": (
                "nothing was built, mounted, credentialed or recorded against this "
                "resident: this is one throwaway turn from its declaration, and it "
                f"{_charge_note(rehearsal)}"
            ),
            **rehearsal.to_dict(),
        }

    def record_refusal(request_id: str, reason: str) -> None:
        """Correct a retirement's logged outcome, saying how far it actually got.

        ``refused`` only where the refusal changed nothing. A retirement that stopped after
        the mark left the resident already out of the scheduler, the board and the watchdog,
        and one that stopped after the commit left that in git — logging either as "refused"
        would tell whoever reads the audit trail the opposite of what happened.
        """
        landed = reason not in RETIRE_UNTOUCHED
        db.set_request_outcome(
            request_id,
            f"{'stopped part-way' if landed else 'refused'}: {reason}",
            {"reason": reason, "changed_something": landed},
        )

    @routes.post("/residents/{resident_id}/retire")
    def retire_declared_resident(
        resident_id: str, request: Request, body: RetirePost | None = None
    ) -> dict[str, Any]:
        """End a resident: mark it retired in git, stop its container, remove its token.

        The counterpart of ``POST /residents/{id}/provision`` (warren#331), and the reason
        it had to exist: retirement is not a manifest edit. Writing ``retired: true``
        through ``PUT …/declaration`` marks the resident and leaves its container running
        with a live village token on the host — the half that matters most left undone —
        so a control panel that could only edit declarations could show a retired badge it
        had no way to make true.

        **The order is the safety argument, and it is the nursery's, not this route's.**
        ``retired: true`` is what takes the resident out of the scheduler, the board,
        delegation and the watchdog; stopping the container first would leave a window in
        which the watchdog notices it die and dutifully puts it back. So: mark, commit,
        ``docker compose down``, then remove the ``.env`` and the compose file — in that
        order, by one pipeline ``steward retire`` also calls.

        **This one commits through the nursery**, unlike ``POST /residents``, which asks the
        pipeline not to and commits afterwards through :mod:`steward.authoring`. The reason
        is the order above: retirement's commit belongs *between* the mark and the stop, and
        the only code inside that sequence is the pipeline. What comes with the nursery's
        commit is the nursery's dirty-worktree refusal, which is named rather than hidden —
        a server that committed a retirement into a checkout somebody was half-way through
        would be a server nobody can revert one decision in.

        **200, not 202**, for the reason provision answers 200: by the time this returns the
        container is down and the credential is gone. There is nothing left to acknowledge.
        """
        asked = body or RetirePost()
        # Resolved the way every other `/residents/{id}` route resolves — so a uid names a
        # resident here too, and an id that exists with a manifest that does not validate is
        # `resident_invalid` rather than a 404 sending somebody to look for a missing
        # directory. It is also where the retired check belongs: `retire_resident` itself
        # deliberately reconciles a half-finished retirement when you run it again, which is
        # break-glass at a terminal and not a button. A control panel offers Provision to a
        # retired resident, and this refusal is what says so to anything that does not.
        result = validate_path(residents_dir, settings.skills_dir)
        resident = deps.find_resident(result, resident_id)
        if resident.retired:
            _refuse(
                409,
                "resident_retired",
                f"resident {resident.id!r} is already retired, so there is nothing here to "
                f"end. The way back is the other direction: set retired: false in "
                f"{resident.path}, commit that decision, and POST "
                f"/residents/{resident.id}/provision to put its container up again. A "
                f"retirement left half done — marked, but the container still up — is "
                f"`steward retire {resident.id}` at a terminal.",
            )
        request_id = deps.accept(
            request,
            "rehearsed" if asked.dry_run else "retired",
            {"resident": resident.id},
        )
        if not asked.dry_run and asked.revision is None:
            reason = "retirement_rehearsal_required"
            record_refusal(request_id, reason)
            _refuse(409, reason, "rehearse retirement before confirming the current plan")
        try:
            common = {
                "residents_dir": residents_dir,
                "skills_dir": settings.skills_dir,
                "transport": transport,
                "identity": deps.write_settings(request)["identity"],
                "resident_dirty_only": True,
                "revision_of": au.revision_of,
                "emitter": deps.sink,
            }
            if asked.dry_run:
                # The revision and the plan describe the same locked bytes. Otherwise an
                # edit between planning and hashing could bind confirmation to a plan the
                # operator never saw.
                with au.authoring_lock(residents_dir):
                    report = retirer(resident.id, dry_run=True, **common)
                    revision = au.revision_of(resident.path)
            else:
                report = retirer(
                    resident.id,
                    dry_run=False,
                    expected_revision=asked.revision,
                    durable_guard=au.authoring_lock(residents_dir),
                    **common,
                )
                revision = asked.revision
        except NurseryError as exc:
            # Keyed on the nursery's own `reason` exactly as provision is. An unnamed one is
            # the host: a `docker compose down` that failed, a machine that stopped
            # answering between the stop and the removal. Those say so rather than borrowing
            # a name that would send an operator to look at the declaration.
            reason = exc.reason or RETIRE_FAILED
            record_refusal(request_id, reason)
            _refuse(RETIRE_STATUS.get(reason, 409), reason, str(exc))
        except TransportError as exc:
            record_refusal(request_id, RETIRE_REFUSED)
            _refuse(409, RETIRE_REFUSED, str(exc))
        # The nursery committed the mark; the API pushes it, as it pushes every commit it
        # makes (warren#351). After the host was reached, on purpose: the push is a record
        # of a retirement that has already happened, and a burrow that cannot reach GitHub
        # must still be able to stop a resident.
        push = None
        if report.commit is not None and deps.settings.push is not None:
            repo = au.repo_toplevel(residents_dir)
            push = au.push_commit(repo, deps.settings.push) if repo is not None else None
        message = _retire_message(report)
        return {
            "request_id": request_id,
            "message": message if push is None else f"{message.rstrip('.')}. {push.note}",
            **report.to_dict(),
            "revision": revision,
            "push": push.to_dict() if push is not None else None,
        }

    @routes.get("/residents/{resident_id}/declaration")
    def get_declaration(resident_id: str) -> dict[str, Any]:
        """Return the two files that declare this resident, as text and as data.

        The editable source, not the projection :func:`resident_view` serves. Both are
        useful and they are not the same thing: the view is assembled from a validated
        model and is what a fleet list draws, while this is what is actually in git —
        comments, field order, and all — which is the only thing you can sensibly write
        back. ``PUT`` takes exactly this shape.
        """
        result = validate_path(residents_dir, settings.skills_dir)
        resident = deps.find_resident(result, resident_id)
        soul_file = resident.manifest.soul.file
        declaration = au.read_declaration(residents_dir, resident.id, soul_file)
        return {
            "id": resident.id,
            "uid": str(resident.manifest.uid),
            "manifest": yaml.safe_load(declaration.manifest_text),
            "text": declaration.manifest_text,
            "soul": declaration.soul_text,
            "soul_file": soul_file,
            "revision": au.revision_of(
                *au.declaration_paths(residents_dir, resident.id, soul_file)
            ),
            "paths": [str(p) for p in au.declaration_paths(residents_dir, resident.id, soul_file)],
        }

    def gated_edit(
        resident_id: str, manifest_text: str, body: DeclarationPut, request: Request
    ) -> _GatedEdit | None:
        """Hold a session's declaration edit to the one decision that authorises it.

        Returns ``None`` for a human caller, who needs no decision and is refused for
        presenting one: consuming an approval on a person's behalf would put a spent mark
        against a decision they never used, and the ledger's whole value is that the mark
        means what it says.
        """
        session = session_of(request)
        if session is None:
            if body.approval_request_id is not None:
                _refuse(
                    422,
                    "approval_not_needed",
                    "`approval_request_id` is how a granted session proves a human approved "
                    "the edit it is making; a human caller is the approval and steward will "
                    "not spend one on their behalf",
                )
            return None
        # Past the gate, so the resident holds `residents.grant_skill`. That buys the
        # route, not the write.
        if body.approval_request_id is None:
            _refuse(
                403,
                "session_credential_forbidden",
                f"{session.resident_id} presented the credential for run {session.run_id} "
                f"and named no approved request; the residents.grant_skill grant opens this "
                f"door only against a human's yes. Nothing was recorded.",
            )
        if body.soul is not None:
            _refuse(
                403,
                EDIT_NOT_APPROVED,
                "this edit also carries a soul document; what a decision opens is the one "
                "manifest line it describes, so send `manifest` or `text` alone and leave "
                "the soul as it is",
            )
        resident = deps.find_resident(
            validate_path(residents_dir, settings.skills_dir), resident_id
        )
        # One read of each file, and the fingerprint is of *those* bytes. Reading the
        # declaration to compare it and then reading it again to fingerprint it would
        # leave a window for a write to land between the two, and the revision handed to
        # `write_declaration` would then describe bytes this gate never compared.
        manifest_path, soul_path = au.declaration_paths(
            residents_dir, resident.id, resident.manifest.soul.file
        )
        declared = manifest_path.read_bytes()
        soul = soul_path.read_bytes() if soul_path.is_file() else None
        try:
            edit = approved_edit(
                db.approval(body.approval_request_id),
                request_id=body.approval_request_id,
                presented_by=session.resident_id,
                writing_to=resident.id,
                now=ev.utc_now_iso(deps.now()),
            )
            edit.check(
                _manifest_data(declared.decode("utf-8"), "declared"),
                _manifest_data(manifest_text, "offered"),
            )
        except UnapprovedEditError as exc:
            _refuse(403, EDIT_NOT_APPROVED, f"{exc}. Nothing was written.")
        return _GatedEdit(
            request_id=body.approval_request_id,
            act=edit.act,
            revision=au.revision_of_bytes((manifest_path.name, declared), (soul_path.name, soul)),
        )

    @routes.put("/residents/{resident_id}/declaration")
    def put_declaration(resident_id: str, body: DeclarationPut, request: Request) -> dict[str, Any]:
        """Replace a resident's declaration, if it validates, and commit it.

        **A human act, and one narrow exception to that** (warren#437). A resident that
        could rewrite its own charter would be choosing the rules it is held to, so a
        session reaches this route only holding ``residents.grant_skill``, and gets through
        it only while presenting the id of an approval it raised, that a human answered
        ``approve``, that has not expired or been spent, and that describes exactly the edit
        being made. The decision is consumed by the write, so one yes is one edit.

        A full replacement rather than a patch. Merging a partial edit into a manifest means
        steward deciding what a missing key meant — cleared, or untouched? — and the
        declaration is the wrong file to be clever with. Read it, change it, write it back.
        The ``revision`` from the ``GET`` is how two editors find out about each other.

        Nothing is written unless the whole tree still validates with this change applied,
        including the checks that only exist across residents. A refusal has written
        nothing, committed nothing, and left the resident exactly as it was.
        """
        manifest_text = (
            body.text
            if body.text is not None
            else yaml.safe_dump(body.manifest, sort_keys=False, allow_unicode=True)
        )
        declaration = au.Declaration(manifest_text=manifest_text, soul_text=body.soul)
        gated = gated_edit(resident_id, manifest_text, body, request)
        request_id = deps.accept(request, "written", {"resident": resident_id})
        if gated is not None and not db.consume_approval(gated.request_id, by=request_id):
            # The match above read the decision as unspent; something else spent it in
            # between. The claim is the authority, not the read.
            db.set_request_outcome(request_id, f"refused: {EDIT_NOT_APPROVED}")
            _refuse(
                403,
                EDIT_NOT_APPROVED,
                f"approval {gated.request_id!r} was spent on another write while this one "
                f"was being checked; one approval is one edit. Nothing was written.",
            )

        def give_the_decision_back() -> None:
            """Un-spend the claim, because the write it was claimed for did not happen.

            Wired to every ``Exception`` and not only the named refusal, because that is
            exactly the class :func:`steward.authoring._authoring_files` rolls the tree
            back for: a git call that died or an ``OSError`` under the tree leaves the
            declaration as it was just as a validation refusal does, and a manifest typo
            must not cost a human's yes either way. What is left uncovered is the process
            dying outright, which fails closed: the decision reads as spent, nothing was
            written, and the honest next move is to ask again.
            """
            if gated is not None:
                db.release_approval(gated.request_id, by=request_id)

        try:
            written = au.write_declaration(
                residents_dir,
                resident_id,
                declaration,
                request_id=request_id,
                principal=deps.acting_principal(request),
                skills_dir=settings.skills_dir,
                expected_revision=body.revision or (gated.revision if gated else None),
                **deps.write_settings(request),
            )
        except au.AuthoringError as exc:
            db.set_request_outcome(request_id, f"refused: {exc.reason}")
            give_the_decision_back()
            deps.refuse_write(exc)
        except Exception:
            give_the_decision_back()
            raise
        return {
            "request_id": request_id,
            "status": "accepted",
            "id": written.paths[0].parent.name,
            "revision": written.revision,
            "paths": [str(p) for p in written.paths],
            "commit": written.commit.to_dict(),
            "approval": None
            if gated is None
            else {"request_id": gated.request_id, "act": gated.act},
            "warnings": [au.diagnostic_as_dict(d) for d in written.validation.warnings],
            "message": (
                f"written and validated; {written.commit.note}. The scheduler picks this up "
                f"on its next wake-up, or immediately via POST /reload"
            ),
        }

    return routes
