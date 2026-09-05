import datetime as dt
import json
import pathlib
import unittest

import retention
from village_state import ProjectionPolicy, project_village


NOW = dt.datetime(2026, 8, 26, 12, tzinfo=dt.UTC)


def event(kind, agent="agent-1", minutes=0, source="claude-code", **payload):
    return {
        "v": 0,
        "ts": (NOW + dt.timedelta(minutes=minutes))
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z"),
        "source": source,
        "agent_id": agent,
        "project": "life",
        "type": kind,
        "payload": payload,
    }


RESIDENT = {
    "valid": True,
    "manifest_version": 1,
    "match": {"agent_id": "agent-1"},
    "home": 2,
    "meta": {"name": "Burrow", "char": "Monk", "accent": "#123456", "role": "keeper"},
    "routines": [],
}


class VillageProjectionTests(unittest.TestCase):
    def test_discord_events_render_lines_without_changing_villager_state_or_mood(self):
        idle = event("idle", minutes=1)
        baseline = project_village([idle], [RESIDENT], NOW + dt.timedelta(minutes=3))
        cases = {
            "chat_message_posted": (
                {
                    "resident": "pip",
                    "route": "discord:pip",
                    "channel": "household",
                    "length": 42,
                },
                "posted to #household",
            ),
            "chat_post_refused": (
                {
                    "resident": "pip",
                    "route": "discord:pip",
                    "channel": "household",
                    "reason": "not allowed",
                },
                "was refused a post to #household: not allowed",
            ),
            "discord_channel_created": (
                {
                    "resident": "herald",
                    "route": "discord:herald",
                    "channel": "announcements",
                },
                "created #announcements",
            ),
            "discord_thread_created": (
                {
                    "resident": "herald",
                    "route": "discord:herald",
                    "channel": "announcements",
                    "thread": "release-42",
                },
                "created thread release-42 in #announcements",
            ),
            "discord_thread_archived": (
                {
                    "resident": "herald",
                    "route": "discord:herald",
                    "channel": "announcements",
                    "thread": "release-42",
                },
                "archived thread release-42 in #announcements",
            ),
            "discord_message_pinned": (
                {
                    "resident": "herald",
                    "route": "discord:herald",
                    "channel": "announcements",
                    "message": "1234567890",
                },
                "pinned a message in #announcements",
            ),
            "discord_topic_set": (
                {
                    "resident": "herald",
                    "route": "discord:herald",
                    "channel": "announcements",
                },
                "set the topic for #announcements",
            ),
        }
        baseline_villager = baseline["villagers"][0]
        for kind, (payload, line) in cases.items():
            with self.subTest(kind=kind):
                discord_event = event(kind, minutes=2, source="steward", **payload)
                state = project_village(
                    [idle, discord_event], [RESIDENT], NOW + dt.timedelta(minutes=3)
                )
                [villager] = state["villagers"]
                self.assertEqual("resting", villager["state"])
                self.assertEqual(baseline_villager["last_ts"], villager["last_ts"])
                self.assertEqual(baseline_villager["mood"], villager["mood"])
                self.assertEqual(line, villager["last_line"])
                self.assertEqual(kind, villager["history"][-1]["type"])

    def test_declaration_is_identity_not_activity_and_latest_wins_until_retired(self):
        first = event(
            "resident_declared", source="steward", name="Pip", char="Monk",
            accent="#123456", role="helper", summary=None, resident_id="pip",
            uid="0198-uid", home=0,
        )
        renamed = event(
            "resident_declared", minutes=1, source="steward", name="Juniper", char="Hunter",
            accent="#654321", role="helper", summary="renamed", resident_id="pip",
            uid="0198-uid", home=0,
        )
        state = project_village([first, renamed], [], NOW + dt.timedelta(minutes=1))
        [villager] = state["villagers"]
        self.assertEqual(("Juniper", "Hunter", "#654321"), (villager["name"], villager["char"], villager["accent"]))
        self.assertEqual(("resident", 0, "resting", []), (villager["residency"], villager["home"], villager["state"], villager["history"]))
        retired = event("resident_retired", minutes=2, source="steward", resident_id="pip", uid="0198-uid")
        self.assertEqual([], project_village([first, renamed, retired], [], NOW + dt.timedelta(minutes=2))["villagers"])

        stale = event(
            "resident_retired", minutes=2, source="steward", resident_id="old-pip", uid="old-uid"
        )
        [still_present] = project_village(
            [first, renamed, stale], [], NOW + dt.timedelta(minutes=2)
        )["villagers"]
        self.assertEqual("Juniper", still_present["name"])

    def test_invalid_protocol_events_never_enter_the_snapshot(self):
        fixtures = json.loads(
            (
                pathlib.Path(__file__).parent / "fixtures/protocol-v0-validation.json"
            ).read_text()
        )
        invalid = [item["event"] for item in fixtures if not item["valid"]]
        state = project_village(invalid, [], NOW, ProjectionPolicy())
        self.assertEqual([], state["villagers"])
        self.assertEqual(len(invalid), len(state["diagnostics"]))

    def test_failed_tool_is_an_explicit_terminal_activity_state(self):
        state = project_village(
            [event("tool_failed", tool="Bash", error="exit code 1")],
            [],
            NOW + dt.timedelta(minutes=1),
        )
        [villager] = state["villagers"]
        self.assertEqual("failed", villager["state"])
        self.assertEqual("Bash failed", villager["last_line"])
        self.assertIsNone(villager["place"])

    def test_heartbeat_refreshes_liveness_without_replacing_visible_activity(self):
        state = project_village(
            [
                event("task_started", minutes=-120, prompt="make build"),
                event("tool_called", minutes=-115, tool="Bash"),
                event("heartbeat", minutes=-2, tool="Bash"),
            ],
            [],
            NOW,
        )
        [villager] = state["villagers"]
        self.assertEqual("working", villager["state"])
        self.assertEqual("using Bash", villager["last_line"])
        self.assertEqual("workshop", villager["place"])
        self.assertEqual(
            ["task_started", "tool_called"],
            [item["type"] for item in villager["history"]],
        )
        self.assertEqual(
            event("heartbeat", minutes=-2, tool="Bash")["ts"], villager["last_ts"]
        )

    def test_tool_places_are_owned_by_the_python_projection(self):
        expected = {
            "Read": "library",
            "WebSearch": "library",
            "Bash": "workshop",
            "Email": "post-office",
            "Agent": "delegation",
            "Unknown": None,
        }
        for index, (tool, place) in enumerate(expected.items()):
            with self.subTest(tool=tool):
                state = project_village(
                    [event("tool_called", agent=f"agent-{index}", tool=tool)],
                    [],
                    NOW,
                )
                self.assertEqual(place, state["villagers"][0]["place"])

    def test_projects_resident_and_visitor_identity_and_lifecycle(self):
        state = project_village(
            [
                event("task_started", prompt="work"),
                event("tool_called", agent="guest", tool="Read"),
            ],
            [RESIDENT],
            NOW + dt.timedelta(minutes=1),
            ProjectionPolicy(),
            cursor="c1",
            generation=3,
        )
        self.assertEqual(1, state["schema_version"])
        self.assertEqual((3, "c1"), (state["generation"], state["cursor"]))
        residents = {item["id"]: item for item in state["villagers"]}
        self.assertEqual(
            ("resident", "home", 2),
            (
                residents["agent-1"]["residency"],
                residents["agent-1"]["base"],
                residents["agent-1"]["home"],
            ),
        )
        self.assertEqual(
            ("visitor", "lodge", None),
            (
                residents["guest"]["residency"],
                residents["guest"]["base"],
                residents["guest"]["home"],
            ),
        )

    def test_clock_transitions_do_not_require_an_event(self):
        events = [event("tool_called", tool="Read")]
        fresh = project_village(
            events, [], NOW + dt.timedelta(minutes=1), ProjectionPolicy()
        )
        stale = project_village(
            events, [], NOW + dt.timedelta(minutes=31), ProjectionPolicy()
        )
        absent = project_village(
            events, [], NOW + dt.timedelta(hours=13), ProjectionPolicy()
        )
        self.assertEqual("working", fresh["villagers"][0]["state"])
        self.assertEqual("stale", stale["villagers"][0]["state"])
        self.assertEqual([], absent["villagers"])

    def test_ambiguous_project_or_retained_child_lineage_cannot_grant_a_home(self):
        project_resident = {
            **RESIDENT,
            "match": {"project": "life"},
            "file": "one.json",
        }
        duplicate = {**project_resident, "file": "two.json", "home": 3}
        ambiguous = project_village(
            [event("tool_called", tool="Read")],
            [project_resident, duplicate],
            NOW,
            ProjectionPolicy(),
        )
        child = project_village(
            [
                event("task_started", parent_agent_id="parent", prompt="child"),
                event("tool_called", minutes=1, tool="Read"),
            ],
            [project_resident],
            NOW + dt.timedelta(minutes=1),
            ProjectionPolicy(),
        )
        self.assertEqual("visitor", ambiguous["villagers"][0]["residency"])
        self.assertEqual("visitor", child["villagers"][0]["residency"])

    def test_projects_task_approval_journal_routine_mood_and_diagnostics(self):
        events = [
            event(
                "task_posted",
                source="steward",
                task_id="t1",
                title="Fix it",
                posted_by="me",
                required_skills=[],
            ),
            event(
                "task_claimed",
                agent="agent-1",
                minutes=1,
                source="steward",
                task_id="t1",
                title="Fix it",
                claimant="agent-1",
            ),
            event(
                "needs_human",
                minutes=2,
                message="Ship?",
                request_id="r1",
                action="ship",
                detail="prod",
                options=["approve", "deny"],
            ),
            event(
                "journal_written",
                minutes=3,
                source="steward",
                day="2026-08-26",
                routine="daily",
                path="journals/2026-08-26.md",
            ),
            event(
                "routine_started",
                minutes=4,
                source="steward",
                routine="daily",
                run_id="run-1",
                trigger="schedule",
            ),
            event("tool_failed", minutes=5, tool="Bash", error="boom"),
            {"bad": True},
        ]
        state = project_village(
            events, [RESIDENT], NOW + dt.timedelta(minutes=6), ProjectionPolicy()
        )
        self.assertEqual("claimed", state["tasks"][0]["state"])
        self.assertEqual("pending", state["approvals"][0]["state"])
        self.assertEqual("2026-08-26", state["journals"][0]["day"])
        self.assertEqual("running", state["routines"][0]["state"])
        self.assertEqual(
            1, state["villagers"][0]["mood"]["signals"]["failure"]["failures"]
        )
        self.assertEqual("malformed_event", state["diagnostics"][0]["kind"])

    def test_steward_lifecycle_facts_reach_the_villager_as_readable_activity(self):
        """warren#276: the four Steward types the gate used to refuse."""
        delegated = project_village(
            [
                event("task_started", prompt="work"),
                event(
                    "task_delegated",
                    minutes=1,
                    source="steward",
                    task_id="t2",
                    title="Draft the letter",
                    to="agent-2",
                    route="inbox",
                    parent_task_id=None,
                    depth=1,
                    **{"from": "agent-1"},
                ),
            ],
            [],
            NOW + dt.timedelta(minutes=2),
        )
        [villager] = delegated["villagers"]
        self.assertEqual("handed “Draft the letter” to agent-2", villager["last_line"])
        self.assertEqual(
            ["task_started", "task_delegated"],
            [item["type"] for item in villager["history"]],
        )

        reported = project_village(
            [
                event(
                    "task_session_finished",
                    source="steward",
                    task_id="t1",
                    title="Research X",
                    claimant="agent-1",
                    run_id="run-7",
                    outcome="ok",
                    artifacts=[],
                    duration_s=42.5,
                    reason="lease_lost",
                )
            ],
            [],
            NOW + dt.timedelta(minutes=1),
        )
        self.assertEqual(
            "reported back on “Research X” after losing the claim",
            reported["villagers"][0]["last_line"],
        )

        restarted = project_village(
            [
                event(
                    "resident_restarted",
                    source="steward",
                    reason="container was not running",
                    attempt=2,
                )
            ],
            [],
            NOW + dt.timedelta(minutes=1),
        )
        self.assertEqual(
            "was restarted (attempt 2): container was not running",
            restarted["villagers"][0]["last_line"],
        )

    def test_a_delegated_job_opens_its_own_board_row(self):
        """warren#277: a handoff is how Steward opens a row for a named resident."""
        state = project_village(
            [
                event(
                    "task_delegated",
                    source="steward",
                    task_id="t2",
                    title="Draft the letter",
                    to="agent-2",
                    route="inbox",
                    parent_task_id=None,
                    depth=1,
                    **{"from": "agent-1"},
                )
            ],
            [],
            NOW + dt.timedelta(minutes=1),
        )
        [task] = state["tasks"]
        self.assertEqual(
            {
                "id": "t2",
                "title": "Draft the letter",
                "state": "open",
                "required_skills": [],
                "posted_by": "agent-1",
                "assignee": "agent-2",
                "claimant": None,
                "updated_at": "2026-08-26T12:00:00.000Z",
            },
            task,
        )

    def test_the_receiver_moves_the_row_a_delegation_opened(self):
        """The claim used to be dropped with its row, taking the close with it."""
        state = project_village(
            [
                event(
                    "task_delegated",
                    source="steward",
                    task_id="t2",
                    title="Draft the letter",
                    to="agent-2",
                    route="inbox",
                    parent_task_id=None,
                    depth=1,
                    **{"from": "agent-1"},
                ),
                event(
                    "task_claimed",
                    agent="agent-2",
                    minutes=1,
                    source="steward",
                    task_id="t2",
                    title="Draft the letter",
                    claimant="agent-2",
                ),
                event(
                    "task_done",
                    agent="agent-2",
                    minutes=2,
                    source="steward",
                    task_id="t2",
                    title="Draft the letter",
                    claimant="agent-2",
                    artifacts=["letter.md"],
                ),
            ],
            [],
            NOW + dt.timedelta(minutes=3),
        )
        [task] = state["tasks"]
        self.assertEqual("done", task["state"])
        self.assertEqual("agent-2", task["claimant"])
        # The addressee outlives the claim: it is who the work was *for*, not who took it.
        self.assertEqual("agent-2", task["assignee"])
        self.assertEqual("agent-1", task["posted_by"])

    def test_a_posted_job_is_addressed_to_nobody(self):
        """The open board is the absence of an addressee, not an empty string."""
        state = project_village(
            [
                event(
                    "task_posted",
                    source="steward",
                    task_id="t1",
                    title="Fix it",
                    posted_by="me",
                    required_skills=["python"],
                )
            ],
            [],
            NOW + dt.timedelta(minutes=1),
        )
        [task] = state["tasks"]
        self.assertIsNone(task["assignee"])
        self.assertEqual(["python"], task["required_skills"])

    def test_a_repeat_origin_refreshes_the_row_it_already_opened(self):
        """warren#282: one job is one card, whatever the log replayed.

        A second post for a live row restates where the job came from — never that
        nobody has taken it. It supplies the canonical title, skills and poster; the
        claim between the two posts still owns the state, the claimant and the clock.
        """
        state = project_village(
            [
                event(
                    "task_posted",
                    source="steward",
                    task_id="t1",
                    title="Write the letter",
                    posted_by="steward:api",
                    required_skills=["python"],
                ),
                event(
                    "task_claimed",
                    agent="agent-2",
                    minutes=1,
                    source="steward",
                    task_id="t1",
                    title="Write the letter",
                    claimant="agent-2",
                ),
                event(
                    "task_posted",
                    minutes=2,
                    source="steward",
                    task_id="t1",
                    title="Write the letter, again",
                    posted_by="steward:api",
                    required_skills=["rust"],
                ),
            ],
            [],
            NOW + dt.timedelta(minutes=3),
        )
        self.assertEqual(
            [
                {
                    "id": "t1",
                    "title": "Write the letter, again",
                    "state": "claimed",
                    "required_skills": ["rust"],
                    "posted_by": "steward:api",
                    "assignee": None,
                    "claimant": "agent-2",
                    "updated_at": "2026-08-26T12:01:00.000Z",
                }
            ],
            state["tasks"],
        )

    def test_a_handoff_of_a_claimed_job_reopens_nothing(self):
        """The second origin arriving through the *other* door changes nothing here.

        warren#277 made a repeat origin reachable through two different event types.
        A handoff restates the row as addressed work — an addressee and no required
        skills — and leaves the claim standing.
        """
        state = project_village(
            [
                event(
                    "task_posted",
                    source="steward",
                    task_id="t1",
                    title="Write the letter",
                    posted_by="steward:api",
                    required_skills=["python"],
                ),
                event(
                    "task_claimed",
                    agent="agent-2",
                    minutes=1,
                    source="steward",
                    task_id="t1",
                    title="Write the letter",
                    claimant="agent-2",
                ),
                event(
                    "task_delegated",
                    minutes=2,
                    source="steward",
                    task_id="t1",
                    title="Write the letter",
                    to="agent-3",
                    route="inbox",
                    parent_task_id=None,
                    depth=1,
                    **{"from": "agent-1"},
                ),
            ],
            [],
            NOW + dt.timedelta(minutes=3),
        )
        self.assertEqual(
            [
                {
                    "id": "t1",
                    "title": "Write the letter",
                    "state": "claimed",
                    "required_skills": [],
                    "posted_by": "agent-1",
                    "assignee": "agent-3",
                    "claimant": "agent-2",
                    "updated_at": "2026-08-26T12:01:00.000Z",
                }
            ],
            state["tasks"],
        )

    def test_a_repeat_post_of_an_untaken_job_is_its_new_posted_age(self):
        """An open row's clock is its posted age, so the newest post supplies it.

        Once something moves the row the clock belongs to that transition instead —
        a replayed post is not news about work already under way.
        """
        state = project_village(
            [
                event(
                    "task_posted",
                    source="steward",
                    task_id="t1",
                    title="Write the letter",
                    posted_by="steward:api",
                    required_skills=["python"],
                ),
                event(
                    "task_posted",
                    minutes=2,
                    source="steward",
                    task_id="t1",
                    title="Write the letter, again",
                    posted_by="steward:api",
                    required_skills=["rust"],
                ),
            ],
            [],
            NOW + dt.timedelta(minutes=3),
        )
        [task] = state["tasks"]
        self.assertEqual("open", task["state"])
        self.assertEqual("2026-08-26T12:02:00.000Z", task["updated_at"])

    def test_a_transition_is_held_until_the_event_that_opens_its_row(self):
        """Rotation can hand the reducer a claim before the post it belongs to.

        Keeping the newest origin and the newest transition is enough to rebuild the
        row, but they arrive in log order, and a repeat origin is the later of the
        two. A claim read before its row exists is held, not thrown away — otherwise
        rotation would turn a claimed job back into an open one.
        """
        state = project_village(
            [
                event(
                    "task_claimed",
                    agent="agent-2",
                    minutes=1,
                    source="steward",
                    task_id="t1",
                    title="Write the letter",
                    claimant="agent-2",
                ),
                event(
                    "task_posted",
                    minutes=2,
                    source="steward",
                    task_id="t1",
                    title="Write the letter, again",
                    posted_by="steward:api",
                    required_skills=["rust"],
                ),
            ],
            [],
            NOW + dt.timedelta(minutes=3),
        )
        [task] = state["tasks"]
        self.assertEqual("claimed", task["state"])
        self.assertEqual("agent-2", task["claimant"])
        self.assertEqual("2026-08-26T12:01:00.000Z", task["updated_at"])

    def test_a_transition_whose_row_never_opens_stays_dropped(self):
        """Holding is not inventing: with no origin anywhere, there is no job."""
        state = project_village(
            [
                event(
                    "task_claimed",
                    agent="agent-2",
                    minutes=1,
                    source="steward",
                    task_id="t1",
                    title="Write the letter",
                    claimant="agent-2",
                ),
                event(
                    "task_done",
                    agent="agent-2",
                    minutes=2,
                    source="steward",
                    task_id="t1",
                    title="Write the letter",
                    claimant="agent-2",
                    artifacts=[],
                ),
            ],
            [],
            NOW + dt.timedelta(minutes=3),
        )
        self.assertEqual([], state["tasks"])

    def test_a_late_session_report_never_closes_the_board_row(self):
        """The lease sweep owns the task; this fact describes only the late run."""
        state = project_village(
            [
                event(
                    "task_posted",
                    source="steward",
                    task_id="t1",
                    title="Fix it",
                    posted_by="me",
                    required_skills=[],
                ),
                event(
                    "task_claimed",
                    minutes=1,
                    source="steward",
                    task_id="t1",
                    title="Fix it",
                    claimant="agent-1",
                ),
                event(
                    "task_session_finished",
                    minutes=2,
                    source="steward",
                    task_id="t1",
                    title="Fix it",
                    claimant="agent-1",
                    run_id="run-7",
                    outcome="ok",
                    artifacts=[],
                    duration_s=1.0,
                    reason="lease_lost",
                ),
            ],
            [],
            NOW + dt.timedelta(minutes=3),
        )
        [task] = state["tasks"]
        self.assertEqual("claimed", task["state"])
        self.assertEqual("agent-1", task["claimant"])

    def test_a_dropped_chat_message_never_creates_or_animates_a_villager(self):
        """A stranger knocking is not evidence the resident is awake or at work."""
        drop = event(
            "chat_message_dropped",
            minutes=5,
            source="steward",
            route="telegram",
            address="@life_agent_bot",
            reason="not an operator",
            **{"from": "87654321"},
        )
        alone = project_village([drop], [], NOW + dt.timedelta(minutes=6))
        self.assertEqual([], alone["villagers"])

        idle = event("idle", minutes=1)
        resting = project_village([idle, drop], [], NOW + dt.timedelta(minutes=6))
        [villager] = resting["villagers"]
        self.assertEqual("resting", villager["state"])
        self.assertEqual("finished, resting", villager["last_line"])
        # Neither the villager's clock nor its mood is aged by somebody else's knock.
        self.assertEqual(idle["ts"], villager["last_ts"])
        self.assertEqual(idle["ts"], villager["mood"]["anchor"])
        # It is still in the history, which is the whole point of recording it.
        self.assertEqual(
            ["idle", "chat_message_dropped"],
            [item["type"] for item in villager["history"]],
        )

    def test_a_dropped_chat_message_is_a_bounded_diagnostic(self):
        """The door, the knocker and the rule that refused them — nothing else."""
        state = project_village(
            [
                event(
                    "chat_message_dropped",
                    source="steward",
                    route="telegram",
                    address="@life_agent_bot",
                    reason="not an operator",
                    **{"from": "87654321"},
                )
            ],
            [],
            NOW + dt.timedelta(minutes=1),
        )
        [diagnostic] = state["diagnostics"]
        self.assertEqual(
            {
                "kind": "chat_message_dropped",
                "agent_id": "agent-1",
                "project": "life",
                "route": "telegram",
                "address": "@life_agent_bot",
                "from": "87654321",
                "reason": "not an operator",
                "suppressed": 0,
                "ts": "2026-08-26T12:00:00.000Z",
            },
            diagnostic,
        )

    def test_a_drop_carries_the_knocks_it_stands_for(self):
        """Steward emits one record per stranger per window and counts the rest."""
        state = project_village(
            [
                event(
                    "chat_message_dropped",
                    source="steward",
                    route="telegram",
                    address="@life_agent_bot",
                    reason="not an operator",
                    suppressed=199,
                    **{"from": "87654321"},
                )
            ],
            [],
            NOW + dt.timedelta(minutes=1),
        )
        [diagnostic] = state["diagnostics"]
        self.assertEqual(199, diagnostic["suppressed"])

    def test_a_knock_storm_cannot_evict_the_projections_own_complaints(self):
        """An outsider gets a share of the channel, never the whole of it (warren#278)."""
        policy = ProjectionPolicy(diagnostics=10, ambient_diagnostics=3)
        complaints = [
            event(
                "needs_human_resolved",
                agent="agent-1",
                minutes=index,
                source="steward",
                request_id=f"req-{index}",
                decided_by="miha",
                action="deploy",
                decision="approve",
            )
            for index in range(4)
        ]
        knocks = [
            event(
                "chat_message_dropped",
                agent="agent-1",
                minutes=10 + index,
                source="steward",
                route="telegram",
                address="@life_agent_bot",
                reason="not an operator",
                **{"from": "87654321"},
            )
            for index in range(50)
        ]
        state = project_village(
            complaints + knocks, [], NOW + dt.timedelta(minutes=61), policy
        )
        kinds = [item["kind"] for item in state["diagnostics"]]
        # Every knock arrived after every complaint, so newest-wins alone would have left
        # nothing but knocks. The knocks still take the room the complaints did not want:
        # the outsider's number is a floor, not a ceiling, or a full channel of ten would
        # be holding seven.
        self.assertEqual(4, kinds.count("orphan_approval_resolution"))
        self.assertEqual(policy.diagnostics, len(kinds))
        # Append order survives the split: the channel still reads oldest to newest.
        self.assertEqual(
            ["orphan_approval_resolution"] * 4 + ["chat_message_dropped"] * 6, kinds
        )

    def test_a_contested_channel_gives_an_outsider_exactly_their_floor(self):
        """When everybody wants the channel, the fleet is served out of all but the share."""
        policy = ProjectionPolicy(diagnostics=10, ambient_diagnostics=3)
        complaints = [
            event(
                "needs_human_resolved",
                agent="agent-1",
                minutes=index,
                source="steward",
                request_id=f"req-{index}",
                decided_by="miha",
                action="deploy",
                decision="approve",
            )
            for index in range(20)
        ]
        knocks = [
            event(
                "chat_message_dropped",
                agent="agent-1",
                minutes=30 + index,
                source="steward",
                route="telegram",
                address="@life_agent_bot",
                reason="not an operator",
                **{"from": "87654321"},
            )
            for index in range(50)
        ]
        state = project_village(
            complaints + knocks, [], NOW + dt.timedelta(minutes=90), policy
        )
        kinds = [item["kind"] for item in state["diagnostics"]]

        self.assertEqual(3, kinds.count("chat_message_dropped"))
        self.assertEqual(7, kinds.count("orphan_approval_resolution"))

    def test_a_knock_storm_cannot_push_a_villager_off_its_own_card(self):
        """The same share, on the history the village actually draws."""
        policy = ProjectionPolicy(events_per_villager=10, ambient_events_per_villager=3)
        work = [
            event("tool_called", agent="agent-1", minutes=index, tool="Read")
            for index in range(20)
        ]
        knocks = [
            event(
                "chat_message_dropped",
                agent="agent-1",
                minutes=30 + index,
                source="steward",
                route="telegram",
                address="@life_agent_bot",
                reason="not an operator",
                **{"from": "87654321"},
            )
            for index in range(50)
        ]
        state = project_village(work + knocks, [], NOW + dt.timedelta(minutes=90), policy)
        [villager] = state["villagers"]
        types = [item["type"] for item in villager["history"]]

        self.assertEqual(7, types.count("tool_called"))
        self.assertEqual(3, types.count("chat_message_dropped"))

    def test_history_share_keeps_append_order_across_channel_shapes(self):
        # O = fleet work, A = ambient knock. Literal indices describe the newest
        # records entitled to each share, including unused ambient capacity.
        cases = [
            ("", 4, 1, []),
            ("OAO", 4, 1, [0, 1, 2]),
            ("AAAAAA", 4, 1, [2, 3, 4, 5]),
            ("OOOOOO", 4, 1, [2, 3, 4, 5]),
            ("OAOAOAOA", 4, 1, [2, 4, 6, 7]),
            ("OAAAAAA", 4, 1, [0, 4, 5, 6]),
            ("OAOAOAOA", 4, 0, [0, 2, 4, 6]),
            ("OAOAOAOA", 2, 4, [5, 7]),
            ("OAOA", 0, 1, []),
        ]
        for shape, capacity, floor, expected in cases:
            with self.subTest(shape=shape, capacity=capacity, floor=floor):
                events = [
                    event(
                        "tool_called" if kind == "O" else "chat_message_dropped",
                        # Reverse event times to distinguish append order from time.
                        minutes=-index,
                        source="steward" if kind == "A" else "claude-code",
                        tool="Read",
                        route="telegram",
                        address="@life_agent_bot",
                        reason="not an operator",
                        **{"from": "87654321"},
                    )
                    for index, kind in enumerate(shape)
                ]
                state = project_village(
                    [event(
                        "resident_declared", source="steward", name="Burrow",
                        char="Monk", accent="#123456", role="keeper",
                        summary=None, resident_id="burrow", uid="0198-uid", home=2,
                    )] + events,
                    [RESIDENT], NOW,
                    ProjectionPolicy(
                        events_per_villager=capacity,
                        ambient_events_per_villager=floor,
                    ),
                )
                [villager] = state["villagers"]
                self.assertEqual(
                    [events[index]["ts"] for index in expected],
                    [item["ts"] for item in villager["history"]],
                )

    def test_snapshot_is_bounded_deterministic_and_json_serializable(self):
        policy = ProjectionPolicy(events_per_villager=2, tasks=2, diagnostics=2)
        events = [
            event("tool_called", agent=f"a-{index}", minutes=index, tool="Read")
            for index in range(6)
        ]
        first = project_village(events, [], NOW + dt.timedelta(minutes=7), policy)
        second = project_village(events, [], NOW + dt.timedelta(minutes=7), policy)
        self.assertEqual(first, second)
        self.assertTrue(
            all(len(villager["history"]) <= 2 for villager in first["villagers"])
        )
        self.assertLessEqual(len(first["diagnostics"]), 2)
        json.dumps(first, allow_nan=False)

    def test_a_second_conflicting_resolution_cannot_rewrite_the_first_decision(self):
        """warren#266: the first close owns the answer, later conflicts are complaints.

        `retention._approval_keep_indexes` carries exactly one close forward, so a
        projection that let the newest close win rendered `deny` from the raw tail and
        `approve` from the rotated log — the same evidence, two decisions.
        """
        events = [
            event(
                "needs_human",
                minutes=0,
                message="Ship?",
                request_id="r1",
                action="ship",
                detail={"env": "prod"},
                options=["approve", "deny"],
            ),
            event(
                "needs_human_resolved",
                minutes=1,
                source="steward",
                request_id="r1",
                action="ship",
                decision="deny",
                decided_by="miha",
            ),
            event(
                "needs_human_resolved",
                minutes=2,
                source="steward",
                request_id="r1",
                action="ship",
                decision="approve",
                decided_by="miha",
            ),
        ]
        state = project_village(
            events, [RESIDENT], NOW + dt.timedelta(minutes=3), ProjectionPolicy()
        )
        [approval] = state["approvals"]
        self.assertEqual("resolved", approval["state"])
        self.assertEqual("deny", approval["decision"])
        self.assertEqual(
            (NOW + dt.timedelta(minutes=1))
            .isoformat(timespec="milliseconds")
            .replace("+00:00", "Z"),
            approval["resolved_at"],
        )
        self.assertEqual(
            ["conflicting_approval_resolution"],
            [item["kind"] for item in state["diagnostics"]],
        )
        self.assertEqual("r1", state["diagnostics"][0]["request_id"])

    def test_a_conflicting_resolution_storm_leaves_one_deduplicated_complaint(self):
        """The channel is bounded, so one contested request is one complaint."""
        events = [
            event(
                "needs_human",
                minutes=0,
                message="Ship?",
                request_id="r1",
                action="ship",
                detail={"env": "prod"},
                options=["approve", "deny"],
            ),
            event(
                "needs_human_resolved",
                minutes=1,
                source="steward",
                request_id="r1",
                action="ship",
                decision="deny",
                decided_by="miha",
            ),
        ] + [
            event(
                "needs_human_resolved",
                minutes=2 + index,
                source="steward",
                request_id="r1",
                action="ship",
                decision="approve",
                decided_by="miha",
            )
            for index in range(20)
        ]
        state = project_village(
            events, [RESIDENT], NOW + dt.timedelta(minutes=30), ProjectionPolicy()
        )
        self.assertEqual("deny", state["approvals"][0]["decision"])
        self.assertEqual(
            ["conflicting_approval_resolution"],
            [item["kind"] for item in state["diagnostics"]],
        )

    def test_an_exact_close_replay_is_idempotent_and_silent(self):
        """Steward replays a recorded answer; a replay is not a disagreement."""
        events = [
            event(
                "needs_human",
                minutes=0,
                message="Ship?",
                request_id="r1",
                action="ship",
                detail={"env": "prod"},
                options=["approve", "deny"],
            ),
            event(
                "needs_human_resolved",
                minutes=1,
                source="steward",
                request_id="r1",
                action="ship",
                decision="approve",
                decided_by="miha",
            ),
            event(
                "needs_human_resolved",
                minutes=5,
                source="steward",
                request_id="r1",
                action="ship",
                decision="approve",
                decided_by="miha",
            ),
        ]
        state = project_village(
            events, [RESIDENT], NOW + dt.timedelta(minutes=6), ProjectionPolicy()
        )
        [approval] = state["approvals"]
        self.assertEqual("approve", approval["decision"])
        self.assertEqual(
            (NOW + dt.timedelta(minutes=1))
            .isoformat(timespec="milliseconds")
            .replace("+00:00", "Z"),
            approval["resolved_at"],
        )
        self.assertEqual([], state["diagnostics"])

    def test_rotation_cannot_change_a_contested_decision(self):
        """The vector the issue asks for: one log, projected on both sides of rotation.

        The filler is what makes this a real vector rather than a tautology — rotation
        only reaches the second close once ordinary activity has pushed the log past its
        per-villager bound, and it keeps the first. Before warren#266 the raw tail read
        `approve` and the rotated log read `deny` from these same bytes.
        """
        events = [
            event(
                "needs_human",
                minutes=0,
                message="Ship?",
                request_id="r1",
                action="ship",
                detail={"env": "prod"},
                options=["approve", "deny"],
            ),
            event(
                "needs_human_resolved",
                minutes=1,
                source="steward",
                request_id="r1",
                action="ship",
                decision="deny",
                decided_by="miha",
            ),
            event(
                "needs_human_resolved",
                minutes=2,
                source="steward",
                request_id="r1",
                action="ship",
                decision="approve",
                decided_by="miha",
            ),
        ] + [
            event("tool_called", minutes=3 + index, tool="Read") for index in range(80)
        ]
        now = NOW + dt.timedelta(minutes=200)
        retained = retention.carry_forward(
            [json.dumps(item) for item in events],
            int(now.timestamp() * 1000),
            retention.POLICY,
        )
        retained_events = [
            value
            for value in (json.loads(line) for line in retained.lines)
            if "_burrow_internal" not in value
        ]
        self.assertEqual(
            ["deny"],
            [
                item["payload"]["decision"]
                for item in retained_events
                if item["type"] == "needs_human_resolved"
            ],
            "rotation must have dropped the second close for this vector to mean anything",
        )
        complete = project_village(events, [RESIDENT], now, ProjectionPolicy())
        rotated = project_village(retained_events, [RESIDENT], now, ProjectionPolicy())
        self.assertEqual("deny", complete["approvals"][0]["decision"])
        self.assertEqual(complete["approvals"], rotated["approvals"])

    def test_rotation_is_observationally_invisible(self):
        events = [
            event("task_started", prompt="work"),
            event("tool_failed", minutes=1, tool="Bash"),
        ]
        lines = [json.dumps(item) for item in events]
        retained = retention.carry_forward(
            lines,
            int((NOW + dt.timedelta(minutes=2)).timestamp() * 1000),
            retention.POLICY,
        )
        retained_events = []
        for line in retained.lines:
            value = json.loads(line)
            if "_burrow_internal" not in value:
                retained_events.append(value)
        complete = project_village(
            events, [], NOW + dt.timedelta(minutes=2), ProjectionPolicy()
        )
        rotated = project_village(
            retained_events, [], NOW + dt.timedelta(minutes=2), ProjectionPolicy()
        )
        for state in (complete, rotated):
            state.pop("evaluated_at")
            state.pop("cursor")
        self.assertEqual(complete, rotated)


if __name__ == "__main__":
    unittest.main()
