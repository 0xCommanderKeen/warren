"""The journal: whose it is, which day it belongs to, and how much of it survives."""

import datetime as dt
from pathlib import Path

import pytest

from conftest import RESIDENTS_DIR, ResidentWriter, valid_manifest
from steward import journal as j
from steward import manifest as m

LJUBLJANA = "Europe/Ljubljana"


def routine(**overrides: object) -> m.Routine:
    data: dict = {
        "id": "close-of-day",
        "schedule": "30 22 * * *",
        "schedule_tz": LJUBLJANA,
        "prompt": "Look back over the day.",
        "timeout_s": 600,
        "journal": "close_of_day",
    }
    data.update(overrides)
    return m.Routine.model_validate(data)


@pytest.fixture
def resident(write_resident: ResidentWriter, tmp_path: Path):
    """Build a resident whose memory is a real, throwaway directory."""

    def _build(**memory: object) -> m.Resident:
        data = valid_manifest()
        data["memory"] = {"kind": "directory", "path": str(tmp_path / "memory"), **memory}
        return m.load_manifest(write_resident(data))

    return _build


def write(manifest: m.ResidentManifest, day: str, text: str = "a day happened") -> Path:
    return j.write_entry(manifest, dt.date.fromisoformat(day), "close-of-day", text)


# ------------------------------------------------------------------- where it lives


def test_the_location_comes_from_the_manifest_and_nowhere_else(resident) -> None:
    hob = resident(journal="diary")
    assert j.resolve_journal_dir(hob.manifest) == (
        Path(hob.manifest.memory.path).resolve() / "diary"
    )


def test_the_default_journal_directory_is_a_subdirectory_of_memory(resident) -> None:
    hob = resident()
    assert j.resolve_journal_dir(hob.manifest).name == m.DEFAULT_JOURNAL_DIR
    assert j.resolve_journal_dir(hob.manifest).parent == Path(hob.manifest.memory.path).resolve()


def test_a_memory_path_with_a_tilde_is_expanded() -> None:
    maren = m.load_manifest(RESIDENTS_DIR / "burrow-builder" / "manifest.yaml")
    assert "~" not in str(j.resolve_journal_dir(maren.manifest))
    assert j.resolve_journal_dir(maren.manifest).is_relative_to(Path.home())


# --------------------------------------------------------------- loud, not silent


def test_a_memory_that_is_a_single_file_cannot_hold_a_journal(resident) -> None:
    hob = resident(kind="file")
    complaint = j.journal_complaint(hob.manifest)
    assert complaint is not None
    assert "memory.kind" in complaint
    with pytest.raises(m.ManifestError, match="nowhere to keep one entry per day"):
        j.resolve_journal_dir(hob.manifest)


def test_a_remote_memory_reference_cannot_hold_a_journal(resident) -> None:
    hob = resident(path="s3://bucket/residents/test-agent")
    assert "remote reference" in (j.journal_complaint(hob.manifest) or "")
    with pytest.raises(m.ManifestError):
        j.resolve_journal_dir(hob.manifest)


@pytest.mark.parametrize("reference", ["/etc", "../../elsewhere", "ok/../../out"])
def test_a_journal_reference_may_not_climb_out_of_memory(resident, reference: str) -> None:
    hob = resident(journal=reference)
    complaint = j.journal_complaint(hob.manifest)
    assert complaint is not None
    assert "memory.journal" in complaint


def test_the_diagnostic_names_the_manifest_it_came_from(resident) -> None:
    hob = resident(kind="file")
    with pytest.raises(m.ManifestError) as raised:
        j.resolve_journal_dir(hob.manifest, source=hob.path)
    assert raised.value.diagnostics[0].file == hob.path
    assert raised.value.diagnostics[0].field_path == "memory"
    assert raised.value.diagnostics[0].example


def test_a_healthy_memory_block_has_no_complaint(resident) -> None:
    assert j.journal_complaint(resident().manifest) is None


# ------------------------------------------------------------------- the local day


def test_a_late_evening_run_belongs_to_that_evening() -> None:
    # 23:55 in Ljubljana, which is 21:55 UTC on the same date.
    moment = dt.datetime(2026, 8, 24, 21, 55, tzinfo=dt.UTC)
    assert j.local_day(routine(schedule="55 23 * * *"), moment) == dt.date(2026, 8, 24)


def test_a_run_after_local_midnight_belongs_to_the_new_day_not_the_utc_one() -> None:
    # 00:30 on the 25th in Ljubljana is still 22:30 on the 24th in UTC. The household
    # is the one keeping the day, so the entry is the 25th's.
    moment = dt.datetime(2026, 8, 24, 22, 30, tzinfo=dt.UTC)
    assert moment.date() == dt.date(2026, 8, 24)
    assert j.local_day(routine(schedule="30 0 * * *"), moment) == dt.date(2026, 8, 25)


