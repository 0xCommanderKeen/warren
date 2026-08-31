"""The cross-process one-session-per-resident claim (warren#111)."""

import os
import signal
import threading
import time
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from conftest import ClaimHolderSpawner
from steward.claims import (
    CLAIM_GRACE_S,
    ClaimRefused,
    ResidentClaim,
    ResidentClaims,
)
from steward.events import utc_now_iso
from steward.store import Store


@pytest.fixture
def store(tmp_path: Path) -> Iterator[Store]:
    with Store(tmp_path / "steward.db") as opened:
        yield opened


def test_a_hold_wins_the_claim_and_reports_the_holder(store: Store):
    claims = ResidentClaims(store, heartbeat_every_s=3600.0)
    with claims.hold("hob", kind="routine", ref="morning-brief", run_id="r1") as held:
        assert not isinstance(held, ClaimRefused)
        live = claims.holder("hob")
        assert live is not None
        assert live.kind == "routine"
        assert live.ref == "morning-brief"
        assert live.run_id == "r1"
    assert claims.holder("hob") is None


def test_a_second_process_is_refused_while_the_first_holds(store: Store):
    first = ResidentClaims(store, heartbeat_every_s=3600.0, holder="host:111")
    second = ResidentClaims(store, heartbeat_every_s=3600.0, holder="host:222")
    with first.hold("hob", kind="routine", ref="morning-brief", run_id="r1"):
        with second.hold("hob", kind="chat", ref="c-1") as refused:
            assert isinstance(refused, ClaimRefused)
            assert "one session per resident" in refused.reason
            assert "morning-brief" in refused.reason
            assert "host:111" in refused.reason
        # The refused holder released nothing: the first claim is untouched.
        assert claims_row(store, "hob").holder == "host:111"


def test_two_residents_do_not_contend(store: Store):
    claims = ResidentClaims(store, heartbeat_every_s=3600.0)
    with (
        claims.hold("hob", kind="routine", ref="a") as one,
        claims.hold("nib", kind="routine", ref="b") as two,
    ):
        assert not isinstance(one, ClaimRefused)
        assert not isinstance(two, ClaimRefused)


def test_a_released_claim_is_immediately_available(store: Store):
    first = ResidentClaims(store, heartbeat_every_s=3600.0)
    second = ResidentClaims(store, heartbeat_every_s=3600.0)
    with first.hold("hob", kind="routine", ref="a"):
        pass
    with second.hold("hob", kind="routine", ref="b") as held:
        assert not isinstance(held, ClaimRefused)


def test_a_dead_holder_is_reclaimed_only_after_the_grace(store: Store):
    """The whole crash story: a holder that stopped beating is takeable, and not before."""
    now = datetime.now(UTC)
    store.claim_resident(
        "hob",
        token="dead",
        holder="host:999",
        kind="routine",
        ref="morning-brief",
        stale_before=utc_now_iso(now - timedelta(seconds=CLAIM_GRACE_S)),
        now=utc_now_iso(now - timedelta(seconds=CLAIM_GRACE_S / 2)),
    )
    fresh = ResidentClaims(store, heartbeat_every_s=3600.0, clock=lambda: now)
    with fresh.hold("hob", kind="routine", ref="b") as refused:
        assert isinstance(refused, ClaimRefused)

    later = now + timedelta(seconds=CLAIM_GRACE_S)
    patient = ResidentClaims(store, heartbeat_every_s=3600.0, clock=lambda: later)
    with patient.hold("hob", kind="routine", ref="b") as held:
        assert not isinstance(held, ClaimRefused)


def test_a_reclaimed_holder_cannot_release_the_claim_that_replaced_it(store: Store):
    """The fencing token: a stale holder that wakes up must not free the new session."""
    now = datetime.now(UTC)
    store.claim_resident(
        "hob",
        token="stale",
        holder="host:999",
        stale_before=utc_now_iso(now - timedelta(seconds=CLAIM_GRACE_S)),
        now=utc_now_iso(now - timedelta(seconds=CLAIM_GRACE_S * 2)),
    )
    taken = store.claim_resident(
        "hob",
        token="fresh",
        holder="host:111",
        stale_before=utc_now_iso(now - timedelta(seconds=CLAIM_GRACE_S)),
        now=utc_now_iso(now),
    )
    assert taken is not None
    assert store.release_resident_claim("hob", token="stale") is False
    assert store.renew_resident_claim("hob", token="stale") is False
    assert claims_row(store, "hob").released_at is None
    assert store.release_resident_claim("hob", token="fresh") is True


