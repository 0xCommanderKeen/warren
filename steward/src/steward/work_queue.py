"""GitHub facts for the work queue; recommendations belong to the reporting resident."""

import json
import re
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel, Field, ValidationError

from steward.secrets import read_secret

REPOSITORY_PATTERN = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")
PAGE_SIZE = 100
MAX_PAGES = 100
MAX_CHAINS = 1000
MAX_RESPONSE_BYTES = 4_000_000
REQUEST_TIMEOUT = 5
REFRESH_DEADLINE = 45


class QueueUnavailableError(Exception):
    """The tracker cannot supply a complete issue inventory."""


class Issue(BaseModel):
    """The fields the projection reads from GitHub's issue representation."""

    number: int = Field(gt=0)
    title: str
    state: Literal["open", "closed"]
    body: str | None = None
    labels: list[dict[str, Any]] = Field(default_factory=list)
    closed_at: datetime | None = None
    updated_at: datetime
    pull_request: dict[str, Any] | None = None


def _dependency_lines(body: str | None) -> list[str]:
    """Keep nested sections and ambiguous code blocks inside the dependency section."""
    section: list[str] = []
    level = 0
    fenced = False
    for line in (body or "").splitlines():
        if line.strip().startswith(("```", "~~~")):
            fenced = not fenced
            if level:
                section.append("Code block in dependency section requires review.")
            continue
        if fenced:
            continue
        heading = re.fullmatch(r"(#{1,6})\s+(.+?)\s*#*\s*", line)
        if heading:
            if heading[2].lower() == "blocked by":
                level = len(heading[1])
                continue
            if len(heading[1]) <= level:
                level = 0
        if level and line.strip():
            section.append(line.strip())
    return section


def blockers(body: str | None, repository: str) -> tuple[list[int], list[str]]:
    """Read explicit dependencies; ambiguous prose can never prove an issue unblocked."""
    numbers: set[int] = set()
    unknown: list[str] = []
    for line in _dependency_lines(body):
        text = re.sub(r"^[-*+]\s+(?:\[[ xX]\]\s*)?", "", line).strip()
        if text.lower().rstrip(".") in {"none", "n/a"}:
            continue
        match = re.fullmatch(r"#([1-9][0-9]*)(\s+.*)?", text)
        if not match:
            match = re.fullmatch(
                rf"https://github\.com/{re.escape(repository)}/issues/([1-9][0-9]*)(\s+.*)?",
                text,
            )
        if match:
            numbers.add(int(match[1]))
        # Descriptions may hide an independent prerequisite. Show the reference but
        # require review of every suffix, rather than interpreting natural language.
        if not match or match[2]:
            unknown.append(line)
    return sorted(numbers), unknown


def project_queue(  # noqa: PLR0913 — source facts and independent projection filters
    repository: str,
    issues: list[Issue],
    pulls: Mapping[int, str],
    *,
    observed_at: str,
    since: datetime | None = None,
    ranked: tuple[int, ...] = (),
) -> dict[str, Any]:
    """Compute dependency facts and live states without assigning a priority order."""
    by_number = {issue.number: issue for issue in issues}
    dependencies = {i.number: blockers(i.body, repository) for i in issues}

    def state(number: int) -> str:
        issue = by_number.get(number)
        return issue.state if issue else "unknown"

    def chains(number: int) -> tuple[list[list[int]], bool]:
        paths: list[list[int]] = []
        pending = [[number]]
        truncated = False
        while pending:
            path = pending.pop()
            children = dependencies.get(path[-1], ([], []))[0]
            if not children or path[-1] in path[:-1]:
                paths.append(path)
                continue
            # Bound branching without hiding that a chain was cut short.
            if len(paths) + len(pending) + len(children) > MAX_CHAINS:
                paths.append(path)
                truncated = True
                continue
            pending.extend([*path, child] for child in reversed(children))
        return paths, truncated

    def view(issue: Issue) -> dict[str, Any]:
        refs, unknown = dependencies[issue.number]
        labels = sorted(str(label["name"]) for label in issue.labels if "name" in label)
        paths, truncated = chains(issue.number) if refs else ([], False)
        return {
            "number": issue.number,
            "title": issue.title,
            "url": f"https://github.com/{repository}/issues/{issue.number}",
            "state": issue.state,
            "labels": labels,
            "priorities": [label for label in labels if label.startswith("priority:")],
            "updated_at": issue.updated_at.isoformat(),
            "closed_at": issue.closed_at.isoformat() if issue.closed_at else None,
            "blockers": [{"number": ref, "state": state(ref)} for ref in refs],
            "unknown_blockers": unknown,
            "chains": paths,
            "chains_truncated": truncated,
            "stale_blocked": (
                issue.state == "open"
                and "status:blocked" in labels
                and bool(refs)
                and not unknown
                and all(state(ref) == "closed" for ref in refs)
            ),
        }

    return {
        "repository": repository,
        "observed_at": observed_at,
        "since": since.isoformat() if since else None,
        "issues": [
            view(i)
            for i in sorted(issues, key=lambda i: i.number)
            if i.state == "open" and i.pull_request is None
        ],
        "recently_closed": [
            view(i)
            for i in sorted(issues, key=lambda i: i.number)
            if i.pull_request is None
            and i.closed_at is not None
            and (since is None or i.closed_at >= since)
        ],
        "pull_requests": [
            {
                "number": i.number,
                "title": i.title,
                "url": f"https://github.com/{repository}/pull/{i.number}",
                "mergeability": pulls.get(i.number, "UNKNOWN"),
            }
            for i in sorted(issues, key=lambda i: i.number)
            if i.pull_request is not None and i.state == "open"
        ],
        "ranked_items": [{"number": n, "state": state(n)} for n in ranked],
    }