def test_a_routine_in_utc_reads_the_day_in_utc() -> None:
    moment = dt.datetime(2026, 8, 24, 22, 30, tzinfo=dt.UTC)
    assert j.local_day(routine(schedule_tz="UTC"), moment) == dt.date(2026, 8, 24)


def test_the_entry_filename_is_the_local_day(resident) -> None:
    hob = resident()
    path = j.entry_path(hob.manifest, dt.date(2026, 8, 24))
    assert path.name == "2026-08-24.md"


# ------------------------------------------------------------------ writing entries


def test_an_entry_opens_with_resident_date_and_routine(resident) -> None:
    hob = resident()
    path = write(hob.manifest, "2026-08-24", "The inbox was quiet.")
    body = path.read_text(encoding="utf-8")
    assert body.startswith("---\n")
    assert "resident: test-agent" in body
    assert "date: 2026-08-24" in body
    assert "routine: close-of-day" in body
    assert body.rstrip().endswith("The inbox was quiet.")


def test_writing_creates_the_journal_directory(resident) -> None:
    hob = resident()
    assert not j.resolve_journal_dir(hob.manifest).exists()
    assert write(hob.manifest, "2026-08-24").is_file()


# ------------------------------------------------------------------ reading it back


def test_a_resident_that_has_never_written_has_no_entry(resident) -> None:
    hob = resident()
    assert j.latest_entry(hob.manifest) is None
    assert j.read_entries(hob.manifest) == []


def test_the_latest_entry_is_the_newest_one(resident) -> None:
    hob = resident()
    write(hob.manifest, "2026-08-22", "two days ago")
    write(hob.manifest, "2026-08-24", "yesterday")
    write(hob.manifest, "2026-08-23", "the day between")

    latest = j.latest_entry(hob.manifest)
    assert latest is not None
    assert "yesterday" in latest
    assert "2026-08-24" in latest
    assert "the day between" not in latest


def test_a_failed_day_falls_back_to_the_previous_surviving_entry(resident) -> None:
    """A session that died before it journaled leaves yesterday's entry standing."""
    hob = resident()
    write(hob.manifest, "2026-08-23", "the last night that worked")
    # Nothing was written for the 24th, because that session failed.
    latest = j.latest_entry(hob.manifest)
    assert latest is not None
    assert "the last night that worked" in latest
    assert "2026-08-23" in latest


def test_read_entries_are_newest_first_and_structured(resident) -> None:
    hob = resident()
    for day in ("2026-08-21", "2026-08-22", "2026-08-23"):
        write(hob.manifest, day, f"entry for {day}")

    entries = j.read_entries(hob.manifest)
    assert [entry.date.isoformat() for entry in entries] == [
        "2026-08-23",
        "2026-08-22",
        "2026-08-21",
    ]
    assert all(entry.routine == "close-of-day" for entry in entries)
    assert entries[0].text == "entry for 2026-08-23"
    assert entries[0].as_dict()["date"] == "2026-08-23"
    assert entries[0].as_dict()["path"] == str(entries[0].path)


def test_read_entries_honours_its_limit(resident) -> None:
    hob = resident()
    for day in range(1, 8):
        write(hob.manifest, f"2026-08-0{day}")
    assert len(j.read_entries(hob.manifest, 3)) == 3
    assert j.read_entries(hob.manifest, 0) == []
    assert j.read_entries(hob.manifest, -1) == []


def test_an_entry_written_without_a_header_still_reads(resident) -> None:
    hob = resident()
    directory = j.resolve_journal_dir(hob.manifest)
    directory.mkdir(parents=True)
    (directory / "2026-08-24.md").write_text("just prose, no header\n", encoding="utf-8")

    entries = j.read_entries(hob.manifest)
    assert len(entries) == 1
    assert entries[0].routine is None, "steward reports what the file says, not what it wishes"
    assert entries[0].text == "just prose, no header"


def test_files_that_are_not_dated_entries_are_left_alone(resident) -> None:
    hob = resident()
    write(hob.manifest, "2026-08-24")
    directory = j.resolve_journal_dir(hob.manifest)
    (directory / "notes.md").write_text("somebody else's file", encoding="utf-8")
    (directory / "2026-13-45.md").write_text("not a date", encoding="utf-8")
    (directory / "2026-08-25").mkdir()

    assert [entry.date.isoformat() for entry in j.read_entries(hob.manifest)] == ["2026-08-24"]
    j.rotate(hob.manifest, keep=0)
    assert (directory / "notes.md").is_file(), "rotation only ever removes what it can name"


