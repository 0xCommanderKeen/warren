"""One throwaway turn from a declaration, before anything is provisioned (warren#446).

A dry run (``residents.dry_run``, warren#414) reads a *plan*: the compose fragment, the
argv, the next fires. It costs nothing and says nothing about whether the charter reads
right — whether the voice is the one the job needs, whether a rule lands, whether the
resident would knock where it should. A rehearsal answers that, and only that: the
declaration's charter, soul and skills are assembled into the prompt exactly as a real
chat wake-up would assemble them, one message is answered, the reply comes back, and
nothing survives the call.

What a rehearsal deliberately does **not** have, because none of it exists yet for a
resident that has only been declared:

``no container``
    The session is placed locally (:data:`steward.runners.LOCAL_PLACEMENT`) whatever the
    declaration says about ``runner.placement``. A container-placed declaration names a
    container nobody has built; a rehearsal that tried to ``docker exec`` into it would
    fail for a reason that has nothing to do with the charter it was asked about.
``no mounts, no memory directory``
    The turn happens in a temporary directory that is created for it and removed after.
    The declaration's ``workspace`` grants are not passed on: those name directories the
    provisioned resident may reach, and a resident that has not been provisioned has not
    been given them.
``no tools``
    The turn is bounded to ``tools: []`` — a session that can think and reply and touch
    nothing — whatever the declaration grants. ``tools`` is a *grant*, and the issue's
    "no grants" covers it: a real session's tool grant is confined by the container it
    was placed in, and this one is not placed in anything. A declaration asking for
    ``unrestricted`` would otherwise run a shell on the control plane's own host, under
    steward's account, at the request of whoever could write a declaration. A rehearsal
    asks how a resident *sounds*, and that question needs no tools to answer.
``no credential``
    The environment carries nothing. A rehearsal is not a run, there is no row in the
    registry to back a session credential, and a resident that has not been provisioned
    has no business holding one.
``no events, no run row, no journal``
    Steward emits nothing, writes nothing, and leaves the rehearsed resident's ledger
    untouched. It stays exactly as unprovisioned as it was. The one qualifier worth
    saying out loud: a *local* placement whose environment names
    ``STEWARD_SESSION_EMITTER`` gives every session steward's chronicle hooks
    (:func:`steward.runners.session_emitter`), and a rehearsal is a session, so the
    brain's own SessionStart/Stop hooks still reach the village on such a host. Steward
    records nothing about a rehearsal; the CLI it launched may still say it woke up.

The declaration does not have to be an unprovisioned one, and that is deliberate. The
motivating caller is ``raise-resident``, rehearsing a skeleton before it knocks — but
"read the charter back to me before I change it" is the same question about a resident
that has been running for months, and refusing it would only mean editing blind.

The one thing a rehearsal *does* spend is money, which is why it is a door of its own —
``residents.rehearse``, never implied by the free ``residents.dry_run``. The spend is
charged to the **caller's** budget line rather than to the declaration's: the resident
being rehearsed does not exist yet, and the caller is who chose to spend a model turn.
"""

import logging
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

from steward.manifest import Resident, ResidentManifest, ToolGrant, retired_complaint
from steward.prompt import assemble_chat_prompt
from steward.runners import LOCAL_PLACEMENT, RunRequest, RunResult, build_runner
from steward.runs import RUN_REHEARSAL
from steward.sessions import DEFAULT_CHAT_TIMEOUT_S, RunGuard, RunnerFactory
from steward.skills import SkillLibrary, describe_missing, effective_skills, missing_skills
from steward.store import new_id

__all__ = [
    "REHEARSAL_TIMEOUT_S",
    "Rehearsal",
    "RehearsalError",
    "rehearse",
]

log = logging.getLogger("steward.rehearsal")

#: How long a rehearsal gets before steward kills it, when no budget says otherwise.
#:
#: The chat timeout, because a rehearsal *is* one turn of chat: somebody asked a question
#: and is waiting for the answer. The payer's ``budgets.max_run_seconds`` still caps it
#: like any other session — that is the "same per-run cap as a routine" the issue asks
#: for, applied to the budget line the turn is actually charged against.
REHEARSAL_TIMEOUT_S = DEFAULT_CHAT_TIMEOUT_S

#: What a rehearsal may touch: nothing. See the module docstring's ``no tools``.
REHEARSAL_TOOLS = ToolGrant(root=())


def _origin(resident_id: str) -> str:
    """Name what this spend descends from: a rehearsal of one named declaration.

    Not ``resident:<payer>``: that origin means "this resident acting on its own
    initiative", and a rehearsal is money one resident spent looking at another's
    declaration. The spend rolls up under a word that says so.
    """
    return f"rehearsal:{resident_id}"


class RehearsalError(Exception):
    """A rehearsal that steward refused before any model turn was spent."""

    def __init__(self, message: str, *, reason: str) -> None:
        """Carry the stable refusal name beside the sentence it is served with."""
        super().__init__(message)
        self.reason = reason