def test_the_heartbeat_keeps_a_long_hold_alive(store: Store):
    """A session longer than the grace stays claimed, because the hold keeps beating."""
    claims = ResidentClaims(store, heartbeat_every_s=0.01)
    with claims.hold("hob", kind="routine", ref="a"):
        opened = claims_row(store, "hob").heartbeat_at
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            if claims_row(store, "hob").heartbeat_at > opened:
                break
            time.sleep(0.01)
        assert claims_row(store, "hob").heartbeat_at > opened


def test_a_claim_stolen_under_a_live_holder_is_reported(store: Store, caplog):
    """Releasing a claim somebody else now holds is a fact worth a line in the log."""
    claims = ResidentClaims(store, heartbeat_every_s=3600.0)
    with caplog.at_level("WARNING"), claims.hold("hob", kind="routine", ref="a"):
        store.claim_resident(
            "hob",
            token="thief",
            holder="host:999",
            stale_before=utc_now_iso(datetime.now(UTC) + timedelta(days=1)),
        )
    assert "claim on 'hob' was taken" in caplog.text


def test_holder_reads_a_released_row_as_free(store: Store):
    store.claim_resident("hob", token="t", holder="h", stale_before=utc_now_iso())
    store.release_resident_claim("hob", token="t")
    assert ResidentClaims(store).holder("hob") is None
    assert store.resident_claim("hob") is not None


def test_holder_reads_a_stale_row_as_free(store: Store):
    now = datetime.now(UTC)
    store.claim_resident(
        "hob",
        token="t",
        holder="h",
        stale_before=utc_now_iso(now),
        now=utc_now_iso(now - timedelta(seconds=CLAIM_GRACE_S * 2)),
    )
    assert ResidentClaims(store, clock=lambda: now).holder("hob") is None


def test_a_resident_nobody_has_ever_run_has_no_row_at_all(store: Store):
    assert store.resident_claim("hob") is None


def test_describe_names_what_is_running():
    claim = ResidentClaim(
        resident_id="hob",
        token="t",
        holder="dxp2800:42",
        kind="routine",
        ref="morning-brief",
        run_id="abc",
        claimed_at="2026-08-31T09:00:00.000Z",
        heartbeat_at="2026-08-31T09:00:00.000Z",
    )
    described = claim.describe()
    assert "routine 'morning-brief'" in described
    assert "dxp2800:42" in described
    assert "2026-08-31T09:00:00.000Z" in described
    assert "run abc" in described


def test_describe_survives_a_claim_with_no_reference():
    claim = ResidentClaim(
        resident_id="hob",
        token="t",
        holder="dxp2800:42",
        kind="chat",
        ref="",
        run_id="",
        claimed_at="2026-08-31T09:00:00.000Z",
        heartbeat_at="2026-08-31T09:00:00.000Z",
    )
    assert claim.describe() == "a chat session held by dxp2800:42 since 2026-08-31T09:00:00.000Z"


class BrokenStore:
    """Every claim operation raises, the way an unreachable database does."""

    def claim_resident(self, *_args: object, **_kwargs: object) -> ResidentClaim | None:
        """Fail the way a locked database fails."""
        raise RuntimeError("database is locked")

    def renew_resident_claim(self, *_args: object, **_kwargs: object) -> bool:
        """Fail the way a locked database fails."""
        raise RuntimeError("database is locked")

    def release_resident_claim(self, *_args: object, **_kwargs: object) -> bool:
        """Fail the way a locked database fails."""
        raise RuntimeError("database is locked")

    def resident_claim(self, *_args: object, **_kwargs: object) -> ResidentClaim | None:
        """Fail the way a locked database fails."""
        raise RuntimeError("database is locked")


def test_a_broken_store_does_not_take_the_session_down(caplog):
    """A claim steward cannot write is a warning, not a routine that never fired."""
    claims = ResidentClaims(BrokenStore(), heartbeat_every_s=3600.0)
    with caplog.at_level("WARNING"), claims.hold("hob", kind="routine", ref="a") as held:
        assert not isinstance(held, ClaimRefused)
        assert held.claim is None
    assert "runs unclaimed" in caplog.text
    assert claims.holder("hob") is None
    assert "could not read the session claim" in caplog.text


def test_a_broken_store_does_not_break_the_release_or_the_heartbeat(caplog):
    """The two writes that happen after a hold started must not raise out of it either."""
    claims = ResidentClaims(BrokenStore(), heartbeat_every_s=0.01)
    stop = threading.Event()
    beating = threading.Thread(target=claims._beat, args=("hob", "token", stop))
    with caplog.at_level("WARNING"):
        beating.start()
        time.sleep(0.1)
        stop.set()
        beating.join(timeout=5.0)
        claims._release("hob", "token")
    assert not beating.is_alive()
    assert "could not renew the session claim" in caplog.text
    assert "could not release the session claim" in caplog.text


