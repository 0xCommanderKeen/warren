#!/usr/bin/env python3
"""Cross-map walk fixture for the viewer.

Nine agents — eight with house plots in the far corners of the village, one
traveler on the street — cycle between working at their door, knocking at your
door, and resting. Every knock is a walk from a corner plot to the plaza, so the
fixture exercises the longest routes on the map: past houses, around the border
trees, through the fence gates.

    python3 tests/fixture_walks.py                 # live: one transition every 3 s
    python3 tests/fixture_walks.py --once          # one snapshot, then exit
    python3 tests/fixture_walks.py --out /tmp/x.jsonl --every 2

Point the server at it and open the viewer:

    BURROW_EVENTS=/tmp/burrow-fixture.jsonl python3 serve.py 8899
"""

import argparse
import datetime as dt
import itertools
import json
import os
import sys
import time

AGENTS = [
    ("fixture:a-01", "north-west"),
    ("fixture:a-02", "north-west2"),
    ("fixture:a-03", "north-east"),
    ("fixture:a-04", "north-east2"),
    ("fixture:a-05", "south-west"),
    ("fixture:a-06", "south-west2"),
    ("fixture:a-07", "south-east"),
    ("fixture:a-08", "south-east2"),
    ("fixture:a-09", "traveler"),
]

# every agent runs this loop out of phase with the others, so at any moment some
# are crossing the map to your door and others are crossing back home
CYCLE = [
    ("tool_called", {"tool": "Read", "detail": "far side of the village"}),
    ("needs_human", {"message": "walked over to ask you something"}),
    ("needs_human", {"message": "still waiting at your door"}),
    ("idle", {}),
    ("task_started", {"prompt": "heading home to work"}),
    ("tool_called", {"tool": "Bash", "detail": "tinkering at the door"}),
]


def now():
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def event(agent_id, project, kind, payload):
    return {
        "v": 0,
        "ts": now(),
        "source": "fixture",
        "agent_id": agent_id,
        "project": project,
        "cwd": "/tmp/burrow-fixture/" + project,
        "type": kind,
        "payload": payload,
    }


def write(path, events):
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        for e in events:
            f.write(json.dumps(e, ensure_ascii=False) + "\n")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="/tmp/burrow-fixture.jsonl")
    ap.add_argument(
        "--every", type=float, default=3.0, help="seconds between transitions"
    )
    ap.add_argument("--once", action="store_true", help="write one snapshot and exit")
    ap.add_argument("--fresh", action="store_true", help="truncate the log first")
    args = ap.parse_args()

    if args.fresh and os.path.exists(args.out):
        os.remove(args.out)

    # seed: everyone home and working, so the village starts settled
    write(
        args.out,
        [event(a, p, "task_started", {"prompt": "settling in"}) for a, p in AGENTS],
    )
    print(f"fixture → {args.out}", file=sys.stderr)
    if args.once:
        write(
            args.out,
            [event(a, p, *CYCLE[i % len(CYCLE)]) for i, (a, p) in enumerate(AGENTS)],
        )
        return

    for step in itertools.count():
        time.sleep(args.every)
        batch = []
        for i, (agent_id, project) in enumerate(AGENTS):
            kind, payload = CYCLE[(step + i) % len(CYCLE)]
            batch.append(event(agent_id, project, kind, payload))
        write(args.out, batch)
        print(f"step {step}: {len(batch)} transitions", file=sys.stderr)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
