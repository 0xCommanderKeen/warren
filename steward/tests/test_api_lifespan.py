"""API behavior: lifespan."""

import threading

import pytest

from support.api import (
    ApiFactory,
    _pending,
)
from support.api import (
    api as api,  # noqa: PLC0414 — pytest fixture discovery
)


def test_lifespan_surfaces_an_outbox_worker_that_cannot_stop(api: ApiFactory) -> None:
    harness = api()
    worker = harness.client.app.state.approval_outbox
    entered = threading.Event()
    release = threading.Event()

    class SlowTransitions:
        def __init__(self) -> None:
            self.store = harness.store

        def reconcile_announcements(self) -> None:
            entered.set()
            release.wait()

    worker.transitions = SlowTransitions()
    worker.close_timeout = 0.01
    with pytest.raises(TimeoutError, match="did not stop"), harness.client:
        assert entered.wait(1.0)
    assert worker.alive
    release.set()
    worker.close(timeout=1.0)


def test_a_second_lifespan_drains_work_committed_after_the_first(api: ApiFactory) -> None:
    harness = api()

    with harness.client:
        pass

    approval_id = _pending(harness)
    record, recorded = harness.store.decide(approval_id, "approve")
    assert recorded
    assert record is not None
    assert harness.events("needs_human_resolved") == []

    with harness.client:
        for _ in range(100):
            resolved = harness.events("needs_human_resolved")
            if resolved:
                break
            threading.Event().wait(0.01)

    assert [event["payload"]["request_id"] for event in resolved] == [approval_id]
