"""The credential a named human operator presents, and the identity it carries.

Steward has had exactly two kinds of caller (steward #41). A **session** presents the
credential minted for its own run — a real principal, resolved from the run registry,
allowed reads and ``POST /delegate`` and nothing else. A **human** presents
``STEWARD_TOKEN``, which is a master key with no principal behind it: one shared secret,
one constant-time compare, and the honest most steward can say about a change made with
it is *which door it came through* (:data:`steward.api.API_PRINCIPAL`).

That was tolerable while the only humans holding it were the CLI and the environment. It
stopped being tolerable when a browser wanted it: townhall is a write surface on the
shared origin, and pasting the master token into a tab hands every operator the key that
also boots the server, that no one can revoke without a redeploy, and that names nobody in
the audit trail it leaves. warren#225's criterion is that the master token never lands in
a browser again.

So a third kind of caller, and this module is the one place that says what one is:

**Minted by name, on a terminal.** ``steward operator mint <name>`` is the only way one
comes into existence, and it prints the plaintext exactly once. There is no HTTP path that
mints, revokes, or lists one: a credential that could mint its successor would make
revocation a suggestion.

**Stored as a digest.** Only :func:`credential_digest` of it reaches ``steward.db``, the
same one-way function the run registry uses, so a copy of the database yields no live
credentials. The plaintext exists in the operator's password manager and their browser
tab's ``sessionStorage``, and nowhere steward controls.

**Revocable, and revoked forever.** ``steward operator revoke <name>`` stamps the row
rather than deleting it — who held a credential and when it stopped working is exactly the
question an audit asks, and a deleted row cannot answer it. The master token is not
revocable at all short of restarting the server with a different one, which is the
difference this module exists to make.

**A principal, unlike the master token.** An operator has a name and an email, so a write
made with one is committed *by them* — :meth:`OperatorPrincipal.identity` is handed
straight to :mod:`steward.authoring` as the git author, and an approval decided with one
records their name rather than ``api``. This is the payoff: the audit trail stops saying
"somebody with the token" and starts saying who.

**Recognisable on sight.** :data:`OPERATOR_CREDENTIAL_PREFIX` is what makes it redactable,
exactly as the session prefix does: :data:`OPERATOR_CREDENTIAL_PATTERN` is folded into
:data:`steward.manifest.SECRET_VALUE_PATTERNS`, so an operator credential pasted into a
manifest fails validation and one echoed into a session's stdout never survives into an
event.

**What it is not.** It is not a second authorisation tier. An operator reaches exactly what
the master token reaches — the session credential's 403 allowlist is untouched, because an
operator is a *human* principal and the allowlist exists to keep sessions out of human
acts. Distinguishing what an operator may do from what the token may do would be a
different issue, and pretending to do it here with one shared code path would be worse than
not doing it.

This module sits directly on :mod:`steward.session_auth` and on nothing else in steward.
It borrows that module's digest so the two credential kinds can never disagree about what
"stored as a digest" means, and inherits its position underneath the store and the
redactor for the same reason.
"""

import re
import secrets
from dataclasses import dataclass

from steward.session_auth import credential_digest

__all__ = [
    "OPERATOR_CREDENTIAL_BYTES",
    "OPERATOR_CREDENTIAL_PATTERN",
    "OPERATOR_CREDENTIAL_PREFIX",
    "OperatorPrincipal",
    "credential_digest",
    "looks_like_operator_credential",
    "new_operator_credential",
    "operator_email",
]

#: What every operator credential starts with. A different prefix from the session one on
#: purpose: the two are checked against different tables and mean different things, and a
#: refusal that could not say which kind was presented would be a refusal nobody can act on.
OPERATOR_CREDENTIAL_PREFIX = "steward-operator-"

#: How much randomness goes after the prefix, in bytes before URL-safe encoding. The same
#: 32 as a session credential: this one lives longer, so it may not be weaker.
OPERATOR_CREDENTIAL_BYTES = 32

#: The credential's shape, for the redactor. Anchored on the prefix and requiring a
#: plausible amount of randomness after it, so the constant alone — in this file, in the
#: docs, in a diagnostic — is not mistaken for a live credential.
OPERATOR_CREDENTIAL_PATTERN = re.compile(
    rf"\b{re.escape(OPERATOR_CREDENTIAL_PREFIX)}[A-Za-z0-9_-]{{20,}}"
)

#: The domain a minted operator's git author address defaults to. Local rather than a real
#: one: steward has no way to verify an address, and inventing a plausible external one
#: would put a name on a commit that nobody can be reached at.
OPERATOR_EMAIL_DOMAIN = "steward-operator.localhost"


def new_operator_credential() -> str:
    """Return one unguessable, prefixed credential for one named operator.

    ``secrets.token_urlsafe`` rather than a UUID4, for the reason
    :func:`steward.session_auth.new_session_credential` gives: this is a bearer secret
    presented to an HTTP endpoint, and 32 bytes from the system CSPRNG is the right amount
    of unguessable for that.
    """
    return f"{OPERATOR_CREDENTIAL_PREFIX}{secrets.token_urlsafe(OPERATOR_CREDENTIAL_BYTES)}"


def looks_like_operator_credential(value: str) -> bool:
    """Report whether a presented bearer value is *shaped* like an operator credential.

    A cheap syntactic test that grants nothing, used to decide which check to run. The API
    tries the master token first and only reaches for a table when what was presented could
    not be anything else.
    """
    return bool(OPERATOR_CREDENTIAL_PATTERN.fullmatch(value))


def operator_email(name: str) -> str:
    """Return the default git author address for an operator who named no other one.

    Derived from the name rather than left blank, because git wants an address and a blank
    one produces a commit whose author is unparseable. Anything outside the safe set
    becomes a hyphen, so a name with a space in it still yields a valid address.
    """
    local = re.sub(r"[^a-z0-9._-]+", "-", name.strip().lower()).strip("-.") or "operator"
    return f"{local}@{OPERATOR_EMAIL_DOMAIN}"


@dataclass(frozen=True, slots=True)
class OperatorPrincipal:
    """Who an operator credential says the caller is, resolved from the live row.

    Read from steward's own table rather than from a request header, which is the whole
    point: an operator cannot claim a name that was not minted for them, and cannot keep
    one that has been revoked.
    """

    name: str
    email: str

    @property
    def identity(self) -> tuple[str, str]:
        """The git author this operator's writes are committed as, as ``(name, email)``.

        Returned as a pair rather than a :class:`steward.nursery.CommitIdentity` so this
        module keeps its one-way dependency: the authoring layer knows about operators
        only through the caller that hands it this.
        """
        return (self.name, self.email)

    @property
    def principal(self) -> str:
        """How a commit trailer describes the change this operator made.

        Distinct from :data:`steward.api.API_PRINCIPAL` in exactly the way that matters:
        that one can only name the door, and this one names the person who came through it.
        """
        return f"{self.name}, over the steward API with an operator credential"
