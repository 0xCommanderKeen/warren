"""A reporting resident's note, admitted only after its successful routine receipt."""

import os
import stat
from pathlib import Path
from typing import Annotated, Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from steward.credential_policy import redact_mapping
from steward.store.records import LedgerEntry

NOTE_FILE = "queue-review.json"
MAX_NOTE_BYTES = 64_000
Text = Annotated[str, Field(min_length=1, max_length=4000)]


class Evidence(BaseModel):
    """A command receipt or a file-at-commit excerpt the reader can inspect."""

    model_config = ConfigDict(extra="forbid")
    source: Text
    quote: Text


class Recommendation(BaseModel):
    """Judgement attached to an issue number, never a copied issue state."""

    model_config = ConfigDict(extra="forbid")
    number: int = Field(gt=0)
    reason: Text
    evidence: list[Evidence] = Field(min_length=1, max_length=12)


class QueueNote(BaseModel):
    """The sole note schema; the order is the resident's recommendation."""

    model_config = ConfigDict(extra="forbid")
    run_id: str = Field(min_length=1, max_length=128)
    repository: str
    commit: str = Field(pattern=r"^[0-9a-f]{40}$")
    recommendations: list[Recommendation] = Field(max_length=30)


def read_note(memory: Path, receipt: LedgerEntry | None, repository: str) -> dict[str, Any]:
    """Bind a bounded, non-symlink note to the latest successful queue-review run."""
    if receipt is None:
        return {"note": None, "message": "No queue-review run has been recorded."}
    run = {
        "run_id": receipt.run_id,
        "outcome": receipt.outcome,
        "recorded_at": receipt.recorded_at,
        "resident": receipt.resident,
    }
    if receipt.outcome != "ok" or receipt.kind != "routine" or receipt.ref != "queue-review":
        return {"note": None, "run": run, "message": "The latest queue review did not succeed."}
    try:
        directory = os.open(memory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            descriptor = os.open(
                NOTE_FILE, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory
            )
            with os.fdopen(descriptor, "rb") as stream:
                if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                    raise ValueError("queue note is not a regular file")
                raw = stream.read(MAX_NOTE_BYTES + 1)
        finally:
            os.close(directory)
        if len(raw) > MAX_NOTE_BYTES:
            return {"note": None, "run": run, "message": "The queue note exceeds its size limit."}
        note = QueueNote.model_validate_json(raw)
    except OSError, ValidationError, ValueError:
        return {"note": None, "run": run, "message": "The latest queue note is missing or invalid."}
    if note.run_id != receipt.run_id or note.repository != repository:
        return {
            "note": None,
            "run": run,
            "message": "The note does not match the recorded run and repository.",
        }
    published = note.model_dump()
    published["recommendations"] = [redact_mapping(item) for item in published["recommendations"]]
    return {"note": published, "run": run, "message": None}
