"""Chat doubles and bridge fixtures shared by single and shared bot tests."""

import copy
from collections.abc import Callable, Iterator, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from conftest import ResidentWriter, valid_manifest
from steward import chat as ch
from steward import events as ev
from steward import manifest as m
from steward.manifest import (
    CHAT_ROUTE_KIND,
    ResidentManifest,
    load_manifest,
)
from steward.runners import Outcome, Runner, RunRequest, RunResult
from steward.scheduler import WakeHooks
from steward.sessions import RunGuard
from steward.store import Store

#: A bot token shaped exactly like BotFather's, and belonging to nobody.
FAKE_BOT_TOKEN = "123456789:AAHfake-token-for-tests-only-nothing-real"


FAKE_DISCORD_TOKEN = "M" + "a" * 23 + ".ABC123." + "b" * 27


#: The one Telegram user id this suite's fleet answers.
OPERATOR = "4242"


#: The conversation every message in this suite arrives in.
CONVERSATION = "777"


NOW = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)


# --------------------------------------------------------------------------------------
# the harness
# --------------------------------------------------------------------------------------


def chat_manifest(memory: Path, **overrides: object) -> dict[str, Any]:
    """Build a manifest with a live chat route and a memory directory that exists."""
    data = valid_manifest()
    memory.mkdir(parents=True, exist_ok=True)
    data["memory"] = {"kind": "directory", "path": str(memory), "journal": "journal"}
    # A resident that is talked to rather than scheduled — the shape warren#108 was filed
    # for. It grants no skills and declares no routine, so nothing here is about either.
    data["skills"] = []
    data["routines"] = []
    data["routes"] = [
        *data["routes"],
        {"id": "chat", "kind": CHAT_ROUTE_KIND, "address": "telegram:testy", "status": "active"},
    ]
    data.update(copy.deepcopy(overrides))
    return data


class ScriptedRunner(Runner):
    """A runner that returns a prepared result and remembers what it was asked to run."""

    def __init__(self, result: RunResult | None = None) -> None:
        """Hold the result every run of this runner will return."""
        super().__init__()
        self.result = result or RunResult(outcome=Outcome.OK, output="I am alive.", exit_status=0)
        self.requests: list[RunRequest] = []

    def run(self, request: RunRequest) -> RunResult:
        """Record the request and return the prepared result."""
        self.requests.append(request)
        return self.result


class FakeTransport:
    """A chat transport that hands over canned messages and keeps every reply."""

    name = "telegram"

    def __init__(self, batches: Sequence[list[ch.Message] | None] = ()) -> None:
        """Queue what each successive poll returns. ``None`` is an unreachable bot."""
        self.batches: list[list[ch.Message] | None] = list(batches)
        self.polls: list[tuple[str, int]] = []
        self.sent: list[tuple[str, str, str]] = []

    def poll(self, token: str, offset: int) -> list[ch.Message] | None:
        """Return the next queued batch, remembering what was asked for."""
        self.polls.append((token, offset))
        return self.batches.pop(0) if self.batches else []

    def send(self, token: str, conversation: str, text: str) -> bool:
        """Record one reply as delivered."""
        self.sent.append((token, conversation, text))
        return True

    def identity(self, token: str) -> str | None:
        """Return a harmless fake identity for protocol compatibility."""
        del token
        return "@fake"


def message(  # noqa: PLR0913 — one keyword per fact a test varies about a message
    text: str = "are you alive?",
    *,
    update_id: int = 1,
    sender: str = OPERATOR,
    conversation: str = CONVERSATION,
    at: datetime = NOW,
    private: bool = True,
    bot: bool = False,
    allowed_public: bool = False,
    reply_to: str | None = None,
) -> ch.Message:
    """Build one inbound message, already parsed."""
    return ch.Message(
        update_id=update_id,
        conversation=conversation,
        sender=sender,
        text=text,
        at=at,
        access=(
            ch.ConversationAccess.ALLOWLISTED_PUBLIC
            if allowed_public
            else ch.ConversationAccess.PRIVATE
            if private
            else ch.ConversationAccess.PUBLIC
        ),
        bot=bot,
        reply_to=reply_to,
    )


@pytest.fixture
def store(tmp_path: Path) -> Iterator[Store]:
    """Open a real file-backed store, so a second process can see the same claims."""
    with Store(tmp_path / "steward.db") as opened:
        yield opened


@pytest.fixture
def sink() -> ev.NullEmitter:
    return ev.NullEmitter()


@pytest.fixture
def chat_door(write_resident: ResidentWriter, tmp_path: Path) -> ch.ChatRoute:
    """One doorway, for the tests that are about the doorway rather than about a bridge."""
    resident = load_manifest(write_resident(chat_manifest(tmp_path / "memory")))
    [route] = ch.chat_routes([resident])
    return route


type BridgeMaker = Callable[..., ch.ChatBridge]


@pytest.fixture
def make_bridge(
    write_resident: ResidentWriter, store: Store, sink: ev.NullEmitter, tmp_path: Path
) -> BridgeMaker:
    """Build a bridge over one resident with a live chat route and a fake bot."""

    def _make(  # noqa: PLR0913 — one keyword per thing a chat test varies
        manifest: dict[str, Any] | None = None,
        *,
        transport: ch.ChatTransport | None = None,
        runner: Runner | None = None,
        operators: frozenset[str] = frozenset({OPERATOR}),
        operators_by_transport: dict[str, frozenset[str]] | None = None,
        tokens: dict[str, str] | None = None,
        transports: dict[str, ch.ChatTransport] | None = None,
        guard: RunGuard | None = None,
        hooks: WakeHooks | None = None,
    ) -> ch.ChatBridge:
        declared = manifest if manifest is not None else chat_manifest(tmp_path / "memory")
        resident = load_manifest(write_resident(declared))
        return ch.ChatBridge(
            routes=ch.chat_routes([resident]),
            store=store,
            tokens=tokens if tokens is not None else {"STEWARD_CHAT_TOKEN_TESTY": FAKE_BOT_TOKEN},
            operators=operators,
            operators_by_transport=operators_by_transport or {},
            transports=transports or {},
            transport=transport if transport is not None else FakeTransport(),
            emitter=sink,
            workdir=tmp_path / "fallback",
            runner_factory=lambda _spec, _placement: runner or ScriptedRunner(),
            guard=guard,
            hooks=hooks,
            clock=lambda: NOW,
            state_path=tmp_path / "state" / "scheduler.json",
        )

    return _make


# --------------------------------------------------------------------------------------
# routines[].deliver: chat (warren#385)
# --------------------------------------------------------------------------------------


DIGEST = m.Routine.model_validate(
    {
        "id": "digest",
        "schedule": "0 8 * * *",
        "prompt": "Write the digest, or reply NOTHING.",
        "timeout_s": 600,
        "deliver": "chat",
        "quiet_word": "NOTHING",
    }
)


def manifest_for(manifest: dict | None = None) -> ResidentManifest:
    """Build a validated manifest straight from the shared fixture data."""
    return ResidentManifest.model_validate(manifest or valid_manifest())
