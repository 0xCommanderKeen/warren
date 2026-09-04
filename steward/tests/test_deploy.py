"""The transport seam and the compose fragment: everything the nursery ships, without a NAS."""

import io
import os
import tarfile
from collections.abc import Sequence
from dataclasses import replace
from pathlib import Path

import pytest
import yaml

from conftest import ResidentWriter, valid_manifest
from steward.deploy import (
    BURROW_ENV,
    BURROW_HOME_ENV,
    CHRONICLE_TOKEN_ENV,
    CHRONICLE_URL_ENV,
    COMPOSE_FILENAME,
    DEFAULT_HOST,
    DEFAULT_USER,
    ENV_FILENAME,
    BurrowTransport,
    LocalTransport,
    SshTransport,
    TransportError,
    bundle_changes,
    bundle_for,
    compose_argv,
    emitter_env,
    memory_host_dir,
    pack,
    render_compose,
    render_env,
    target_for,
    transport_for,
)
from steward.manifest import Memory, Resident, load_manifest, validate_manifest
from steward.runners import CommandOutcome

VILLAGE = {
    CHRONICLE_URL_ENV: "http://dxp2800:8737",
    CHRONICLE_TOKEN_ENV: "s3cret-village-token",
}


def resident(write_resident: ResidentWriter, **overrides: object) -> Resident:
    """Write a valid resident and load it back through the ordinary validator."""
    return load_manifest(write_resident(valid_manifest() | overrides))


# ------------------------------------------------------------------- resolving a target


def test_a_manifest_with_no_deploy_block_still_has_an_address(write_resident) -> None:
    """The dxp2800 layout is a documented default, not an assumption nobody wrote down."""
    target = target_for(resident(write_resident).manifest)

    assert target.host == DEFAULT_HOST
    assert target.user == DEFAULT_USER
    assert target.container == "steward-test-agent"
    assert target.path == "~/docker/warren/residents/test-agent"
    assert target.image == "steward-resident:latest"
    assert target.command == ("sleep", "infinity")


def test_the_manifest_wins_over_every_default(write_resident) -> None:
    deploy = {
        "host": "other-nas",
        "user": "someone",
        "path": "/srv/agents/quill",
        "container": "quill",
        "image": "ghcr.io/example/agent:v2",
        "command": ["python", "-m", "agent"],
    }
    target = target_for(resident(write_resident, deploy=deploy).manifest)

    assert target.describe() == (
        "quill on someone@other-nas:/srv/agents/quill (ghcr.io/example/agent:v2)"
    )
    assert target.compose_path == "/srv/agents/quill/docker-compose.yaml"
    assert target.env_path == "/srv/agents/quill/.env"
    assert "someone" not in str(target.to_dict()["command"])


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("host", "dxp2800; rm -rf /"),
        ("user", "Miha && curl evil"),
        ("path", "~/docker/$(whoami)"),
        ("image", "steward-resident `id`"),
    ],
)
def test_a_deploy_field_that_would_reach_a_remote_shell_fails_validation(
    write_resident, field: str, value: str
) -> None:
    """The remote end is a shell, so these patterns are a boundary, not a style guide."""
    result = validate_manifest(write_resident(valid_manifest() | {"deploy": {field: value}}))

    assert not result.ok
    assert any(f"deploy.{field}" in d.field_path for d in result.errors)


# ------------------------------------------------------------------------- the compose


def test_the_compose_fragment_is_valid_yaml_naming_this_resident(write_resident) -> None:
    one = resident(write_resident)
    target = target_for(one.manifest)
    rendered = yaml.safe_load(render_compose(one, target))

    service = rendered["services"]["test-agent"]
    assert service["container_name"] == "steward-test-agent"
    assert service["image"] == "steward-resident:latest"
    assert service["environment"]["CHRONICLE_AGENT_ID"] == "claude-code:test-agent"
    assert service["environment"]["CHRONICLE_PROJECT"] == "test-agent"
    # warren#361: the pre-rename BURROW_* twins are gone, not merely unread.
    assert not [key for key in service["environment"] if key.startswith("BURROW_")]
    assert service["command"] == ["sleep", "infinity"]


