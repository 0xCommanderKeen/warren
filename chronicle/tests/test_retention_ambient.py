"""What a stranger's knocking may cost the villager whose door it is (warren#278).

`chat_message_dropped` is the one event an outsider causes. warren#276 taught the
reducer and rotation that it decides nothing — a knock cannot create a villager,
animate one, or keep a departed one in the village — and left the other half open:
a knock is still an ordinary event for the *budgets*. Rotation keeps the newest
`events_per_agent` events of each agent, so a few hundred knocks would push that
resident's own tools, tasks and sessions out of its history early, and an outsider
would be deciding what an operator can see.

Steward bounds the same storm at the other end by emitting one record per stranger
per door per catch-up window. These tests are the half that has to hold when that
one is outrun: by a scanner rotating sender ids, by a daemon that restarted, or by
a Steward too old to have a limiter at all.
"""

import datetime
import json
import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

import retention
from village_state import project_village


NOW = datetime.datetime(2026, 9, 1, 12, 0, tzinfo=datetime.UTC)


def event(kind, minutes, agent="claude-code:pip", source="claude-code", **payload):
    stamp = NOW + datetime.timedelta(minutes=minutes)
    return {
        "v": 0,
        "ts": stamp.isoformat(timespec="milliseconds").replace("+00:00", "Z"),
        "source": source,
        "agent_id": agent,
        "project": "life",
        "type": kind,
        "payload": payload,
    }


def knock(minutes, sender="87654321", agent="claude-code:pip"):
    return event(
        "chat_message_dropped",
        minutes,
        agent=agent,
        source="steward",
        route="telegram",
        address="@pip_bot",
        reason="not an operator",
        **{"from": sender},
    )


def rotate(events, minutes):
    at = NOW + datetime.timedelta(minutes=minutes)
    result = retention.carry_forward(
        [json.dumps(item, separators=(",", ":")) for item in events],
        int(at.timestamp() * 1000),
        retention.POLICY,
    )
    kept = []
    for line in result.lines:
        value = json.loads(line)
        if "_burrow_internal" not in value:
            kept.append(value)
    return kept


class AmbientBudgetTests(unittest.TestCase):
    def test_a_knock_storm_cannot_push_a_residents_own_history_out(self):
        work = [event("tool_called", index, tool="Read") for index in range(20)]
        storm = [knock(30 + index, sender=str(index)) for index in range(200)]

        kept = rotate(work + storm, 240)
        types = [item["type"] for item in kept]

        self.assertEqual(20, types.count("tool_called"), "the resident's own work")
        self.assertLessEqual(
            types.count("chat_message_dropped"),
            retention.KEEP_AMBIENT_PER_AGENT,
            "an outsider gets a share of the budget, not the whole of it",
        )

    def test_the_newest_knocks_are_the_ones_kept(self):
        """Bounded like everything else here: what survives is the recent end of it."""
        storm = [knock(index, sender=str(index)) for index in range(200)]

        kept = rotate([event("tool_called", 0, tool="Read"), *storm], 240)
        senders = [
            item["payload"]["from"]
            for item in kept
            if item["type"] == "chat_message_dropped"
        ]

        self.assertEqual(
            [str(index) for index in range(200 - len(senders), 200)], senders
        )

    def test_a_knock_still_survives_rotation_at_all(self):
        """A share is not zero: the door being knocked on is a fact worth carrying."""
        kept = rotate([event("tool_called", 0, tool="Read"), knock(1)], 5)

        self.assertEqual(1, [item["type"] for item in kept].count("chat_message_dropped"))

    def test_the_budget_is_per_agent_so_one_door_cannot_starve_another(self):
        loud = [knock(index, sender=str(index), agent="claude-code:pip") for index in range(200)]
        quiet = [knock(1, agent="claude-code:maren")]

        kept = rotate(
            [
                event("tool_called", 0, agent="claude-code:pip", tool="Read"),
                event("tool_called", 0, agent="claude-code:maren", tool="Read"),
                *loud,
                *quiet,
            ],
            240,
        )
        knocks = [item for item in kept if item["type"] == "chat_message_dropped"]

        self.assertEqual(
            1, len([item for item in knocks if item["agent_id"] == "claude-code:maren"])
        )

    def test_what_rotation_kept_is_what_the_projection_reads(self):
        """The two halves have to agree, or rotation invents a village nobody logged."""
        events = [event("tool_called", 0, tool="Read"), knock(1), knock(2)]

        kept = rotate(events, 5)
        at = NOW + datetime.timedelta(minutes=5)
        rotated = project_village(kept, [], at)
        complete = project_village(events, [], at)

        for state in (complete, rotated):
            state.pop("evaluated_at")
            state.pop("cursor")
        self.assertEqual(complete, rotated)


if __name__ == "__main__":
    unittest.main()
