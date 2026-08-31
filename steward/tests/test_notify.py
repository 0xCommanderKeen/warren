"""Notifications: where a tap goes, what it says, and what must never fail because of one."""

import json
import logging
import re
import threading
import uuid
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any

import pytest

from conftest import ResidentWriter, valid_manifest
from steward import events as ev
from steward import notify as nf
from steward.manifest import (
    NOTIFICATION_KINDS,
    NOTIFICATION_TRANSPORTS,
    SECRET_REDACTION,
    ResidentManifest,
    load_manifest,
)

UID = "7e36d76a-1ad8-4d65-a619-8c6e7fb93ed9"

#: ntfy's own topic rule. A derived topic that does not match this is a topic the server
#: refuses, which would be a notification that silently never arrives.
NTFY_TOPIC_RE = re.compile(r"^[-_A-Za-z0-9]{1,64}$")


def manifest(**overrides: object) -> ResidentManifest:
    """Build a validated manifest, with whatever the test wants to vary."""
    data = valid_manifest()
    data.update(overrides)
    return ResidentManifest.model_validate(data)


def tapping(
    *on: str, status: str = "active", note: str | None = None, **overrides: object
) -> ResidentManifest:
    """Build a manifest that has opted into ntfy taps for the named kinds."""
    return manifest(
        notifications={
            "transport": "ntfy",
            "on": list(on) or ["needs_human"],
            "status": status,
            "note": note,
        },
        **overrides,
    )


def needs_human(**payload: Any) -> ev.Event:  # noqa: ANN401 — one payload field per keyword
    """Build the knock the approval transition emits."""
    return ev.needs_human_event(
        message=payload.pop("message", "Testy wants to send email"),
        request_id=payload.pop("request_id", "req-1"),
        action=payload.pop("action", "send_email"),
        agent_id="claude-code:test-agent",
        project="test-agent",
        **payload,
    )


def task_done(**payload: Any) -> ev.Event:  # noqa: ANN401 — one payload field per keyword
    """Build the close the board publishes."""
    return ev.task_done_event(
        task_id=payload.pop("task_id", "task-1"),
        title=payload.pop("title", "Summarise the day"),
        claimant="claude-code:test-agent",
        project="test-agent",
        **payload,
    )


class FakeTransport:
    """A transport that records instead of sending. The seam a test stands in."""

    def __init__(self, *, delivers: bool = True, raises: bool = False) -> None:
        """Decide up front how this transport behaves, and start with nothing sent."""
        self.sent: list[nf.Tap] = []
        self.delivers = delivers
        self.raises = raises

    @property
    def name(self) -> str:
        """Answer to the manifest word that selects it."""
        return nf.NTFY

    def address(self, manifest: ResidentManifest) -> str:
        """Say where it would have sent, without deriving anything real."""
        return f"fake://{manifest.id}"

    def send(self, manifest: ResidentManifest, tap: nf.Tap) -> bool:  # noqa: ARG002
        """Record the tap, and behave the way the test asked."""
        if self.raises:
            raise RuntimeError("the transport exploded")
        self.sent.append(tap)
        return self.delivers


@pytest.fixture
def transport() -> FakeTransport:
    return FakeTransport()


@pytest.fixture
def notifier(transport: FakeTransport) -> nf.Notifier:
    return nf.Notifier({nf.NTFY: transport})


# ------------------------------------------------------------------ the derived topic


def test_the_topic_is_derived_stably_and_is_a_name_ntfy_accepts() -> None:
    topic = nf.ntfy_topic(UID)
    assert topic == nf.ntfy_topic(UID)
    assert topic.startswith(nf.TOPIC_PREFIX)
    assert NTFY_TOPIC_RE.match(topic)
    assert len(topic) == len(nf.TOPIC_PREFIX) + nf.TOPIC_CHARS


def test_the_topic_never_carries_the_uid_it_was_derived_from() -> None:
    """The whole argument for hashing: showing the uid must not disclose the topic."""
    topic = nf.ntfy_topic(UID)
    assert UID not in topic
    assert UID.replace("-", "") not in topic
    assert UID.partition("-")[0] not in topic


def test_two_residents_get_two_topics() -> None:
    assert nf.ntfy_topic(UID) != nf.ntfy_topic("3a78217a-df03-4f3b-a46a-4c75b4ad929f")


