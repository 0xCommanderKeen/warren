"""Persistence and execution share one state directory, including legacy imports."""

from pathlib import Path

import pytest

from steward import scheduler
from steward.state_paths import default_state_path
from steward.store import Store, default_db_path


@pytest.mark.parametrize("configured", [None, "", "  \t"])
def test_unconfigured_paths_remain_relative_to_working_directory(monkeypatch, configured):
    monkeypatch.delenv("STEWARD_STATE", raising=False)
    if configured is not None:
        monkeypatch.setenv("STEWARD_STATE", configured)
    assert default_state_path() == Path(".steward/state/scheduler.json")
    assert scheduler.default_state_path() == Path(".steward/state/scheduler.json")
    assert default_db_path() == Path(".steward/state/steward.db")
    assert Path(".steward/state/scheduler.json") == scheduler.DEFAULT_STATE_PATH
    assert scheduler.STATE_ENV == "STEWARD_STATE"


def test_explicit_environment_overrides_process_configuration(monkeypatch):
    monkeypatch.setenv("STEWARD_STATE", "/process/scheduler.json")
    assert scheduler.default_state_path({}) == Path(".steward/state/scheduler.json")
    assert scheduler.default_state_path({"STEWARD_STATE": "  relative/tick.json  "}) == Path(
        "relative/tick.json"
    )


@pytest.mark.parametrize("configured", ["  {home}/state/tick.json  ", "~/state/tick.json"])
def test_configured_directory_is_shared_by_scheduler_and_persistent_store(
    monkeypatch, tmp_path, configured
):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("STEWARD_STATE", configured.format(home=tmp_path))
    assert scheduler.default_state_path() == tmp_path / "state" / "tick.json"
    assert default_db_path() == tmp_path / "state" / "steward.db"
    with Store.open_default() as store:
        assert store.path == tmp_path / "state" / "steward.db"
    assert (tmp_path / "state" / "steward.db").is_file()
