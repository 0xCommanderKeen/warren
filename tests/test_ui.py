"""The management console: what steward serves, and the contract between the two.

Three kinds of test live here.

*The shell.* ``/ui`` is the one thing on this server that is not behind the token, and it
has to be: the browser must load the script before there is anything to ask a human for a
token with. So these assert both halves of that — the shell and its assets come back
without an ``Authorization`` header, and every data endpoint the shell then calls does
not.

*The new reads.* ``GET /routines``, ``GET /requests``, and ``GET /requests/{id}`` exist
because the console genuinely could not answer three questions without them: what is
standing work fleet-wide, and what became of the thing I asked for.

*The contract.* ``ui/app.js`` declares every path it will ever call in one ``ROUTES`` map.
This file parses that map and asserts each entry is a real route on the real app, so a
typo in the JavaScript fails Python's test run instead of a panel at two in the morning.
"""

import copy
import json
import re
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from conftest import ResidentWriter, valid_manifest
from steward import events as ev
from steward.api import (
    ApiConfig,
    create_app,
    default_ui_dir,
    latest_run_requests,
)
from steward.deploy import DeployTarget
from steward.nursery import (
    DeclareStage,
    NurseryReport,
    ProvisionStage,
    RegisterStage,
    raise_resident,
)
from steward.runners import MockRunner
from steward.scheduler import STALE_TICK_AFTER_S, SchedulerState, load_scheduled
from steward.store import RequestRecord, Store

TOKEN = "a-shared-secret"
AUTH = {"Authorization": f"Bearer {TOKEN}"}

REPO_ROOT = Path(__file__).resolve().parents[1]
UI_DIR = REPO_ROOT / "ui"

#: The three files the console is. Anything else in ``ui/`` would need a mention here and
#: in the README, which is the point: a console you cannot list is a console nobody audits.
UI_ASSETS = ("index.html", "app.css", "app.js")


@dataclass
class Console:
    """One built app and the collaborators a test needs to look inside it."""

    client: TestClient
    store: Store
    residents_dir: Path


type ConsoleFactory = Callable[..., Console]


@pytest.fixture
def console(tmp_path: Path, write_resident: ResidentWriter) -> Iterator[ConsoleFactory]:
    """Build an app serving the real ``ui/`` directory, against a scratch fleet."""
    built: list[Console] = []

    def _make(
        *,
        manifest: dict[str, Any] | None = None,
        ui_dir: Path | None = UI_DIR,
        residents: bool = True,
        nursery: Any = raise_resident,  # noqa: ANN401 — the pipeline seam, injected
    ) -> Console:
        residents_dir = tmp_path / "residents"
        residents_dir.mkdir(exist_ok=True)
        if residents:
            write_resident(manifest or valid_manifest(), root=residents_dir)
        store = Store(":memory:")
        app = create_app(
            ApiConfig(
                residents_dir=residents_dir,
                token=TOKEN,
                workdir=tmp_path,
                ui_dir=ui_dir,
            ),
            store=store,
            emitter=ev.EventEmitter(url=None, fallback=tmp_path / "events.jsonl"),
            runner_factory=MockRunner,
            nursery=nursery,
        )
        console = Console(
            client=TestClient(app, headers=dict(AUTH)),
            store=store,
            residents_dir=residents_dir,
        )
        built.append(console)
        return console

    yield _make

    for item in built:
        item.client.app.state.runs.shutdown()
        item.store.close()


def anonymous(console: Console) -> TestClient:
    """Return a client holding no token at all — which is what a fresh browser tab is."""
    return TestClient(console.client.app)


# --------------------------------------------------------------------------------------
# the shell
# --------------------------------------------------------------------------------------


def test_the_shell_is_served_without_a_token(console: ConsoleFactory) -> None:
    # The whole reason the mount sits outside the gate: the script has to load before
    # there is anything to ask a person for a token with.
    response = anonymous(console()).get("/ui/")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert "operator console" in response.text


