"""Credential rejection and redaction for declarations, documents and runtime output."""

import re
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import Any

from steward.diagnostics import Diagnostic
from steward.operator_auth import OPERATOR_CREDENTIAL_PATTERN
from steward.session_auth import SESSION_CREDENTIAL_PATTERN

#: The vocabulary a credential-shaped name is built from — one source, so the validator
#: that rejects such a field in a manifest and the redactor that scrubs one out of a knock
#: (:func:`redact_secrets`) can never disagree about what the word "token" means.
_CREDENTIAL_WORDS = (
    r"token|tokens|secret|secrets|password|passwd|passphrase|"
    r"api[_-]?key|apikey|access[_-]?key|private[_-]?key|signing[_-]?key|session[_-]?key|"
    r"client[_-]?secret|credential|credentials|bearer|authorization|cookie|"
    r"refresh[_-]?token|access[_-]?token|auth[_-]?token"
)

CREDENTIAL_KEY_PATTERN = re.compile(
    rf"(?:^|[_.\- ])(?:{_CREDENTIAL_WORDS})(?:$|[_.\- ])",
    re.IGNORECASE,
)

SECRET_VALUE_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"), "an inline private key"),
    (re.compile(r"\bsk-[A-Za-z0-9_-]{16,}"), "an inline API key"),
    (re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}"), "an inline GitHub token"),
    (re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}"), "an inline GitHub token"),
    (re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}"), "an inline Slack token"),
    # A BotFather token: the bot's numeric id, a colon, and thirty-odd characters of
    # secret. Both halves of the rule apply to it (warren#108). A chat route's ``address``
    # is a *reference* to a bot and the token lives in steward's environment, so one
    # written into a manifest is a credential in git — and a resident that stumbles across
    # its own bot's token must not be able to echo it back into the chat, which is the one
    # place a reply is guaranteed to be read by whoever is watching that conversation.
    (re.compile(r"\b\d{6,12}:[A-Za-z0-9_-]{30,}"), "an inline Telegram bot token"),
    (
        re.compile(r"\b[MN][A-Za-z\d]{23,}\.[\w-]{6}\.[\w-]{27,}"),
        "an inline Discord bot token",
    ),
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), "an inline AWS access key id"),
    (re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b"), "an inline Google API key"),
    (
        re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}"),
        "an inline JWT",
    ),
    (re.compile(r"://[^/\s:@]+:[^/\s@]{3,}@"), "an inline password in a URL"),
    # Steward's own per-run session credential (steward #41). It is here rather than in a
    # redaction-only list because both halves of the rule apply to it: a session must not
    # be able to leak its credential into a burrow event, *and* a credential must never be
    # committed into a manifest, a soul, or a skill.
    (SESSION_CREDENTIAL_PATTERN, "an inline steward session credential"),
    # And steward's own named-operator credential (warren#225), for both halves of the
    # same rule: it is the credential a browser holds, so it is the one most likely to be
    # pasted somewhere by hand, and it outlives a run rather than dying with one — which
    # makes leaking it worse than leaking a session's, not better.
    (OPERATOR_CREDENTIAL_PATTERN, "an inline steward operator credential"),
)

# A reference field holds a pointer (path, URL, scheme-prefixed handle). A long run of
# random-looking characters with no separators is a value pretending to be a reference.
_BLOB_CHARSET = re.compile(r"[A-Za-z0-9+_=-]{32,}")
_HEX_DIGEST = re.compile(r"[0-9a-fA-F]{32,}")


def _looks_like_opaque_blob(value: str) -> bool:
    """Report whether a reference-shaped field holds something that looks like a secret."""
    if not _BLOB_CHARSET.fullmatch(value):
        return False
    if _HEX_DIGEST.fullmatch(value):
        return True
    return (
        any(char.isdigit() for char in value)
        and any(char.isupper() for char in value)
        and any(char.islower() for char in value)
    )


REFERENCE_FIELDS = frozenset({"memory.path", "routes.address", "app_grants.status_ref"})