class GitHubQueue:
    """One cached tracker inventory shared by the API's callers, with bounded refreshes."""

    def __init__(
        self,
        repository: str,
        *,
        token: str | None = None,
        fetch: Callable[[str], Any] | None = None,
    ) -> None:
        """Fix the repository at configuration time, never from an HTTP caller."""
        if not REPOSITORY_PATTERN.fullmatch(repository) or any(
            part in {".", ".."} for part in repository.split("/")
        ):
            raise QueueUnavailableError("Set STEWARD_QUEUE_REPOSITORY to owner/repository.")
        self.repository = repository
        self._token = token
        self._fetch = fetch or self._get
        self._lock = threading.Lock()
        self._loaded = 0.0
        self._retry_at = 0.0
        self._error: str | None = None
        self._issues: list[Issue] = []
        self._pulls: dict[int, str] = {}
        self._observed = ""
        self._deadline = 0.0

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> GitHubQueue:
        """Use the existing secret directory for optional private-repository access."""
        return cls(
            env.get("STEWARD_QUEUE_REPOSITORY", ""),
            token=read_secret("STEWARD_QUEUE_GITHUB_TOKEN", env=env),
        )

    def _get(self, path: str) -> Any:  # noqa: ANN401 — external JSON is validated on ingress
        remaining = self._deadline - time.monotonic()
        if remaining <= 0:
            raise QueueUnavailableError("GitHub queue refresh exceeded its time limit.")
        headers = {
            "Accept": "application/vnd.github+json",
            "User-Agent": "steward-queue",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        request = urllib.request.Request(
            f"https://api.github.com/repos/{self.repository}/{path}",
            headers=headers,
        )
        try:
            with urllib.request.urlopen(  # noqa: S310 — fixed GitHub HTTPS host
                request, timeout=min(REQUEST_TIMEOUT, remaining)
            ) as response:
                raw = response.read(MAX_RESPONSE_BYTES + 1)
            if len(raw) > MAX_RESPONSE_BYTES:
                raise QueueUnavailableError("GitHub queue response exceeded its size limit.")
            return json.loads(raw)
        except (OSError, ValueError) as exc:
            # Never echo an upstream body, URL or Authorization header into the UI.
            raise QueueUnavailableError(
                "GitHub queue read failed; check access and rate limits."
            ) from exc

    def read(
        self,
        *,
        since: datetime | None = None,
        ranked: tuple[int, ...] = (),
    ) -> dict[str, Any]:
        """Return facts with their observation time; a failed refresh is a visible failure."""
        with self._lock:
            now = time.monotonic()
            if now < self._retry_at:
                raise QueueUnavailableError(self._error or "GitHub queue is unavailable.")
            ttl = 300
            if not self._observed or now - self._loaded >= ttl:
                try:
                    self._refresh()
                except QueueUnavailableError as exc:
                    self._error = str(exc)
                    self._retry_at = time.monotonic() + 60
                    raise
            return project_queue(
                self.repository,
                self._issues,
                self._pulls,
                observed_at=self._observed,
                since=since,
                ranked=ranked,
            )

    def _refresh(self) -> None:
        self._deadline = time.monotonic() + REFRESH_DEADLINE
        inventory: dict[int, Issue] = {}
        for page in range(1, MAX_PAGES + 1):
            raw = self._fetch(
                f"issues?state=all&sort=created&direction=asc&per_page={PAGE_SIZE}&page={page}"
            )
            try:
                if not isinstance(raw, list):
                    raise QueueUnavailableError("GitHub did not return an issue list.")
                for value in raw:
                    issue = Issue.model_validate(value)
                    inventory[issue.number] = issue
            except ValidationError as exc:
                raise QueueUnavailableError("GitHub returned an invalid issue record.") from exc
            if len(raw) < PAGE_SIZE:
                break
        else:
            raise QueueUnavailableError(
                "GitHub queue exceeded the pagination limit; no partial queue shown."
            )
        pulls: dict[int, str] = {}
        for issue in inventory.values():
            if issue.pull_request is None or issue.state != "open":
                continue
            try:
                detail = self._fetch(f"pulls/{issue.number}")
                mergeable = detail.get("mergeable") if isinstance(detail, dict) else None
                pulls[issue.number] = (
                    "MERGEABLE"
                    if mergeable is True
                    else "CONFLICTING"
                    if mergeable is False
                    else "UNKNOWN"
                )
            except QueueUnavailableError:
                pulls[issue.number] = "UNKNOWN"
        self._issues = list(inventory.values())
        self._pulls = pulls
        self._observed = datetime.now(UTC).isoformat()
        self._loaded = time.monotonic()