def test_the_compose_fragment_declares_the_container_a_sandbox(write_resident) -> None:
    """``IS_SANDBOX=1`` is in every resident's compose environment (warren#391).

    The resident image runs as root and the claude CLI refuses ``--permission-mode
    bypassPermissions`` for root unless this variable says the process is already
    sandboxed. A manifest that asks for the bypass would otherwise die at its first fire
    with "cannot be used with root/sudo privileges" — measured in steward-hob, CLI
    2.1.243. The container is the sandbox, so the fact is rendered for every resident,
    not switched on by the one manifest that happens to need it today.
    """
    one = resident(write_resident)
    service = yaml.safe_load(render_compose(one, target_for(one.manifest)))["services"][
        "test-agent"
    ]
    assert service["environment"]["IS_SANDBOX"] == "1"
    assert "./memory:/data/residents/test-agent/memory" in service["volumes"]
    assert "./claude:/root/.claude" in service["volumes"]
    assert service["extra_hosts"] == ["dockerhost:host-gateway"], (
        "the village address steward copies into .env is http://dockerhost:8737 on the burrow"
    )


def test_the_compose_fragment_sets_the_clock_to_the_routines_zone(write_resident) -> None:
    """``TZ`` follows the routines' ``schedule_tz`` when they all agree (warren#386)."""
    data = valid_manifest()
    for routine in data["routines"]:
        routine["schedule_tz"] = "Europe/Ljubljana"
    one = load_manifest(write_resident(data))
    service = yaml.safe_load(render_compose(one, target_for(one.manifest)))["services"][
        "test-agent"
    ]
    assert service["environment"]["TZ"] == "Europe/Ljubljana"


def test_deploy_tz_wins_over_the_routines_zone(write_resident) -> None:
    data = valid_manifest()
    for routine in data["routines"]:
        routine["schedule_tz"] = "Europe/Ljubljana"
    data["deploy"] = {"tz": "America/New_York"}
    one = load_manifest(write_resident(data))
    service = yaml.safe_load(render_compose(one, target_for(one.manifest)))["services"][
        "test-agent"
    ]
    assert service["environment"]["TZ"] == "America/New_York"


def test_a_resident_with_no_routines_keeps_a_utc_clock(write_resident) -> None:
    one = resident(write_resident, routines=[])
    service = yaml.safe_load(render_compose(one, target_for(one.manifest)))["services"][
        "test-agent"
    ]
    assert service["environment"]["TZ"] == "UTC"


def test_the_compose_fragment_references_the_secrets_instead_of_carrying_them(
    write_resident,
) -> None:
    """The token reaches the container through .env, and the compose file only points at it."""
    one = resident(write_resident)
    text = render_compose(one, target_for(one.manifest))

    assert "${CHRONICLE_TOKEN-}" in text
    assert "${CHRONICLE_URL:?" in text
    assert VILLAGE[CHRONICLE_TOKEN_ENV] not in text


def test_a_smuggled_memory_path_stays_data_and_makes_no_new_compose_keys(write_resident) -> None:
    """#61, the second line of defence: even a value that got past validation is a scalar.

    The manifest patterns already reject a ``memory.path`` carrying a newline, but the
    render must not *depend* on them: it builds a dict and lets ``yaml.safe_dump`` quote
    every value, so a smuggled ``privileged: true`` lands as part of one string and never
    becomes a key of its own.
    """
    one = resident(write_resident)
    smuggled = "/data/memory\n    privileged: true\n    volumes:\n    - /:/host"
    evil = Memory.model_construct(
        kind="directory", path=smuggled, journal="journal", journal_keep=30
    )
    bad = replace(one, manifest=one.manifest.model_copy(update={"memory": evil}))

    service = yaml.safe_load(render_compose(bad, target_for(bad.manifest)))["services"][
        "test-agent"
    ]

    assert set(service) == {
        "image",
        "container_name",
        "restart",
        "init",
        "extra_hosts",
        "working_dir",
        "environment",
        "volumes",
        "command",
    }
    assert "privileged" not in service
    assert service["working_dir"] == smuggled  # preserved verbatim, as one quoted scalar
    assert len(service["volumes"]) == 2