@pytest.mark.parametrize("asset", UI_ASSETS)
def test_every_asset_is_served_without_a_token(console: ConsoleFactory, asset: str) -> None:
    response = anonymous(console()).get(f"/ui/{asset}")
    assert response.status_code == 200
    assert response.text == (UI_DIR / asset).read_text(encoding="utf-8")


def test_the_bare_path_redirects_to_the_shell(console: ConsoleFactory) -> None:
    response = anonymous(console()).get("/ui", follow_redirects=False)
    assert response.status_code in {307, 308}
    assert response.headers["location"].endswith("/ui/")


def test_the_shell_loads_its_own_assets(console: ConsoleFactory) -> None:
    # Offline by construction: a NAS with the internet unplugged has to render this, so
    # both references are relative and both files come off the same mount.
    shell = anonymous(console()).get("/ui/").text
    assert 'href="app.css"' in shell
    assert 'src="app.js"' in shell


def test_the_data_behind_the_shell_still_needs_the_token(console: ConsoleFactory) -> None:
    open_door = anonymous(console())
    for route in ("/residents", "/routines", "/skills", "/jobs", "/approvals", "/requests"):
        assert open_door.get(route).status_code == 401, route


def test_an_unconfigured_console_falls_back_to_the_checkouts_own(
    console: ConsoleFactory,
) -> None:
    built = console(ui_dir=None)
    assert built.client.app.state.ui_dir == UI_DIR
    assert anonymous(built).get("/ui/").status_code == 200


def test_a_directory_without_an_index_is_not_mounted(
    console: ConsoleFactory, tmp_path: Path
) -> None:
    hollow = tmp_path / "hollow"
    hollow.mkdir()
    built = console(ui_dir=hollow)
    assert built.client.app.state.ui_dir is None
    assert anonymous(built).get("/ui/").status_code == 404


def test_the_finder_locates_the_checkouts_console() -> None:
    assert default_ui_dir() == UI_DIR


def test_an_install_with_no_console_finds_none(monkeypatch: pytest.MonkeyPatch) -> None:
    # A wheel that ships no ui/ is a real install, not a broken one: it serves the API and
    # says nothing about a console. Nothing is mounted, and `steward serve` stays quiet.
    monkeypatch.setattr("steward.api.UI_INDEX", "a-file-that-is-not-there.html")
    assert default_ui_dir() is None


# --------------------------------------------------------------------------------------
# the fleet-wide routine ledger
# --------------------------------------------------------------------------------------


def test_the_ledger_carries_every_routine_of_every_resident(console: ConsoleFactory) -> None:
    body = console().client.get("/routines").json()
    assert [row["key"] for row in body["routines"]] == ["test-agent/daily-summary"]
    row = body["routines"][0]
    assert row["resident_name"] == "Testy"
    assert row["accent"] == "#a68a4f"
    assert row["schedule"] == "0 7 * * *"
    assert row["schedule_tz"] == "UTC"
    assert row["enabled"] is True


def test_an_unfired_routine_reports_no_anchor_rather_than_a_last_run(
    console: ConsoleFactory,
) -> None:
    # The distinction the field name exists for: never fired is not the same as fired
    # long ago, and a ledger that blurred the two would let a dead routine look alive.
    row = console().client.get("/routines").json()["routines"][0]
    assert row["anchor"] is None
    assert row["last_request"] is None


def test_an_enabled_routine_promises_a_next_fire_and_a_disabled_one_does_not(
    console: ConsoleFactory,
) -> None:
    data = copy.deepcopy(valid_manifest())
    data["routines"][0]["enabled"] = False
    rows = console(manifest=data).client.get("/routines").json()["routines"]
    assert rows[0]["enabled"] is False
    assert rows[0]["next_fire"] is None

    live = console().client.get("/routines").json()["routines"][0]
    assert live["next_fire"] is not None
    assert live["next_fire"] > "2020"


