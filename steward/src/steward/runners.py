"""Runners: the one seam through which steward launches a headless session.

A resident's manifest declares *which brain* it runs on (``runner.kind`` and
``runner.model``); this module is the only place in steward that turns that
declaration into a process. The scheduler, and later the job board, delegation, and
approvals, all go through :func:`build_runner` — **no other module may spawn an LLM
CLI**, which ``tests/test_runners.py`` asserts by reading the source tree.

Four kinds:

``claude``
    ``claude -p <prompt> --model <model> --output-format json``. The JSON result
    carries usage and cost, so a claude run feeds the budget ledger for free.
``codex``
    ``codex exec <prompt>``. Plain text out; usage is not available, and steward
    reports it as unknown rather than guessing.
``command``
    An argv template from the manifest, with ``{prompt}`` and ``{workdir}``
    substituted in a single pass. Never a shell, ever: manifest content is data.
``mock``
    Deterministic, no network, no subprocess. Used by tests and ``--dry-run``.

Every runner returns the same :class:`RunResult` with a truthful ``outcome``:
``ok`` only when the process exited zero, ``timeout`` only when steward killed it,
``failed`` for everything else.

Where the process runs is a second, independent axis (steward #58): a resolved
:class:`Placement` says whether the session happens in this process's own machine or
inside the resident's container over ``docker exec``. The kind keeps answering *what
argv* — which is what keeps ``--output-format json`` and the budget ledger's cost parse
working wherever the session is placed.

One more thing lives here, and it is not a session: :func:`run_argv`, a short bounded
command whose exit status is the whole answer. The watchdog needs it to ask ``docker``
whether a container is running and to restart it when it is not. It is in this module
rather than that one because the rule this repo enforces is *steward starts processes in
exactly one file* — a rule worth keeping even for the processes that are not brains.
"""

import contextlib
import fcntl
import hashlib
import json
import logging
import math
import os
import re
import secrets
import select
import shlex
import shutil
import signal
import subprocess
import sys
import time
from abc import ABC, abstractmethod
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from enum import StrEnum
from pathlib import Path
from typing import Any, ClassVar

from steward.manifest import UNPLACEABLE_RUNNER_KINDS, ToolGrant
from steward.manifest import Runner as RunnerSpec
from steward.session_auth import SESSION_TOKEN_ENV

__all__ = [
    "CHRONICLE_HOOKS",
    "CHRONICLE_MIRROR_ENV",
    "CHRONICLE_TOKEN_ENV",
    "CHRONICLE_URL_ENV",
    "CONTAINER_EMITTER",
    "COST_USD_MAX",
    "LOCAL_PLACEMENT",
    "SESSION_EMITTER_ENV",
    "SESSION_ENV_BASE",
    "SESSION_ENV_PASSTHROUGH_ENV",
    "SESSION_ENV_REFUSED",
    "TOKENS_MAX",
    "TRANSFER_TIMEOUT_S",
    "ClaudeRunner",
    "CodexRunner",
    "CommandOutcome",
    "CommandRun",
    "CommandRunner",
    "MockRunner",
    "Outcome",
    "PipedRun",
    "Placement",
    "RunRequest",
    "RunResult",
    "Runner",
    "RunnerError",
    "build_runner",
    "check_cli_support",
    "check_runner",
    "check_session_emitter",
    "check_session_ingest",
    "hook_settings",
    "required_flags",
    "run_argv",
    "session_emitter",
    "session_emitter_outbox",
    "session_environment",
    "skills_home",
    "substitute",
]

OUTPUT_MAX_CHARS = 20_000
KILL_GRACE_S = 5.0

#: How long a short bounded command (``docker inspect``, ``docker restart``) may take
#: before steward stops waiting. Not a session timeout: this is a control-plane call that
#: should answer in milliseconds, and one that hangs is itself the answer.
COMMAND_TIMEOUT_S = 20.0

#: The most a single session may claim to have cost before steward stops believing it.
#: This is the widest data channel from a model process into steward — ``total_cost_usd``
#: and ``usage.*`` are read straight out of the child's own stdout JSON — and it is the one
#: place where a safety control's input is supplied by the thing being controlled. The
#: daily cap is ``SUM(cost_usd) >= limit``, so a single ``NaN`` row makes every subsequent
#: comparison return ``False`` and the cap silently stops tripping for the rest of the day;
#: a large negative row offsets real spend the same way (steward #129). These are absurdity
#: ceilings rather than policy: the cap a resident actually runs under is its manifest's,
#: and no real session comes near these.
COST_USD_MAX = 10_000.0

#: The same ceiling for token counts, which are written to an 8-byte SQLite ``INTEGER``
#: and summed. Generous by orders of magnitude against any real session.
TOKENS_MAX = 1_000_000_000

# --------------------------------------------------------------------------------------
# the session environment (steward #41)
# --------------------------------------------------------------------------------------

#: What every session inherits from the process that launched it, by name.
#:
#: This list is the boundary. Until steward #41 the child environment was
#: ``{**os.environ, **request.env}``, and a ``steward serve`` with auth on holds
#: ``STEWARD_TOKEN`` in ``os.environ`` *by construction* — there is no ``--token`` flag —
#: while ``POST /residents/{id}/routines/{id}/run`` fires sessions from inside that very
#: process. So every locally launched session was carrying the master key into the API,
#: which the CLI it was supposed to use instead calls "a credential no session should be
#: holding" in the same repo. The token-free ``<needs-human>``/``<delegate>`` channels were
#: a convention the sessions were never held to.
#:
#: An allowlist rather than a denylist, because the failure modes are not symmetric: a
#: name missing from an allowlist is a session that cannot find something and says so,
#: while a name missing from a denylist is a secret nobody notices leaving. The container
#: launcher in steward #58 has to name its variables one at a time anyway (``docker exec
#: -e``), so the two placements agree instead of diverging.
#:
#: Three groups, and the reason each is here:
SESSION_ENV_BASE = (
    # The shape of the machine. Without ``PATH`` there is no brain to launch at all, and
    # ``HOME`` is where every CLI in this repo keeps its own credentials and settings —
    # which is the point: the brain's auth comes off the disk, under the account steward
    # runs as, rather than through a variable steward passes around.
    "HOME",
    "PATH",
    "LOGNAME",
    "SHELL",
    "TERM",
    "TMPDIR",
    "USER",
    # Locale and clock, so a session's own output and timestamps read the way the
    # operator's do.
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
    "TZ",
    # How this host reaches the network at all. A NAS behind a proxy or with its own CA
    # bundle would otherwise have every session fail at the first HTTPS call, and none of
    # these is a credential.
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "NO_PROXY",
    "http_proxy",
    "https_proxy",
    "no_proxy",
    "NODE_EXTRA_CA_CERTS",
    "REQUESTS_CA_BUNDLE",
    "SSL_CERT_DIR",
    "SSL_CERT_FILE",
    # Steward's own *configuration* — never its credentials. A session with shell access is
    # expected to call ``steward delegate`` and ``steward approval raise``, and those
    # commands open the same database and read the same caps the control plane does:
    # ``STEWARD_STATE`` is where ``default_db_path`` looks, and the other three are read by
    # ``max_depth``, ``repeat_deny_window_s`` and ``EventEmitter.from_env``. Drop them and a
    # session's CLI quietly opens a different ``steward.db`` under its own working
    # directory, or runs under a delegation depth cap nobody chose.
    "STEWARD_EVENTS_FALLBACK",
    "STEWARD_MAX_DELEGATION_DEPTH",
    "STEWARD_REPEAT_DENY_WINDOW_H",
    "STEWARD_STATE",
    # Where the village is, so a session's own emitter posts to the same burrow. The
    # ingest token is deliberately *not* here — see :data:`SESSION_ENV_REFUSED`.
    "CHRONICLE_URL",
    # The API address paired with this run's per-session credential. Unlike STEWARD_TOKEN,
    # this is an address, not authority; the credential still bounds what the caller can do.
    "STEWARD_URL",
    # And where else it posts. Here so an operator who *wants* a mirror can say so; the
    # reason it needs saying at all is :data:`CHRONICLE_MIRROR_ENV`.
    "CHRONICLE_MIRROR",
)

#: Names a runner adds for its own brain, on top of :data:`SESSION_ENV_BASE`.
#:
#: A session has to be able to authenticate to its own model provider — that credential is
#: the session's fuel, not steward's master key, and a session that cannot buy tokens is
#: not a bounded session but a broken one. Everything else about the brain (which model,
#: which tools, which directory) is already declared in the manifest and passed as argv,
#: so this stays to auth and endpoint.
CLAUDE_ENV_NAMES = (
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
    "ANTHROPIC_BASE_URL",
    "ANTHROPIC_CUSTOM_HEADERS",
    "AWS_REGION",
    "CLAUDE_CODE_USE_BEDROCK",
    "CLAUDE_CODE_USE_VERTEX",
    "CLAUDE_CONFIG_DIR",
    "CLOUD_ML_REGION",
    "GOOGLE_APPLICATION_CREDENTIALS",
)

CODEX_ENV_NAMES = (
    "CODEX_HOME",
    "OPENAI_API_KEY",
    "OPENAI_BASE_URL",
)