#: The exact field paths whose *names* look credential-shaped and are not. There is one,
#: and it earns its place: ``budgets.daily_tokens`` is how many tokens a resident may
#: spend in a day — an integer, and the whole point of the field is that a person reads it
#: next to ``daily_cost_usd``. The alternative was to name it something the scanner does
#: not recognise, which would have meant letting a regex choose the vocabulary of the
#: manifest. An exemption is a path, never a prefix and never a pattern, so nothing new
#: slips through by being spelled cleverly.
CREDENTIAL_NAME_EXEMPT = frozenset({"budgets.daily_tokens"})

CREDENTIAL_EXAMPLE = (
    "drop the field entirely — credentials live outside this repo; "
    "declare access instead: app_grants: [{id: gmail, name: Gmail, status: granted}]"
)


def _walk(node: object, path: str = "") -> Iterator[tuple[str, object]]:
    """Yield ``(dotted_path, value)`` for every node of a parsed YAML tree."""
    yield path, node
    if isinstance(node, Mapping):
        for key, value in node.items():
            child = f"{path}.{key}" if path else str(key)
            yield from _walk(value, child)
    elif isinstance(node, (list, tuple)):
        for index, value in enumerate(node):
            yield from _walk(value, f"{path}[{index}]")


def _strip_indices(path: str) -> str:
    return re.sub(r"\[\d+\]", "", path)


def scan_for_credentials(data: object, source: Path) -> list[Diagnostic]:
    """Reject credential-shaped keys and inline secrets anywhere in a parsed tree.

    Runs before schema validation so a secret is never bound into a model, let alone
    written back out.
    """
    diagnostics: list[Diagnostic] = []
    for path, value in _walk(data):
        if not path:
            continue
        normalized = _strip_indices(path)
        leaf = normalized.rsplit(".", 1)[-1]
        if normalized not in CREDENTIAL_NAME_EXEMPT and CREDENTIAL_KEY_PATTERN.search(leaf):
            diagnostics.append(
                Diagnostic(
                    file=source,
                    field_path=path,
                    problem=(
                        f"field name {leaf!r} is credential-shaped; manifests carry "
                        f"references and grants, never credentials"
                    ),
                    example=CREDENTIAL_EXAMPLE,
                )
            )
            continue
        if not isinstance(value, str):
            continue
        for pattern, description in SECRET_VALUE_PATTERNS:
            if pattern.search(value):
                diagnostics.append(
                    Diagnostic(
                        file=source,
                        field_path=path,
                        problem=f"value looks like {description}",
                        example=CREDENTIAL_EXAMPLE,
                    )
                )
                break
        else:
            if normalized in REFERENCE_FIELDS and _looks_like_opaque_blob(value):
                diagnostics.append(
                    Diagnostic(
                        file=source,
                        field_path=path,
                        problem=(
                            "value is an opaque blob where a reference is expected; "
                            "this field points at a location, it does not hold a value"
                        ),
                        example="/data/residents/hob/memory  (or https://…, op://…)",
                    )
                )
    return diagnostics


def scan_text_for_secrets(text: str, source: Path, field_path: str) -> list[Diagnostic]:
    """Reject inline secrets in a free-form document (soul body, skill file)."""
    diagnostics: list[Diagnostic] = []
    for pattern, description in SECRET_VALUE_PATTERNS:
        if pattern.search(text):
            diagnostics.append(
                Diagnostic(
                    file=source,
                    field_path=field_path,
                    problem=f"document contains {description}",
                    example=CREDENTIAL_EXAMPLE,
                )
            )
    return diagnostics


#: What a scrubbed secret leaves behind. A marker, not a deletion: a human reading the
#: knock still sees that a secret *was* there and that steward removed it, rather than a
#: gap that reads like the session simply said nothing.
SECRET_REDACTION = "[redacted:secret]"  # noqa: S105 — a redaction marker, not a credential

