"""The transport seam and the compose fragment: everything the nursery ships, without a NAS."""

import io
import tarfile
from collections.abc import Sequence
from dataclasses import replace
from pathlib import Path

import pytest
import yaml

from conftest import ResidentWriter, valid_manifest
from steward.deploy import (
    CHRONICLE_TOKEN_ENV,
    CHRONICLE_URL_ENV,
    COMPOSE_FILENAME,
    DEFAULT_HOST,
    DEFAULT_USER,
    ENV_FILENAME,
    LEGACY_TOKEN_ENV,
    LEGACY_URL_ENV,
    LocalTransport,
    SshTransport,
    TransportError,
    bundle_changes,
    bundle_for,
    compose_argv,
    emitter_env,
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
    assert target.path == "~/docker/steward-test-agent"
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
    # The frozen vendored emitter in the image reads only the old spelling.
    assert service["environment"]["BURROW_AGENT_ID"] == "claude-code:test-agent"
    assert service["environment"]["BURROW_PROJECT"] == "test-agent"
    assert service["command"] == ["sleep", "infinity"]
    assert "./memory:/data/residents/test-agent/memory" in service["volumes"]
    assert "./claude:/root/.claude" in service["volumes"]


def test_the_compose_fragment_references_the_secrets_instead_of_carrying_them(
    write_resident,
) -> None:
    """The token reaches the container through .env, and the compose file only points at it."""
    one = resident(write_resident)
    text = render_compose(one, target_for(one.manifest))

    assert "${CHRONICLE_TOKEN-}" in text
    assert "${CHRONICLE_URL:?" in text
    assert "${BURROW_TOKEN-}" in text
    assert "${BURROW_URL:?" in text
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
        LEGACY_URL_ENV: "http://dxp2800:8737",
    }


def test_the_env_file_carries_both_spellings_at_the_same_value() -> None:
    """The emitter in the image is frozen at the pre-rename spelling (warren#234).

    A resident container runs docker/resident/burrow-emit.py, a pinned copy that predates
    warren#216 and reads BURROW_* only. Writing just the new names would leave every
    deployed resident posting nowhere, and it would do it silently: the container starts,
    the agent works, the events go to a file nobody reads.
    """
    values = emitter_env({CHRONICLE_URL_ENV: "http://dxp2800:8737", CHRONICLE_TOKEN_ENV: "s3cret"})
    assert values == {
        CHRONICLE_URL_ENV: "http://dxp2800:8737",
        LEGACY_URL_ENV: "http://dxp2800:8737",
        CHRONICLE_TOKEN_ENV: "s3cret",
        LEGACY_TOKEN_ENV: "s3cret",
    }


def test_stewards_own_pre_rename_environment_still_provisions() -> None:
    """Steward on the NAS is itself configured under the old spelling."""
    values = emitter_env({LEGACY_URL_ENV: "http://dxp2800:8737", LEGACY_TOKEN_ENV: "s3cret"})
    assert values[CHRONICLE_URL_ENV] == "http://dxp2800:8737"
    assert values[LEGACY_URL_ENV] == "http://dxp2800:8737"
    assert values[CHRONICLE_TOKEN_ENV] == "s3cret"


def test_the_new_spelling_wins_wherever_both_are_set() -> None:
    values = emitter_env({CHRONICLE_URL_ENV: "http://new:8737", LEGACY_URL_ENV: "http://old:8737"})
    assert values == {
        CHRONICLE_URL_ENV: "http://new:8737",
        LEGACY_URL_ENV: "http://new:8737",
    }


def test_compose_commands_name_the_file_and_the_project_explicitly(write_resident) -> None:
    """There is no shell on the far side to `cd` in, so both are absolute."""
    target = target_for(resident(write_resident).manifest)
    argv = compose_argv(target, "up", "-d")

    assert argv == (
        "docker",
        "compose",
        "-f",
        "~/docker/steward-test-agent/docker-compose.yaml",
        "--project-directory",
        "~/docker/steward-test-agent",
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
    landed = tmp_path / "host" / "docker" / "steward-test-agent"
    assert (landed / COMPOSE_FILENAME).is_file()
    assert host.read(target.env_path) == render_env(VILLAGE)
    assert host.sent == ["~/docker/steward-test-agent"]


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
    built = transport_for(target)

    assert isinstance(built, SshTransport)
    assert built.target == "Miha@other-nas"
