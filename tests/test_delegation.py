"""Delegation: who may hand work to whom, and what the village is told about it.

The refusals get as much room as the happy path on purpose. A delegation steward should
have refused and did not is a resident doing work nobody granted it; a delegation steward
refused and said nothing about is a session that thinks it asked for help. Both are the
kind of quiet lie this repo exists to avoid, so every rejection here is checked twice —
for the structured reason it returns, and for the events it did *not* emit.
"""

import copy
import json
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from conftest import REPO_ROOT, ResidentWriter, valid_manifest
from steward import board as b
from steward import delegation as dg
from steward import events as ev
from steward import prompt
from steward.budgets import BudgetGuard
from steward.manifest import Resident, load_manifest, validate_tree
from steward.runners import Outcome, Runner, RunRequest, RunResult
from steward.skills import library_for
from steward.store import ORIGIN_UNATTRIBUTED, JobRecord, Store

NOW = datetime(2026, 8, 25, 12, 0, tzinfo=UTC)

SENDER = "sender-agent"
RECEIVER = "receiver-agent"


def soul_for(name: str, agent_id: str) -> str:
    """Return a soul whose frontmatter agrees with the manifest it sits beside."""
    return (
        f"---\nagent_id: {agent_id}\nname: {name}\nchar: Monk\n"
        f'accent: "#a68a4f"\nrole: test bot\n---\n'
        f"A villager that exists only inside a test.\n\n## Voice\n\nFlat, factual, short.\n"
    )


