"""Write scoped, read-only Discord guild context into resident memory."""

import json
import os
import stat
from collections.abc import Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Protocol

from steward.chat import Address, DiscordTransport, tokens_from_env
from steward.deploy import memory_host_dir
from steward.manifest import Resident
from steward.store import Store

MIRROR_INTERVAL = timedelta(minutes=15)
MIRROR_SCOPE = "members.read"
MAX_MIRROR_ITEMS = 1000


class GuildReader(Protocol):
    """Discord reads required to construct one guild mirror."""

    def guild_snapshot(self, token: str, guild: str) -> Mapping[str, object] | None:
        """Return raw channels and guild members from Discord."""
        ...


@dataclass
class GuildMirror:
    """Refresh token-free Discord context for residents explicitly granted member reads."""

    residents: Sequence[Resident]
    store: Store
    transport: GuildReader | None
    tokens: Mapping[str, str] = field(default_factory=dict, repr=False)
    guild: str = ""
    refreshed_at: dict[str, datetime] = field(default_factory=dict, init=False)

    @classmethod
    def from_env(
        cls,
        residents: Sequence[Resident],
        store: Store,
        env: Mapping[str, str] | None = None,
    ) -> GuildMirror:
        """Build the daemon mirror from operator-owned Discord configuration."""
        source = os.environ if env is None else env
        return cls(
            residents,
            store,
            DiscordTransport.from_env(source),
            tokens_from_env(source),
            (source.get("STEWARD_CHAT_DISCORD_GUILD") or "").strip(),
        )

    def refresh(
        self,
        resident: Resident,
        *,
        now: datetime,
        force: bool = False,
        memory_fd: int | None = None,
    ) -> bool:
        """Write one resident's mirror when due, or always immediately before its session."""
        if not self._permitted(resident) or self.transport is None or not self.guild:
            return False
        previous = self.refreshed_at.get(resident.id)
        if not force and previous is not None and now - previous < MIRROR_INTERVAL:
            return False
        token = self._token_for(resident)
        if not token:
            return False
        snapshot = self.transport.guild_snapshot(token, self.guild)
        if snapshot is None:
            return False
        document = self._document(snapshot, now)
        owned_fd: int | None = None
        try:
            if memory_fd is None:
                owned_fd = os.open(
                    self._memory_dir(resident), os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
                )
                memory_fd = owned_fd
            self._write_pinned(memory_fd, document)
        except OSError:
            return False
        finally:
            if owned_fd is not None:
                os.close(owned_fd)
        self.refreshed_at[resident.id] = now
        return True

    def refresh_all(self, *, now: datetime) -> None:
        """Apply the fifteen-minute cadence to every permitted resident."""
        for resident in self.residents:
            self.refresh(resident, now=now)

    def update_residents(self, residents: Sequence[Resident]) -> None:
        """Adopt a control-plane manifest refresh without discarding cadence state."""
        self.residents = tuple(residents)

    def _document(self, snapshot: Mapping[str, object], now: datetime) -> dict[str, object]:
        channels = [
            {"id": str(item["id"]), "name": str(item["name"]), "type": item["type"]}
            for item in _objects(snapshot.get("channels"))[:MAX_MIRROR_ITEMS]
            if "id" in item and "name" in item and "type" in item
        ]
        members = []
        for item in _objects(snapshot.get("members"))[:MAX_MIRROR_ITEMS]:
            user = item.get("user")
            if isinstance(user, Mapping) and user.get("id") and user.get("username"):
                members.append(
                    {
                        "id": str(user["id"]),
                        "username": str(user["username"]),
                        "joined_at": str(item.get("joined_at") or ""),
                    }
                )
        cutoff = now - timedelta(hours=24)
        delegated = []
        for job in self.store.jobs():
            if not job.delegated or _moment(job.created_at) < cutoff:
                continue
            delegated.append(
                {
                    "task_id": job.task_id,
                    "title": job.title,
                    "from": job.delegated_by,
                    "to": job.assignee,
                    "route": job.route,
                    "created_at": job.created_at,
                }
            )
        bots = []
        for resident in self.residents:
            token = self._token_for(resident)
            reachable = self._reachable(token)
            bots.append({"resident": resident.id, "bot": token is not None, "reachable": reachable})
        return {
            "generated_at": now.astimezone(UTC)
            .isoformat(timespec="seconds")
            .replace("+00:00", "Z"),
            "guild": self.guild,
            "channels": channels,
            "members": members,
            "resident_bots": bots,
            "task_delegated": delegated[-MAX_MIRROR_ITEMS:],
        }

    @staticmethod
    def _permitted(resident: Resident) -> bool:
        if resident.manifest.memory.kind != "directory":
            return False
        return any(
            grant.id == "discord" and grant.status == "granted" and MIRROR_SCOPE in grant.scopes
            for grant in resident.manifest.app_grants
        )

    def _token_for(self, resident: Resident) -> str | None:
        for route in resident.manifest.routes:
            address = Address.parse(route.address)
            if route.accepts_chat and address is not None and address.transport == "discord":
                return self.tokens.get(address.token_env)
        return None

    def _reachable(self, token: str | None) -> bool:
        identity = getattr(self.transport, "identity", None)
        if not token or not callable(identity):
            return False
        try:
            return bool(identity(token))
        except Exception:  # noqa: BLE001 — the mirror reports unreachable instead
            return False

    @staticmethod
    def _memory_dir(resident: Resident) -> Path:
        if resident.manifest.runner.container_placed:
            candidate = memory_host_dir(resident.manifest)
        else:
            candidate = Path(resident.manifest.memory.path).expanduser()
        return candidate

    @staticmethod
    def _write_pinned(memory_fd: int, document: Mapping[str, object]) -> None:
        """Atomically write beneath an already-open directory without following symlinks."""
        if not stat.S_ISDIR(os.fstat(memory_fd).st_mode):
            raise NotADirectoryError("resident memory descriptor is not a directory")
        with suppress(FileExistsError):
            os.mkdir("discord", mode=0o700, dir_fd=memory_fd)
        discord_fd = os.open(
            "discord", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=memory_fd
        )
        temporary = f".guild.json.{os.getpid()}.tmp"
        file_fd = -1
        try:
            file_fd = os.open(
                temporary,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                0o600,
                dir_fd=discord_fd,
            )
            content = (json.dumps(document, indent=2, sort_keys=True) + "\n").encode()
            with os.fdopen(file_fd, "wb", closefd=True) as stream:
                file_fd = -1
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(
                temporary,
                "guild.json",
                src_dir_fd=discord_fd,
                dst_dir_fd=discord_fd,
            )
        finally:
            if file_fd >= 0:
                os.close(file_fd)
            with suppress(FileNotFoundError):
                os.unlink(temporary, dir_fd=discord_fd)
            os.close(discord_fd)


def _objects(value: object) -> list[Mapping[str, object]]:
    return [item for item in value if isinstance(item, Mapping)] if isinstance(value, list) else []


def _moment(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value)
        return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)
    except ValueError:
        return datetime.min.replace(tzinfo=UTC)
