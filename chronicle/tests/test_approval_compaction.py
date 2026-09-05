"""A co-retained watchdog knock must never lose its recorded answer (#526)."""

import datetime as dt
import unittest

from retention import MOOD_AUTHORITY_LIMIT, ProjectionFold, VIEWER_LINE_LIMIT
from protocol import validate_event
from tests.test_village_state import NOW, event
from village_state import project_village


class ApprovalCompactionTests(unittest.TestCase):
    def test_incremental_updates_do_not_resurrect_an_answered_watchdog_knock(self):
        knock = event(
            "needs_human",
            minutes=-1000,
            source="steward",
            request_id="restart-hob",
            action="resident_restart_failed",
            message="Hob needs help",
            detail={"resident": "hob"},
            options=["approve", "deny"],
            expires_at=None,
        )
        close = event(
            "needs_human_resolved",
            source="steward",
            minutes=-300,
            request_id="restart-hob",
            action="resident_restart_failed",
            decision="approve",
            decided_by="operator",
        )
        noise = [
            event("tool_called", agent="other", minutes=2, tool="Read")
            for _ in range(VIEWER_LINE_LIMIT + 1)
        ]
        routine = [
            event(
                "routine_started",
                source="steward",
                minutes=3,
                routine="chat",
                run_id="chat-1",
                trigger="chat",
            ),
            event(
                "routine_finished",
                source="steward",
                minutes=4,
                routine="chat",
                run_id="chat-1",
                outcome="ok",
                duration_s=60,
                artifacts=[],
            ),
        ]
        old_run = event(
            "routine_started",
            source="steward",
            minutes=-1001,
            routine="close-of-day",
            run_id="old-run",
            trigger="schedule",
        )
        roots = [
            event("task_started", agent=f"visitor-{i}", minutes=2, prompt="Work")
            for i in range(MOOD_AUTHORITY_LIMIT + 1)
        ]
        events = [old_run, knock, close, *roots, *noise, *routine]
        self.assertTrue(all(validate_event(item) is None for item in events))
        now = NOW + dt.timedelta(minutes=5)
        fold = ProjectionFold()
        initial = fold.replace(events, now)
        self.assertEqual(
            "resolved", project_village(initial, [], now)["approvals"][0]["state"]
        )
        for _ in range(3):
            compacted = fold.extend([event("heartbeat", agent="other", minutes=5)], now)
            snapshot = project_village(compacted, [], now)
            self.assertEqual("resolved", snapshot["approvals"][0]["state"])
            self.assertEqual("approve", snapshot["approvals"][0]["decision"])
            hob = next(
                person for person in snapshot["villagers"] if person["id"] == "agent-1"
            )
            self.assertNotEqual("knocking", hob["state"])


if __name__ == "__main__":
    unittest.main()