def test_an_empty_entry_file_is_not_an_entry(resident) -> None:
    hob = resident()
    directory = j.resolve_journal_dir(hob.manifest)
    directory.mkdir(parents=True)
    (directory / "2026-08-24.md").write_text("---\nroutine: x\n---\n\n   \n", encoding="utf-8")
    assert j.read_entries(hob.manifest) == []
    assert j.latest_entry(hob.manifest) is None


def test_an_unreadable_journal_directory_is_simply_empty(resident) -> None:
    hob = resident()
    j.resolve_journal_dir(hob.manifest).parent.mkdir(parents=True)
    j.resolve_journal_dir(hob.manifest).write_text("this is a file, not a directory")
    assert j.read_entries(hob.manifest) == []


# ------------------------------------------------------ a bad entry never bricks a read


def test_an_entry_with_invalid_utf8_is_read_not_raised(resident) -> None:
    """The journal is text a model wrote; a bad byte degrades to replaced, never a raise."""
    hob = resident()
    directory = j.resolve_journal_dir(hob.manifest)
    directory.mkdir(parents=True)
    (directory / "2026-08-24.md").write_bytes(
        b"---\nresident: test-agent\nroutine: close-of-day\n---\n\nbad \xff\xfe bytes\n"
    )

    entries = j.read_entries(hob.manifest)  # must not raise
    assert len(entries) == 1
    assert "bad" in entries[0].text
    latest = j.latest_entry(hob.manifest)  # must not raise
    assert latest is not None
    assert "bad" in latest


def test_a_garbage_file_does_not_stop_the_read_of_the_real_ones(resident) -> None:
    hob = resident()
    write(hob.manifest, "2026-08-23", "a real entry")
    directory = j.resolve_journal_dir(hob.manifest)
    # A garbage file dated newer than the real entry: read must survive it, not raise.
    (directory / "2026-08-24.md").write_bytes(b"\x80\x81\x82\xff\xfe")

    entries = j.read_entries(hob.manifest)
    assert [e.date.isoformat() for e in entries] == ["2026-08-24", "2026-08-23"]
    latest = j.latest_entry(hob.manifest)
    assert latest is not None


# --------------------------------------------------- a shared journal dir does not leak


def test_a_shared_journal_dir_does_not_cross_feed_between_residents(resident) -> None:
    """Two manifests pointing at the same dir must not read each other's entries (#77)."""
    alice = resident()  # id test-agent, via valid_manifest
    write(alice.manifest, "2026-08-23", "alice was here")
    directory = j.resolve_journal_dir(alice.manifest)
    (directory / "2026-08-24.md").write_text(
        "---\nresident: bob\ndate: 2026-08-24\nroutine: close-of-day\n---\n\nbob's private note\n",
        encoding="utf-8",
    )

    latest = j.latest_entry(alice.manifest)
    assert latest is not None
    assert "alice was here" in latest
    assert "bob" not in latest
    assert [e.date.isoformat() for e in j.read_entries(alice.manifest)] == ["2026-08-23"]


def test_a_legacy_headerless_entry_is_still_read_as_this_residents(resident) -> None:
    """Ownership is lenient: an entry with no resident header counts as this resident's."""
    hob = resident()
    directory = j.resolve_journal_dir(hob.manifest)
    directory.mkdir(parents=True)
    (directory / "2026-08-24.md").write_text("just prose, no header\n", encoding="utf-8")
    assert j.latest_entry(hob.manifest) is not None


# ------------------------------------------------------------------------- the cap


def test_an_oversized_entry_is_truncated_at_the_documented_cap(resident) -> None:
    hob = resident()
    write(hob.manifest, "2026-08-24", "note " * 5000)
    latest = j.latest_entry(hob.manifest)
    assert latest is not None
    assert len(latest) <= j.JOURNAL_MAX_CHARS + 100
    assert latest.endswith("[truncated at the injection cap]")


def test_the_cap_is_a_parameter_not_a_secret(resident) -> None:
    hob = resident()
    write(hob.manifest, "2026-08-24", "x" * 500)
    assert len(j.latest_entry(hob.manifest, 80) or "") <= 80 + 100
    assert j.JOURNAL_MAX_CHARS == 4000


def test_an_entry_inside_the_cap_is_left_exactly_alone(resident) -> None:
    hob = resident()
    write(hob.manifest, "2026-08-24", "short and finished")
    latest = j.latest_entry(hob.manifest)
    assert latest is not None
    assert "truncated" not in latest
    assert latest.endswith("short and finished")


