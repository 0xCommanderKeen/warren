# ruff: noqa: ANN401 — fixture factories deliberately accept malformed external values
"""Queue behavior at the projection, tracker and authenticated HTTP interfaces."""

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from steward.api import ApiConfig, create_app
from steward.events import NullEmitter
from steward.work_queue import GitHubQueue, Issue, QueueUnavailableError, project_queue

NOW = "2026-09-05T12:00:00+00:00"
REPO = "owner/repo"


def issue(number=1, **fields: Any):
    return Issue.model_validate(
        {"number": number, "title": f"Issue {number}", "state": "open", "updated_at": NOW, **fields}
    )


def project(*items: Issue, **options: Any):
    return project_queue(REPO, list(items), {}, observed_at=NOW, **options)


def test_only_closed_explicit_blockers_prove_a_blocked_label_stale():
    result = project(
        issue(
            1, labels=[{"name": "status:blocked"}], body="## Blocked by\n- #2\n- #3\n## Notes\n#99"
        ),
        issue(2, state="closed"),
        issue(3, state="closed"),
        issue(4, labels=[{"name": "status:blocked"}], body="## Blocked by\n- #99"),
        issue(5, labels=[{"name": "status:blocked"}], body="## Blocked by\nNone"),
        issue(
            6, labels=[{"name": "status:blocked"}], body="## Blocked by\n- #2\n- operator sign-off"
        ),
    )
    rows = {item["number"]: item for item in result["issues"]}
    assert rows[1]["stale_blocked"] is True
    assert rows[1]["blockers"] == [
        {"number": 2, "state": "closed"},
        {"number": 3, "state": "closed"},
    ]
    assert rows[4]["stale_blocked"] is False
    assert rows[4]["blockers"] == [{"number": 99, "state": "unknown"}]
    assert rows[5]["stale_blocked"] is False
    assert rows[6]["unknown_blockers"] == ["- operator sign-off"]
    assert rows[6]["stale_blocked"] is False


@pytest.mark.parametrize(
    "body",
    [
        "## Blocked by\n- https://github.com/other/repo/issues/2",
        "## Blocked by\n- #2 and #3",
        "## Blocked by\nNone — but do not start before #2",
        "```\n## Blocked by\n- #2\n```",
    ],
)
def test_ambiguous_or_quoted_dependencies_never_prove_staleness(body):
    result = project(
        issue(body=body, labels=[{"name": "status:blocked"}]), issue(2, state="closed")
    )
    assert result["issues"][0]["stale_blocked"] is False


def test_cycles_terminate_and_cross_repo_references_do_not_alias_local_numbers():
    result = project(issue(1, body="## Blocked by\n- #2"), issue(2, body="## Blocked by\n- #1"))
    assert result["issues"][0]["chains"] == [[1, 2, 1]]


def test_recent_closures_use_closed_at_not_updated_at_and_ranked_items_keep_live_state():
    result = project(
        issue(1, state="closed", closed_at="2026-09-04T10:00:00Z"),
        issue(2, state="closed", closed_at="2026-09-05T10:00:00Z"),
        since=datetime(2026, 9, 5, tzinfo=UTC),
        ranked=(1, 2, 99),
    )
    assert [i["number"] for i in result["recently_closed"]] == [2]
    assert result["ranked_items"] == [
        {"number": 1, "state": "closed"},
        {"number": 2, "state": "closed"},
        {"number": 99, "state": "unknown"},
    ]


def test_tracker_paginates_without_mixing_prs_into_issues_and_caches_reads():
    calls = []

    def fetch(path) -> Any:
        calls.append(path)
        if path.endswith("&page=1"):
            return [issue(n).model_dump(mode="json", exclude_none=True) for n in range(1, 101)]
        if path.endswith("&page=2"):
            return [issue(101, pull_request={"url": "unused"}).model_dump(mode="json")]
        return {"mergeable": False}

    source = GitHubQueue(REPO, fetch=fetch)
    result = source.read()
    assert len(result["issues"]) == 100
    assert result["pull_requests"][0]["mergeability"] == "CONFLICTING"
    assert len(calls) == 3
    assert source.read()["observed_at"] == result["observed_at"]
    assert len(calls) == 3


def test_failed_inventory_is_not_an_empty_or_partially_complete_queue():
    def fetch(_path) -> Any:
        raise QueueUnavailableError("offline")

    with pytest.raises(QueueUnavailableError, match="offline"):
        GitHubQueue(REPO, fetch=fetch).read()


def test_unknown_mergeability_remains_unknown():
    def fetch(path) -> Any:
        if path.startswith("issues"):
            return [issue(pull_request={}).model_dump(mode="json")]
        return {"mergeable": None}

    assert GitHubQueue(REPO, fetch=fetch).read()["pull_requests"][0]["mergeability"] == "UNKNOWN"


