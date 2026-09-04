"""Container placement against a real docker daemon (steward #58's acceptance tests).

The unit tests in ``test_runners.py`` pin the argv shapes against a stubbed ``docker``;
these prove the two claims a stub cannot: that a timed-out session is *verifiably dead
inside the container* — ``ps`` shows no survivor, not merely a dead local client — and
that the environment boundary and the mount-side working directory hold against a real
``docker exec``.

Skipped wholesale when no docker daemon answers, so the suite stays runnable on a
machine with no docker at all. The container is throwaway ``alpine:3`` with a shell
script standing in for the brain: what is under test is the launcher, the shim, and the
kill — none of which care which brain they carry.
"""

import secrets
import shutil
import subprocess
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

from conftest import ResidentWriter, valid_manifest
from steward import runners as r
from steward import topology
from steward.deploy import render_compose, target_for
from steward.manifest import Runner as RunnerSpec
from steward.manifest import ToolGrant, load_manifest
from steward.skills import Skill, materialize

UNRESTRICTED = ToolGrant("unrestricted")


#: The docker CLI by absolute path, resolved once — and the module-wide skip when the
#: daemon does not answer, so the suite stays runnable on a machine with no docker.
DOCKER = shutil.which("docker") or "docker"


def _docker_answers() -> bool:
    """Report whether a docker *daemon* answers here, not merely whether a client exists.

    ``docker version --format '{{.Server.Version}}'`` rather than ``docker info``: measured
    against docker 27.3.1, ``info`` prints the client's half of the report and **exits 0**
    when nothing is listening at ``DOCKER_HOST`` (see
    ``test_docker_info_exits_zero_at_an_endpoint_with_no_daemon``). This gate is what makes
    the module's "skipped wholesale when no docker daemon answers" promise true, so it has
    to be the probe that can actually tell — otherwise a host with the CLI and no daemon
    skips nothing and fails everything.
    """
    if shutil.which("docker") is None:
        return False
    try:
        probe = subprocess.run(  # noqa: S603 — fixed argv, a capability probe
            [DOCKER, "version", "--format", "{{.Server.Version}}"],
            capture_output=True,
            timeout=15,
            check=False,
        )
    except OSError, subprocess.TimeoutExpired:
        return False
    return probe.returncode == 0 and bool(probe.stdout.strip())


pytestmark = pytest.mark.skipif(not _docker_answers(), reason="no docker daemon on this host")


def _docker(*argv: str, stdin: bytes | None = None, timeout: float = 60) -> str:
    completed = subprocess.run(  # noqa: S603 — fixed test argv against a throwaway container
        [DOCKER, *argv], input=stdin, capture_output=True, timeout=timeout, check=True
    )
    return completed.stdout.decode()


@pytest.fixture
def container() -> Iterator[str]:
    """Provide a throwaway running container, removed however the test ends.

    ``--init`` because the rendered compose files run residents the same way
    (:func:`steward.deploy.render_compose`): PID 1 must reap what a killed session
    leaves behind, or every timeout strands zombies for the container's lifetime.
    """
    name = f"steward-test-{secrets.token_hex(4)}"
    _docker("run", "-d", "--init", "--name", name, "alpine:3", "sleep", "300", timeout=120)
    try:
        yield name
    finally:
        subprocess.run(  # noqa: S603 — cleanup must run even after a failure
            [DOCKER, "rm", "-f", name], capture_output=True, check=False, timeout=60
        )


def _pid_file_exists(container: str, run_id: str) -> bool:
    probe = subprocess.run(  # noqa: S603 — fixed probe argv
        [DOCKER, "exec", container, "test", "-e", f"/run/steward/{run_id}.pid"],
        capture_output=True,
        check=False,
        timeout=30,
    )
    return probe.returncode == 0


def _install_brain(container: str, script: str) -> None:
    """Put a shell script named ``claude`` on the container's PATH."""
    _docker(
        "exec",
        "-i",
        container,
        "sh",
        "-c",
        "cat > /usr/local/bin/claude && chmod +x /usr/local/bin/claude",
        stdin=("#!/bin/sh\n" + script + "\n").encode(),
    )


