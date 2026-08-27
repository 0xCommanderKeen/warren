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

One more thing lives here, and it is not a session: :func:`run_argv`, a short bounded
command whose exit status is the whole answer. The watchdog needs it to ask ``docker``
whether a container is running and to restart it when it is not. It is in this module
rather than that one because the rule this repo enforces is *steward starts processes in
exactly one file* — a rule worth keeping even for the processes that are not brains.
"""

import contextlib
import hashlib
import json
import logging
import math
import os
import re
import select
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

from steward.manifest import Runner as RunnerSpec

__all__ = [
    "COST_USD_MAX",
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
    "RunRequest",
    "RunResult",
    "Runner",
    "RunnerError",
    "build_runner",
    "check_runner",
    "run_argv",
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
    """Raised when a runner cannot be built from a manifest declaration."""


@dataclass(frozen=True, slots=True)
class RunRequest:
    """One headless session steward wants run."""

    prompt: str
    workdir: Path
    timeout_s: int
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
            [self.prompt, str(self.workdir), str(self.timeout_s), self.model or ""]
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
        can carry a secret the session printed (an ``sk-ant-…`` key, a ``BURROW_TOKEN=``),
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

    def __init__(self, spec: RunnerSpec | None = None) -> None:
        """Hold the manifest declaration this runner was built from."""
        self.spec = spec if spec is not None else RunnerSpec(kind="mock")

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
        """One line naming the brain in use — 'which model is Hob on' is answerable."""
        model = self.spec.model or "default model"
        return f"{self.kind} ({model})"


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


class _ProcessRunner(Runner):
    """Shared body for every runner that launches a real process."""

    #: The executable this runner needs on PATH. An instance attribute, because a
    #: ``command`` runner only learns its binary from the manifest template.
    binary: str = ""

    @abstractmethod
    def argv(self, request: RunRequest) -> list[str]:
        """Return the exact argv for this request. Never a shell string."""

    def parse(self, result: RunResult, stdout: str) -> RunResult:  # noqa: ARG002
        """Enrich a finished result from the CLI's own output. Default: unchanged."""
        return result

    def check(self) -> str | None:
        """Report a missing binary by name, with the runner kind that wants it."""
        if not self.binary:
            return None
        if shutil.which(self.binary) is None:
            return (
                f"runner kind {self.kind!r} needs the {self.binary!r} executable, "
                f"which is not on PATH"
            )
        return None

    def run(self, request: RunRequest) -> RunResult:
        """Launch the session, bound by its timeout, and report what happened."""
        argv = self.argv(request)
        env = {**os.environ, **request.env}
        workdir_fd = request.workdir_fd
        launch_argv = argv
        status_reader: int | None = None
        status_writer: int | None = None
        inherited_fds: tuple[int, ...] = ()
        if workdir_fd is not None:
            status_reader, status_writer = os.pipe()
            inherited_fds = (workdir_fd, status_writer)
            launch_argv = [
                sys.executable,
                "-I",
                "-S",
                "-c",
                _DESCRIPTOR_CWD_HELPER,
                str(workdir_fd),
                str(status_writer),
                *argv,
            ]
        started = time.monotonic()
        try:
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
            if status_writer is not None:
                os.close(status_writer)
            return RunResult(
                outcome=Outcome.FAILED,
                duration_s=time.monotonic() - started,
                error=f"cannot launch {argv[0]!r}: {exc.strerror or exc}",
            )

        if status_writer is not None:
            os.close(status_writer)
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


class ClaudeRunner(_ProcessRunner):
    """``claude -p`` in headless mode, asked for JSON so usage and cost come back."""

    kind: ClassVar[str] = "claude"
    binary: str = "claude"
    #: ``claude`` discovers skills under the working directory, so a granted skill is
    #: both injected into the prompt and written here before the run.
    skills_dir: ClassVar[str | None] = ".claude/skills"

    def argv(self, request: RunRequest) -> list[str]:
        """Build the claude headless argv: prompt, model, JSON output, permissions."""
        argv = [self.binary, "-p", request.prompt, "--output-format", "json"]
        model = request.model or self.spec.model
        if model:
            argv += ["--model", model]
        if self.spec.permission_mode:
            argv += ["--permission-mode", self.spec.permission_mode]
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

    def __init__(self, spec: RunnerSpec | None = None) -> None:
        """Refuse to exist without the template the manifest promised."""
        super().__init__(spec)
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
        *,
        behavior: Callable[[RunRequest], RunResult] | None = None,
    ) -> None:
        """Optionally take a behavior that decides each result."""
        super().__init__(spec)
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
        completed = subprocess.run(  # noqa: S603 — argv list, shell=False, no template
            parts, input=stdin, capture_output=True, timeout=timeout_s, check=False
        )
    except OSError as exc:
        return CommandOutcome(
            argv=tuple(parts), error=f"cannot launch {parts[0]!r}: {exc.strerror or exc}"
        )
    except subprocess.TimeoutExpired:
        return CommandOutcome(
            argv=tuple(parts), error=f"{parts[0]!r} did not answer within {timeout_s:.0f}s"
        )
    return CommandOutcome(
        argv=tuple(parts),
        exit_status=completed.returncode,
        stdout=completed.stdout.decode("utf-8", "replace")[:OUTPUT_MAX_CHARS],
        stderr=completed.stderr.decode("utf-8", "replace")[:OUTPUT_MAX_CHARS],
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


def build_runner(spec: RunnerSpec, *, force_mock: bool = False) -> Runner:
    """Build the runner a manifest declares.

    ``force_mock`` is how ``--dry-run`` keeps its promise: a rehearsal must not be
    able to reach a real brain, whatever the manifest says.
    """
    if force_mock:
        return MockRunner(spec)
    try:
        factory = RUNNER_KINDS[spec.kind]
    except KeyError:
        known = ", ".join(sorted(RUNNER_KINDS))
        raise RunnerError(f"unknown runner kind {spec.kind!r}; known kinds: {known}") from None
    return factory(spec)


def skills_home(spec: RunnerSpec) -> str | None:
    """Return where this runner kind loads on-disk skills from, or ``None``.

    Answered from the class rather than an instance, so asking "does this brain read
    skills off disk" never builds a runner, let alone launches one.
    """
    runner = RUNNER_KINDS.get(spec.kind)
    return getattr(runner, "skills_dir", None) if runner is not None else None


def check_runner(spec: RunnerSpec) -> str | None:
    """Return why a declared runner cannot run, or ``None``. Never launches anything."""
    try:
        return build_runner(spec).check()
    except RunnerError as exc:
        return str(exc)


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
