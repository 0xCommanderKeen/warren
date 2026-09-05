"""Published queue response: computed tracker facts beside an attributed note."""

from typing import Literal

from pydantic import BaseModel, ConfigDict

from steward.queue_report import QueueNote


class _Response(BaseModel):
    model_config = ConfigDict(extra="forbid")


class BlockerState(_Response):
    """A referenced number, including a dependency the tracker could not identify."""

    number: int
    state: Literal["open", "closed", "unknown"]


class QueueIssue(_Response):
    """One issue's mechanical facts, without a recommendation rank."""

    number: int
    title: str
    url: str
    state: Literal["open", "closed"]
    labels: list[str]
    priorities: list[str]
    updated_at: str
    closed_at: str | None
    blockers: list[BlockerState]
    unknown_blockers: list[str]
    chains: list[list[int]]
    chains_truncated: bool
    stale_blocked: bool


class QueuePullRequest(_Response):
    """A PR's nullable mergeability, distinct from CI and branch protection."""

    number: int
    title: str
    url: str
    mergeability: Literal["MERGEABLE", "CONFLICTING", "UNKNOWN"]


class QueueReviewRun(_Response):
    """A run receipt taken from Steward's ledger, never from the note."""

    run_id: str
    outcome: str
    recorded_at: str
    resident: str


class QueueReport(_Response):
    """The note if provenance agrees, or an explicit absence/failure."""

    note: QueueNote | None
    message: str | None
    run: QueueReviewRun | None = None


class QueueResponse(_Response):
    """A complete issue inventory with the observation's time and resident attribution."""

    repository: str
    observed_at: str
    since: str | None
    issues: list[QueueIssue]
    recently_closed: list[QueueIssue]
    pull_requests: list[QueuePullRequest]
    ranked_items: list[BlockerState]
    reporter: str
    report: QueueReport