# ------------------------------------------------------------------------ rotation


def test_rotation_keeps_the_newest_entries_and_no_more(resident) -> None:
    hob = resident()
    days = [dt.date(2026, 1, 1) + dt.timedelta(days=n) for n in range(40)]
    for day in days:
        write(hob.manifest, day.isoformat())

    removed = j.rotate(hob.manifest)
    surviving = [entry.date for entry in j.read_entries(hob.manifest, 100)]

    assert len(surviving) == m.DEFAULT_KEEP_ENTRIES
    assert surviving == sorted(days[-m.DEFAULT_KEEP_ENTRIES :], reverse=True)
    assert len(removed) == 10
    assert all(not path.exists() for path in removed)


def test_the_manifest_sets_the_retention_bound(resident) -> None:
    hob = resident(journal_keep=3)
    for n in range(9):
        write(hob.manifest, f"2026-08-0{n + 1}")
    j.rotate(hob.manifest)
    assert [e.date.isoformat() for e in j.read_entries(hob.manifest, 100)] == [
        "2026-08-09",
        "2026-08-08",
        "2026-08-07",
    ]


def test_rotation_under_the_bound_removes_nothing(resident) -> None:
    hob = resident(journal_keep=30)
    write(hob.manifest, "2026-08-24")
    assert j.rotate(hob.manifest) == ()
    assert len(j.read_entries(hob.manifest)) == 1


def test_rotating_an_empty_journal_is_harmless(resident) -> None:
    assert j.rotate(resident().manifest) == ()


def test_an_empty_file_left_by_a_died_session_does_not_evict_a_real_entry(resident) -> None:
    """journal_keep=1 with one empty + one real file keeps the real one (#78)."""
    hob = resident(journal_keep=1)
    write(hob.manifest, "2026-08-23", "the real entry")
    directory = j.resolve_journal_dir(hob.manifest)
    # A session that created today's file then died before writing a body.
    (directory / "2026-08-24.md").write_text("\n  \n", encoding="utf-8")

    removed = j.rotate(hob.manifest)
    surviving = [e.date.isoformat() for e in j.read_entries(hob.manifest, 100)]
    assert surviving == ["2026-08-23"], "the empty file must not count toward retention"
    # The real entry is untouched; the unreadable file steward leaves in place.
    assert (directory / "2026-08-23.md").is_file()
    assert (directory / "2026-08-24.md") not in removed


def test_rotation_never_deletes_another_residents_entry_in_a_shared_dir(resident) -> None:
    hob = resident(journal_keep=1)
    write(hob.manifest, "2026-08-22", "hob, two days ago")
    write(hob.manifest, "2026-08-23", "hob, yesterday")
    directory = j.resolve_journal_dir(hob.manifest)
    (directory / "2026-08-24.md").write_text(
        "---\nresident: someone-else\ndate: 2026-08-24\n---\n\nnot hob's to rotate\n",
        encoding="utf-8",
    )

    j.rotate(hob.manifest)
    assert (directory / "2026-08-24.md").is_file(), "another resident's entry is never rotated"
    assert (directory / "2026-08-23.md").is_file(), "hob keeps his newest"
    assert not (directory / "2026-08-22.md").exists(), "hob's older entry rotates out"


# --------------------------------------------------------- cross-resident isolation


def test_two_residents_never_read_each_others_journals(
    write_resident: ResidentWriter, tmp_path: Path
) -> None:
    def build(name: str) -> m.Resident:
        data = valid_manifest()
        data["id"] = name
        data["agent_id"] = f"claude-code:{name}"
        data["memory"] = {"kind": "directory", "path": str(tmp_path / name / "memory")}
        soul = "---\nname: Testy\n---\nA villager that exists only inside a test.\n"
        return m.load_manifest(write_resident(data, soul=soul, root=tmp_path / name / "residents"))

    first, second = build("first-agent"), build("second-agent")
    write(first.manifest, "2026-08-24", "only the first resident wrote this")

    assert j.resolve_journal_dir(first.manifest) != j.resolve_journal_dir(second.manifest)
    assert "first resident" in (j.latest_entry(first.manifest) or "")
    assert j.latest_entry(second.manifest) is None
    assert j.read_entries(second.manifest) == []


# ------------------------------------------------------------- the block fallback


def test_a_journal_block_is_extracted_verbatim() -> None:
    output = "Here is the day.\n<journal>\nQuiet. Two drafts left.\n</journal>\nDone."
    assert j.extract_block(output) == "Quiet. Two drafts left."