def test_rendering_the_same_resident_twice_gives_the_same_bytes(write_resident) -> None:
    """Convergence is decided by comparison, so a render that drifted would break it."""
    one = resident(write_resident)
    target = target_for(one.manifest)

    assert render_compose(one, target) == render_compose(one, target)
    assert pack(bundle_for(one, target, VILLAGE)) == pack(bundle_for(one, target, VILLAGE))


def test_the_env_file_is_sorted_lines_and_nothing_else() -> None:
    assert render_env(VILLAGE) == (
        "CHRONICLE_TOKEN=s3cret-village-token\nCHRONICLE_URL=http://dxp2800:8737\n"
    )
    assert render_env({}) == ""


def test_a_secret_with_a_line_break_in_it_is_refused() -> None:
    """A .env has no quoting, so a second line would silently become a second variable."""
    with pytest.raises(TransportError, match="line break"):
        render_env({CHRONICLE_TOKEN_ENV: "one\ntwo"})


def test_a_village_with_no_address_is_refused_before_anything_is_built() -> None:
    with pytest.raises(TransportError, match=CHRONICLE_URL_ENV):
        emitter_env({})


def test_a_village_with_no_token_is_allowed_and_says_so() -> None:
    """Chronicle's ingest is open when its own token is unset; that is a real deployment."""
    assert emitter_env({CHRONICLE_URL_ENV: "http://dxp2800:8737"}) == {
        CHRONICLE_URL_ENV: "http://dxp2800:8737",
    }


def test_the_env_file_carries_one_spelling_of_each_variable() -> None:
    """warren#361: the BURROW_* twins are gone from the resident's .env.

    They were there because a deployed container ran whatever image its host was last
    shipped, and a pre-warren#234 image's emitter read BURROW_* only. Every host has since
    been rebuilt and re-provisioned, so the twins bought nothing and named a service that
    no longer exists.
    """
    values = emitter_env({CHRONICLE_URL_ENV: "http://dxp2800:8737", CHRONICLE_TOKEN_ENV: "s3cret"})
    assert values == {
        CHRONICLE_URL_ENV: "http://dxp2800:8737",
        CHRONICLE_TOKEN_ENV: "s3cret",
    }


def test_a_pre_rename_environment_no_longer_configures_a_provision() -> None:
    """The old spelling is not read either: steward's own environment was re-spelled.

    A refusal is the right failure here — a resident provisioned with nowhere to emit is
    the silent one, and this is the loud one.
    """
    with pytest.raises(TransportError, match=CHRONICLE_URL_ENV):
        emitter_env({"BURROW_URL": "http://dxp2800:8737", "BURROW_TOKEN": "s3cret"})


def test_compose_commands_name_the_file_and_the_project_explicitly(write_resident) -> None:
    """There is no shell on the far side to `cd` in, so both are absolute."""
    target = target_for(resident(write_resident).manifest)
    argv = compose_argv(target, "up", "-d")

    assert argv == (
        "docker",
        "compose",
        "-f",
        "~/docker/warren/residents/test-agent/docker-compose.yaml",
        "--project-directory",
        "~/docker/warren/residents/test-agent",
        "-p",
        "test-agent",
        "up",
        "-d",
    )


# --------------------------------------------------------------------------- the bundle


