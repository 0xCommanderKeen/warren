"""Chat config behavior through the public chat interface."""

import json
from pathlib import Path

from click.testing import CliRunner

from conftest import ResidentWriter
from steward import chat as ch
from steward import secrets as sec
from steward.cli import main
from steward.manifest import load_manifest
from support.chat import (
    FAKE_BOT_TOKEN,
    FAKE_DISCORD_TOKEN,
    OPERATOR,
    FakeTransport,
    chat_manifest,
)
from support.chat_http import DiscordApi
from support.chat_http import discord_api as discord_api  # noqa: PLC0414 — pytest fixture discovery
from support.cli import runner as runner  # noqa: PLC0414 — pytest fixture discovery

# --------------------------------------------------------------------------------------
# where a bot's token comes from
# --------------------------------------------------------------------------------------


def test_an_address_names_a_transport_and_a_bot():
    address = ch.Address.parse("telegram:pip")
    assert address is not None
    assert (address.transport, address.reference) == ("telegram", "pip")
    assert str(address) == "telegram:pip"


def test_an_address_with_no_transport_names_no_bot():
    assert ch.Address.parse("pip") is None


def test_the_token_variable_is_the_reference_upper_cased():
    assert ch.token_env_name("pip") == "STEWARD_CHAT_TOKEN_PIP"


def test_a_non_telegram_token_variable_includes_its_transport():
    address = ch.Address.parse("discord:pip")
    assert address is not None
    assert address.token_env == "STEWARD_CHAT_TOKEN_DISCORD_PIP"


def test_a_hyphenated_reference_folds_to_a_legal_variable_name():
    assert ch.token_env_name("polica-librarian") == "STEWARD_CHAT_TOKEN_POLICA_LIBRARIAN"


def test_operators_are_read_from_one_comma_separated_list():
    env = {ch.OPERATORS_ENV: " 4242, 99 ,"}
    assert ch.operators_from_env(env) == frozenset({"4242", "99"})


def test_operator_ids_are_scoped_to_their_transport_with_telegram_compatibility():
    env = {ch.OPERATORS_ENV: "4242, telegram:99, discord:31337"}

    assert ch.operators_from_env(env, transport="telegram") == frozenset({"4242", "99"})
    assert ch.operators_from_env(env, transport="discord") == frozenset({"31337"})


def test_no_operator_list_means_nobody():
    assert ch.operators_from_env({}) == frozenset()


def test_a_blank_token_is_not_a_token():
    assert ch.tokens_from_env({"STEWARD_CHAT_TOKEN_PIP": "   "}) == {}


def test_token_variable_names_include_empty_configured_slots():
    env = {"STEWARD_CHAT_TOKEN_PIP": "", "STEWARD_CHAT_TOKEN_HOB": FAKE_BOT_TOKEN}

    assert ch.token_env_names(env) == ["STEWARD_CHAT_TOKEN_HOB", "STEWARD_CHAT_TOKEN_PIP"]


# --------------------------------------------------------------------------------------
# who is reachable
# --------------------------------------------------------------------------------------


def test_only_an_active_chat_route_with_a_readable_address_is_reachable(
    write_resident: ResidentWriter, tmp_path: Path
):
    declared = chat_manifest(tmp_path / "memory")
    declared["routes"][-1]["status"] = "pending"
    assert ch.chat_routes([load_manifest(write_resident(declared))]) == []


def test_a_retired_resident_has_closed_every_door_it_had(
    write_resident: ResidentWriter, tmp_path: Path
):
    declared = chat_manifest(tmp_path / "memory", retired=True)
    assert ch.chat_routes([load_manifest(write_resident(declared))]) == []