@pytest.mark.parametrize("mode", ["rw", "ro"])
def test_a_container_session_obeys_the_rendered_mount_mode(
    tmp_path: Path,
    mode: str,
    write_resident: ResidentWriter,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The rendered declaration gives rw host writes and makes ro writes fail."""
    host = tmp_path / "vault"
    host.mkdir()
    name = f"steward-test-mount-{secrets.token_hex(4)}"
    data = valid_manifest()
    data["workspace"] = ["/vault"]
    data["deploy"] = {
        "container": name,
        "image": "alpine:3",
        "command": ["sleep", "300"],
        "mounts": [{"host": str(host), "container": "/vault", "mode": mode}],
    }
    resident = load_manifest(write_resident(data))
    compose_dir = tmp_path / "compose"
    compose_dir.mkdir()
    (compose_dir / "memory").mkdir()
    (compose_dir / "claude").mkdir()
    compose = compose_dir / "docker-compose.yaml"
    compose.write_text(render_compose(resident, target_for(resident.manifest)), encoding="utf-8")
    monkeypatch.setenv("CHRONICLE_URL", "http://dockerhost:8737")
    _docker("compose", "-f", str(compose), "up", "-d", timeout=120)
    try:
        _install_brain(name, "echo resident > /vault/from-resident")
        runner = r.build_runner(
            RunnerSpec(kind="claude"),
            r.Placement(container=name, workdir="/tmp"),  # noqa: S108
        )

        result = runner.run(
            r.RunRequest(
                prompt="write",
                workdir=tmp_path,
                timeout_s=30,
                tools=UNRESTRICTED,
                workspace=("/vault",),
            )
        )

        if mode == "rw":
            assert result.ok, (result.error, result.output)
            assert (host / "from-resident").read_text(encoding="utf-8") == "resident\n"
        else:
            assert not result.ok or "Read-only file system" in result.output
            assert not (host / "from-resident").exists()
    finally:
        subprocess.run(  # noqa: S603 — cleanup of this test's generated compose project
            [DOCKER, "compose", "-f", str(compose), "down"],
            capture_output=True,
            check=False,
            timeout=60,
        )


def test_a_timed_out_session_is_verifiably_dead_inside_the_container(
    container: str, tmp_path: Path
) -> None:
    """The acceptance crux: after `_terminate`, `docker exec ps` shows no survivor.

    The fake brain prints an escalation and hangs with a child process, exactly the
    shape of a stuck session. The timeout must keep the partial output (the ``_drain``
    guarantee), and the group kill inside the container must take both the brain and
    its child — a ledger row saying timeout over a session still burning tokens is the
    lie this launcher exists to refuse.
    """
    _install_brain(container, 'echo "partial escalation"; sleep 120')
    runner = r.build_runner(
        RunnerSpec(kind="claude"),
        r.Placement(container=container, workdir="/tmp"),  # noqa: S108 — a container path
    )

    result = runner.run(
        r.RunRequest(
            prompt="hang",
            workdir=tmp_path,
            timeout_s=2,
            tools=UNRESTRICTED,
            env={"STEWARD_RUN_ID": "run-int-timeout"},
        )
    )

    assert result.outcome is r.Outcome.TIMEOUT
    assert "partial escalation" in result.output
    time.sleep(0.5)
    survivors = _docker("exec", container, "ps")
    assert "claude" not in survivors
    assert "sleep 120" not in survivors
    assert not _pid_file_exists(container, "run-int-timeout"), (
        "the kill path must remove the dead run's pid file"
    )


def test_a_container_session_runs_in_the_mount_with_only_named_env_and_reports_cost(
    container: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One real run: the exec workdir, the env boundary, and the ledger numbers.

    The brain reports back its working directory, its run id, and whether it can see a
    ``STEWARD_TOKEN`` that is present in the control plane's environment — through the
    same ``--output-format json`` document the real CLI emits, so the cost lands the way
    the budget ledger will read it.
    """
    _install_brain(
        container,
        "cat <<JSON\n"
        '{"type":"result","is_error":false,'
        '"result":"$(pwd)|$STEWARD_RUN_ID|${STEWARD_TOKEN:-unseen}",'
        '"usage":{"input_tokens":11,"output_tokens":7},"total_cost_usd":0.0042}\n'
        "JSON",
    )
    monkeypatch.setenv("STEWARD_TOKEN", "the-master-key")
    runner = r.build_runner(
        RunnerSpec(kind="claude"),
        r.Placement(container=container, workdir="/tmp"),  # noqa: S108 — a container path
    )

    result = runner.run(
        r.RunRequest(
            prompt="report",
            workdir=tmp_path,
            timeout_s=30,
            tools=UNRESTRICTED,
            env={"STEWARD_RUN_ID": "run-int-ok"},
        )
    )

    assert result.ok, (result.error, result.output)
    assert result.output == "/tmp|run-int-ok|unseen"  # noqa: S108 — a container path
    assert (result.input_tokens, result.output_tokens) == (11, 7)
    assert result.cost_usd == pytest.approx(0.0042)
    assert not _pid_file_exists(container, "run-int-ok"), (
        "an ordinary exit must remove its own pid file"
    )


# ------------------------------------------------- where the docker client points (#59)

#: A TCP endpoint no docker daemon is ever listening on. Port 1 is privileged and
#: reserved, so this fails at connect rather than reaching somebody's real daemon.
NOWHERE = "tcp://127.0.0.1:1"


def test_a_bogus_docker_host_reaches_the_real_docker_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The measurement steward #59's documentation rests on, against a real client.

    ``test_runners.py`` proves ``run_argv`` passes the parent environment through by
    reading it back out of a stub. This proves the consequence that matters: the *real*
    docker client honours the ``DOCKER_HOST`` steward's daemon was started with, so
    pointing it at another machine genuinely moves supervision there. The failure is the
    proof — a client that had ignored the variable would have answered from this host.

    What it deliberately does not prove is that a remote ``DOCKER_HOST`` is enough for
    *container-placed execution*: that half also needs the host side of the resident's
    memory mount on the control plane's own filesystem, which
    :func:`steward.sessions.workdir_refusal` requires and no endpoint can supply. See
    ``docs/topology.md``.
    """
    monkeypatch.setenv("DOCKER_HOST", NOWHERE)

    outcome = r.run_argv([DOCKER, "ps"])

    assert not outcome.ok
    assert NOWHERE in outcome.stderr, outcome.stderr


def test_docker_info_exits_zero_at_an_endpoint_with_no_daemon(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Why `steward.topology` does not trust the exit status of its own probe.

    Against an unreachable endpoint, `docker info` prints the *client's* half of the
    report, writes "Cannot connect to the Docker daemon" to stderr, and exits **0** — so a
    status-only reachability check reports a client talking to itself as a healthy daemon.
    `_ask_docker` requires the server fields to be filled in instead, and this is the
    measurement that rule rests on; a future docker that starts exiting non-zero here is
    welcome to break this test.
    """
    monkeypatch.setenv("DOCKER_HOST", NOWHERE)

    outcome = r.run_argv([DOCKER, "info", "--format", topology.DAEMON_FORMAT])

    assert outcome.ok, "docker info stopped exiting 0 at a dead endpoint; simplify the probe"
    assert outcome.stdout.strip() == "", outcome.stdout
    assert topology._ask_docker(r.run_argv).complaint == topology.NO_SERVER


def test_a_granted_skill_crosses_the_mount_and_pruning_removes_it(tmp_path: Path) -> None:
    """Criterion 5 across the real boundary: materialize host-side, read it from inside.

    The control plane writes skills into the host side of the memory mount; the session
    discovers them at the container's `.claude/skills` through the bind mount. And the
    load-bearing half — a skill removed from the manifest is *gone* from the next
    session — must hold across the same boundary, because pruning is how an ungranted
    skill stops being a capability.
    """
    host_memory = tmp_path / "memory"
    host_memory.mkdir()
    name = f"steward-test-{secrets.token_hex(4)}"
    try:
        _docker(
            "run",
            "-d",
            "--init",
            "--name",
            name,
            "-v",
            f"{host_memory}:/data/memory",
            "alpine:3",
            "sleep",
            "300",
            timeout=120,
        )
    except subprocess.CalledProcessError as exc:
        pytest.skip(f"docker cannot bind-mount {host_memory}: {exc.stderr.decode()[:200]}")
    try:
        granted = [
            Skill(name="daily-summary", description="Summarise the day.", body="Do it."),
            Skill(name="write-journal", description="Close the day.", body="Write it."),
        ]
        materialize(granted, host_memory, ".claude/skills")
        inside = _docker("exec", name, "ls", "/data/memory/.claude/skills")
        assert "daily-summary" in inside
        assert "write-journal" in inside

        materialize(granted[:1], host_memory, ".claude/skills")
        inside = _docker("exec", name, "ls", "/data/memory/.claude/skills")
        assert "daily-summary" in inside
        assert "write-journal" not in inside
    finally:
        subprocess.run(  # noqa: S603 — cleanup must run even after a failure
            [DOCKER, "rm", "-f", name], capture_output=True, check=False, timeout=60
        )