def test_the_ledger_carries_what_became_of_the_last_asked_for_run(
    console: ConsoleFactory,
) -> None:
    built = console()
    accepted = built.client.post("/residents/test-agent/routines/daily-summary/run")
    assert accepted.status_code == 202
    built.client.app.state.runs.wait(timeout=10.0)

    row = built.client.get("/routines").json()["routines"][0]
    assert row["last_request"]["request_id"] == accepted.json()["request_id"]
    assert row["last_request"]["outcome"] == "ran"


def test_a_fleet_nothing_has_ever_ticked_says_so_rather_than_dead(
    console: ConsoleFactory, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The reason `alive` is a tri-state: a fresh install where no daemon was ever started
    # and a daemon that died an hour ago are different problems with different fixes.
    monkeypatch.setenv("STEWARD_STATE", str(tmp_path / "never.json"))
    scheduler = console().client.get("/routines").json()["scheduler"]

    assert scheduler["alive"] is None
    assert scheduler["last_tick"] is None
    assert scheduler["stale_after_s"] == STALE_TICK_AFTER_S


def test_the_ledger_reads_back_whether_a_scheduler_is_still_ticking(
    console: ConsoleFactory, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state_path = tmp_path / "heartbeat.json"
    monkeypatch.setenv("STEWARD_STATE", str(state_path))
    built = console()
    state = SchedulerState(path=state_path)

    state.record_tick(datetime.now(UTC))
    state.save()
    live = built.client.get("/routines").json()["scheduler"]
    assert live["alive"] is True
    assert live["last_tick"] is not None

    # The daemon stopped. The next fires below are still promised and nothing keeps them.
    state.record_tick(datetime.now(UTC) - timedelta(seconds=STALE_TICK_AFTER_S + 1))
    state.save()
    assert built.client.get("/routines").json()["scheduler"]["alive"] is False


def test_the_ledger_names_manifests_that_did_not_validate(console: ConsoleFactory) -> None:
    built = console()
    broken = built.residents_dir / "broken"
    broken.mkdir()
    (broken / "manifest.yaml").write_text("id: broken\n", encoding="utf-8")
    body = built.client.get("/routines").json()
    assert body["errors"], "a broken manifest must be named, never quietly omitted"


def test_an_empty_tree_is_an_empty_ledger(console: ConsoleFactory) -> None:
    body = console(residents=False).client.get("/routines").json()
    assert body["routines"] == []
    assert body["state_path"]


def test_the_ledger_needs_the_token(console: ConsoleFactory) -> None:
    assert anonymous(console()).get("/routines").status_code == 401


def test_a_retired_residents_routines_are_listed_but_never_firable(
    console: ConsoleFactory,
) -> None:
    """The cross-check between the nursery and the console.

    ``load_scheduled`` leaves retired residents out, so the scheduler will never fire
    these — and a ledger that promised a ``next_fire`` for one would be promising
    something no process anywhere intends to do. They stay *listed*, because "what used
    to run here" is a question this view exists to answer.
    """
    data = copy.deepcopy(valid_manifest())
    data["retired"] = True
    built = console(manifest=data)

    rows = built.client.get("/routines").json()["routines"]
    assert [row["key"] for row in rows] == ["test-agent/daily-summary"]
    assert rows[0]["retired"] is True
    assert rows[0]["enabled"] is True, "the routine's own switch is untouched by retirement"
    assert rows[0]["next_fire"] is None

    # And the refusal the console greys the button out with is the real one.
    refused = built.client.post("/residents/test-agent/routines/daily-summary/run")
    assert refused.status_code == 409
    assert refused.json()["detail"]["error"] == "resident_retired"


def test_a_living_residents_routines_say_they_are_not_retired(console: ConsoleFactory) -> None:
    # The other half: the flag is on every row, so the console never has to infer absence.
    row = console().client.get("/routines").json()["routines"][0]
    assert row["retired"] is False
    assert row["next_fire"] is not None


def test_the_scheduler_and_the_ledger_agree_about_a_retired_resident(
    console: ConsoleFactory,
) -> None:
    # One fact, two readers: whatever load_scheduled would hand the scheduler is exactly
    # what the ledger promises a next fire for.
    data = copy.deepcopy(valid_manifest())
    data["retired"] = True
    built = console(manifest=data)

    scheduled = {item.key for item in load_scheduled(built.residents_dir)}
    promised = {
        row["key"] for row in built.client.get("/routines").json()["routines"] if row["next_fire"]
    }
    assert scheduled == set()
    assert promised == scheduled


# --------------------------------------------------------------------------------------
# the request log: how a 202 is ever confirmed
# --------------------------------------------------------------------------------------


def test_the_request_log_starts_empty(console: ConsoleFactory) -> None:
    assert console().client.get("/requests").json() == {"requests": []}


def test_an_accepted_request_can_be_read_back_by_its_id(console: ConsoleFactory) -> None:
    built = console()
    accepted = built.client.post("/jobs", json={"title": "Research X"}).json()
    record = built.client.get(f"/requests/{accepted['request_id']}").json()
    assert record["path"] == "/jobs"
    assert record["method"] == "POST"
    assert record["outcome"] == "posted"


def test_a_queued_run_becomes_ran_in_the_log_and_not_before(console: ConsoleFactory) -> None:
    # The whole promise the console rests on: 202 says queued, and only this log ever
    # says it happened.
    built = console()
    accepted = built.client.post("/residents/test-agent/routines/daily-summary/run").json()
    built.client.app.state.runs.wait(timeout=10.0)
    record = built.client.get(f"/requests/{accepted['request_id']}").json()
    assert record["outcome"] == "ran"
    assert record["detail"]["routine"] == "test-agent/daily-summary"


def test_the_log_is_newest_first(console: ConsoleFactory) -> None:
    built = console()
    first = built.client.post("/jobs", json={"title": "One"}).json()["request_id"]
    second = built.client.post("/jobs", json={"title": "Two"}).json()["request_id"]
    ids = [row["request_id"] for row in built.client.get("/requests").json()["requests"]]
    assert ids == [second, first]


def test_the_log_window_is_clamped(console: ConsoleFactory) -> None:
    built = console()
    for index in range(4):
        built.client.post("/jobs", json={"title": f"Task {index}"})
    assert len(built.client.get("/requests", params={"limit": 2}).json()["requests"]) == 2
    assert len(built.client.get("/requests", params={"limit": 0}).json()["requests"]) == 1
    assert len(built.client.get("/requests", params={"limit": 9999}).json()["requests"]) == 4


def test_an_unknown_request_id_is_a_404_that_explains_itself(console: ConsoleFactory) -> None:
    response = console().client.get("/requests/nope")
    assert response.status_code == 404
    assert response.json()["detail"]["error"] == "unknown_request"
    assert "refused" in response.json()["detail"]["message"]


def test_a_refused_request_is_never_logged(console: ConsoleFactory) -> None:
    built = console()
    assert built.client.post("/residents/test-agent/routines/nope/run").status_code == 404
    assert built.client.get("/requests").json()["requests"] == []


def test_the_request_log_needs_the_token(console: ConsoleFactory) -> None:
    assert anonymous(console()).get("/requests").status_code == 401


def test_the_newest_request_per_routine_wins() -> None:
    def record(request_id: str, routine: str, outcome: str) -> RequestRecord:
        return RequestRecord(
            request_id=request_id,
            received_at="2026-08-24T10:00:00.000Z",
            method="POST",
            path="/x",
            outcome=outcome,
            detail={"routine": routine},
        )

    indexed = latest_run_requests(
        [
            record("a", "one/tick", "failed"),
            record("b", "one/tick", "ran"),
            record("c", "two/tick", "queued"),
            RequestRecord("d", "…", "POST", "/jobs", "posted", {"task_id": "t"}),
        ]
    )
    assert indexed["one/tick"]["request_id"] == "b"
    assert set(indexed) == {"one/tick", "two/tick"}


# --------------------------------------------------------------------------------------
# what the resident view now carries
# --------------------------------------------------------------------------------------


def test_a_resident_carries_its_voice(console: ConsoleFactory) -> None:
    body = console().client.get("/residents/test-agent").json()
    assert body["voice"] == "Flat, factual, short."


def test_a_soul_with_no_voice_says_so_rather_than_omitting_it(
    console: ConsoleFactory, write_resident: ResidentWriter
) -> None:
    built = console()
    write_resident(
        {**valid_manifest(), "id": "voiceless", "agent_id": "claude-code:voiceless"},
        root=built.residents_dir,
        soul=(
            "---\nagent_id: claude-code:voiceless\nname: Testy\nchar: Monk\n"
            'accent: "#a68a4f"\nrole: test bot\n---\nNo voice section at all.\n'
        ),
    )
    body = built.client.get("/residents/voiceless").json()
    assert body["voice"] is None


def test_a_resident_carries_its_delegation_flags(console: ConsoleFactory) -> None:
    data = copy.deepcopy(valid_manifest())
    data["delegation"] = {"send": True, "to": ["life-agent"]}
    body = console(manifest=data).client.get("/residents/test-agent").json()
    assert body["delegation"] == {"send": True, "to": ["life-agent"], "note": None}


# --------------------------------------------------------------------------------------
# the deploy path, as the pending ledger sees it
#
# The console renders a deploy from two things and invents neither: the 201 body's own
# report, and the request log it then polls. Both are checked here.
# --------------------------------------------------------------------------------------

NEW_RESIDENT: dict[str, Any] = {
    "id": "note-keeper",
    "name": "Quill",
    "char": "Scribe",
    "accent": "#4f7ea6",
    "role": "note bot",
    "charter": {
        "mission": "Keep the village's notes in order.",
        "duties": ["Tidy the notes each evening."],
        "rules": ["Never delete a note without asking."],
        "escalation": "Raise needs_human before anything irreversible.",
    },
}


def canned_pipeline(*, problems: tuple[str, ...] = ()) -> Any:  # noqa: ANN401 — a seam
    """Return a nursery that reaches no host, so a UI test can look at the answer's shape."""

    def pipeline(spec: Any, **kwargs: Any) -> NurseryReport:  # noqa: ANN401
        directory = Path("/srv/steward/residents") / spec.id
        target = DeployTarget(
            resident_id=spec.id,
            host="dxp2800",
            user="Miha",
            path=f"~/docker/steward-{spec.id}",
            container=f"steward-{spec.id}",
            image="steward-resident:latest",
            command=("sleep", "infinity"),
        )
        return NurseryReport(
            resident_id=spec.id,
            declare=DeclareStage(
                resident_id=spec.id,
                manifest_path=directory / "manifest.yaml",
                soul_path=directory / "soul.md",
                written=True,
                note="declared and validated",
            ),
            # Honoured rather than ignored: a stub that provisioned whatever it was asked
            # would let a declare-only request come back describing a container.
            provision=ProvisionStage(
                target=target,
                files=("docker-compose.yaml", ".env"),
                compose="services:\n  note-keeper: {}\n",
                compose_changed=True,
                env_keys=("BURROW_TOKEN", "BURROW_URL"),
                commands=(("ssh", "Miha@dxp2800", "docker compose up -d"),),
                sent=True,
            )
            if kwargs.get("provision")
            else None,
            register=RegisterStage(
                problems=problems,
                fires=() if problems else (("tidy-notes", "2026-08-26T20:00:00+02:00"),),
            ),
        )

    return pipeline


def test_a_deploy_leaves_a_request_the_ledger_can_poll(console: ConsoleFactory) -> None:
    built = console(nursery=canned_pipeline())

    accepted = built.client.post("/residents", json=NEW_RESIDENT | {"deploy": True}).json()
    record = built.client.get(f"/requests/{accepted['request_id']}").json()

    assert record["path"] == "/residents"
    assert record["outcome"] == "deployed"
    assert record["detail"] == {"resident": "note-keeper"}


def test_declaring_without_deploying_says_declared_in_the_same_log(
    console: ConsoleFactory,
) -> None:
    # The two paths are told apart in the log, so a person reading it afterwards can see
    # which requests reached a machine and which only wrote a file.
    built = console(nursery=canned_pipeline())

    accepted = built.client.post("/residents", json=NEW_RESIDENT).json()

    assert built.client.get(f"/requests/{accepted['request_id']}").json()["outcome"] == "declared"


def test_the_answer_carries_the_whole_report_for_the_panel_to_print(
    console: ConsoleFactory,
) -> None:
    body = (
        console(nursery=canned_pipeline())
        .client.post("/residents", json=NEW_RESIDENT | {"deploy": True})
        .json()
    )

    # Everything ui/app.js's declared() panel reads, in one place, so a rename on either
    # side fails here rather than rendering a column of "undefined".
    assert body["provision"]["target"]["container"] == "steward-note-keeper"
    assert body["provision"]["commands"] == ["ssh Miha@dxp2800 docker compose up -d"]
    assert body["provision"]["env_keys"] == ["BURROW_TOKEN", "BURROW_URL"]
    assert body["provision"]["compose"].startswith("services:")
    assert body["register"]["next_fires"] == [
        {"routine": "tidy-notes", "at": "2026-08-26T20:00:00+02:00"}
    ]
    assert body["declare"]["commit"] is None


def test_a_deploy_whose_schedule_check_failed_does_not_read_as_a_success(
    console: ConsoleFactory,
) -> None:
    """The container went up and the check did not pass. The message must say both."""
    built = console(nursery=canned_pipeline(problems=("runner binary not found: claude",)))

    body = built.client.post("/residents", json=NEW_RESIDENT | {"deploy": True}).json()

    assert body["register"]["ok"] is False
    assert "did not pass" in body["message"]
    assert "register.problems" in body["message"]


def test_raising_a_retired_resident_is_refused_by_its_own_code(console: ConsoleFactory) -> None:
    # A code rather than prose, because the form has to tell this refusal apart from
    # "that name is taken by a different declaration" without matching on a sentence.
    data = copy.deepcopy(valid_manifest())
    data["retired"] = True
    built = console(manifest=data)

    response = built.client.post(
        "/residents", json=NEW_RESIDENT | {"id": "test-agent", "deploy": True}
    )

    assert response.status_code == 409
    assert response.json()["detail"]["error"] == "resident_retired"
    assert "retired: false" in response.json()["detail"]["message"]


# --------------------------------------------------------------------------------------
# the contract between ui/app.js and the API
# --------------------------------------------------------------------------------------

ROUTES_BLOCK = re.compile(r"const ROUTES = \{(.*?)\n\};", re.DOTALL)
ROUTE_LITERAL = re.compile(r'"(/[^"]*)"')


def declared_routes() -> list[str]:
    """Pull every path ``ui/app.js`` says it will call out of its one ROUTES map."""
    source = (UI_DIR / "app.js").read_text(encoding="utf-8")
    block = ROUTES_BLOCK.search(source)
    assert block is not None, "ui/app.js must declare its paths in one `const ROUTES = {…}`"
    paths = ROUTE_LITERAL.findall(block.group(1))
    assert paths, "the ROUTES map is empty"
    return paths


def test_every_path_the_console_calls_is_a_real_route(console: ConsoleFactory) -> None:
    served = {getattr(route, "path", None) for route in console().client.app.routes}
    missing = [path for path in declared_routes() if path not in served]
    assert not missing, f"ui/app.js calls paths this API does not serve: {missing}"


def test_the_console_declares_the_paths_it_actually_needs() -> None:
    # Not a duplicate of the route check: this one catches a *deleted* declaration, which
    # would leave a panel silently unable to load rather than calling a 404.
    declared = set(declared_routes())
    for path in (
        "/residents",
        "/residents/{resident_id}",
        "/residents/{resident_id}/journal",
        "/residents/{resident_id}/budget",
        "/residents/{resident_id}/inbox",
        "/residents/{resident_id}/routines/{routine_id}/run",
        "/routines",
        "/skills",
        "/jobs",
        "/approvals",
        "/approvals/{request_id}",
        "/requests/{request_id}",
    ):
        assert path in declared, f"ui/app.js no longer declares {path}"


def test_the_console_fetches_through_exactly_one_door() -> None:
    # The ROUTES contract above is only worth anything if nothing bypasses it.
    source = (UI_DIR / "app.js").read_text(encoding="utf-8")
    assert source.count("fetch(") == 1, "every request must go through the single call() helper"


def test_the_console_pulls_in_nothing_from_the_network() -> None:
    # It runs on a NAS behind a tailnet. A CDN reference would be a blank page there.
    for asset in UI_ASSETS:
        text = (UI_DIR / asset).read_text(encoding="utf-8")
        for scheme in ("http://", "https://", "//cdn", "@import url("):
            offenders = [
                line for line in text.splitlines() if scheme in line and "www.w3.org" not in line
            ]
            assert not offenders, f"{asset} reaches for {scheme}: {offenders}"


def test_the_console_never_claims_an_effect_the_api_did_not_confirm() -> None:
    # The same rule tests/test_api.py holds the API to, held to the thing that renders it.
    source = (UI_DIR / "app.js").read_text(encoding="utf-8")
    assert '"confirmed"' in source
    # Every confirmation must read steward back rather than trusting the 202.
    for name in ("confirmRun", "confirmJob", "confirmApproval", "confirmDeclared"):
        body = source.split(f"function {name}(")[1].split("\n}")[0]
        assert "await call(" in body, f"{name} must read an outcome back from steward"


def test_the_console_only_renders_decisions_the_request_offered() -> None:
    source = (UI_DIR / "app.js").read_text(encoding="utf-8")
    for decision in ("approve", "deny", "edit"):
        assert f'offered.has("{decision}")' in source


def test_the_deploy_switch_is_on_now_that_the_endpoint_is_real() -> None:
    # It was off while POST /residents deployed nothing, because a checkbox that quietly
    # did nothing would be the exact lie this console exists not to tell. The endpoint
    # takes the flag now and runs the nursery behind it, so the control is honest.
    shell = (UI_DIR / "index.html").read_text(encoding="utf-8")
    assert "window.STEWARD_UI = { deploy: true };" in shell


def test_the_form_actually_sends_the_checkbox(console: ConsoleFactory) -> None:
    # The switch is only worth flipping if the box is wired to the body. A checkbox that
    # rendered and was then dropped on the way out would be the same lie in a new place.
    source = (UI_DIR / "app.js").read_text(encoding="utf-8")
    assert "body.deploy = form.elements.deploy.checked" in source
    # And `deploy` is a field the API genuinely accepts, not one it forbids as extra.
    built = console(nursery=canned_pipeline())
    assert built.client.post("/residents", json=NEW_RESIDENT | {"deploy": False}).status_code == 201


def test_the_declared_panel_prints_the_pipelines_own_report() -> None:
    """No invention: every line about the host is a field steward sent back."""
    source = (UI_DIR / "app.js").read_text(encoding="utf-8")
    provision = source.split("function provisionBlock(")[1].split("\n}")[0]
    for field in (
        "provision.files",
        "provision.env_keys",
        "provision.commands",
        "provision.compose",
        "provision.sent",
        "provision.compose_changed",
    ):
        assert field in provision, f"the provision panel does not print {field}"
    register = source.split("function registerBlock(")[1].split("\n}")[0]
    assert "register.problems" in register
    assert "register.next_fires" in register


def test_the_console_greys_out_run_now_for_a_retired_resident() -> None:
    # A control that can only ever fail should look like one before it is pressed — and
    # it must say the same thing the API would have said.
    source = (UI_DIR / "app.js").read_text(encoding="utf-8")
    assert "409 resident_retired" in source, "the button must name the refusal it would get"
    button = source.split("function runButton(")[1].split("\n}\n")[0]
    assert "row.retired" in button
    assert "RETIRED_REFUSAL" in button


def test_the_console_badges_a_retired_resident_in_both_places() -> None:
    source = (UI_DIR / "app.js").read_text(encoding="utf-8")
    listing = source.split("async function viewResidents(")[1].split("\nfunction gauge(")[0]
    detail = source.split("async function viewResident(")[1].split("\nfunction soulPanel(")[0]
    assert 'badge("retired", "fail")' in listing, "the residents list must badge a retired one"
    assert 'badge("retired", "fail")' in detail, "so must the detail header"


def test_the_console_reads_a_closed_inbox_as_closed() -> None:
    # A delegation route flipped to pending or disabled stops pickup, so a console that
    # badged it "inbox on" would show an open door with the post piling up behind it
    # (steward #46). Both places that render an inbox go off the route's *status*.
    source = (UI_DIR / "app.js").read_text(encoding="utf-8")
    badge_fn = source.split("function inboxBadge(")[1].split("\n}")[0]
    assert 'route.status === "active"' in badge_fn
    assert 'badge("inbox closed", "fail")' in badge_fn
    line = source.split("function routeLine(")[1].split("\n}")[0]
    assert "route.accepts" in line, "the panel must say which doors actually take letters"
    assert "nothing will pick them up" in line


def test_the_console_puts_text_in_as_text() -> None:
    # No innerHTML anywhere: a resident's name or an API message cannot become script.
    source = (UI_DIR / "app.js").read_text(encoding="utf-8")
    for hazard in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval("):
        assert hazard not in source, f"ui/app.js uses {hazard}"


def test_the_element_builder_applies_css_custom_and_ordinary_properties() -> None:
    """The style helper honours both halves of CSSStyleDeclaration's interface."""
    source = (UI_DIR / "app.js").read_text(encoding="utf-8")
    element_builder = source.split("function el(")[1].split("\n}\n", 1)[0]

    assert 'name.startsWith("--")' in element_builder
    assert "node.style.setProperty(name, setting)" in element_builder
    assert "node.style[name] = setting" in element_builder


def test_resident_accents_reach_the_elements_whose_css_consumes_them() -> None:
    source = (UI_DIR / "app.js").read_text(encoding="utf-8")
    styles = (UI_DIR / "app.css").read_text(encoding="utf-8")
    listing = source.split("async function viewResidents(")[1].split("\nfunction gauge(")[0]
    detail = source.split("async function viewResident(")[1].split("\nfunction soulPanel(")[0]

    assert '"--accent": resident.soul.accent' in listing
    assert '"--accent": soul.accent' in detail
    assert "a.row:hover { border-left-color: var(--accent, var(--ember)); }" in styles
    assert "border-left: 3px solid var(--accent, var(--ember));" in styles


def test_the_shell_is_valid_enough_to_boot() -> None:
    shell = (UI_DIR / "index.html").read_text(encoding="utf-8")
    for anchor in ('id="main"', 'id="nav"', 'id="gate"', 'id="ledger"', 'id="rail"'):
        assert anchor in shell, f"the shell is missing {anchor}, which app.js looks up by id"
    source = (UI_DIR / "app.js").read_text(encoding="utf-8")
    for hook in re.findall(r'getElementById\("([^"]+)"\)', source):
        assert f'id="{hook}"' in shell, f"app.js looks up #{hook}, which the shell does not have"


def test_the_nav_and_the_router_agree() -> None:
    shell = (UI_DIR / "index.html").read_text(encoding="utf-8")
    source = (UI_DIR / "app.js").read_text(encoding="utf-8")
    views = set(re.findall(r'data-view="([^"]+)"', shell))
    block = source.split("const VIEWS = {")[1].split("};")[0]
    known = set(re.findall(r"^\s*(\w+):", block, re.MULTILINE))
    assert views <= known, f"the rail links to views the router does not know: {views - known}"


def test_the_shell_is_not_a_json_document(console: ConsoleFactory) -> None:
    # A regression guard shaped like a joke: if the mount ever ends up behind the API's
    # catch-all, this is the first thing that would change.
    body = anonymous(console()).get("/ui/").text
    with pytest.raises(json.JSONDecodeError):
        json.loads(body)
