"""Structured approvals: the grammar a session writes, and what steward does with it."""

import threading
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from conftest import ResidentWriter
from steward import approvals as ap
from steward import events as ev
from steward import prompt as p
from steward.manifest import SECRET_REDACTION, ResidentManifest, load_manifest
from steward.store import ApprovalRecord, Store

NOW = datetime(2026, 8, 24, 12, 0, tzinfo=UTC)


@pytest.fixture
def store() -> Iterator[Store]:
    with Store(":memory:") as opened:
        yield opened


@pytest.fixture
def sink() -> ev.NullEmitter:
    return ev.NullEmitter()


@pytest.fixture
def manifest(write_resident: ResidentWriter) -> ResidentManifest:
    return load_manifest(write_resident()).manifest


def raw_block(attrs: str = 'action="send_email"', body: str = '{"to": "a@example.com"}') -> str:
    """One bare block, as the grammar parser sees it — no machine-read region around it."""
    return f"<needs-human {attrs}>\n{body}\n</needs-human>"


def region(*blocks: str) -> str:
    """Wrap blocks in the machine-read region a real session ends its message with.

    The harvester acts only on this region (steward #62), so anything exercising
    :func:`approvals.harvest` — as opposed to the bare-grammar parser
    :func:`approvals.extract_requests` — has to speak the way a taught session speaks.
    """
    return f"{p.ACTIONS_OPEN}\n" + "\n".join(blocks) + f"\n{p.ACTIONS_CLOSE}"


def block(attrs: str = 'action="send_email"', body: str = '{"to": "a@example.com"}') -> str:
    return region(raw_block(attrs, body))


# --------------------------------------------------------------------------- the grammar


def test_a_well_formed_block_carries_everything_a_panel_needs() -> None:
    output = "I drafted the reply.\n\n" + block(
        'action="send_email" expires-in="4h" options="approve,deny"'
    )
    (request,) = ap.extract_requests(output)
    assert request.ok
    assert request.action == "send_email"
    assert request.detail == {"to": "a@example.com"}
    assert request.options == ("approve", "deny")
    assert request.expires_in_s == 4 * 3600
    assert request.expires_at(NOW) == "2026-08-24T16:00:00.000Z"


def test_the_optional_attributes_have_documented_defaults() -> None:
    (request,) = ap.extract_requests(block())
    assert request.options == ("approve", "deny", "edit")
    assert request.expires_in_s == ap.DEFAULT_EXPIRES_IN_S == 24 * 3600


def test_plain_prose_between_the_markers_is_still_a_real_question() -> None:
    (request,) = ap.extract_requests(block(body="Should I cancel Thursday?"))
    assert request.ok
    assert request.detail == {"note": "Should I cancel Thursday?"}


def test_an_empty_body_is_a_request_with_no_detail() -> None:
    (request,) = ap.extract_requests(block(body="   "))
    assert request.ok
    assert request.detail == {}


def test_every_block_is_kept_because_two_asks_are_two_questions() -> None:
    output = block('action="send_email"') + "\nand also\n" + block('action="spend_money"')
    requests = ap.extract_requests(output)
    assert [request.action for request in requests] == ["send_email", "spend_money"]


def test_output_with_no_block_asks_for_nothing() -> None:
    assert ap.extract_requests("just a normal session, nothing gated") == []
    assert ap.extract_requests("") == []


@pytest.mark.parametrize(
    ("attrs", "body", "complaint"),
    [
        ('expires-in="4h"', "{}", "needs an action"),
        ('action="Send Email"', "{}", "is not a slug"),
        ('action="send_email" expires-in="soon"', "{}", "is not a duration"),
        ('action="send_email" expires-in="0h"', "{}", "positive duration"),
        ('action="send_email" options="maybe"', "{}", "unknown option"),
        ('action="send_email" options=" , "', "{}", "options is empty"),
        ('action="send_email" urgency="high"', "{}", "unknown attribute"),
        ("action=send_email", "{}", "could not read"),
        ('action="send_email"', '{"to": ', "does not parse"),
        ('action="send_email"', "[1, 2]", "must be an object"),
    ],
)
def test_a_malformed_block_complains_loudly_instead_of_vanishing(
    attrs: str, body: str, complaint: str
) -> None:
    (request,) = ap.extract_requests(block(attrs, body))
    assert not request.ok
    assert request.action == ap.UNREADABLE_ACTION
    assert request.problem is not None
    assert complaint in request.problem