@dataclass(frozen=True, slots=True)
class Rehearsal:
    """What one throwaway turn came to."""

    resident_id: str
    prompt: str
    reply: str
    outcome: str
    ok: bool
    duration_s: float
    timeout_s: int
    error: str = ""
    #: The resident whose budget line paid for the turn, or ``""`` when a human asked and
    #: there was no line to charge. Said rather than left to be inferred: "who paid" is
    #: the whole reason this door is separate from the free one.
    charged_to: str = ""
    cost_usd: float | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None

    def to_dict(self) -> dict[str, Any]:
        """Render the rehearsal as the API answers it."""
        return {
            "id": self.resident_id,
            "rehearsal": True,
            "reply": self.reply,
            "outcome": self.outcome,
            "ok": self.ok,
            "error": self.error,
            "duration_s": self.duration_s,
            "timeout_s": self.timeout_s,
            "prompt_chars": len(self.prompt),
            "charged_to": self.charged_to,
            "cost_usd": self.cost_usd,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
        }


def _reply_text(result: RunResult) -> str:
    """Turn what the session produced into the answer the caller reads.

    A failed session answers with steward's own summary rather than with the child's
    stdout, for :meth:`steward.chat.ChatBridge._answer_text`'s reason: a session that died
    while printing a key must not have that key forwarded as its "reply".
    """
    if not result.ok:
        return ""
    return (result.output or "").strip()


def rehearse(  # noqa: PLR0913 — one keyword per collaborator the turn needs
    resident: Resident,
    message: str,
    *,
    library: SkillLibrary,
    runner_factory: RunnerFactory = build_runner,
    guard: RunGuard | None = None,
    payer: ResidentManifest | None = None,
    timeout_s: int = REHEARSAL_TIMEOUT_S,
    now: datetime | None = None,
) -> Rehearsal:
    """Answer one message from ``resident``'s declaration, and keep nothing.

    ``payer`` is the resident whose budget line the turn is charged against — the caller,
    never ``resident`` itself. ``None`` means a human asked: humans have no ledger line,
    so nothing is recorded and no budget refuses the turn.
    """
    complaint = retired_complaint(resident)
    if complaint is not None:
        raise RehearsalError(complaint, reason="resident_retired")
    missing = missing_skills(resident.manifest, library)
    if missing:
        raise RehearsalError(
            describe_missing(resident.id, missing, library), reason="unknown_skill"
        )
    if guard is not None and payer is not None:
        refusal = _budget_refusal(guard, payer, now)
        if refusal is not None:
            raise RehearsalError(
                f"{payer.id} pays for this rehearsal, and {refusal}", reason="rehearsal_refused"
            )
        timeout_s = guard.timeout_for(payer, timeout_s)
    prompt = assemble_chat_prompt(
        resident.manifest,
        message,
        soul_text=resident.soul.body,
        # Everything a running resident accumulates is absent on purpose: an
        # unprovisioned declaration has no journal, no pending decisions, no answered
        # letters and no conversation so far. The charter, the soul and the skills are
        # what a rehearsal is about, and they are assembled here exactly as a real chat
        # wake-up assembles them.
        journal_entry=None,
        skills=effective_skills(resident.manifest, library),
        decisions=None,
        answered_letters=None,
    )
    runner = runner_factory(resident.manifest.runner, LOCAL_PLACEMENT)
    with TemporaryDirectory(prefix="steward-rehearsal-") as scratch:
        result = runner.run(
            RunRequest(
                prompt=prompt,
                workdir=Path(scratch),
                timeout_s=timeout_s,
                tools=REHEARSAL_TOOLS,
                workspace=(),
                model=resident.manifest.runner.model,
                env={},
            )
        )
    charged_to = _charge(guard, payer, resident, result, now)
    return Rehearsal(
        resident_id=resident.id,
        prompt=prompt,
        reply=_reply_text(result),
        outcome=str(result.outcome),
        ok=result.ok,
        duration_s=result.duration_s,
        timeout_s=timeout_s,
        error="" if result.ok else result.summary(),
        charged_to=charged_to,
        cost_usd=result.cost_usd,
        input_tokens=result.input_tokens,
        output_tokens=result.output_tokens,
    )


def _budget_refusal(guard: RunGuard, payer: ResidentManifest, now: datetime | None) -> str | None:
    """Return why the payer may not spend a turn right now, or ``None``."""
    try:
        return guard.allow(payer, now)
    except Exception as exc:  # noqa: BLE001 — an unreadable budget refuses safely
        log.warning("%s: could not read the budget: %s", payer.id, exc)
        return f"its budget is unreadable: {type(exc).__name__}: {exc}"


def _charge(
    guard: RunGuard | None,
    payer: ResidentManifest | None,
    resident: Resident,
    result: RunResult,
    now: datetime | None,
) -> str:
    """Append the turn to the payer's ledger, and say whose line took it.

    Best effort, like every other post-run accounting in steward: the turn has already
    been spent by the time this runs, and a ledger that cannot be written is a warning
    rather than a reason to pretend the rehearsal did not happen.
    """
    if guard is None or payer is None:
        return ""
    try:
        guard.record(
            payer,
            result=result,
            kind=RUN_REHEARSAL,
            run_id=new_id(),
            trigger="",
            ref=resident.id,
            origin=_origin(resident.id),
            now=now,
        )
    except Exception as exc:  # noqa: BLE001 — accounting cannot un-spend a finished turn
        log.warning("%s: could not record what this rehearsal cost: %s", payer.id, exc)
        return ""
    return payer.id
