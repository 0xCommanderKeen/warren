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