def test_an_unclosed_block_is_reported_as_a_truncated_ask() -> None:
    (request,) = ap.extract_requests('<needs-human action="send_email">\n{"to": "a"}\n')
    assert not request.ok
    assert request.problem is not None
    assert "no closing" in request.problem


def test_a_dangling_marker_beside_a_good_block_is_reported_too() -> None:
    output = block() + '\n<needs-human action="spend_money">'
    good, dangling = ap.extract_requests(output)
    assert good.ok
    assert not dangling.ok


@pytest.mark.parametrize(
    ("text", "seconds"), [("90s", 90), ("30m", 1800), ("4h", 14400), ("2d", 172800)]
)
def test_the_duration_vocabulary_is_exactly_four_units(text: str, seconds: int) -> None:
    assert ap.parse_duration(text) == seconds


def test_the_protocol_steward_teaches_is_the_protocol_steward_parses() -> None:
    """The prompt cannot document a format the parser refuses; the example is proof."""
    (request,) = ap.extract_requests(p.ESCALATION_PROTOCOL)
    assert request.ok, request.problem
    assert request.action == "send_email"
    assert request.options == ("approve", "deny", "edit")


# ---------------------------------------------------------------------------- raising


def test_raising_persists_the_request_and_knocks(
    store: Store, sink: ev.NullEmitter, manifest: ResidentManifest
) -> None:
    (parsed,) = ap.extract_requests(block('action="send_email" expires-in="1h"'))
    record = ap.raise_request(store, sink, manifest=manifest, request=parsed, now=NOW)

    assert record.pending
    assert record.resident == manifest.id
    assert record.agent_id == manifest.agent_id
    assert record.expires_at == "2026-08-24T13:00:00.000Z"
    assert store.approval(record.request_id) is not None

    (event,) = sink.events
    assert ev.validate_event(event.to_dict()) == ()
    assert event.type == "needs_human"
    assert event.payload["message"] == "Testy wants to send email"
    assert event.payload["request_id"] == record.request_id
    assert event.payload["action"] == "send_email"
    assert event.payload["detail"] == {"to": "a@example.com"}
    assert event.payload["options"] == ["approve", "deny", "edit"]
    assert event.payload["expires_at"] == record.expires_at


def test_a_malformed_ask_still_reaches_a_person(
    store: Store, sink: ev.NullEmitter, manifest: ResidentManifest
) -> None:
    """A session that tried to escalate and failed must not look like one that did not."""
    raised = ap.harvest(store, sink, manifest=manifest, output=block('expires-in="4h"'))
    (record,) = raised
    assert record.action == ap.UNREADABLE_ACTION
    assert "needs an action" in str(record.detail["problem"])
    assert record.detail["raw"].startswith("<needs-human")
    assert sink.events[0].type == "needs_human"


def test_harvesting_a_session_with_nothing_to_ask_creates_nothing(
    store: Store, sink: ev.NullEmitter, manifest: ResidentManifest
) -> None:
    assert ap.harvest(store, sink, manifest=manifest, output="all quiet") == []
    assert store.approvals() == []
    assert sink.events == []


def test_the_message_is_derived_so_it_can_never_disagree_with_the_action(
    manifest: ResidentManifest,
) -> None:
    assert ap.human_message(manifest, "spend_money") == "Testy wants to spend money"


# --------------------------------------------------------------- the repeat-deny guard