def test_a_heartbeat_that_finds_the_claim_gone_stops_beating(store: Store, caplog):
    """A holder that was reclaimed says so once and stops, rather than beating forever."""
    claims = ResidentClaims(store, heartbeat_every_s=0.01)
    stop = threading.Event()
    with caplog.at_level("WARNING"):
        beating = threading.Thread(target=claims._beat, args=("hob", "gone", stop))
        beating.start()
        beating.join(timeout=5.0)
    assert not beating.is_alive()
    assert "no longer holds the resident claim" in caplog.text


def test_a_heartbeat_that_loses_to_its_own_shutdown_says_nothing(caplog):
    """The hold ended and released the claim; a beat still in flight is not a reclaim.

    Calling that "another process reclaimed it" would be steward reporting an incident that
    did not happen, at the exact moment a perfectly ordinary session ended.
    """
    stop = threading.Event()

    class ReleasingStore(BrokenStore):
        """Ends the hold underneath the beat, exactly as ``_release`` would."""

        def renew_resident_claim(self, *_args: object, **_kwargs: object) -> bool:
            stop.set()
            return False

    claims = ResidentClaims(ReleasingStore(), heartbeat_every_s=0.01)
    with caplog.at_level("WARNING"):
        beating = threading.Thread(target=claims._beat, args=("hob", "token", stop))
        beating.start()
        beating.join(timeout=5.0)
    assert not beating.is_alive()
    assert "no longer holds the resident claim" not in caplog.text


def test_a_hold_that_cannot_start_its_heartbeat_gives_the_claim_back(
    store: Store, monkeypatch: pytest.MonkeyPatch
):
    """The claim is written before the thread exists, so the thread's failure must free it.

    A row nothing ever releases would refuse the resident for a full grace window over an
    error the operator never saw.
    """

    def no_threads_left(_self: threading.Thread) -> None:
        raise RuntimeError("can't start new thread")

    monkeypatch.setattr(threading.Thread, "start", no_threads_left)
    claims = ResidentClaims(store, heartbeat_every_s=3600.0)
    with pytest.raises(RuntimeError, match="can't start new thread"), claims.hold("hob"):
        pytest.fail("the body must not run when the heartbeat could not be started")
    assert claims.holder("hob") is None
    assert claims_row(store, "hob").released_at is not None


# --------------------------------------------------------------------------------------
# two real processes
# --------------------------------------------------------------------------------------


def test_a_second_process_really_cannot_take_a_live_claim(
    tmp_path: Path, store: Store, claim_holder: ClaimHolderSpawner
):
    """The point of the whole issue, proven with two operating-system processes."""
    claim_holder(tmp_path / "steward.db", "hob", ref="morning-brief", run_id="held-run")
    claims = ResidentClaims(store, heartbeat_every_s=3600.0)
    with claims.hold("hob", kind="routine", ref="morning-brief") as refused:
        assert isinstance(refused, ClaimRefused)
        assert "morning-brief" in refused.reason
        assert "held-run" in refused.reason
    # A different resident is not blocked by hob's session.
    with claims.hold("nib", kind="routine", ref="other") as held:
        assert not isinstance(held, ClaimRefused)


def test_a_killed_holder_s_claim_is_reclaimable(
    tmp_path: Path, store: Store, claim_holder: ClaimHolderSpawner
):
    """SIGKILL is the crash: nothing releases the claim, and the grace still frees it."""
    grace_s = 1.0
    holder = claim_holder(tmp_path / "steward.db", "hob", grace_s=grace_s, beat_s=0.05)
    claims = ResidentClaims(store, grace_s=grace_s, heartbeat_every_s=3600.0)
    with claims.hold("hob", kind="routine", ref="mine") as refused:
        assert isinstance(refused, ClaimRefused)
    os.kill(holder.pid, signal.SIGKILL)
    holder.process.wait(timeout=30)
    # The row is still there, unreleased: nothing ran a finally block.
    row = store.resident_claim("hob")
    assert row is not None
    assert row.released_at is None
    deadline = time.monotonic() + 30.0
    while time.monotonic() < deadline:
        with claims.hold("hob", kind="routine", ref="mine") as held:
            if not isinstance(held, ClaimRefused):
                return
        time.sleep(0.1)
    raise AssertionError("a dead holder's claim never became reclaimable")


def claims_row(store: Store, resident_id: str) -> ResidentClaim:
    """Read the raw claim row, live or not."""
    row = store.resident_claim(resident_id)
    assert row is not None
    return row