def test_the_topic_does_not_change_when_the_uid_is_merely_respelled() -> None:
    """An operator who resubscribed because somebody upper-cased a UUID would not forgive it."""
    assert nf.ntfy_topic(UID.upper()) == nf.ntfy_topic(UID)
    assert nf.ntfy_topic(uuid.UUID(UID)) == nf.ntfy_topic(UID)
    assert nf.ntfy_topic(f"{{{UID}}}") == nf.ntfy_topic(UID)


def test_a_namespace_separates_two_installations_reading_one_tree() -> None:
    """The laptop checkout and the NAS hold the same uids; they must not share a phone."""
    assert nf.ntfy_topic(UID, "laptop") != nf.ntfy_topic(UID)
    assert nf.ntfy_topic(UID, "laptop") != nf.ntfy_topic(UID, "nas")
    assert nf.ntfy_topic(UID, "laptop") == nf.ntfy_topic(UID, "laptop")


def test_a_uid_that_is_not_a_uuid_still_derives_a_usable_topic() -> None:
    """Unreachable from a validated manifest, and it must not raise if it ever happens."""
    topic = nf.ntfy_topic("not-a-uuid-at-all")
    assert NTFY_TOPIC_RE.match(topic)
    assert topic == nf.ntfy_topic("not-a-uuid-at-all")


# ------------------------------------------------------------------ what a tap says


def test_a_knock_leads_with_the_message_and_carries_what_answers_it() -> None:
    tap = nf.tap_for(
        manifest(), needs_human(detail={"to": "anna@example.com"}, options=["approve"])
    )
    assert tap is not None
    assert tap.kind == ev.NEEDS_HUMAN
    assert tap.title == "Testy wants to send email"
    assert tap.priority == nf.PRIORITY_HIGH
    assert tap.tags == ("door",)
    assert "action: send_email" in tap.body
    assert "anna@example.com" in tap.body
    assert "options: approve" in tap.body
    assert "request: req-1" in tap.body


def test_a_knock_with_no_detail_still_reads_as_a_sentence() -> None:
    tap = nf.tap_for(manifest(), needs_human(detail=None))
    assert tap is not None
    assert tap.body.startswith("Testy · test-agent")
    assert "expires:" not in tap.body


def test_a_knock_names_its_deadline_when_it_has_one() -> None:
    tap = nf.tap_for(manifest(), needs_human(expires_at="2026-08-31T12:00:00.000Z"))
    assert tap is not None
    assert "expires: 2026-08-31T12:00:00.000Z" in tap.body


def test_a_finished_task_names_the_resident_and_the_work() -> None:
    tap = nf.tap_for(manifest(), task_done(artifacts=["summary.md"]))
    assert tap is not None
    assert tap.kind == ev.TASK_DONE
    assert tap.title == "Testy finished: Summarise the day"
    assert tap.priority == nf.PRIORITY_DEFAULT
    assert "task: task-1" in tap.body
    assert "artifacts: summary.md" in tap.body


def test_a_finished_task_with_no_artifacts_says_nothing_about_them() -> None:
    tap = nf.tap_for(manifest(), task_done())
    assert tap is not None
    assert "artifacts" not in tap.body


def test_an_event_nobody_should_be_woken_for_is_not_a_tap() -> None:
    context = ev.RunContext(
        agent_id="claude-code:test-agent",
        project="test-agent",
        routine="daily-summary",
        run_id="run-1",
    )
    assert nf.tap_for(manifest(), context.started("schedule")) is None
    finished = context.finished(outcome="ok", artifacts=[], duration_s=1.0)
    assert nf.tap_for(manifest(), finished) is None
    assert nf.tap_for(manifest(), context.failed(error="boom", duration_s=1.0)) is None


# ------------------------------------------------------------------ redaction


def test_a_secret_in_a_finished_task_is_scrubbed_before_it_reaches_a_phone() -> None:
    """``task_done`` is *not* redacted on its way to chronicle, so it is redacted here."""
    tap = nf.tap_for(
        manifest(),
        task_done(title="pushed with sk-ant-api03-AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"),
    )
    assert tap is not None
    assert "sk-ant-api03" not in tap.title
    assert SECRET_REDACTION in tap.title


def test_a_secret_in_an_artifact_path_is_scrubbed_too() -> None:
    tap = nf.tap_for(manifest(), task_done(artifacts=["ANTHROPIC_API_KEY=sk-live-abcdefghijkl"]))
    assert tap is not None
    assert "sk-live-abcdefghijkl" not in tap.body
    assert SECRET_REDACTION in tap.body