def ask(  # noqa: PLR0913 — the collaborators, plus every knob one of these tests varies
    store: Store,
    sink: ev.NullEmitter,
    manifest: ResidentManifest,
    *,
    at: datetime,
    action: str = "send_email",
    repeat_guard: bool = True,
) -> ApprovalRecord:
    """Raise one ordinary session-chosen request at a chosen moment."""
    (parsed,) = ap.extract_requests(block(f'action="{action}" expires-in="1h"'))
    return ap.raise_request(
        store, sink, manifest=manifest, request=parsed, now=at, repeat_guard=repeat_guard
    )


def deny(store: Store, record: ApprovalRecord, *, at: datetime) -> None:
    """Answer a knock the way a human at burrow's panel does."""
    _, recorded = store.decide(record.request_id, "deny", decided_by="api", now=ev.utc_now_iso(at))
    assert recorded, "the fixture's deny has to be the decision that was recorded"


def test_asking_again_after_a_deny_is_auto_denied_without_waking_anybody(
    store: Store, sink: ev.NullEmitter, manifest: ResidentManifest
) -> None:
    """The enforcement behind "a deny means do not ask again" (steward #33)."""
    deny(store, ask(store, sink, manifest, at=NOW), at=NOW)
    sink.events.clear()

    again = ask(store, sink, manifest, at=NOW + timedelta(hours=2))
    assert not again.pending
    assert again.decision == "deny"
    assert again.decided_by == "repeat"
    assert again.decided_at
    assert sink.events == [], "nobody's phone buzzes for a question already answered"
    assert store.pending_approvals() == []
    assert len(store.approvals()) == 2, "the swallowed ask is still on the record"


def test_a_deny_by_expiry_silences_the_repeat_too(
    store: Store, sink: ev.NullEmitter, manifest: ResidentManifest
) -> None:
    """Nobody answering in time is the same answer, to the resident, as a person saying no."""
    ask(store, sink, manifest, at=NOW)
    ap.expire(store, sink, NOW + timedelta(hours=2))
    sink.events.clear()

    again = ask(store, sink, manifest, at=NOW + timedelta(hours=3))
    assert again.decided_by == "repeat"
    assert sink.events == []


def test_the_same_question_a_day_later_knocks_again(
    store: Store, sink: ev.NullEmitter, manifest: ResidentManifest
) -> None:
    """The window is a pause, not a ban: forever belongs in the charter, not here."""
    deny(store, ask(store, sink, manifest, at=NOW), at=NOW)
    sink.events.clear()

    again = ask(store, sink, manifest, at=NOW + timedelta(hours=13))
    assert again.pending
    assert [event.type for event in sink.events] == ["needs_human"]


def test_an_auto_deny_does_not_push_the_window_out_again(
    store: Store, sink: ev.NullEmitter, manifest: ResidentManifest
) -> None:
    """Otherwise a resident that kept asking would silence itself for good."""
    deny(store, ask(store, sink, manifest, at=NOW), at=NOW)
    swallowed = ask(store, sink, manifest, at=NOW + timedelta(hours=11))
    assert swallowed.decided_by == "repeat"
    sink.events.clear()

    again = ask(store, sink, manifest, at=NOW + timedelta(hours=13))
    assert again.pending, "the window is measured from the human's deny, not from a repeat"
    assert [event.type for event in sink.events] == ["needs_human"]


def test_a_deny_only_answers_for_the_action_it_was_about(
    store: Store, sink: ev.NullEmitter, manifest: ResidentManifest
) -> None:
    deny(store, ask(store, sink, manifest, at=NOW, action="send_email"), at=NOW)
    sink.events.clear()

    other = ask(store, sink, manifest, at=NOW + timedelta(hours=1), action="spend_money")
    assert other.pending
    assert [event.type for event in sink.events] == ["needs_human"]