def test_the_bundle_carries_the_declaration_the_repo_holds(write_resident) -> None:
    one = resident(write_resident)
    files = bundle_for(one, target_for(one.manifest), VILLAGE)

    assert set(files) == {
        COMPOSE_FILENAME,
        ENV_FILENAME,
        "manifest.yaml",
        "soul.md",
        "memory/.keep",
        "claude/.keep",
    }
    assert b"Testy" in files["soul.md"]
    assert yaml.safe_load(files["manifest.yaml"])["id"] == "test-agent"


def test_the_archive_is_deterministic_and_keeps_the_env_private(write_resident) -> None:
    one = resident(write_resident)
    archive = pack(bundle_for(one, target_for(one.manifest), VILLAGE))

    with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
        modes = {member.name: member.mode for member in tar.getmembers()}
        times = {member.mtime for member in tar.getmembers()}
    assert modes[ENV_FILENAME] == 0o600
    assert modes[COMPOSE_FILENAME] == 0o644
    assert times == {0}


# ------------------------------------------------------------------- the local transport


def test_the_local_transport_unpacks_the_real_archive(tmp_path: Path, write_resident) -> None:
    """The fake host receives exactly the bytes the ssh transport would have piped."""
    one = resident(write_resident)
    target = target_for(one.manifest)
    host = LocalTransport(root=tmp_path / "host")

    outcome = host.send(bundle_for(one, target, VILLAGE), target.path)

    assert outcome.ok
    landed = tmp_path / "host" / "docker" / "warren" / "residents" / "test-agent"
    assert (landed / COMPOSE_FILENAME).is_file()
    assert host.read(target.env_path) == render_env(VILLAGE)
    assert host.sent == ["~/docker/warren/residents/test-agent"]


def test_the_local_transport_records_commands_instead_of_running_them(tmp_path: Path) -> None:
    host = LocalTransport(root=tmp_path / "host")
    assert host.run(["docker", "compose", "up", "-d"]).ok
    assert host.calls == [("docker", "compose", "up", "-d")]
    assert host.touched


def test_a_command_the_fake_host_refuses_comes_back_as_a_failure(tmp_path: Path) -> None:
    host = LocalTransport(root=tmp_path / "host", fail_on="up")
    assert not host.run(["docker", "compose", "up", "-d"]).ok


def test_an_unreachable_fake_host_raises_rather_than_lying(tmp_path: Path) -> None:
    host = LocalTransport(root=tmp_path / "host", unreachable=True)
    with pytest.raises(TransportError):
        host.run(["docker", "ps"])
    with pytest.raises(TransportError):
        host.send({"a": b"b"}, "~/x")
    with pytest.raises(TransportError):
        host.read("~/x")


def test_reading_a_file_the_fake_host_does_not_have_is_none(tmp_path: Path) -> None:
    assert LocalTransport(root=tmp_path / "host").read("~/nothing") is None


def test_changes_are_named_file_by_file(tmp_path: Path, write_resident) -> None:
    one = resident(write_resident)
    target = target_for(one.manifest)
    host = LocalTransport(root=tmp_path / "host")
    files = bundle_for(one, target, VILLAGE)

    assert COMPOSE_FILENAME in bundle_changes(host, files, target.path)
    host.send(files, target.path)
    assert bundle_changes(host, files, target.path) == ()


# --------------------------------------------------------------------- the ssh transport


class Recorder:
    """A stand-in for run_argv that remembers argv and stdin, and starts nothing."""

    def __init__(self, *, ok: bool = True) -> None:
        """Start with nothing recorded, answering ok or not as the test asked."""
        self.calls: list[tuple[str, ...]] = []
        self.stdin: list[bytes | None] = []
        self.timeouts: list[float] = []
        self.ok = ok

    def __call__(
        self,
        argv: Sequence[str],
        timeout_s: float = 20.0,
        *,
        stdin: bytes | None = None,
    ) -> CommandOutcome:
        """Record the call and answer without launching anything."""
        self.calls.append(tuple(argv))
        self.stdin.append(stdin)
        self.timeouts.append(timeout_s)
        return CommandOutcome(argv=tuple(argv), exit_status=0 if self.ok else 1, stdout="hello\n")