#: A credential-shaped assignment in free-form text — ``CHRONICLE_TOKEN=…``, ``api_key: …`` —
#: built from the same vocabulary the manifest validator rejects (:data:`_CREDENTIAL_WORDS`)
#: so a secret a session writes into a ``needs_human`` detail is scrubbed by the same
#: definition of "credential" that keeps one out of a manifest. Authorization headers
#: have their own matcher below: their scheme is context, not the value to remove.
_CREDENTIAL_ASSIGNMENT = re.compile(
    rf"(?<![A-Za-z0-9])(?!authorization\s*[:=])"
    rf"(?P<key>(?:[A-Za-z0-9]+[_.\- ])*(?:{_CREDENTIAL_WORDS}))"
    r"(?P<sep>\s*[:=]\s*)(?P<value>\S+)",
    re.IGNORECASE,
)

_AUTHORIZATION_ASSIGNMENT = re.compile(
    r"(?P<key>\bauthorization)(?P<sep>\s*[:=]\s*)"
    r"(?P<scheme>(?:bearer|basic)\s+)?(?P<value>[^\s'\"]+)",
    re.IGNORECASE,
)

#: A whole PEM private key, header to footer. :data:`SECRET_VALUE_PATTERNS` matches only
#: the ``BEGIN`` marker — enough to *detect* one in a manifest — but redaction has to take
#: the key material with it, not leave the base64 body behind, so egress gets its own
#: block-spanning pattern.
_PEM_BLOCK = re.compile(
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----",
    re.DOTALL,
)


def redact_secrets(text: str) -> str:
    """Return ``text`` with any inline secret replaced by :data:`SECRET_REDACTION`.

    The egress twin of the manifest scanners (steward #65): where
    :func:`scan_for_credentials` and :func:`scan_text_for_secrets` *refuse* a secret on
    the way into the repo, this *removes* one on the way out to the village — a session
    that puts an ``sk-…`` key, a PEM block, a JWT, a URL password, or a ``TOKEN=…``
    assignment into a ``needs_human`` message or detail must not have it POSTed to burrow
    verbatim. It reuses the very patterns those scanners use — the value shapes in
    :data:`SECRET_VALUE_PATTERNS` and the credential vocabulary in
    :data:`_CREDENTIAL_ASSIGNMENT` — so "what counts as a secret" is defined once. Only the
    secret is cut; the words around it survive, so the knock still reads as a question.
    """
    text = _PEM_BLOCK.sub(SECRET_REDACTION, text)
    for pattern, _ in SECRET_VALUE_PATTERNS:
        text = pattern.sub(SECRET_REDACTION, text)
    text = _AUTHORIZATION_ASSIGNMENT.sub(
        lambda match: (
            f"{match.group('key')}{match.group('sep')}"
            f"{match.group('scheme') or ''}{SECRET_REDACTION}"
        ),
        text,
    )
    return _CREDENTIAL_ASSIGNMENT.sub(
        lambda match: f"{match.group('key')}{match.group('sep')}{SECRET_REDACTION}", text
    )


def redact_mapping(mapping: Mapping[str, Any] | None) -> dict[str, object] | None:
    """Return ``mapping`` with every string it carries, at any depth, scrubbed of secrets.

    The structured twin of :func:`redact_secrets`, and it lives here rather than at any one
    egress because the same model-written ``detail`` reaches humans by more than one road —
    a ``needs_human`` event POSTed to burrow, a decision printed by ``steward show``. It
    recurses into nested maps and lists so a secret buried under a key or inside a list is
    scrubbed as surely as one at the top level; non-string leaves (the numbers a budget
    pause reports, the ISO instants of a window) are facts steward built and pass through
    untouched.
    """
    if mapping is None:
        return None
    return {str(key): _redact_node(value) for key, value in mapping.items()}


def _redact_node(value: object) -> object:
    """Redact one node of a detail tree: a string, a nested map, a list, or a leaf."""
    if isinstance(value, str):
        return redact_secrets(value)
    if isinstance(value, Mapping):
        return {str(key): _redact_node(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_redact_node(item) for item in value]
    return value