def test_stewards_own_knocks_are_never_swallowed_as_repeats(
    store: Store, sink: ev.NullEmitter, manifest: ResidentManifest
) -> None:
    """A budget pause and a crash loop knock about the resident, not for it."""
    deny(store, ask(store, sink, manifest, at=NOW, action="budget_unpause"), at=NOW)
    sink.events.clear()

    again = ask(
        store,
        sink,
        manifest,
        at=NOW + timedelta(hours=1),
        action="budget_unpause",
        repeat_guard=False,
    )
    assert again.pending
    assert [event.type for event in sink.events] == ["needs_human"]


def test_the_resident_hears_that_its_ask_was_auto_denied(
    store: Store, sink: ev.NullEmitter, manifest: ResidentManifest
) -> None:
    """Swallowed at the door is not swallowed in silence: the next session is told."""
    first = ask(store, sink, manifest, at=NOW)
    deny(store, first, at=NOW)
    ask(store, sink, manifest, at=NOW + timedelta(hours=2))

    preamble, records = ap.deliver_decisions(store, manifest.id)
    assert preamble is not None
    assert "'repeat' is also a deny" in preamble
    assert [record.decided_by for record in records] == ["api", "repeat"]
    assert "send_email: deny (decided by repeat" in preamble


def test_a_second_unreadable_escalation_still_reaches_a_person(
    store: Store, sink: ev.NullEmitter, manifest: ResidentManifest
) -> None:
    """The catch-all action is one name over many questions, so no deny answers for it."""
    ap.harvest(store, sink, manifest=manifest, output=block('expires-in="4h"'), now=NOW)
    ap.expire(store, sink, NOW + timedelta(hours=5))
    sink.events.clear()

    (again,) = ap.harvest(
        store,
        sink,
        manifest=manifest,
        output=block('action="spend_money" expires-in="4h"', '{"to": '),
        now=NOW + timedelta(hours=6),
    )
    assert again.action == ap.UNREADABLE_ACTION
    assert again.pending, "a different malformed ask is a different question"
    assert [event.type for event in sink.events] == ["needs_human"]


def test_the_window_is_read_from_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    hour_s = 3600
    assert ap.repeat_deny_window_s({}) == ap.DEFAULT_REPEAT_DENY_WINDOW_H * hour_s
    assert ap.repeat_deny_window_s({ap.REPEAT_DENY_WINDOW_ENV: "4"}) == 4 * hour_s
    assert ap.repeat_deny_window_s({ap.REPEAT_DENY_WINDOW_ENV: "0"}) == 0
    for bad in ("soon", "-2", "1.5"):
        assert ap.repeat_deny_window_s({ap.REPEAT_DENY_WINDOW_ENV: bad}) == (
            ap.DEFAULT_REPEAT_DENY_WINDOW_H * hour_s
        )
    monkeypatch.setenv(ap.REPEAT_DENY_WINDOW_ENV, "2")
    assert ap.repeat_deny_window_s() == 2 * hour_s