def test_redaction_happens_before_the_cap_so_no_live_prefix_survives() -> None:
    """Redact-then-bound. Bounding first would leave the first half of a live key showing."""
    key = "sk-ant-api03-" + "B" * 60
    padding = "x" * (nf.TITLE_MAX_CHARS - 20)
    tap = nf.tap_for(manifest(), task_done(title=f"{padding}{key}"))
    assert tap is not None
    assert "sk-ant-api03" not in tap.title
    assert "sk-ant" not in tap.title
    assert len(tap.title) <= nf.TITLE_MAX_CHARS


def test_a_very_long_body_is_bounded() -> None:
    tap = nf.tap_for(manifest(), needs_human(detail={"note": "y" * 50_000}))
    assert tap is not None
    assert len(tap.body) <= nf.BODY_MAX_CHARS


# ------------------------------------------------------------------ header safety


def test_a_title_carrying_a_newline_cannot_write_a_header() -> None:
    """A session writes the message this becomes; a header value it controls is injection."""
    value = nf._header_text("wants to send email\r\nPriority: max\nTags: skull")
    assert "\n" not in value
    assert "\r" not in value
    assert value == "wants to send email Priority: max Tags: skull"


def test_a_title_with_an_accent_is_encoded_rather_than_mangled() -> None:
    value = nf._header_text("Miha wants to send Žito an email")
    assert value.isascii()
    assert value.startswith("=?utf-8?")


def test_a_header_value_is_bounded() -> None:
    assert len(nf._header_text("z" * 5000)) == nf.TITLE_MAX_CHARS


# ------------------------------------------------------------------ the ntfy transport


@pytest.fixture
def ntfy_server() -> Iterator[tuple[str, list[dict[str, Any]]]]:
    """Run a real HTTP server that answers like ntfy, and record what it was sent."""
    seen: list[dict[str, Any]] = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            length = int(self.headers["Content-Length"])
            seen.append(
                {
                    "path": self.path,
                    "headers": dict(self.headers),
                    "body": self.rfile.read(length).decode("utf-8"),
                }
            )
            self.send_response(200)
            self.end_headers()

        def log_message(self, format: str, *args: object) -> None:  # noqa: A002
            """Keep the test output quiet."""

    server = HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", seen
    finally:
        server.shutdown()
        server.server_close()


def test_a_reachable_ntfy_gets_the_tap_at_the_derived_topic(
    ntfy_server: tuple[str, list[dict[str, Any]]],
) -> None:
    url, seen = ntfy_server
    resident = tapping()
    transport = nf.NtfyTransport(base_url=url, token="s3cret")
    tap = nf.tap_for(resident, needs_human())
    assert tap is not None

    assert transport.send(resident, tap) is True

    request = seen[0]
    assert request["path"] == f"/{nf.ntfy_topic(resident.uid)}"
    assert request["headers"]["Title"] == "Testy wants to send email"
    assert request["headers"]["Priority"] == "high"
    assert request["headers"]["Tags"] == "door"
    assert request["headers"]["Authorization"] == "Bearer s3cret"
    assert request["body"] == tap.body


def test_a_tap_with_no_tags_sends_no_tags_header(
    ntfy_server: tuple[str, list[dict[str, Any]]],
) -> None:
    url, seen = ntfy_server
    resident = tapping()
    plain = nf.Tap(kind="test", title="plain", body="nothing to see")
    assert nf.NtfyTransport(base_url=url).send(resident, plain) is True
    assert "Tags" not in seen[0]["headers"]


def test_an_untokened_transport_sends_no_authorization(
    ntfy_server: tuple[str, list[dict[str, Any]]],
) -> None:
    url, seen = ntfy_server
    resident = tapping()
    nf.NtfyTransport(base_url=url).send(resident, nf.probe_tap(resident))
    assert "Authorization" not in seen[0]["headers"]
    assert "test" in seen[0]["body"]


def test_an_unreachable_ntfy_is_a_false_and_a_log_line_and_nothing_else(
    caplog: pytest.LogCaptureFixture,
) -> None:
    resident = tapping()
    transport = nf.NtfyTransport(base_url="http://127.0.0.1:1")
    with caplog.at_level(logging.WARNING, logger="steward.notify"):
        assert transport.send(resident, nf.probe_tap(resident)) is False
    assert "could not tap" in caplog.text
    assert resident.id in caplog.text