def test_ssh_puts_the_user_and_host_in_front_of_every_command() -> None:
    recorder = Recorder()
    transport = SshTransport(host="dxp2800", user="Miha", command=recorder)

    transport.run(["docker", "ps"])

    assert recorder.calls == [
        (
            "ssh",
            "-o",
            "BatchMode=yes",
            "-o",
            "ConnectTimeout=10",
            "Miha@dxp2800",
            "docker",
            "ps",
        )
    ]
    assert transport.plan(["docker", "ps"]) == recorder.calls[0]
    assert transport.describe() == "ssh Miha@dxp2800"


def test_ssh_ships_files_as_a_tar_on_stdin_because_scp_is_broken() -> None:
    recorder = Recorder()
    transport = SshTransport(command=recorder)

    outcome = transport.send({"a.txt": b"hello"}, "~/docker/x")

    assert outcome.ok
    prefix = (
        "ssh",
        "-o",
        "BatchMode=yes",
        "-o",
        "ConnectTimeout=10",
        "Miha@dxp2800",
    )
    assert recorder.calls == [
        (*prefix, "mkdir", "-p", "~/docker/x"),
        (*prefix, "tar", "-xf", "-", "-C", "~/docker/x"),
    ]
    assert recorder.stdin[0] is None
    assert recorder.timeouts == [20.0, 120.0]
    with tarfile.open(fileobj=io.BytesIO(recorder.stdin[1] or b"")) as tar:
        assert tar.getnames() == ["a.txt"]


@pytest.mark.parametrize(
    ("remote", "timeout_s"),
    [
        (("docker", "compose", "pull"), 600.0),
        (("docker", "compose", "up", "-d"), 300.0),
        (("docker", "compose", "down"), 120.0),
        (("docker", "compose", "-p", "pull", "up", "-d"), 300.0),
        (("cat", "~/docker/x/docker-compose.yaml"), 20.0),
        (("test", "-e", "~/docker/x/docker-compose.yaml"), 20.0),
    ],
)
def test_ssh_uses_a_timeout_matched_to_the_remote_operation(
    remote: tuple[str, ...], timeout_s: float
) -> None:
    recorder = Recorder()

    SshTransport(command=recorder).run(remote)

    assert recorder.timeouts == [timeout_s]


def test_ssh_does_not_pipe_a_tar_into_a_directory_it_could_not_make() -> None:
    recorder = Recorder(ok=False)
    transport = SshTransport(command=recorder)

    assert not transport.send({"a.txt": b"hello"}, "~/docker/x").ok
    assert len(recorder.calls) == 1


def test_reading_over_ssh_is_a_cat_and_a_missing_file_is_none() -> None:
    assert SshTransport(command=Recorder()).read("~/x") == "hello\n"
    assert SshTransport(command=Recorder(ok=False)).read("~/x") is None


class Unreachable:
    """A stand-in for run_argv that answers the way a host nobody can reach does."""

    def __init__(self, *, error: str | None = None, exit_status: int | None = None) -> None:
        """Answer with steward's own diagnostic, or with ssh's reserved status."""
        self.error = error
        self.exit_status = exit_status

    def __call__(
        self,
        argv: Sequence[str],
        timeout_s: float = 20.0,  # noqa: ARG002 — part of the signature run_argv has
        *,
        stdin: bytes | None = None,  # noqa: ARG002 — likewise
    ) -> CommandOutcome:
        """Report the failure without launching anything."""
        return CommandOutcome(argv=tuple(argv), error=self.error, exit_status=self.exit_status)


