"""CLI behavior: serve."""

from pathlib import Path

import pytest
from click.testing import CliRunner

from steward.cli import main
from support.cli import (
    runner as runner,  # noqa: PLC0414 — pytest fixture discovery
)

# --------------------------------------------------------------------------------------
# serve
# --------------------------------------------------------------------------------------


def test_serve_refuses_to_start_without_a_token(
    runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("STEWARD_TOKEN", raising=False)
    result = runner.invoke(
        main, ["serve", "--residents", str(tmp_path), "--db", str(tmp_path / "s.db")]
    )
    assert result.exit_code == 1
    assert "STEWARD_TOKEN" in result.output
    assert "--allow-open" in result.output
    assert not (tmp_path / "s.db").exists()


def test_serve_binds_loopback_by_default(
    runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("STEWARD_TOKEN", "a-shared-secret")
    monkeypatch.setenv("STEWARD_CORS_ORIGINS", "http://village.local")
    served: dict[str, object] = {}
    monkeypatch.setattr(
        "steward.cli.run_server",
        lambda app, *, host, port: served.update(app=app, host=host, port=port),
    )
    result = runner.invoke(
        main, ["serve", "--residents", str(tmp_path), "--db", str(tmp_path / "s.db")]
    )
    assert result.exit_code == 0, result.output
    assert served["host"] == "127.0.0.1"
    assert served["port"] == 8801
    assert "http://127.0.0.1:8801" in result.output
    assert "http://village.local" in result.output


def test_serve_says_out_loud_when_it_has_no_token(
    runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("STEWARD_TOKEN", raising=False)
    monkeypatch.delenv("STEWARD_CORS_ORIGINS", raising=False)
    monkeypatch.setattr("steward.cli.run_server", lambda *_a, **_k: None)
    result = runner.invoke(
        main,
        [
            "serve",
            "--allow-open",
            "--port",
            "9000",
            "--residents",
            str(tmp_path),
            "--db",
            str(tmp_path / "s.db"),
        ],
    )
    assert result.exit_code == 0, result.output
    assert "without a token" in result.output
    assert "cors: none" in result.output


def test_allow_open_is_refused_on_a_non_loopback_bind(
    runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """--allow-open serves every write path with no token; a public bind is refused (#81)."""
    monkeypatch.delenv("STEWARD_TOKEN", raising=False)
    served: dict[str, object] = {}
    monkeypatch.setattr("steward.cli.run_server", lambda *_a, **_k: served.setdefault("ran", True))
    result = runner.invoke(
        main,
        ["serve", "--allow-open", "--host", "0.0.0.0", "--residents", str(tmp_path)],  # noqa: S104
    )
    assert result.exit_code == 1
    assert "loopback" in result.output
    assert "ran" not in served, "the server was never started"


def test_allow_open_is_permitted_on_loopback(
    runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("STEWARD_TOKEN", raising=False)
    monkeypatch.setattr("steward.cli.run_server", lambda *_a, **_k: None)
    for host in ("127.0.0.1", "::1", "localhost"):
        result = runner.invoke(
            main, ["serve", "--allow-open", "--host", host, "--residents", str(tmp_path)]
        )
        assert result.exit_code == 0, result.output
