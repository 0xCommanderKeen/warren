"""API behavior: auth."""

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from steward.api import (
    ApiConfig,
    ApiError,
    create_app,
)
from support.api import (
    TOKEN,
    ApiFactory,
    _pending,
)
from support.api import (
    api as api,  # noqa: PLC0414 — pytest fixture discovery
)

# --------------------------------------------------------------------------------------
# auth
# --------------------------------------------------------------------------------------


def test_an_unset_token_refuses_to_start(tmp_path: Path) -> None:
    with pytest.raises(ApiError, match="--allow-open"):
        create_app(ApiConfig(residents_dir=tmp_path, token=None))


def test_a_blank_token_counts_as_unset(tmp_path: Path) -> None:
    with pytest.raises(ApiError, match="STEWARD_TOKEN"):
        create_app(ApiConfig(residents_dir=tmp_path, token="   "))


def test_open_mode_is_the_only_way_to_serve_without_a_token(api: ApiFactory) -> None:
    harness = api(token=None, allow_open=True)
    assert harness.client.get("/residents").status_code == 200


def test_a_missing_token_is_401(api: ApiFactory) -> None:
    harness = api()
    anonymous = TestClient(harness.client.app)
    response = anonymous.get("/residents")
    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"
    assert response.json()["detail"]["error"] == "unauthorized"


def test_a_wrong_token_is_401_and_queues_nothing(api: ApiFactory) -> None:
    harness = api()
    response = harness.client.post(
        "/jobs", json={"title": "Research X"}, headers={"Authorization": "Bearer wrong-secret"}
    )
    assert response.status_code == 401
    assert harness.store.jobs() == []
    assert harness.store.export_request_history() == []
    assert harness.events() == []


def test_a_token_in_the_wrong_scheme_is_401(api: ApiFactory) -> None:
    harness = api()
    response = harness.client.get("/residents", headers={"Authorization": f"Basic {TOKEN}"})
    assert response.status_code == 401


def test_the_token_is_compared_in_constant_time(
    api: ApiFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A naive == leaks the token one byte at a time to anyone who can time a request."""
    calls: list[tuple[bytes, bytes]] = []

    def spy(left: bytes, right: bytes) -> bool:
        calls.append((left, right))
        return left == right

    monkeypatch.setattr("steward.api.compare_digest", spy)
    harness = api()

    assert harness.client.get("/residents").status_code == 200
    assert calls == [(TOKEN.encode(), TOKEN.encode())]

    calls.clear()
    same_length = "a-shared-secreT"
    assert (
        harness.client.get(
            "/residents", headers={"Authorization": f"Bearer {same_length}"}
        ).status_code
        == 401
    )
    assert calls == [(same_length.encode(), TOKEN.encode())]


@pytest.mark.parametrize(
    "values",
    [
        [f"Bearer {TOKEN}", "Bearer wrong"],
        ["Bearer wrong", f"Bearer {TOKEN}"],
        [f"Bearer {TOKEN}", f"Bearer {TOKEN}"],
    ],
)
def test_duplicate_authorization_is_401_before_deep_body(
    api: ApiFactory, values: list[str]
) -> None:
    harness = api()
    request_id = _pending(harness)
    raw = '{"decision":"edit","edit":' + "[" * 1_000 + "0" + "]" * 1_000 + "}"
    response = harness.client.post(
        f"/approvals/{request_id}",
        content=raw,
        headers=[("content-type", "application/json"), *[("authorization", v) for v in values]],
    )
    assert response.status_code == 401
    assert response.json()["detail"]["error"] == "unauthorized"
    assert harness.store.approval(request_id).pending  # ty: ignore[unresolved-attribute]


def test_the_schema_is_not_served_unauthenticated(api: ApiFactory) -> None:
    harness = api()
    assert harness.client.get("/openapi.json").status_code == 404
    assert harness.client.get("/docs").status_code == 404