def test_the_report_names_the_variable_the_token_belongs_in(
    write_resident: ResidentWriter, tmp_path: Path
):
    resident = load_manifest(write_resident(chat_manifest(tmp_path / "memory")))
    [report] = ch.describe_chat([resident], {})
    assert report.token_env == "STEWARD_CHAT_TOKEN_TESTY"
    assert not report.token_set
    assert not report.reachable
    assert report.note is not None
    assert "STEWARD_CHAT_TOKEN_TESTY" in report.note


def test_the_report_never_carries_the_token_itself(write_resident: ResidentWriter, tmp_path: Path):
    resident = load_manifest(write_resident(chat_manifest(tmp_path / "memory")))
    [report] = ch.describe_chat(
        [resident], {"STEWARD_CHAT_TOKEN_TESTY": FAKE_BOT_TOKEN, ch.OPERATORS_ENV: OPERATOR}
    )
    assert report.token_set
    assert report.reachable
    assert FAKE_BOT_TOKEN not in json.dumps(report.to_dict())


def test_a_pending_route_is_still_reported_so_it_can_be_wired_up(
    write_resident: ResidentWriter, tmp_path: Path
):
    declared = chat_manifest(tmp_path / "memory")
    declared["routes"][-1]["status"] = "pending"
    resident = load_manifest(write_resident(declared))
    [report] = ch.describe_chat([resident], {"STEWARD_CHAT_TOKEN_TESTY": FAKE_BOT_TOKEN})
    assert report.status == "pending"
    assert not report.reachable


def test_chat_list_discovers_a_discord_bot_handle(
    discord_api: DiscordApi,
    runner: CliRunner,
    write_resident: ResidentWriter,
    tmp_path: Path,
    monkeypatch,
):
    declared = chat_manifest(tmp_path / "memory")
    declared["routes"][-1]["address"] = "discord:testy"
    tree = write_resident(declared).parent.parent
    discord_api.queue("GET", "/users/@me", (200, {"id": "42", "username": "Pip"}))
    monkeypatch.setenv("STEWARD_CHAT_TOKEN_DISCORD_TESTY", FAKE_DISCORD_TOKEN)
    monkeypatch.setenv(ch.DISCORD_API_URL_ENV, discord_api.url)
    monkeypatch.setenv(ch.OPERATORS_ENV, "discord:31337")

    result = runner.invoke(main, ["chat", "list", "--residents", str(tree)])

    assert result.exit_code == 0
    assert "test-agent/chat: discord:testy — reachable, bot @Pip" in result.output


def test_chat_list_names_the_variable_and_never_prints_the_token(
    runner: CliRunner, write_resident: ResidentWriter, tmp_path: Path, monkeypatch
):
    monkeypatch.setenv("STEWARD_CHAT_TOKEN_TESTY", FAKE_BOT_TOKEN)
    monkeypatch.setenv(ch.OPERATORS_ENV, OPERATOR)
    tree = write_resident(chat_manifest(tmp_path / "memory")).parent.parent

    result = runner.invoke(main, ["chat", "list", "--residents", str(tree)])

    assert result.exit_code == 0
    assert "test-agent/chat: telegram:testy — reachable" in result.output
    assert "STEWARD_CHAT_TOKEN_TESTY (set)" in result.output
    assert FAKE_BOT_TOKEN not in result.output


def test_chat_list_says_what_is_missing_before_a_bot_is_wired_up(
    runner: CliRunner, write_resident: ResidentWriter, tmp_path: Path
):
    tree = write_resident(chat_manifest(tmp_path / "memory")).parent.parent

    result = runner.invoke(main, ["chat", "list", "--residents", str(tree)])

    assert "not reachable yet" in result.output
    assert "set STEWARD_CHAT_TOKEN_TESTY" in result.output


def test_chat_list_json_is_the_machine_view(
    runner: CliRunner, write_resident: ResidentWriter, tmp_path: Path
):
    tree = write_resident(chat_manifest(tmp_path / "memory")).parent.parent

    result = runner.invoke(main, ["chat", "list", "--residents", str(tree), "--format", "json"])

    (row,) = json.loads(result.output)
    assert row["token_env"] == "STEWARD_CHAT_TOKEN_TESTY"
    assert row["reachable"] is False