def test_a_failed_send_stops_trying_for_a_while(
    ntfy_server: tuple[str, list[dict[str, Any]]],
) -> None:
    """A dead ntfy costs one timeout a minute, not one per knock in a storm of them."""
    url, seen = ntfy_server
    now = 0.0
    resident = tapping()
    transport = nf.NtfyTransport(base_url="http://127.0.0.1:1", clock=lambda: now)
    tap = nf.probe_tap(resident)

    assert transport.send(resident, tap) is False  # tries, fails, trips the breaker
    transport.base_url = url
    now = nf.BREAKER_SECONDS - 1.0
    assert transport.send(resident, tap) is False  # inside the window: does not even try
    assert seen == []
    now = nf.BREAKER_SECONDS + 1.0
    assert transport.send(resident, tap) is True  # the window passed
    assert len(seen) == 1


def test_every_tap_the_breaker_swallows_is_still_said(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A knock nobody was told about must not also be a knock nobody can find out about."""
    now = 0.0
    resident = tapping()
    transport = nf.NtfyTransport(base_url="http://127.0.0.1:1", clock=lambda: now)

    assert transport.send(resident, nf.probe_tap(resident)) is False
    now = 1.0
    caplog.clear()
    with caplog.at_level(logging.WARNING, logger="steward.notify"):
        assert transport.send(resident, nf.probe_tap(resident)) is False
    assert "dropped a test tap" in caplog.text
    assert resident.id in caplog.text


def test_a_target_that_is_not_an_http_url_taps_nowhere(caplog: pytest.LogCaptureFixture) -> None:
    resident = tapping()
    transport = nf.NtfyTransport(base_url="file:///etc")
    with caplog.at_level(logging.WARNING, logger="steward.notify"):
        assert transport.send(resident, nf.probe_tap(resident)) is False
    assert "not an http(s) URL" in caplog.text


# ------------------------------------------------------------------ configuration


def test_the_default_target_is_the_public_ntfy() -> None:
    transport = nf.NtfyTransport.from_env({})
    assert transport.base_url == nf.DEFAULT_NTFY_URL
    assert transport.token is None
    assert transport.timeout_s == nf.NTFY_TIMEOUT_S
    assert transport.namespace == ""


def test_the_environment_names_the_server_the_token_and_the_namespace() -> None:
    transport = nf.NtfyTransport.from_env(
        {
            nf.NTFY_URL_ENV: "https://ntfy.example/",
            nf.NTFY_TOKEN_ENV: "tk_live",
            nf.NTFY_TIMEOUT_ENV: "0.5",
            nf.NAMESPACE_ENV: "nas",
        }
    )
    assert transport.base_url == "https://ntfy.example"
    assert transport.token == "tk_live"
    assert transport.timeout_s == 0.5
    assert transport.namespace == "nas"


@pytest.mark.parametrize("raw", ["not-a-number", "0", "-3"])
def test_an_unusable_timeout_falls_back_rather_than_crashing_a_knock(raw: str) -> None:
    transport = nf.NtfyTransport.from_env({nf.NTFY_TIMEOUT_ENV: raw})
    assert transport.timeout_s == nf.NTFY_TIMEOUT_S


def test_from_env_reads_the_process_environment_when_given_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(nf.NTFY_URL_ENV, "https://ntfy.example")
    assert (
        nf.Notifier.from_env()
        .transports[nf.NTFY]
        .address(tapping())
        .startswith("https://ntfy.example/steward-")
    )


# ------------------------------------------------------------------ whether to send


def test_a_manifest_with_no_notifications_block_taps_nobody(
    notifier: nf.Notifier, transport: FakeTransport
) -> None:
    assert notifier.tap(manifest(), needs_human()) is False
    assert transport.sent == []


def test_a_declared_transport_taps(notifier: nf.Notifier, transport: FakeTransport) -> None:
    assert notifier.tap(tapping("needs_human"), needs_human()) is True
    assert [tap.kind for tap in transport.sent] == [ev.NEEDS_HUMAN]


def test_a_kind_the_resident_did_not_ask_for_is_not_tapped(
    notifier: nf.Notifier, transport: FakeTransport
) -> None:
    assert notifier.tap(tapping("needs_human"), task_done()) is False
    assert transport.sent == []


def test_a_resident_can_ask_for_both(notifier: nf.Notifier, transport: FakeTransport) -> None:
    resident = tapping("needs_human", "task_done")
    assert notifier.tap(resident, needs_human()) is True
    assert notifier.tap(resident, task_done()) is True
    assert [tap.kind for tap in transport.sent] == [ev.NEEDS_HUMAN, ev.TASK_DONE]


@pytest.mark.parametrize("status", ["pending", "disabled"])
def test_a_declaration_that_is_not_active_yet_is_silent(
    status: str, notifier: nf.Notifier, transport: FakeTransport
) -> None:
    """A topic nobody has subscribed to is a knock into an empty room; say so, don't send."""
    assert notifier.tap(tapping(status=status), needs_human()) is False
    assert transport.sent == []


def test_a_transport_this_build_cannot_deliver_through_says_so_and_sends_nothing(
    caplog: pytest.LogCaptureFixture,
) -> None:
    empty = nf.Notifier({})
    with caplog.at_level(logging.WARNING, logger="steward.notify"):
        assert empty.tap(tapping(), needs_human()) is False
    assert "cannot deliver through" in caplog.text


def test_a_transport_that_raises_cannot_take_the_caller_with_it(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The promise the transitions layer relies on: reporting a fact cannot fail the fact."""
    exploding = nf.Notifier({nf.NTFY: FakeTransport(raises=True)})
    with caplog.at_level(logging.WARNING, logger="steward.notify"):
        assert exploding.tap(tapping(), needs_human()) is False
    assert "raised while sending" in caplog.text


def test_a_transport_that_reports_failure_reports_it_up(notifier: nf.Notifier) -> None:
    refusing = nf.Notifier({nf.NTFY: FakeTransport(delivers=False)})
    assert refusing.tap(tapping(), needs_human()) is False
    assert notifier.tap(tapping(), needs_human()) is True


def test_send_resolves_the_transport_itself_when_nobody_names_one(
    notifier: nf.Notifier, transport: FakeTransport
) -> None:
    resident = tapping()
    assert notifier.send(resident, nf.probe_tap(resident)) is True
    assert transport.sent[0].kind == "test"
    assert notifier.send(manifest(), nf.probe_tap(manifest())) is False


def test_describe_is_what_an_operator_reads(notifier: nf.Notifier) -> None:
    described = notifier.describe(tapping(note="Miha's phone"))
    assert described.to_dict() == {
        "resident": "test-agent",
        "transport": "ntfy",
        "status": "active",
        "on": ["needs_human"],
        "enabled": True,
        "address": "fake://test-agent",
        "note": "Miha's phone",
    }
    assert notifier.describe(manifest()).address is None


def test_describe_still_shows_the_address_of_a_block_being_wired_up(
    notifier: nf.Notifier,
) -> None:
    """`pending` sends nothing and still has to say where to subscribe — that is the point."""
    pending = notifier.describe(tapping(status="pending"))
    assert pending.enabled is False
    assert pending.address == "fake://test-agent"
    assert notifier.transport_for(tapping(status="pending")) is None


# ------------------------------------------------------------------ the two vocabularies


def test_the_manifest_kinds_are_the_event_types_they_claim_to_be() -> None:
    """``manifest`` cannot import ``events`` (events imports it), so the strings are checked."""
    assert NOTIFICATION_KINDS == (ev.NEEDS_HUMAN, ev.TASK_DONE)
    assert set(NOTIFICATION_TRANSPORTS) == {nf.NTFY}
    assert set(nf._RENDERERS) == set(NOTIFICATION_KINDS)


def test_every_declarable_kind_actually_renders_a_tap() -> None:
    """An ``on:`` value that produced no tap would be a declaration that silently does nothing."""
    built = {ev.NEEDS_HUMAN: needs_human(), ev.TASK_DONE: task_done()}
    assert set(built) == set(NOTIFICATION_KINDS)
    for kind, event in built.items():
        tap = nf.tap_for(manifest(), event)
        assert tap is not None, kind


# ------------------------------------------------------------------ the residents tree


def test_a_real_resident_manifest_round_trips_through_validation(
    write_resident: ResidentWriter,
) -> None:
    path = write_resident(
        {
            **valid_manifest(),
            "notifications": {"transport": "ntfy", "on": ["needs_human"], "note": "Miha's phone"},
        }
    )
    loaded = load_manifest(path)
    assert loaded.manifest.notifications.enabled is True
    assert nf.NtfyTransport().address(loaded.manifest).startswith("https://ntfy.sh/steward-")


def test_a_notifications_block_carries_nothing_that_could_reach_the_topic() -> None:
    """A notifications block carries a label and a vocabulary, never an address or a token."""
    resident = tapping(note="Miha's phone")
    rendered = json.dumps(resident.model_dump(mode="json")["notifications"])
    assert nf.ntfy_topic(resident.uid) not in rendered
    assert "token" not in rendered
