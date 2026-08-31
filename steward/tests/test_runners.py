"""The runner seam: what each brain is actually told, and who may spawn one."""

import inspect
import json
import os
import re
import subprocess
import sys
import threading
import time
from collections.abc import Mapping
from dataclasses import replace
from pathlib import Path
from typing import Literal

import pytest

from conftest import StubWriter
from steward import runners as r
from steward.manifest import PermissionMode, ToolGrant
from steward.manifest import Runner as RunnerSpec

SRC = Path(__file__).resolve().parents[1] / "src" / "steward"
WORKDIR = "/var/tmp/work"  # noqa: S108 — a literal in a substitution test, never created
PWNED = "/var/tmp/steward-pwned"  # noqa: S108 — the file a shell-injection test must not create

#: What most of these tests declare, because most of them are about something else. The
#: argv a bound produces has its own tests below.
UNRESTRICTED = ToolGrant("unrestricted")


def request_for(
    tmp_path: Path,
    prompt: str = "say hello",
    timeout_s: int = 10,
    env: Mapping[str, str] | None = None,
) -> r.RunRequest:
    return r.RunRequest(
        tools=UNRESTRICTED,
        prompt=prompt,
        workdir=tmp_path,
        timeout_s=timeout_s,
        env=env or {},
    )


def dumps(tmp_path: Path) -> dict[str, str]:
    """Where a stub should write the argv and cwd it was launched with.

    Handed over in ``request.env`` rather than exported into the test process, because
    since steward #41 a session inherits only :data:`steward.runners.SESSION_ENV_BASE` —
    which is the behaviour under test, and a fixture that needed the old leak to work would
    be a fixture asserting the leak.
    """
    return {"ARGV_DUMP": str(tmp_path / "argv.txt"), "CWD_DUMP": str(tmp_path / "cwd.txt")}


# ------------------------------------------------------------------------------- factory


def test_the_factory_builds_every_declared_kind() -> None:
    assert isinstance(r.build_runner(RunnerSpec(kind="claude")), r.ClaudeRunner)
    assert isinstance(r.build_runner(RunnerSpec(kind="codex")), r.CodexRunner)
    assert isinstance(r.build_runner(RunnerSpec(kind="mock")), r.MockRunner)
    spec = RunnerSpec(kind="command", command=["tool", "{prompt}"])
    assert isinstance(r.build_runner(spec), r.CommandRunner)


def test_dry_run_can_never_reach_a_real_brain() -> None:
    runner = r.build_runner(RunnerSpec(kind="claude", model="claude-opus-5"), force_mock=True)
    assert isinstance(runner, r.MockRunner)


def test_an_unknown_kind_names_the_kinds_that_exist() -> None:
    spec = RunnerSpec(kind="claude")
    object.__setattr__(spec, "kind", "oracle")  # the schema would refuse this; the factory must too
    with pytest.raises(r.RunnerError, match=r"unknown runner kind 'oracle'.*claude"):
        r.build_runner(spec)


def test_a_command_runner_without_a_template_refuses_to_exist() -> None:
    spec = RunnerSpec(kind="mock")
    object.__setattr__(spec, "kind", "command")
    assert r.check_runner(spec) == "runner kind 'command' requires a command template"


def test_a_threaded_process_starts_in_the_descriptor_bound_admitted_directory(
    tmp_path: Path,
) -> None:
    admitted = tmp_path / "work"
    admitted.mkdir()
    descriptor = os.open(admitted, os.O_RDONLY | os.O_DIRECTORY)
    admitted_inode = os.fstat(descriptor).st_ino
    admitted.rename(tmp_path / "old-work")
    admitted.mkdir()
    stop = threading.Event()
    background = threading.Thread(target=stop.wait)
    background.start()
    target = """
import errno
import os
import sys

try:
    os.fstat(int(sys.argv[1]))
except OSError as exc:
    assert exc.errno == errno.EBADF
else:
    raise AssertionError("admission descriptor leaked into target")
print(os.stat(".").st_ino)
"""
    spec = RunnerSpec(
        kind="command",
        command=[sys.executable, "-c", target, str(descriptor), "{prompt}"],
    )
    try:
        result = r.build_runner(spec).run(
            r.RunRequest(
                tools=UNRESTRICTED, prompt="", workdir=admitted, workdir_fd=descriptor, timeout_s=10
            )
        )
    finally:
        os.close(descriptor)
        stop.set()
        background.join()

    assert result.ok
    assert "preexec_fn" not in inspect.getsource(r._ProcessRunner.run)
    assert int(result.output.strip()) == admitted_inode
    assert admitted.stat().st_ino != admitted_inode


def test_a_descriptor_bound_launch_keeps_a_missing_binary_diagnostic(tmp_path: Path) -> None:
    descriptor = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    spec = RunnerSpec(kind="command", command=["missing-steward-test-binary", "{prompt}"])
    try:
        result = r.build_runner(spec).run(
            r.RunRequest(
                tools=UNRESTRICTED, prompt="", workdir=tmp_path, workdir_fd=descriptor, timeout_s=10
            )
        )
    finally:
        os.close(descriptor)

    assert not result.ok
    assert result.error is not None
    assert result.error.startswith("cannot launch 'missing-steward-test-binary':")
    assert not result.error_is_child


