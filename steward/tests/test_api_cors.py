"""API behavior: cors."""

from collections.abc import Callable

import pytest
from fastapi.testclient import TestClient

from support.api import (
    AUTH,
    FORBIDDEN,
    NEW_RESIDENT,
    ApiFactory,
    Harness,
    _pending,
)
from support.api import (
    api as api,  # noqa: PLC0414 — pytest fixture discovery
)
from support.api import (
    village as village,  # noqa: PLC0414 — pytest fixture discovery
)
from support.api import (
    writable as writable,  # noqa: PLC0414 — pytest fixture discovery
)

# --------------------------------------------------------------------------------------
# CORS, and the language of the answers
# --------------------------------------------------------------------------------------


def test_cors_headers_appear_only_for_a_configured_origin(api: ApiFactory) -> None:
    village = "http://village.local:8080"
    harness = api(cors_origins=(village,))

    allowed = harness.client.get("/residents", headers={"Origin": village})
    assert allowed.headers["access-control-allow-origin"] == village

    other = harness.client.get("/residents", headers={"Origin": "https://evil.example"})
    assert "access-control-allow-origin" not in other.headers


@pytest.mark.parametrize("method", ["POST", "PUT"])
@pytest.mark.parametrize("allowed", [True, False])
def test_write_preflight_obeys_the_origin_allowlist_without_a_token(
    api: ApiFactory, method: str, *, allowed: bool
) -> None:
    """A browser cannot put the bearer token on a preflight, so the gate cannot see it."""
    village = "http://village.local:8080"
    harness = api(cors_origins=(village,))
    anonymous = TestClient(harness.client.app)
    # A new public write method must also join this preflight matrix.
    methods = {
        verb.upper()
        for path in harness.client.app.openapi()["paths"].values()
        for verb in path
        if verb.upper() in {"POST", "PUT", "PATCH", "DELETE"}
    }
    assert methods == {"POST", "PUT"}

    response = anonymous.options(
        "/residents/test-agent/routines/daily-summary/run",
        headers={
            "Origin": village if allowed else "https://evil.example",
            "Access-Control-Request-Method": method,
            "Access-Control-Request-Headers": "authorization, content-type",
        },
    )
    if allowed:
        assert response.status_code == 200
        assert response.headers["access-control-allow-origin"] == village
        assert method in response.headers["access-control-allow-methods"].split(", ")
        assert {"authorization", "content-type"} <= {
            header.strip().lower()
            for header in response.headers["access-control-allow-headers"].split(",")
        }
    else:
        assert response.status_code == 400
        assert "access-control-allow-origin" not in response.headers
    assert harness.events() == []


@pytest.mark.parametrize("path", ["/skills/daily-summary", "/residents/test-agent/declaration"])
def test_cross_origin_editor_can_preflight_and_save_put(
    writable: Callable[..., Harness], path: str
) -> None:
    village = "http://village.local:8080"
    harness = writable(cors_origins=(village,))
    browser = TestClient(harness.client.app)
    preflight = browser.options(
        path,
        headers={
            "Origin": village,
            "Access-Control-Request-Method": "PUT",
            "Access-Control-Request-Headers": "authorization, content-type",
        },
    )
    assert preflight.status_code == 200
    assert preflight.headers["access-control-allow-origin"] == village
    headers = AUTH | {"Origin": village, "Content-Type": "application/json"}
    loaded = browser.get(path, headers=headers).json()
    if "manifest" in loaded:
        loaded["manifest"]["summary"] = "Edited from the village."
        edit = {"manifest": loaded["manifest"], "revision": loaded["revision"]}
    else:
        edit = {
            "description": "Edited from the village.",
            "body": "Read. Answer. Escalate.",
            "revision": loaded["revision"],
        }

    saved = browser.put(path, headers=headers, json=edit)

    assert saved.status_code == 200
    assert saved.headers["access-control-allow-origin"] == village
    reread = browser.get(path, headers=headers)
    assert reread.headers["access-control-allow-origin"] == village
    for key, value in edit.items():
        if key != "revision":
            assert reread.json()[key] == value
    assert reread.json()["revision"] != loaded["revision"]


def test_without_configured_origins_no_browser_is_invited(api: ApiFactory) -> None:
    harness = api()
    response = harness.client.get("/residents", headers={"Origin": "http://village.local"})
    assert "access-control-allow-origin" not in response.headers


def test_no_endpoint_claims_the_work_is_done(api: ApiFactory) -> None:
    """Acknowledgement, not effect: the API may say accepted, queued, recorded."""
    harness = api()
    request_id = _pending(harness)
    bodies = [
        harness.client.post("/residents/test-agent/routines/daily-summary/run").json(),
        harness.client.post("/jobs", json={"title": "Research X"}).json(),
        harness.client.post(f"/approvals/{request_id}", json={"decision": "deny"}).json(),
        harness.client.post("/residents", json=NEW_RESIDENT).json(),
    ]
    harness.settle()

    for body in bodies:
        assert body["status"] in {"accepted", "queued", "recorded"}
        assert not FORBIDDEN.search(body["message"]), body["message"]