#: The operator's named escape hatch: a comma-separated list of variables this fleet's
#: sessions also need.
#:
#: An allowlist that cannot be extended is an allowlist that gets reverted the first time
#: a real deployment needs one name nobody upstream thought of — a ``command`` runner
#: template that shells out over ``ssh`` needs ``SSH_AUTH_SOCK``, and this repo documents
#: that template as the multi-machine workaround. So the hatch exists, and it is a list of
#: *names*: what a session may see stays a decision somebody wrote down, and ``steward``
#: can print it.
SESSION_ENV_PASSTHROUGH_ENV = "STEWARD_SESSION_ENV_PASSTHROUGH"

#: Names the passthrough refuses to forward, however it is spelled.
#:
#: ``STEWARD_TOKEN`` is the whole reason steward #41 exists: it is the master key into
#: steward's own API, where deciding an approval and delegating as any resident hang off
#: the same shared secret, so there is no configuration in which a session legitimately
#: holds it. ``STEWARD_SESSION_TOKEN`` is refused for the opposite reason — it must come
#: from the mint at fire time, per run, and an operator-supplied one would be a credential
#: with no run behind it and no expiry.
#:
#: ``CHRONICLE_TOKEN`` is not in this set, and that is a narrower statement than it looks.
#: It is off the default allowlist for its own reason — one shared ingest secret whose
#: holder can post events as any ``agent_id`` — and a session should not be given it. What
#: a session loses without it depends on the village, and it is *not* nothing: against an
#: open village the hook events land anyway, and against a token-guarded one every one of
#: them is a 401 and journals to the hook emitter's own ``~/.chronicle/events.jsonl``.
#: Nothing drains that file — ``steward events flush`` drains steward's *own* emitter's
#: ``events.jsonl.pending``, a different queue under a different owner — so those events are
#: neither lost nor delivered (warren#449). Naming it here is therefore an operator buying
#: *live* emission at the price of that shared secret, and steward does not refuse it only
#: because the choice is legitimately theirs to get wrong. What steward does instead is
#: refuse to let the trade happen by accident: :func:`check_session_ingest` is where
#: ``steward doctor`` gets the line that says a local session's events cannot be delivered
#: from here. Per-resident ingest credentials are the real answer, and are their own issue.
SESSION_ENV_REFUSED = frozenset({"STEWARD_TOKEN", SESSION_TOKEN_ENV})


def passthrough_names(inherited: Mapping[str, str]) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Split ``$STEWARD_SESSION_ENV_PASSTHROUGH`` into what it forwards and what it may not.

    Returns ``(forwarded, refused)``. Refusals are returned rather than dropped so the
    caller can say them out loud: an operator who listed ``STEWARD_TOKEN`` believes their
    sessions have it, and silently not forwarding it would be a boundary that holds for the
    wrong reason.
    """
    raw = (inherited.get(SESSION_ENV_PASSTHROUGH_ENV) or "").strip()
    named = tuple(dict.fromkeys(part.strip() for part in raw.split(",") if part.strip()))
    forwarded = tuple(name for name in named if name not in SESSION_ENV_REFUSED)
    refused = tuple(name for name in named if name in SESSION_ENV_REFUSED)
    return forwarded, refused


def session_environment(
    request: RunRequest,
    *,
    allowed: Sequence[str],
    inherited: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Build the exact environment one session is launched with. Nothing else gets in.

    Two sources, in this order: the allowlisted names this host actually has set, then
    ``request.env`` — the facts steward *chose* to tell this session, which win, because a
    resident's ``CHRONICLE_AGENT_ID`` is its identity and not the launching process's.

    An unset name is absent rather than empty. ``PATH=""`` is not "no preference", it is a
    ``PATH`` with one entry, the current directory, and a session that cannot find its own
    brain should fail saying so rather than exec something out of its workdir.
    """
    source = os.environ if inherited is None else inherited
    forwarded, refused = passthrough_names(source)
    for name in refused:
        log.warning(
            "%s names %s, which no session may hold; it is not being forwarded",
            SESSION_ENV_PASSTHROUGH_ENV,
            name,
        )
    names = (*allowed, *forwarded)
    env = {name: source[name] for name in names if name in source}
    env.update(request.env)
    return env


#: The two flags a bounded session is launched with. Both, or the bound does not hold:
#: ``--tools`` alone leaves the host's MCP servers reachable (see :meth:`ClaudeRunner.argv`),
#: and ``--strict-mcp-config`` alone bounds nothing.
#:
#: :meth:`ClaudeRunner.argv` emits these and :func:`check_cli_support` probes for them, and
#: both read this one tuple. They must: the probe exists to say the installed CLI supports
#: what argv sends, so a second copy of the spelling is a probe that can go on passing over
#: an argv it no longer describes.
TOOL_BOUND_FLAGS = ("--tools", "--strict-mcp-config")

#: The flag that widens a session past its own working directory
#: (:attr:`RunRequest.workspace`), read by the same two places for the same reason.
WORKSPACE_FLAG = "--add-dir"

#: The flag naming which settings files a session loads, and the value steward gives it —
#: the CLI's spelling for *none of them* (steward #206). One tuple for the same reason
#: :data:`TOOL_BOUND_FLAGS` is one: :meth:`ClaudeRunner.argv` spends it and
#: :func:`required_flags` hands the spelling to the doctor probe, and a second copy is a
#: probe that can go on passing over an argv it no longer describes.
#:
#: Unlike the two above, this is not compiled from anything a manifest says — it is a
#: property of how steward launches every ``claude`` session, so every claude resident is
#: probed for it whether or not it declared a thing.
SETTING_SOURCES = ("--setting-sources", "")

_PLACEHOLDER = re.compile(r"\{(prompt|workdir)\}")

log = logging.getLogger("steward.runners")

_DESCRIPTOR_CWD_HELPER = """
import os
import sys

workdir_fd = int(sys.argv[1])
status_fd = int(sys.argv[2])
argv = sys.argv[3:]
try:
    os.set_inheritable(status_fd, False)
    os.fchdir(workdir_fd)
    os.close(workdir_fd)
    os.execvpe(argv[0], argv, os.environ)
except OSError as exc:
    message = str(exc.strerror or exc).encode("utf-8", "replace")[:1000]
    try:
        os.write(status_fd, message)
    except OSError:
        pass
    raise SystemExit(126) from None
"""


class Outcome(StrEnum):
    """What actually happened to a run. Nothing here is a guess."""

    OK = "ok"
    FAILED = "failed"
    TIMEOUT = "timeout"


class RunnerError(Exception):
    """Raised when a runner cannot be built, or placed, from a manifest declaration."""


@dataclass(frozen=True, slots=True)
class Placement:
    """Where a session's process runs. The second axis of a runner (steward #58).

    ``runner.kind`` answers *which brain*; this answers *on which machine, in which
    filesystem*. It is a resolved value object rather than the manifest itself, so the
    runner seam stays ignorant of deploy blocks: whoever builds a runner resolves the
    address once (``steward.deploy.placement_for``) and hands it over.

    ``container=None`` is local placement — the process the scheduler runs in, exactly
    as every session has always launched. A named container means sessions happen inside
    it via ``docker exec``; ``workdir`` is then the *container-side* working directory
    (the mount point of the resident's memory volume), which is deliberately a separate
    string from :attr:`RunRequest.workdir` — that one stays the control plane's own path
    to the same files, where the journal is read and skills are materialized.
    """

    container: str | None = None
    workdir: str | None = None

    def __post_init__(self) -> None:
        """Refuse a container workdir with no container: half an address is none."""
        if self.workdir is not None and self.container is None:
            raise RunnerError("a placement workdir names a path inside a container; name one")

    @property
    def is_container(self) -> bool:
        """True when sessions are placed inside the resident's own container."""
        return self.container is not None

    @property
    def container_name(self) -> str:
        """Return the container's name, on the paths only reached when :attr:`is_container`.

        The one place the ``str | None`` is narrowed: every caller is behind an
        ``is_container`` guard, and spelling ``container or ""`` at each would restate
        the invariant four times over.
        """
        return self.container or ""

    def describe(self) -> str:
        """One word or one name, for the line that answers 'where does Hob run'."""
        return f"container {self.container}" if self.container is not None else "local"


#: The placement every session has always had: the scheduler's own machine and process
#: environment. The default everywhere a placement is not explicitly resolved.
LOCAL_PLACEMENT = Placement()


# --------------------------------------------------------------------------------------
# the hooks steward declares for a session (steward #264)
# --------------------------------------------------------------------------------------

#: The flag that names a settings document on argv — the one channel
#: :data:`SETTING_SOURCES` deliberately leaves open.
#:
#: Measured 2026-09-04 against CLI 2.1.260, and 2026-08-31 against 2.1.243/2.1.252
#: (``docs/settings-sources.md``): a document named here has its hooks fire with the
#: sources closed, and a ``.claude/settings.json`` in the session's own working directory
#: still fires nothing. So this is steward *declaring* a settings document rather than
#: inheriting whatever the filesystem holds — the same shape as ``--tools`` and
#: ``--strict-mcp-config``, and the reason #206 could close the sources without closing
#: the village's eyes for good.
SETTINGS_FLAG = "--settings"

#: The six hook events chronicle's emitter answers to, and the whole of what steward
#: declares. ``docker/resident/settings.json`` wires the same six for a human running
#: ``claude`` in the container by hand, and ``tests/test_resident_image.py`` holds the two
#: lists to each other: an event added to one and not the other is a village that half
#: sees a session.
CHRONICLE_HOOKS = (
    "UserPromptSubmit",
    "PreToolUse",
    "PostToolUse",
    "Notification",
    "Stop",
    "SessionEnd",
)