def test_chat_list_over_a_fleet_that_declares_no_chat_says_so(
    runner: CliRunner, write_resident: ResidentWriter
):
    tree = write_resident().parent.parent
    result = runner.invoke(main, ["chat", "list", "--residents", str(tree)])
    assert result.exit_code == 0
    assert "declares a chat route" in result.output


def test_chat_list_names_configured_token_slots_without_declared_routes(
    runner: CliRunner, write_resident: ResidentWriter, monkeypatch
):
    tree = write_resident().parent.parent
    monkeypatch.setenv("STEWARD_CHAT_TOKEN_PIP", "")
    monkeypatch.setenv("STEWARD_CHAT_TOKEN_HOB", FAKE_BOT_TOKEN)

    result = runner.invoke(main, ["chat", "list", "--residents", str(tree)])

    assert result.exit_code == 0
    assert "STEWARD_CHAT_TOKEN_PIP (unset)" in result.output
    assert "STEWARD_CHAT_TOKEN_HOB (set)" in result.output
    assert FAKE_BOT_TOKEN not in result.output


def test_an_unreadable_poll_timeout_falls_back_to_the_default():
    assert ch.poll_timeout_from_env({ch.POLL_TIMEOUT_ENV: "soon"}) == ch.DEFAULT_POLL_TIMEOUT_S
    assert ch.poll_timeout_from_env({ch.POLL_TIMEOUT_ENV: "-3"}) == ch.DEFAULT_POLL_TIMEOUT_S


def test_an_address_steward_cannot_read_is_reported_rather_than_polled(
    write_resident: ResidentWriter, tmp_path: Path
):
    declared = chat_manifest(tmp_path / "memory")
    declared["routes"][-1]["address"] = "just-a-name"
    resident = load_manifest(write_resident(declared))

    [report] = ch.describe_chat([resident], {})

    assert report.token_env is None
    assert report.note is not None
    assert "<transport>:<reference>" in report.note
    assert ch.chat_routes([resident]) == []


def test_a_discord_route_names_its_missing_token(write_resident: ResidentWriter, tmp_path: Path):
    declared = chat_manifest(tmp_path / "memory")
    declared["routes"][-1]["address"] = "discord:testy"
    resident = load_manifest(write_resident(declared))

    [report] = ch.describe_chat([resident], {})

    assert not report.reachable
    assert report.note == (
        "no token — set STEWARD_CHAT_TOKEN_DISCORD_TESTY to the token issued for discord:testy"
    )


def test_chat_list_reports_unknown_configured_discord_rooms(
    write_resident: ResidentWriter, tmp_path: Path
):
    declared = chat_manifest(tmp_path / "memory")
    declared["routes"][-1].update(address="discord:testy", posts_to=["household", "missing"])
    resident = load_manifest(write_resident(declared))

    class DiscordRooms(FakeTransport):
        name = "discord"

        def channels(self, token: str, guild: str) -> dict[str, str]:
            assert (token, guild) == (FAKE_BOT_TOKEN, "home")
            return {"household": "42"}

    [report] = ch.describe_chat(
        [resident],
        {
            "STEWARD_CHAT_TOKEN_DISCORD_TESTY": FAKE_BOT_TOKEN,
            "STEWARD_CHAT_DISCORD_GUILD": "home",
            ch.OPERATORS_ENV: "discord:31337",
        },
        transports={"discord": DiscordRooms()},
    )

    assert report.reachable, "an unknown room breaks posts_to and nothing else"
    assert report.note == "unknown Discord channel name(s): missing"


