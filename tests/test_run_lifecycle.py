"""Crash recovery and fencing at the run terminal seam."""

from datetime import UTC, datetime, timedelta

from steward import events as ev
from steward.run_lifecycle import RunTransitions
from steward.store import Store

NOW = datetime(2026, 8, 27, 12, tzinfo=UTC)


def _open(store: Store) -> None:
    store.open_run(
        run_id="run-1",
        kind="routine",
        agent_id="claude-code:test",
        project="test",
        ref="daily",
        timeout_s=60,
        event_log_path="events.jsonl",
        owner_token="owner-fence",
        now=ev.utc_now_iso(NOW - timedelta(hours=1)),
    )


def _success() -> ev.Event:
    return ev.RunContext("claude-code:test", "test", "daily", "run-1").finished(
        outcome="success", artifacts=(), duration_s=1
    )


def test_crash_after_claim_before_emit_replays_chosen_outcome() -> None:
    store = Store(":memory:")
    _open(store)
    runs = RunTransitions(store)
    assert runs.session_claim("run-1", _success(), owner_token="owner-fence", now=NOW)

    sink = ev.NullEmitter()
    assert runs.publish_pending(sink, now=NOW) == ["run-1"]
    assert [event.type for event in sink.events] == [ev.ROUTINE_FINISHED]
    assert store.open_runs() == []


def test_crash_after_emit_before_finalize_reemits_same_identity() -> None:
    store = Store(":memory:")
    _open(store)
    runs = RunTransitions(store)
    assert runs.session_claim("run-1", _success(), owner_token="owner-fence", now=NOW)
    chosen = store.terminal_runs()[0]
    first = ev.NullEmitter()
    runs_event = ev.Event(  # reconstruct the simulated first publish
        type=ev.ROUTINE_FINISHED,
        agent_id="claude-code:test",
        project="test",
        payload={"run_id": "run-1", "event_id": chosen.terminal_event_id},
    )
    first.emit_durable(runs_event)

    second = ev.NullEmitter()
    assert runs.publish_pending(second, now=NOW) == ["run-1"]
    assert runs_event.payload["event_id"] == second.events[0].payload["event_id"]


def test_watchdog_cannot_override_live_owner_or_chosen_success() -> None:
    store = Store(":memory:")
    _open(store)
    assert store.renew_run("run-1", owner_token="owner-fence", now=ev.utc_now_iso(NOW))
    runs = RunTransitions(store)
    failure = ev.routine_failed_event(
        agent_id="claude-code:test",
        project="test",
        routine="daily",
        run_id="run-1",
        error="gone",
    )
    assert not runs.watchdog_claim("run-1", failure, now=NOW, grace_s=120)
    assert runs.session_claim("run-1", _success(), owner_token="owner-fence", now=NOW)
    assert not runs.watchdog_claim("run-1", failure, now=NOW + timedelta(hours=1), grace_s=120)


def test_wrong_owner_token_cannot_renew_or_choose_terminal() -> None:
    store = Store(":memory:")
    _open(store)
    runs = RunTransitions(store)
    assert not store.renew_run("run-1", owner_token="intruder")
    assert not runs.session_claim("run-1", _success(), owner_token="intruder", now=NOW)


def test_zero_durable_sinks_does_not_finalize_the_chosen_outcome() -> None:
    class NoSink:
        def emit(self, event: ev.Event) -> bool:  # noqa: ARG002
            return False

    store = Store(":memory:")
    _open(store)
    runs = RunTransitions(store)
    assert runs.session_claim("run-1", _success(), owner_token="owner-fence", now=NOW)

    assert runs.publish_pending(NoSink(), now=NOW) == []
    assert [run.run_id for run in store.open_runs()] == ["run-1"]