def test_api_authentication_timestamp_validation_and_missing_report(tmp_path: Path):
    app = create_app(
        ApiConfig(residents_dir=tmp_path, db_path=tmp_path / "db", token="test"),
        emitter=NullEmitter(),
        queue_source=GitHubQueue(REPO, fetch=lambda _: []),
    )
    with TestClient(app) as client:
        assert client.get("/queue").status_code == 401
        response = client.get("/queue", headers={"Authorization": "Bearer test"})
        assert response.status_code == 200
        assert response.json()["report"]["note"] is None
        assert (
            client.get(
                "/queue?since=2026-09-05T00:00:00", headers={"Authorization": "Bearer test"}
            ).status_code
            == 422
        )


@pytest.mark.parametrize(
    "repository", ["../repo", "owner/..", "https://example.com/repo", "owner/repo/extra"]
)
def test_repository_configuration_cannot_change_the_github_endpoint(repository):
    with pytest.raises(QueueUnavailableError):
        GitHubQueue(repository)


def test_expired_observation_is_not_reused_after_a_failed_refresh(monkeypatch):
    clock = [1.0]
    calls = []
    monkeypatch.setattr("steward.work_queue.time.monotonic", lambda: clock[0])

    def fetch(path) -> Any:
        calls.append(path)
        if len(calls) > 1:
            raise QueueUnavailableError("offline")
        return [issue().model_dump(mode="json")]

    source = GitHubQueue(REPO, fetch=fetch)
    assert len(source.read()["issues"]) == 1
    clock[0] = 302.0
    with pytest.raises(QueueUnavailableError, match="offline"):
        source.read()
    with pytest.raises(QueueUnavailableError, match="offline"):
        source.read()
    assert len(calls) == 2


def test_invalid_inventory_records_fail_instead_of_becoming_unblocked():
    source = GitHubQueue(REPO, fetch=lambda _: [{"number": 1, "state": "maybe"}])
    with pytest.raises(QueueUnavailableError, match="invalid issue"):
        source.read()


def test_github_transport_sends_only_read_requests_and_does_not_echo_failures(monkeypatch):
    requests = []

    def urlopen(request, *, timeout) -> Any:
        requests.append((request, timeout))
        raise OSError("a sensitive upstream response")

    monkeypatch.setattr("steward.work_queue.urllib.request.urlopen", urlopen)
    with pytest.raises(QueueUnavailableError, match="check access") as caught:
        GitHubQueue(REPO, token="private-token").read()
    assert "sensitive" not in str(caught.value)
    request, timeout = requests[0]
    assert request.get_method() == "GET"
    assert request.full_url.startswith("https://api.github.com/repos/owner/repo/issues?")
    assert request.get_header("Authorization") == "Bearer private-token"
    assert 0 < timeout <= 5


def test_api_admits_report_receipt_and_refreshes_recommended_item_state(tmp_path, write_resident):
    import json  # noqa: PLC0415 — local fixture construction

    from conftest import valid_manifest  # noqa: PLC0415
    from steward.store import Store  # noqa: PLC0415

    memory = tmp_path / "memory"
    memory.mkdir()
    manifest = valid_manifest()
    manifest["memory"] = {"kind": "directory", "path": str(memory)}
    path = write_resident(manifest)
    (memory / "queue-review.json").write_text(
        json.dumps(
            {
                "run_id": "review-1",
                "repository": REPO,
                "commit": "a" * 40,
                "recommendations": [
                    {
                        "number": 1,
                        "reason": "Next when inspected.",
                        "evidence": [{"source": "gh issue view 1", "quote": "OPEN"}],
                    }
                ],
            }
        )
    )
    with Store(":memory:") as db:
        db.record_run(
            resident="test-agent",
            agent_id="claude-code:test-agent",
            kind="routine",
            trigger="schedule",
            ref="queue-review",
            run_id="review-1",
            outcome="ok",
        )
        app = create_app(
            ApiConfig(residents_dir=path.parent.parent, token="test", queue_reporter="test-agent"),
            store=db,
            emitter=NullEmitter(),
            queue_source=GitHubQueue(
                REPO, fetch=lambda _: [issue(state="closed").model_dump(mode="json")]
            ),
        )
        with TestClient(app) as client:
            response = client.get("/queue", headers={"Authorization": "Bearer test"})
        assert response.status_code == 200
        result = response.json()
        assert result["report"]["run"]["run_id"] == "review-1"
        assert result["report"]["note"]["recommendations"][0]["reason"] == "Next when inspected."
        assert result["ranked_items"] == [{"number": 1, "state": "closed"}]


@pytest.mark.parametrize(
    "body",
    [
        "## Blocked by\n- #2 and operator approval",
        "## Blocked by\n- #2\n### Release gate\n- operator approval",
    ],
)
def test_prose_and_nested_gates_keep_stale_certification_unknown(body):
    result = project(
        issue(body=body, labels=[{"name": "status:blocked"}]), issue(2, state="closed")
    )
    assert result["issues"][0]["stale_blocked"] is False
    assert result["issues"][0]["unknown_blockers"]