def test_chat_list_reports_unknown_configured_discord_listen_channels(
    write_resident: ResidentWriter, tmp_path: Path
):
    declared = chat_manifest(tmp_path / "memory")
    declared["routes"][-1].update(address="discord:testy", listens_in=["missing"])
    resident = load_manifest(write_resident(declared))

    class DiscordRooms(FakeTransport):
        name = "discord"

        def channels(self, token: str, guild: str) -> dict[str, str]:
            del token, guild
            return {"household": "42"}

    [report] = ch.describe_chat(
        [resident],
        {
            "STEWARD_CHAT_TOKEN_DISCORD_TESTY": FAKE_BOT_TOKEN,
            "STEWARD_CHAT_DISCORD_GUILD": "home",
            ch.OPERATORS_ENV: "discord:31337",
        },
        transports={"discord": DiscordRooms()},
    )

    assert report.reachable, "an unknown room breaks listens_in and nothing else"
    assert report.note == "unknown Discord channel name(s): missing"


def test_chat_list_says_a_resident_has_nowhere_to_keep_a_conversation(
    write_resident: ResidentWriter, tmp_path: Path
):
    declared = chat_manifest(tmp_path / "memory")
    declared["memory"] = {"kind": "file", "path": str(tmp_path / "memory.md")}
    resident = load_manifest(write_resident(declared))

    [report] = ch.describe_chat(
        [resident], {"STEWARD_CHAT_TOKEN_TESTY": FAKE_BOT_TOKEN, ch.OPERATORS_ENV: OPERATOR}
    )

    assert not report.reachable
    assert report.note is not None
    assert "nowhere to keep a conversation" in report.note


# --------------------------------------------------------------------------------------
# secrets: a token that arrives as a file, and a daemon that notices (warren#462)
# --------------------------------------------------------------------------------------


def test_a_token_can_arrive_as_a_file_instead_of_a_variable(tmp_path: Path):
    """The whole point: provisioning a bot writes a file, not a line in an .env."""
    directory = tmp_path / "secrets"
    sec.write_secret("STEWARD_CHAT_TOKEN_TESTY", FAKE_BOT_TOKEN, directory=directory)

    tokens = ch.tokens_from_env({sec.SECRETS_DIR_ENV: str(directory)})

    assert tokens == {"STEWARD_CHAT_TOKEN_TESTY": FAKE_BOT_TOKEN}


def test_a_file_beats_the_variable_of_the_same_name(tmp_path: Path):
    directory = tmp_path / "secrets"
    sec.write_secret("STEWARD_CHAT_TOKEN_TESTY", FAKE_BOT_TOKEN, directory=directory)

    tokens = ch.tokens_from_env(
        {sec.SECRETS_DIR_ENV: str(directory), "STEWARD_CHAT_TOKEN_TESTY": "stale"}
    )

    assert tokens["STEWARD_CHAT_TOKEN_TESTY"] == FAKE_BOT_TOKEN


def test_a_file_only_slot_is_listed_among_the_token_names(tmp_path: Path):
    directory = tmp_path / "secrets"
    sec.write_secret("STEWARD_CHAT_TOKEN_TESTY", FAKE_BOT_TOKEN, directory=directory)

    assert ch.token_env_names({sec.SECRETS_DIR_ENV: str(directory)}) == ["STEWARD_CHAT_TOKEN_TESTY"]


def test_chat_list_reports_a_route_whose_token_is_only_a_file(
    write_resident: ResidentWriter, tmp_path: Path
):
    directory = tmp_path / "secrets"
    sec.write_secret("STEWARD_CHAT_TOKEN_TESTY", FAKE_BOT_TOKEN, directory=directory)
    resident = load_manifest(write_resident(chat_manifest(tmp_path / "memory")))

    [report] = ch.describe_chat(
        [resident],
        {
            sec.SECRETS_DIR_ENV: str(directory),
            ch.OPERATORS_ENV: OPERATOR,
            ch.API_URL_ENV: "http://127.0.0.1:1",
        },
        transports={ch.TELEGRAM: FakeTransport()},
    )

    assert report.token_set
    assert report.reachable
