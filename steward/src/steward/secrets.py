"""Where a credential steward holds actually lives: one file per secret, mode 600.

Before warren#462 a bot token reached the fleet exactly one way — a line in the ``.env``
beside the burrow's compose file, appended over ssh, followed by a container recreate. That
is three problems in one step. The paste is the step a Claude session cannot take (a
classifier refuses it, and rightly: it is a credential going into a shell heredoc); the
recreate is a blunt instrument for "one more token exists"; and the ``.env`` is read exactly
once, at process start, so nothing short of a restart can ever change it.

A **directory of files** fixes all three. It is writable through an authenticated API call
rather than a shell, it can be re-read on a timer by a process that is already running, and
each secret is its own file with its own mode rather than a line in a shared one.

Two rules hold the design together:

**File, then environment.** :func:`read_secret` looks in the directory first and falls back
to the mapping it was given. Nothing migrates by force: a burrow whose ``.env`` still holds
every token behaves exactly as it did, and a token written as a file simply wins for that
one name. That is what makes this deployable without a cutover.

**A name is an environment variable name.** ``STEWARD_CHAT_TOKEN_DISCORD_HOB``, not a path.
The grammar (:func:`valid_name`) is checked on the way in *and* on the way out, so a name
that arrived over HTTP can never be joined onto the directory as ``../../etc/passwd``, and
a file somebody dropped in by hand that is not a credential slot is not listed as one.

Nothing here belongs in ``steward.db``. The database is one file that gets copied freely —
three ``data.bak-*`` directories sit on the burrow as this is written — and a resident with
database access would gain a path to another resident's identity, which ``app_grants``
exists to prevent.
"""

from __future__ import annotations

import logging
import os
import re
import tempfile
from collections.abc import Mapping
from pathlib import Path

__all__ = [
    "DEFAULT_SECRETS_DIR",
    "NAME_MAX_CHARS",
    "SECRETS_DIR_ENV",
    "VALUE_MAX_CHARS",
    "SecretError",
    "overlay",
    "read_secret",
    "secret_names",
    "secrets_dir",
    "valid_name",
    "write_secret",
]

log = logging.getLogger("steward.secrets")

#: Where the burrow bind-mounts the directory into every control-plane container. An
#: absolute path outside the checkout on purpose: a secret must never be one ``git add``
#: away from a repository, and ``/secrets`` is a mount point rather than a directory of the
#: image, so a container running without the mount finds nothing rather than something old.
DEFAULT_SECRETS_DIR = "/secrets"

#: How a laptop, a test run, or a second burrow layout moves that directory. Set in the
#: environment like every other steward path setting, and read from the *same* mapping a
#: caller passes for the environment, so a reader working from an explicit mapping cannot
#: accidentally read the real machine's secrets.
SECRETS_DIR_ENV = "STEWARD_SECRETS_DIR"

#: The grammar of a secret name: exactly what an environment variable may be called, which
#: is what these names *are*. Anchored, upper-case, no separators but ``_``, and no trailing
#: underscore — so ``valid_name`` doubles as the traversal guard for every path join below.
_NAME = re.compile(r"^[A-Z][A-Z0-9]*(?:_[A-Z0-9]+)*$")

#: How long a name may be. Far past any real slot; short enough that no filesystem refuses.
NAME_MAX_CHARS = 128

#: How long a value may be. The longest credential steward carries is a Discord bot token at
#: about seventy characters, and a PEM key is the only plausible reason to want more. Bounded
#: because this is a write path into a burrow's disk, and an unbounded one is a way to fill it.
VALUE_MAX_CHARS = 8192


class SecretError(Exception):
    """Raised when steward refuses to write a secret, and says exactly why.

    Never carries the value — only the name and the rule it broke. A refusal that quoted
    what it refused would put the credential into a log line, which is the failure mode this
    whole module exists to avoid.
    """


def valid_name(name: str) -> bool:
    """Return whether ``name`` is a secret slot steward will read or write.

    The one gate. Called before every path join in this module, so no caller can turn a
    name into a directory traversal, a hidden file, or a nested path.
    """
    return bool(name) and len(name) <= NAME_MAX_CHARS and _NAME.fullmatch(name) is not None


def secrets_dir(env: Mapping[str, str] | None = None) -> Path:
    """Return the directory secrets are read from, from the environment or the default."""
    source = os.environ if env is None else env
    configured = (source.get(SECRETS_DIR_ENV) or "").strip()
    return Path(configured) if configured else Path(DEFAULT_SECRETS_DIR)


