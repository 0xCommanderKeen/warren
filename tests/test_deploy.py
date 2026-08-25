"""The transport seam and the compose fragment: everything the nursery ships, without a NAS."""

import io
import tarfile
from collections.abc import Sequence
from pathlib import Path

import pytest
import yaml

from conftest import ResidentWriter, valid_manifest
from steward.deploy import (
    BURROW_TOKEN_ENV,
    BURROW_URL_ENV,
    COMPOSE_FILENAME,
    DEFAULT_HOST,
    DEFAULT_USER,
    ENV_FILENAME,
    LocalTransport,
    SshTransport,
    TransportError,
    bundle_changes,
    bundle_for,
    burrow_env,
    compose_argv,
    pack,
    render_compose,
    render_env,
    target_for,
    transport_for,
)
from steward.manifest import Resident, load_manifest, validate_manifest
from steward.runners import CommandOutcome

VILLAGE = {BURROW_URL_ENV: "http://dxp2800:8737", BURROW_TOKEN_ENV: "s3cret-village-token"}


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

    assert "${BURROW_TOKEN-}" in text
    assert "${BURROW_URL:?" in text
    assert VILLAGE[BURROW_TOKEN_ENV] not in text


def test_rendering_the_same_resident_twice_gives_the_same_bytes(write_resident) -> None:
    """Convergence is decided by comparison, so a render that drifted would break it."""
    one = resident(write_resident)
    target = target_for(one.manifest)

    assert render_compose(one, target) == render_compose(one, target)
    assert pack(bundle_for(one, target, VILLAGE)) == pack(bundle_for(one, target, VILLAGE))


def test_the_env_file_is_sorted_lines_and_nothing_else() -> None:
    assert render_env(VILLAGE) == (
        "BURROW_TOKEN=s3cret-village-token\nBURROW_URL=http://dxp2800:8737\n"
    )
    assert render_env({}) == ""


def test_a_secret_with_a_line_break_in_it_is_refused() -> None:
    """A .env has no quoting, so a second line would silently become a second variable."""
    with pytest.raises(TransportError, match="line break"):
        render_env({BURROW_TOKEN_ENV: "one\ntwo"})


def test_a_village_with_no_address_is_refused_before_anything_is_built() -> None:
    with pytest.raises(TransportError, match=BURROW_URL_ENV):
        burrow_env({})


def test_a_village_with_no_token_is_allowed_and_says_so() -> None:
    """Burrow's ingest is open when its own token is unset; that is a real deployment."""
    assert burrow_env({BURROW_URL_ENV: "http://dxp2800:8737"}) == {
        BURROW_URL_ENV: "http://dxp2800:8737"
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
        self.ok = ok

    def __call__(
        self,
        argv: Sequence[str],
        timeout_s: float = 20.0,  # noqa: ARG002 — part of the signature run_argv has
        *,
        stdin: bytes | None = None,
    ) -> CommandOutcome:
        """Record the call and answer without launching anything."""
        self.calls.append(tuple(argv))
        self.stdin.append(stdin)
        return CommandOutcome(argv=tuple(argv), exit_status=0 if self.ok else 1, stdout="hello\n")


def test_ssh_puts_the_user_and_host_in_front_of_every_command() -> None:
    recorder = Recorder()
    transport = SshTransport(host="dxp2800", user="Miha", command=recorder)

    transport.run(["docker", "ps"])

    assert recorder.calls == [("ssh", "Miha@dxp2800", "docker", "ps")]
    assert transport.plan(["docker", "ps"])[:2] == ("ssh", "Miha@dxp2800")
    assert transport.describe() == "ssh Miha@dxp2800"


def test_ssh_ships_files_as_a_tar_on_stdin_because_scp_is_broken() -> None:
    recorder = Recorder()
    transport = SshTransport(command=recorder)

    outcome = transport.send({"a.txt": b"hello"}, "~/docker/x")

    assert outcome.ok
    assert recorder.calls[0] == ("ssh", "Miha@dxp2800", "mkdir", "-p", "~/docker/x")
    assert recorder.calls[1] == ("ssh", "Miha@dxp2800", "tar", "-xf", "-", "-C", "~/docker/x")
    with tarfile.open(fileobj=io.BytesIO(recorder.stdin[1] or b"")) as tar:
        assert tar.getnames() == ["a.txt"]


def test_ssh_does_not_pipe_a_tar_into_a_directory_it_could_not_make() -> None:
    recorder = Recorder(ok=False)
    transport = SshTransport(command=recorder)

    assert not transport.send({"a.txt": b"hello"}, "~/docker/x").ok
    assert len(recorder.calls) == 1


def test_reading_over_ssh_is_a_cat_and_a_missing_file_is_none() -> None:
    assert SshTransport(command=Recorder()).read("~/x") == "hello\n"
    assert SshTransport(command=Recorder(ok=False)).read("~/x") is None


def test_the_default_transport_is_built_from_the_manifest(write_resident) -> None:
    target = target_for(resident(write_resident, deploy={"host": "other-nas"}).manifest)
    built = transport_for(target)

    assert isinstance(built, SshTransport)
    assert built.target == "Miha@other-nas"