@pytest.mark.parametrize(
    ("command", "why"),
    [
        (Unreachable(error="cannot launch 'ssh': No such file or directory"), "no ssh at all"),
        (Unreachable(error="'ssh' did not answer within 20s"), "the host never answered"),
        (Unreachable(exit_status=255), "ssh refused the connection, so cat never ran"),
    ],
)
def test_a_host_steward_never_reached_is_not_an_empty_one(command: Unreachable, why: str) -> None:
    """``None`` from ``read`` must mean the file is absent, and nothing weaker.

    ``run`` reports a missing ssh binary, a host that never answered, and an auth refusal
    all as non-ok outcomes. Folding them into ``None`` told the caller the file was not
    there — about a machine steward never reached (steward #136).
    """
    with pytest.raises(TransportError) as raised:
        SshTransport(command=command).read("~/docker/x/compose.yaml")

    assert f"{DEFAULT_USER}@{DEFAULT_HOST}" in str(raised.value), why


class Filesystem:
    """A stand-in for run_argv that answers ``cat`` and ``test -e`` about a fake tree.

    The point is that the two disagree: ``cat`` exits 1 for a file that is missing *and*
    for one it may not open, while ``test -e`` exits 1 only for genuinely not there.
    """

    def __init__(self, *, present: bool, readable: bool) -> None:
        """Describe the one path this fake knows about."""
        self.present = present
        self.readable = readable

    def __call__(
        self,
        argv: Sequence[str],
        timeout_s: float = 20.0,  # noqa: ARG002 — part of the signature run_argv has
        *,
        stdin: bytes | None = None,  # noqa: ARG002 — likewise
    ) -> CommandOutcome:
        """Answer as the remote shell would for this path's state."""
        parts = tuple(argv)
        if "test" in parts:
            asked = parts[-1]
            exists = self.present or asked in {"~", "."}
            return CommandOutcome(argv=parts, exit_status=0 if exists else 1)
        if self.present and self.readable:
            return CommandOutcome(argv=parts, exit_status=0, stdout="services:\n")
        detail = "Permission denied" if self.present else "No such file or directory"
        return CommandOutcome(argv=parts, exit_status=1, stderr=f"cat: {detail}")


def test_a_file_that_is_there_but_unreadable_is_not_reported_as_absent() -> None:
    """``exists`` asks the question ``cat`` cannot answer (steward #136).

    A compose file under a directory steward may not read — root-owned on the NAS, say —
    makes ``cat`` exit 1 with exactly the status a missing file gives. Reading that as
    absence is how ``steward retire`` reported success with the container still running,
    which is the whole bug; fixing only the unreachable-host half would have left the
    same wrong conclusion reachable by a different route.
    """
    unreadable = SshTransport(command=Filesystem(present=True, readable=False))

    assert unreadable.exists("~/docker/x/compose.yaml") is True
    assert SshTransport(command=Filesystem(present=False, readable=False)).exists("~/x") is False


def test_an_unsearchable_parent_is_not_reported_as_an_absent_file() -> None:
    """A false ``test -e`` below a forbidden directory is not proof of absence."""

    def unsearchable(
        argv: Sequence[str],
        timeout_s: float = 20.0,  # noqa: ARG001 — part of the run_argv signature
        *,
        stdin: bytes | None = None,  # noqa: ARG001 — likewise
    ) -> CommandOutcome:
        parts = tuple(argv)
        predicate, candidate = parts[-2], parts[-1]
        if predicate == "-e":
            exists = candidate in {"~/docker", "~", "."}
            return CommandOutcome(argv=parts, exit_status=0 if exists else 1)
        assert predicate == "-x"
        return CommandOutcome(argv=parts, exit_status=1)

    with pytest.raises(TransportError, match="not searchable"):
        SshTransport(command=unsearchable).exists("~/docker/x/compose.yaml")


@pytest.mark.parametrize(
    "command",
    [
        Unreachable(error="'ssh' did not answer within 20s"),
        Unreachable(exit_status=255),
    ],
)
def test_exists_will_not_call_an_unreachable_host_empty_either(command: Unreachable) -> None:
    """The same discipline as ``read``: not reached is never an answer about the host."""
    with pytest.raises(TransportError):
        SshTransport(command=command).exists("~/docker/x/compose.yaml")


