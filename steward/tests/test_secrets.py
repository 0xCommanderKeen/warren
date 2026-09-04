"""The secrets directory: what steward reads from it, and what it refuses to write to it.

Files first, environment second (warren#462). Every test here works against a scratch
directory rather than the deployed ``/secrets`` mount, so the suite never reads or writes
a credential a developer actually holds.
"""

from __future__ import annotations

import stat
from pathlib import Path

import pytest

from steward import secrets as sec


def test_default_directory_is_the_deployed_mount() -> None:
    """The compose file bind-mounts one path, and the code names the same one."""
    assert sec.secrets_dir({}) == Path(sec.DEFAULT_SECRETS_DIR)


def test_the_environment_can_move_the_directory(tmp_path: Path) -> None:
    """A laptop and a test run point the resolver somewhere they own."""
    assert sec.secrets_dir({sec.SECRETS_DIR_ENV: str(tmp_path)}) == tmp_path


def test_a_blank_setting_falls_back_to_the_default() -> None:
    """An exported-but-empty variable is unset, as everywhere else in steward."""
    assert sec.secrets_dir({sec.SECRETS_DIR_ENV: "   "}) == Path(sec.DEFAULT_SECRETS_DIR)


# -- names -----------------------------------------------------------------------------


@pytest.mark.parametrize(
    "name",
    ["STEWARD_CHAT_TOKEN_HOB", "A", "A_B_C", "STEWARD_CHAT_TOKEN_DISCORD_HOB9"],
)
def test_an_environment_shaped_name_is_accepted(name: str) -> None:
    """The names are the existing environment names, so that is the whole grammar."""
    assert sec.valid_name(name)


@pytest.mark.parametrize(
    "name",
    [
        "",
        "lower",
        "WITH SPACE",
        "WITH-DASH",
        "9LEADING",
        "..",
        "../../etc/passwd",
        "NESTED/NAME",
        "TRAILING_",
        "A" * (sec.NAME_MAX_CHARS + 1),
    ],
)
def test_anything_that_is_not_one_is_refused(name: str) -> None:
    """A name is a file name on a mount steward writes to: traversal never gets a turn."""
    assert not sec.valid_name(name)


def test_writing_refuses_a_name_the_grammar_rejects(tmp_path: Path) -> None:
    """The refusal is raised, not silently skipped — a caller must not think it wrote."""
    with pytest.raises(sec.SecretError, match="not a secret name"):
        sec.write_secret("../escape", "value", directory=tmp_path)


# -- reading ---------------------------------------------------------------------------


def test_a_file_is_read(tmp_path: Path) -> None:
    """The point of the whole change: a token can arrive as a file."""
    (tmp_path / "STEWARD_CHAT_TOKEN_HOB").write_text("from-file\n", encoding="utf-8")
    assert sec.read_secret("STEWARD_CHAT_TOKEN_HOB", env={}, directory=tmp_path) == "from-file"


def test_a_file_wins_over_the_environment(tmp_path: Path) -> None:
    """File then env, in that order, so a written secret takes effect over a stale one."""
    (tmp_path / "STEWARD_CHAT_TOKEN_HOB").write_text("from-file", encoding="utf-8")
    value = sec.read_secret(
        "STEWARD_CHAT_TOKEN_HOB", env={"STEWARD_CHAT_TOKEN_HOB": "from-env"}, directory=tmp_path
    )
    assert value == "from-file"


def test_the_environment_still_answers_when_no_file_exists(tmp_path: Path) -> None:
    """Nothing migrates by force: the burrow's existing ``.env`` keeps working."""
    value = sec.read_secret(
        "STEWARD_CHAT_TOKEN_HOB", env={"STEWARD_CHAT_TOKEN_HOB": "from-env"}, directory=tmp_path
    )
    assert value == "from-env"


def test_an_empty_file_is_not_a_secret(tmp_path: Path) -> None:
    """A slot somebody created and never filled is unset, not a token of zero length."""
    (tmp_path / "STEWARD_CHAT_TOKEN_HOB").write_text("\n", encoding="utf-8")
    assert sec.read_secret("STEWARD_CHAT_TOKEN_HOB", env={}, directory=tmp_path) is None


def test_a_missing_directory_reads_as_nothing_set(tmp_path: Path) -> None:
    """A laptop has no ``/secrets``, and that is not an error anybody needs to hear."""
    assert sec.read_secret("STEWARD_CHAT_TOKEN_HOB", env={}, directory=tmp_path / "nope") is None
    assert sec.secret_names(tmp_path / "nope") == []


def test_a_name_the_grammar_rejects_never_reaches_the_filesystem(tmp_path: Path) -> None:
    """Reading is a lookup, not a path join a caller can steer."""
    (tmp_path.parent / "outside").write_text("secret", encoding="utf-8")
    assert sec.read_secret("../outside", env={}, directory=tmp_path) is None


def test_listing_returns_only_names_the_grammar_accepts(tmp_path: Path) -> None:
    """A stray ``.gitkeep`` or an editor's backup file is not a secret."""
    (tmp_path / "STEWARD_CHAT_TOKEN_HOB").write_text("x", encoding="utf-8")
    (tmp_path / ".gitkeep").write_text("", encoding="utf-8")
    (tmp_path / "notes.txt").write_text("x", encoding="utf-8")
    (tmp_path / "SUBDIR").mkdir()
    assert sec.secret_names(tmp_path) == ["STEWARD_CHAT_TOKEN_HOB"]


