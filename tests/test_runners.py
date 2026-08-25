"""The runner seam: what each brain is actually told, and who may spawn one."""

import json
import re
from pathlib import Path

import pytest

from conftest import StubWriter
from steward import runners as r
from steward.manifest import Runner as RunnerSpec

SRC = Path(__file__).resolve().parents[1] / "src" / "steward"
WORKDIR = "/var/tmp/work"  # noqa: S108 — a literal in a substitution test, never created
PWNED = "/var/tmp/steward-pwned"  # noqa: S108 — the file a shell-injection test must not create


def request_for(tmp_path: Path, prompt: str = "say hello", timeout_s: int = 10) -> r.RunRequest:
    return r.RunRequest(prompt=prompt, workdir=tmp_path, timeout_s=timeout_s)


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


def test_claude_runner_passes_prompt_model_and_cwd(
    stub_bin: StubWriter, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    stub_bin("claude", CLAUDE_STUB)
    argv_dump = tmp_path / "argv.txt"
    cwd_dump = tmp_path / "cwd.txt"
    monkeypatch.setenv("ARGV_DUMP", str(argv_dump))
    monkeypatch.setenv("CWD_DUMP", str(cwd_dump))
    workdir = tmp_path / "work"
    workdir.mkdir()

    runner = r.build_runner(RunnerSpec(kind="claude", model="claude-opus-5"))
    result = runner.run(r.RunRequest(prompt="write it", workdir=workdir, timeout_s=10))

    argv = argv_dump.read_text().splitlines()
    assert argv == ["-p", "write it", "--output-format", "json", "--model", "claude-opus-5"]
    assert Path(cwd_dump.read_text().strip()).resolve() == workdir.resolve()

    assert result.outcome is r.Outcome.OK
    assert result.output == "the summary"
    assert (result.input_tokens, result.output_tokens) == (120, 34)
    assert result.cost_usd == pytest.approx(0.0125)


def test_claude_runner_passes_permission_mode_when_declared(
    stub_bin: StubWriter, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    stub_bin("claude", CLAUDE_STUB)
    monkeypatch.setenv("ARGV_DUMP", str(tmp_path / "argv.txt"))
    monkeypatch.setenv("CWD_DUMP", str(tmp_path / "cwd.txt"))
    spec = RunnerSpec(kind="claude", permission_mode="acceptEdits")
    r.build_runner(spec).run(request_for(tmp_path))
    assert "--permission-mode\nacceptEdits" in (tmp_path / "argv.txt").read_text()


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
    stub_bin: StubWriter, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    stub_bin("codex", 'printf "%s\\n" "$@" > "$ARGV_DUMP"')
    argv_dump = tmp_path / "argv.txt"
    monkeypatch.setenv("ARGV_DUMP", str(argv_dump))

    runner = r.build_runner(RunnerSpec(kind="codex", model="gpt-5-codex"))
    result = runner.run(r.RunRequest(prompt="tidy up", workdir=tmp_path, timeout_s=10))

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


def test_shell_metacharacters_stay_one_argument(
    stub_bin: StubWriter, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    stub_bin("my-agent", 'printf "%s\\n" "$@" > "$ARGV_DUMP"')
    argv_dump = tmp_path / "argv.txt"
    monkeypatch.setenv("ARGV_DUMP", str(argv_dump))
    hostile = f"; rm -rf / && $(whoami) `id` | tee {PWNED}"

    spec = RunnerSpec(kind="command", command=["my-agent", "--prompt", "{prompt}"])
    result = r.build_runner(spec).run(r.RunRequest(prompt=hostile, workdir=tmp_path, timeout_s=10))

    assert result.outcome is r.Outcome.OK
    assert argv_dump.read_text().splitlines() == ["--prompt", hostile]
    assert not Path(PWNED).exists()


def test_command_runner_substitutes_the_workdir(
    stub_bin: StubWriter, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    stub_bin("my-agent", 'printf "%s\\n" "$@" > "$ARGV_DUMP"')
    monkeypatch.setenv("ARGV_DUMP", str(tmp_path / "argv.txt"))
    spec = RunnerSpec(kind="command", command=["my-agent", "{workdir}", "{prompt}"])
    r.build_runner(spec).run(r.RunRequest(prompt="p", workdir=tmp_path, timeout_s=10))
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


def test_request_env_reaches_the_session(
    stub_bin: StubWriter, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    stub_bin("claude", 'echo "$BURROW_AGENT_ID" > "$ARGV_DUMP"')
    monkeypatch.setenv("ARGV_DUMP", str(tmp_path / "env.txt"))
    runner = r.build_runner(RunnerSpec(kind="claude"))
    runner.run(
        r.RunRequest(
            prompt="p",
            workdir=tmp_path,
            timeout_s=10,
            env={"BURROW_AGENT_ID": "claude-code:life-agent"},
        )
    )
    assert (tmp_path / "env.txt").read_text().strip() == "claude-code:life-agent"


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