def test_a_host_that_answered_and_said_no_is_still_an_empty_one() -> None:
    """The other half: ssh connected, ``cat`` exited non-zero, the file is genuinely gone.

    This is the answer ``read`` exists to give, and the one the fix must not swallow.
    """
    assert SshTransport(command=Unreachable(exit_status=1)).read("~/docker/x/nope") is None


def test_the_default_transport_is_built_from_the_manifest(write_resident) -> None:
    target = target_for(resident(write_resident, deploy={"host": "other-nas"}).manifest)
    built = transport_for(target, {})

    assert isinstance(built, SshTransport)
    assert built.target == "Miha@other-nas"


def test_a_resident_of_this_burrow_is_provisioned_here_not_over_ssh(write_resident) -> None:
    """warren#356: the deployed API cannot ssh to the machine it is on, so it does not.

    STEWARD_BURROW names this machine; a target whose host is that name is *here*. A
    laptop never sets the variable, so from a laptop the NAS stays an ssh target.
    """
    here = target_for(resident(write_resident).manifest)
    elsewhere = target_for(resident(write_resident, deploy={"host": "other-nas"}).manifest)
    burrow = {BURROW_ENV: DEFAULT_HOST, BURROW_HOME_ENV: "/home/Miha"}

    local = transport_for(here, burrow)
    assert isinstance(local, BurrowTransport)
    assert local.home == "/home/Miha"
    assert local.kind == "burrow"
    assert isinstance(transport_for(elsewhere, burrow), SshTransport)
    assert isinstance(transport_for(here, {}), SshTransport), "a laptop reaches the NAS over ssh"


def test_the_burrow_transport_defaults_home_to_the_deploy_user(write_resident) -> None:
    target = target_for(resident(write_resident).manifest)
    built = transport_for(target, {BURROW_ENV: DEFAULT_HOST})
    assert isinstance(built, BurrowTransport)
    assert built.home == f"/home/{DEFAULT_USER}"


# ------------------------------------------------------------------ the burrow transport


def recording(outcomes: list[tuple[tuple[str, ...], float]]):
    """Record what would have run and say it succeeded: a stand-in for run_argv."""

    def command(
        argv: Sequence[str], timeout_s: float = 20.0, *, stdin: bytes | None = None
    ) -> CommandOutcome:
        del stdin  # a fake feeds nothing
        parts = tuple(argv)
        outcomes.append((parts, timeout_s))
        return CommandOutcome(argv=parts, exit_status=0)

    return command


def test_the_burrow_transport_runs_host_paths_with_no_ssh_in_front(write_resident) -> None:
    """The plan is the argv that runs: every ~/ already the host's path, no prefix at all.

    The compose CLI in the API's process resolves the bundle's ./memory against the
    project directory and hands the daemon host paths — so the directory has to be named
    by its host path here, and steward's own $HOME (/root in the image) must never leak in.
    """
    seen: list[tuple[tuple[str, ...], float]] = []
    burrow = BurrowTransport(
        burrow="dxp2800", user="Miha", home="/home/Miha", command=recording(seen)
    )
    target = target_for(resident(write_resident).manifest)

    outcome = burrow.run(compose_argv(target, "up", "-d"))

    assert outcome.ok
    assert seen == [
        (
            (
                "docker",
                "compose",
                "-f",
                "/home/Miha/docker/warren/residents/test-agent/docker-compose.yaml",
                "--project-directory",
                "/home/Miha/docker/warren/residents/test-agent",
                "-p",
                "test-agent",
                "up",
                "-d",
            ),
            300.0,
        )
    ]
    assert burrow.plan(("rm", "-f", "~/docker/x/.env", "~")) == (
        "rm",
        "-f",
        "/home/Miha/docker/x/.env",
        "/home/Miha",
    )
    assert "~" not in " ".join(seen[0][0])