def test_an_unreadable_file_reads_as_unset_rather_than_raising(tmp_path: Path) -> None:
    """A permissions mistake on the mount must not take a daemon down mid-poll."""
    path = tmp_path / "STEWARD_CHAT_TOKEN_HOB"
    path.write_text("x", encoding="utf-8")
    path.chmod(0o000)
    try:
        assert sec.read_secret("STEWARD_CHAT_TOKEN_HOB", env={}, directory=tmp_path) is None
    finally:
        path.chmod(0o600)


# -- the overlay -----------------------------------------------------------------------


def test_the_overlay_puts_files_over_the_environment(tmp_path: Path) -> None:
    """One mapping every existing ``env``-shaped reader can be handed unchanged."""
    (tmp_path / "STEWARD_CHAT_TOKEN_HOB").write_text("from-file", encoding="utf-8")
    (tmp_path / "STEWARD_CHAT_TOKEN_PIP").write_text("pip-file", encoding="utf-8")
    merged = sec.overlay(
        {"STEWARD_CHAT_TOKEN_HOB": "from-env", "STEWARD_CHAT_OPERATORS": "42"},
        directory=tmp_path,
    )
    assert merged["STEWARD_CHAT_TOKEN_HOB"] == "from-file"
    assert merged["STEWARD_CHAT_TOKEN_PIP"] == "pip-file"
    assert merged["STEWARD_CHAT_OPERATORS"] == "42"


def test_the_overlay_does_not_mutate_what_it_was_given(tmp_path: Path) -> None:
    """``os.environ`` is passed in by every default caller and must come back untouched."""
    (tmp_path / "STEWARD_CHAT_TOKEN_HOB").write_text("from-file", encoding="utf-8")
    source = {"STEWARD_CHAT_TOKEN_HOB": "from-env"}
    sec.overlay(source, directory=tmp_path)
    assert source == {"STEWARD_CHAT_TOKEN_HOB": "from-env"}


def test_the_overlay_reads_its_directory_from_the_mapping(tmp_path: Path) -> None:
    """A caller with its own environment mapping gets that mapping's secrets directory."""
    (tmp_path / "STEWARD_CHAT_TOKEN_HOB").write_text("from-file", encoding="utf-8")
    merged = sec.overlay({sec.SECRETS_DIR_ENV: str(tmp_path)})
    assert merged["STEWARD_CHAT_TOKEN_HOB"] == "from-file"


# -- writing ---------------------------------------------------------------------------


def test_writing_creates_a_private_file(tmp_path: Path) -> None:
    """Mode 600: the file is the credential, so nothing but its owner may read it."""
    directory = tmp_path / "secrets"
    sec.write_secret("STEWARD_CHAT_TOKEN_HOB", "shhh", directory=directory)
    path = directory / "STEWARD_CHAT_TOKEN_HOB"
    assert path.read_text(encoding="utf-8") == "shhh"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(directory.stat().st_mode) == 0o700


def test_writing_replaces_an_existing_secret_and_keeps_the_mode(tmp_path: Path) -> None:
    """Rotation is the common case, and a rotated file must not widen."""
    sec.write_secret("STEWARD_CHAT_TOKEN_HOB", "first", directory=tmp_path)
    sec.write_secret("STEWARD_CHAT_TOKEN_HOB", "second", directory=tmp_path)
    path = tmp_path / "STEWARD_CHAT_TOKEN_HOB"
    assert path.read_text(encoding="utf-8") == "second"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_writing_leaves_no_temporary_file_behind(tmp_path: Path) -> None:
    """Atomic through a rename, and the scratch file is inside the same private directory."""
    sec.write_secret("STEWARD_CHAT_TOKEN_HOB", "shhh", directory=tmp_path)
    assert sorted(p.name for p in tmp_path.iterdir()) == ["STEWARD_CHAT_TOKEN_HOB"]


def test_writing_strips_the_newline_a_paste_carries(tmp_path: Path) -> None:
    """A token pasted out of a browser arrives with whitespace; the bot API refuses it."""
    sec.write_secret("STEWARD_CHAT_TOKEN_HOB", "  shhh\n", directory=tmp_path)
    assert sec.read_secret("STEWARD_CHAT_TOKEN_HOB", env={}, directory=tmp_path) == "shhh"


def test_writing_refuses_a_blank_value(tmp_path: Path) -> None:
    """Unsetting is a different act; a blank PUT is a mistake, not a deletion."""
    with pytest.raises(sec.SecretError, match="blank"):
        sec.write_secret("STEWARD_CHAT_TOKEN_HOB", "   ", directory=tmp_path)
    assert not (tmp_path / "STEWARD_CHAT_TOKEN_HOB").exists()


def test_writing_refuses_a_value_longer_than_any_credential(tmp_path: Path) -> None:
    """A bound, so the write path cannot be used to fill the burrow's disk."""
    with pytest.raises(sec.SecretError, match="longer than"):
        sec.write_secret(
            "STEWARD_CHAT_TOKEN_HOB", "x" * (sec.VALUE_MAX_CHARS + 1), directory=tmp_path
        )


def test_writing_refuses_a_value_that_is_not_one_line(tmp_path: Path) -> None:
    """A file read back line-first must round-trip; a pasted block is a paste mistake."""
    with pytest.raises(sec.SecretError, match="one line"):
        sec.write_secret("STEWARD_CHAT_TOKEN_HOB", "one\ntwo", directory=tmp_path)
