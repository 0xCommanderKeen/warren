#!/usr/bin/env python3
"""Drive a burrow log from a scripted scenario, to exercise the viewer without a
live fleet.

    python3 fixtures/demo.py                       # the "village" scenario, looping
    python3 fixtures/demo.py library --once        # the library trip, once through
    python3 fixtures/demo.py --list
    python3 fixtures/demo.py states --out /tmp/x.jsonl --speed 2

Then serve the village against the same log:

    BURROW_EVENTS=/tmp/burrow-demo.jsonl python3 serve.py

This is a driver, not a simulator. It writes real protocol events (v0, see
docs/protocol.md) into a real log and lets the real projection decide what the
village looks like — the same path a live fleet takes. Nothing here reaches into
the viewer to place a sprite or light a window, because a debug tool that fakes
village state can show a village that never happened, and then the one rule only
holds in production. What is faked is the fleet; everything downstream of the log
stays honest.

Companion to play.py, which replays a recorded .jsonl. This one scripts the
timeline in code, so a scenario can loop, hold, and reach states that are awkward
to record — a villager going stale takes 30 real minutes.
"""

import argparse
import datetime
import json
import sys
import time

MINUTE = 60

# Every villager the scenarios below can cast. agent_id is what the projection
# keys on, so these stay stable across runs and keep their houses.
AGENTS = {
    "scholar": {"id": "demo:scholar", "project": "atlas", "cwd": "/tmp/atlas"},
    "scribe": {"id": "demo:scribe", "project": "almanac", "cwd": "/tmp/almanac"},
    "mason": {"id": "demo:mason", "project": "burrow", "cwd": "/tmp/burrow"},
    "courier": {"id": "demo:courier", "project": "mail", "cwd": "/tmp/mail"},
    "forester": {"id": "demo:forester", "project": "life", "cwd": "/tmp/life"},
    "hermit": {"id": "demo:hermit", "project": "attic", "cwd": "/tmp/attic"},
}


# ————— beats: (seconds from scenario start, agent, type, payload, backdate) —————


def _beat(t, agent, type_, payload=None, backdate=0):
    return {
        "t": t,
        "agent": agent,
        "type": type_,
        "payload": payload or {},
        "backdate": backdate,
    }


def started(t, agent, prompt):
    return _beat(t, agent, "task_started", {"prompt": prompt})


def tool(t, agent, name, detail=None):
    payload = {"tool": name}
    if detail:
        payload["detail"] = detail
    return _beat(t, agent, "tool_called", payload)


def made(t, agent, artifact):
    return _beat(t, agent, "artifact_produced", {"artifact": artifact})


def knock(t, agent, message):
    return _beat(t, agent, "needs_human", {"message": message})


def idle(t, agent):
    return _beat(t, agent, "idle")


def gone(t, agent):
    return _beat(t, agent, "session_ended")


def stale(t, agent, name, detail=None, minutes_ago=45):
    """Written now, stamped in the past. The projection calls a villager stale
    after 30 minutes of silence, which is a long time to sit and watch for."""
    beat = tool(t, agent, name, detail)
    beat["backdate"] = minutes_ago * MINUTE
    return beat


SCENARIOS = {
    # The protocol rule this exists to show: research happens at the library,
    # everything else happens at home, and only the latest event moves anybody.
    "library": {
        "blurb": "one villager walks to the library and back",
        "loop_every": 60,
        "script": [
            started(0, "scholar", "how is the Lisbon tram network numbered?"),
            tool(4, "scholar", "Read", "notes/lisbon.md"),  # at home
            tool(10, "scholar", "WebSearch", "lisbon tram numbering"),  # → library
            tool(20, "scholar", "WebFetch", "carris.pt/en/history"),  # stays put
            tool(30, "scholar", "Edit", "notes/lisbon.md"),  # → home
            made(36, "scholar", "notes/lisbon.md"),
            idle(42, "scholar"),
        ],
    },
    # Several villagers at one place: distinct slots, and a departure that must
    # not nudge whoever stays.
    "slots": {
        "blurb": "three villagers share the library without shoving each other",
        "loop_every": 70,
        "script": [
            tool(0, "scholar", "WebSearch", "lisbon tram numbering"),
            tool(6, "scribe", "WebFetch", "carris.pt/en/history"),  # second slot
            tool(16, "mason", "WebSearch", "phaser tilemap culling"),  # third slot
            tool(26, "scribe", "Write", "almanac/funicular.md"),  # leaves
            tool(36, "scholar", "WebFetch", "wikipedia.org/Trams"),  # still there
            idle(46, "mason"),
            idle(52, "scholar"),
        ],
    },
    # Every state the projection can produce, held side by side. For looking at
    # chips, sprites and doorway light without chasing a moving target.
    "states": {
        "blurb": "one villager per state: working, researching, resting, knocking, stale",
        "loop_every": 90,
        "script": [
            tool(0, "mason", "Edit", "viewer/index.html"),  # working, home
            tool(0, "scholar", "WebSearch", "calm technology"),  # working, library
            idle(0, "scribe"),  # resting
            knock(0, "courier", "the draft reply is ready — send it?"),  # knocking
            stale(0, "forester", "Bash", "long build"),  # stale
            # …and the states hold. Re-assert them so nobody ages out mid-look.
            tool(60, "mason", "Edit", "viewer/index.html"),
            tool(60, "scholar", "WebFetch", "example.com/calm"),
            idle(60, "scribe"),
            knock(60, "courier", "the draft reply is ready — send it?"),
        ],
    },
    # A working fleet: overlapping tasks, a knock that gets answered, someone
    # leaving for the day. The default, because it looks like a real afternoon.
    "village": {
        "blurb": "a full fleet working, knocking, researching and going home",
        "loop_every": 150,
        "script": [
            started(0, "mason", "fix the library trip"),
            tool(5, "mason", "Read", "village_state.py"),
            tool(12, "scholar", "WebSearch", "phaser 3 tilemaps"),
            started(15, "courier", "triage the inbox"),
            tool(20, "courier", "Read", "inbox/2026-08-24.md"),
            tool(26, "mason", "Edit", "village_state.py"),
            tool(30, "scribe", "Grep", "funicular"),
            tool(38, "scholar", "WebFetch", "phaser.io/docs"),
            knock(44, "courier", "reply to the landlord — approve the wording?"),
            tool(50, "mason", "Bash", "python tests/test_village_state.py"),
            tool(56, "scribe", "WebSearch", "elevador da gloria 1915"),
            made(62, "mason", "village_state.py"),
            tool(68, "courier", "Write", "outbox/landlord.md"),  # knock answered
            tool(74, "scholar", "Edit", "notes/phaser.md"),
            tool(80, "hermit", "Bash", "rsync the attic"),
            idle(86, "scholar"),
            tool(92, "scribe", "Write", "almanac/funicular.md"),
            idle(98, "courier"),
            made(104, "scribe", "almanac/funicular.md"),
            tool(110, "mason", "Bash", "git push"),
            idle(116, "scribe"),
            idle(122, "mason"),
            gone(128, "hermit"),  # leaves the village
        ],
    },
}


