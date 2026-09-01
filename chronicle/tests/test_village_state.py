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
