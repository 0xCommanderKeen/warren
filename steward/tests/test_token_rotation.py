"""Master-token rotation through the API and diagnostic boundary."""

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from click.testing import CliRunner
from fastapi.testclient import TestClient

from steward.api import ApiConfig, ApiError, create_app
from steward.cli import main
from steward.store import Store
from support.api import TOKEN, ApiFactory
from support.api import api as api  # noqa: PLC0414 — pytest fixture discovery


def test_previous_token_expires_without_restarting(tmp_path: Path) -> None:
    clock = [datetime(2026, 9, 5, tzinfo=UTC)]
    config = ApiConfig.from_env(
        {
            "STEWARD_TOKEN": "new-secret",
            "STEWARD_TOKEN_PREVIOUS": " old-secret ",
            "STEWARD_TOKEN_PREVIOUS_UNTIL": "2026-09-06T00:00:00Z",
        },
        residents_dir=tmp_path,
    )
    with Store(":memory:") as store:
        app = create_app(config, store=store, now=lambda: clock[0])
        with TestClient(app) as client:
            for token in ("new-secret", "old-secret"):
                assert (
                    client.get(
                        "/residents", headers={"Authorization": f"Bearer {token}"}
                    ).status_code
                    == 200
                )
            clock[0] = datetime(2026, 9, 6, tzinfo=UTC)
            assert (
                client.get("/residents", headers={"Authorization": "Bearer old-secret"}).status_code
                == 401
            )
            assert (
                client.get("/residents", headers={"Authorization": "Bearer new-secret"}).status_code
                == 200
            )


@pytest.mark.parametrize(
    "settings",
    [
        {"STEWARD_TOKEN_PREVIOUS": "old-secret"},
        {"STEWARD_TOKEN_PREVIOUS_UNTIL": "2026-09-06T00:00:00Z"},
        {"STEWARD_TOKEN_PREVIOUS": "old-secret", "STEWARD_TOKEN_PREVIOUS_UNTIL": "bad"},
        {"STEWARD_TOKEN_PREVIOUS": "old-secret", "STEWARD_TOKEN_PREVIOUS_UNTIL": "2026-09-06"},
        {
            "STEWARD_TOKEN_PREVIOUS": "new-secret",
            "STEWARD_TOKEN_PREVIOUS_UNTIL": "2026-09-06T00:00:00Z",
        },
    ],
)
def test_invalid_rotation_refuses_start(tmp_path: Path, settings: dict[str, str]) -> None:
    config = ApiConfig.from_env({"STEWARD_TOKEN": "new-secret", **settings}, residents_dir=tmp_path)
    with pytest.raises(ApiError):
        create_app(config)


def test_rotation_cannot_enable_open_mode(tmp_path: Path) -> None:
    with pytest.raises(ApiError, match="incompatible"):
        create_app(
            ApiConfig(
                residents_dir=tmp_path,
                allow_open=True,
                token_previous="old-secret",
                token_previous_until="2026-09-06T00:00:00Z",
            )
        )


@pytest.mark.parametrize("token", ["new-secret", "old-secret"])
def test_rotating_tokens_share_body_guards_and_reject_duplicate_headers(
    tmp_path: Path, token: str
) -> None:
    config = ApiConfig(
        residents_dir=tmp_path,
        token="new-secret",
        token_previous="old-secret",
        token_previous_until="2026-09-06T00:00:00Z",
    )
    with Store(":memory:") as store:
        app = create_app(config, store=store, now=lambda: datetime(2026, 9, 5, tzinfo=UTC))
        with TestClient(app) as client:
            headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
            assert (
                client.post(
                    "/approvals/missing", headers=headers, content=b" " * 131073
                ).status_code
                == 413
            )
            assert (
                client.post(
                    "/approvals/missing", headers=headers, content="[" * 100 + "0" + "]" * 100
                ).status_code
                == 422
            )
            assert (
                client.get(
                    "/residents", headers=[("Authorization", f"Bearer {token}")] * 2
                ).status_code
                == 401
            )


@pytest.mark.parametrize(
    ("until", "expected"),
    [(None, "clean"), ("2000-01-01T00:00:00Z", "expired"), ("2099-01-01T00:00:00Z", "active")],
)
def test_doctor_reports_rotation_without_secrets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, until: str | None, expected: str
) -> None:
    monkeypatch.setenv("STEWARD_TOKEN", "new-secret")
    monkeypatch.delenv("STEWARD_TOKEN_PREVIOUS", raising=False)
    monkeypatch.delenv("STEWARD_TOKEN_PREVIOUS_UNTIL", raising=False)
    if until:
        monkeypatch.setenv("STEWARD_TOKEN_PREVIOUS", "old-secret")
        monkeypatch.setenv("STEWARD_TOKEN_PREVIOUS_UNTIL", until)
    result = CliRunner().invoke(main, ["doctor", str(tmp_path), "--db", str(tmp_path / "store.db")])
    assert f"master token: {expected}" in result.output
    assert "new-secret" not in result.output
    assert "old-secret" not in result.output


def test_rotation_audit_is_bounded_redacted_and_attached_to_writes(
    api: ApiFactory, caplog: pytest.LogCaptureFixture
) -> None:
    clock = [datetime(2026, 9, 5, tzinfo=UTC)]
    harness = api(
        token_previous="old-secret",
        token_previous_until="2026-09-06T00:00:00Z",
        now=lambda: clock[0],
    )
    with caplog.at_level("INFO", logger="steward.master_auth"):
        for token in (TOKEN, "old-secret"):
            headers = {"Authorization": f"Bearer {token}"}
            for _ in range(10):
                assert harness.client.get("/residents", headers=headers).status_code == 200
            response = harness.client.post("/jobs", json={"title": "Review notes"}, headers=headers)
            assert response.status_code == 202
        records = [r for r in caplog.records if r.name == "steward.master_auth"]
        assert len(records) == 2
        assert {r.__dict__["master_token_slot"] for r in records} == {"current", "previous"}
        assert TOKEN not in caplog.text
        assert "old-secret" not in caplog.text
        history = harness.client.get("/requests").json()["requests"]
        assert {r["detail"]["master_token_slot"] for r in history} == {"current", "previous"}
        clock[0] += timedelta(minutes=1)
        harness.client.get("/residents")
        assert len([r for r in caplog.records if r.name == "steward.master_auth"]) == 3
