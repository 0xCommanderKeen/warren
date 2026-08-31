#!/usr/bin/env python3
"""Replay a fixture event log into a live burrow log, one event at a time.

    python3 fixtures/play.py                                  # 2.5 s per event
    python3 fixtures/play.py fixtures/library-walk.jsonl --step 1
    python3 fixtures/play.py --out /tmp/x.jsonl --step 0      # all at once

Then serve the village against the same log:

    BURROW_EVENTS=/tmp/burrow-play.jsonl python3 serve.py

This is not a simulation of agents. A fixture is a recorded sequence of real
protocol events; the player only re-stamps each one with the current time (the
projection ages villagers out after 30 minutes) and appends it, so the viewer's
projection can be watched end to end without waiting on a live fleet.
"""

import argparse
import datetime
import json
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_FIXTURE = os.path.join(HERE, "library-walk.jsonl")


def now_ts():
    now = datetime.datetime.now(datetime.timezone.utc)
    return now.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def main():
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("fixture", nargs="?", default=DEFAULT_FIXTURE)
    ap.add_argument(
        "--out",
        default="/tmp/burrow-play.jsonl",
        help="event log to write (default: /tmp/burrow-play.jsonl)",
    )
    ap.add_argument(
        "--step",
        type=float,
        default=2.5,
        help="seconds between events (default: 2.5, 0 = no pause)",
    )
    ap.add_argument(
        "--keep",
        action="store_true",
        help="append to the log instead of truncating it first",
    )
    args = ap.parse_args()

    with open(args.fixture, encoding="utf-8") as f:
        events = [json.loads(line) for line in f if line.strip()]

    mode = "a" if args.keep else "w"
    with open(args.out, mode, encoding="utf-8") as out:
        for i, event in enumerate(events):
            event["ts"] = now_ts()
            out.write(json.dumps(event, ensure_ascii=False) + "\n")
            out.flush()
            payload = event.get("payload") or {}
            what = payload.get("tool") or payload.get("prompt") or event["type"]
            print(
                f"{i + 1:2d}/{len(events)}  {event['agent_id']:22s} "
                f"{event['type']:17s} {what}",
                flush=True,
            )
            if args.step and i < len(events) - 1:
                time.sleep(args.step)
    print(f"\nreplayed {len(events)} events into {args.out}", flush=True)


if __name__ == "__main__":
    sys.exit(main())
