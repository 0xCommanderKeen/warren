"""Discord guild mirror: bounded context refreshed for permitted residents."""

import json
import os
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from conftest import ResidentWriter, valid_manifest
from steward.discord_mirror import GuildMirror
from steward.manifest import load_manifest
from steward.store import Store

NOW = datetime(2026, 9, 4, 12, tzinfo=UTC)


@dataclass
class Guilds:
    """Fake Discord read adapter."""

    calls: int = 0

    def guild_snapshot(self, token: str, guild: str):
        """Return one channel and one member without exposing the token."""
        assert (token, guild) == ("secret", "home")
        self.calls += 1
        return {
            "channels": [{"id": "42", "name": "household", "type": 0}],
            "members": [
                {"user": {"id": "7", "username": "Miha"}, "joined_at": "2026-09-01T10:00:00Z"}
            ],
        }

    def identity(self, token: str) -> str | None:
        """Confirm the configured fake bot is reachable."""
        return "@testy" if token == "secret" else None


def test_members_read_writes_bounded_guild_context_and_keeps_it_for_fifteen_minutes(
    write_resident: ResidentWriter, tmp_path: Path
) -> None:
    data = valid_manifest()
    memory = tmp_path / "memory"
    memory.mkdir()
    data["memory"] = {"kind": "directory", "path": str(memory), "journal": "journal"}
    data["routes"].append(
        {"id": "discord", "kind": "chat", "address": "discord:testy", "status": "active"}
    )
    data["app_grants"] = [
        {
            "id": "discord",
            "name": "Discord",
            "status": "granted",
            "scopes": ["members.read"],
        }
    ]
    person = load_manifest(write_resident(data))
    guilds = Guilds()
    with Store(tmp_path / "steward.db") as store:
        store.delegate_job(
            title="Bring tea",
            detail="",
            assignee=person.id,
            delegated_by="pip",
            route="inbox",
            origin="resident:pip",
            depth=1,
        )
        mirror = GuildMirror(
            [person], store, guilds, {"STEWARD_CHAT_TOKEN_DISCORD_TESTY": "secret"}, "home"
        )
        assert mirror.refresh(person, now=NOW)
        assert not mirror.refresh(person, now=NOW + timedelta(minutes=14))
    document = json.loads((memory / "discord" / "guild.json").read_text())
    assert document == {
        "generated_at": "2026-09-04T12:00:00Z",
        "guild": "home",
        "channels": [{"id": "42", "name": "household", "type": 0}],
        "members": [{"id": "7", "username": "Miha", "joined_at": "2026-09-01T10:00:00Z"}],
        "resident_bots": [{"resident": "test-agent", "bot": True, "reachable": True}],
        "task_delegated": [
            {
                "task_id": document["task_delegated"][0]["task_id"],
                "title": "Bring tea",
                "from": "pip",
                "to": "test-agent",
                "route": "inbox",
                "created_at": document["task_delegated"][0]["created_at"],
            }
        ],
    }
    assert guilds.calls == 1
    assert "secret" not in (memory / "discord" / "guild.json").read_text()


def test_session_refresh_forces_a_fresh_mirror(
    write_resident: ResidentWriter, tmp_path: Path
) -> None:
    data = valid_manifest()
    memory = tmp_path / "memory"
    memory.mkdir()
    data["memory"] = {"kind": "directory", "path": str(memory), "journal": "journal"}
    data["routes"].append(
        {"id": "discord", "kind": "chat", "address": "discord:testy", "status": "active"}
    )
    data["app_grants"] = [
        {"id": "discord", "name": "Discord", "status": "granted", "scopes": ["members.read"]}
    ]
    person = load_manifest(write_resident(data))
    guilds = Guilds()
    with Store(tmp_path / "steward.db") as store:
        mirror = GuildMirror(
            [person], store, guilds, {"STEWARD_CHAT_TOKEN_DISCORD_TESTY": "secret"}, "home"
        )
        mirror.refresh(person, now=NOW)
        mirror.refresh(person, now=NOW + timedelta(minutes=1), force=True)
    assert guilds.calls == 2


def test_session_mirror_stays_under_the_admitted_directory_descriptor(
    write_resident: ResidentWriter, tmp_path: Path
) -> None:
    data = valid_manifest()
    memory = tmp_path / "memory"
    memory.mkdir()
    data["memory"] = {"kind": "directory", "path": str(memory), "journal": "journal"}
    data["routes"].append(
        {"id": "discord", "kind": "chat", "address": "discord:testy", "status": "active"}
    )
    data["app_grants"] = [
        {"id": "discord", "name": "Discord", "status": "granted", "scopes": ["members.read"]}
    ]
    person = load_manifest(write_resident(data))
    admitted = os.open(memory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    original = tmp_path / "original-memory"
    outside = tmp_path / "outside"
    outside.mkdir()
    memory.rename(original)
    memory.symlink_to(outside, target_is_directory=True)
    try:
        with Store(tmp_path / "steward.db") as store:
            mirror = GuildMirror(
                [person],
                store,
                Guilds(),
                {"STEWARD_CHAT_TOKEN_DISCORD_TESTY": "secret"},
                "home",
            )
            assert mirror.refresh(person, now=NOW, force=True, memory_fd=admitted)
    finally:
        os.close(admitted)
    assert (original / "discord" / "guild.json").is_file()
    assert not (outside / "discord").exists()