@pytest.mark.parametrize("closed_fd", [1, 2])
@pytest.mark.parametrize("scenario", ["launch", "missing"])
def test_descriptor_launch_survives_a_parent_with_closed_standard_streams(
    tmp_path: Path, closed_fd: int, scenario: str
) -> None:
    """Helper capabilities cannot be consumed by Popen's stdout/stderr remapping."""
    missing_binary = scenario == "missing"
    result_path = tmp_path / f"result-{closed_fd}-{scenario}.json"
    command = (
        ["missing-steward-test-binary", "{prompt}"]
        if missing_binary
        else ["/bin/sh", "-c", "pwd", "{prompt}"]
    )
    harness = f"""
import json
import os
from pathlib import Path

from steward import runners
from steward.manifest import Runner, ToolGrant

os.close({closed_fd})
workdir = Path({str(tmp_path)!r})
descriptor = os.open(workdir, os.O_RDONLY | os.O_DIRECTORY)
try:
    result = runners.build_runner(
        Runner(kind="command", command={command!r})
    ).run(runners.RunRequest(
        prompt="",
        workdir=workdir,
        workdir_fd=descriptor,
        timeout_s=10,
        tools=ToolGrant("unrestricted"),
    ))
finally:
    os.close(descriptor)
Path({str(result_path)!r}).write_text(json.dumps({{
    "ok": result.ok,
    "output": result.output.strip(),
    "error": result.error,
    "error_is_child": result.error_is_child,
}}))
"""
    completed = subprocess.run(  # noqa: S603 — fixed interpreter and generated harness
        [sys.executable, "-c", harness],
        cwd=SRC.parents[1],
        env={**os.environ, "PYTHONPATH": str(SRC.parent)},
        capture_output=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr.decode()
    observed = json.loads(result_path.read_text())
    if missing_binary:
        assert not observed["ok"]
        assert observed["error"].startswith("cannot launch 'missing-steward-test-binary':")
        assert "No such file or directory" in observed["error"]
        assert not observed["error_is_child"]
    else:
        assert observed["ok"], observed
        assert observed["output"] == str(tmp_path)


def test_descriptor_helper_ignores_hostile_python_startup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    startup = tmp_path / "startup"
    startup.mkdir()
    (startup / "sitecustomize.py").write_text("import time; time.sleep(30)\n")
    descriptor = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    monkeypatch.setenv("PYTHONPATH", str(startup))
    spec = RunnerSpec(kind="command", command=["/bin/sh", "-c", "printf ready", "{prompt}"])
    try:
        result = r.build_runner(spec).run(
            r.RunRequest(
                tools=UNRESTRICTED, prompt="", workdir=tmp_path, workdir_fd=descriptor, timeout_s=1
            )
        )
    finally:
        os.close(descriptor)

    assert result.outcome is r.Outcome.OK
    assert result.output == "ready"


def test_a_stalled_descriptor_handshake_consumes_the_timeout_and_cleans_up(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    marker = tmp_path / "helper-child-survived"
    helper = f"""
import os
import time

if os.fork() == 0:
    time.sleep(1.5)
    open({str(marker)!r}, "w").close()
    os._exit(0)
time.sleep(30)
"""
    monkeypatch.setattr(r, "_DESCRIPTOR_CWD_HELPER", helper)
    real_pipe = os.pipe
    status_fds: list[int] = []

    def recording_pipe() -> tuple[int, int]:
        status_fds.extend(created := real_pipe())
        return created

    monkeypatch.setattr(r.os, "pipe", recording_pipe)
    descriptor = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    spec = RunnerSpec(kind="command", command=["/bin/sh", "-c", "printf unreachable", "{prompt}"])
    try:
        result = r.build_runner(spec).run(
            r.RunRequest(
                tools=UNRESTRICTED, prompt="", workdir=tmp_path, workdir_fd=descriptor, timeout_s=1
            )
        )
    finally:
        os.close(descriptor)

    assert result.outcome is r.Outcome.TIMEOUT
    assert result.duration_s < 3
    assert result.exit_status is not None
    assert all(_fd_is_closed(fd) for fd in status_fds)
    time.sleep(0.5)
    assert not marker.exists(), "the timed-out helper's process group must not survive"


def _fd_is_closed(fd: int) -> bool:
    try:
        os.fstat(fd)
    except OSError:
        return True
    return False


def test_describe_names_the_brain() -> None:
    assert r.build_runner(RunnerSpec(kind="claude", model="opus")).describe() == "claude (opus)"
    assert r.build_runner(RunnerSpec(kind="codex")).describe() == "codex (default model)"
    spec = RunnerSpec(kind="command", command=["run", "{prompt}"])
    assert r.build_runner(spec).describe() == "command (run {prompt})"


# ------------------------------------------------------------------------ missing binary


@pytest.mark.usefixtures("empty_path")
def test_a_missing_binary_is_a_clear_diagnostic_not_a_midnight_surprise() -> None:
    complaint = r.check_runner(RunnerSpec(kind="claude"))
    assert complaint is not None
    assert "'claude'" in complaint
    assert "not on PATH" in complaint

    complaint = r.check_runner(RunnerSpec(kind="command", command=["my-agent", "{prompt}"]))
    assert complaint is not None
    assert "'my-agent'" in complaint


def test_a_present_binary_passes_the_check(stub_bin: StubWriter) -> None:
    stub_bin("claude", "exit 0")
    assert r.check_runner(RunnerSpec(kind="claude")) is None


def test_a_mock_runner_needs_nothing_on_disk() -> None:
    assert r.check_runner(RunnerSpec(kind="mock")) is None


# ------------------------------------------------------------------------- claude runner


CLAUDE_STUB = r"""
printf '%s\n' "$@" > "$ARGV_DUMP"
pwd > "$CWD_DUMP"
cat <<'JSON'
{"type":"result","is_error":false,"result":"the summary",
 "usage":{"input_tokens":120,"output_tokens":34},"total_cost_usd":0.0125}
JSON
"""


def test_claude_runner_passes_prompt_model_and_cwd(stub_bin: StubWriter, tmp_path: Path) -> None:
    stub_bin("claude", CLAUDE_STUB)
    argv_dump = tmp_path / "argv.txt"
    cwd_dump = tmp_path / "cwd.txt"
    workdir = tmp_path / "work"
    workdir.mkdir()

    runner = r.build_runner(RunnerSpec(kind="claude", model="claude-opus-5"))
    result = runner.run(
        r.RunRequest(
            tools=UNRESTRICTED,
            prompt="write it",
            workdir=workdir,
            timeout_s=10,
            env=dumps(tmp_path),
        )
    )

    argv = argv_dump.read_text().splitlines()
    assert argv == ["-p", "write it", "--output-format", "json", "--model", "claude-opus-5"]
    assert Path(cwd_dump.read_text().strip()).resolve() == workdir.resolve()

    assert result.outcome is r.Outcome.OK
    assert result.output == "the summary"
    assert (result.input_tokens, result.output_tokens) == (120, 34)
    assert result.cost_usd == pytest.approx(0.0125)


def test_claude_runner_passes_permission_mode_when_declared(
    stub_bin: StubWriter, tmp_path: Path
) -> None:
    stub_bin("claude", CLAUDE_STUB)
    spec = RunnerSpec(kind="claude", permission_mode="acceptEdits")
    r.build_runner(spec).run(request_for(tmp_path, env=dumps(tmp_path)))
    assert "--permission-mode\nacceptEdits" in (tmp_path / "argv.txt").read_text()


# ------------------------------------------------------- the tool bound (steward #204)


def claude_argv(  # noqa: PLR0913 — one keyword per thing a test wants to vary
    stub_bin: StubWriter,
    tmp_path: Path,
    tools: ToolGrant,
    *,
    model: str | None = None,
    permission_mode: PermissionMode | None = None,
    workspace: tuple[str, ...] = (),
) -> list[str]:
    """Run a stubbed claude and return the argv it was actually handed.

    The stub is written here rather than by each caller, so no test in this group can
    accidentally reach a real ``claude`` on the developer's PATH.
    """
    stub_bin("claude", CLAUDE_STUB)
    spec = RunnerSpec(kind="claude", model=model, permission_mode=permission_mode)
    r.build_runner(spec).run(
        r.RunRequest(
            prompt="say hello",
            workdir=tmp_path,
            timeout_s=10,
            tools=tools,
            workspace=workspace,
            env=dumps(tmp_path),
        )
    )
    return (tmp_path / "argv.txt").read_text().splitlines()


def test_an_unrestricted_resident_gets_the_argv_it_always_got(
    stub_bin: StubWriter, tmp_path: Path
) -> None:
    """Migrating a resident to `tools: unrestricted` must change nothing about its run.

    Every live resident is migrated that way, so this is the assertion that says the
    migration is a change in what the manifest *says*, not in what steward *does*.
    """
    argv = claude_argv(stub_bin, tmp_path, UNRESTRICTED, model="claude-opus-5")

    assert argv == ["-p", "say hello", "--output-format", "json", "--model", "claude-opus-5"]


def test_a_bounded_resident_is_launched_with_the_names_and_strict_mcp(
    stub_bin: StubWriter, tmp_path: Path
) -> None:
    """The pair, always: `--tools` alone leaves the host's MCP servers reachable."""
    argv = claude_argv(stub_bin, tmp_path, ToolGrant(["Read", "Glob", "Grep"]))

    assert argv[-3:] == ["--tools", "Read,Glob,Grep", "--strict-mcp-config"]


def test_a_resident_bounded_to_nothing_says_so_on_the_command_line(
    stub_bin: StubWriter, tmp_path: Path
) -> None:
    """`tools: []` is a declaration, not an omission, and the CLI has a spelling for it.

    `--tools ""` is documented as *disable all tools*, and it measures that way: a session
    launched with it had none and answered by writing tool-call markup as plain text.
    """
    argv = claude_argv(stub_bin, tmp_path, ToolGrant([]))

    assert argv[-3:] == ["--tools", "", "--strict-mcp-config"]


def test_the_bound_survives_a_permissive_permission_mode(
    stub_bin: StubWriter, tmp_path: Path
) -> None:
    """Both flags reach the CLI together; neither replaces the other.

    Measured against CLI 2.1.247: `--tools Read --permission-mode acceptEdits` still had no
    Bash. The two are different axes — which tools exist, and whether a call to one is
    approved — which is why a manifest may carry both and steward passes both.
    """
    argv = claude_argv(stub_bin, tmp_path, ToolGrant(["Read"]), permission_mode="acceptEdits")

    assert "--permission-mode" in argv
    assert argv[argv.index("--permission-mode") + 1] == "acceptEdits"
    assert argv[-3:] == ["--tools", "Read", "--strict-mcp-config"]


def test_two_requests_that_differ_only_in_tools_are_two_requests(tmp_path: Path) -> None:
    """The mock digest has to see the bound, or a rehearsal reuses another session's answer."""

    def request(tools: ToolGrant) -> r.RunRequest:
        return r.RunRequest(prompt="p", workdir=tmp_path, timeout_s=10, tools=tools)

    bounded = request(ToolGrant(["Read"]))

    assert bounded.key() != request(ToolGrant(["Read", "Bash"])).key()
    assert bounded.key() != request(UNRESTRICTED).key()


def test_a_declared_workspace_reaches_argv_one_flag_at_a_time(
    stub_bin: StubWriter, tmp_path: Path
) -> None:
    """One `--add-dir` per directory, not the variadic `--add-dir a b`.

    A variadic option followed by another flag is a parser question steward does not need
    an opinion about. Measured: repeating it accumulates, and a session handed two
    directories read a file in each.
    """
    argv = claude_argv(
        stub_bin, tmp_path, UNRESTRICTED, workspace=("/data/library", "/data/incoming")
    )

    assert argv[-4:] == ["--add-dir", "/data/library", "--add-dir", "/data/incoming"]


def test_declaring_no_workspace_adds_nothing(stub_bin: StubWriter, tmp_path: Path) -> None:
    """The default is the safe one: the session stays in the directory it was confined to."""
    assert "--add-dir" not in claude_argv(stub_bin, tmp_path, UNRESTRICTED)


def test_a_bounded_session_can_still_be_given_somewhere_to_work(
    stub_bin: StubWriter, tmp_path: Path
) -> None:
    """The two declarations are orthogonal and compile side by side.

    `tools` narrows what exists; `workspace` widens where it may act. Neither is the other,
    and a resident that moves files outside its memory directory needs both.
    """
    argv = claude_argv(
        stub_bin,
        tmp_path,
        ToolGrant(["Bash", "Read"]),
        permission_mode="acceptEdits",
        workspace=("/data/library",),
    )

    assert "--permission-mode" in argv
    assert argv[-5:] == [
        "--tools",
        "Bash,Read",
        "--strict-mcp-config",
        "--add-dir",
        "/data/library",
    ]


def test_two_requests_that_differ_only_in_workspace_are_two_requests(tmp_path: Path) -> None:
    def request(workspace: tuple[str, ...]) -> r.RunRequest:
        return r.RunRequest(
            prompt="p", workdir=tmp_path, timeout_s=10, tools=UNRESTRICTED, workspace=workspace
        )

    assert request(("/data/library",)).key() != request(()).key()
    assert request(("/data/library",)).key() != request(("/data/incoming",)).key()


# --------------------------------------------- whether the installed CLI can hold a bound

#: A `claude --help` that knows both flags, and one that knows neither.
HELP_STUB = 'echo "  --tools <tools...>  bound the built-in set"; echo "  --strict-mcp-config"'
OLD_HELP_STUB = 'echo "  --allowed-tools <tools...>  pre-approve permission rules"'


def test_an_unrestricted_resident_asks_the_cli_nothing(
    stub_bin: StubWriter,
) -> None:
    """Nothing is declared, so there is nothing the installed CLI could fail to hold."""
    stub_bin("claude", OLD_HELP_STUB)
    assert r.check_cli_support(RunnerSpec(kind="claude"), UNRESTRICTED, ()) is None


def test_a_cli_that_knows_the_flags_can_hold_the_bound(stub_bin: StubWriter) -> None:
    stub_bin("claude", HELP_STUB)
    assert r.check_cli_support(RunnerSpec(kind="claude"), ToolGrant(["Read"]), ()) is None


def test_a_cli_too_old_for_the_flags_is_a_complaint_not_a_silent_grant(
    stub_bin: StubWriter,
) -> None:
    """The failure this catches is invisible at run time: the session just has more tools.

    Validation cannot reach it — the manifest is perfectly valid, and the CLI it will run
    against is not in the file. `steward doctor` is the only place the two meet.
    """
    stub_bin("claude", OLD_HELP_STUB)
    complaint = r.check_cli_support(RunnerSpec(kind="claude"), ToolGrant(["Read"]), ())

    assert complaint is not None
    assert "--tools" in complaint
    assert "--strict-mcp-config" in complaint


def test_half_the_pair_is_still_a_complaint(stub_bin: StubWriter) -> None:
    """`--tools` without `--strict-mcp-config` bounds the built-ins and leaks the host's MCP."""
    stub_bin("claude", 'echo "  --tools <tools...>"')
    complaint = r.check_cli_support(RunnerSpec(kind="claude"), ToolGrant(["Read"]), ())

    assert complaint is not None
    assert "--strict-mcp-config" in complaint
    assert "--tools" not in complaint.split("does not support", 1)[1].split(",", 1)[0]


@pytest.mark.usefixtures("empty_path")
def test_a_cli_that_will_not_answer_is_unproven_rather_than_assumed_fine() -> None:
    assert "unproven" in (
        r.check_cli_support(RunnerSpec(kind="claude"), ToolGrant(["Read"]), ()) or ""
    )


def test_a_kind_that_compiles_no_tool_flag_is_not_probed(stub_bin: StubWriter) -> None:
    """Validation already refuses a bound under codex/command; doctor does not re-litigate."""
    stub_bin("claude", OLD_HELP_STUB)
    assert r.check_cli_support(RunnerSpec(kind="mock"), ToolGrant(["Read"]), ()) is None


def test_a_cli_that_cannot_widen_a_session_is_a_complaint_too(stub_bin: StubWriter) -> None:
    """A workspace grant is a flag as much as a bound is, and the same CLI has to have it."""
    stub_bin("claude", HELP_STUB)  # knows --tools and --strict-mcp-config, not --add-dir
    complaint = r.check_cli_support(RunnerSpec(kind="claude"), UNRESTRICTED, ("/data/library",))

    assert complaint is not None
    assert "--add-dir" in complaint


def test_the_flags_a_manifest_needs_are_exactly_what_it_declared() -> None:
    """Nothing declared, nothing probed — and a kind that compiles none is asked for none."""
    claude = RunnerSpec(kind="claude")

    assert r.required_flags(claude, UNRESTRICTED, ()) == ()
    assert r.required_flags(claude, UNRESTRICTED, ("/data",)) == ("--add-dir",)
    assert r.required_flags(claude, ToolGrant(["Read"]), ()) == (
        "--tools",
        "--strict-mcp-config",
    )
    assert r.required_flags(RunnerSpec(kind="codex"), ToolGrant(["Read"]), ("/data",)) == ()


# ------------------------------------------- what a brain may claim it spent (steward #129)


HOSTILE_COST_STUB = r"""
cat <<JSON
{"type":"result","is_error":false,"result":"the summary",
 "usage":{"input_tokens":120,"output_tokens":34},"total_cost_usd":$COST}
JSON
"""


@pytest.mark.parametrize(
    ("reported", "why"),
    [
        ("NaN", "every comparison against a poisoned sum returns False"),
        ("Infinity", "and an infinite one trips the cap for ever"),
        ("-Infinity", "as does its opposite, in the other direction"),
        ("-5.0", "a negative cost offsets real spend"),
        ("1e12", "and an absurd one is a bug or a lie either way"),
    ],
)
def test_a_cost_steward_cannot_believe_is_recorded_as_unknown(
    stub_bin: StubWriter, tmp_path: Path, reported: str, why: str
) -> None:
    """The daily cap is computed from a number the model process supplies.

    ``json.loads`` accepts ``NaN``, ``Infinity`` and ``-Infinity`` — non-standard literals
    that pass every type gate and then poison the ``SUM`` the cap reads. One such row and
    ``spent >= limit`` returns ``False`` for the rest of the day, so a declared safety
    control silently stops being one while the gauge still reads green.

    Refused, not clamped: the run is recorded as usage-unknown, which the ledger already
    has a word for, rather than as a cost steward made up.
    """
    stub_bin("claude", HOSTILE_COST_STUB)

    result = r.build_runner(RunnerSpec(kind="claude")).run(
        request_for(tmp_path, env={"COST": reported})
    )

    assert result.outcome is r.Outcome.OK
    assert result.output == "the summary", "the run itself is untouched"
    assert result.cost_usd is None, why


def test_one_refused_number_discredits_the_whole_usage_report(
    stub_bin: StubWriter, tmp_path: Path
) -> None:
    """Refusing only the bad field would leave the run flagged as usage-known.

    ``BudgetGuard.record`` derives ``usage_known`` by OR-ing the three usage fields, and
    the ledger writes ``cost_usd or 0.0``. So a payload carrying a NaN cost *and* honest
    token counts would land as ``cost_usd=0.0, usage_known=1``: not counted toward the
    daily cap, and not counted in ``Spend.unreported`` either — invisible in both
    directions, which is a worse place to be than the NaN was.

    A number steward will not believe discredits the report it arrived in.
    """
    stub_bin(
        "claude",
        r"""
cat <<'JSON'
{"type":"result","is_error":false,"result":"the summary",
 "usage":{"input_tokens":120,"output_tokens":34},"total_cost_usd":NaN}
JSON
""",
    )

    result = r.build_runner(RunnerSpec(kind="claude")).run(request_for(tmp_path))

    assert (result.cost_usd, result.input_tokens, result.output_tokens) == (None, None, None)
    assert result.output == "the summary", "the run itself still happened"
    assert result.outcome is r.Outcome.OK


def test_a_cost_too_large_to_be_a_float_is_refused_not_raised(
    stub_bin: StubWriter, tmp_path: Path
) -> None:
    """``json`` hands back unbounded ints, and ``float()`` on one raises.

    A JSON integer literal past ~1e308 clears the ``isinstance(value, (int, float))`` gate
    and then cannot be converted at all. Unguarded, the ``OverflowError`` escaped
    ``parse`` into the blanket handler in ``sessions.run`` — turning a session that
    finished into a FAILED one with its output thrown away, which is precisely what
    bounding the value at the boundary was supposed to avoid.
    """
    stub_bin(
        "claude",
        'cat <<\'JSON\'\n{"type":"result","is_error":false,"result":"the summary",'
        '"total_cost_usd":' + "9" * 400 + "}\nJSON\n",
    )

    result = r.build_runner(RunnerSpec(kind="claude")).run(request_for(tmp_path))

    assert result.outcome is r.Outcome.OK, "an unbelievable number is not a failed run"
    assert result.output == "the summary"
    assert result.cost_usd is None


def test_a_plausible_cost_still_gets_through(stub_bin: StubWriter, tmp_path: Path) -> None:
    """The bound is an absurdity ceiling, not a policy — ordinary money passes."""
    stub_bin("claude", HOSTILE_COST_STUB)

    result = r.build_runner(RunnerSpec(kind="claude")).run(
        request_for(tmp_path, env={"COST": "12.5"})
    )

    assert result.cost_usd == pytest.approx(12.5)


@pytest.mark.parametrize("reported", ["-1", "999999999999999999999"])
def test_a_token_count_steward_cannot_believe_is_recorded_as_unknown(
    stub_bin: StubWriter, tmp_path: Path, reported: str
) -> None:
    """Token counts are summed into an 8-byte column, so they are bounded too.

    And a refused count discredits the cost that arrived with it, for the same reason a
    refused cost discredits the counts: ``usage_known`` is one flag over all three, so a
    partially-believed report would be recorded as a fully-trusted one.
    """
    stub_bin(
        "claude",
        r"""
cat <<JSON
{"type":"result","is_error":false,"result":"the summary",
 "usage":{"input_tokens":$TOKENS,"output_tokens":$TOKENS},"total_cost_usd":0.01}
JSON
""",
    )
    result = r.build_runner(RunnerSpec(kind="claude")).run(
        request_for(tmp_path, env={"TOKENS": reported})
    )

    assert (result.input_tokens, result.output_tokens) == (None, None)
    assert result.cost_usd is None, "and the cost that came with it is not believed either"
    assert result.output == "the summary", "the run itself still happened"


def test_claude_reporting_its_own_error_is_a_failed_run(
    stub_bin: StubWriter, tmp_path: Path
) -> None:
    body = json.dumps({"type": "result", "is_error": True, "result": "hit the rate limit"})
    stub_bin("claude", f"cat <<'JSON'\n{body}\nJSON")
    result = r.build_runner(RunnerSpec(kind="claude")).run(request_for(tmp_path))
    assert result.outcome is r.Outcome.FAILED
    assert result.error == "hit the rate limit"


def test_unparsable_output_keeps_the_raw_text(stub_bin: StubWriter, tmp_path: Path) -> None:
    stub_bin("claude", "echo not json at all")
    result = r.build_runner(RunnerSpec(kind="claude")).run(request_for(tmp_path))
    assert result.outcome is r.Outcome.OK
    assert result.output.strip() == "not json at all"
    assert result.input_tokens is None


def test_a_nonzero_exit_is_failed_with_stderr(stub_bin: StubWriter, tmp_path: Path) -> None:
    stub_bin("claude", "echo 'the model is unavailable' >&2; exit 3")
    result = r.build_runner(RunnerSpec(kind="claude")).run(request_for(tmp_path))
    assert result.outcome is r.Outcome.FAILED
    assert result.exit_status == 3
    assert result.error == "the model is unavailable"  # the raw child text — local log only
    # ...but the summary that reaches a village event is classified, never the child text.
    assert result.summary() == "exit status 3"
    assert "the model is unavailable" not in result.summary()


@pytest.mark.usefixtures("empty_path")
def test_a_binary_that_is_not_there_is_a_failed_run_not_an_exception(tmp_path: Path) -> None:
    result = r.build_runner(RunnerSpec(kind="claude")).run(request_for(tmp_path))
    assert result.outcome is r.Outcome.FAILED
    assert "cannot launch 'claude'" in (result.error or "")
    # A launch failure is steward's own diagnostic, not child output, so it is safe to
    # surface in the summary an event serializes.
    assert "cannot launch 'claude'" in result.summary()
    assert result.error_is_child is False


def test_a_failure_never_leaks_child_output_into_the_summary(
    stub_bin: StubWriter, tmp_path: Path
) -> None:
    """The summary that reaches a village event is classified: never the child's own text."""
    secret_key = "sk-ant-api03-DEADBEEFsecretkeymaterial"
    stub_bin("claude", f'echo "{secret_key}" >&2; echo "BURROW_TOKEN=hunter2" >&2; exit 1')
    result = r.build_runner(RunnerSpec(kind="claude")).run(request_for(tmp_path))

    assert result.outcome is r.Outcome.FAILED
    # What an event builder serializes — nothing the child chose to print.
    assert result.summary() == "exit status 1"
    assert secret_key not in result.summary()
    assert "BURROW_TOKEN" not in result.summary()
    # The raw child text is still kept, but only for the local log.
    assert secret_key in (result.error or "")


# -------------------------------------------------------------------------- codex runner


def test_codex_runner_uses_exec_and_puts_the_prompt_last(
    stub_bin: StubWriter, tmp_path: Path
) -> None:
    stub_bin("codex", 'printf "%s\\n" "$@" > "$ARGV_DUMP"')
    argv_dump = tmp_path / "argv.txt"

    runner = r.build_runner(RunnerSpec(kind="codex", model="gpt-5-codex"))
    result = runner.run(
        r.RunRequest(
            tools=UNRESTRICTED,
            prompt="tidy up",
            workdir=tmp_path,
            timeout_s=10,
            env=dumps(tmp_path),
        )
    )

    assert argv_dump.read_text().splitlines() == ["exec", "--model", "gpt-5-codex", "tidy up"]
    assert result.outcome is r.Outcome.OK
    assert result.cost_usd is None  # codex tells us nothing, so steward claims nothing


# ------------------------------------------------------------------------ command runner


def test_substitution_fills_both_placeholders_and_nothing_else() -> None:
    argv = r.substitute(
        ["tool", "--prompt", "{prompt}", "--cwd", "{workdir}", "--literal", "{other}"],
        prompt="hello",
        workdir=WORKDIR,
    )
    assert argv == ["tool", "--prompt", "hello", "--cwd", WORKDIR, "--literal", "{other}"]


def test_a_prompt_can_never_smuggle_in_a_second_substitution() -> None:
    argv = r.substitute(["tool", "{prompt}", "{workdir}"], prompt="{workdir}", workdir="/secret")
    assert argv == ["tool", "{workdir}", "/secret"], "one pass only: a prompt is data"


def test_shell_metacharacters_stay_one_argument(stub_bin: StubWriter, tmp_path: Path) -> None:
    stub_bin("my-agent", 'printf "%s\\n" "$@" > "$ARGV_DUMP"')
    argv_dump = tmp_path / "argv.txt"
    hostile = f"; rm -rf / && $(whoami) `id` | tee {PWNED}"

    spec = RunnerSpec(kind="command", command=["my-agent", "--prompt", "{prompt}"])
    result = r.build_runner(spec).run(
        r.RunRequest(
            tools=UNRESTRICTED,
            prompt=hostile,
            workdir=tmp_path,
            timeout_s=10,
            env=dumps(tmp_path),
        )
    )

    assert result.outcome is r.Outcome.OK
    assert argv_dump.read_text().splitlines() == ["--prompt", hostile]
    assert not Path(PWNED).exists()


def test_command_runner_substitutes_the_workdir(stub_bin: StubWriter, tmp_path: Path) -> None:
    stub_bin("my-agent", 'printf "%s\\n" "$@" > "$ARGV_DUMP"')
    spec = RunnerSpec(kind="command", command=["my-agent", "{workdir}", "{prompt}"])
    r.build_runner(spec).run(
        r.RunRequest(
            tools=UNRESTRICTED, prompt="p", workdir=tmp_path, timeout_s=10, env=dumps(tmp_path)
        )
    )
    assert (tmp_path / "argv.txt").read_text().splitlines() == [str(tmp_path), "p"]


# --------------------------------------------------------------------------- timeout


def test_a_run_that_overruns_is_killed_and_says_so(stub_bin: StubWriter, tmp_path: Path) -> None:
    stub_bin("claude", "sleep 30")
    result = r.build_runner(RunnerSpec(kind="claude")).run(request_for(tmp_path, timeout_s=1))
    assert result.outcome is r.Outcome.TIMEOUT
    assert not result.ok
    assert "1s timeout" in (result.error or "")
    assert result.duration_s < 10


def test_a_timeout_kills_the_whole_process_group(stub_bin: StubWriter, tmp_path: Path) -> None:
    marker = tmp_path / "child-survived"
    stub_bin("claude", f"(sleep 3; touch {marker}) & sleep 30")
    result = r.build_runner(RunnerSpec(kind="claude")).run(request_for(tmp_path, timeout_s=1))
    assert result.outcome is r.Outcome.TIMEOUT
    assert not marker.exists(), "a killed session must not leave children running"


def test_a_timeout_keeps_the_partial_stdout_it_already_produced(
    stub_bin: StubWriter, tmp_path: Path
) -> None:
    """A session that escalates then hangs must not lose the question it already asked."""
    block = "<needs-human>approve the wire transfer?</needs-human>"
    stub_bin("claude", f"printf '%s\\n' '{block}'; sleep 30")
    result = r.build_runner(RunnerSpec(kind="claude")).run(request_for(tmp_path, timeout_s=1))
    assert result.outcome is r.Outcome.TIMEOUT
    assert block in result.output, "the escalation printed before the hang is kept"
    # The timeout reason is steward's own, so it is safe in a summary — and it is never the
    # child's block, which stays in output for a harvester to read.
    assert "timeout" in result.summary()
    assert block not in result.summary()


# ------------------------------------------------------------------------------ mock


def test_the_mock_runner_is_deterministic(tmp_path: Path) -> None:
    runner = r.MockRunner(RunnerSpec(kind="mock", model="pretend"))
    first = runner.run(request_for(tmp_path))
    second = runner.run(request_for(tmp_path))
    assert first == second
    assert first.outcome is r.Outcome.OK
    assert "pretend" in first.output
    other = runner.run(request_for(tmp_path, prompt="a different prompt"))
    assert other.output != first.output
    assert len(runner.requests) == 3


def test_the_mock_runner_takes_an_injected_behavior(tmp_path: Path) -> None:
    runner = r.MockRunner(behavior=lambda _: r.RunResult(outcome=r.Outcome.TIMEOUT))
    assert runner.run(request_for(tmp_path)).outcome is r.Outcome.TIMEOUT


def test_result_summary_reports_what_happened() -> None:
    assert "killed after" in r.RunResult(outcome=r.Outcome.TIMEOUT, duration_s=9.0).summary()
    assert r.RunResult(outcome=r.Outcome.FAILED, exit_status=2).summary() == "exit status 2"


def test_request_env_reaches_the_session(stub_bin: StubWriter, tmp_path: Path) -> None:
    stub_bin("claude", 'echo "$BURROW_AGENT_ID" > "$ARGV_DUMP"')
    runner = r.build_runner(RunnerSpec(kind="claude"))
    runner.run(
        r.RunRequest(
            tools=UNRESTRICTED,
            prompt="p",
            workdir=tmp_path,
            timeout_s=10,
            env={
                "ARGV_DUMP": str(tmp_path / "env.txt"),
                "BURROW_AGENT_ID": "claude-code:life-agent",
            },
        )
    )
    assert (tmp_path / "env.txt").read_text().strip() == "claude-code:life-agent"


# -------------------------------------------- the session environment (steward #41)

#: A stub that reports its *whole* environment, one ``NAME=value`` per line. Every test in
#: this group reads the child's real environment rather than steward's source: the
#: acceptance criterion for steward #41 is what the process actually sees.
ENV_DUMP_STUB = 'env > "$ENV_DUMP"'


def child_env(
    stub_bin: StubWriter,
    tmp_path: Path,
    *,
    request: r.RunRequest | None = None,
    kind: Literal["claude", "codex"] = "claude",
) -> dict[str, str]:
    """Launch a real stub and return the environment it was actually given."""
    stub_bin(kind, ENV_DUMP_STUB)
    dump = tmp_path / "env.txt"
    asked = request or request_for(tmp_path)
    handed = dict(asked.env)
    handed["ENV_DUMP"] = str(dump)
    result = r.build_runner(RunnerSpec(kind=kind)).run(replace(asked, env=handed))
    assert result.outcome is r.Outcome.OK, result
    observed: dict[str, str] = {}
    for line in dump.read_text().splitlines():
        name, separator, value = line.partition("=")
        if separator:
            observed[name] = value
    return observed


def test_a_session_never_sees_stewards_api_token(
    stub_bin: StubWriter, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The leak steward #41 is about, asserted against the child's own environment.

    ``steward serve`` with auth on holds ``STEWARD_TOKEN`` in ``os.environ`` by
    construction — there is no ``--token`` flag — and fires sessions from inside that
    process. Every locally launched session was therefore carrying the master key into the
    API, which is the credential the escalation boundary and the two-manifest delegation
    rule both assume no session holds.
    """
    monkeypatch.setenv("STEWARD_TOKEN", "the-master-key")
    monkeypatch.setenv("BURROW_TOKEN", "the-shared-ingest-secret")

    observed = child_env(stub_bin, tmp_path)

    assert "STEWARD_TOKEN" not in observed
    assert "BURROW_TOKEN" not in observed
    assert "the-master-key" not in observed.values()


def test_a_session_sees_only_the_allowlist_and_what_steward_chose(
    stub_bin: StubWriter, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Nothing ambient gets in, and everything deliberate does.

    The second half matters as much as the first: an allowlist that also dropped
    ``request.env`` would be a session with no identity, and ``BURROW_AGENT_ID`` is how
    burrow knows which villager acted.
    """
    monkeypatch.setenv("SOME_UNRELATED_SECRET", "nope")
    monkeypatch.setenv("STEWARD_STATE", "/state/scheduler.json")

    observed = child_env(
        stub_bin,
        tmp_path,
        request=request_for(tmp_path, env={"BURROW_AGENT_ID": "claude-code:life-agent"}),
    )

    assert "SOME_UNRELATED_SECRET" not in observed
    assert observed["BURROW_AGENT_ID"] == "claude-code:life-agent"
    assert observed["STEWARD_STATE"] == "/state/scheduler.json", (
        "a session's own `steward delegate` has to open the same database"
    )
    assert observed["PATH"], "and it still has to be able to find its brain"


def test_a_session_finds_its_brains_own_credential(
    stub_bin: StubWriter, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A session that cannot buy tokens is not a bounded session, it is a broken one.

    And each brain gets only its own: the provider credential is the session's fuel, so it
    passes, but there is no reason a claude session should be holding an OpenAI key.
    """
    monkeypatch.setenv("ANTHROPIC_API_KEY", "anthropic-fuel")
    monkeypatch.setenv("OPENAI_API_KEY", "openai-fuel")

    claude = child_env(stub_bin, tmp_path)
    codex = child_env(stub_bin, tmp_path, kind="codex")

    assert claude["ANTHROPIC_API_KEY"] == "anthropic-fuel"
    assert "OPENAI_API_KEY" not in claude
    assert codex["OPENAI_API_KEY"] == "openai-fuel"
    assert "ANTHROPIC_API_KEY" not in codex


def test_request_env_wins_over_the_launching_process(
    stub_bin: StubWriter, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A resident's identity is its manifest's, not the launching process's."""
    monkeypatch.setenv("BURROW_URL", "http://the-control-planes-village")

    observed = child_env(
        stub_bin,
        tmp_path,
        request=request_for(tmp_path, env={"BURROW_URL": "http://this-residents-village"}),
    )

    assert observed["BURROW_URL"] == "http://this-residents-village"


def test_an_unset_allowlisted_name_is_absent_rather_than_empty(
    stub_bin: StubWriter, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``PATH=""`` is not "no preference", it is a PATH whose one entry is the cwd."""
    monkeypatch.delenv("TZ", raising=False)

    observed = child_env(stub_bin, tmp_path)

    assert "TZ" not in observed


def test_the_operator_can_name_extra_variables_a_session_needs(
    stub_bin: StubWriter, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The escape hatch, without which the `ssh` command template stops working."""
    monkeypatch.setenv("SSH_AUTH_SOCK", "/tmp/agent.sock")  # noqa: S108 — a value, never opened
    monkeypatch.setenv(r.SESSION_ENV_PASSTHROUGH_ENV, " SSH_AUTH_SOCK , ")

    observed = child_env(stub_bin, tmp_path)

    assert observed["SSH_AUTH_SOCK"] == "/tmp/agent.sock"  # noqa: S108 — the same value


def test_the_passthrough_refuses_the_master_key_and_says_so(
    stub_bin: StubWriter,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """An operator who listed it believes their sessions have it; silence would be worse."""
    monkeypatch.setenv("STEWARD_TOKEN", "the-master-key")
    monkeypatch.setenv(r.SESSION_ENV_PASSTHROUGH_ENV, "STEWARD_TOKEN,STEWARD_SESSION_TOKEN")

    with caplog.at_level("WARNING"):
        observed = child_env(stub_bin, tmp_path)

    assert "STEWARD_TOKEN" not in observed
    assert "STEWARD_SESSION_TOKEN" not in observed
    assert "which no session may hold" in caplog.text


def test_every_refused_name_is_a_name_the_allowlist_does_not_carry() -> None:
    """The two lists cannot disagree: a refused name on the allowlist would be a hole."""
    assert r.SESSION_ENV_REFUSED.isdisjoint(r.SESSION_ENV_BASE)
    assert r.SESSION_ENV_REFUSED.isdisjoint(r.ClaudeRunner.env_names)
    assert r.SESSION_ENV_REFUSED.isdisjoint(r.CodexRunner.env_names)


# -------------------------------------------------------- the seam is the only seam


def test_only_the_runner_module_may_launch_a_session() -> None:
    """No code path outside runners.py spawns an LLM CLI. Verified by reading the tree."""
    forbidden = re.compile(r"\bsubprocess\b|\bos\.system\b|\bos\.exec|\bos\.popen\b|\bPopen\b")
    offenders = {
        path.name
        for path in SRC.glob("*.py")
        if path.name != "runners.py" and forbidden.search(path.read_text(encoding="utf-8"))
    }
    assert offenders == set(), (
        f"{sorted(offenders)} launch processes directly; every session must go through "
        f"steward.runners so 'which brain is Hob on' has one answer"
    )


# ---------------------------------------------------------- feeding a command stdin


def test_a_command_can_be_fed_a_payload_on_stdin(stub_bin: StubWriter, tmp_path: Path) -> None:
    """The nursery pipes a tar into `tar -xf -` this way, because the NAS has no working scp."""
    dump = tmp_path / "stdin.bin"
    stub_bin("swallow", f'cat > "{dump}"')

    outcome = r.run_argv(["swallow"], stdin=b"a tar archive, or near enough\x00")

    assert outcome.ok
    assert dump.read_bytes() == b"a tar archive, or near enough\x00"


def test_a_command_with_no_stdin_sees_an_empty_one(stub_bin: StubWriter) -> None:
    stub_bin("echoback", "cat")
    assert r.run_argv(["echoback"]).stdout == ""


def test_a_timed_out_command_does_not_leave_children_running(
    stub_bin: StubWriter, tmp_path: Path
) -> None:
    marker = tmp_path / "command-child-survived"
    stub_bin("slow-control-plane", f"(sleep 1; touch {marker}) & sleep 30")

    outcome = r.run_argv(["slow-control-plane"], timeout_s=0.2)

    assert not outcome.ok
    assert "did not answer" in outcome.summary()
    time.sleep(1.1)
    assert not marker.exists(), "a timed-out command must not leave its remote child running"


# ----------------------------------------------- where the control plane's docker points


def test_a_control_plane_command_inherits_the_daemons_docker_host(
    stub_bin: StubWriter, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`run_argv` passes this process's whole environment through — including DOCKER_HOST.

    Unlike a session (steward #41), a control-plane command is *not* environment-scrubbed:
    ``docker inspect`` and ``docker restart`` are steward's own tools and need this host's
    docker configuration to work at all. Which is what makes ``DOCKER_HOST`` a real pointer
    rather than a hopeful one, and therefore what steward #59 is allowed to document.
    """
    monkeypatch.setenv("DOCKER_HOST", "ssh://Miha@dxp2800")
    stub_bin("say-docker-host", 'printf %s "$DOCKER_HOST"')

    assert r.run_argv(["say-docker-host"]).stdout == "ssh://Miha@dxp2800"


def test_a_container_launch_hands_docker_the_daemons_docker_host(
    stub_bin: StubWriter, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The exec launcher passes it too, so both halves of #58 point at the same daemon.

    ``_run_in_container`` builds its child environment as ``{**os.environ, **request.env}``
    — the docker *client* is a control-plane tool like the nursery's ssh, and only the
    named ``-e`` variables cross into the container.
    """
    seen = tmp_path / "docker-host-seen"
    monkeypatch.setenv("DOCKER_HOST", "ssh://Miha@dxp2800")
    stub_bin("docker", f'printf %s "$DOCKER_HOST" > {seen}; printf "{{}}"')
    runner = r.build_runner(RunnerSpec(kind="claude"), PLACED)

    runner.run(request_for(tmp_path, env={"STEWARD_RUN_ID": "run-dh"}))

    assert seen.read_text(encoding="utf-8") == "ssh://Miha@dxp2800"


# ------------------------------------------------- container placement (steward #58)


CONTAINER = "steward-testy"


def docker_stub(  # noqa: PLR0913 — one keyword per behaviour a test wants to pick
    stub_bin: StubWriter,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    session: str = "json",
    running: str = "true",
    brain_probe: int = 0,
    kill_status: int = 0,
    help_flags: str = "",
) -> Path:
    """Stub the ``docker`` CLI and return the log every invocation appends its argv to.

    The stub answers ``inspect`` with ``running``, an exec carrying ``command -v`` with
    ``brain_probe``, an exec carrying ``--help`` with ``help_flags``, an exec carrying the
    kill script by exiting zero, and the session exec itself per ``session``: ``json``
    prints the claude result document, ``hang`` prints a line and sleeps far past any
    test timeout.
    """
    log = tmp_path / "docker-calls.log"
    monkeypatch.setenv("DOCKER_LOG", str(log))
    if session == "json":
        act = (
            "cat <<'JSON'\n"
            '{"type":"result","is_error":false,"result":"the summary",\n'
            ' "usage":{"input_tokens":120,"output_tokens":34},"total_cost_usd":0.0125}\n'
            "JSON"
        )
    else:
        act = 'printf "partial thought\\n"; sleep 30'
    stub_bin(
        "docker",
        f"""
{{ printf '%s\\n' "===CALL"; for a in "$@"; do printf '%s\\n' "$a"; done; }} >> "$DOCKER_LOG"
case "$1" in
  inspect) printf '{running}\\n'; exit 0 ;;
esac
for a in "$@"; do
  case "$a" in
    *"command -v"*) exit {brain_probe} ;;
    *"kill -9"*) exit {kill_status} ;;
    --help) printf '%s\\n' "{help_flags}"; exit 0 ;;
  esac
done
{act}
""",
    )
    return log


def docker_calls(log: Path) -> list[list[str]]:
    """Parse the stub's log into one argv list per docker invocation."""
    calls: list[list[str]] = []
    for line in log.read_text(encoding="utf-8").splitlines():
        if line == "===CALL":
            calls.append([])
        else:
            calls[-1].append(line)
    return calls


PLACED = r.Placement(container=CONTAINER, workdir="/data/memory")


def test_a_container_placed_session_execs_into_the_container(
    stub_bin: StubWriter, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The whole shape at once: workdir, named env, shim, untouched brain argv.

    The brain argv after the shim is byte for byte what a local placement builds —
    including ``--output-format json`` — which is what keeps ``parse`` feeding tokens and
    cost into the budget ledger for a container-placed resident.
    """
    log = docker_stub(stub_bin, monkeypatch, tmp_path)
    runner = r.build_runner(RunnerSpec(kind="claude", model="claude-opus-5"), PLACED)

    result = runner.run(
        request_for(tmp_path, env={"STEWARD_RUN_ID": "run-42", "BURROW_AGENT_ID": "cc:testy"})
    )

    assert result.outcome is r.Outcome.OK
    assert result.output == "the summary"
    assert (result.input_tokens, result.output_tokens) == (120, 34)
    assert result.cost_usd == pytest.approx(0.0125)

    (call,) = docker_calls(log)
    assert call[:3] == ["exec", "-w", "/data/memory"]
    assert call[3:7] == ["-e", "BURROW_AGENT_ID", "-e", "STEWARD_RUN_ID"]
    assert call[7:9] == [CONTAINER, "sh"]
    assert call[9] == "-c"
    shim = call[10]
    assert 'printf %s "$$" > /run/steward/run-42.pid' in shim
    assert "rm -f /run/steward/run-42.pid" in shim
    assert call[11:] == [
        "claude",
        "-p",
        "say hello",
        "--output-format",
        "json",
        "--model",
        "claude-opus-5",
    ]


def test_a_container_session_carries_only_the_named_variables(
    stub_bin: StubWriter, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """No allowlist, no passthrough, no control-plane secrets — only ``request.env``.

    The container has its own environment from compose and its ``.env``, and the brain's
    credentials live on its ``/root/.claude`` volume. A ``STEWARD_TOKEN`` in the control
    plane's environment must not be visible inside the session (steward #58's acceptance
    line), and neither must the base names local placement forwards.
    """
    log = docker_stub(stub_bin, monkeypatch, tmp_path)
    monkeypatch.setenv("STEWARD_TOKEN", "the-master-key")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-not-for-the-wire")
    monkeypatch.setenv(r.SESSION_ENV_PASSTHROUGH_ENV, "ANTHROPIC_API_KEY")
    runner = r.build_runner(RunnerSpec(kind="claude"), PLACED)

    credential = "steward-session-not-for-any-ps"
    runner.run(
        request_for(
            tmp_path,
            env={"STEWARD_RUN_ID": "run-9", "STEWARD_SESSION_TOKEN": credential},
        )
    )

    (call,) = docker_calls(log)
    named = [call[index + 1] for index, part in enumerate(call) if part == "-e"]
    assert named == ["STEWARD_RUN_ID", "STEWARD_SESSION_TOKEN"]
    joined = "\n".join(call)
    assert "STEWARD_TOKEN=" not in joined
    assert "ANTHROPIC_API_KEY" not in joined
    assert "HOME" not in joined
    # By name, never NAME=value: the values include the per-run session credential, and
    # an argv is readable in any host `ps` for the life of the run.
    assert credential not in joined


def test_a_container_timeout_is_a_kill_inside_the_container(
    stub_bin: StubWriter, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The kill must go inside the container, first — a dead client is not a dead run.

    Killing the local ``docker exec`` client alone would leave the session burning
    tokens behind a ledger row that says timeout, and the partial output the session
    already streamed must survive (the ``_drain`` guarantee).
    """
    log = docker_stub(stub_bin, monkeypatch, tmp_path, session="hang")
    runner = r.build_runner(RunnerSpec(kind="claude"), PLACED)

    result = runner.run(request_for(tmp_path, timeout_s=1, env={"STEWARD_RUN_ID": "run-7"}))

    assert result.outcome is r.Outcome.TIMEOUT
    assert "partial thought" in result.output
    _session, kill = docker_calls(log)
    assert "sleep 30" not in "\n".join(kill), "the kill call must not be the session call"
    assert kill[0] == "exec"
    assert kill[1] == CONTAINER
    script = kill[-1]
    assert 'kill -9 -"$pid"' in script
    assert "/run/steward/run-7.pid" in script
    assert "rm -f /run/steward/run-7.pid" in script
    assert "could not be delivered" not in (result.error or "")


def test_an_undeliverable_kill_is_said_out_loud(
    stub_bin: StubWriter, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A timeout whose kill never arrived is a session that may still be running.

    The outcome stays timeout — steward did give up on the run — but the words must not
    claim a kill that did not land; that would be the trap-(a) lie in a different tense.
    """
    log = docker_stub(stub_bin, monkeypatch, tmp_path, session="hang", kill_status=1)
    runner = r.build_runner(RunnerSpec(kind="claude"), PLACED)

    result = runner.run(request_for(tmp_path, timeout_s=1, env={"STEWARD_RUN_ID": "run-8"}))

    assert result.outcome is r.Outcome.TIMEOUT
    assert result.error is not None
    assert "could not be delivered" in result.error
    assert "may still be running" in result.error
    assert not result.error_is_child, "this is steward's own diagnostic, safe for an event"
    assert len(docker_calls(log)) == 2


def test_a_run_id_that_is_not_a_name_never_reaches_the_shim(
    stub_bin: StubWriter, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The run id is minted by steward, and the pid file still refuses to trust it."""
    log = docker_stub(stub_bin, monkeypatch, tmp_path)
    runner = r.build_runner(RunnerSpec(kind="claude"), PLACED)
    hostile = 'x"; rm -rf /; echo "'

    runner.run(request_for(tmp_path, env={"STEWARD_RUN_ID": hostile}))

    (call,) = docker_calls(log)
    shim = call[call.index("-c") + 1]
    assert hostile not in shim
    assert re.search(r"/run/steward/[0-9a-f]{24}\.pid", shim)


def test_a_container_placement_ignores_the_workdir_descriptor(
    stub_bin: StubWriter, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The launch must not wrap itself in the local cwd helper over the descriptor.

    The descriptor pins a control-plane directory; across `docker exec` it means nothing.
    """
    log = docker_stub(stub_bin, monkeypatch, tmp_path)
    runner = r.build_runner(RunnerSpec(kind="claude"), PLACED)
    fd = os.open(tmp_path, os.O_RDONLY)
    try:
        request = replace(request_for(tmp_path, env={"STEWARD_RUN_ID": "run-3"}), workdir_fd=fd)
        result = runner.run(request)
    finally:
        os.close(fd)

    assert result.outcome is r.Outcome.OK
    (call,) = docker_calls(log)
    assert call[0] == "exec"


def test_container_check_probes_the_container_not_the_local_path(
    stub_bin: StubWriter, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The control plane's own PATH has no claude here, and it must not matter.

    A container-placed resident is ready when *its container* is running and carries
    the brain.
    """
    docker_stub(stub_bin, monkeypatch, tmp_path)

    assert r.check_runner(RunnerSpec(kind="claude"), PLACED) is None


def test_a_stopped_container_is_a_loud_complaint(
    stub_bin: StubWriter, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    docker_stub(stub_bin, monkeypatch, tmp_path, running="false")

    complaint = r.check_runner(RunnerSpec(kind="claude"), PLACED)

    assert complaint is not None
    assert CONTAINER in complaint
    assert "not running" in complaint


@pytest.mark.usefixtures("empty_path")
def test_an_unanswerable_docker_is_a_loud_complaint() -> None:
    complaint = r.check_runner(RunnerSpec(kind="claude"), PLACED)

    assert complaint is not None
    assert "docker could not answer" in complaint


def test_a_brainless_container_names_the_missing_binary(
    stub_bin: StubWriter, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    docker_stub(stub_bin, monkeypatch, tmp_path, brain_probe=1)

    complaint = r.check_runner(RunnerSpec(kind="claude"), PLACED)

    assert complaint is not None
    assert "'claude'" in complaint
    assert CONTAINER in complaint


def test_an_unplaceable_kind_is_refused_at_the_factory() -> None:
    """The seam mirrors validation, for specs that never crossed a validator.

    A container-placed mock or command runner may not be built at all.
    """
    for spec in (
        RunnerSpec(kind="mock"),
        RunnerSpec(kind="command", command=["tool", "{prompt}"]),
    ):
        with pytest.raises(r.RunnerError, match="cannot be placed in a container"):
            r.build_runner(spec, PLACED)
        complaint = r.check_runner(spec, PLACED)
        assert complaint is not None
        assert "cannot be placed" in complaint


def test_describe_names_both_axes() -> None:
    runner = r.build_runner(RunnerSpec(kind="claude", model="claude-opus-5"), PLACED)
    assert runner.describe() == f"claude (claude-opus-5) in container {CONTAINER}"


def test_the_flag_probe_asks_the_container_cli(
    stub_bin: StubWriter, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The probe must ask the CLI that will actually run the sessions.

    The CLI a provisioned container carries is pinned by its image, not by the laptop
    that validated the manifest.
    """
    log = docker_stub(
        stub_bin, monkeypatch, tmp_path, help_flags="--tools --strict-mcp-config --add-dir"
    )

    assert r.check_cli_support(RunnerSpec(kind="claude"), ToolGrant(["Read"]), (), PLACED) is None
    (call,) = docker_calls(log)
    assert call == ["exec", CONTAINER, "claude", "--help"]


def test_the_flag_probe_reports_a_container_cli_too_old_to_bound(
    stub_bin: StubWriter, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    docker_stub(stub_bin, monkeypatch, tmp_path, help_flags="--model only")

    complaint = r.check_cli_support(RunnerSpec(kind="claude"), ToolGrant(["Read"]), (), PLACED)

    assert complaint is not None
    assert "--tools" in complaint
