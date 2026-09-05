"""API behavior: config."""

from pathlib import Path

import pytest

from steward import authoring as au
from steward.api import (
    ApiConfig,
)


def test_config_reads_the_environment(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("STEWARD_TOKEN", "from-env")
    monkeypatch.setenv("STEWARD_CORS_ORIGINS", "http://a.local, http://b.local ,")
    monkeypatch.setenv("STEWARD_RESIDENTS", str(tmp_path / "elsewhere"))

    config = ApiConfig.from_env()
    assert config.token == "from-env"
    assert config.cors_origins == ("http://a.local", "http://b.local")
    assert config.residents_dir == tmp_path / "elsewhere"


def test_config_defaults_to_the_residents_tree(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("STEWARD_RESIDENTS", raising=False)
    monkeypatch.delenv("STEWARD_CORS_ORIGINS", raising=False)
    config = ApiConfig.from_env({})
    assert config.residents_dir == Path("residents")
    assert config.cors_origins == ()
    assert config.push is None


def test_config_reads_the_push_target_from_the_environment() -> None:
    """``STEWARD_PUSH_BRANCH`` turns the push on; the remote is ``origin`` unless named."""
    assert ApiConfig.from_env({"STEWARD_PUSH_BRANCH": "burrow/residents"}).push == au.PushTarget(
        remote="origin", branch="burrow/residents"
    )
    assert ApiConfig.from_env(
        {"STEWARD_PUSH_BRANCH": " burrow/residents ", "STEWARD_PUSH_REMOTE": " github "}
    ).push == au.PushTarget(remote="github", branch="burrow/residents")
    # A remote alone names nowhere to push to, and a blank branch is no branch.
    assert ApiConfig.from_env({"STEWARD_PUSH_REMOTE": "origin"}).push is None
    assert ApiConfig.from_env({"STEWARD_PUSH_BRANCH": "   "}).push is None
