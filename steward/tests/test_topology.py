"""Whether the docker this process reaches holds the containers manifests name (#59).

The whole module is a report, so every test here is about what a human reads and what it
is derived from. Docker is a fake in all of them — the one test that runs a real docker
client lives in ``test_container_integration.py``, where the rest of the daemon-shaped
tests already are.
"""

import socket
from collections.abc import Sequence
from pathlib import Path

import pytest

from conftest import SECOND_RESIDENT_UID, VALID_SOUL, ResidentWriter, valid_manifest
from steward import topology as t
from steward.manifest import Resident, load_manifest
from steward.runners import CommandOutcome

#: The daemon reading a healthy docker on the NAS would give.
NAS_DAEMON = "dxp2800\t27.3.1\n"


class FakeDocker:
    """A docker that answers one fixed way and remembers every argv it was handed."""

    def __init__(self, stdout: str = NAS_DAEMON, *, status: int = 0) -> None:
        """Answer with this ``docker info`` output and this exit status, recording nothing yet."""
        self.stdout = stdout
        self.status = status
        self.calls: list[tuple[str, ...]] = []

    def __call__(self, argv: Sequence[str]) -> CommandOutcome:
        """Record the call and answer without launching anything."""
        self.calls.append(tuple(argv))
        return CommandOutcome(argv=tuple(argv), exit_status=self.status, stdout=self.stdout)


def supervised_resident(write_resident: ResidentWriter, **deploy: object) -> Resident:
    """Write a resident that declares a container, so there is something to supervise."""
    data = valid_manifest()
    data["deploy"] = {"container": "steward-test-agent", **deploy}
    return load_manifest(write_resident(data))


def local_resident(write_resident: ResidentWriter) -> Resident:
    """Write a resident with no deploy block at all — the pip/burrow-builder shape."""
    return load_manifest(write_resident(valid_manifest()))


# ------------------------------------------------------------------- naming this burrow


def test_the_burrow_is_this_machine_when_nobody_said_otherwise() -> None:
    """The fallback nobody has to configure: what the machine calls itself."""
    assert t.this_burrow({}) == socket.gethostname()
    assert socket.gethostname().casefold() in t.burrow_names({})


def test_a_declared_burrow_replaces_the_hostname_rather_than_joining_it() -> None:
    """An operator who names their burrow is saying the hostname is the wrong answer."""
    names = t.burrow_names({t.BURROW_ENV: "dxp2800"})

    assert names == frozenset({"dxp2800"}) | t.LOOPBACK_NAMES
    assert t.this_burrow({t.BURROW_ENV: "dxp2800"}) == "dxp2800"


def test_a_blank_declaration_is_no_declaration() -> None:
    """`STEWARD_BURROW=` is not a burrow named after the empty string."""
    assert t.this_burrow({t.BURROW_ENV: "   "}) == socket.gethostname()


def test_a_docker_host_is_reported_only_when_it_actually_points_somewhere() -> None:
    assert t.docker_endpoint({}) is None
    assert t.docker_endpoint({t.DOCKER_HOST_ENV: ""}) is None
    assert t.docker_endpoint({t.DOCKER_HOST_ENV: "ssh://Miha@dxp2800"}) == "ssh://Miha@dxp2800"


# --------------------------------------------------------------- what needs supervising


def test_a_resident_that_declares_no_container_is_nothing_to_supervise(
    write_resident: ResidentWriter,
) -> None:
    """And docker is never asked: a laptop fleet must not wait on a daemon it does not need."""
    docker = FakeDocker()

    report = t.survey([local_resident(write_resident)], env={}, command=docker)

    assert report.supervised == ()
    assert docker.calls == []
    assert report.unreachable == ()
    (note,) = report.notes()
    assert note.ok
    assert "nothing here needs docker" in note.text


def test_a_retired_resident_is_left_out_like_the_watchdog_leaves_it_out(
    write_resident: ResidentWriter,
) -> None:
    """`steward retire` removes the container, so its absence is the lifecycle working."""
    data = valid_manifest()
    data["deploy"] = {"container": "steward-test-agent"}
    data["retired"] = True
    docker = FakeDocker()

    report = t.survey([load_manifest(write_resident(data))], env={}, command=docker)

    assert report.supervised == ()
    assert docker.calls == []


def test_docker_is_asked_exactly_once_however_many_containers_there_are(
    write_resident: ResidentWriter, tmp_path: Path
) -> None:
    """The daemon's identity is a property of this process, not of each resident."""
    first = supervised_resident(write_resident)
    second_data = valid_manifest()
    second_data["id"] = "second-agent"
    second_data["uid"] = SECOND_RESIDENT_UID
    second_data["agent_id"] = "claude-code:second-agent"
    second_data["deploy"] = {"container": "steward-second-agent"}
    second_soul = VALID_SOUL.replace("claude-code:test-agent", "claude-code:second-agent")
    second = load_manifest(write_resident(second_data, soul=second_soul, root=tmp_path / "more"))
    docker = FakeDocker()

    report = t.survey([first, second], env={t.BURROW_ENV: "dxp2800"}, command=docker)

    assert docker.calls == [("docker", "info", "--format", t.DAEMON_FORMAT)]
    assert [item.resident_id for item in report.supervised] == ["test-agent", "second-agent"]