#: The two that fire per tool call, and therefore carry a matcher. ``"*"`` is every tool;
#: an entry with no matcher at these two events is not "all of them" but a rule the CLI
#: matches against a tool name, so leaving it off is how a session reports one hook and
#: emits nothing.
MATCHED_HOOKS = frozenset({"PreToolUse", "PostToolUse"})

#: How long one hook gets before the CLI abandons it, and the same five seconds
#: ``docker/resident/settings.json`` gives its own copy.
#:
#: Against a measured fire of ~50-60 ms — re-measured 2026-09-04 against the vendored
#: warren#234 bundle with no village reachable, so it journals to its durable outbox rather
#: than posting, matching the ~54 ms median that issue recorded. That is about 1% of the
#: budget. The margin is for a village that is slow, not for one that is down: an emitter
#: with nowhere to post writes to the outbox and returns, and telemetry must never be able
#: to hold a routine open.
HOOK_TIMEOUT_S = 5

#: Where the resident image bakes chronicle's emitter, and the path a container-placed
#: session's hooks run.
#:
#: Not ``/root/.claude/chronicle-emit.py``, which is the copy ``entrypoint.sh`` seeds for a
#: human running ``claude`` in the container by hand. That directory is a bind mount from
#: the host: declaring the copy inside it would put arbitrary code on every tool call
#: behind a path outside the image, which is the hole #206 closed, reopened from the other
#: side. This one is in the image layer, under no mount, and steward built it — which is
#: the whole difference between a settings document steward *declares* and one it inherits.
CONTAINER_EMITTER = "/opt/steward/chronicle-emit.py"

#: How a host tells steward where its emitter is, for local placement.
#:
#: Container placement needs no configuration because steward ships that filesystem and
#: can promise what is on it. A host is somebody else's machine: steward installs no
#: emitter there, and naming one that is not there is not a quiet miss but a dead session
#: — measured 2026-09-04, a ``--settings`` path that does not exist is ``Error: Settings
#: file not found``, exit 1, no run at all. So local placement declares nothing until an
#: operator names a file they know exists, and until they do a local session emits exactly
#: what it has emitted since #206: nothing per-session, and steward's own run-level
#: brackets as always.
SESSION_EMITTER_ENV = "STEWARD_SESSION_EMITTER"

#: The emitter's second target, and the one steward has to switch off by name.
#:
#: chronicle's emitter mirrors every event to ``http://127.0.0.1:8737`` unless this is set,
#: and *presence* is what decides: only an explicitly empty value turns it off, so an
#: environment that simply does not mention it gets the loopback POST. That default is a
#: laptop convenience — a developer's chronicle running beside the session — and the
#: resident image already bakes ``CHRONICLE_MIRROR=""`` to be rid of it, because inside a
#: container nothing is ever listening there and every hook would pay a refused connection
#: and write a breaker file for it.
#:
#: Local placement is the half of that steward does not ship, and until #264 it did not
#: matter: a local session had no hooks, so nothing mirrored anywhere. Now it does, so
#: steward makes the same choice the image makes — empty unless something said otherwise.
#: The case this is really about is not the wasted connection: it is a control plane
#: running on a machine that *does* have a chronicle dev server on 8737, where every
#: production session would quietly duplicate its events into somebody's scratch village.
CHRONICLE_MIRROR_ENV = "CHRONICLE_MIRROR"

#: Where the village is, and the secret that gets in. Both are read here rather than only
#: in :mod:`steward.deploy` because this module decides what a *session* is told, and
#: :func:`check_session_ingest` needs to compare the two. ``deploy`` imports these names
#: from here so there is one spelling of each; ``tests/test_runners.py`` holds them
#: together with :mod:`steward.events`' own pair.
CHRONICLE_URL_ENV = "CHRONICLE_URL"
CHRONICLE_TOKEN_ENV = "CHRONICLE_TOKEN"  # noqa: S105 — a variable name, not a credential

#: How long the emitter's own ``--status`` may take before doctor stops waiting. It reads
#: one small JSON file and prints one line; a probe that hangs is a report that hangs, and
#: this one is evidence attached to a warning rather than the warning itself.
EMITTER_STATUS_TIMEOUT_S = 5.0


def session_emitter(
    placement: Placement, *, inherited: Mapping[str, str] | None = None
) -> str | None:
    """Name the emitter script a session's hooks should run here, or ``None`` for silence.

    One question with two answers, because the two placements know different amounts about
    the filesystem the session lands in — see :data:`CONTAINER_EMITTER` and
    :data:`SESSION_EMITTER_ENV` for why each is the answer it is.

    ``inherited`` is keyword-only for the same reason :func:`session_environment`'s is, and
    it is the honest seam for the one caller that is not the launching process: ``steward
    doctor`` answers this question about a *scheduler* that may run under a different
    environment than the shell the report was typed into.
    """
    if placement.is_container:
        return CONTAINER_EMITTER
    source = os.environ if inherited is None else inherited
    return (source.get(SESSION_EMITTER_ENV) or "").strip() or None


def hook_settings(emitter: str) -> str:
    """Render the settings document steward hands a session, as the JSON the flag takes.

    A JSON *string* rather than a path, which the flag accepts either way (``--settings
    <file-or-json>``) and which was measured to fire hooks either way. The string is the
    better of the two here for three reasons, in the order they bite:

    - a file can be missing, and a missing one is a dead session rather than lost
      telemetry (:data:`SESSION_EMITTER_ENV`);
    - a file is a thing that exists between being written and being read, so a
      container-placed session's settings would have to be shipped in an image and every
      resident already running an older one would fail to launch rather than emit;
    - a file is a thing something else can rewrite, and the point of #264 is a document
      whose contents are steward's own words at the moment it launches the session.

    What it carries is *only* ``hooks``. ``permissions``, ``model`` and ``env`` are settings
    keys too, and every one of them is already a manifest dimension or a deliberate
    refusal: a settings document that could re-decide them would be the inheritance #206
    closed, wearing steward's name.

    Two things about the command, both about failing in the right direction. The path is
    shell-quoted, because the CLI runs a hook command through a shell and an emitter under
    a path with a space in it would otherwise be two words — a hook that "runs" and emits
    nothing. And ``|| true`` is not defensive noise: a hook that exits 2 is the CLI's
    *blocking* code, and ``python3 <missing file>`` exits exactly 2 (measured). Without the
    guard, an emitter path that is wrong — a resident on an image older than the one that
    baked it, an operator's typo in ``$STEWARD_SESSION_EMITTER`` — would not lose telemetry
    but deny every tool call the session made, fleet-wide. The emitter never blocks
    deliberately (it exits 0 on its own failures too), so nothing is being suppressed that
    was ever meant to be heard: this is the same trade ``entrypoint.sh`` already makes out
    loud, that a resident must not be taken down over telemetry.
    """
    step = {
        "type": "command",
        "command": f"python3 {shlex.quote(emitter)} || true",
        "timeout": HOOK_TIMEOUT_S,
    }
    hooks = {
        event: [{"matcher": "*", "hooks": [step]} if event in MATCHED_HOOKS else {"hooks": [step]}]
        for event in CHRONICLE_HOOKS
    }
    return json.dumps({"hooks": hooks}, separators=(",", ":"))