def stamp(when):
    return when.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def serialize(beat, now):
    who = AGENTS[beat["agent"]]
    ts = now - datetime.timedelta(seconds=beat["backdate"])
    event = {
        "v": 0,
        "ts": stamp(ts),
        "source": "demo",
        "agent_id": who["id"],
        "project": who["project"],
        "cwd": who["cwd"],
        "type": beat["type"],
        "payload": beat["payload"],
    }
    return json.dumps(event, ensure_ascii=False)


def describe(beat):
    payload = beat["payload"]
    return (
        payload.get("tool")
        or payload.get("message")
        or payload.get("artifact")
        or payload.get("prompt")
        or ""
    )


def run_pass(scenario, out, speed):
    """One pass through a scenario, in real time."""
    script = sorted(scenario["script"], key=lambda b: b["t"])
    start = time.monotonic()
    for beat in script:
        due = start + beat["t"] / speed
        delay = due - time.monotonic()
        if delay > 0:
            time.sleep(delay)
        # Stamped when written, not when scheduled: a laptop that slept must not
        # append events dated in the past and age the whole village out at once.
        now = datetime.datetime.now(datetime.timezone.utc)
        with open(out, "a", encoding="utf-8") as f:
            f.write(serialize(beat, now) + "\n")
        print(
            f"  +{round(beat['t'] / speed):3d}s  {AGENTS[beat['agent']]['id']:14s} "
            f"{beat['type']:17s} {describe(beat)}".rstrip(),
            flush=True,
        )


def main():
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument(
        "scenario",
        nargs="?",
        default="village",
        help="which scenario to run (default: village)",
    )
    ap.add_argument("--list", action="store_true", help="list the scenarios and exit")
    ap.add_argument(
        "--out",
        default="/tmp/burrow-demo.jsonl",
        help="event log to write (default: /tmp/burrow-demo.jsonl)",
    )
    ap.add_argument(
        "--speed",
        type=float,
        default=1.0,
        help="time multiplier (default: 1, 2 = twice as fast)",
    )
    ap.add_argument("--once", action="store_true", help="one pass instead of looping")
    ap.add_argument(
        "--keep",
        action="store_true",
        help="append to the log instead of truncating it first",
    )
    args = ap.parse_args()

    if args.list:
        for name, scenario in SCENARIOS.items():
            print(f"{name:9s} {scenario['blurb']}")
        return 0

    scenario = SCENARIOS.get(args.scenario)
    if not scenario:
        print(
            f'no scenario "{args.scenario}". Try: {", ".join(SCENARIOS)}',
            file=sys.stderr,
        )
        return 2
    if args.speed <= 0:
        print("--speed must be greater than 0", file=sys.stderr)
        return 2

    if not args.keep:
        open(args.out, "w", encoding="utf-8").close()

    print(f"{args.scenario}: {scenario['blurb']}")
    print(f"writing {args.out} at {args.speed:g}× — serve it with:")
    print(f"  BURROW_EVENTS={args.out} python3 serve.py")
    print(
        "one pass, then stop" if args.once else "looping (ctrl-c to stop)", flush=True
    )

    try:
        while True:
            run_pass(scenario, args.out, args.speed)
            if args.once:
                break
            time.sleep(scenario["loop_every"] / args.speed)
    except KeyboardInterrupt:
        print("\nstopped", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