# ------------------------------------------------------------------------ what it reaches


def test_a_container_on_this_burrow_is_supervised_from_here(
    write_resident: ResidentWriter,
) -> None:
    report = t.survey(
        [supervised_resident(write_resident)],
        env={t.BURROW_ENV: "dxp2800"},
        command=FakeDocker(),
    )

    (item,) = report.supervised
    assert item.reach is t.Reach.HERE
    assert report.unreachable == ()
    assert any("supervised from here" in note.text and note.ok for note in report.notes())


def test_a_container_on_another_burrow_is_called_out_by_name(
    write_resident: ResidentWriter,
) -> None:
    """The #59 gap itself: local docker, a container that lives on the NAS, and silence."""
    report = t.survey(
        [supervised_resident(write_resident, host="dxp2800")],
        env={t.BURROW_ENV: "laptop"},
        # A docker that answers happily — about the wrong machine, which is the trap.
        command=FakeDocker("laptop\t27.3.1\n"),
    )

    (item,) = report.supervised
    assert item.reach is t.Reach.ELSEWHERE
    assert report.unreachable == (item,)
    (complaint,) = [note for note in report.notes() if not note.ok]
    assert "runs on dxp2800" in complaint.text
    assert "the watchdog cannot see it" in complaint.text
    assert "Run steward's daemons on dxp2800" in complaint.text


def test_what_docker_says_about_itself_beats_what_the_hostname_says(
    write_resident: ResidentWriter,
) -> None:
    """A measurement outranks a guess: the daemon names the machine being supervised.

    This is what keeps a NAS whose `hostname` is not its tailnet name from reporting its
    own containers as unreachable — the far more damaging direction of a wrong answer.
    """
    report = t.survey(
        [supervised_resident(write_resident, host="dxp2800")],
        env={t.BURROW_ENV: "some-other-name"},
        command=FakeDocker("DXP2800\t27.3.1\n"),
    )

    (item,) = report.supervised
    assert item.reach is t.Reach.HERE


def test_a_docker_host_pointer_is_unverified_rather_than_fine(
    write_resident: ResidentWriter,
) -> None:
    """Steward will not claim an endpoint it cannot prove reaches the declared host."""
    report = t.survey(
        [supervised_resident(write_resident, host="dxp2800")],
        env={t.BURROW_ENV: "laptop", t.DOCKER_HOST_ENV: "ssh://Miha@dxp2800"},
        command=FakeDocker("\t27.3.1\n"),
    )

    (item,) = report.supervised
    assert item.reach is t.Reach.UNVERIFIED
    assert report.unreachable == report.supervised
    (complaint,) = [note for note in report.notes() if not note.ok]
    assert "DOCKER_HOST=ssh://Miha@dxp2800" in complaint.text
    assert "cannot verify" in complaint.text


def test_being_on_the_right_burrow_proves_nothing_once_docker_host_is_set(
    write_resident: ResidentWriter,
) -> None:
    """The module's own false-fine, and the reason the three signals are ranked.

    A watchdog running *on the NAS* with `DOCKER_HOST` pointed at some other daemon
    supervises nothing on the NAS — so answering from what this machine is called would
    print "supervised from here" over exactly the gap #59 is about. The local name is only
    consulted once the calls are known to be local.
    """
    report = t.survey(
        [supervised_resident(write_resident, host="dxp2800")],
        env={t.BURROW_ENV: "dxp2800", t.DOCKER_HOST_ENV: "tcp://elsewhere:2375"},
        command=FakeDocker("something-else\t27.3.1\n"),
    )

    (item,) = report.supervised
    assert item.reach is t.Reach.UNVERIFIED
    assert report.unreachable == report.supervised


def test_a_docker_host_that_answers_for_the_declared_host_is_reached_after_all(
    write_resident: ResidentWriter,
) -> None:
    """`DOCKER_HOST` is unverifiable in general and provable in the one case it names itself."""
    report = t.survey(
        [supervised_resident(write_resident, host="dxp2800")],
        env={t.BURROW_ENV: "laptop", t.DOCKER_HOST_ENV: "ssh://Miha@dxp2800"},
        command=FakeDocker(NAS_DAEMON),
    )

    (item,) = report.supervised
    assert item.reach is t.Reach.HERE
    assert report.unreachable == ()


# ---------------------------------------------------------------- when docker says nothing


