"""The credential a session is given, and the identity it carries.

Steward's API token is a master key: one shared secret, one constant-time compare, no
principals (:func:`steward.api._authorized`). That is fine for a human operator and wrong
for a session — with it, the resident that *asks* for an approval can also *decide* it,
and a resident can sign a letter with any other resident's name. Both undo the guarantees
the escalation boundary and the two-manifest delegation rule are built on (steward #41).

So a session gets its own credential instead, and this module is the one place that says
what one is:

**Minted per run.** :func:`new_session_credential` is called where a run is opened — the
board's ``work`` and the scheduler's fire path, beside ``new_owner_token`` — and handed to
the child through ``RunRequest.env``, deliberately, rather than inherited from whatever
the control plane happens to be holding.

**Stored as a digest.** Only :func:`credential_digest` of it reaches the run registry, so
a copy of ``steward.db`` yields no live credentials. The plaintext exists in exactly two
places: the child's environment, and the ``Authorization`` header it presents.

**Expiring with the run.** There is no second clock. The digest is looked up against the
negation of the watchdog's burial condition — the run is open, no terminal fact has been
chosen for it, and its heartbeat is not stale — so a credential is accepted exactly while
nobody could yet call the run dead. A credential that leaked into a transcript is worthless
by the time anybody reads the transcript.

**Recognisable on sight.** :data:`SESSION_CREDENTIAL_PREFIX` is what makes the credential
redactable: ``redact_secrets`` matches its shape (:data:`SESSION_CREDENTIAL_PATTERN` is
folded into :data:`steward.manifest.SECRET_VALUE_PATTERNS`), so a session that prints its
own credential into its own stdout cannot have it survive into a burrow event or an
approval rendering. The run ledger needs no redaction and gets none: every column in
``run_ledger`` is a fact steward built — two ids, a kind, an outcome, four numbers — and
none of it is session-written text.

This module deliberately imports nothing from steward. Both ends of the credential's life
depend on it — the store that keeps the digest and the manifest redactor that scrubs the
plaintext — so it has to sit underneath both, like :mod:`steward.runs`.
"""

import hashlib
import re
import secrets
from dataclasses import dataclass

__all__ = [
    "SESSION_CREDENTIAL_BYTES",
    "SESSION_CREDENTIAL_PATTERN",
    "SESSION_CREDENTIAL_PREFIX",
    "SESSION_TOKEN_ENV",
    "SessionPrincipal",
    "credential_digest",
    "looks_like_session_credential",
    "new_session_credential",
]

#: The variable a session finds its own credential in. Named like steward's other
#: variables and containing the word ``TOKEN`` on purpose: the credential-assignment
#: matcher in :func:`steward.manifest.redact_secrets` already scrubs ``…TOKEN=…`` out of
#: anything a session writes, so the assignment form is covered by the general rule and
#: the bare value by :data:`SESSION_CREDENTIAL_PATTERN`.
SESSION_TOKEN_ENV = "STEWARD_SESSION_TOKEN"  # noqa: S105 — an env var name, not a credential

#: What every session credential starts with. A prefix rather than a bare random string,
#: because an unrecognisable secret cannot be redacted: ``owner_token`` is a plain UUID4
#: and there is no way to tell one out of context from any other UUID4 in a log.
SESSION_CREDENTIAL_PREFIX = "steward-session-"

#: How much randomness goes after the prefix, in bytes before URL-safe encoding.
SESSION_CREDENTIAL_BYTES = 32

#: The credential's shape, for the redactor. Anchored on the prefix and requiring a
#: plausible amount of randomness after it, so the constant alone — as it appears in this
#: file, in the docs, and in a diagnostic — is not mistaken for a live credential.
SESSION_CREDENTIAL_PATTERN = re.compile(
    rf"\b{re.escape(SESSION_CREDENTIAL_PREFIX)}[A-Za-z0-9_-]{{20,}}"
)


def new_session_credential() -> str:
    """Return one unguessable, prefixed credential for one run.

    ``secrets.token_urlsafe`` rather than a UUID4: this is a bearer secret presented to an
    HTTP endpoint, and 32 bytes from the system CSPRNG is the right amount of unguessable
    for that, where a UUID4's 122 bits are the right amount of unique for an identifier.
    """
    return f"{SESSION_CREDENTIAL_PREFIX}{secrets.token_urlsafe(SESSION_CREDENTIAL_BYTES)}"


def credential_digest(credential: str) -> str:
    """Return the digest the run registry stores, or ``''`` for no credential.

    One function for minting and for checking, so the two can never disagree about what
    is hashed. Empty in, empty out — a run opened without a credential stores the empty
    string, and the empty string must never match a presented credential.
    """
    if not credential:
        return ""
    return hashlib.sha256(credential.encode("utf-8")).hexdigest()


def looks_like_session_credential(value: str) -> bool:
    """Report whether a presented bearer value is shaped like a session credential.

    A cheap syntactic test, used to decide *which* check to run rather than to grant
    anything: the API tries the human token first, and only reaches for the run registry
    when what was presented could not be anything else.
    """
    return bool(SESSION_CREDENTIAL_PATTERN.fullmatch(value))


@dataclass(frozen=True, slots=True)
class SessionPrincipal:
    """Who a session credential says the caller is, resolved from its live run.

    Two fields, and no more than the two things anybody asks. ``resident_id`` is the
    identity — read from the run registry rather than from a request body, which is the
    whole point: a session cannot name a resident it is not. ``run_id`` is what a refusal
    says out loud, so an operator reading a 403 can find the exact session in the ledger.

    Deliberately not a copy of the whole run row. The rest of what a run knows — its
    ``agent_id``, its kind, the task it is working — is already answerable from the
    registry by ``run_id``, and a field nothing reads is a field that quietly goes wrong.
    """

    run_id: str
    resident_id: str