def secret_names(
    directory: Path | str | None = None, env: Mapping[str, str] | None = None
) -> list[str]:
    """Return the names that have a file in the directory, sorted. Missing means none.

    A directory that is not there is the normal state of a laptop and of a burrow nobody has
    written a secret on yet, so it is an empty list rather than a complaint. Only names the
    grammar accepts are returned: a ``.gitkeep``, an editor's backup, or a subdirectory is
    not a credential slot no matter what it is called.
    """
    path = Path(directory) if directory is not None else secrets_dir(env)
    try:
        entries = list(path.iterdir())
    except OSError:
        return []
    return sorted(entry.name for entry in entries if valid_name(entry.name) and entry.is_file())


def read_secret(
    name: str,
    *,
    env: Mapping[str, str] | None = None,
    directory: Path | str | None = None,
) -> str | None:
    """Return one secret's value: the file if there is one, else the environment, else ``None``.

    Whitespace is stripped from both sources, and an empty result is ``None`` rather than
    ``""``. A slot somebody created and never filled is *unset* — the same answer the
    environment gives for an exported-but-blank variable — because "set to nothing" is a
    state no caller wants to have to distinguish.

    Never raises. An unreadable file is a permissions mistake on a mount, and a daemon that
    died mid-poll over one would be a worse outcome than a route that reports no token.
    """
    source = os.environ if env is None else env
    if not valid_name(name):
        return None
    path = (Path(directory) if directory is not None else secrets_dir(source)) / name
    try:
        value = path.read_text(encoding="utf-8").strip()
    except OSError:
        value = ""
    except UnicodeDecodeError:
        log.warning("%s is not text; ignoring the file and falling back to the environment", name)
        value = ""
    return value or (source.get(name) or "").strip() or None


def overlay(
    env: Mapping[str, str] | None = None, *, directory: Path | str | None = None
) -> dict[str, str]:
    """Return ``env`` with every secret file laid over it — one mapping, files winning.

    The adapter that lets warren#462 land without rewriting every reader in steward. Anything
    that already takes an ``env``-shaped mapping (:func:`steward.chat.tokens_from_env`,
    :func:`steward.chat.operators_from_env`, a transport's ``from_env``) can be handed this
    instead and gains file resolution with no further change.

    A copy: ``os.environ`` is the default argument of every caller and must come back
    untouched — a module that mutated the process environment as a side effect of reading
    would leak a credential into every child process steward launches.
    """
    source = dict(os.environ if env is None else env)
    base = Path(directory) if directory is not None else secrets_dir(source)
    for name in secret_names(base):
        value = read_secret(name, env={}, directory=base)
        if value is not None:
            source[name] = value
    return source


def write_secret(
    name: str,
    value: str,
    *,
    directory: Path | str | None = None,
    env: Mapping[str, str] | None = None,
) -> Path:
    """Write one secret to its own private file, atomically, and return the path.

    Atomic through a temporary file and a rename inside the same directory, for the reason
    every atomic write in steward is: a reader is a *daemon on a timer*, and a half-written
    token is a route that reports "could not identify a bot" until somebody writes it again.
    The scratch file is created inside the secrets directory rather than ``/tmp`` so the
    rename cannot cross a filesystem, and it is created 0600 from the start — there is no
    moment at which the value exists in a world-readable file.

    The directory itself is created 0700. A secret file nobody may read inside a directory
    anybody may list is a name leak, and the names here say which residents exist.
    """
    if not valid_name(name):
        raise SecretError(
            f"{name!r} is not a secret name; a name is an environment variable name — "
            "upper case, digits and underscores, as in STEWARD_CHAT_TOKEN_DISCORD_HOB"
        )
    cleaned = value.strip()
    if not cleaned:
        raise SecretError(f"{name} was given a blank value; there is nothing to store")
    if len(cleaned) > VALUE_MAX_CHARS:
        raise SecretError(
            f"{name} was given a value longer than {VALUE_MAX_CHARS} characters, which is "
            "longer than any credential steward carries"
        )
    if "\n" in cleaned or "\r" in cleaned:
        raise SecretError(
            f"{name} was given a value that is not one line; a credential is a single token, "
            "so a multi-line body is a paste that took more than was meant"
        )
    base = Path(directory) if directory is not None else secrets_dir(env)
    base.mkdir(parents=True, exist_ok=True, mode=0o700)
    destination = base / name
    descriptor, scratch = tempfile.mkstemp(dir=base, prefix=".", suffix=".tmp")
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(cleaned)
            handle.flush()
            os.fsync(handle.fileno())
        Path(scratch).chmod(0o600)
        Path(scratch).replace(destination)
    except BaseException:
        Path(scratch).unlink(missing_ok=True)
        raise
    return destination