def test_output_without_markers_carries_no_entry() -> None:
    assert j.extract_block("I did some things today.") is None
    assert j.extract_block("") is None
    assert j.extract_block("<journal>   </journal>") is None


def test_the_last_block_wins_when_the_instruction_is_quoted_back() -> None:
    output = "<journal>the example</journal>\nand now really:\n<journal>the real entry</journal>"
    assert j.extract_block(output) == "the real entry"


def test_a_block_is_persisted_with_a_header_and_attributed_to_the_routine(resident) -> None:
    hob = resident()
    day = dt.date(2026, 8, 24)
    closed = j.persist_close_of_day(
        hob.manifest, day, "close-of-day", "<journal>Nothing needed a person.</journal>"
    )

    assert closed.persisted is True
    assert closed.path == j.entry_path(hob.manifest, day)
    body = closed.path.read_text(encoding="utf-8")
    assert "routine: close-of-day" in body
    assert "Nothing needed a person." in body


def test_a_file_the_session_wrote_itself_wins_over_a_block(resident) -> None:
    hob = resident()
    day = dt.date(2026, 8, 24)
    own = write(hob.manifest, day.isoformat(), "I wrote this myself.")

    closed = j.persist_close_of_day(
        hob.manifest, day, "close-of-day", "<journal>steward's copy</journal>"
    )

    assert closed.persisted is False, "steward does not rewrite what the resident wrote"
    assert closed.path == own
    assert "I wrote this myself." in own.read_text(encoding="utf-8")
    assert "steward's copy" not in own.read_text(encoding="utf-8")


def test_an_empty_file_the_session_left_behind_does_not_beat_a_real_block(resident) -> None:
    hob = resident()
    day = dt.date(2026, 8, 24)
    path = j.entry_path(hob.manifest, day)
    path.parent.mkdir(parents=True)
    path.write_text("\n  \n", encoding="utf-8")

    closed = j.persist_close_of_day(hob.manifest, day, "close-of-day", "<journal>real</journal>")
    assert closed.persisted is True
    assert "real" in path.read_text(encoding="utf-8")


def test_no_file_and_no_block_is_no_entry_not_an_invented_one(resident) -> None:
    hob = resident()
    closed = j.persist_close_of_day(
        hob.manifest, dt.date(2026, 8, 24), "close-of-day", "I ran out of time."
    )
    assert closed.path is None
    assert closed.persisted is False
    assert j.latest_entry(hob.manifest) is None


def test_closing_the_day_also_rotates(resident) -> None:
    hob = resident(journal_keep=2)
    write(hob.manifest, "2026-08-20")
    write(hob.manifest, "2026-08-21")
    write(hob.manifest, "2026-08-22")

    closed = j.persist_close_of_day(
        hob.manifest, dt.date(2026, 8, 23), "close-of-day", "<journal>tonight</journal>"
    )
    assert len(closed.rotated) == 2
    assert [e.date.isoformat() for e in j.read_entries(hob.manifest, 100)] == [
        "2026-08-23",
        "2026-08-22",
    ]


# ------------------------------------------------------------------- the instruction


def test_the_instruction_names_the_file_the_header_and_the_markers(resident) -> None:
    hob = resident()
    day = dt.date(2026, 8, 24)
    text = j.close_of_day_instruction(hob.manifest, day, "close-of-day")

    assert str(j.entry_path(hob.manifest, day)) in text
    assert "resident: test-agent" in text
    assert "date: 2026-08-24" in text
    assert "routine: close-of-day" in text
    assert j.JOURNAL_OPEN in text
    assert j.JOURNAL_CLOSE in text
    assert "the one that counts" in text


def test_the_closing_routine_is_the_flagged_one(resident) -> None:
    hob = resident()
    assert j.close_of_day_routine(hob.manifest) is None, "the fixture flags nothing"

    data = valid_manifest()
    data["routines"] = [
        {**data["routines"][0], "id": "morning"},
        {
            "id": "close-of-day",
            "schedule": "30 22 * * *",
            "prompt": "Look back.",
            "timeout_s": 600,
            "journal": "close_of_day",
        },
    ]
    manifest = m.ResidentManifest.model_validate(data)
    closer = j.close_of_day_routine(manifest)
    assert closer is not None
    assert closer.id == "close-of-day"


def test_a_disabled_closer_does_not_close_the_day() -> None:
    data = valid_manifest()
    data["routines"] = [
        {
            "id": "close-of-day",
            "schedule": "30 22 * * *",
            "prompt": "Look back.",
            "timeout_s": 600,
            "journal": "close_of_day",
            "enabled": False,
        }
    ]
    assert j.close_of_day_routine(m.ResidentManifest.model_validate(data)) is None