def test_a_docker_that_does_not_answer_names_what_is_left_unsupervised(
    write_resident: ResidentWriter,
) -> None:
    report = t.survey(
        [supervised_resident(write_resident)],
        env={t.BURROW_ENV: "dxp2800"},
        command=FakeDocker("", status=1),
    )

    assert not report.daemon.answered
    assert report.unreachable == report.supervised
    complaint = report.notes()[0]
    assert not complaint.ok
    assert "docker did not answer" in complaint.text
    assert "nothing is supervising test-agent" in complaint.text
    # And the resident's own line must not then say the opposite two lines later. The
    # container is on this burrow — which is not supervision, because no docker on this
    # burrow answered for it.
    resident_line = report.notes()[1]
    assert not resident_line.ok
    assert "the right burrow, but no docker here answered for it" in resident_line.text
    assert "supervised from here" not in resident_line.text


def test_a_docker_that_exits_zero_with_no_server_did_not_answer(
    write_resident: ResidentWriter,
) -> None:
    """The trap the status alone walks into, and the reason this is not a status check.

    Measured against docker 27.3.1: `docker info` against an unreachable `DOCKER_HOST`
    prints the client's half of the report and exits **0**, with the server fields empty.
    Believing the status there would report a client talking to itself as a healthy
    daemon — the exact false "everything is fine" this module exists to stop telling. The
    real client's behaviour is pinned in `test_container_integration.py`.
    """
    report = t.survey(
        [supervised_resident(write_resident, host="dxp2800")],
        env={t.BURROW_ENV: "laptop", t.DOCKER_HOST_ENV: "tcp://127.0.0.1:1"},
        command=FakeDocker("\t\n"),
    )

    assert not report.daemon.answered
    assert report.daemon.complaint == t.NO_SERVER
    assert report.unreachable == report.supervised
    assert "no server version" in report.notes()[0].text


def test_an_unanswerable_docker_never_makes_a_container_look_reachable(
    write_resident: ResidentWriter,
) -> None:
    """The daemon name is the only measured signal; without it, the declaration decides."""
    report = t.survey(
        [supervised_resident(write_resident, host="dxp2800")],
        env={t.BURROW_ENV: "laptop"},
        command=FakeDocker("", status=1),
    )

    (item,) = report.supervised
    assert item.reach is t.Reach.ELSEWHERE


def test_the_docker_line_never_carries_the_daemons_own_output(
    write_resident: ResidentWriter,
) -> None:
    """`summary()` is the boundary: a report says the class of failure, not the child's text."""

    def leaky(argv: Sequence[str]) -> CommandOutcome:
        return CommandOutcome(
            argv=tuple(argv),
            exit_status=1,
            stderr="Cannot connect to the Docker daemon at tcp://SECRET-HOST:2375",
        )

    report = t.survey([supervised_resident(write_resident)], env={}, command=leaky)

    assert "SECRET-HOST" not in report.notes()[0].text
    assert "exit status 1" in report.notes()[0].text


def test_a_daemon_that_did_not_answer_never_describes_itself_as_one_that_did() -> None:
    """`describe()` must not turn a failed probe into a nameless-but-present daemon."""
    assert t.Daemon(complaint="exit status 1").describe() == "no answer"


def test_the_report_names_the_docker_it_reached(write_resident: ResidentWriter) -> None:
    report = t.survey(
        [supervised_resident(write_resident)],
        env={t.BURROW_ENV: "dxp2800"},
        command=FakeDocker(),
    )

    assert "dxp2800's own docker" in report.notes()[0].text
    assert "answers as dxp2800 27.3.1" in report.notes()[0].text


def test_a_daemon_that_will_not_name_itself_is_still_reported(
    write_resident: ResidentWriter,
) -> None:
    """Docker Desktop answers `docker-desktop`; a rootless daemon can answer nothing at all."""
    report = t.survey(
        [supervised_resident(write_resident)],
        env={t.BURROW_ENV: "dxp2800"},
        command=FakeDocker("\t27.3.1\n"),
    )

    assert "an unnamed daemon 27.3.1" in report.notes()[0].text


@pytest.mark.parametrize("placement", ["local", "container"])
def test_container_placement_needs_a_reachable_docker_too(
    write_resident: ResidentWriter, placement: str
) -> None:
    """Placement is the other half of #58; both halves shell out to the same docker."""
    data = valid_manifest()
    data["deploy"] = {"container": "named-by-hand"}
    data["runner"] = {"kind": "claude", "placement": placement}
    docker = FakeDocker()

    report = t.survey([load_manifest(write_resident(data))], env={}, command=docker)

    (item,) = report.supervised
    assert item.container == "named-by-hand"
    assert docker.calls


def test_a_resident_with_no_declared_container_is_never_given_the_default_name(
    write_resident: ResidentWriter,
) -> None:
    """`target_for` would invent `steward-<id>`, and the watchdog disowns that name.

    `DockerSupervisor.owns` reads `deploy.container` and nothing else, so a report that
    named the default would be promising supervision of a container the supervisor will
    never try. An absent `deploy` block means unsupervised, exactly as `docs/manifest.md`
    says.
    """
    resident = local_resident(write_resident)

    assert not t.supervises(resident.manifest)
    assert t.survey([resident], env={}, command=FakeDocker()).supervised == ()