def resident_manifest(
    resident_id: str,
    *,
    name: str,
    delegation: dict[str, Any] | None = None,
    routes: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Build a valid manifest for one end of a handoff."""
    data = copy.deepcopy(valid_manifest())
    data["id"] = resident_id
    data["agent_id"] = f"claude-code:{resident_id}"
    data["soul"]["name"] = name
    if delegation is not None:
        data["delegation"] = delegation
    if routes is not None:
        data["routes"] = [*data["routes"], *routes]
    return data


def inbox_route(route_id: str = "inbox", status: str = "active") -> dict[str, Any]:
    """Return the receiving declaration: a route steward may deliver work into."""
    return {
        "id": route_id,
        "kind": "delegation",
        "address": "steward:delegation",
        "status": status,
    }


def sender_manifest(
    delegation: dict[str, Any] | None = None, routes: list[dict[str, Any]] | None = None
) -> dict[str, Any]:
    """Build the permitted sender: it may hand work over, and receives none."""
    return resident_manifest(
        SENDER, name="Sender", delegation=delegation or {"send": True}, routes=routes
    )


def receiver_manifest(routes: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """Build the declared receiver: one active route work may be delivered into."""
    return resident_manifest(RECEIVER, name="Receiver", routes=routes or [inbox_route()])


@pytest.fixture
def store() -> Iterator[Store]:
    with Store(":memory:") as opened:
        yield opened


@pytest.fixture
def sink() -> ev.NullEmitter:
    return ev.NullEmitter()


type Fleet = Callable[..., list[Resident]]


@pytest.fixture
def fleet(write_resident: ResidentWriter, tmp_path: Path) -> Fleet:
    """Write a residents tree and return the loaded residents, in order."""

    def _write(*manifests: dict[str, Any]) -> list[Resident]:
        residents: list[Resident] = []
        for data in manifests:
            path = write_resident(data, soul=soul_for(data["soul"]["name"], data["agent_id"]))
            residents.append(load_manifest(path))
        return residents

    _ = tmp_path
    return _write


type MakeDelegator = Callable[..., dg.Delegator]


@pytest.fixture
def make_delegator(fleet: Fleet, store: Store, sink: ev.NullEmitter) -> MakeDelegator:
    def _make(*manifests: dict[str, Any], max_depth: int = dg.DEFAULT_MAX_DEPTH) -> dg.Delegator:
        residents = fleet(*(manifests or (sender_manifest(), receiver_manifest())))
        return dg.Delegator(residents=residents, store=store, emitter=sink, max_depth=max_depth)

    return _make


def handoff(
    to: str = RECEIVER, route: str = "inbox", title: str = "Read the background"
) -> dg.Handoff:
    """Build one parsed handoff, as a block or the CLI would have produced it."""
    return dg.Handoff(
        raw="<delegate…>",
        to=to,
        route=route,
        title=title,
        detail="everything the receiver needs",
    )


def sender_of(delegator: dg.Delegator, resident_id: str = SENDER) -> Resident:
    """Return one resident of the delegator's fleet, insisting it is there."""
    found = delegator.resident(resident_id)
    assert found is not None, f"no resident {resident_id}"
    return found


def job(store: Store, task_id: str) -> JobRecord:
    """Read one task back, insisting it exists."""
    found = store.job(task_id)
    assert found is not None, f"no task {task_id}"
    return found


def refusal(
    delegator: dg.Delegator,
    *,
    sender: Resident | None = None,
    to: str = RECEIVER,
    route: str = "inbox",
) -> dg.DelegationError:
    """Delegate and expect a refusal, returning the error it was refused with."""
    with pytest.raises(dg.DelegationError) as caught:
        delegator.delegate(
            sender=sender if sender is not None else delegator.resident(SENDER),
            handoff=handoff(to=to, route=route),
        )
    return caught.value


# ------------------------------------------------------------------------- the grammar


def test_a_block_names_both_ends_and_the_work() -> None:
    output = """Done with my part.

    <delegate to="life-agent" route="inbox">
    {"title": "Check the errand list", "detail": "the long version"}
    </delegate>
    """
    (parsed,) = dg.extract_handoffs(output)
    assert parsed.ok
    assert parsed.to == "life-agent"
    assert parsed.route == "inbox"
    assert parsed.title == "Check the errand list"
    assert parsed.detail == "the long version"


def test_every_block_becomes_a_handoff_not_only_the_last() -> None:
    """Two neighbours asked for two things; dropping either loses one of them."""
    output = (
        '<delegate to="a" route="inbox">{"title": "first"}</delegate>\n'
        '<delegate to="b" route="inbox">{"title": "second"}</delegate>'
    )
    assert [h.title for h in dg.extract_handoffs(output)] == ["first", "second"]


def test_a_detail_is_optional() -> None:
    (parsed,) = dg.extract_handoffs('<delegate to="a" route="inbox">{"title": "t"}</delegate>')
    assert parsed.ok
    assert parsed.detail == ""


def test_a_structured_detail_survives_as_json() -> None:
    (parsed,) = dg.extract_handoffs(
        '<delegate to="a" route="inbox">{"title": "t", "detail": {"q": 1}}</delegate>'
    )
    assert json.loads(parsed.detail) == {"q": 1}


@pytest.mark.parametrize(
    ("block", "complaint"),
    [
        ('<delegate route="inbox">{"title": "t"}</delegate>', "needs a to"),
        ('<delegate to="a">{"title": "t"}</delegate>', "needs a route"),
        ('<delegate to="a" route="inbox">not json</delegate>', "not JSON"),
        ('<delegate to="a" route="inbox">[1, 2]</delegate>', "must be a JSON object"),
        ('<delegate to="a" route="inbox"></delegate>', "needs a JSON body"),
        ('<delegate to="a" route="inbox">{"detail": "d"}</delegate>', "non-empty title"),
        (
            '<delegate to="a" route="inbox">{"title": "t", "due": "friday"}</delegate>',
            "unknown key(s)",
        ),
        ('<delegate to="a" route="inbox" when="now">{"title": "t"}</delegate>', "unknown attr"),
        ('<delegate to=a route=inbox>{"title": "t"}</delegate>', "as an attribute"),
    ],
)
def test_a_malformed_block_comes_back_with_the_complaint(block: str, complaint: str) -> None:
    (parsed,) = dg.extract_handoffs(block)
    assert not parsed.ok
    assert complaint in (parsed.problem or "")


def test_an_unclosed_block_is_reported_rather_than_dropped() -> None:
    """The likeliest shape of a session killed mid-sentence."""
    (parsed,) = dg.extract_handoffs('here you go\n<delegate to="a" route="inbox">{"title": "t"}')
    assert not parsed.ok
    assert "no closing" in (parsed.problem or "")


def test_output_with_no_block_hands_nothing_over() -> None:
    assert dg.extract_handoffs("just a normal answer") == []
    assert dg.extract_handoffs("") == []


# ------------------------------------------------------------------- both declarations


def test_a_permitted_sender_reaches_a_declared_route(
    make_delegator: MakeDelegator, store: Store, sink: ev.NullEmitter
) -> None:
    delegator = make_delegator()
    task = delegator.delegate(sender=delegator.resident(SENDER), handoff=handoff())

    assert task.assignee == RECEIVER
    assert task.delegated_by == SENDER
    assert task.route == "inbox"
    assert task.depth == 1
    assert task.status == "open"
    assert task.origin == f"resident:{SENDER}"
    assert [item.task_id for item in store.inbox(RECEIVER)] == [task.task_id]
    assert [event.type for event in sink.events] == ["task_delegated"]


def test_the_event_names_both_ends(make_delegator: MakeDelegator, sink: ev.NullEmitter) -> None:
    delegator = make_delegator()
    task = delegator.delegate(sender=delegator.resident(SENDER), handoff=handoff())
    (event,) = sink.events

    assert ev.validate_event(event.to_dict()) == ()
    assert event.agent_id == f"claude-code:{SENDER}", "burrow walks the villager carrying it"
    assert event.payload == {
        "task_id": task.task_id,
        "title": "Read the background",
        "from": f"claude-code:{SENDER}",
        "to": f"claude-code:{RECEIVER}",
        "route": "inbox",
        "parent_task_id": None,
        "depth": 1,
    }


def test_silence_in_a_manifest_is_not_permission(
    make_delegator: MakeDelegator, store: Store, sink: ev.NullEmitter
) -> None:
    """No delegation block at all: the sender may not hand anything to anybody."""
    delegator = make_delegator(resident_manifest(SENDER, name="Sender"), receiver_manifest())
    error = refusal(delegator)

    assert error.reason == dg.NOT_PERMITTED
    assert "send: true" in str(error)
    assert store.inbox(RECEIVER) == []
    assert sink.events == [], "a refusal emits nothing at all"


def test_an_allowlist_that_does_not_name_the_receiver_refuses(
    make_delegator: MakeDelegator, sink: ev.NullEmitter
) -> None:
    delegator = make_delegator(
        sender_manifest({"send": True, "to": ["somebody-else"]}),
        receiver_manifest(),
    )
    error = refusal(delegator)
    assert error.reason == dg.RECIPIENT_NOT_ALLOWED
    assert sink.events == []


def test_an_allowlist_that_names_the_receiver_allows_it(make_delegator: MakeDelegator) -> None:
    delegator = make_delegator(
        sender_manifest({"send": True, "to": [RECEIVER]}), receiver_manifest()
    )
    task = delegator.delegate(sender=delegator.resident(SENDER), handoff=handoff())
    assert task.assignee == RECEIVER


def test_a_receiver_with_no_such_route_refuses(
    make_delegator: MakeDelegator, sink: ev.NullEmitter
) -> None:
    delegator = make_delegator()
    error = refusal(delegator, route="letterbox")
    assert error.reason == dg.UNKNOWN_ROUTE
    assert "inbox" in str(error), "the refusal names the routes that would have worked"
    assert sink.events == []


def test_a_route_of_another_kind_is_not_a_letterbox(
    make_delegator: MakeDelegator, sink: ev.NullEmitter
) -> None:
    """A resident's email route is not a door steward may push work through."""
    delegator = make_delegator()
    error = refusal(delegator, route="schedule")
    assert error.reason == dg.ROUTE_NOT_DELEGABLE
    assert sink.events == []


def test_a_route_that_is_not_open_yet_takes_no_letters(
    make_delegator: MakeDelegator, sink: ev.NullEmitter
) -> None:
    delegator = make_delegator(
        sender_manifest(),
        resident_manifest(RECEIVER, name="Receiver", routes=[inbox_route(status="pending")]),
    )
    error = refusal(delegator)
    assert error.reason == dg.ROUTE_INACTIVE
    assert sink.events == []


def test_a_recipient_nobody_has_heard_of_refuses_with_the_near_miss(
    make_delegator: MakeDelegator, sink: ev.NullEmitter
) -> None:
    delegator = make_delegator()
    error = refusal(delegator, to="receiver-agnt")
    assert error.reason == dg.UNKNOWN_RECIPIENT
    assert "receiver-agent" in str(error)
    assert sink.events == []


def test_a_resident_cannot_hand_work_to_itself(
    make_delegator: MakeDelegator, sink: ev.NullEmitter
) -> None:
    delegator = make_delegator(sender_manifest(routes=[inbox_route()]), receiver_manifest())
    error = refusal(delegator, to=SENDER)
    assert error.reason == dg.SELF_DELEGATION
    assert sink.events == []


def test_a_human_needs_no_manifest_to_hand_work_over(make_delegator: MakeDelegator) -> None:
    """``sender=None`` is a person with the API token; the receiver's route still rules."""
    delegator = make_delegator()
    task = delegator.delegate(sender=None, handoff=handoff())
    assert task.delegated_by == dg.HUMAN_SENDER
    assert task.origin == "human:api"

    with pytest.raises(dg.DelegationError) as caught:
        delegator.delegate(sender=None, handoff=handoff(route="schedule"))
    assert caught.value.reason == dg.ROUTE_NOT_DELEGABLE


# ------------------------------------------------------------------------- the chain


def three_way(make: MakeDelegator) -> dg.Delegator:
    """Build a fleet where everybody may send and everybody may receive."""
    return make(
        resident_manifest(SENDER, name="Sender", delegation={"send": True}, routes=[inbox_route()]),
        resident_manifest(
            RECEIVER, name="Receiver", delegation={"send": True}, routes=[inbox_route()]
        ),
        resident_manifest(
            "third-agent", name="Third", delegation={"send": True}, routes=[inbox_route()]
        ),
    )


def test_depth_counts_hops_and_the_cap_stops_the_chain(
    make_delegator: MakeDelegator, sink: ev.NullEmitter
) -> None:
    delegator = three_way(make_delegator)
    delegator.max_depth = 2
    first = delegator.delegate(sender=delegator.resident(SENDER), handoff=handoff())
    second = delegator.delegate(
        sender=delegator.resident(RECEIVER),
        handoff=handoff(to="third-agent"),
        parent_task_id=first.task_id,
    )
    assert (first.depth, second.depth) == (1, 2)

    before = len(sink.events)
    with pytest.raises(dg.DelegationError) as caught:
        delegator.delegate(
            sender=delegator.resident("third-agent"),
            handoff=handoff(to=RECEIVER),
            parent_task_id=second.task_id,
        )
    assert caught.value.reason == dg.MAX_DEPTH_EXCEEDED
    assert len(sink.events) == before, "nothing is emitted for a refusal"


def test_a_chain_may_never_come_back_to_somebody_it_visited(
    make_delegator: MakeDelegator, store: Store
) -> None:
    """A → B → A is a loop, and a loop spends a budget until somebody notices."""
    delegator = three_way(make_delegator)
    first = delegator.delegate(sender=delegator.resident(SENDER), handoff=handoff())

    with pytest.raises(dg.DelegationError) as caught:
        delegator.delegate(
            sender=delegator.resident(RECEIVER),
            handoff=handoff(to=SENDER),
            parent_task_id=first.task_id,
        )
    assert caught.value.reason == dg.CYCLE
    assert store.inbox(SENDER) == []


def test_a_longer_loop_is_the_same_mistake(make_delegator: MakeDelegator) -> None:
    delegator = three_way(make_delegator)
    first = delegator.delegate(sender=delegator.resident(SENDER), handoff=handoff())
    second = delegator.delegate(
        sender=delegator.resident(RECEIVER),
        handoff=handoff(to="third-agent"),
        parent_task_id=first.task_id,
    )
    with pytest.raises(dg.DelegationError) as caught:
        delegator.delegate(
            sender=delegator.resident("third-agent"),
            handoff=handoff(to=SENDER),
            parent_task_id=second.task_id,
        )
    assert caught.value.reason == dg.CYCLE


def test_a_zero_cap_turns_delegation_off_fleet_wide(make_delegator: MakeDelegator) -> None:
    delegator = make_delegator(max_depth=0)
    assert refusal(delegator).reason == dg.MAX_DEPTH_EXCEEDED


def test_the_cap_is_read_from_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    assert dg.max_depth({}) == dg.DEFAULT_MAX_DEPTH
    assert dg.max_depth({dg.MAX_DEPTH_ENV: "5"}) == 5
    assert dg.max_depth({dg.MAX_DEPTH_ENV: "0"}) == 0
    assert dg.max_depth({dg.MAX_DEPTH_ENV: "deep"}) == dg.DEFAULT_MAX_DEPTH
    assert dg.max_depth({dg.MAX_DEPTH_ENV: "-2"}) == dg.DEFAULT_MAX_DEPTH
    monkeypatch.setenv(dg.MAX_DEPTH_ENV, "2")
    assert dg.max_depth() == 2


def test_the_whole_chain_attributes_to_one_origin(
    make_delegator: MakeDelegator, store: Store
) -> None:
    """Budget attribution: spend rolls up to the root, not to the last hop."""
    delegator = three_way(make_delegator)
    root = store.post_job(title="The thing a human actually asked for")
    first = delegator.delegate(
        sender=delegator.resident(SENDER), handoff=handoff(), parent_task_id=root.task_id
    )
    second = delegator.delegate(
        sender=delegator.resident(RECEIVER),
        handoff=handoff(to="third-agent"),
        parent_task_id=first.task_id,
    )
    assert first.origin == f"task:{root.task_id}"
    assert second.origin == first.origin

    chain = store.lineage(second.task_id)
    assert [item.task_id for item in chain] == [root.task_id, first.task_id, second.task_id]
    assert [item.depth for item in chain] == [0, 1, 2]


def test_a_task_nobody_delegated_is_a_chain_of_one(store: Store) -> None:
    posted = store.post_job(title="Just a notice")
    assert [item.task_id for item in store.lineage(posted.task_id)] == [posted.task_id]
    assert store.lineage("nobody") == []


def test_an_inbox_survives_a_restart(tmp_path: Path, fleet: Fleet) -> None:
    """Durable means durable: the letter is still there after steward is restarted."""
    residents = fleet(sender_manifest(), receiver_manifest())
    db = tmp_path / "state" / "steward.db"
    with Store(db) as store:
        delegator = dg.Delegator(residents=residents, store=store, emitter=ev.NullEmitter())
        task = delegator.delegate(sender=residents[0], handoff=handoff())

    with Store(db) as reopened:
        (waiting,) = reopened.inbox(RECEIVER)
        assert waiting.task_id == task.task_id
        assert waiting.title == "Read the background"
        assert waiting.delegated_by == SENDER
        assert waiting.route == "inbox"
        assert waiting.depth == 1
        assert waiting.origin == f"resident:{SENDER}"


# ------------------------------------------------------ harvesting a session's output


def test_a_refused_handoff_knocks_at_a_human_door(
    make_delegator: MakeDelegator, store: Store, sink: ev.NullEmitter
) -> None:
    """The session has finished; there is nobody left to answer, so a person hears it."""
    delegator = make_delegator(resident_manifest(SENDER, name="Sender"), receiver_manifest())
    output = f'<delegate to="{RECEIVER}" route="inbox">{{"title": "Read this"}}</delegate>'
    (delivery,) = delegator.harvest(sender_of(delegator), output, now=NOW)

    assert not delivery.accepted
    assert delivery.reason == dg.NOT_PERMITTED
    assert store.inbox(RECEIVER) == []
    assert [event.type for event in sink.events] == ["needs_human"], "refused, but never silent"

    (record,) = store.pending_approvals()
    assert record.action == dg.REJECTED_ACTION
    assert record.detail["reason"] == dg.NOT_PERMITTED
    assert record.detail["to"] == RECEIVER
    assert "Read this" in record.message


def test_a_block_steward_cannot_read_knocks_too(
    make_delegator: MakeDelegator, store: Store, sink: ev.NullEmitter
) -> None:
    """Mirrors unreadable_escalation: a session that tried and failed is not a quiet one."""
    delegator = make_delegator()
    (delivery,) = delegator.harvest(
        sender_of(delegator), '<delegate to="a" route="inbox">nonsense</delegate>'
    )

    assert delivery.reason == dg.UNREADABLE_BLOCK
    (record,) = store.pending_approvals()
    assert record.action == dg.UNREADABLE_ACTION
    assert "could not read" in record.message
    assert record.detail["raw"].startswith("<delegate")
    assert [event.type for event in sink.events] == ["needs_human"]


def test_a_harvested_block_is_delivered(make_delegator: MakeDelegator, store: Store) -> None:
    delegator = make_delegator()
    output = f'<delegate to="{RECEIVER}" route="inbox">{{"title": "Read this"}}</delegate>'
    (delivery,) = delegator.harvest(sender_of(delegator), output)
    assert delivery.accepted
    assert [item.title for item in store.inbox(RECEIVER)] == ["Read this"]


# ---------------------------------------------------------------- working the delivery


class ScriptedRunner(Runner):
    """A runner that returns a prepared result and remembers what it was asked to run."""

    def __init__(self, result: RunResult | None = None) -> None:
        """Hold the result every run of this runner will return."""
        super().__init__()
        self.result = result or RunResult(outcome=Outcome.OK, output="read it", exit_status=0)
        self.requests: list[RunRequest] = []

    def run(self, request: RunRequest) -> RunResult:
        """Record the request and return the prepared result."""
        self.requests.append(request)
        return self.result


type MakeDispatcher = Callable[..., b.Dispatcher]


@pytest.fixture
def make_dispatcher(store: Store, sink: ev.NullEmitter, tmp_path: Path) -> MakeDispatcher:
    def _make(residents: list[Resident], runner: Runner | None = None) -> b.Dispatcher:
        return b.Dispatcher(
            residents=residents,
            store=store,
            emitter=sink,
            workdir=tmp_path,
            runner_factory=lambda _spec: runner or ScriptedRunner(),
        )

    return _make


def test_the_receiver_works_its_inbox_on_its_next_wake_up(
    fleet: Fleet, make_dispatcher: MakeDispatcher, store: Store, sink: ev.NullEmitter
) -> None:
    residents = fleet(sender_manifest(), receiver_manifest())
    dispatcher = make_dispatcher(residents)
    task = dispatcher.delegator.delegate(sender=residents[0], handoff=handoff())
    sink.events.clear()

    (report,) = dispatcher.dispatch(NOW).reports

    assert report.delegated
    assert report.done
    assert report.resident_id == RECEIVER
    assert job(store, task.task_id).status == "done"
    assert [event.type for event in sink.events] == ["task_claimed", "task_done"]


def test_pickup_and_completion_carry_the_parent(
    fleet: Fleet, make_dispatcher: MakeDispatcher, store: Store, sink: ev.NullEmitter
) -> None:
    residents = fleet(sender_manifest(), receiver_manifest())
    dispatcher = make_dispatcher(residents)
    root = store.post_job(title="The root task")
    dispatcher.delegator.delegate(
        sender=residents[0], handoff=handoff(), parent_task_id=root.task_id
    )
    sink.events.clear()

    dispatcher.dispatch(NOW)
    claimed, done = sink.events
    assert claimed.payload["parent_task_id"] == root.task_id
    assert done.payload["parent_task_id"] == root.task_id


def test_an_ordinary_board_task_says_nothing_about_a_parent(
    fleet: Fleet, make_dispatcher: MakeDispatcher, store: Store, sink: ev.NullEmitter
) -> None:
    """The payload of a claim is byte-identical to one from before delegation existed."""
    data = receiver_manifest()
    data["routes"] = [*data["routes"], {"id": "job-board", "kind": "job-board", "address": "b"}]
    data["board"] = {"claim": True}
    residents = fleet(data)
    store.post_job(title="A notice for anybody")

    make_dispatcher(residents).dispatch(NOW)
    claimed, done = sink.events
    assert "parent_task_id" not in claimed.payload
    assert "parent_task_id" not in done.payload


def test_a_letter_is_never_claimed_off_the_open_board(
    fleet: Fleet, make_dispatcher: MakeDispatcher, store: Store
) -> None:
    """Somebody else's letter is not a notice, however well the skills match."""
    board_taker = receiver_manifest()
    board_taker["id"] = "board-agent"
    board_taker["agent_id"] = "claude-code:board-agent"
    board_taker["soul"]["name"] = "Boarder"
    board_taker["routes"] = [
        *board_taker["routes"],
        {"id": "job-board", "kind": "job-board", "address": "b"},
    ]
    board_taker["board"] = {"claim": True}
    residents = fleet(sender_manifest(), receiver_manifest(), board_taker)
    dispatcher = make_dispatcher(residents)
    task = dispatcher.delegator.delegate(sender=residents[0], handoff=handoff())

    claimed = store.claim_next_job(
        claimant="claude-code:board-agent",
        skills=["daily-summary", "write-journal"],
        lease_expires_at=ev.utc_now_iso(NOW + timedelta(seconds=60)),
    )
    assert claimed is None, "the open board has nothing on it"
    assert job(store, task.task_id).status == "open"


def test_the_inbox_is_drained_before_the_board(
    fleet: Fleet, make_dispatcher: MakeDispatcher, store: Store
) -> None:
    """Work addressed to you personally comes ahead of work addressed to nobody."""
    receiver = receiver_manifest()
    receiver["routes"] = [
        *receiver["routes"],
        {"id": "job-board", "kind": "job-board", "address": "b"},
    ]
    receiver["board"] = {"claim": True, "max_claims_per_wake": 1}
    residents = fleet(sender_manifest(), receiver)
    dispatcher = make_dispatcher(residents)
    store.post_job(title="A notice for anybody")
    dispatcher.delegator.delegate(sender=residents[0], handoff=handoff())

    reports = dispatcher.dispatch(NOW).reports
    assert [report.task.title for report in reports] == [
        "Read the background",
        "A notice for anybody",
    ]


def test_a_dropped_letter_goes_back_to_the_inbox_loudly(
    fleet: Fleet, make_dispatcher: MakeDispatcher, store: Store, sink: ev.NullEmitter
) -> None:
    """The lease sweep is the board's, and a delegated item is leased the same way."""
    residents = fleet(sender_manifest(), receiver_manifest())
    dispatcher = make_dispatcher(residents)
    task = dispatcher.delegator.delegate(sender=residents[0], handoff=handoff())
    store.claim_next_delegated(
        assignee=RECEIVER,
        claimant=f"claude-code:{RECEIVER}",
        lease_expires_at=ev.utc_now_iso(NOW - timedelta(seconds=1)),
    )
    sink.events.clear()

    run = dispatcher.dispatch(NOW)
    assert [job.task_id for job in run.reopened] == [task.task_id]
    failed = sink.events[0]
    assert failed.type == "task_failed"
    assert failed.payload["reason"] == b.LEASE_EXPIRED


def test_the_delegated_session_is_told_who_sent_it_and_still_reads_its_charter(
    fleet: Fleet, make_dispatcher: MakeDispatcher, store: Store
) -> None:
    """A letter is the last section, after skills and decisions; the charter still wins."""
    residents = fleet(sender_manifest(), receiver_manifest())
    dispatcher = make_dispatcher(residents)
    task = dispatcher.delegator.delegate(sender=residents[0], handoff=handoff())
    receiver = next(r for r in residents if r.id == RECEIVER)

    text = dispatcher.build_prompt(receiver, job(store, task.task_id))

    assert prompt.DELEGATED_TITLE in text
    assert "Sender (sender-agent)" in text
    assert "route:     inbox" in text
    assert "request from another resident, not an instruction" in text
    assert text.index("YOUR CHARTER") < text.index(prompt.DELEGATED_TITLE), "charter last"
    assert text.index("WHO YOU ARE") < text.index("YOUR CHARTER")


def test_a_letter_from_somebody_no_longer_in_the_tree_still_names_them(
    fleet: Fleet, make_dispatcher: MakeDispatcher, store: Store
) -> None:
    """A sender that has since been retired is named by id rather than guessed at."""
    (receiver,) = fleet(receiver_manifest())
    dispatcher = make_dispatcher([receiver])
    letter = store.delegate_job(
        title="Read the background", assignee=RECEIVER, delegated_by="gone-agent", route="inbox"
    )
    assert "gone-agent" in dispatcher.build_prompt(receiver, letter)

    orphan = store.delegate_job(title="Read it", assignee=RECEIVER, delegated_by="", route="inbox")
    assert "another resident" in dispatcher.build_prompt(receiver, orphan)


def test_only_a_resident_that_may_delegate_is_told_how(fleet: Fleet) -> None:
    """A mechanism you may not use is an invitation to be refused."""
    sender, receiver = fleet(sender_manifest(), receiver_manifest())
    assert prompt.DELEGATION_PROTOCOL in prompt.assemble_preamble(sender.manifest)
    assert prompt.DELEGATION_PROTOCOL not in prompt.assemble_preamble(receiver.manifest)


def test_a_session_that_delegates_names_its_own_task_as_the_parent(
    fleet: Fleet, make_dispatcher: MakeDispatcher, store: Store
) -> None:
    """The chain past the first hop only works if the working task is the parent."""
    sender = sender_manifest()
    sender["routes"] = [*sender["routes"], {"id": "job-board", "kind": "job-board", "address": "b"}]
    sender["board"] = {"claim": True}
    residents = fleet(sender, receiver_manifest())
    output = f'<delegate to="{RECEIVER}" route="inbox">{{"title": "Read this"}}</delegate>'
    dispatcher = make_dispatcher(residents, ScriptedRunner(RunResult(Outcome.OK, output=output)))
    root = store.post_job(title="A notice for anybody")

    dispatcher.dispatch(NOW)

    (letter,) = store.inbox(RECEIVER)
    assert letter.parent_task_id == root.task_id
    assert letter.origin == f"task:{root.task_id}"
    assert letter.depth == 1


def test_a_routine_session_can_hand_work_over_through_the_wake_hook(
    fleet: Fleet, make_dispatcher: MakeDispatcher, store: Store
) -> None:
    """The scheduler's existing hook, unchanged in shape: no parent, its own initiative."""
    residents = fleet(sender_manifest(), receiver_manifest())
    dispatcher = make_dispatcher(residents)
    output = f'<delegate to="{RECEIVER}" route="inbox">{{"title": "Read this"}}</delegate>'

    dispatcher.harvest(residents[0].manifest, output)

    (letter,) = store.inbox(RECEIVER)
    assert letter.parent_task_id is None
    assert letter.origin == f"resident:{SENDER}"


def test_a_broken_store_does_not_turn_a_handoff_into_a_failed_task(
    fleet: Fleet, make_dispatcher: MakeDispatcher, store: Store
) -> None:
    residents = fleet(sender_manifest(), receiver_manifest())
    dispatcher = make_dispatcher(residents)
    store.close()
    output = f'<delegate to="{RECEIVER}" route="inbox">{{"title": "Read this"}}</delegate>'
    assert dispatcher.hand_over(residents[0].manifest, output) == ()


def test_a_session_of_an_unknown_resident_hands_nothing_over(
    fleet: Fleet, make_dispatcher: MakeDispatcher
) -> None:
    residents = fleet(sender_manifest(), receiver_manifest())
    stranger = resident_manifest("stranger-agent", name="Stranger")
    (loaded,) = fleet(stranger)
    assert make_dispatcher(residents).hand_over(loaded.manifest, "anything") == ()


# ------------------------------------------------------------------------- the pilot


def test_maren_hands_hob_a_piece_of_work_and_the_whole_chain_is_readable(
    store: Store, sink: ev.NullEmitter, tmp_path: Path
) -> None:
    """The pilot, on the residents this repo actually ships.

    Maren works a task, writes a ``<delegate>`` block in her output, and finishes. Steward
    validates it against both manifests, delivers it into Hob's declared route, and tells
    the village. Hob picks it up on his own next wake-up, works it as an ordinary
    provisioned session, and closes it — and every event from the handoff onwards names
    the parent, so the chain from the human's task to Hob's answer reads off the log.
    """
    residents = validate_tree(REPO_ROOT / "residents").residents
    dispatcher = b.Dispatcher(
        residents=residents,
        store=store,
        emitter=sink,
        workdir=tmp_path,
        library=library_for(REPO_ROOT / "residents"),
        runner_factory=lambda _spec: ScriptedRunner(),
    )
    maren = next(r for r in residents if r.id == "burrow-builder")
    # The human's task, and Maren already holds it: this is the session she is finishing.
    root = store.post_job(title="Rewrite the projection rules", posted_by="api")
    store.claim_next_job(
        claimant=maren.agent_id,
        skills=["research"],
        lease_expires_at=ev.utc_now_iso(NOW + timedelta(hours=1)),
    )

    output = (
        "I have the protocol half. The household half is not mine.\n\n"
        '<delegate to="life-agent" route="handoff">\n'
        '{"title": "Check what the errand list actually contains",\n'
        ' "detail": "I need the real shape of an errand before I render one."}\n'
        "</delegate>\n"
    )
    (delivery,) = dispatcher.hand_over(maren.manifest, output, parent_task_id=root.task_id)
    assert delivery.accepted
    letter = delivery.task
    assert letter is not None
    assert letter.assignee == "life-agent"
    assert letter.route == "handoff"

    delegated = sink.events[0]
    assert delegated.type == "task_delegated"
    assert delegated.agent_id == "steward:burrow-builder"
    assert delegated.payload["to"] == "claude-code:life-agent"
    assert delegated.payload["depth"] == 1

    (report,) = dispatcher.dispatch(NOW).reports
    assert report.resident_id == "life-agent"
    assert report.done

    assert [event.type for event in sink.events] == [
        "task_delegated",
        "task_claimed",
        "task_done",
    ]
    assert sink.events[-1].payload["parent_task_id"] == root.task_id

    chain = store.lineage(letter.task_id)
    assert [item.title for item in chain] == [
        "Rewrite the projection rules",
        "Check what the errand list actually contains",
    ]
    assert chain[-1].origin == f"task:{root.task_id}"
    assert chain[-1].status == "done"


def test_a_parent_steward_has_never_seen_is_refused(
    make_delegator: MakeDelegator, sink: ev.NullEmitter
) -> None:
    """Dropping the named parent would keep the letter and quietly lose the chain."""
    delegator = make_delegator()
    with pytest.raises(dg.DelegationError) as caught:
        delegator.delegate(
            sender=sender_of(delegator), handoff=handoff(), parent_task_id="not-a-task"
        )
    assert caught.value.reason == dg.UNKNOWN_PARENT
    assert sink.events == []


def test_a_resident_that_closed_its_door_stops_taking_letters(
    fleet: Fleet, make_dispatcher: MakeDispatcher, store: Store
) -> None:
    """Delivered while the route was open, then the manifest shut it: it waits on the mat."""
    store.delegate_job(
        title="Read the background", assignee=RECEIVER, delegated_by=SENDER, route="inbox"
    )
    (closed,) = fleet(receiver_manifest(routes=[inbox_route(status="disabled")]))

    assert make_dispatcher([closed]).dispatch(NOW).reports == ()
    assert [item.status for item in store.inbox(RECEIVER)] == ["open"]


# --------------------------------------------- delegated work under a budget (#7 and #8)
#
# The seam between the two features that landed side by side. A letter is a session, so
# it spends the receiver's day out of the receiver's cap — not the sender's, and not out
# of nothing. Three facts hold that together: a delegated run is ledgered as one, a
# receiver with no money left does not open its post, and the ledger can be read back by
# the origin the chain rolls up to rather than only by who happened to hold the work.


def budgeted_receiver(**budgets: object) -> dict[str, Any]:
    """Build the declared receiver, with a daily cap on what its inbox may cost it."""
    data = receiver_manifest()
    data["budgets"] = dict(budgets)
    return data


@pytest.fixture
def guard(store: Store, sink: ev.NullEmitter) -> BudgetGuard:
    return BudgetGuard(store, sink)


def costly_runner(cost: float) -> ScriptedRunner:
    """Return a runner whose session reports what it cost, the way a claude run does."""
    return ScriptedRunner(
        RunResult(
            outcome=Outcome.OK,
            output="read it",
            exit_status=0,
            cost_usd=cost,
            input_tokens=600,
            output_tokens=400,
        )
    )


def guarded_dispatcher(  # noqa: PLR0913, PLR0917 — the collaborators an entry point wires
    residents: list[Resident],
    store: Store,
    sink: ev.NullEmitter,
    guard: BudgetGuard,
    workdir: Path,
    runner: Runner | None = None,
) -> b.Dispatcher:
    """Build a dispatcher with the budget guard wired in, as every entry point does."""
    return b.Dispatcher(
        residents=residents,
        store=store,
        emitter=sink,
        workdir=workdir,
        runner_factory=lambda _spec: runner or ScriptedRunner(),
        guard=guard,
    )


def test_a_delegated_session_is_ledgered_as_delegated_and_counts_against_the_day(
    fleet: Fleet, store: Store, sink: ev.NullEmitter, guard: BudgetGuard, tmp_path: Path
) -> None:
    """Somebody else's request still spends your day, and the ledger says whose day it was.

    ``kind="delegated"`` rather than ``"task"``, because "what did the board cost me" and
    "what did my neighbours cost me" are two different questions a household will want
    apart. And it is the *receiver's* cap it counts against: the resident that burned the
    tokens is the resident that has fewer of them left.
    """
    residents = fleet(sender_manifest(), budgeted_receiver(daily_cost_usd=10.0))
    dispatcher = guarded_dispatcher(residents, store, sink, guard, tmp_path, costly_runner(2.5))
    letter = dispatcher.delegator.delegate(sender=residents[0], handoff=handoff())

    (report,) = dispatcher.dispatch(NOW).reports

    assert report.done
    (entry,) = store.ledger(RECEIVER)
    assert entry.kind == "delegated"
    assert entry.ref == letter.task_id
    assert entry.cost_usd == pytest.approx(2.5)
    assert entry.tokens == 1000
    # The sender asked for the work; it did not pay for it.
    assert store.ledger(SENDER) == []

    status = guard.status(residents[1].manifest, NOW)
    assert status.spend.cost_usd == pytest.approx(2.5)
    assert status.spend.runs == 1


def test_a_paused_receiver_leaves_its_post_on_the_mat(
    fleet: Fleet, store: Store, sink: ev.NullEmitter, guard: BudgetGuard, tmp_path: Path
) -> None:
    """A letter is not a way around a cap the same household set.

    The item is not refused, dropped, or failed — none of those would be true, and a
    neighbour is still waiting on an answer. It stays exactly where it was delivered,
    open and unclaimed, for whoever lifts the pause. Nothing ran, so nothing was spent.
    """
    residents = fleet(sender_manifest(), budgeted_receiver(daily_cost_usd=1.0))
    runner = costly_runner(0.25)
    dispatcher = guarded_dispatcher(residents, store, sink, guard, tmp_path, runner)
    letter = dispatcher.delegator.delegate(sender=residents[0], handoff=handoff())
    # Yesterday's answer already blew today's dollar.
    guard.record(residents[1].manifest, result=RunResult(outcome=Outcome.OK, cost_usd=4.0), now=NOW)
    sink.events.clear()

    run = dispatcher.dispatch(NOW).reports

    assert run == (), "a paused resident must not work an inbox item either"
    assert runner.requests == [], "no session may be started for a paused resident"
    assert [item.status for item in store.inbox(RECEIVER)] == ["open"]
    assert job(store, letter.task_id).claimant is None
    # One knock, naming the number — the same machinery a refused fire goes through.
    assert [event.type for event in sink.events] == ["needs_human"]
    assert "daily_cost_usd" in sink.events[0].payload["message"]
    assert store.budget_pause(RECEIVER) is not None
    # And nothing new on the ledger: a run that never happened costs nothing.
    assert [entry.kind for entry in store.ledger(RECEIVER)] == ["routine"]


def test_spend_rolls_up_to_the_origin_the_chain_descends_from(
    fleet: Fleet, store: Store, sink: ev.NullEmitter, guard: BudgetGuard, tmp_path: Path
) -> None:
    """Roll one question's cost up across everybody who touched it.

    The question ``origin`` was recorded for. A human posts one task, the holder hands a
    piece of it to a neighbour, and the neighbour's spend rolls up to the original task
    rather than stopping at whichever resident was last in the line. A routine that came
    off no task at all is reported as unattributed rather than dropped.
    """
    residents = fleet(sender_manifest(), receiver_manifest())
    dispatcher = guarded_dispatcher(residents, store, sink, guard, tmp_path, costly_runner(1.5))
    root = store.post_job(title="Rewrite the projection rules", posted_by="api")
    dispatcher.delegator.delegate(
        sender=residents[0], handoff=handoff(), parent_task_id=root.task_id
    )
    # A routine of the sender's own, which belongs to no chain.
    guard.record(
        residents[0].manifest,
        result=RunResult(outcome=Outcome.OK, cost_usd=0.75),
        ref="daily-summary",
        now=NOW,
    )

    dispatcher.dispatch(NOW)

    rollup = {spend.origin: spend for spend in store.spend_by_origin()}
    assert set(rollup) == {f"task:{root.task_id}", ORIGIN_UNATTRIBUTED}
    chained = rollup[f"task:{root.task_id}"]
    assert chained.cost_usd == pytest.approx(1.5)
    assert chained.tokens == 1000
    assert chained.runs == 1
    assert rollup[ORIGIN_UNATTRIBUTED].cost_usd == pytest.approx(0.75)


def test_the_rollup_honours_the_window_it_is_asked_about(
    fleet: Fleet, store: Store, sink: ev.NullEmitter, guard: BudgetGuard, tmp_path: Path
) -> None:
    """Yesterday's chain is not today's bill: the same half-open window the gauges use."""
    residents = fleet(sender_manifest(), receiver_manifest())
    dispatcher = guarded_dispatcher(residents, store, sink, guard, tmp_path, costly_runner(3.0))
    dispatcher.delegator.delegate(sender=residents[0], handoff=handoff())

    dispatcher.dispatch(NOW)

    window = guard.status(residents[1].manifest, NOW).window
    assert [spend.cost_usd for spend in store.spend_by_origin(since=window.start_iso)] == [
        pytest.approx(3.0)
    ]
    assert store.spend_by_origin(until=window.start_iso) == []


def test_a_resident_on_both_lists_is_paused_once_not_knocked_at_twice(
    fleet: Fleet, store: Store, sink: ev.NullEmitter, guard: BudgetGuard, tmp_path: Path
) -> None:
    """The inbox and the board each ask the budget, and one exhausted cap is one knock.

    The dedupe is the conditional insert in the store, not an order the two loops have to
    remember to keep. A household woken by two notifications for one budget would learn to
    ignore both.
    """
    receiver = budgeted_receiver(daily_cost_usd=1.0)
    receiver["routes"] = [
        *receiver["routes"],
        {"id": "job-board", "kind": "job-board", "address": "b"},
    ]
    receiver["board"] = {"claim": True, "max_claims_per_wake": 1}
    residents = fleet(sender_manifest(), receiver)
    dispatcher = guarded_dispatcher(residents, store, sink, guard, tmp_path, costly_runner(0.25))
    dispatcher.delegator.delegate(sender=residents[0], handoff=handoff())
    store.post_job(title="A notice for anybody")
    guard.record(residents[1].manifest, result=RunResult(outcome=Outcome.OK, cost_usd=9.0), now=NOW)
    sink.events.clear()

    assert dispatcher.dispatch(NOW).reports == ()

    assert [event.type for event in sink.events] == ["needs_human"]
    assert [item.status for item in store.inbox(RECEIVER)] == ["open"]
    assert [item.status for item in store.jobs()] == ["open", "open"]
