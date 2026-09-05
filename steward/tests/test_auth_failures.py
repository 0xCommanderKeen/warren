"""Failure throttling as observed by HTTP callers and the request log."""

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from fastapi.testclient import TestClient

from steward.api import ApiConfig, create_app
from steward.auth_failures import AuthFailures, FailurePolicy
from steward.store import Store
from support.api import TOKEN, ApiFactory, mint_operator, open_session_run
from support.api import api as api  # noqa: PLC0414 — pytest fixture discovery


def test_failed_auth_is_throttled_but_valid_token_still_works(tmp_path: Path) -> None:
    clock = [0.0]
    with Store(":memory:") as store:
        app = create_app(
            ApiConfig(token="valid", residents_dir=tmp_path),
            store=store,
            monotonic=lambda: clock[0],
        )
        with TestClient(app) as client:
            assert [client.get("/residents").status_code for _ in range(6)] == [401] * 5 + [429]
            refused = client.get("/residents")
            assert refused.headers["Retry-After"] == "12"
            assert (
                client.get("/residents", headers={"Authorization": "Bearer valid"}).status_code
                == 200
            )
            clock[0] = 12.0
            assert client.get("/residents").status_code == 401
            assert client.get("/residents").status_code == 429


def test_spoofed_forwarding_does_not_reset_bucket_and_audit_is_bounded(tmp_path: Path) -> None:
    with Store(":memory:") as store:
        app = create_app(
            ApiConfig(token="valid", residents_dir=tmp_path), store=store, monotonic=lambda: 0.0
        )
        with TestClient(app, client=("192.0.2.1", 1)) as client:
            for n in range(100):
                response = client.get(
                    "/residents?secret=never-store-this",
                    headers={
                        "Authorization": "Bearer never-store-this",
                        "X-Forwarded-For": f"192.0.2.{n}",
                    },
                )
            assert response.status_code == 429
            rows = client.get("/requests", headers={"Authorization": "Bearer valid"}).json()
            assert "never-store-this" not in str(rows)
            assert len(store.recent_requests(limit=100)) == 1
        records = store.recent_requests(limit=100)
        assert len(records) == 2
        assert sum(r.detail["failed"] for r in records) == 100
        assert sum(r.detail["throttled"] for r in records) == 95


def test_sources_are_isolated_and_idle_buckets_expire(tmp_path: Path) -> None:
    clock = [0.0]
    with Store(":memory:") as store:
        app = create_app(
            ApiConfig(token="valid", residents_dir=tmp_path),
            store=store,
            monotonic=lambda: clock[0],
        )
        with TestClient(app, client=("192.0.2.1", 1)) as first:
            for _ in range(6):
                first.get("/residents")
            second = TestClient(app, client=("192.0.2.2", 1))
            assert second.get("/residents").status_code == 401
            clock[0] = 301.0
            assert [first.get("/residents").status_code for _ in range(6)] == [401] * 5 + [429]


def test_capacity_evicts_oldest_source_and_concurrent_failures_share_allowance() -> None:
    with Store(":memory:") as store:
        gate = AuthFailures(store, FailurePolicy(capacity=2), now=lambda: 0.0)
        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(gate.refuse, ["192.0.2.1"] * 20))
        assert results.count(0) == 5
        assert results.count(12) == 15
        assert gate.refuse("192.0.2.2") == 0
        assert gate.refuse("192.0.2.3") == 0
        assert gate.refuse("192.0.2.1") == 0


def test_all_valid_credential_kinds_survive_a_throttled_source(api: ApiFactory) -> None:
    harness = api(token_previous="old-secret", token_previous_until="2099-01-01T00:00:00Z")
    operator = mint_operator(harness)
    session = open_session_run(harness)
    for _ in range(6):
        refused = harness.client.get("/residents", headers={"Authorization": "Bearer wrong"})
    assert refused.status_code == 429
    for token in (TOKEN, "old-secret", operator, session):
        assert (
            harness.client.get(
                "/residents", headers={"Authorization": f"Bearer {token}"}
            ).status_code
            == 200
        )
    forbidden = harness.client.post(
        "/jobs", json={"title": "Must not create"}, headers={"Authorization": f"Bearer {session}"}
    )
    assert forbidden.status_code == 403
    assert harness.store.jobs() == []