def test_the_burrow_transport_writes_the_bundle_where_the_host_has_it(
    tmp_path: Path, write_resident
) -> None:
    """The same tar ssh would pipe, unpacked in place: .env at 0600, directories made."""
    home = tmp_path / "home" / "Miha"
    burrow = BurrowTransport(burrow="dxp2800", home=str(home), command=recording([]))
    one = resident(write_resident)
    target = target_for(one.manifest)
    files = bundle_for(one, target, VILLAGE)

    assert burrow.exists(target.path) is False
    assert burrow.read(target.compose_path) is None
    assert bundle_changes(burrow, files, target.path) == tuple(
        sorted(n for n in files if not n.endswith("/.keep"))
    )

    sent = burrow.send(files, target.path)

    landed = home / "docker" / "warren" / "residents" / "test-agent"
    assert sent.ok
    assert (landed / COMPOSE_FILENAME).read_bytes() == files[COMPOSE_FILENAME]
    assert (landed / ENV_FILENAME).stat().st_mode & 0o777 == 0o600
    assert (landed / "memory").is_dir()
    assert burrow.exists(target.path) is True
    assert burrow.read(target.compose_path) == files[COMPOSE_FILENAME].decode()
    assert bundle_changes(burrow, files, target.path) == (), "a second run has nothing to send"


def test_the_burrow_transport_reports_a_directory_it_could_not_write(tmp_path: Path) -> None:
    """A failure to land the bundle is an outcome the nursery can name, not an exception."""
    blocked = tmp_path / "file-not-a-directory"
    blocked.write_text("in the way", encoding="utf-8")
    burrow = BurrowTransport(burrow="dxp2800", home=str(tmp_path), command=recording([]))

    outcome = burrow.send({"a.txt": b"hello"}, "~/file-not-a-directory/a")

    assert not outcome.ok
    assert "file-not-a-directory" in outcome.stderr


@pytest.mark.skipif(os.geteuid() == 0, reason="root can look at anything")
def test_the_burrow_transport_fails_closed_on_a_path_it_may_not_inspect(tmp_path: Path) -> None:
    """Steward #136 again: 'forbidden' must never read as 'absent'."""
    sealed = tmp_path / "sealed"
    sealed.mkdir()
    (sealed / "docker-compose.yaml").write_text("services: {}\n", encoding="utf-8")
    sealed.chmod(0)
    burrow = BurrowTransport(burrow="dxp2800", home=str(tmp_path), command=recording([]))
    try:
        with pytest.raises(TransportError, match="cannot inspect"):
            burrow.exists("~/sealed/docker-compose.yaml")
        with pytest.raises(TransportError, match="cannot read"):
            burrow.read("~/sealed/docker-compose.yaml")
    finally:
        sealed.chmod(0o700)


# ------------------------------------------------------------- one home on the burrow


def test_memory_host_dir_resolves_tilde_against_the_burrow_home(write_resident) -> None:
    """The deployed control plane's ~ is /root, which is nobody's home on the host.

    With STEWARD_BURROW_HOME set, every steward process on the burrow computes the path
    the host actually has — the one their compose file mounts at that same path — instead
    of three views of one directory. Without it, this process's own home, as always.
    """
    placed = resident(
        write_resident,
        deploy={"container": "steward-test-agent"},
        runner={"kind": "claude", "model": "claude-haiku-4-5-20251001", "placement": "container"},
    ).manifest

    assert memory_host_dir(placed, {BURROW_HOME_ENV: "/home/Miha"}) == Path(
        "/home/Miha/docker/warren/residents/test-agent/memory"
    )
    assert (
        memory_host_dir(placed, {})
        == Path("~/docker/warren/residents/test-agent/memory").expanduser()
    )
    local = resident(
        write_resident, memory={"kind": "directory", "path": "~/notes/memory"}
    ).manifest
    assert memory_host_dir(local, {BURROW_HOME_ENV: "/home/Miha"}) == Path(
        "/home/Miha/notes/memory"
    )
    assert memory_host_dir(local, {}) == Path("~/notes/memory").expanduser()