def test_a_zero_window_turns_the_guard_off_fleet_wide(
    store: Store,
    sink: ev.NullEmitter,
    manifest: ResidentManifest,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(ap.REPEAT_DENY_WINDOW_ENV, "0")
    deny(store, ask(store, sink, manifest, at=NOW), at=NOW)
    sink.events.clear()

    again = ask(store, sink, manifest, at=NOW + timedelta(minutes=1))
    assert again.pending
    assert [event.type for event in sink.events] == ["needs_human"]


# ---------------------------------------------------------------------------- expiry


def test_expiry_denies_by_default_and_closes_the_loop_in_the_log(
    store: Store, sink: ev.NullEmitter, manifest: ResidentManifest
) -> None:
    (parsed,) = ap.extract_requests(block('action="spend_money" expires-in="1h"'))
    record = ap.raise_request(store, sink, manifest=manifest, request=parsed, now=NOW)
    sink.events.clear()

    later = datetime(2026, 8, 24, 14, 0, tzinfo=UTC)
    (expired,) = ap.expire(store, sink, later)
    assert expired.request_id == record.request_id
    assert expired.decision == "deny"
    assert expired.decided_by == "expiry"

    (event,) = sink.events
    assert ev.validate_event(event.to_dict()) == ()
    assert event.type == "needs_human_resolved"
    assert event.agent_id == manifest.agent_id, "the villager who knocked walks away"
    assert event.payload == {
        "request_id": record.request_id,
        "decision": "deny",
        "decided_by": "expiry",
        "action": "spend_money",
    }


def test_nothing_expires_before_its_deadline(
    store: Store, sink: ev.NullEmitter, manifest: ResidentManifest
) -> None:
    (parsed,) = ap.extract_requests(block('action="spend_money" expires-in="4h"'))
    ap.raise_request(store, sink, manifest=manifest, request=parsed, now=NOW)
    assert ap.expire(store, sink, NOW) == []
    assert len(store.pending_approvals()) == 1


# --------------------------------------------------------------------------- delivery


def test_a_decision_is_injected_once_and_then_marked_delivered(
    store: Store, sink: ev.NullEmitter, manifest: ResidentManifest
) -> None:
    (parsed,) = ap.extract_requests(block())
    record = ap.raise_request(store, sink, manifest=manifest, request=parsed, now=NOW)
    store.decide(record.request_id, "approve", decided_by="api", now=ev.utc_now_iso(NOW))

    text, delivered = ap.deliver_decisions(store, manifest.id)
    assert text is not None
    assert "send_email: approve" in text
    assert "decided by api" in text
    assert [r.request_id for r in delivered] == [record.request_id]

    assert ap.deliver_decisions(store, manifest.id) == (None, [])


def test_an_edited_decision_carries_the_humans_version_into_the_next_session(
    store: Store, sink: ev.NullEmitter, manifest: ResidentManifest
) -> None:
    (parsed,) = ap.extract_requests(block())
    record = ap.raise_request(store, sink, manifest=manifest, request=parsed, now=NOW)
    store.decide(
        record.request_id,
        "edit",
        decided_by="api",
        edit={"subject": "shorter"},
        now=ev.utc_now_iso(NOW),
    )
    text, _ = ap.deliver_decisions(store, manifest.id)
    assert text is not None
    assert "shorter" in text
    assert "what you asked" in text


def test_an_empty_set_of_decisions_renders_nothing() -> None:
    assert ap.decisions_preamble([]) is None


def test_a_decision_rendered_for_a_human_carries_no_secret(
    store: Store, manifest: ResidentManifest
) -> None:
    """Everything a model or a human typed is scrubbed; the request still reads as itself.

    ``steward show`` prints a decision to a person, and the stored ``message``/``detail``
    are session-written text no validator scanned. Field-level redaction, not a pass over
    the rendered line, so ``rotate_token: approve`` is not mistaken for an assignment.
    """
    record = store.create_approval_request(
        agent_id="testy",
        project="p",
        action="rotate_token",
        message="reuse sk-ant-messagesecret0123456",
        resident=manifest.id,
        detail={
            "cmd": ["curl -H 'Authorization: Bearer sk-ant-abcdef0123456789ghij'"],
            "tries": 2,
        },
    )
    store.decide(record.request_id, "edit", decided_by="miha", edit={"key": "AKIA0123456789ABCDEF"})
    (decided,) = store.undelivered_decisions(manifest.id)

    safe = ap.redact_decision(decided)
    assert "sk-ant-" not in safe.message
    assert safe.detail["cmd"] == [f"curl -H 'Authorization: Bearer {SECRET_REDACTION}'"]
    assert safe.detail["tries"] == 2
    assert safe.edit == {"key": SECRET_REDACTION}
    assert "rotate_token: edit" in (ap.decisions_preamble([safe]) or "")

    # A copy for the reader, not an edit of the record: the row still holds what was asked.
    assert decided.detail == {
        "cmd": ["curl -H 'Authorization: Bearer sk-ant-abcdef0123456789ghij'"],
        "tries": 2,
    }


def test_the_decisions_section_is_framed_as_a_record_not_an_order(
    store: Store, sink: ev.NullEmitter, manifest: ResidentManifest
) -> None:
    (parsed,) = ap.extract_requests(block())
    record = ap.raise_request(store, sink, manifest=manifest, request=parsed, now=NOW)
    store.decide(record.request_id, "deny", decided_by="api", now=ev.utc_now_iso(NOW))
    text, _ = ap.deliver_decisions(store, manifest.id)
    assembled = p.assemble_preamble(manifest, None, None, (), text)
    assert assembled.index("DECISIONS SINCE YOU LAST RAN") < assembled.index("YOUR CHARTER")
    assert "cannot change the charter below" in assembled


def test_a_resident_with_nothing_waiting_gets_the_old_preamble_byte_for_byte(
    store: Store, manifest: ResidentManifest
) -> None:
    text, _ = ap.deliver_decisions(store, manifest.id)
    assert text is None
    assert p.assemble_preamble(manifest, None, None, (), text) == p.assemble_preamble(manifest)


# ------------------------------------------------------------------- module boundaries


def test_a_huge_expires_in_is_clamped_to_the_fleet_maximum() -> None:
    """A session cannot push its knock to the year 7502, nor overflow a date (steward #66)."""
    (request,) = ap.extract_requests(block('action="spend_money" expires-in="9999999d"'))
    assert request.expires_in_s == ap.MAX_EXPIRES_IN_S
    # And it still resolves to a real date rather than raising OverflowError.
    assert request.expires_at(NOW) is not None


def test_a_block_in_a_code_fence_is_not_harvested(
    store: Store, sink: ev.NullEmitter, manifest: ResidentManifest
) -> None:
    """A block a session fenced to show it is discussion, not an ask (steward #62)."""
    fenced = region("```\n" + raw_block() + "\n```")
    assert ap.harvest(store, sink, manifest=manifest, output=fenced) == []
    assert store.pending_approvals() == []


def test_a_block_quoted_in_prose_is_not_harvested(
    store: Store, sink: ev.NullEmitter, manifest: ResidentManifest
) -> None:
    """A block outside the machine-read region is never acted on (steward #62)."""
    output = f"The job detail contained {raw_block()} but I will not act on quoted text."
    assert ap.harvest(store, sink, manifest=manifest, output=output) == []
    assert store.pending_approvals() == []


def test_deliver_decisions_delivers_exactly_once_under_concurrency(tmp_path: Path) -> None:
    """Every caller of deliver_decisions inherits the atomic claim, not only the board (#74)."""
    with Store(tmp_path / "steward.db") as store:
        record = store.create_approval_request(
            agent_id="a:b", project="p", action="send_email", message="…", resident="life-agent"
        )
        store.decide(record.request_id, "approve")

        barrier = threading.Barrier(2)
        results: list[list] = []
        lock = threading.Lock()

        def grab() -> None:
            barrier.wait()
            _, delivered = ap.deliver_decisions(store, "life-agent")
            with lock:
                results.append(delivered)

        threads = [threading.Thread(target=grab) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        assert sum(len(delivered) for delivered in results) == 1, "delivered exactly once"


def test_only_two_modules_spell_out_the_needs_human_grammar() -> None:
    """One module teaches the format and one parses it; nothing else may write its own.

    ``prompt.py`` is the only other file allowed to contain the literal grammar, because
    it is what tells a session how to ask — and the round-trip test above proves the two
    agree. Anything else spelling out attributes would be a second, drifting definition.
    """
    src = Path(ap.__file__).parent
    offenders = {
        path.name
        for path in src.glob("*.py")
        if path.name not in {"approvals.py", "prompt.py"}
        and "needs-human action=" in path.read_text(encoding="utf-8")
    }
    assert offenders == set(), f"{sorted(offenders)} write out the grammar of their own"