@dataclass(frozen=True, slots=True)
class RunRequest:
    """One headless session steward wants run."""

    prompt: str
    workdir: Path
    timeout_s: int
    #: Which tools this session may reach, straight off the manifest. Required, and with
    #: no default on purpose: a request built without one would be an unbounded session
    #: that nobody chose to make unbounded, which is the exact silence-as-a-grant this
    #: field exists to end. Forgetting it is a type error, not a quiet grant.
    tools: ToolGrant
    #: Directories this session may reach beyond :attr:`workdir`. Defaulted, unlike
    #: :attr:`tools`, because forgetting it grants nothing: the session stays inside the
    #: one directory it was always confined to.
    workspace: tuple[str, ...] = ()
    model: str | None = None
    env: Mapping[str, str] = field(default_factory=dict)
    workdir_fd: int | None = field(default=None, repr=False, compare=False)

    @property
    def execution_workdir(self) -> str:
        """Return the declared path exposed to command-template substitution."""
        return str(self.workdir)

    def key(self) -> str:
        """Return a stable digest of the request, so mock results are reproducible."""
        material = "\x00".join(
            [
                self.prompt,
                str(self.workdir),
                str(self.timeout_s),
                self.model or "",
                # Two requests that differ only in what the session may touch are two
                # different sessions, so they must not share a mock result.
                self.tools.describe(),
                "\x1f".join(self.workspace),
            ]
        )
        return hashlib.sha256(material.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class RunResult:
    """The uniform result of a session, whichever brain ran it.

    ``artifacts`` is best effort and often empty: most CLIs do not report what they
    wrote, and steward would rather claim nothing than claim a file it did not see.
    The session's own burrow emitter is what reports artifacts truthfully.
    """

    outcome: Outcome
    output: str = ""
    exit_status: int | None = None
    duration_s: float = 0.0
    input_tokens: int | None = None
    output_tokens: int | None = None
    cost_usd: float | None = None
    artifacts: tuple[str, ...] = ()
    error: str | None = None
    #: Whether :attr:`error` is the child's own stdout/stderr rather than steward's own
    #: diagnostic. When it is, :meth:`summary` refuses to return it: a session's output can
    #: carry a secret it printed, and only steward's own words (a timeout, a launch that
    #: failed, a pre-flight refusal) are safe to serialize into a village event.
    error_is_child: bool = False

    @property
    def ok(self) -> bool:
        """True only when the process exited zero."""
        return self.outcome is Outcome.OK

    def summary(self) -> str:
        """Return a classified one-line reason, safe to serialize into a village event.

        Steward's own diagnostics — a timeout, a launch that failed, a pre-flight refusal a
        caller built into ``error`` — are surfaced, because they are steward's words and
        they are the useful ones. But the child's own stdout or stderr never is: that text
        can carry a secret the session printed (an ``sk-ant-…`` key, a ``CHRONICLE_TOKEN=``),
        so when :attr:`error_is_child` is set the summary falls back to the *class* of the
        failure — an exit status, or ``"runner error"`` — and the raw text stays in the
        local log only, reachable through :attr:`error`.
        """
        if self.error and not self.error_is_child:
            return self.error
        if self.outcome is Outcome.TIMEOUT:
            return f"killed after {self.duration_s:.0f}s (timeout)"
        if self.exit_status is not None:
            return f"exit status {self.exit_status}"
        return "runner error"


class Runner(ABC):
    """The seam. One method, one truthful result, no exceptions for a failed run."""

    kind: ClassVar[str] = "abstract"

    #: Where this brain loads on-disk skills from, relative to the session's working
    #: directory — or ``None`` when it has no such loader and the prompt is the only
    #: way it hears about a skill. Steward owns the directory it names.
    skills_dir: ClassVar[str | None] = None

    def __init__(self, spec: RunnerSpec | None = None, placement: Placement | None = None) -> None:
        """Hold the manifest declaration and resolved placement this runner was built from."""
        self.spec = spec if spec is not None else RunnerSpec(kind="mock")
        self.placement = placement if placement is not None else LOCAL_PLACEMENT

    @abstractmethod
    def run(self, request: RunRequest) -> RunResult:
        """Run one session to completion and describe what happened."""

    def check(self) -> str | None:
        """Return why this runner cannot run, or ``None`` when it is ready.

        Called at validate/schedule time so a missing binary is a loud diagnostic in
        daylight rather than a silent failure at 7am.
        """
        return None

    def describe(self) -> str:
        """One line naming the brain and where it runs — both axes of one question."""
        model = self.spec.model or "default model"
        described = f"{self.kind} ({model})"
        if self.placement.is_container:
            described += f" in {self.placement.describe()}"
        return described


# --------------------------------------------------------------------------------------
# subprocess plumbing — the only place in steward that starts a process
# --------------------------------------------------------------------------------------


def substitute(template: Sequence[str], *, prompt: str, workdir: str) -> list[str]:
    """Fill ``{prompt}``/``{workdir}`` in an argv template, in one pass.

    One pass matters: a prompt containing the literal text ``{workdir}`` is inserted
    as data and never re-scanned, so no manifest and no model output can smuggle a
    second substitution in. Nothing here goes near a shell, so a prompt full of
    ``;``, ``$(…)`` or backticks is one ordinary argv element.
    """
    values = {"prompt": prompt, "workdir": workdir}
    return [_PLACEHOLDER.sub(lambda match: values[match.group(1)], part) for part in template]


def _terminate(process: subprocess.Popen[bytes]) -> None:
    """Kill a timed-out session, and its children, as firmly as the platform allows."""
    try:
        os.killpg(os.getpgid(process.pid), signal.SIGKILL)
    except OSError, AttributeError:
        process.kill()
    with contextlib.suppress(subprocess.TimeoutExpired):
        process.wait(timeout=KILL_GRACE_S)


def _drain(process: subprocess.Popen[bytes]) -> tuple[bytes, bytes]:
    """Read whatever a killed process already wrote, so a timeout keeps partial output.

    The first :meth:`~subprocess.Popen.communicate` raised its ``TimeoutExpired`` before
    it could bind stdout, which is how a timed-out session used to report an empty
    ``output`` and drop an escalation printed a moment before it hung. Now that the
    process is dead its pipes are at EOF, and a second call returns the bytes buffered
    before the kill. Never raises: draining a corpse is best effort.
    """
    try:
        return process.communicate(timeout=KILL_GRACE_S)
    except subprocess.TimeoutExpired, ValueError, OSError:
        return b"", b""


def _remaining(started: float, timeout_s: int) -> float:
    """Return the unspent part of one session's absolute timeout budget."""
    return max(0.0, started + timeout_s - time.monotonic())


def _duplicate_for_helper(fd: int) -> int:
    """Return an owned close-on-exec duplicate outside the standard-stream range."""
    return fcntl.fcntl(fd, fcntl.F_DUPFD_CLOEXEC, 3)


def _timeout_result(
    process: subprocess.Popen[bytes], *, started: float, timeout_s: int
) -> RunResult:
    """Kill and reap a run whose shared launch/execution deadline expired."""
    _terminate(process)
    duration = time.monotonic() - started
    raw_out, _raw_err = _drain(process)
    return RunResult(
        outcome=Outcome.TIMEOUT,
        output=raw_out.decode("utf-8", "replace")[:OUTPUT_MAX_CHARS],
        exit_status=process.returncode,
        duration_s=duration,
        error=f"exceeded its {timeout_s}s timeout and was killed",
    )


def _await_descriptor_launch(
    status_reader: int,
    process: subprocess.Popen[bytes],
    *,
    started: float,
    timeout_s: int,
    executable: str,
) -> RunResult | None:
    """Wait within the session deadline for the cwd helper to exec or diagnose failure."""
    with os.fdopen(status_reader, "rb") as status:
        ready, _, _ = select.select([status], [], [], _remaining(started, timeout_s))
        if not ready:
            return _timeout_result(process, started=started, timeout_s=timeout_s)
        helper_error = os.read(status.fileno(), 1000).decode("utf-8", "replace")
    if not helper_error:
        return None
    try:
        raw_out, _raw_err = process.communicate(timeout=_remaining(started, timeout_s))
    except subprocess.TimeoutExpired:
        return _timeout_result(process, started=started, timeout_s=timeout_s)
    return RunResult(
        outcome=Outcome.FAILED,
        output=raw_out.decode("utf-8", "replace")[:OUTPUT_MAX_CHARS],
        exit_status=process.returncode,
        duration_s=time.monotonic() - started,
        error=f"cannot launch {executable!r}: {helper_error}",
    )


#: Where a container-placed session's pid file lives inside the container. Written by the
#: launch shim, read and removed by the kill path, removed by the shim itself on an
#: ordinary exit — so in steady state the directory is empty.
_PID_DIR = "/run/steward"

#: What a pid file may be named after. ``STEWARD_RUN_ID`` is minted by steward, but the
#: name still crosses into a shell script, so anything that does not match is replaced by
#: a random token rather than trusted. No ``/`` in the class, so no name can climb out of
#: :data:`_PID_DIR`.
_SAFE_PID_NAME = re.compile(r"[A-Za-z0-9._-]{1,120}")


def _pid_file_path(env: Mapping[str, str]) -> str:
    """Name the in-container pid file for one session, keyed on its run id.

    The run id when the wake carries one — so an operator debugging a stuck container can
    match ``/run/steward/<run id>.pid`` to the ledger — and a random token otherwise. Every
    kind of wake does carry one (``STEWARD_RUN_ID``), so the fallback is for requests built
    outside the session lifecycle.
    """
    run_id = env.get("STEWARD_RUN_ID", "")
    name = run_id if _SAFE_PID_NAME.fullmatch(run_id) else secrets.token_hex(12)
    return f"{_PID_DIR}/{name}.pid"


def _container_shim(pid_path: str) -> str:
    """Return the ``sh -c`` script a container-placed session launches through.

    It records its own pid and then runs the brain as ``"$0" "$@"`` — the argv elements
    travel as data after the script, never inside it, so a prompt full of quotes cannot
    reopen the script. ``$$`` is the whole point: measured against docker 27, a process
    ``docker exec`` starts is the leader of its own process group and session, and every
    child the brain spawns stays in that group — so the recorded pid is exactly what
    ``kill -9 -<pid>`` needs to take the whole session down (:meth:`_kill_in_container`).

    The brain is *not* ``exec``-ed: this shell outlives it to remove the pid file, which
    keeps :data:`_PID_DIR` from accumulating one file per run in a directory no tmpfs
    clears. The exit status is carried out explicitly so the cleanup cannot launder a
    failed session into ``exit 0``, and a shim that could not even record its pid exits
    125 without running the brain at all — steward must never be unable to stop a session
    it started.
    """
    return (
        f'mkdir -p {_PID_DIR} && printf %s "$$" > {pid_path} || exit 125; '
        f'"$0" "$@"; status=$?; rm -f {pid_path}; exit $status'
    )


class _ProcessRunner(Runner):
    """Shared body for every runner that launches a real process."""

    #: The executable this runner needs on PATH. An instance attribute, because a
    #: ``command`` runner only learns its binary from the manifest template.
    binary: str = ""

    #: What this runner's brain needs from the launching environment, on top of
    #: :data:`SESSION_ENV_BASE`. Empty by default, so a new runner kind starts with the
    #: narrow environment and has to say what else it needs rather than inheriting the
    #: control plane's.
    env_names: ClassVar[tuple[str, ...]] = ()

    @abstractmethod
    def argv(self, request: RunRequest) -> list[str]:
        """Return the exact argv for this request. Never a shell string."""

    def parse(self, result: RunResult, stdout: str) -> RunResult:  # noqa: ARG002
        """Enrich a finished result from the CLI's own output. Default: unchanged."""
        return result

    def check(self) -> str | None:
        """Report why this runner cannot run: a missing binary, or a container that is not.

        For a container placement the local PATH is irrelevant and the two questions that
        matter are asked instead, reusing the exact probe :class:`~steward.watchdog.
        DockerSupervisor` health-checks with: is the container running, and is the brain
        on PATH *inside* it. A container-placed resident whose container is down must
        fail ``steward doctor`` and the scheduler's startup check, not its next fire.
        """
        if self.placement.is_container:
            return self._check_container(self.placement.container_name)
        if not self.binary:
            return None
        if shutil.which(self.binary) is None:
            return (
                f"runner kind {self.kind!r} needs the {self.binary!r} executable, "
                f"which is not on PATH"
            )
        return None

    def _check_container(self, container: str) -> str | None:
        """Ask docker, on this host, whether the named container can hold a session."""
        outcome = run_argv(["docker", "inspect", "--format", "{{.State.Running}}", container])
        if not outcome.ok:
            return (
                f"docker could not answer for container {container!r} on this host "
                f"({outcome.summary()}); a container-placed resident needs its container "
                f"where the scheduler runs"
            )
        if outcome.stdout.strip().lower() != "true":
            return (
                f"container {container!r} is not running, so this resident's sessions "
                f"have nowhere to happen; `docker compose up -d` in its deploy directory "
                f"brings it back"
            )
        if not self.binary:
            return None
        probe = run_argv(
            ["docker", "exec", container, "sh", "-c", 'command -v "$1"', "sh", self.binary]
        )
        if not probe.ok:
            return (
                f"runner kind {self.kind!r} needs the {self.binary!r} executable inside "
                f"container {container!r}, which is not on the container's PATH"
            )
        return None

    def environment(self, request: RunRequest) -> dict[str, str]:
        """Return this session's whole environment: the allowlist, then ``request.env``."""
        return session_environment(request, allowed=(*SESSION_ENV_BASE, *self.env_names))

    def run(self, request: RunRequest) -> RunResult:
        """Launch the session, bound by its timeout, and report what happened."""
        if self.placement.is_container:
            return self._run_in_container(request)
        started = time.monotonic()
        argv = self.argv(request)
        env = self.environment(request)
        workdir_fd = request.workdir_fd
        launch_argv = argv
        status_reader: int | None = None
        status_writer: int | None = None
        helper_fds: tuple[int, ...] = ()
        inherited_fds: tuple[int, ...] = ()
        try:
            if workdir_fd is not None:
                helper_workdir = _duplicate_for_helper(workdir_fd)
                helper_fds = (helper_workdir,)
                status_reader, raw_status_writer = os.pipe()
                try:
                    status_writer = _duplicate_for_helper(raw_status_writer)
                finally:
                    os.close(raw_status_writer)
                helper_fds += (status_writer,)
                inherited_fds = helper_fds
                launch_argv = [
                    sys.executable,
                    "-I",
                    "-S",
                    "-c",
                    _DESCRIPTOR_CWD_HELPER,
                    str(helper_workdir),
                    str(status_writer),
                    *argv,
                ]
            process = subprocess.Popen(  # noqa: S603 — argv + capability cwd
                launch_argv,
                cwd=(request.execution_workdir if request.workdir_fd is None else None),
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                start_new_session=True,
                pass_fds=inherited_fds,
            )
        except OSError as exc:
            if status_reader is not None:
                os.close(status_reader)
            for fd in helper_fds:
                os.close(fd)
            return RunResult(
                outcome=Outcome.FAILED,
                duration_s=time.monotonic() - started,
                error=f"cannot launch {argv[0]!r}: {exc.strerror or exc}",
            )

        for fd in helper_fds:
            os.close(fd)
        if status_reader is not None:
            launch_failure = _await_descriptor_launch(
                status_reader,
                process,
                started=started,
                timeout_s=request.timeout_s,
                executable=argv[0],
            )
            if launch_failure is not None:
                return launch_failure

        try:
            remaining = _remaining(started, request.timeout_s)
            raw_out, raw_err = process.communicate(timeout=remaining)
        except subprocess.TimeoutExpired:
            # The timeout result drains partial stdout too: an escalation printed just
            # before the hang is honest output and must remain available to its harvester.
            return _timeout_result(process, started=started, timeout_s=request.timeout_s)

        return self._conclude(process, raw_out, raw_err, started)

    def _conclude(
        self,
        process: subprocess.Popen[bytes],
        raw_out: bytes,
        raw_err: bytes,
        started: float,
    ) -> RunResult:
        """Turn one reaped process into the uniform result, whichever launcher ran it."""
        duration = time.monotonic() - started
        stdout = raw_out.decode("utf-8", "replace")
        stderr = raw_err.decode("utf-8", "replace")
        outcome = Outcome.OK if process.returncode == 0 else Outcome.FAILED
        result = RunResult(
            outcome=outcome,
            output=stdout[:OUTPUT_MAX_CHARS],
            exit_status=process.returncode,
            duration_s=duration,
            error=None if outcome is Outcome.OK else (stderr.strip() or stdout.strip())[:1000],
            # The failure reason above is the child's own stderr/stdout: keep it for the
            # local log, but never let summary() serialize it into a village event.
            error_is_child=outcome is not Outcome.OK,
        )
        return self.parse(result, stdout)

    def _run_in_container(self, request: RunRequest) -> RunResult:
        """Run the session inside the resident's own container, over ``docker exec``.

        The brain argv is exactly what :meth:`argv` builds — same flags, same
        ``--output-format json``, so :meth:`parse` still feeds tokens and cost into the
        budget ledger — wrapped in the pid-recording shim (:func:`_container_shim`) so a
        timeout can be a real kill inside the container, not just a dead local client.

        Two deliberate differences from local placement:

        - **The session sees only the named variables.** ``docker exec -e NAME`` — the
          bare name, whose value docker reads from the *client's* environment (measured
          against docker 27) — carries ``request.env``, the facts steward chose to tell
          this session, and nothing else: no :data:`SESSION_ENV_BASE`, no passthrough,
          no control-plane ``STEWARD_TOKEN``. By name and not ``NAME=value``, because
          the values include the per-run session credential and an argv is readable in
          any host ``ps`` for the life of the run. The container already has its own
          environment from its compose file and ``.env``, and the brain's credentials
          live on its ``/root/.claude`` volume; the compose ``.env`` is the operator's
          hatch here, where :data:`SESSION_ENV_PASSTHROUGH_ENV` is the local one.
        - **``request.workdir_fd`` is ignored.** The descriptor pins a directory in the
          control plane's mount namespace, which means nothing across ``docker exec``;
          the working directory inside the container is the placement's own
          (``docker exec -w``), and the control plane keeps using the descriptor for
          what happens on its side of the mount (skills, journal).

        The local ``docker`` client inherits the control plane's environment (plus the
        named values above, for ``-e`` to read) — it is a control-plane tool, like the
        nursery's ``ssh``, and needs its own ``DOCKER_HOST`` and config; none of that
        environment crosses into the container beyond the named variables.
        """
        started = time.monotonic()
        argv = self.argv(request)
        pid_path = _pid_file_path(request.env)
        launch_argv = ["docker", "exec"]
        if self.placement.workdir:
            launch_argv += ["-w", self.placement.workdir]
        for name in sorted(request.env):
            launch_argv += ["-e", name]
        launch_argv += [self.placement.container_name, "sh", "-c", _container_shim(pid_path)]
        launch_argv += argv
        try:
            process = subprocess.Popen(  # noqa: S603 — argv list, shell=False
                launch_argv,
                env={**os.environ, **request.env},
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                start_new_session=True,
            )
        except OSError as exc:
            return RunResult(
                outcome=Outcome.FAILED,
                duration_s=time.monotonic() - started,
                error=f"cannot launch 'docker': {exc.strerror or exc}",
            )
        try:
            raw_out, raw_err = process.communicate(timeout=_remaining(started, request.timeout_s))
        except subprocess.TimeoutExpired:
            # Inside first: killing the local client alone leaves the session running in
            # the container, burning tokens behind a ledger row that says timeout — the
            # exact lie this launcher exists to refuse (steward #58). Once the group is
            # dead in there, the client exits by itself and the usual reap drains the
            # partial output it had already streamed.
            undelivered = self._kill_in_container(pid_path)
            result = _timeout_result(process, started=started, timeout_s=request.timeout_s)
            if undelivered is not None:
                # The ledger row still says timeout — steward did give up on the run —
                # but the words must not claim a kill that may not have landed: a
                # session possibly still burning tokens behind a clean "was killed" is
                # the same lie in a different tense.
                return replace(
                    result,
                    error=(
                        f"{result.error}, but the kill inside container "
                        f"{self.placement.container_name!r} could not be delivered "
                        f"({undelivered}); the session may still be running there"
                    ),
                )
            return result
        return self._conclude(process, raw_out, raw_err, started)

    def _kill_in_container(self, pid_path: str) -> str | None:
        """Kill the timed-out session's whole process group inside the container.

        The group kill is the real one — the recorded pid leads its own group, so
        ``kill -9 -<pid>`` takes the brain and every child it spawned. The direct kill is
        a fallback for a pid that somehow stopped leading a group, and the ``rm`` keeps a
        dead run's pid file from shadowing a future one. A kill that finds no pid file or
        no such process is success — the session is already gone.

        Returns ``None`` when the kill command ran inside the container, and steward's
        own one-line reason when it could not be delivered at all (no docker, a wedged
        daemon, a container that stopped answering) — which the caller must surface,
        because a timeout whose kill never arrived is a session that may still be
        running.
        """
        script = (
            f'pid="$(cat {pid_path} 2>/dev/null)"; '
            f'if [ -n "$pid" ]; then '
            f'kill -9 -"$pid" 2>/dev/null || kill -9 "$pid" 2>/dev/null; fi; '
            f"rm -f {pid_path}"
        )
        outcome = run_argv(["docker", "exec", self.placement.container_name, "sh", "-c", script])
        if outcome.ok:
            return None
        log.warning(
            "could not deliver the kill inside container %s: %s",
            self.placement.container,
            outcome.summary(),
        )
        return outcome.summary()


class ClaudeRunner(_ProcessRunner):
    """``claude -p`` in headless mode, asked for JSON so usage and cost come back."""

    kind: ClassVar[str] = "claude"
    binary: str = "claude"
    env_names: ClassVar[tuple[str, ...]] = CLAUDE_ENV_NAMES
    #: Where a granted skill is written before the run. Not where the session finds it:
    #: since steward #206 every claude session is launched with ``--setting-sources ""``
    #: and ``.claude/skills`` is discovered through the *project* setting source, so the
    #: CLI no longer sees this directory. The prompt carries each skill's body and is the
    #: delivery path; this copy is a file a session with ``Read`` can open, and the thing
    #: a future design that restores discovery would build on.
    skills_dir: ClassVar[str | None] = ".claude/skills"

    def environment(self, request: RunRequest) -> dict[str, str]:
        """Return the allowlisted environment, plus the one default a declared hook needs.

        Only when this session actually carries hooks, and only for a name nothing else
        set: a session with no emitter runs no emitter, and an operator who deliberately
        exported a ``CHRONICLE_MIRROR`` (or a wake that names one) keeps it. See
        :data:`CHRONICLE_MIRROR_ENV` for why the *absence* of this variable is a decision
        rather than a non-answer.
        """
        env = super().environment(request)
        if session_emitter(self.placement) is not None:
            env.setdefault(CHRONICLE_MIRROR_ENV, "")
        return env

    def argv(self, request: RunRequest) -> list[str]:
        """Build the claude headless argv: prompt, model, JSON output, permissions, tools.

        The tool bound is compiled here and nowhere else — ``--tools`` with the declared
        names, and ``--strict-mcp-config`` beside it, always the pair. Measured against CLI
        2.1.247, in an empty directory, under ``env -i HOME PATH TERM``:

        - ``--allowed-tools Read`` and Bash still ran. It pre-approves permission *rules*
          and removes nothing, so a bound compiled to it would read as a boundary in the
          manifest and be inert at run time. It is not the mechanism, and
          ``docs/manifest.md`` records that by name so it is not reached for later.
        - ``--tools Read`` and Bash was gone — but the session still listed
          ``mcp__spell__spell_search``, an MCP tool from the *calling machine's* config that
          steward never declared. On its own, ``--tools`` bounds the built-in set and leaks
          the host's servers straight through.
        - ``--tools Read --strict-mcp-config`` and the session had exactly ``Read``. That
          pair is the enforcement, which is why neither flag is emitted without the other.

        Removal is independent of ``--permission-mode``: ``--tools Read --permission-mode
        acceptEdits`` still had no Bash. A permissive mode cannot hand back a tool this
        argv took away.

        ``unrestricted`` emits neither flag, so an unbounded resident's tool argv is byte
        for byte what it was before that field existed. An empty list emits ``--tools ""``,
        which the CLI documents — and which measured out — as *no tools at all*.

        ``--setting-sources ""`` goes on every claude session, bounded or not, and is the
        one flag here that no manifest asks for (steward #206). Measured 2026-08-31, in
        ``docs/settings-sources.md``: a settings file at any of the three sources registers
        a ``SessionStart`` hook that runs, and sets ``permissions.defaultMode``, and neither
        is gated by the workspace trust flag that was the only thing standing in the way.
        A session's working directory *is* the resident's memory directory, so ``project``
        and ``local`` are files under the constrained session's own hand; ``user`` is
        whatever the launching machine happens to hold. None of the three is a source a
        resident should read, so none of them is named.

        ``--settings`` is the other half of that statement and goes on beside it whenever
        this placement has an emitter to name (steward #264): closing the sources also
        switched off the per-session chronicle hooks that were riding on them, and this is
        steward declaring those six hooks itself rather than inheriting six from a file
        somebody else owns. The resident writes nothing; steward declares everything. See
        :func:`hook_settings` for what the document may carry and :func:`session_emitter`
        for why a local placement often names nothing at all.
        """
        argv = [self.binary, "-p", request.prompt, "--output-format", "json", *SETTING_SOURCES]
        emitter = session_emitter(self.placement)
        if emitter is not None:
            argv += [SETTINGS_FLAG, hook_settings(emitter)]
        model = request.model or self.spec.model
        if model:
            argv += ["--model", model]
        if self.spec.permission_mode:
            argv += ["--permission-mode", self.spec.permission_mode]
        bound = request.tools.bound
        if bound is not None:
            tools_flag, strict_flag = TOOL_BOUND_FLAGS
            argv += [tools_flag, ",".join(bound), strict_flag]
        # One flag per directory rather than the variadic spelling `--add-dir a b`: a
        # variadic option followed by another flag is a parser question steward does not
        # need to have an opinion about. Measured: repeating it accumulates, and a session
        # given two directories read a file in each.
        for directory in request.workspace:
            argv += [WORKSPACE_FLAG, directory]
        return argv

    def parse(self, result: RunResult, stdout: str) -> RunResult:
        """Pull text, usage, and cost out of ``--output-format json`` when present."""
        payload = _load_json_object(stdout)
        if payload is None:
            return result
        usage = payload.get("usage")
        usage = usage if isinstance(usage, Mapping) else {}
        text = payload.get("result")
        is_error = bool(payload.get("is_error"))
        reported = (
            payload.get("total_cost_usd"),
            usage.get("input_tokens"),
            usage.get("output_tokens"),
        )
        believed: tuple[float | None, int | None, int | None] = (
            _as_float(reported[0]),
            _as_int(reported[1]),
            _as_int(reported[2]),
        )
        # One number steward will not believe discredits the report it came in, not just
        # itself. Keeping the others would leave the run flagged *usage-known* while the
        # money is recorded as zero — a green gauge over real spend, which is the whole
        # failure this bound exists to prevent, and exactly the shape of steward #125.
        # ``usage_known`` is one flag for all three (:meth:`BudgetGuard.record`), so the
        # honest way to say "steward did not learn what this cost" is to say it about the
        # whole report (steward #129).
        if any(_refused(said, took) for said, took in zip(reported, believed, strict=True)):
            believed = (None, None, None)
        cost_usd, input_tokens, output_tokens = believed
        return replace(
            result,
            output=text[:OUTPUT_MAX_CHARS] if isinstance(text, str) else result.output,
            outcome=Outcome.FAILED if is_error else result.outcome,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cost_usd=cost_usd,
            error=(str(text)[:1000] if is_error and isinstance(text, str) else result.error),
            # A model that reports its own error writes that text itself, so it is child
            # output too: kept for the local log, kept out of any event.
            error_is_child=(True if is_error and isinstance(text, str) else result.error_is_child),
        )


class CodexRunner(_ProcessRunner):
    """``codex exec`` — the same seam, a different brain."""

    kind: ClassVar[str] = "codex"
    binary: str = "codex"
    env_names: ClassVar[tuple[str, ...]] = CODEX_ENV_NAMES

    def argv(self, request: RunRequest) -> list[str]:
        """Build the codex headless argv."""
        argv = [self.binary, "exec"]
        model = request.model or self.spec.model
        if model:
            argv += ["--model", model]
        argv.append(request.prompt)
        return argv


class CommandRunner(_ProcessRunner):
    """An arbitrary argv template from the manifest, for anything else."""

    kind: ClassVar[str] = "command"

    def __init__(self, spec: RunnerSpec | None = None, placement: Placement | None = None) -> None:
        """Refuse to exist without the template the manifest promised."""
        super().__init__(spec, placement)
        if not self.spec.command:
            raise RunnerError("runner kind 'command' requires a command template")
        self.template: tuple[str, ...] = tuple(self.spec.command)
        self.binary = self.template[0]

    def argv(self, request: RunRequest) -> list[str]:
        """Substitute the two allowed placeholders; everything else stays literal."""
        return substitute(self.template, prompt=request.prompt, workdir=request.execution_workdir)

    def check(self) -> str | None:
        """Report a template whose executable is not on PATH."""
        if shutil.which(self.binary) is None:
            return f"runner command template starts with {self.binary!r}, which is not on PATH"
        return None

    def describe(self) -> str:
        """Name the command, since 'command' alone says nothing useful."""
        return f"command ({' '.join(self.template)})"


class MockRunner(Runner):
    """Deterministic, offline, and injectable. Tests and ``--dry-run`` use this.

    The same request always produces the same result, because the output is derived
    from a digest of the request. Pass ``behavior`` to make a run fail, time out, or
    block — a scheduler test needs all three.
    """

    kind: ClassVar[str] = "mock"

    def __init__(
        self,
        spec: RunnerSpec | None = None,
        placement: Placement | None = None,
        *,
        behavior: Callable[[RunRequest], RunResult] | None = None,
    ) -> None:
        """Optionally take a behavior that decides each result."""
        super().__init__(spec, placement)
        self.behavior = behavior
        self.requests: list[RunRequest] = []

    def run(self, request: RunRequest) -> RunResult:
        """Return the injected result, or a deterministic successful one."""
        self.requests.append(request)
        if self.behavior is not None:
            return self.behavior(request)
        digest = request.key()[:12]
        return RunResult(
            outcome=Outcome.OK,
            output=f"[mock {self.spec.model or 'model'}] {digest}",
            exit_status=0,
            duration_s=0.0,
        )


# --------------------------------------------------------------------------------------
# short bounded commands — not sessions, but still processes, so still here
# --------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class CommandOutcome:
    """What one short command did. A missing binary is a result, not an exception."""

    argv: tuple[str, ...]
    exit_status: int | None = None
    stdout: str = ""
    stderr: str = ""
    error: str | None = None

    @property
    def ok(self) -> bool:
        """True only when the command ran and exited zero."""
        return self.exit_status == 0

    def summary(self) -> str:
        """Return a classified one-line reason, safe to serialize into a village event.

        Like :meth:`RunResult.summary`, this never carries the command's own output. The
        watchdog turns a command's summary into a ``resident_restarted`` reason and a
        ``needs_human`` detail, both of which reach the village, so ``stdout``/``stderr``
        stay on the outcome for a local log and only the *class* of the result crosses the
        wire. ``error`` is steward's own diagnostic (a missing binary, a hang), not the
        command's, so it is safe to keep.
        """
        if self.error:
            return self.error
        return f"exit status {self.exit_status}"


#: How the watchdog reaches a command. Injectable so its tests never need a real docker.
type CommandRun = Callable[[Sequence[str]], CommandOutcome]

#: The same seam for callers that also feed the command something on stdin — the nursery
#: pipes a tar archive into ``ssh … tar -xf -``, because UGOS's ``scp`` is broken and a
#: pipe is the transport that actually works against the NAS. Loosely typed on purpose:
#: a fake in a test may take ``(argv)`` and ignore the rest.
type PipedRun = Callable[..., CommandOutcome]

#: How long steward waits on a command that is moving bytes rather than answering a
#: question. A tar of a resident's bundle over the tailnet is small, but it is not
#: instant, and killing it at the control-plane timeout would leave a half-written
#: directory on the far side.
TRANSFER_TIMEOUT_S = 120.0


def run_argv(
    argv: Sequence[str],
    timeout_s: float = COMMAND_TIMEOUT_S,
    *,
    stdin: bytes | None = None,
) -> CommandOutcome:
    """Run one short command to completion and describe what happened. Never raises.

    An argv list and ``shell=False``, exactly like a session: the container name comes out
    of a manifest, and a manifest is data. A missing binary, a non-zero exit, and a hang
    are all reported as outcomes, because the caller is a watchdog and a watchdog that
    crashes is worse than the thing it was watching.

    ``stdin`` feeds the process a fixed payload and closes the pipe. It exists for exactly
    one caller — the nursery piping a tar archive into ``tar -xf -`` on the far side of an
    ``ssh`` — and it is bytes rather than a stream because steward only ever sends
    something it has already finished building.
    """
    parts = [str(part) for part in argv]
    try:
        process = subprocess.Popen(  # noqa: S603 — argv list, shell=False, no template
            parts,
            stdin=subprocess.PIPE if stdin is not None else subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            # A separate group lets timeout cleanup reach ssh and the local processes it
            # may have spawned. Windows has no POSIX session/process-group contract.
            start_new_session=os.name == "posix",
        )
    except OSError as exc:
        return CommandOutcome(
            argv=tuple(parts), error=f"cannot launch {parts[0]!r}: {exc.strerror or exc}"
        )
    try:
        stdout, stderr = process.communicate(input=stdin, timeout=timeout_s)
    except subprocess.TimeoutExpired:
        _terminate(process)
        stdout, stderr = _drain(process)
        return CommandOutcome(
            argv=tuple(parts),
            stdout=stdout.decode("utf-8", "replace")[:OUTPUT_MAX_CHARS],
            stderr=stderr.decode("utf-8", "replace")[:OUTPUT_MAX_CHARS],
            error=f"{parts[0]!r} did not answer within {timeout_s:.0f}s",
        )
    return CommandOutcome(
        argv=tuple(parts),
        exit_status=process.returncode,
        stdout=stdout.decode("utf-8", "replace")[:OUTPUT_MAX_CHARS],
        stderr=stderr.decode("utf-8", "replace")[:OUTPUT_MAX_CHARS],
    )


# --------------------------------------------------------------------------------------
# factory
# --------------------------------------------------------------------------------------

RUNNER_KINDS: Mapping[str, type[Runner]] = {
    ClaudeRunner.kind: ClaudeRunner,
    CodexRunner.kind: CodexRunner,
    CommandRunner.kind: CommandRunner,
    MockRunner.kind: MockRunner,
}


def build_runner(
    spec: RunnerSpec, placement: Placement | None = None, *, force_mock: bool = False
) -> Runner:
    """Build the runner a manifest declares, placed where its manifest says it runs.

    ``force_mock`` is how ``--dry-run`` keeps its promise: a rehearsal must not be
    able to reach a real brain, whatever the manifest says — nor a real container,
    which is why the mock is built before placement is even looked at.

    The container refusal mirrors :func:`steward.manifest._check_placement` (one set,
    :data:`~steward.manifest.UNPLACEABLE_RUNNER_KINDS`, read by both): validation is the
    daylight diagnostic, this is the seam refusing to build what validation would have
    refused to admit, for specs that never crossed a validator.
    """
    resolved = placement if placement is not None else LOCAL_PLACEMENT
    if force_mock:
        return MockRunner(spec)
    if resolved.is_container and spec.kind in UNPLACEABLE_RUNNER_KINDS:
        raise RunnerError(
            f"runner kind {spec.kind!r} cannot be placed in a container; "
            f"placement 'container' needs a kind that runs a real brain argv"
        )
    try:
        factory = RUNNER_KINDS[spec.kind]
    except KeyError:
        known = ", ".join(sorted(RUNNER_KINDS))
        raise RunnerError(f"unknown runner kind {spec.kind!r}; known kinds: {known}") from None
    return factory(spec, resolved)


def skills_home(spec: RunnerSpec) -> str | None:
    """Return where this runner kind wants on-disk skills written, or ``None``.

    Answered from the class rather than an instance, so asking "does this brain take a
    copy on disk" never builds a runner, let alone launches one.

    *Wants written*, not *reads*: since steward #206 a claude session loads no setting
    sources and does not discover ``.claude/skills`` (see :attr:`ClaudeRunner.skills_dir`).
    """
    runner = RUNNER_KINDS.get(spec.kind)
    return getattr(runner, "skills_dir", None) if runner is not None else None


def check_runner(spec: RunnerSpec, placement: Placement | None = None) -> str | None:
    """Return why a declared runner cannot run, or ``None``. Never launches a session.

    A container placement makes this *does-it-answer* probe ask docker instead of PATH —
    which is a process, like :func:`check_cli_support`'s, and lands here for the same
    reason: steward starts processes in exactly one file.
    """
    try:
        return build_runner(spec, placement).check()
    except RunnerError as exc:
        return str(exc)


def _cli_help(binary: str, placement: Placement) -> str | None:
    """Return ``<binary> --help``, or ``None`` when the binary would not answer.

    Asked *where the sessions run*: on this host's PATH for a local placement, and inside
    the container for a container one — which is the whole point of the probe, since the
    CLI a provisioned container carries is pinned by its image, not by the laptop that
    validated the manifest.

    Deliberately not cached. The answer is a property of the installed CLI rather than of
    the resident asking, so a cache looks free — but the only caller is ``steward doctor``,
    which asks once per *bounded* resident and then exits, and a module-global cache over a
    PATH lookup is a stale answer waiting to be given to somebody who just upgraded.
    """
    argv = (
        ["docker", "exec", placement.container_name, binary, "--help"]
        if placement.is_container
        else [binary, "--help"]
    )
    outcome = run_argv(argv)
    if not outcome.ok:
        return None
    return outcome.stdout + outcome.stderr


def required_flags(
    spec: RunnerSpec,
    tools: ToolGrant,
    workspace: Sequence[str],
    placement: Placement | None = None,
) -> tuple[str, ...]:
    """Return the CLI flags a session for this manifest is launched with, in argv order.

    Empty for a kind that compiles none — which, for both declarations, is a kind
    validation refuses to pair with the declaration in the first place.

    Never empty for ``claude``, though nothing may be declared at all:
    :data:`SETTING_SOURCES` is on every claude argv rather than compiled from a
    declaration, so every claude resident has something the installed binary has to
    support.

    ``placement`` is here because :data:`SETTINGS_FLAG` is not a property of the manifest
    either: whether a session carries one is :func:`session_emitter`'s answer, and a probe
    that asked for it unconditionally would fail a perfectly good local host that names no
    emitter and therefore never sends the flag.
    """
    if spec.kind != ClaudeRunner.kind:
        return ()
    setting_sources_flag, _value = SETTING_SOURCES
    flags: list[str] = [setting_sources_flag]
    if session_emitter(placement if placement is not None else LOCAL_PLACEMENT) is not None:
        flags.append(SETTINGS_FLAG)
    if not tools.unrestricted:
        flags.extend(TOOL_BOUND_FLAGS)
    if workspace:
        flags.append(WORKSPACE_FLAG)
    return tuple(flags)


def check_session_emitter(placement: Placement) -> str | None:
    """Return why this placement's declared emitter cannot run, or ``None``.

    Only container placement is answerable here, and only container placement needs
    answering: steward names :data:`CONTAINER_EMITTER` in a container it did not
    necessarily build this version of, and a resident still running an image from before
    warren#361 carries the emitter under its pre-rename name. For local placement the path
    came from an operator who can see their own filesystem, and steward has no second
    opinion worth printing.

    This exists because the failure it catches is invisible by construction. The hook
    command ends in ``|| true`` so that a missing emitter costs telemetry rather than
    denying every tool call (:func:`hook_settings`) — which means a resident with the wrong
    path looks perfectly healthy, runs perfectly well, and tells the village nothing. Doctor
    is the one place that can say so, and a report that asserted the channel was live
    without ever looking would be the most confident wrong line in it.
    """
    emitter = session_emitter(placement)
    if emitter is None or not placement.is_container:
        return None
    outcome = run_argv(["docker", "exec", placement.container_name, "test", "-f", emitter])
    if outcome.ok:
        return None
    return (
        f"{emitter} is not in {placement.container_name} — this resident's sessions run "
        f"and emit nothing; re-ship the image and re-provision"
    )


def check_session_ingest(
    placement: Placement, *, inherited: Mapping[str, str] | None = None
) -> str | None:
    """Return why a local session's hook events cannot be *delivered*, or ``None``.

    :func:`check_session_emitter` asks whether the hooks fire. This asks the second
    question, which #264 left open and warren#449 measured: a hook that fires still has to
    post, and the emitter authenticates with ``CHRONICLE_TOKEN``.

    Only **local** placement is answerable and only local placement needs answering.
    ``docker exec`` runs a container-placed session in the container's own environment, and
    ``render_compose`` writes both ``CHRONICLE_URL`` and ``CHRONICLE_TOKEN`` into every
    resident's service, so a provisioned resident's hooks are already authenticated
    (measured 2026-09-04 against docker 27.3.1). A local session gets
    :data:`SESSION_ENV_BASE`, which carries the URL and deliberately not the token
    (:data:`SESSION_ENV_REFUSED`'s comment argues why).

    The condition is a comparison of two things this host already knows, so there is no
    probe and no guess about somebody else's server: **this host holds an ingest token**
    (so the village it points at wants one — it is the same token ``deploy`` writes into
    every resident's ``.env`` and the same one steward's own emitter posts with), and
    **the session will not inherit it**. Both, and every per-session event that resident
    fires is a 401.

    That failure is the worst-shaped one there is, which is why it is worth a function: a
    401 is just another failed POST to the emitter, so the event is journaled rather than
    lost — to ``~/.chronicle/events.jsonl``, the hook emitter's own outbox, which nothing
    on the control plane drains (``steward events flush`` drains steward's *own* emitter's
    ``events.jsonl.pending``, a different file). The hooks fire, the outbox grows, the
    village stays empty, and nothing anywhere says why. This is the somewhere that says
    why.

    ``inherited`` is the same seam :func:`session_emitter` takes, and for the same reason:
    doctor is reporting on the environment the *scheduler* will run in.
    """
    emitter = session_emitter(placement, inherited=inherited)
    if emitter is None or placement.is_container:
        return None
    source = os.environ if inherited is None else inherited
    if not (source.get(CHRONICLE_TOKEN_ENV) or "").strip():
        return None
    forwarded, _ = passthrough_names(source)
    if CHRONICLE_TOKEN_ENV in (*SESSION_ENV_BASE, *ClaudeRunner.env_names, *forwarded):
        return None
    village = (source.get(CHRONICLE_URL_ENV) or "").strip() or "the village"
    return (
        f"{CHRONICLE_TOKEN_ENV} is set here but no session may inherit it, so every event "
        f"this resident's hooks post to {village} is rejected 401 and journaled to the "
        f"emitter's own ~/.chronicle/events.jsonl — an outbox nothing drains, because "
        f"`steward events flush` drains steward's own queue, a different file. Place this "
        f"resident in a container, where its compose environment carries the token, or "
        f"name {CHRONICLE_TOKEN_ENV} in ${SESSION_ENV_PASSTHROUGH_ENV} and accept that "
        f"every session then holds a secret that can post as any agent_id"
    )


def session_emitter_outbox(
    placement: Placement, *, inherited: Mapping[str, str] | None = None
) -> str | None:
    """Return the hook emitter's own one-line outbox reading, or ``None`` when there is none.

    Evidence for :func:`check_session_ingest`, not a check of its own. The complaint above
    is an inference from two variables; this is the outbox itself saying how many events
    are queued and when one was last acknowledged, which is the difference between "your
    sessions would 401" and "your sessions have been 401ing for three days".

    Local placement only, and for the same reason the complaint is: the file it reads lives
    under the account this process runs as, which for a local session is also the account
    the session runs as. Asking a container for its outbox would be asking a different
    machine a question doctor has no line to print the answer on.

    Never raises and never fails the report. The emitter ships ``--status`` precisely so an
    operator can read this cheaply (it opens one JSON file), but an emitter path that does
    not resolve, an old emitter without the flag, and a hang are all just "no reading" here
    — the complaint stands on its own without it.
    """
    emitter = session_emitter(placement, inherited=inherited)
    if emitter is None or placement.is_container:
        return None
    outcome = run_argv(["python3", emitter, "--status"], timeout_s=EMITTER_STATUS_TIMEOUT_S)
    if not outcome.ok:
        return None
    for line in outcome.stdout.splitlines():
        if line.strip():
            return line.strip()
    return None


def check_cli_support(
    spec: RunnerSpec,
    tools: ToolGrant,
    workspace: Sequence[str],
    placement: Placement | None = None,
) -> str | None:
    """Return why the *installed* brain cannot honour what this manifest declares, or ``None``.

    The CLI is the part of the boundary steward does not ship, and a manifest that declares
    a bound is perfectly valid against a ``claude`` too old to have the flag. That version
    does not quietly ignore it — measured: an unknown option is ``error: unknown option``
    and exit 1 — so the failure is loud. It is loud *at the resident's next fire*, which for
    the 07:00 routine means a failed session in a ledger nobody is reading, over a manifest
    that validated clean. Probing the flag here moves that from 7am to daylight, which is
    the whole of what this function buys.

    It matters most where it is least visible: a provisioned resident installs its own CLI
    from a hand-written bootstrap in the image, pinned by ``CLAUDE_VERSION`` in the
    Makefile, so the version running a manifest on the NAS is not the one on the laptop
    that validated it.

    Unlike :func:`check_runner` this *does* start a process — ``<binary> --help``, which is
    not a session and not a brain, and lands in this module for the same reason
    :func:`run_argv` does: steward starts processes in exactly one file.
    """
    resolved = placement if placement is not None else LOCAL_PLACEMENT
    needed = required_flags(spec, tools, workspace, resolved)
    if not needed:
        return None
    binary = ClaudeRunner.binary
    help_text = _cli_help(binary, resolved)
    if help_text is None:
        where = f" inside {resolved.describe()}" if resolved.is_container else ""
        return (
            f"cannot ask {binary!r}{where} what it supports, "
            f"so what this manifest declares is unproven"
        )
    missing = [flag for flag in needed if flag not in help_text]
    if missing:
        return (
            f"the installed {binary!r} does not support {', '.join(missing)}, so a session "
            f"for this resident would fail to launch rather than run without what "
            f"steward declares for it"
        )
    return None


# --------------------------------------------------------------------------------------
# small parsing helpers
# --------------------------------------------------------------------------------------


def _load_json_object(text: str) -> Mapping[str, Any] | None:
    """Parse the CLI's stdout as a JSON object, tolerating trailing noise."""
    stripped = text.strip()
    if not stripped:
        return None
    try:
        loaded = json.loads(stripped)
    except ValueError:
        return None
    if isinstance(loaded, list) and loaded and isinstance(loaded[-1], Mapping):
        return loaded[-1]
    return loaded if isinstance(loaded, Mapping) else None


def _as_int(value: object) -> int | None:
    """Return a token count steward is willing to believe, else ``None``.

    A count that is negative or past :data:`TOKENS_MAX` is refused rather than clamped:
    steward would rather record usage as *unknown* — which the ledger already has a word
    for, and which ``Spend.unreported`` already counts — than write into the gauge a
    number it does not believe.
    """
    if not isinstance(value, int) or isinstance(value, bool):
        return None
    if value < 0 or value > TOKENS_MAX:
        log.warning("ignoring an implausible token count reported by the runner: %r", value)
        return None
    return value


def _as_float(value: object) -> float | None:
    """Return a cost steward is willing to believe, else ``None``.

    The type gate is not enough on its own. ``json.loads`` accepts the non-standard
    literals ``NaN``, ``Infinity`` and ``-Infinity``, and every one of them survives
    ``isinstance(value, float)`` and then poisons the ``SUM`` the daily cap is read from.
    So finiteness, sign and magnitude are checked here, at the boundary where the number
    crosses out of the child process, rather than trusted downstream (steward #129).
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        number = float(value)
    except OverflowError:
        # A JSON integer literal past ~1e308. ``json`` hands those back as a Python int of
        # unbounded width, so it clears the isinstance gate above and then cannot be made
        # into a float at all. Refused like any other number steward will not believe,
        # rather than raised: this is a value on a result, not a reason to lose the run.
        log.warning("ignoring a cost too large to represent: %r", value)
        return None
    if not math.isfinite(number) or number < 0.0 or number > COST_USD_MAX:
        log.warning("ignoring an implausible cost reported by the runner: %r", value)
        return None
    return number


def _refused(reported: object, believed: object) -> bool:
    """Report whether the child said something here that steward would not take."""
    return reported is not None and believed is None
