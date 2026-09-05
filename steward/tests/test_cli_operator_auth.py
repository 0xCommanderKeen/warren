"""CLI behavior: operator auth."""

import json
from pathlib import Path

from click.testing import CliRunner

from steward.cli import main
from steward.operator_auth import OPERATOR_CREDENTIAL_PREFIX
from steward.store import Store
from support.cli import (
    runner as runner,  # noqa: PLC0414 — pytest fixture discovery
)

# --------------------------------------------------------------------------------------
# operator credentials (warren#225)
# --------------------------------------------------------------------------------------


def test_minting_prints_the_credential_once_and_stores_only_its_digest(
    runner: CliRunner, tmp_path: Path
) -> None:
    """The terminal is the only place the plaintext ever exists on steward's side."""
    db = tmp_path / "steward.db"

    result = runner.invoke(main, ["operator", "mint", "Miha", "--db", str(db)])

    assert result.exit_code == 0
    credential = result.output.strip().splitlines()[-1]
    assert credential.startswith(OPERATOR_CREDENTIAL_PREFIX)
    with Store(db) as store:
        assert store.operator_principal(credential) is not None
        assert credential not in db.read_bytes().decode("utf-8", "replace")


def test_a_minted_operator_gets_a_git_author_address_derived_from_their_name(
    runner: CliRunner, tmp_path: Path
) -> None:
    """Git wants an address, and a blank one produces an unparseable author line."""
    db = tmp_path / "steward.db"

    runner.invoke(main, ["operator", "mint", "Miha Zelnik", "--db", str(db)])

    with Store(db) as store:
        assert store.operators()[0].email == "miha-zelnik@steward-operator.localhost"


def test_an_explicit_email_is_what_the_commits_carry(runner: CliRunner, tmp_path: Path) -> None:
    """A real address is better than a derived one, so the flag wins when it is given."""
    db = tmp_path / "steward.db"

    runner.invoke(
        main, ["operator", "mint", "Miha", "--email", "miha@example.invalid", "--db", str(db)]
    )

    with Store(db) as store:
        assert store.operators()[0].email == "miha@example.invalid"


def test_minting_over_a_live_credential_is_refused_and_says_how_to_rotate(
    runner: CliRunner, tmp_path: Path
) -> None:
    """Silently rotating would leave the old holder unable to tell it had stopped working."""
    db = tmp_path / "steward.db"
    runner.invoke(main, ["operator", "mint", "Miha", "--db", str(db)])

    result = runner.invoke(main, ["operator", "mint", "Miha", "--db", str(db)])

    assert result.exit_code == 1
    assert "already holds a live credential" in result.output
    assert "steward operator revoke" in result.output


def test_revoking_stops_the_credential_and_keeps_the_row(runner: CliRunner, tmp_path: Path) -> None:
    """A deleted row cannot answer "who could act as this fleet's operator, and until when"."""
    db = tmp_path / "steward.db"
    minted = runner.invoke(main, ["operator", "mint", "Miha", "--db", str(db)])
    credential = minted.output.strip().splitlines()[-1]

    result = runner.invoke(main, ["operator", "revoke", "Miha", "--db", str(db)])

    assert result.exit_code == 0
    assert "revoked Miha's credential" in result.output
    with Store(db) as store:
        assert store.operator_principal(credential) is None
        assert [record.live for record in store.operators()] == [False]


def test_revoking_a_name_that_holds_nothing_says_so_rather_than_failing(
    runner: CliRunner, tmp_path: Path
) -> None:
    """Nothing was revoked, and nothing pretends otherwise."""
    result = runner.invoke(main, ["operator", "revoke", "Nobody", "--db", str(tmp_path / "s.db")])

    assert result.exit_code == 0
    assert "no live operator credential" in result.output


def test_listing_shows_revoked_credentials_rather_than_hiding_them(
    runner: CliRunner, tmp_path: Path
) -> None:
    """The audit question is about credentials that *used* to work, so they are listed."""
    db = tmp_path / "steward.db"
    runner.invoke(main, ["operator", "mint", "Miha", "--note", "townhall", "--db", str(db)])
    runner.invoke(main, ["operator", "mint", "Ana", "--db", str(db)])
    runner.invoke(main, ["operator", "revoke", "Ana", "--db", str(db)])

    result = runner.invoke(main, ["operator", "list", "--db", str(db)])

    assert "live  Miha" in result.output
    assert "townhall" in result.output
    assert "gone  Ana" in result.output


def test_listing_an_empty_table_names_what_that_means(runner: CliRunner, tmp_path: Path) -> None:
    """No credentials is not an error: it means every human is presenting the master token."""
    result = runner.invoke(main, ["operator", "list", "--db", str(tmp_path / "s.db")])

    assert result.exit_code == 0
    assert "every human caller is presenting STEWARD_TOKEN" in result.output


def test_listing_as_json_never_carries_a_plaintext_credential(
    runner: CliRunner, tmp_path: Path
) -> None:
    """The machine-readable view is the one most likely to be piped somewhere it should not."""
    db = tmp_path / "steward.db"
    minted = runner.invoke(main, ["operator", "mint", "Miha", "--db", str(db)])
    credential = minted.output.strip().splitlines()[-1]

    result = runner.invoke(main, ["operator", "list", "--db", str(db), "--format", "json"])

    payload = json.loads(result.output)
    assert payload[0]["name"] == "Miha"
    assert credential not in result.output


def test_an_operator_needs_a_name_to_be_committed_as(runner: CliRunner, tmp_path: Path) -> None:
    """A nameless operator credential would be the master token with extra steps."""
    result = runner.invoke(main, ["operator", "mint", "   ", "--db", str(tmp_path / "s.db")])

    assert result.exit_code == 1
    assert "an operator needs a name" in result.output
