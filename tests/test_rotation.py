"""Rotation: the live log stays bounded, archives keep the history, and the
village the viewer reduces to is identical before and after the roll.

    python3 tests/test_rotation.py        (from the repo root)
"""
import copy
import datetime
import http.client
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import serve
import approval_protocol
from tests.http_test_support import RunningServer


def ts(minutes_ago=0):
    now = datetime.datetime.now(datetime.timezone.utc)
    when = now - datetime.timedelta(minutes=minutes_ago)
    return when.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def event(agent, etype="tool_called", minutes_ago=0, **payload):
    return {"v": 0, "ts": ts(minutes_ago), "source": "test", "agent_id": agent,
            "project": "burrow", "cwd": "/tmp", "type": etype, "payload": payload}


def village(lines, now_ms=None):
    """The viewer's projection, boiled down to what each villager shows: state
    is decided by the latest event, and only live villagers are drawn."""
    now_ms = now_ms or int(datetime.datetime.now(datetime.timezone.utc).timestamp() * 1000)
    per_agent = {}
    for line in lines:
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        if not ev.get("agent_id") or ev.get("type") not in serve.EVENT_TYPES:
            continue
        kept = per_agent.setdefault(ev["agent_id"], [])
        kept.append(ev)
        if len(kept) > serve.KEEP_PER_AGENT:
            kept.pop(0)
    out = {}
    for agent, kept in per_agent.items():
        last = kept[-1]
        if last["type"] == "session_ended":
            continue
        if now_ms - serve.event_ms(last) > serve.DROP_MS:
            continue
        out[agent] = {"last": last, "history": [json.dumps(e, sort_keys=True) for e in kept]}
    return out


def protocol_events(lines):
    """Decode public v0 events while excluding reserved transport metadata."""
    return [item for item in map(json.loads, lines)
            if item.get("_burrow_internal") != serve.MOOD_AUTHORITY_KIND]


class CarryForwardTest(unittest.TestCase):
    @staticmethod
    def journal(agent, day, routine="close-of-day", path=None, minutes_ago=0):
        observed = event(agent, "journal_written", minutes_ago, routine=routine,
                         day=day, path=path or f"/journal/{day}.md")
        observed["source"] = "steward"
        return observed

    def test_routine_only_villager_and_history_are_identical_after_rotation(self):
        agent_id = "codex:pip"
        events = []
        for index in range(serve.KEEP_PER_AGENT + 10):
            item = event(agent_id, "routine_started", 10 - index / 100,
                         routine="heartbeat", run_id=f"hourly-{index}",
                         trigger="schedule")
            item["source"] = "steward"
            events.append(item)
        events.append({**events[-1], "ts": ts(0), "type": "routine_finished",
                       "payload": {"routine": "heartbeat", "run_id": "hourly-89",
                                   "outcome": "ok", "artifacts": [],
                                   "duration_s": 1.25}})
        lines = list(map(json.dumps, events))
        now_ms = serve.event_ms(events[-1])
        rotated = serve.carry_forward(lines, now_ms)
        script = r"""
const fs=require('node:fs'),p=require('./viewer/projection.js');
const input=JSON.parse(fs.readFileSync(0,'utf8'));
const shape=lines=>p.reduce(lines,input.now,[]).map(v=>({id:v.id,state:v.state,
 lastTs:v.lastTs,lastLine:v.lastLine,events:v.events.map(e=>e.type)}));
process.stdout.write(JSON.stringify(input.groups.map(shape)));
"""
        completed = subprocess.run(["node", "-e", script], cwd=os.path.dirname(
            os.path.dirname(__file__)), input=json.dumps({"groups": [lines, rotated],
                                                          "now": now_ms}),
            capture_output=True, text=True, check=True)
        before, after = json.loads(completed.stdout)
        self.assertEqual(after, before)
        self.assertEqual(after[0]["state"], "resting")
        self.assertEqual(after[0]["lastLine"], "finished heartbeat, ok in 1.25s")
        self.assertEqual(len(after[0]["events"]), serve.KEEP_PER_AGENT)
        self.assertLessEqual(len(rotated), serve.VIEWER_LINE_LIMIT)

    def test_rotated_invalid_and_non_steward_routines_never_create_villagers(self):
        forged = event("codex:pip", "routine_started", routine="heartbeat",
                       run_id="forged", trigger="schedule")
        malformed = {**forged, "source": "steward",
                     "payload": {"routine": "heartbeat", "run_id": "",
                                 "trigger": "schedule"}}
        rotated = serve.carry_forward(list(map(json.dumps, [forged, malformed])),
                                      serve.event_ms(forged))
        self.assertEqual(protocol_events(rotated), [])

    def test_routine_after_journal_remains_the_visible_successor_after_rotation(self):
        journal = self.journal("codex:pip", "2026-08-25")
        started = event("codex:pip", "routine_started", routine="heartbeat",
                        run_id="after-journal", trigger="schedule")
        started["source"] = "steward"
        chatter = [json.dumps(event(f"codex:gone-{index}", "session_ended"))
                   for index in range(serve.VIEWER_LINE_LIMIT)]
        lines = [json.dumps(journal), json.dumps(started), *chatter]
        rotated = serve.carry_forward(lines, serve.event_ms(started))
        retained = protocol_events(rotated)
        pip = [item for item in retained if item["agent_id"] == "codex:pip"]
        self.assertEqual([item["type"] for item in pip],
                         ["journal_written", "routine_started"])

    def test_live_routine_survives_exact_tail_noise_rotation_and_grouped_reset(self):
        started = event("codex:pip", "routine_started", routine="heartbeat",
                        run_id="boundary", trigger="schedule")
        started["source"] = "steward"
        noise = [event(f"codex:gone-{index}", "session_ended")
                 for index in range(serve.VIEWER_LINE_LIMIT)]
        lines = list(map(json.dumps, [started, *noise]))
        now_ms = serve.event_ms(started)
        rotated = serve.carry_forward(lines, now_ms)
        self.assertLessEqual(len(rotated), serve.VIEWER_LINE_LIMIT)
        retained = protocol_events(rotated)
        self.assertEqual([(item["agent_id"], item["type"]) for item in retained],
                         [("codex:pip", "routine_started")])
        script = r"""
const fs=require('node:fs');
const {createBrowserRuntime}=require('./viewer/browser-runtime.js');
const input=JSON.parse(fs.readFileSync(0,'utf8'));
let views=[];
const runtime=createBrowserRuntime({now:()=>input.now,EventSource:null,
 setTimeout:()=>1,clearTimeout(){},warn(){},onProjection:v=>views.push(v),
 fetch:async url=>url==='/villagers'?{ok:false}:{ok:true,
  headers:{get:n=>n==='X-Burrow-Cursor'?'v1:0123456789abcdef0123456789abcdef:1:2:3:9':null},
  text:async()=>input.lines.join('\n')}});
runtime.poll().then(()=>process.stdout.write(JSON.stringify(runtime.snapshot().villagers.map(v=>({
 id:v.id,state:v.state,line:v.lastLine,history:v.events.map(e=>e.type)})))));
"""
        completed = subprocess.run(["node", "-e", script], cwd=os.path.dirname(
            os.path.dirname(__file__)), input=json.dumps({"lines": rotated, "now": now_ms}),
            capture_output=True, text=True, check=True)
        self.assertEqual(json.loads(completed.stdout), [{
            "id": "codex:pip", "state": "working", "line": "woke for heartbeat",
            "history": ["routine_started"],
        }])

    def test_python_and_javascript_projection_witnesses_match_at_cap(self):
        events = []
        for index in range(120):
            started = event(f"codex:r-{index}", "routine_started",
                            routine="heartbeat", run_id=f"run-{index}", trigger="schedule")
            started["source"] = "steward"
            events.append(started)
            if index % 3 == 0:
                events.append(event(f"codex:r-{index}", "idle"))
            if index % 7 == 0:
                events.append(event(f"codex:r-{index}", "session_ended"))
        parsed = list(enumerate(events))
        now_ms = max(serve.event_ms(item) for item in events)
        python_indexes = sorted(serve._projection_keep_indexes(parsed, now_ms, 80))
        script = r"""
const fs=require('node:fs'),p=require('./viewer/projection.js');
const x=JSON.parse(fs.readFileSync(0,'utf8')), parsed=p.parseEvents(x.events);
const selected=new Set(p.projectionWitnesses(parsed,x.now,x.limit));
process.stdout.write(JSON.stringify(parsed.map((e,i)=>selected.has(e)?i:null).filter(i=>i!==null)));
"""
        completed = subprocess.run(["node", "-e", script], cwd=os.path.dirname(
            os.path.dirname(__file__)), input=json.dumps({"events": events,
                                                          "now": now_ms, "limit": 80}),
            capture_output=True, text=True, check=True)
        self.assertEqual(json.loads(completed.stdout), python_indexes)

    def test_pending_knock_and_routines_share_one_exact_projection_cap(self):
        anchor = datetime.datetime.now(datetime.timezone.utc)
        events = []
        for index in range(serve.VIEWER_LINE_LIMIT - 1):
            started = event(f"codex:r{index:04d}", "routine_started",
                            routine="heartbeat", run_id=f"run-{index}", trigger="schedule")
            started["source"] = "steward"
            started["ts"] = (anchor + datetime.timedelta(milliseconds=index)).isoformat(
                timespec="milliseconds").replace("+00:00", "Z")
            events.append(started)
        pending = event("codex:doorstep", "needs_human", message="Approve deploy?",
                        request_id="exact-cap", action="deploy", detail=None,
                        options=["approve", "deny"])
        pending["source"] = "codex"
        pending["ts"] = (anchor + datetime.timedelta(milliseconds=4000)).isoformat(
            timespec="milliseconds").replace("+00:00", "Z")
        events.append(pending)
        rotated = serve.carry_forward(list(map(json.dumps, events)),
                                      int(anchor.timestamp() * 1000) + 4000)
        retained = protocol_events(rotated)
        self.assertEqual(len(retained), serve.VIEWER_LINE_LIMIT)
        self.assertEqual(retained[0]["agent_id"], "codex:r0000")
        self.assertEqual(retained[-1]["agent_id"], "codex:doorstep")
        self.assertEqual(sum(item["type"] == "routine_started" for item in retained), 3999)

        script = r"""
const fs=require('node:fs'),p=require('./viewer/projection.js');
const batches=JSON.parse(fs.readFileSync(0,'utf8')),shape=lines=>p.reduce(lines,batches.now,[])
 .map(v=>[v.id,v.state,v.lastLine]);
process.stdout.write(JSON.stringify([shape(batches.full),shape(batches.rotated)]));
"""
        completed = subprocess.run(["node", "-e", script], cwd=os.path.dirname(
            os.path.dirname(__file__)), input=json.dumps({"full": list(map(json.dumps, events)),
                "rotated": rotated, "now": int(anchor.timestamp() * 1000) + 4000}),
            capture_output=True, text=True, check=True)
        full, after = json.loads(completed.stdout)
        self.assertEqual(after, full)

    def test_rotation_keeps_canonical_routine_lifecycle_conflicts(self):
        base = datetime.datetime.now(datetime.timezone.utc)
        stamp = lambda seconds: (base + datetime.timedelta(seconds=seconds)).isoformat(
            timespec="milliseconds").replace("+00:00", "Z")
        def fact(agent_id, kind, seconds, **payload):
            item = event(agent_id, kind, routine="heartbeat", run_id="ordered", **payload)
            item["source"] = "steward"; item["ts"] = stamp(seconds)
            return item
        source = [
            fact("codex:delayed", "routine_started", 0, trigger="schedule"),
            fact("codex:delayed", "routine_finished", 60, outcome="ok",
                 duration_s=8, artifacts=[]),
            fact("codex:delayed", "routine_started", 0, trigger="schedule"),
            fact("codex:preclose", "routine_finished", -60, outcome="ok",
                 duration_s=8, artifacts=[]),
            fact("codex:preclose", "routine_started", 0, trigger="schedule"),
            fact("codex:conflict", "routine_started", 0, trigger="schedule"),
            fact("codex:conflict", "routine_failed", 60, error="boom"),
            fact("codex:conflict", "routine_finished", 60, outcome="ok",
                 duration_s=8, artifacts=[]),
        ]
        source.extend(event(f"codex:gone-{index}", "session_ended")
                      for index in range(serve.VIEWER_LINE_LIMIT))
        rotated = serve.carry_forward(list(map(json.dumps, source)),
                                      int(base.timestamp() * 1000) + 120_000)
        script = r"""
const fs=require('node:fs'),p=require('./viewer/projection.js'),x=JSON.parse(fs.readFileSync(0,'utf8'));
const shape=lines=>Object.fromEntries(p.reduce(lines,x.now,[]).filter(v=>v.id.startsWith('codex:')&&!v.id.includes('gone-'))
 .map(v=>[v.id,[v.state,v.lastLine,v.events.map(e=>e.type)]]));
process.stdout.write(JSON.stringify([shape(x.full),shape(x.rotated)]));
"""
        completed = subprocess.run(["node", "-e", script], cwd=os.path.dirname(
            os.path.dirname(__file__)), input=json.dumps({"full": list(map(json.dumps, source)),
                "rotated": rotated, "now": int(base.timestamp() * 1000) + 120_000}),
            capture_output=True, text=True, check=True)
        full, after = json.loads(completed.stdout)
        self.assertEqual(after, full)
        self.assertEqual(full["codex:delayed"][:2],
                         ["resting", "finished heartbeat, ok in 8s"])
        self.assertEqual(full["codex:preclose"][:2], ["working", "woke for heartbeat"])
        self.assertEqual(full["codex:conflict"][:2], ["failed", "heartbeat failed — boom"])

    def test_rotation_keeps_bounded_routine_authority_at_79_80_81_and_hides_orphans(self):
        base = datetime.datetime.now(datetime.timezone.utc)
        stamp = lambda seconds: (base + datetime.timedelta(seconds=seconds)).isoformat(
            timespec="milliseconds").replace("+00:00", "Z")
        def fact(kind, seconds, **payload):
            item = event("codex:bounded", kind, routine="heartbeat", run_id="bounded", **payload)
            item["source"] = "steward"; item["ts"] = stamp(seconds)
            return item
        started = fact("routine_started", 0, trigger="schedule")
        finished = fact("routine_finished", 60, outcome="ok", duration_s=8, artifacts=[])
        noise = [event(f"codex:gone-cap-{index}", "session_ended")
                 for index in range(serve.VIEWER_LINE_LIMIT)]
        script = r"""
const p=require('./viewer/projection.js'),x=JSON.parse(require('node:fs').readFileSync(0,'utf8'));
const shape=records=>{const v=p.reduce(records,x.now,[]).find(v=>v.id==='codex:bounded');
 return v?[v.state,v.lastLine,v.events.length,v.stateEvent.type]:null};
process.stdout.write(JSON.stringify([shape(x.full),shape(x.rotated)]));
"""
        for count in (79, 80, 81):
            source = [started, finished] + [started] * count + noise
            rotated = serve.carry_forward(list(map(json.dumps, source)),
                                          int(base.timestamp() * 1000) + 120_000)
            completed = subprocess.run(["node", "-e", script], cwd=os.path.dirname(
                os.path.dirname(__file__)), input=json.dumps({"full": list(map(json.dumps, source)),
                    "rotated": rotated, "now": int(base.timestamp() * 1000) + 120_000}),
                capture_output=True, text=True, check=True)
            full, after = json.loads(completed.stdout)
            self.assertEqual(after, full)
            self.assertEqual(full, ["resting", "finished heartbeat, ok in 8s", 80,
                                    "routine_finished"])
            retained = [json.loads(line) for line in rotated
                        if not json.loads(line).get("_burrow_internal")]
            self.assertLessEqual(sum(item.get("agent_id") == "codex:bounded"
                                     for item in retained), 80)

        orphan = fact("routine_failed", 60, error="must stay hidden")
        rotated = serve.carry_forward(list(map(json.dumps, [orphan] + noise)),
                                      int(base.timestamp() * 1000) + 120_000)
        completed = subprocess.run(["node", "-e", script], cwd=os.path.dirname(
            os.path.dirname(__file__)), input=json.dumps({"full": [json.dumps(orphan)],
                "rotated": rotated, "now": int(base.timestamp() * 1000) + 120_000}),
            capture_output=True, text=True, check=True)
        self.assertEqual(json.loads(completed.stdout), [None, None])

    def test_python_and_javascript_routine_unicode_ties_match_at_transport_cap(self):
        base = datetime.datetime.now(datetime.timezone.utc)
        stamp = base.isoformat(timespec="milliseconds").replace("+00:00", "Z")
        def started(agent_id, project, run_id):
            item = event(agent_id, "routine_started", routine="heartbeat", run_id=run_id,
                         trigger="schedule")
            item.update(source="steward", project=project, ts=stamp)
            return item
        source = [started(f"codex:scalar-{index}", "life", str(index))
                  for index in range(3999)]
        source += [started("codex:unicode", "😀", "same"),
                   started("codex:unicode", "\ue000", "same")]
        parsed = list(enumerate(source))
        kept = serve._projection_keep_indexes(parsed, int(base.timestamp() * 1000), 4000)
        self.assertEqual(len(kept), 4000)
        rotated = serve.carry_forward(list(map(json.dumps, source)),
                                      int(base.timestamp() * 1000))
        script = r"""
const p=require('./viewer/projection.js'),x=JSON.parse(require('node:fs').readFileSync(0,'utf8'));
const shape=records=>{const all=p.reduce(records,x.now,[]),v=all.find(v=>v.id==='codex:unicode');
 return [all.length,v&&v.project,p.projectionWitnesses(records,x.now,4000).length]};
process.stdout.write(JSON.stringify([shape(x.full),shape(x.rotated)]));
"""
        completed = subprocess.run(["node", "-e", script], cwd=os.path.dirname(
            os.path.dirname(__file__)), input=json.dumps({"full": list(map(json.dumps, source)),
                "rotated": rotated, "now": int(base.timestamp() * 1000)}),
            capture_output=True, text=True, check=True)
        full, after = json.loads(completed.stdout)
        self.assertEqual(after, full)
        self.assertEqual(full, [4000, "\ue000", 4000])

        terminal_source = [started(f"codex:terminal-{index}", "life", str(index))
                           for index in range(3997)]
        terminal_start = started("codex:unicode", "life", "terminal")
        terminal_at = (base + datetime.timedelta(seconds=1)).isoformat(
            timespec="milliseconds").replace("+00:00", "Z")
        def terminal(outcome):
            item = event("codex:unicode", "routine_finished", routine="heartbeat",
                         run_id="terminal", outcome=outcome, duration_s=1, artifacts=[])
            item.update(source="steward", project="life", ts=terminal_at)
            return item
        idle = event("codex:ordinary", "idle")
        idle["ts"] = terminal_at
        terminal_source += [terminal_start, terminal("\ue000"), terminal("😀"), idle]
        rotated_terminal = serve.carry_forward(list(map(json.dumps, terminal_source)),
                                               int(base.timestamp() * 1000) + 1000)
        terminal_script = r"""
const p=require('./viewer/projection.js'),x=JSON.parse(require('node:fs').readFileSync(0,'utf8'));
const shape=records=>{const all=p.reduce(records,x.now,[]),v=all.find(v=>v.id==='codex:unicode');
 return [all.length,v&&v.lastLine,p.projectionWitnesses(records,x.now,4000).length]};
process.stdout.write(JSON.stringify([shape(x.full),shape(x.rotated)]));
"""
        completed = subprocess.run(["node", "-e", terminal_script], cwd=os.path.dirname(
            os.path.dirname(__file__)), input=json.dumps({"full": list(map(json.dumps, terminal_source)),
                "rotated": rotated_terminal, "now": int(base.timestamp() * 1000) + 1000}),
            capture_output=True, text=True, check=True)
        full, after = json.loads(completed.stdout)
        self.assertEqual(after, full)
        self.assertEqual(full, [3999, "finished heartbeat, 😀 in 1s", 4000])

        collision_source = [started(f"codex:nul-{index}", "life", str(index))
                            for index in range(3998)]
        collision_source += [
            {**started("codex:nul", "one", "c"),
             "payload": {"routine": "a\0b", "run_id": "c", "trigger": "schedule"}},
            {**started("codex:nul", "two", "b\0c"),
             "payload": {"routine": "a", "run_id": "b\0c", "trigger": "schedule"}},
        ]
        rotated_collision = serve.carry_forward(list(map(json.dumps, collision_source)),
                                                int(base.timestamp() * 1000))
        collision_script = r"""
const r=require('./viewer/routine-ledger.js'),x=JSON.parse(require('node:fs').readFileSync(0,'utf8'));
const count=records=>r.project(records.map(JSON.parse),x.now).byRoutine.size;
process.stdout.write(JSON.stringify([count(x.full),count(x.rotated)]));
"""
        completed = subprocess.run(["node", "-e", collision_script], cwd=os.path.dirname(
            os.path.dirname(__file__)), input=json.dumps({"full": list(map(json.dumps, collision_source)),
                "rotated": rotated_collision, "now": int(base.timestamp() * 1000)}),
            capture_output=True, text=True, check=True)
        self.assertEqual(json.loads(completed.stdout), [4000, 4000])

    def test_heartbeat_support_matches_reducer_at_exact_projection_limits(self):
        now = datetime.datetime.now(datetime.timezone.utc)
        stamp = now.isoformat(timespec="milliseconds").replace("+00:00", "Z")

        def observed(agent_id, kind, payload=None, source="test"):
            return {"v": 0, "ts": stamp, "source": source, "agent_id": agent_id,
                    "project": "burrow", "type": kind, "payload": payload or {}}

        cases = []
        for prior_type in ["idle", "routine_finished", "tool_called", "tool_called/idle"]:
            kind = prior_type.split("/")[0]
            if kind == "routine_finished":
                prior = observed("codex:boundary", kind,
                    {"routine": "heartbeat", "run_id": "boundary", "outcome": "ok",
                     "artifacts": [], "duration_s": 1}, "steward")
            elif kind == "tool_called":
                prior = observed("codex:boundary", kind, {"tool": "Read"})
            else:
                prior = observed("codex:boundary", kind)
            heartbeat = observed("codex:boundary", "heartbeat")
            for limit, noise_count in [(1, 0), (serve.VIEWER_LINE_LIMIT,
                                                serve.VIEWER_LINE_LIMIT - 1)]:
                noise = [observed(f"codex:live-{index}", "idle")
                         for index in range(noise_count)]
                events = [prior]
                if prior_type == "tool_called/idle":
                    events.append(observed("codex:boundary", "idle"))
                events.extend([heartbeat, *noise])
                parsed = list(enumerate(events))
                python_indexes = sorted(serve._projection_keep_indexes(
                    parsed, int(now.timestamp() * 1000), limit))
                cases.append({"name": f"{prior_type}/{limit}", "events": events,
                              "now": int(now.timestamp() * 1000), "limit": limit,
                              "python": python_indexes})
                boundary_types = [events[index]["type"] for index in python_indexes
                                  if events[index]["agent_id"] == "codex:boundary"]
                expected = [] if prior_type == "tool_called" else ["heartbeat"]
                self.assertEqual(boundary_types, expected, cases[-1]["name"])

        script = r"""
const fs=require('node:fs'),p=require('./viewer/projection.js');
const cases=JSON.parse(fs.readFileSync(0,'utf8'));
process.stdout.write(JSON.stringify(cases.map(item=>{
 const parsed=p.parseEvents(item.events),selected=new Set(
  p.projectionWitnesses(parsed,item.now,item.limit));
 return parsed.map((event,index)=>selected.has(event)?index:null).filter(index=>index!==null);
})));
"""
        completed = subprocess.run(["node", "-e", script], cwd=os.path.dirname(
            os.path.dirname(__file__)), input=json.dumps(cases), capture_output=True,
            text=True, check=True)
        self.assertEqual(json.loads(completed.stdout),
                         [case["python"] for case in cases])

    def test_shared_mood_fixture_is_byte_equivalent_after_python_rotation(self):
        fixture_path = os.path.join(os.path.dirname(__file__), "fixtures",
                                    "mood-rotation.json")
        with open(fixture_path, encoding="utf-8") as stream:
            fixture = json.load(stream)
        lines = [json.dumps(item, separators=(",", ":"))
                 for item in fixture["events"]]
        now_ms = int(datetime.datetime.fromisoformat(
            fixture["now"].replace("Z", "+00:00")).timestamp() * 1000)
        rotated = serve.carry_forward(lines, now_ms)
        script = r"""
const fs=require('fs');
const projection=require('./viewer/projection.js');
const moods=require('./viewer/moods.js');
const batches=JSON.parse(fs.readFileSync(0,'utf8'));
const derive=lines=>Object.fromEntries(moods.deriveMoods(projection.parseEvents(lines)));
process.stdout.write(JSON.stringify(batches.map(derive)));
"""
        completed = subprocess.run(["node", "-e", script], cwd=os.path.dirname(
            os.path.dirname(__file__)), input=json.dumps([lines, rotated]),
            capture_output=True, text=True, check=True)
        before, after = json.loads(completed.stdout)
        self.assertEqual(after, before)
        self.assertEqual(after["codex:mood"]["glyph"], "!")

    def test_timestamp_disordered_terminal_frontier_matches_javascript_after_rotations(self):
        anchor = datetime.datetime(2026, 8, 25, 12, tzinfo=datetime.timezone.utc)

        def observed(hours, event_type, **payload):
            return {"v": 0, "ts": (anchor + datetime.timedelta(hours=hours))
                    .isoformat(timespec="milliseconds").replace("+00:00", "Z"),
                    "source": "codex", "agent_id": "codex:frontier",
                    "project": "burrow", "type": event_type, "payload": payload}

        initial = [observed(-10, "tool_failed", tool="early"),
                   observed(-23.5, "heartbeat")]
        initial.extend(observed(hours, "tool_failed", tool=f"frontier-{index}")
                       for index, hours in enumerate((-23, -22.5, -22, -21.5)))
        initial.extend([observed(-1, "tool_called", tool="Read"),
                        observed(0, "idle")])
        appended = observed(2, "idle")
        initial_lines = [json.dumps(item, separators=(",", ":")) for item in initial]
        once = serve.carry_forward(initial_lines, serve.event_ms(initial[-1]))
        twice = serve.carry_forward(
            [*once, json.dumps(appended, separators=(",", ":"))],
            serve.event_ms(appended))
        thrice = serve.carry_forward(twice, serve.event_ms(appended))
        self.assertEqual(thrice, twice, "repeated Python rotation is byte stable")

        script = r"""
const p=require('./viewer/projection.js'),m=require('./viewer/moods.js');
const input=JSON.parse(require('node:fs').readFileSync(0,'utf8'));
const read=lines=>Object.fromEntries(m.deriveMoods(p.parseEvents(lines)));
process.stdout.write(JSON.stringify({full:read([...input.initial,input.append]),
  grouped:read([...input.once,input.append]),rotated:read(input.twice)}));
"""
        completed = subprocess.run(["node", "-e", script], input=json.dumps({
            "initial": initial_lines, "append": json.dumps(appended, separators=(",", ":")),
            "once": once, "twice": twice}), text=True,
            cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            check=True, capture_output=True)
        result = json.loads(completed.stdout)
        self.assertEqual(result["grouped"], result["full"])
        self.assertEqual(result["rotated"], result["full"])
        mood = result["full"]["codex:frontier"]
        self.assertEqual([mood["signals"]["failure"]["streak"],
                          mood["signals"]["failure"]["failures"],
                          mood["enoughEvidence"], mood["status"]],
                         [2, 2, True, "watchful"])

    def test_terminal_witness_ceiling_matches_javascript_and_new_epoch_recovers(self):
        anchor = datetime.datetime(2026, 8, 25, 12, tzinfo=datetime.timezone.utc)

        def frontier(count):
            records = []
            for index in range(count):
                when = anchor - datetime.timedelta(hours=24) + datetime.timedelta(
                    milliseconds=(index + 1) * 24 * 60 * 60 * 1000 / count)
                records.append({"v": 0,
                    "ts": when.isoformat(timespec="milliseconds").replace("+00:00", "Z"),
                    "source": "codex", "agent_id": "codex:ceiling",
                    "project": "burrow", "type": "heartbeat",
                    "payload": {"sequence": index}})
            return records

        groups = []
        for count in (160, 161):
            events = frontier(count)
            lines = [json.dumps(item, separators=(",", ":")) for item in events]
            rotated = serve.carry_forward(lines, int(anchor.timestamp() * 1000))
            repeated = serve.carry_forward(rotated, int(anchor.timestamp() * 1000))
            self.assertEqual(repeated, rotated)
            capsule = serve._mood_authority_from_line(rotated[0])
            self.assertEqual(bool(capsule and capsule["overflow"]), count == 161)
            groups.append({"full": lines, "rotated": rotated})

        fresh = frontier(1)
        fresh_lines = serve.carry_forward(
            [json.dumps(fresh[0], separators=(",", ":"))],
            int(anchor.timestamp() * 1000))
        self.assertIsNone(serve._mood_authority_from_line(fresh_lines[0]))

        script = r"""
const p=require('./viewer/projection.js'),m=require('./viewer/moods.js');
const groups=JSON.parse(require('node:fs').readFileSync(0,'utf8'));
const inspect=lines=>{const batch=p.parseEvents(lines),
  retained=m.retainMoodWitnesses(batch),state=p.moodAuthorityState(retained),
  mood=m.deriveMoods(batch).get('codex:ceiling');return {overflow:state.overflow,
  status:mood.status,mood:JSON.stringify(mood),retained:[...retained]};};
process.stdout.write(JSON.stringify(groups.map(group=>({full:inspect(group.full),
  rotated:inspect(group.rotated)}))));
"""
        completed = subprocess.run(["node", "-e", script], input=json.dumps(groups),
            text=True, cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            check=True, capture_output=True)
        results = json.loads(completed.stdout)
        self.assertEqual(results[0]["full"]["overflow"], False)
        self.assertEqual(results[0]["rotated"]["mood"], results[0]["full"]["mood"])
        self.assertEqual(results[1]["full"]["status"], "authority history uncertain")
        self.assertEqual(results[1]["rotated"], results[1]["full"])
        for group, result in zip(groups, results):
            self.assertEqual(protocol_events(group["rotated"]),
                             result["full"]["retained"],
                             "Python and JavaScript retain identical public witness bytes")

    def test_forged_nonoverflow_manifest_cannot_hide_witness_overflow(self):
        anchor = datetime.datetime(2026, 8, 25, 12, tzinfo=datetime.timezone.utc)
        heartbeats = []
        for index in range(161):
            heartbeats.append({"v": 0,
                "ts": (anchor - datetime.timedelta(seconds=160 - index))
                    .isoformat(timespec="milliseconds").replace("+00:00", "Z"),
                "source": "codex", "agent_id": "codex:forged-frontier",
                "project": "burrow", "type": "heartbeat",
                "payload": {"sequence": index}})
        root = {**heartbeats[0],
                "ts": (anchor - datetime.timedelta(hours=3))
                    .isoformat(timespec="milliseconds").replace("+00:00", "Z"),
                "type": "task_started", "payload": {"prompt": "old root"}}
        raw = [*heartbeats, root]
        indexes = [*range(159), 161]
        capsule = {"_burrow_internal": serve.MOOD_AUTHORITY_KIND,
                   "events": [root], "ordinals": ["161"], "copies": ["161"],
                   "raw_ordinals": list(map(str, indexes)),
                   "raw_indexes": [str(index).zfill(16) for index in indexes],
                   "raw_count": str(len(raw)).zfill(16),
                   "overflow": False, "observed": 1}
        lines = [json.dumps(capsule, separators=(",", ":")),
                 *[json.dumps(item, separators=(",", ":")) for item in raw]]
        rotated = serve.carry_forward(lines, int(anchor.timestamp() * 1000))
        rerotated = serve.carry_forward(rotated, int(anchor.timestamp() * 1000))
        rebuilt = serve._mood_authority_from_line(rotated[0])
        self.assertTrue(rebuilt and rebuilt["overflow"],
                        "semantic rejection must rebuild canonical durable overflow")
        self.assertEqual(rerotated, rotated)
        self.assertLessEqual(len(protocol_events(rotated)),
                             serve.MAX_MOOD_RETAINED_PER_AGENT)

        script = r"""
const p=require('./viewer/projection.js'),m=require('./viewer/moods.js');
const batches=JSON.parse(require('node:fs').readFileSync(0,'utf8'));
const read=lines=>m.deriveMoods(p.parseEvents(lines)).get('codex:forged-frontier');
process.stdout.write(JSON.stringify(batches.map(read)));
"""
        completed = subprocess.run(["node", "-e", script], input=json.dumps([
            [json.dumps(item, separators=(",", ":")) for item in raw], lines,
            rotated, [*rotated, json.dumps({**root,
                "ts": (anchor + datetime.timedelta(minutes=1))
                    .isoformat(timespec="milliseconds").replace("+00:00", "Z"),
                "type": "idle", "payload": {}}, separators=(",", ":"))]]),
            text=True, cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            check=True, capture_output=True)
        full, parsed, after_rotation, appended = json.loads(completed.stdout)
        self.assertEqual(parsed, full, "invalid capsule preserves the raw epoch atomically")
        self.assertEqual(after_rotation, full)
        self.assertEqual(appended["status"], "authority history uncertain")

    def test_future_plain_supersession_keeps_backup_sufficiency_witnesses(self):
        fixture_path = os.path.join(os.path.dirname(__file__), "fixtures",
                                    "mood-future-sufficiency.json")
        with open(fixture_path, encoding="utf-8") as stream:
            fixture = json.load(stream)
        initial = [json.dumps(item, separators=(",", ":"))
                   for item in fixture["initial"]]
        appended = json.dumps(fixture["append"], separators=(",", ":"))
        now_ms = serve.event_ms(fixture["initial"][3])
        grouped = serve.carry_forward(initial, now_ms)
        once = serve.carry_forward([*grouped, appended], now_ms)
        twice = serve.carry_forward(once, now_ms)
        script = r"""
const p=require('./viewer/projection.js'),m=require('./viewer/moods.js');
const batches=JSON.parse(require('node:fs').readFileSync(0,'utf8'));
const derive=lines=>Object.fromEntries(m.deriveMoods(p.parseEvents(lines)));
process.stdout.write(JSON.stringify(batches.map(derive)));
"""
        full = [*initial, appended]
        completed = subprocess.run(
            ["node", "-e", script], input=json.dumps([full, once, twice]), text=True,
            cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            check=True, capture_output=True)
        expected, after_once, after_twice = json.loads(completed.stdout)
        agent_id = fixture["agent_id"]
        self.assertEqual(after_once[agent_id], expected[agent_id])
        self.assertEqual(after_twice[agent_id], expected[agent_id])
        self.assertEqual(expected[agent_id]["status"], "active")
        self.assertEqual(expected[agent_id]["evidence"],
                         {"count": 6, "spanMs": 30 * 60 * 1000})
        self.assertEqual(twice, once,
                         "a second Python rotation is byte-for-byte stable")

    def test_overflow_bounds_cross_agent_collision_attachments(self):
        anchor = datetime.datetime(2026, 8, 25, 12,
                                   tzinfo=datetime.timezone.utc)
        events = []
        for index in range(300):
            timestamp = (anchor + datetime.timedelta(seconds=index)).isoformat(
                timespec="milliseconds").replace("+00:00", "Z")
            for agent_id, action in (("codex:hub", "deploy"),
                                     (f"codex:temporary-{index}", "erase")):
                events.append({
                    "v": 0, "ts": timestamp, "source": "codex",
                    "agent_id": agent_id, "project": "burrow",
                    "type": "needs_human", "payload": {
                        "message": f"collision {index}",
                        "request_id": f"shared-{index}", "action": action,
                        "detail": None, "options": ["approve"]}})
        lines = [json.dumps(item, separators=(",", ":")) for item in events]
        rotated = serve.carry_forward(
            lines, int((anchor + datetime.timedelta(minutes=5)).timestamp() * 1000))
        capsule = serve._mood_authority_from_line(rotated[0])
        self.assertTrue(capsule["overflow"])
        counts = {}
        for item in protocol_events(rotated):
            counts[item["agent_id"]] = counts.get(item["agent_id"], 0) + 1
        self.assertLessEqual(max(counts.values()),
                             serve.MAX_MOOD_RETAINED_PER_AGENT)

        script = r"""
const p=require('./viewer/projection.js'),m=require('./viewer/moods.js');
const lines=JSON.parse(require('node:fs').readFileSync(0,'utf8'));
const mood=m.deriveMoods(p.parseEvents(lines)).get('codex:hub');
process.stdout.write(JSON.stringify(mood));
"""
        completed = subprocess.run(
            ["node", "-e", script], input=json.dumps(rotated), text=True,
            cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            check=True, capture_output=True)
        self.assertEqual(json.loads(completed.stdout)["status"],
                         "authority history uncertain")

    def test_mood_rotation_ignores_orphan_resolutions_as_threshold_witnesses(self):
        fixture_path = os.path.join(os.path.dirname(__file__), "fixtures",
                                    "mood-rotation-adversarial.json")
        with open(fixture_path, encoding="utf-8") as stream:
            fixture = json.load(stream)
        lines = [json.dumps(item, separators=(",", ":"))
                 for item in fixture["events"]]
        now_ms = int(datetime.datetime.fromisoformat(
            fixture["now"].replace("Z", "+00:00")).timestamp() * 1000)
        rotated = serve.carry_forward(lines, now_ms)
        script = r"""
const projection=require('./viewer/projection.js');
const moods=require('./viewer/moods.js');
const batches=JSON.parse(require('fs').readFileSync(0,'utf8'));
const derive=lines=>Object.fromEntries(moods.deriveMoods(projection.parseEvents(lines)));
process.stdout.write(JSON.stringify(batches.map(derive)));
"""
        completed = subprocess.run(["node", "-e", script], cwd=os.path.dirname(
            os.path.dirname(__file__)), input=json.dumps([lines, rotated]),
            capture_output=True, text=True, check=True)
        before, after = json.loads(completed.stdout)
        self.assertTrue(before["codex:mood-adversarial"]["enoughEvidence"])
        self.assertEqual(before["codex:mood-interrupted"]["signals"]["failure"], {
            "observed": True, "streak": 0, "failures": 3,
            "failuresLabel": "3+"})
        self.assertEqual(after, before)

    def test_mood_rotation_preserves_uncapped_and_append_order_authority(self):
        fixture_path = os.path.join(os.path.dirname(__file__), "fixtures",
                                    "mood-rotation-regressions.json")
        with open(fixture_path, encoding="utf-8") as stream:
            fixture = json.load(stream)
        base = {"v": 0, "source": "codex", "project": "burrow"}
        plain = fixture["plain"]
        plain_events = [
            {**base, "ts": plain["knock"], "agent_id": plain["agent_id"],
             "type": "needs_human", "payload": {"message": "legacy"}},
            {**base, "source": "steward", "ts": plain["failure"],
             "agent_id": plain["agent_id"], "type": "routine_failed",
             "payload": {"routine": "nightly", "run_id": "r", "error": "boom"}},
            {**base, "ts": plain["boundary"], "agent_id": plain["agent_id"],
             "type": "idle", "payload": {}},
        ]
        capacity = fixture["capacity"]
        capacity_events = [
            {**base, "ts": capacity["oldest"], "agent_id": capacity["agent_id"],
             "type": "needs_human", "payload": {"message": "Old",
                 "request_id": "oldest", "action": "deploy", "detail": None,
                 "options": ["approve", "deny"]}}
        ]
        newest = datetime.datetime.fromisoformat(
            fixture["now"].replace("Z", "+00:00"))
        for index in range(capacity["recent_count"]):
            when = newest - datetime.timedelta(
                minutes=capacity["recent_count"] - 1 - index)
            capacity_events.append({
                **base, "ts": when.isoformat(timespec="milliseconds").replace("+00:00", "Z"),
                "agent_id": capacity["agent_id"], "type": "needs_human",
                "payload": {"message": "Recent", "request_id": f"recent-{index}",
                            "action": "deploy", "detail": None,
                            "options": ["approve", "deny"]}})
        events = [*plain_events, *capacity_events]
        parsed = list(enumerate(events))
        selected_indexes = serve._mood_keep_indexes(
            parsed, {plain["agent_id"], capacity["agent_id"]})
        mood_selected = [events[index] for index in sorted(selected_indexes)]
        self.assertIn(2, selected_indexes,
                      "append-later idle accompanies a retained plain anchor")
        self.assertTrue(any(item["payload"].get("request_id") == "oldest"
                            for item in mood_selected))

        lines = [json.dumps(item, separators=(",", ":")) for item in events]
        now_ms = int(newest.timestamp() * 1000)
        rotated = serve.carry_forward(lines, now_ms)
        script = r"""
const projection=require('./viewer/projection.js');
const moods=require('./viewer/moods.js');
const batches=JSON.parse(require('fs').readFileSync(0,'utf8'));
const derive=lines=>Object.fromEntries(moods.deriveMoods(projection.parseEvents(lines)));
process.stdout.write(JSON.stringify(batches.map(derive)));
"""
        completed = subprocess.run(["node", "-e", script], cwd=os.path.dirname(
            os.path.dirname(__file__)), input=json.dumps([lines, rotated]),
            capture_output=True, text=True, check=True)
        before, after = json.loads(completed.stdout)
        self.assertEqual(after, before)
        self.assertFalse(after[plain["agent_id"]]["signals"]["unresolvedNeed"]["observed"])
        self.assertEqual(after[capacity["agent_id"]]["signals"]["unresolvedNeed"]["request_id"],
                         "oldest")
        self.assertEqual(after[capacity["agent_id"]]["status"], "blocked")

    def test_mood_rotation_completes_anchored_closes_and_cross_agent_collisions(self):
        """Python carries the same per-lifecycle authority the JS reducer uses."""
        agent = "codex:anchor"
        first = self.approval("r1", agent)
        first["ts"] = "2026-08-25T12:20:00.000Z"
        first_close = self.resolution("r1", agent)
        first_close["ts"] = "2026-08-25T12:10:00.000Z"
        second = self.approval("r2", agent)
        second["ts"] = "2026-08-25T12:11:00.000Z"
        second_close = self.resolution("r2", agent)
        second_close["ts"] = "2026-08-25T12:12:00.000Z"
        closed = [first, first_close, second, second_close]
        closed_indexes = serve._mood_keep_indexes(
            list(enumerate(closed)), {agent})
        self.assertIn(1, closed_indexes,
                      "the timestamp-anchor request carries its exact close")

        canonical = self.approval("shared", "codex:source", 10)
        collision = self.approval("shared", "codex:owner", 500)
        collision["payload"]["message"] = "Owner's incompatible question"
        owner_idle = event("codex:owner", "idle")
        collided = [canonical, collision, owner_idle]
        collided_indexes = serve._mood_keep_indexes(
            list(enumerate(collided)), {"codex:owner"})
        self.assertIn(0, collided_indexes,
                      "canonical cross-agent authority survives for the projected owner")
        self.assertIn(1, collided_indexes)

        script = r"""
const projection=require('./viewer/projection.js');
const moods=require('./viewer/moods.js');
const groups=JSON.parse(require('fs').readFileSync(0,'utf8'));
const derive=events=>Object.fromEntries(moods.deriveMoods(projection.parseEvents(events)));
process.stdout.write(JSON.stringify(groups.map(group=>[derive(group.full),derive(group.kept)])));
"""
        groups = [{"full": closed,
                   "kept": [closed[index] for index in sorted(closed_indexes)]},
                  {"full": collided,
                   "kept": [collided[index] for index in sorted(collided_indexes)]}]
        completed = subprocess.run(
            ["node", "-e", script], input=json.dumps(groups), text=True,
            cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            check=True, capture_output=True)
        (closed_before, closed_after), (collision_before, collision_after) = json.loads(
            completed.stdout)
        self.assertEqual(closed_after, closed_before)
        self.assertEqual(collision_after["codex:owner"],
                         collision_before["codex:owner"])
        self.assertEqual(collision_after["codex:owner"]["status"], "blocked")

    def test_mood_capsule_survives_two_stage_invalidation_and_cross_agent_reuse(self):
        """A rotation capsule replaces finite resolved-lifecycle fallback stacks."""
        anchor = datetime.datetime(2026, 8, 25, 20, tzinfo=datetime.timezone.utc)
        agent = "codex:capsule-python"
        base = {"v": 0, "source": "codex", "agent_id": agent,
                "project": "burrow"}

        def when(minutes):
            return (anchor + datetime.timedelta(minutes=minutes)).isoformat(
                timespec="milliseconds").replace("+00:00", "Z")

        def knock(index, request_id, owner=agent, action="deploy"):
            return {**base, "agent_id": owner, "ts": when(index),
                    "type": "needs_human", "payload": {"message": request_id,
                        "request_id": request_id, "action": action, "detail": None,
                        "options": ["approve", "deny"]}}

        def close(index, request_id):
            return {**base, "source": "steward", "ts": when(index),
                    "type": "needs_human_resolved", "payload": {
                        "request_id": request_id, "decision": "approve",
                        "decided_by": "human", "action": "deploy"}}

        first = []
        for index in range(81):
            first.extend([knock(index * 2, f"q{index}"),
                          close(index * 2 + 1, f"q{index}")])
        first_idle = {**base, "ts": when(163), "type": "idle", "payload": {}}
        first.append(first_idle)
        first_lines = list(map(lambda item: json.dumps(item, separators=(",", ":")), first))
        once = serve.carry_forward(first_lines, serve.event_ms(first_idle))
        self.assertEqual(sum("mood-authority-v1" in line for line in once), 1)

        later = [knock(200 + index, f"q{index}", action="erase")
                 for index in range(1, 81)]
        second_idle = {**base, "ts": when(300), "type": "idle", "payload": {}}
        later.append(second_idle)
        later_lines = list(map(lambda item: json.dumps(item, separators=(",", ":")), later))
        twice = serve.carry_forward([*once, *later_lines], serve.event_ms(second_idle))

        other = "codex:capsule-other"
        reuse = knock(301, "q0", owner=other, action="erase")
        other_idle = {**base, "agent_id": other, "ts": when(302),
                      "type": "idle", "payload": {}}
        thrice = serve.carry_forward([*twice, json.dumps(reuse, separators=(",", ":")),
                                      json.dumps(other_idle, separators=(",", ":"))],
                                     serve.event_ms(other_idle))
        script = r"""
const projection=require('./viewer/projection.js');
const moods=require('./viewer/moods.js');
const groups=JSON.parse(require('fs').readFileSync(0,'utf8'));
const derive=events=>Object.fromEntries(moods.deriveMoods(projection.parseEvents(events)));
process.stdout.write(JSON.stringify(groups.map(derive)));
"""
        full_second = [*first_lines, *later_lines]
        full_third = [*full_second, json.dumps(reuse, separators=(",", ":")),
                      json.dumps(other_idle, separators=(",", ":"))]
        completed = subprocess.run(["node", "-e", script], input=json.dumps(
            [full_second, twice, full_third, thrice]), text=True,
            cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            check=True, capture_output=True)
        full_two, rotated_two, full_three, rotated_three = json.loads(completed.stdout)
        self.assertEqual(rotated_two[agent], full_two[agent])
        self.assertEqual(rotated_two[agent]["status"], "authority history uncertain")
        self.assertIsNone(rotated_two[agent]["signals"]["interaction"]["kind"])
        self.assertEqual(rotated_three[other], full_three[other])
        self.assertEqual(rotated_three[other]["status"], "authority history uncertain")

    def test_mood_authority_overflow_is_bounded_fast_and_reclaimable(self):
        agent = "codex:authority-pressure"
        anchor = datetime.datetime(2026, 8, 25, 12, tzinfo=datetime.timezone.utc)
        base = {"v": 0, "source": "codex", "agent_id": agent,
                "project": "burrow"}
        count = 20_001
        events = [{**base,
            "ts": (anchor + datetime.timedelta(milliseconds=index)).isoformat(
                timespec="milliseconds").replace("+00:00", "Z"),
            "type": "needs_human", "payload": {"message": f"Request {index}",
                "request_id": f"bounded-{index}", "action": "deploy",
                "detail": None, "options": ["approve", "deny"]}}
            for index in range(count)]
        lines = list(map(json.dumps, events))
        started = time.monotonic()
        once = serve.carry_forward(lines, int(anchor.timestamp() * 1000) + count)
        elapsed = time.monotonic() - started
        self.assertLess(elapsed, 5, f"bounded rotation took {elapsed:.2f}s")
        self.assertLessEqual(len(once), serve.VIEWER_LINE_LIMIT)
        capsule = serve._mood_authority_from_line(once[0])
        self.assertTrue(capsule["overflow"])
        self.assertEqual(capsule["events"], [])
        self.assertEqual([capsule["ordinals"], capsule["copies"],
                          capsule["raw_ordinals"]], [[], [], []])
        self.assertLess(len(once[0].encode()), 32 * 1024)
        twice = serve.carry_forward(once, int(anchor.timestamp() * 1000) + count)
        self.assertEqual(twice, once, "a second rotation reclaims no extra authority")

        script = r"""
const p=require('./viewer/projection.js'),m=require('./viewer/moods.js');
const input=JSON.parse(require('node:fs').readFileSync(0,'utf8'));
const read=lines=>Object.fromEntries(m.deriveMoods(p.parseEvents(lines)));
process.stdout.write(JSON.stringify([read(input.full),read(input.retained)]));
"""
        completed = subprocess.run(["node", "-e", script], input=json.dumps({
            "full": lines, "retained": once}), text=True,
            cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            check=True, capture_output=True)
        full, retained = json.loads(completed.stdout)
        self.assertEqual(retained[agent], full[agent])
        self.assertEqual(full[agent]["status"], "authority history uncertain")

    def test_mood_capsule_encoded_boundary_and_rotation_reclamation(self):
        anchor = datetime.datetime(2026, 8, 25, 12, tzinfo=datetime.timezone.utc)
        base = {"v": 0, "source": "codex", "agent_id": "codex:byte-boundary",
                "project": "burrow"}

        def timestamp(index):
            return (anchor + datetime.timedelta(milliseconds=index)).isoformat(
                timespec="milliseconds").replace("+00:00", "Z")

        def lines(count):
            result = []
            for index in range(count):
                detail = {"pad": "x" * 50}
                request_id = f"bounded-{index}"
                result.extend([
                    json.dumps({**base, "ts": timestamp(index * 2),
                        "type": "needs_human", "payload": {"message": f"r{index}",
                            "request_id": request_id, "action": "deploy",
                            "detail": detail, "options": ["approve", "deny"]}},
                        separators=(",", ":")),
                    json.dumps({**base, "source": "steward",
                        "ts": timestamp(index * 2 + 1),
                        "type": "needs_human_resolved", "payload": {
                            "request_id": request_id, "decision": "approve",
                            "decided_by": "human", "action": "deploy",
                            "detail": detail}}, separators=(",", ":")),
                ])
            return result

        boundary = next(count for count in range(1, 200)
                        if serve._mood_authority_from_line(serve.carry_forward(
                            lines(count), int(anchor.timestamp() * 1000) + 1000)[0])["overflow"])
        below = lines(boundary - 1)
        rotated_below = serve.carry_forward(below, int(anchor.timestamp() * 1000) + 1000)
        capsule_below = serve._mood_authority_from_line(rotated_below[0])
        self.assertFalse(capsule_below["overflow"])
        self.assertLessEqual(len(rotated_below[0].encode()),
                             serve.MOOD_AUTHORITY_MAX_BYTES)
        self.assertGreater(len(rotated_below[0].encode()), 32_000,
                           "exercise the actual near-ceiling capsule")

        above = lines(boundary)
        rotated_above = serve.carry_forward(above, int(anchor.timestamp() * 1000) + 1000)
        capsule_above = serve._mood_authority_from_line(rotated_above[0])
        self.assertTrue(capsule_above["overflow"])
        self.assertEqual([capsule_above["events"], capsule_above["ordinals"],
                          capsule_above["copies"], capsule_above["raw_ordinals"]],
                         [[], [], [], []])
        self.assertLess(sum(len(line.encode()) + 1 for line in rotated_above),
                        .9 * sum(len(line.encode()) + 1 for line in above))

    def test_typed_binary64_authority_matches_javascript_for_property_fixture(self):
        fixture_path = os.path.join(os.path.dirname(__file__), "fixtures",
                                    "mood-capsule-parity.json")
        with open(fixture_path, encoding="utf-8") as stream:
            bits = json.load(stream)["finite_binary64_bits"]
        import struct
        values = [struct.unpack(">d", bytes.fromhex(token))[0] for token in bits]
        python = [serve._canonical_identity(value) for value in values]
        script = r"""
const t=require('./viewer/typed-json.js'),bits=JSON.parse(require('fs').readFileSync(0,'utf8'));
const values=bits.map(hex=>new DataView(Uint8Array.from(hex.match(/../g),x=>parseInt(x,16)).buffer).getFloat64(0,false));
process.stdout.write(JSON.stringify(values.map(t.identity)));
"""
        completed = subprocess.run(["node", "-e", script], input=json.dumps(bits),
            text=True, cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            check=True, capture_output=True)
        self.assertEqual(json.loads(completed.stdout), python)
        self.assertEqual(serve._canonical_identity(-0.0),
                         serve._canonical_identity(0.0))
        self.assertEqual(serve._canonical_identity(float("inf")),
                         serve._canonical_identity(float("-inf")))

    def test_deep_approval_authority_rotates_iteratively_and_preserves_raw(self):
        depth = 600
        detail = '{"child":' * depth + 'null' + '}' * depth
        request = ('{"v":0,"ts":"2026-08-25T10:00:00.000Z","source":"codex",'
                   '"agent_id":"codex:deep","project":"burrow","type":"needs_human",'
                   '"payload":{"message":"deep","request_id":"deep","action":"deploy",'
                   '"detail":' + detail + ',"options":["approve"]}}')
        close = ('{"v":0,"ts":"2026-08-25T10:01:00.000Z","source":"steward",'
                 '"agent_id":"codex:deep","project":"burrow",'
                 '"type":"needs_human_resolved","payload":{"request_id":"deep",'
                 '"decision":"approve","decided_by":"api","action":"deploy"}}')
        now_ms = serve.event_ms(json.loads(close))
        once = serve.carry_forward([request, close], now_ms)
        capsule = serve._mood_authority_from_line(once[0])
        self.assertIsNotNone(capsule)
        self.assertFalse(capsule["overflow"])
        self.assertEqual([line for line in once if line in {request, close}], [request, close])
        self.assertEqual(serve.carry_forward(once, now_ms), once)

    def test_every_carry_forward_rescan_rejects_nonstandard_json_constants(self):
        root = event("codex:strict", "task_started", prompt="kept")
        root["source"] = "codex"
        valid = json.dumps(root, separators=(",", ":"))
        malformed = valid.replace('"v":0', '"v":NaN')
        for lines in ([malformed, valid], [valid, malformed, valid], [valid, malformed]):
            rotated = serve.carry_forward(lines, serve.event_ms(root))
            self.assertEqual(protocol_events(rotated), [root] * lines.count(valid))
            self.assertFalse(any("NaN" in line or "Infinity" in line for line in rotated))
            self.assertEqual(serve.carry_forward(rotated, serve.event_ms(root)), rotated)

    def test_overflowing_exponent_identity_survives_future_reuse_and_two_rotations(self):
        def request(ts):
            return ('{"v":0,"ts":"' + ts + '","source":"codex",'
                    '"agent_id":"codex:exponent","project":"burrow",'
                    '"type":"needs_human","payload":{"message":"same",'
                    '"request_id":"exponent","action":"deploy",'
                    '"detail":{"n":1e400},"options":["approve"]}}')
        close = ('{"v":0,"ts":"2026-08-25T10:01:00.000Z","source":"steward",'
                 '"agent_id":"codex:exponent","project":"burrow",'
                 '"type":"needs_human_resolved","payload":{"request_id":"exponent",'
                 '"decision":"approve","decided_by":"api","action":"deploy"}}')
        now_ms = serve.event_ms(json.loads(close))
        once = serve.carry_forward([request("2026-08-25T10:00:00.000Z"), close], now_ms)
        twice = serve.carry_forward([*once, request("2026-08-25T10:02:00.000Z")], now_ms + 60_000)
        thrice = serve.carry_forward(twice, now_ms + 60_000)
        self.assertEqual(thrice, twice)
        script = r"""
const p=require('./viewer/projection.js'),a=require('./viewer/approval-knocks.js');
const lines=JSON.parse(require('fs').readFileSync(0,'utf8')),batch=p.parseEvents(lines),state=a.createState();
a.foldValidated(state,batch,{isValidatedBatch:p.isValidatedBatch,rejections:p.approvalRejections(batch)});
const record=state.requests.get('exponent');process.stdout.write(JSON.stringify({collision:record.collided,resolved:!!record.resolution}));
"""
        completed = subprocess.run(["node", "-e", script], input=json.dumps(thrice),
            text=True, cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            check=True, capture_output=True)
        self.assertEqual(json.loads(completed.stdout), {"collision": False, "resolved": True})

    def test_numeric_capsule_boundary_matches_javascript_before_and_after_rotation(self):
        anchor = datetime.datetime(2026, 8, 25, 12, tzinfo=datetime.timezone.utc)
        base = {"v": 0, "source": "codex", "agent_id": "codex:numeric-boundary",
                "project": "burrow"}

        def timestamp(index):
            return (anchor + datetime.timedelta(milliseconds=index)).isoformat(
                timespec="milliseconds").replace("+00:00", "Z")

        def lifecycle(index):
            detail = {"numbers": [1.0, -0.0, 1e-7, 1e20, 1e21,
                                  {"fraction": 1.25e-12, "integer": 42.0}]}
            request_id = f"numeric-{index}"
            return [
                {**base, "ts": timestamp(index * 2), "type": "needs_human",
                 "payload": {"message": request_id, "request_id": request_id,
                             "action": "deploy", "detail": detail,
                             "options": ["approve", "deny"]}},
                {**base, "source": "steward", "ts": timestamp(index * 2 + 1),
                 "type": "needs_human_resolved", "payload": {
                     "request_id": request_id, "decision": "approve",
                     "decided_by": "human", "action": "deploy", "detail": detail}},
            ]

        groups = []
        boundary = next(count for count in range(1, 150)
                        if serve._mood_authority_from_line(serve.carry_forward(
                            [json.dumps(item, separators=(",", ":"))
                             for index in range(count) for item in lifecycle(index)],
                            int(anchor.timestamp() * 1000) + 1000)[0])["overflow"])
        for count in (boundary - 1, boundary):
            events = [item for index in range(count) for item in lifecycle(index)]
            lines = [json.dumps(item, separators=(",", ":")) for item in events]
            groups.append({"full": lines, "rotated": serve.carry_forward(
                lines, int(anchor.timestamp() * 1000) + 1000)})
        script = r"""
const p=require('./viewer/projection.js'),m=require('./viewer/moods.js');
const groups=JSON.parse(require('node:fs').readFileSync(0,'utf8'));
const inspect=lines=>{const parsed=p.parseEvents(lines),kept=m.retainMoodWitnesses(parsed),
  state=p.moodAuthorityState(kept); return {mood:Object.fromEntries(m.deriveMoods(parsed)),
  overflow:state.overflow,bytes:p.moodAuthorityCapsuleByteLength(state.events,state.copies,state)}};
process.stdout.write(JSON.stringify(groups.map(group=>({js:inspect(group.full),
  rotated:inspect(group.rotated)}))));
"""
        completed = subprocess.run(["node", "-e", script], input=json.dumps(groups),
            text=True, cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            check=True, capture_output=True)
        results = json.loads(completed.stdout)
        for result in results:
            self.assertEqual(result["rotated"]["overflow"], result["js"]["overflow"])
            self.assertEqual(result["rotated"]["bytes"], result["js"]["bytes"])
            self.assertEqual(result["rotated"]["mood"], result["js"]["mood"])
        self.assertFalse(results[0]["js"]["overflow"])
        self.assertTrue(results[1]["js"]["overflow"])

    def test_capsule_manifest_excludes_non_mood_projection_records(self):
        """Approval/task/presence retention cannot consume Mood capsule bytes."""
        anchor = datetime.datetime(2026, 8, 25, 12, tzinfo=datetime.timezone.utc)
        base = {"v": 0, "source": "codex", "agent_id": "codex:manifest",
                "project": "burrow"}
        events = []
        for index in range(50):
            when = (anchor + datetime.timedelta(milliseconds=index * 3))
            request_id = f"manifest-{index}"
            detail = {"pad": "x" * 38}
            events.extend([
                {**base, "ts": when.isoformat(timespec="milliseconds").replace("+00:00", "Z"),
                 "type": "needs_human", "payload": {"message": request_id,
                     "request_id": request_id, "action": "deploy", "detail": detail,
                     "options": ["approve", "deny"]}},
                {**base, "source": "steward",
                 "ts": (when + datetime.timedelta(milliseconds=1)).isoformat(
                     timespec="milliseconds").replace("+00:00", "Z"),
                 "type": "needs_human_resolved", "payload": {"request_id": request_id,
                     "decision": "approve", "decided_by": "human",
                     "action": "deploy", "detail": detail}},
            ])
        # These records survive other projections, but none is an additional
        # Mood witness beyond the independently selected anchor/work evidence.
        events.extend([
            {**base, "ts": (anchor + datetime.timedelta(seconds=1)).isoformat(
                timespec="milliseconds").replace("+00:00", "Z"),
             "type": "tool_called", "payload": {"tool": "Read"}},
            {**base, "agent_id": "steward", "source": "steward",
             "ts": (anchor + datetime.timedelta(seconds=2)).isoformat(
                timespec="milliseconds").replace("+00:00", "Z"),
             "type": "task_posted", "payload": {"task_id": "other-projection",
                "subject": "Retained task", "description": "not Mood authority"}},
        ])
        lines = [json.dumps(item, separators=(",", ":")) for item in events]
        rotated = serve.carry_forward(lines, int(anchor.timestamp() * 1000) + 3000)
        script = r"""
const p=require('./viewer/projection.js'),m=require('./viewer/moods.js');
const input=JSON.parse(require('node:fs').readFileSync(0,'utf8'));
const read=lines=>Object.fromEntries(m.deriveMoods(p.parseEvents(lines)));
const retained=m.retainMoodWitnesses(p.parseEvents(input.full));
const js=p.moodAuthorityState(retained),py=p.moodAuthorityState(p.parseEvents(input.rotated));
process.stdout.write(JSON.stringify({before:read(input.full),after:read(input.rotated),
  js:{overflow:js.overflow,raw:js.rawOrdinals.length,bytes:p.moodAuthorityCapsuleByteLength(js.events,js.copies,js)},
  py:{overflow:py.overflow,raw:py.rawOrdinals.length,bytes:p.moodAuthorityCapsuleByteLength(py.events,py.copies,py)}}));
"""
        completed = subprocess.run(["node", "-e", script], input=json.dumps(
            {"full": lines, "rotated": rotated}), text=True,
            cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            check=True, capture_output=True)
        result = json.loads(completed.stdout)
        self.assertEqual(result["after"], result["before"])
        self.assertEqual(result["py"], result["js"])
        self.assertFalse(result["js"]["overflow"])

    def test_plain_and_malformed_fallback_rotation_scales_linearly(self):
        anchor = datetime.datetime(2026, 8, 25, 12, tzinfo=datetime.timezone.utc)
        base = {"v": 0, "source": "codex", "agent_id": "codex:plain-pressure",
                "project": "burrow", "type": "needs_human"}
        lines = []
        for index in range(6000):
            payload = ({"message": f"plain {index}"} if index % 2 else
                       {"message": f"fallback {index}", "request_id": f"bad-{index}"})
            event = {**base, "ts": (anchor + datetime.timedelta(milliseconds=index))
                     .isoformat(timespec="milliseconds").replace("+00:00", "Z"),
                     "payload": payload}
            lines.append(json.dumps(event, separators=(",", ":")))
        started = time.monotonic()
        serve.carry_forward(lines, int(anchor.timestamp() * 1000) + 7000)
        self.assertLess(time.monotonic() - started, 2.5)

    def test_shared_adversarial_mood_lifecycles_survive_python_rotation(self):
        fixture_path = os.path.join(os.path.dirname(__file__), "fixtures",
                                    "mood-lifecycle-adversarial.json")
        with open(fixture_path, encoding="utf-8") as stream:
            fixture = json.load(stream)
        events = fixture["events"]
        mood_indexes = serve._mood_keep_indexes(
            list(enumerate(events)), {"codex:close", "codex:owner"})
        self.assertIn(0, mood_indexes,
                      "Mood's exact q1 close independently carries its canonical knock")
        self.assertIn(6, mood_indexes,
                      "Mood independently carries the owner's cross-agent canonical truth")
        lines = [json.dumps(item, separators=(",", ":")) for item in events]
        now_ms = int(datetime.datetime.fromisoformat(
            fixture["now"].replace("Z", "+00:00")).timestamp() * 1000)
        rotated = serve.carry_forward(lines, now_ms)
        decoded = [json.loads(line) for line in rotated]
        self.assertIn(events[0], decoded,
                      "the retained q1 close carries its displaced canonical knock")
        self.assertIn(events[6], decoded,
                      "the projected collision owner carries cross-agent canonical truth")

        script = r"""
const projection=require('./viewer/projection.js');
const moods=require('./viewer/moods.js');
const batches=JSON.parse(require('fs').readFileSync(0,'utf8'));
const derive=events=>Object.fromEntries(moods.deriveMoods(projection.parseEvents(events)));
process.stdout.write(JSON.stringify(batches.map(derive)));
"""
        completed = subprocess.run(
            ["node", "-e", script], input=json.dumps([events, decoded]), text=True,
            cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            check=True, capture_output=True)
        before, after = json.loads(completed.stdout)
        self.assertEqual(after["codex:close"], before["codex:close"])
        self.assertEqual(after["codex:owner"], before["codex:owner"])
        self.assertEqual(after["codex:close"]["signals"]["interaction"]["kind"],
                         "approval decision")
        self.assertEqual(after["codex:owner"]["signals"]["unresolvedNeed"]["kind"],
                         "structured collision")

    def test_shared_capsule_raw_append_order_survives_repeated_rotation(self):
        fixture_path = os.path.join(os.path.dirname(__file__), "fixtures",
                                    "mood-authority-order.json")
        with open(fixture_path, encoding="utf-8") as stream:
            fixture = json.load(stream)
        lines = list(map(json.dumps, fixture["events"]))
        now_ms = int(datetime.datetime.fromisoformat(
            fixture["now"].replace("Z", "+00:00")).timestamp() * 1000)
        once = serve.carry_forward(lines, now_ms)
        twice = serve.carry_forward(once, now_ms)
        script = r"""
const p=require('./viewer/projection.js'),m=require('./viewer/moods.js');
const groups=JSON.parse(require('node:fs').readFileSync(0,'utf8'));
const read=lines=>Object.fromEntries(m.deriveMoods(p.parseEvents(lines)));
process.stdout.write(JSON.stringify(groups.map(read)));
"""
        completed = subprocess.run(["node", "-e", script],
            input=json.dumps([lines, once, twice]), text=True,
            cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            check=True, capture_output=True)
        full, rotated, rerotated = json.loads(completed.stdout)
        self.assertEqual(rotated, full)
        self.assertEqual(rerotated, full)
        self.assertEqual(full["codex:orphan-first"]["signals"]["unresolvedNeed"]["request_id"],
                         "late")
        self.assertFalse(full["codex:plain-root"]["signals"]["unresolvedNeed"]["observed"])
        self.assertEqual(full["codex:multiple-roots"]["signals"]["interaction"]["level"],
                         "aging")

    def test_unsafe_mood_capsules_are_ignored_atomically(self):
        root = event("codex:owner", "task_started", prompt="Hello")
        root["source"] = "codex"
        other = copy.deepcopy(root); other["agent_id"] = "codex:other"

        def capsule(**changes):
            value = {"_burrow_internal": serve.MOOD_AUTHORITY_KIND,
                     "events": [root], "ordinals": ["7"],
                     "copies": ["7"], "raw_ordinals": ["7"],
                     "raw_indexes": ["0000000000000000"],
                     "raw_count": "0000000000000001",
                     "overflow": False, "observed": 1}
            value.update(changes)
            return json.dumps(value)

        bad = [
            capsule(copies=["7", "7"]),
            capsule(ordinals=["01"]),
            capsule(raw_indexes=[]),
            capsule(raw_indexes=["0000000000000001"]),
            capsule(raw_count="1"),
        ]
        for unsafe in bad:
            retained = serve.carry_forward([unsafe, json.dumps(root)], serve.event_ms(root))
            self.assertEqual(protocol_events(retained).count(root), 1)
            rebuilt = serve._mood_authority_from_line(retained[0])
            self.assertNotEqual(rebuilt.get("copies"), json.loads(unsafe).get("copies"))
        cross = serve.carry_forward([capsule(), json.dumps(other)], serve.event_ms(other))
        self.assertEqual(protocol_events(cross).count(other), 1)
        retained = serve.carry_forward([capsule(copies=[], raw_ordinals=[]),
            capsule(copies=[], raw_ordinals=[]), json.dumps(root)], serve.event_ms(root))
        self.assertEqual(protocol_events(retained).count(root), 1)
        malformed = json.dumps({"_burrow_internal": serve.MOOD_AUTHORITY_KIND,
                                "events": "broken"})
        for records in (["not-json", capsule(), json.dumps(root)],
                        [malformed, json.dumps(root)],
                        [capsule(copies=[], raw_ordinals=[]), malformed,
                         json.dumps(root)]):
            rotated = serve.carry_forward(records, serve.event_ms(root))
            rebuilt = serve._mood_authority_from_line(rotated[0])
            self.assertEqual(rebuilt.get("ordinals"), ["0"],
                             "invalid physical placement/marker rebuilds only raw authority")
            self.assertEqual(protocol_events(rotated).count(root), 1)
        for field in ("events", "ordinals", "copies", "raw_ordinals", "raw_indexes"):
            nonempty = {"_burrow_internal": serve.MOOD_AUTHORITY_KIND,
                        "events": [], "ordinals": [], "copies": [],
                        "raw_ordinals": [], "raw_indexes": [],
                        "raw_count": "0000000000000000",
                        "overflow": True, "observed": 257}
            nonempty[field] = [root] if field == "events" else ["7"]
            self.assertIsNone(serve._mood_authority_from_line(json.dumps(nonempty)))
        self.assertIsNone(serve._mood_authority_from_line(json.dumps({
            "_burrow_internal": serve.MOOD_AUTHORITY_KIND,
            "events": [], "ordinals": [], "copies": [], "raw_ordinals": [],
            "raw_indexes": [], "raw_count": "0000000000000001",
            "overflow": True, "observed": 257})))
        oversized = capsule(pad="x" * serve.MOOD_AUTHORITY_MAX_BYTES)
        self.assertIsNone(serve._mood_authority_from_line(oversized))
        numeric = copy.deepcopy(root); numeric["payload"]["n"] = 1.0
        integer = copy.deepcopy(root); integer["payload"]["n"] = 1
        self.assertEqual(serve._canonical_identity(numeric),
                         serve._canonical_identity(integer))

    def test_capsule_authority_is_the_exact_canonical_source_epoch_fold(self):
        agent = "codex:exact-authority"
        request = event(agent, "needs_human", 30, message="choose",
                        request_id="exact", action="deploy", detail=None,
                        options=["approve", "deny"])
        resolution = event(agent, "needs_human_resolved", 20,
                           request_id="exact", decision="approve",
                           decided_by="human", action="deploy")
        resolution["source"] = "steward"
        capsule = {"_burrow_internal": serve.MOOD_AUTHORITY_KIND,
                   "events": [request], "ordinals": ["5"], "copies": [],
                   "raw_ordinals": ["10"],
                   "raw_indexes": ["0000000000000000"],
                   "raw_count": "0000000000000001", "overflow": False,
                   "observed": 1}
        self.assertFalse(serve._exact_capsule_authority(capsule, [resolution]),
                         "an injected later close cannot be omitted from authority")
        rotated = serve.carry_forward([serve._encode_mood_authority(capsule),
                                       json.dumps(resolution)],
                                      serve.event_ms(resolution))
        rebuilt = (serve._mood_authority_from_line(rotated[0])
                   if rotated and serve.MOOD_AUTHORITY_KIND in rotated[0] else None)
        self.assertFalse(rebuilt and any(item == request
                                        for item in rebuilt["events"]))

        omitted_copy = dict(capsule, raw_ordinals=[], raw_indexes=[])
        omitted_rotated = serve.carry_forward([
            serve._encode_mood_authority(omitted_copy), json.dumps(request)],
            serve.event_ms(request))
        omitted_rebuilt = serve._mood_authority_from_line(omitted_rotated[0])
        self.assertEqual(omitted_rebuilt["copies"], ["0"],
                         "omitted raw authority copy is rebuilt from raw truth")

        plain = event(agent, "needs_human", 40, message="legacy")
        orphan = copy.deepcopy(resolution)
        orphan["payload"]["request_id"] = "orphan"
        for item in (plain, orphan):
            claimed = dict(capsule, events=[item], ordinals=["5"], copies=[],
                           raw_ordinals=[], raw_indexes=[],
                           raw_count="0000000000000000")
            self.assertFalse(serve._exact_capsule_authority(claimed, []))

        valid = dict(capsule, events=[request, resolution],
                     ordinals=["5", "10"], copies=["5"],
                     raw_ordinals=["5"], observed=2)
        self.assertTrue(serve._exact_capsule_authority(valid, [request]),
                        "a valid co-retained authority copy stays accepted")
        reordered = dict(valid, events=[orphan, request], ordinals=["4", "5"],
                         copies=[], raw_ordinals=[], raw_indexes=[],
                         raw_count="0000000000000000")
        self.assertFalse(serve._exact_capsule_authority(reordered, []))

    def test_sparse_capsule_completeness_preserves_every_mood_witness(self):
        authority = event("codex:authority", "task_started", 200,
                          prompt="authority")
        authority["source"] = "codex"
        anchor = event("codex:proof", "idle")
        failure = event("codex:proof", "tool_failed", 60,
                        tool="Bash", error="boom")
        root = event("codex:proof", "task_started", 60, prompt="human")
        root["source"] = "codex"
        knock = event("codex:proof", "needs_human", 60, message="choose",
                      request_id="pending", action="deploy", detail=None,
                      options=["approve", "deny"])
        claimed = event("codex:proof", "task_claimed", 60, task_id="t",
                        title="Task", claimant="codex:proof")
        claimed["source"] = "steward"
        for witness in (failure, root, knock, claimed):
            capsule = {"_burrow_internal": serve.MOOD_AUTHORITY_KIND,
                       "events": [authority], "ordinals": ["0"], "copies": [],
                       "raw_ordinals": ["2"],
                       "raw_indexes": ["0000000000000001"],
                       "raw_count": "0000000000000002", "overflow": False,
                       "observed": 1}
            rotated = serve.carry_forward(
                [serve._encode_mood_authority(capsule), json.dumps(witness),
                 json.dumps(anchor)], serve.event_ms(anchor))
            self.assertIn(witness, protocol_events(rotated), witness["type"])

        irrelevant = event("codex:proof", "idle", 120)
        safe = {"_burrow_internal": serve.MOOD_AUTHORITY_KIND,
                "events": [authority], "ordinals": ["0"], "copies": [],
                "raw_ordinals": ["2"], "raw_indexes": ["0000000000000001"],
                "raw_count": "0000000000000002", "overflow": False,
                "observed": 1}
        rotated = serve.carry_forward([serve._encode_mood_authority(safe),
            json.dumps(irrelevant), json.dumps(anchor)], serve.event_ms(anchor))
        self.assertIsNotNone(serve._mood_authority_from_line(rotated[0]))

    def test_sparse_capsule_manifest_rejects_surplus_indexes_exactly(self):
        authority = event("codex:authority", "task_started", 240,
                          prompt="unrelated authority")
        authority["source"] = "codex"
        plain = event("codex:proof", "needs_human", 120, message="plain")
        idle = event("codex:proof", "idle", 60)
        terminal = event("codex:proof", "routine_finished", routine="r",
                         run_id="r-surplus", outcome="ok", artifacts=[], duration_s=1)
        terminal["source"] = "steward"
        capsule = {"_burrow_internal": serve.MOOD_AUTHORITY_KIND,
                   "events": [authority], "ordinals": ["0"], "copies": [],
                   "raw_ordinals": ["1", "3"],
                   "raw_indexes": ["0000000000000000", "0000000000000002"],
                   "raw_count": "0000000000000003", "overflow": False,
                   "observed": 1}
        raw_lines = [json.dumps(plain), json.dumps(idle), json.dumps(terminal)]
        attacked = serve.carry_forward(
            [serve._encode_mood_authority(capsule), *raw_lines],
            serve.event_ms(terminal))
        expected = serve.carry_forward(raw_lines, serve.event_ms(terminal))
        self.assertEqual(attacked, expected,
                         "surplus manifest is discarded atomically before rotation")

        accepted = {**capsule, "raw_ordinals": ["3"],
                    "raw_indexes": ["0000000000000002"]}
        accepted_lines = serve.carry_forward(
            [serve._encode_mood_authority(accepted), *raw_lines],
            serve.event_ms(terminal))
        self.assertIsNotNone(serve._mood_authority_from_line(accepted_lines[0]),
                             "exact sparse manifest remains valid with irrelevant co-retained records")

    def test_capsule_duplicate_wire_keys_reject_atomically(self):
        root = event("codex:duplicate", "task_started", prompt="raw")
        root["source"] = "codex"
        base = json.dumps({"_burrow_internal": serve.MOOD_AUTHORITY_KIND,
            "events": [root], "ordinals": ["0"], "copies": ["0"],
            "raw_ordinals": ["0"], "raw_indexes": ["0000000000000000"],
            "raw_count": "0000000000000001", "overflow": False,
            "observed": 1}, separators=(",", ":"))
        attacks = [
            base.replace('{"_burrow_internal":',
                '{"_burrow_internal":"mood-authority-v1","_burrow_internal":'),
            base.replace('"events":', '"events":[],"events":'),
            base.replace('"type":"task_started"',
                         '"type":"idle","type":"task_started"'),
        ]
        for line in attacks:
            self.assertIsNone(serve._mood_authority_from_line(line))
            rotated = serve.carry_forward([line, json.dumps(root)],
                                          serve.event_ms(root))
            self.assertIn(root, protocol_events(rotated))

    def test_safe_ordinal_exhaustion_and_capsule_only_two_rotation_boundary(self):
        agent = "codex:max-safe"
        knock = event(agent, "needs_human", 20, message="choose",
                      request_id="max", action="deploy", detail=None,
                      options=["approve", "deny"])
        close = event(agent, "needs_human_resolved", 10, request_id="max",
                      decision="approve", decided_by="human", action="deploy")
        close["source"] = "steward"
        capsule = {"_burrow_internal": serve.MOOD_AUTHORITY_KIND,
                   "events": [knock, close],
                   "ordinals": ["9007199254740990", "9007199254740991"],
                   "copies": [], "raw_ordinals": [], "raw_indexes": [],
                   "raw_count": "0000000000000000", "overflow": False,
                   "observed": 2}
        once = serve.carry_forward([serve._encode_mood_authority(capsule)],
                                   serve.event_ms(close))
        twice = serve.carry_forward(once, serve.event_ms(close))
        self.assertFalse(serve._mood_authority_from_line(once[0])["overflow"])
        self.assertFalse(serve._mood_authority_from_line(twice[0])["overflow"])
        appended = event(agent, "idle")
        exhausted = serve.carry_forward(
            [serve._encode_mood_authority(capsule), json.dumps(appended)],
            serve.event_ms(appended))
        rebuilt = serve._mood_authority_from_line(exhausted[0])
        self.assertTrue(rebuilt["overflow"])
        self.assertEqual([rebuilt["events"], rebuilt["ordinals"],
                          rebuilt["copies"], rebuilt["raw_ordinals"],
                          rebuilt["raw_indexes"]], [[], [], [], [], []])
        self.assertIn(appended, protocol_events(exhausted))

    def test_shared_capsule_field_domain_matrix_is_strict_and_non_throwing(self):
        fixture_path = os.path.join(os.path.dirname(__file__), "fixtures",
                                    "mood-capsule-malformed.json")
        with open(fixture_path, encoding="utf-8") as stream:
            matrix = json.load(stream)
        base = {"_burrow_internal": serve.MOOD_AUTHORITY_KIND,
                "events": [matrix["authority_event"]], "ordinals": ["0"],
                "copies": [], "raw_ordinals": [], "raw_indexes": [],
                "raw_count": "0000000000000000", "overflow": False,
                "observed": 1}
        raw = json.dumps(matrix["raw_event"], separators=(",", ":"))

        def assert_ignored(line):
            rotated = serve.carry_forward([line, raw],
                                          serve.event_ms(matrix["raw_event"]))
            self.assertEqual(protocol_events(rotated), [matrix["raw_event"]])
            self.assertFalse(any(serve._mood_authority_marker(json.loads(item))
                                 for item in rotated))

        for field, value in matrix["invalid_mutations"]:
            malformed = copy.deepcopy(base)
            malformed[field] = value
            assert_ignored(json.dumps(malformed, separators=(",", ":")))
        encoded = json.dumps(base, separators=(",", ":"))
        for token in matrix["nonstandard_observed_tokens"]:
            assert_ignored(encoded.replace('"observed":1', f'"observed":{token}'))
        integral = encoded.replace('"observed":1', '"observed":1.0')
        rotated = serve.carry_forward([integral, raw],
                                      serve.event_ms(matrix["raw_event"]))
        self.assertEqual(serve._mood_authority_from_line(rotated[0])["events"],
                         [matrix["authority_event"]])
        assert_ignored_multi = serve.carry_forward([encoded, encoded, raw],
                                                   serve.event_ms(matrix["raw_event"]))
        self.assertEqual(protocol_events(assert_ignored_multi), [matrix["raw_event"]])

    def test_shared_capsule_schema_and_typed_graph_attacks_preserve_raw_moods(self):
        fixture_path = os.path.join(os.path.dirname(__file__), "fixtures",
                                    "mood-capsule-malformed.json")
        with open(fixture_path, encoding="utf-8") as stream:
            matrix = json.load(stream)
        second = copy.deepcopy(matrix["authority_event"])
        second.update({"ts": "2026-08-25T10:30:00.000Z",
                       "agent_id": "codex:capsule-second",
                       "payload": {"prompt": "Second retained root"}})
        raw_events = [matrix["authority_event"], second, matrix["raw_event"]]
        raw = [json.dumps(item, separators=(",", ":")) for item in raw_events]
        logical = {"events": raw_events[:2], "ordinals": ["1", "2"],
                   "copies": ["1", "2"], "raw_ordinals": ["1", "2", "3"],
                   "raw_indexes": ["0000000000000000", "0000000000000001",
                                   "0000000000000002"],
                   "raw_count": "0000000000000003", "overflow": False,
                   "observed": 2}

        def direct(value):
            return json.dumps({"_burrow_internal": serve.MOOD_AUTHORITY_KIND, **value},
                              separators=(",", ":"))

        def envelope(value, outer=None):
            record = json.loads(serve._encode_mood_authority(
                {"_burrow_internal": serve.MOOD_AUTHORITY_KIND, **value}))
            record.update(outer or {})
            return json.dumps(record, separators=(",", ":"))

        cases = []

        def ignored(line, name):
            self.assertIsNone(serve._mood_authority_from_line(line), name)
            rotated = serve.carry_forward([line, *raw], serve.event_ms(raw_events[-1]))
            self.assertCountEqual(protocol_events(rotated), raw_events,
                                  f"{name}: all raw public evidence survives")
            cases.append(rotated)

        self.assertEqual(len(serve._mood_authority_from_line(envelope(logical))["events"]), 2)
        for mutation in matrix["canonical_schema_mutations"]:
            changed = copy.deepcopy(logical)
            changed[mutation["field"]] = mutation["value"]
            ignored(direct(changed), mutation["name"] + " direct root")
            ignored(envelope(changed), mutation["name"] + " encoded state")
        missing = copy.deepcopy(logical)
        del missing["copies"]
        ignored(direct(missing), "missing direct root field")
        ignored(envelope(missing), "missing encoded state field")
        ignored(envelope(logical, {"surplus": True}), "surplus envelope field")

        def clone_graph():
            return copy.deepcopy(approval_protocol.json_typed_graph(logical))

        def references(node):
            if node[0] == "a":
                return node[1]
            if node[0] == "o":
                return [entry[1] for entry in node[1]]
            return []

        unused = clone_graph()
        unused[0].insert(unused[1], ["s", "unused"])
        unused[1] += 1

        shared_scalar = clone_graph()
        prompt = next(index for index, node in enumerate(shared_scalar[0])
                      if node == ["s", "Retained root"])
        source = next(index for index, node in enumerate(shared_scalar[0])
                      if node == ["s", "codex:capsule-authority"])
        scalar_parent = next(node for node in shared_scalar[0]
                             if prompt in references(node))
        if scalar_parent[0] == "a":
            scalar_parent[1][scalar_parent[1].index(prompt)] = source
        else:
            next(entry for entry in scalar_parent[1] if entry[1] == prompt)[1] = source

        shared_container = clone_graph()
        root_entries = shared_container[0][shared_container[1]][1]
        event_array_index = next(entry[1] for entry in root_entries
                                 if entry[0] == "events")
        shared_container[0][event_array_index][1][1] = \
            shared_container[0][event_array_index][1][0]

        amplified = clone_graph()
        leaf = next(index for index, node in enumerate(amplified[0])
                    if node == ["s", "Retained root"])
        depth = matrix["amplification_depth"]
        for node in amplified[0]:
            if node[0] == "a":
                node[1] = [index + depth if index > leaf else index
                           for index in node[1]]
            elif node[0] == "o":
                for entry in node[1]:
                    if entry[1] > leaf:
                        entry[1] += depth
        if amplified[1] > leaf:
            amplified[1] += depth
        chain = []
        for index in range(depth):
            prior = leaf + index
            chain.append(["a", [prior, prior]])
        amplified[0][leaf + 1:leaf + 1] = chain
        prompt_parent = next(node for node in amplified[0]
                             if node[0] == "o" and any(
                                 entry == ["prompt", leaf] for entry in node[1]))
        next(entry for entry in prompt_parent[1] if entry[0] == "prompt")[1] = leaf + depth

        attacks = {"unused node": unused, "shared scalar": shared_scalar,
                   "shared container": shared_container,
                   "nested amplification": amplified}
        expanded = "Retained root"
        for _ in range(depth):
            expanded = [copy.deepcopy(expanded), copy.deepcopy(expanded)]
        self.assertGreater(len(approval_protocol.json_semantic_key(expanded).encode("utf-8")),
                           serve.MOOD_AUTHORITY_MAX_BYTES)
        shared_leaf = {"bounded": True}
        self.assertFalse(serve._json_domain_within([shared_leaf, shared_leaf]))
        with self.assertRaisesRegex(ValueError, "aliased JSON value"):
            approval_protocol.json_typed_graph([shared_leaf, shared_leaf])
        for name in matrix["typed_graph_attacks"]:
            line = json.dumps({"_burrow_internal": serve.MOOD_AUTHORITY_KIND,
                               "encoding": serve.MOOD_AUTHORITY_ENCODING,
                               "graph": attacks[name]}, separators=(",", ":"))
            self.assertLess(len(line.encode("utf-8")), serve.MOOD_AUTHORITY_MAX_BYTES)
            with self.assertRaisesRegex(ValueError, "noncanonical"):
                approval_protocol.decode_json_typed_graph(attacks[name])
            ignored(line, name)

        script = r"""
const p=require('./viewer/projection.js'),m=require('./viewer/moods.js');
const groups=JSON.parse(require('node:fs').readFileSync(0,'utf8'));
const view=lines=>Object.fromEntries(m.deriveMoods(p.parseEvents(lines)));
process.stdout.write(JSON.stringify(groups.map(view)));
"""
        completed = subprocess.run(["node", "-e", script], input=json.dumps([raw, *cases]),
            text=True, cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            check=True, capture_output=True)
        views = json.loads(completed.stdout)
        self.assertTrue(all(view == views[0] for view in views[1:]),
                        "every invalid capsule leaves the raw Mood result unchanged")

    def test_shared_capsule_depth_bound_is_atomic_and_non_throwing(self):
        fixture_path = os.path.join(os.path.dirname(__file__), "fixtures",
                                    "mood-capsule-malformed.json")
        with open(fixture_path, encoding="utf-8") as stream:
            matrix = json.load(stream)
        self.assertEqual(serve.MOOD_AUTHORITY_MAX_DEPTH,
                         matrix["max_structural_depth"])

        def nested(containers):
            value = "leaf"
            for _ in range(containers):
                value = [value]
            return value

        def capsule(containers):
            authority = copy.deepcopy(matrix["authority_event"])
            authority["type"] = "needs_human"
            authority["payload"] = {"message": "Deep request",
                                    "detail": nested(containers)}
            return {"_burrow_internal": serve.MOOD_AUTHORITY_KIND,
                    "events": [authority], "ordinals": ["0"], "copies": [],
                    "raw_ordinals": [], "raw_indexes": [],
                    "raw_count": "0000000000000000", "overflow": False,
                    "observed": 1}

        accepted = capsule(matrix["accepted_detail_containers"])
        accepted_line = json.dumps(accepted, separators=(",", ":"))
        self.assertIsNotNone(serve._mood_authority_from_line(accepted_line))
        over_depth = json.dumps(capsule(matrix["rejected_detail_containers"]),
                                separators=(",", ":"))
        self.assertIsNone(serve._mood_authority_from_line(over_depth))
        raw = json.dumps(matrix["raw_event"], separators=(",", ":"))
        self.assertEqual(protocol_events(serve.carry_forward([over_depth, raw],
            serve.event_ms(matrix["raw_event"]))), [matrix["raw_event"]])
        parser_overflow = ('{"_burrow_internal":"mood-authority-v1","events":' +
                           '[' * 1100 + '0' + ']' * 1100 + '}')
        self.assertLess(len(parser_overflow.encode("utf-8")),
                        serve.MOOD_AUTHORITY_MAX_BYTES)
        self.assertIsNone(serve._mood_authority_from_line(parser_overflow))
        self.assertEqual(protocol_events(serve.carry_forward([parser_overflow, raw],
            serve.event_ms(matrix["raw_event"]))), [matrix["raw_event"]])

    def test_invalid_tail_records_never_affect_rotation_coordinates_or_mood(self):
        root = event("codex:strict-tail", "task_started", 50, prompt="Root")
        root["source"] = "codex"
        work = [event("codex:strict-tail", "tool_called", 40 - index * 5,
                      tool="Read") for index in range(6)]
        invalid_before = event("codex:strict-tail", "idle", 60)
        invalid_before["source"] = ""
        invalid_among = event("codex:strict-tail", "idle", 1)
        invalid_among["ts"] = "not-a-timestamp"
        original_events = [invalid_before, root, *work[:3], invalid_among, *work[3:]]
        original = [json.dumps(item, separators=(",", ":")) for item in original_events]
        clean = [json.dumps(item, separators=(",", ":"))
                 for item in [root, *work]]
        now_ms = serve.event_ms(work[-1])
        once = serve.carry_forward(original, now_ms)
        clean_once = serve.carry_forward(clean, now_ms)
        self.assertEqual(once, clean_once,
                         "invalid records cannot consume retained/capsule coordinates")
        self.assertNotIn(invalid_before, protocol_events(once))
        self.assertNotIn(invalid_among, protocol_events(once))
        appended = event("codex:strict-tail", "heartbeat", 0)
        twice = serve.carry_forward([*once, json.dumps(appended, separators=(",", ":"))],
                                    serve.event_ms(appended))
        clean_twice = serve.carry_forward(
            [*clean_once, json.dumps(appended, separators=(",", ":"))],
            serve.event_ms(appended))
        self.assertEqual(twice, clean_twice)
        self.assertNotIn(invalid_before, protocol_events(twice))
        self.assertNotIn(invalid_among, protocol_events(twice))
        script = r"""
const p=require('./viewer/projection.js'),m=require('./viewer/moods.js');
const groups=JSON.parse(require('node:fs').readFileSync(0,'utf8'));
const view=lines=>({moods:Object.fromEntries(m.deriveMoods(p.parseEvents(lines))),
  state:p.moodAuthorityState(p.parseEvents(lines))});
process.stdout.write(JSON.stringify(groups.map(view)));
"""
        completed = subprocess.run(["node", "-e", script], input=json.dumps([
            [*original, json.dumps(appended, separators=(",", ":"))],
            [*once, json.dumps(appended, separators=(",", ":"))], twice]), text=True,
            cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            check=True, capture_output=True)
        full, appended_view, rerotated = json.loads(completed.stdout)
        self.assertEqual(appended_view["moods"], full["moods"])
        self.assertEqual(rerotated["moods"], full["moods"])

    def test_grouped_authority_survives_unrelated_appends_and_python_rotation(self):
        fixture_path = os.path.join(os.path.dirname(__file__), "fixtures",
                                    "mood-grouped-unrelated.json")
        with open(fixture_path, encoding="utf-8") as stream:
            fixture = json.load(stream)
        initial = [json.dumps(item, separators=(",", ":")) for item in fixture["initial"]]
        unrelated = [json.dumps(item, separators=(",", ":"))
                     for item in fixture["unrelated"]]
        now_ms = serve.event_ms(fixture["unrelated"][-1])
        once = serve.carry_forward(initial, now_ms)
        twice = serve.carry_forward([*once, unrelated[0]], now_ms)
        thrice = serve.carry_forward([*twice, unrelated[1]], now_ms)
        script = r"""
const p=require('./viewer/projection.js'),m=require('./viewer/moods.js');
const groups=JSON.parse(require('node:fs').readFileSync(0,'utf8'));
const read=lines=>Object.fromEntries(m.deriveMoods(p.parseEvents(lines)));
process.stdout.write(JSON.stringify(groups.map(read)));
"""
        completed = subprocess.run(["node", "-e", script], input=json.dumps(
            [initial, once, twice, thrice]), text=True,
            cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            check=True, capture_output=True)
        moods = json.loads(completed.stdout)
        target = "codex:resident-a"
        for rotated in moods[1:]:
            self.assertEqual(rotated[target], moods[0][target])
        self.assertEqual(moods[0][target]["signals"]["interaction"]["kind"],
                         "root prompt")
        self.assertEqual(moods[0][target]["signals"]["interaction"]["logAgeMs"], 0)

    def test_shared_ambiguous_and_post_collision_mood_authority_survives_rotation(self):
        fixture_path = os.path.join(os.path.dirname(__file__), "fixtures",
                                    "mood-lifecycle-ambiguity.json")
        with open(fixture_path, encoding="utf-8") as stream:
            fixture = json.load(stream)
        ambiguous = fixture["scenarios"]["ambiguous_close"]
        post = fixture["scenarios"]["post_close_collision"]
        pending_start = datetime.datetime.fromisoformat(
            post["pending_start"].replace("Z", "+00:00"))
        pending = []
        for index in range(post["pending_count"]):
            item = copy.deepcopy(post["anchor"])
            item["ts"] = (pending_start + datetime.timedelta(minutes=index)).isoformat(
                timespec="milliseconds").replace("+00:00", "Z")
            item["type"] = "needs_human"
            item["payload"] = {"message": f"Pending {index}",
                               "request_id": f"pressure-{index}",
                               "action": "deploy", "detail": None,
                               "options": ["approve", "deny"]}
            pending.append(item)
        post_collision = [*post["events"], *pending, post["anchor"]]
        scenarios = [
            (ambiguous, {"codex:ambiguous"}),
            (post_collision, {"codex:post-collision"}),
        ]
        rotated_batches = []
        for events, eligible in scenarios:
            indexes = serve._mood_keep_indexes(list(enumerate(events)), eligible)
            selected = [events[index] for index in sorted(indexes)]
            if events is ambiguous:
                self.assertTrue({0, 1}.issubset(indexes),
                                "both incompatible collision candidates remain unresolved")
            else:
                self.assertTrue({0, 1}.issubset(indexes),
                                "final non-collided q1 decision survives pressure")
            lines = [json.dumps(item, separators=(",", ":")) for item in events]
            now_ms = int(datetime.datetime.fromisoformat(
                fixture["now"].replace("Z", "+00:00")).timestamp() * 1000)
            rotated = serve.carry_forward(lines, now_ms)
            rotated_batches.append((events, selected, [json.loads(line) for line in rotated]))

        script = r"""
const projection=require('./viewer/projection.js');
const moods=require('./viewer/moods.js');
const batches=JSON.parse(require('fs').readFileSync(0,'utf8'));
const derive=events=>Object.fromEntries(moods.deriveMoods(projection.parseEvents(events)));
process.stdout.write(JSON.stringify(batches.map(group=>group.map(derive))));
"""
        completed = subprocess.run(
            ["node", "-e", script], input=json.dumps(rotated_batches), text=True,
            cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            check=True, capture_output=True)
        (ambiguous_full, ambiguous_selected, ambiguous_rotated), \
            (post_full, post_selected, post_rotated) = json.loads(completed.stdout)
        self.assertEqual(ambiguous_selected["codex:ambiguous"],
                         ambiguous_full["codex:ambiguous"])
        self.assertEqual(ambiguous_rotated["codex:ambiguous"],
                         ambiguous_full["codex:ambiguous"])
        self.assertEqual(post_selected["codex:post-collision"],
                         post_full["codex:post-collision"])
        self.assertEqual(post_rotated["codex:post-collision"],
                         post_full["codex:post-collision"])
        self.assertEqual(post_full["codex:post-collision"]["signals"]["interaction"]["kind"],
                         "approval decision")

    def test_specialized_mood_history_before_liveness_survives_python_rotation(self):
        anchor = datetime.datetime(2026, 8, 25, 20, tzinfo=datetime.timezone.utc)
        agent_id = "codex:late-visible"
        events = []
        for index in range(4100):
            item = event(agent_id, "routine_finished", 0, routine="watch",
                         run_id=f"run-{index}", outcome="ok", artifacts=[], duration_s=1)
            item["source"] = "steward"
            item["ts"] = (anchor - datetime.timedelta(seconds=index)).isoformat(
                timespec="milliseconds").replace("+00:00", "Z")
            events.append(item)
        idle = event(agent_id, "idle")
        idle["ts"] = (anchor - datetime.timedelta(milliseconds=500)).isoformat(
            timespec="milliseconds").replace("+00:00", "Z")
        events.append(idle)
        lines = [json.dumps(item, separators=(",", ":")) for item in events]
        rotated = serve.carry_forward(lines, int(anchor.timestamp() * 1000))

        script = r"""
const projection=require('./viewer/projection.js');
const moods=require('./viewer/moods.js');
const batches=JSON.parse(require('fs').readFileSync(0,'utf8'));
const derive=lines=>Object.fromEntries(moods.deriveMoods(projection.parseEvents(lines)));
process.stdout.write(JSON.stringify(batches.map(derive)));
"""
        completed = subprocess.run(
            ["node", "-e", script], input=json.dumps([lines, rotated]), text=True,
            cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            check=True, capture_output=True)
        before, after = json.loads(completed.stdout)
        self.assertEqual(after[agent_id], before[agent_id])
        self.assertEqual(after[agent_id]["anchor"],
                         anchor.isoformat(timespec="milliseconds").replace("+00:00", "Z"))
        self.assertEqual(after[agent_id]["status"], "authority history uncertain")
        self.assertEqual(after[agent_id]["evidence"], {"count": 0, "spanMs": 0})
        self.assertTrue(serve._mood_authority_from_line(rotated[0])["overflow"])

    def test_rotation_keeps_bounded_canonical_journal_authority_and_one_conflict(self):
        lines = []
        for index in range(42):
            month, day = (7, index + 1) if index < 31 else (8, index - 30)
            lines.append(json.dumps(self.journal(
                f"codex:{index}", f"2026-{month:02d}-{day:02d}")))
        canonical = self.journal("codex:41", "2026-08-11")
        replay = {**canonical, "ts": ts(500)}
        conflict = self.journal("codex:41", "2026-08-11", routine="nightly")
        second_conflict = self.journal("codex:41", "2026-08-11",
                                       path="/else/2026-08-11.md")
        lines.extend(map(json.dumps, [replay, conflict, second_conflict]))
        tail = [json.loads(line) for line in serve.carry_forward(lines, serve.event_ms(canonical))]
        journals = [item for item in tail if item["type"] == "journal_written"]
        canonical_days = {(item["agent_id"], item["payload"]["day"])
                          for item in journals}
        self.assertEqual(len(canonical_days), 40)
        self.assertNotIn(("codex:0", "2026-07-01"), canonical_days)
        selected = [item for item in journals if item["agent_id"] == "codex:41"]
        self.assertEqual([item["payload"]["routine"] for item in selected],
                         ["close-of-day", "nightly"])

    def test_rotation_preserves_later_session_end_without_erasing_journal_recency(self):
        observed = json.dumps(self.journal("codex:life", "2026-08-25"))
        ended = json.dumps(event("codex:life", "session_ended"))
        tail = serve.carry_forward([observed, ended], int(datetime.datetime.now(
            datetime.timezone.utc).timestamp() * 1000))
        self.assertEqual(tail, [observed, ended])

    def test_rotation_rejects_evicted_journal_replays_and_conflicts(self):
        journals = [self.journal(f"codex:{index:02d}", "2026-08-24")
                    for index in range(40)]
        journals.append(self.journal("codex:new", "2026-08-25"))
        replay = self.journal("codex:00", "2026-08-24")
        conflict = self.journal("codex:00", "2026-08-24", routine="nightly")
        lines = list(map(json.dumps, [*journals, replay, conflict, replay, conflict]))
        retained = [json.loads(line) for line in serve.carry_forward(
            lines, int(datetime.datetime.now(datetime.timezone.utc).timestamp() * 1000))]
        retained_journals = [item for item in retained
                             if item["type"] == "journal_written"]
        keys = {(item["agent_id"], item["payload"]["day"])
                for item in retained_journals}
        self.assertEqual(len(keys), 40)
        self.assertNotIn(("codex:00", "2026-08-24"), keys)
        self.assertIn(("codex:01", "2026-08-24"), keys)

    def test_full_log_journal_authority_merges_with_clipped_ordinary_tail_and_browser_reset(self):
        observed = self.journal("codex:life", "2026-08-25")
        ordinary = [event("codex:life", "tool_called", 0, tool="Read", n=index)
                    for index in range(serve.VIEWER_LINE_LIMIT + 1)]
        lines = list(map(json.dumps, [observed, *ordinary]))
        tail = serve.carry_forward(lines, int(datetime.datetime.now(
            datetime.timezone.utc).timestamp() * 1000))
        decoded = [json.loads(line) for line in tail]
        self.assertEqual(decoded[0], observed,
                         "journal selected from full input keeps original append position")
        self.assertEqual(sum(item["type"] == "journal_written" for item in decoded), 1)
        self.assertEqual([item["payload"]["n"] for item in decoded[1:]],
                         list(range(serve.VIEWER_LINE_LIMIT + 1 - serve.KEEP_PER_AGENT,
                                    serve.VIEWER_LINE_LIMIT + 1)))

        script = r"""
const {createBrowserRuntime}=require('./viewer/browser-runtime.js');
const lines=JSON.parse(require('node:fs').readFileSync(0,'utf8'));
const cursor='v1:0123456789abcdef0123456789abcdef:1:2:3:99';
const resident={file:'life.resident.json',valid:true,manifest_version:1,home:2,
 match:{agent_id:'codex:life'},meta:{name:'Hob',char:'Monk',accent:'#a68a4f'}};
const runtime=createBrowserRuntime({now:()=>Date.now(),EventSource:null,setTimeout:()=>1,
 clearTimeout(){},fetch:async url=>url==='/villagers'?{ok:true,json:async()=>[resident]}:
 ({ok:true,headers:{get:name=>name==='X-Burrow-Cursor'?cursor:
   name==='X-Burrow-Reset'?'1':null},text:async()=>lines.join('\n')})});
runtime.poll().then(()=>process.stdout.write(JSON.stringify({
 journals:runtime.snapshot().journalState.records.size,
 key:runtime.snapshot().journalState.records.has('codex:life\0'+'2026-08-25'),
 villagers:runtime.snapshot().villagers.length,
 last:runtime.snapshot().villagers[0]&&runtime.snapshot().villagers[0].events.at(-1).payload.n
})));
"""
        completed = subprocess.run(
            ["node", "-e", script], input=json.dumps(tail), text=True,
            cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            check=True, capture_output=True)
        self.assertEqual(json.loads(completed.stdout), {
            "journals": 1, "key": True, "villagers": 1,
            "last": serve.VIEWER_LINE_LIMIT,
        })

    def test_full_log_journal_keeps_later_terminal_outside_ordinary_window(self):
        observed = json.dumps(self.journal("codex:life", "2026-08-25"))
        ended = json.dumps(event("codex:life", "session_ended"))
        chatter = [json.dumps(event(f"codex:gone-{index}", "session_ended"))
                   for index in range(serve.VIEWER_LINE_LIMIT)]
        tail = serve.carry_forward([observed, ended, *chatter], int(datetime.datetime.now(
            datetime.timezone.utc).timestamp() * 1000))
        self.assertEqual(tail, [observed, ended],
                         "retained journal cannot resurrect a session whose terminal was clipped")

    def test_journal_merge_never_exceeds_global_transport_window(self):
        observed = json.dumps(self.journal("codex:journal", "2026-08-25"))
        ordinary = [json.dumps(event(f"codex:live-{index}", "tool_called", 0,
                                     tool="Read", n=index))
                    for index in range(serve.VIEWER_LINE_LIMIT + 1)]
        tail = serve.carry_forward([observed, *ordinary], int(datetime.datetime.now(
            datetime.timezone.utc).timestamp() * 1000))
        self.assertEqual(len(tail), serve.VIEWER_LINE_LIMIT)
        self.assertEqual(tail[0], observed)
        self.assertEqual(json.loads(tail[1])["payload"]["n"], 2)
        self.assertEqual(json.loads(tail[-1])["payload"]["n"],
                         serve.VIEWER_LINE_LIMIT)

    def test_rotation_retains_each_journal_predecessor_and_restores_exact_expiry_state(self):
        stale = event("codex:stale", "tool_called", 31, tool="Read")
        dropped = event("codex:dropped", "tool_called", 13 * 60, tool="Bash")
        stale_journal = self.journal("codex:stale", "2026-08-24")
        dropped_journal = self.journal("codex:dropped", "2026-08-24")
        second_predecessor = event("codex:stale", "tool_called", 31, tool="Bash")
        second_journal = self.journal("codex:stale", "2026-08-25")
        chatter = [event(f"codex:unrelated-{index}", "session_ended")
                   for index in range(serve.VIEWER_LINE_LIMIT + 1)]
        source = [stale, stale_journal, dropped, dropped_journal,
                  second_predecessor, second_journal, *chatter]
        lines = list(map(json.dumps, source))
        now_ms = serve.event_ms(second_journal)
        rotated = serve.carry_forward(lines, now_ms)
        decoded = [json.loads(line) for line in rotated]
        self.assertLessEqual(len(rotated), serve.VIEWER_LINE_LIMIT)
        for retained in [stale, stale_journal, dropped, dropped_journal,
                         second_predecessor, second_journal]:
            self.assertIn(retained, decoded)
        self.assertEqual([source.index(item) for item in decoded
                          if item in source[:6]], list(range(6)),
                         "journal/predecessor merge preserves original append order")

        script = r"""
const projection=require('./viewer/projection.js');
const journals=require('./viewer/journal-observations.js');
const input=JSON.parse(require('node:fs').readFileSync(0,'utf8'));
const parsed=projection.parseEvents(input.lines), state=journals.createState(), agents=new Map();
journals.foldValidated(state,parsed,{isValidatedBatch:projection.isValidatedBatch,
 rejections:projection.journalRejections(parsed)});
projection.foldEvents(agents,parsed,state);
const soul=id=>({valid:true,manifest_version:1,home:id==='codex:stale'?1:2,
 match:{agent_id:id},meta:{name:id,char:'Monk',accent:'#fff'}});
const souls=[soul('codex:stale'),soul('codex:dropped')];
const view=at=>projection.reduce(agents,at,souls,null,state).map(item=>({
 id:item.id,state:item.state,lastTs:item.lastTs,doing:item.doing,
 day:item.journal&&item.journal.event.payload.day})).sort((a,b)=>a.id.localeCompare(b.id));
process.stdout.write(JSON.stringify({active:view(input.now+59999),expired:view(input.now+60000)}));
"""
        completed = subprocess.run(
            ["node", "-e", script], input=json.dumps({"lines": rotated, "now": now_ms}),
            text=True, cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            check=True, capture_output=True)
        result = json.loads(completed.stdout)
        self.assertEqual([(item["id"], item["doing"]) for item in result["active"]],
                         [("codex:dropped", "writing the journal"),
                          ("codex:stale", "writing the journal")])
        self.assertEqual(result["expired"], [{
            "id": "codex:stale", "state": "stale",
            "lastTs": serve.event_ms(second_predecessor), "doing": "",
            "day": None,
        }])

    def test_rotation_reset_keeps_every_later_ordinary_authority_under_tail_pressure(self):
        base = datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0)

        def at(agent, kind, seconds, **payload):
            item = event(agent, kind, 0, **payload)
            item["ts"] = (base + datetime.timedelta(seconds=seconds)).isoformat(
                timespec="milliseconds").replace("+00:00", "Z")
            return item

        def observed(agent, day, seconds):
            item = self.journal(agent, day)
            item["ts"] = (base + datetime.timedelta(seconds=seconds)).isoformat(
                timespec="milliseconds").replace("+00:00", "Z")
            return item

        predecessor = {}
        source = []
        successors = {
            "codex:tool": at("codex:tool", "tool_called", -10, tool="Bash"),
            "codex:idle": at("codex:idle", "idle", 2),
            "codex:plain": at("codex:plain", "needs_human", 3, message="Choose"),
            "codex:ended": at("codex:ended", "session_ended", 4),
            "codex:approval": self.approval("journal-approval", "codex:approval"),
        }
        successors["codex:approval"]["ts"] = (base + datetime.timedelta(
            seconds=5)).isoformat(timespec="milliseconds").replace("+00:00", "Z")
        for offset, agent in enumerate([*successors, "codex:active"]):
            predecessor[agent] = at(agent, "tool_called", -31 * 60 - offset,
                                    tool="Read")
            source.extend([predecessor[agent], observed(agent, "2026-08-24", 0)])
            if agent in successors:
                source.append(successors[agent])

        multi_before = at("codex:multi", "tool_called", -60, tool="Read")
        multi_first = observed("codex:multi", "2026-08-24", 0)
        multi_middle = at("codex:multi", "idle", 1)
        multi_second = observed("codex:multi", "2026-08-25", 2)
        multi_after = at("codex:multi", "tool_called", -20, tool="Write")
        source.extend([multi_before, multi_first, multi_middle, multi_second, multi_after])
        resolved_knock = self.approval("resolved-before-journal", "codex:resolved-before")
        resolved_knock["ts"] = at("unused", "idle", -3)["ts"]
        resolved_close = self.resolution("resolved-before-journal", "codex:resolved-before")
        resolved_close["ts"] = at("unused", "idle", -2)["ts"]
        resolved_journal = observed("codex:resolved-before", "2026-08-25", 0)
        source.extend([resolved_knock, resolved_close, resolved_journal])
        source.extend(event(f"codex:unrelated-{index}", "session_ended")
                      for index in range(serve.VIEWER_LINE_LIMIT + 1))
        rotated = serve.carry_forward(list(map(json.dumps, source)),
                                      int(base.timestamp() * 1000) + 30_000)
        decoded = [json.loads(line) for line in rotated]
        self.assertLessEqual(len(rotated), serve.VIEWER_LINE_LIMIT)
        for retained in [*predecessor.values(), *successors.values(), multi_before,
                         multi_first, multi_middle, multi_second, multi_after,
                         resolved_knock, resolved_close, resolved_journal]:
            self.assertIn(retained, decoded)
        indexes = [source.index(item) for item in decoded if item in source]
        self.assertEqual(indexes, sorted(set(indexes)),
                         "the bounded merge preserves original order without duplicates")

        script = r"""
const {createBrowserRuntime}=require('./viewer/browser-runtime.js');
const input=JSON.parse(require('node:fs').readFileSync(0,'utf8'));
const resident=id=>({file:id+'.resident.json',valid:true,manifest_version:1,home:input.ids.indexOf(id),
 match:{agent_id:id},meta:{name:id,char:'Monk',accent:'#fff'}});
async function view(now){
 const runtime=createBrowserRuntime({now:()=>now,EventSource:null,setTimeout:()=>1,clearTimeout(){},
  fetch:async url=>url==='/residents'?{ok:true,json:async()=>({residents:input.ids.map(resident),diagnostics:[]})}:
   ({ok:true,headers:{get:name=>name==='X-Burrow-Cursor'?'v1:0123456789abcdef0123456789abcdef:1:2:3:99':
    name==='X-Burrow-Reset'?'1':null},text:async()=>input.lines.join('\n')})});
 await runtime.refreshResidents(); await runtime.poll();
 return runtime.snapshot().villagers.map(item=>({id:item.id,state:item.state,doing:item.doing,
  lastTs:item.lastTs,day:item.journal&&item.journal.event.payload.day})).sort((a,b)=>a.id.localeCompare(b.id));
}
Promise.all([view(input.base+30000),view(input.base+60000)]).then(([active,expired])=>
 process.stdout.write(JSON.stringify({active,expired})));
"""
        ids = [*successors, "codex:active", "codex:multi", "codex:resolved-before"]
        completed = subprocess.run(
            ["node", "-e", script], input=json.dumps({"lines": rotated,
                "base": int(base.timestamp() * 1000), "ids": ids}), text=True,
            cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            check=True, capture_output=True)
        projected = json.loads(completed.stdout)
        active = {item["id"]: item for item in projected["active"]}
        expired = {item["id"]: item for item in projected["expired"]}
        self.assertNotIn("codex:ended", active)
        self.assertEqual(active["codex:tool"]["doing"], "tinkering")
        self.assertEqual(active["codex:idle"]["state"], "resting")
        self.assertEqual(active["codex:plain"]["state"], "knocking")
        self.assertEqual(active["codex:approval"]["state"], "knocking")
        self.assertEqual(active["codex:active"]["doing"], "writing the journal")
        self.assertEqual(active["codex:multi"]["doing"], "crafting")
        self.assertEqual(active["codex:resolved-before"]["doing"], "writing the journal")
        self.assertEqual(expired["codex:active"]["state"], "stale")
        self.assertEqual(expired["codex:active"]["lastTs"],
                         serve.event_ms(predecessor["codex:active"]))
        self.assertEqual(expired["codex:resolved-before"]["state"], "resting")
        for agent in successors:
            if agent != "codex:ended":
                self.assertEqual(expired[agent], active[agent])

    @staticmethod
    def approval(request_id, agent="approver", minutes_ago=0):
        return event(agent, "needs_human", minutes_ago, message="May I?",
                     request_id=request_id, action="send_email",
                     detail={"to": "a@example.com"},
                     options=["approve", "deny", "edit"])

    @staticmethod
    def resolution(request_id, agent="approver", minutes_ago=0, decision="approve"):
        resolved = event(agent, "needs_human_resolved", minutes_ago,
                         request_id=request_id, decision=decision,
                         decided_by="api", action="send_email")
        resolved["source"] = "steward"
        return resolved

    def test_unresolved_structured_knock_survives_the_ordinary_drop_window(self):
        pending = json.dumps(self.approval("old-pending", minutes_ago=13 * 60))
        tail = serve.carry_forward([pending], int(datetime.datetime.now(
            datetime.timezone.utc).timestamp() * 1000))
        self.assertEqual(protocol_events(tail), [json.loads(pending)])
        self.assertEqual(sum("mood-authority-v1" in line for line in tail), 1)

    def test_retained_journal_carries_distant_approval_truth_into_browser_reset(self):
        """Rotation and grouped replay share pending/closed/collision truth."""
        now = datetime.datetime.now(datetime.timezone.utc)
        now_ms = int(now.timestamp() * 1000)
        ancient_minutes = 13 * 60
        journal = self.journal("codex:life", now.date().isoformat())
        journal["ts"] = now.isoformat(timespec="milliseconds").replace("+00:00", "Z")
        request = self.approval("journal-life", "codex:life", ancient_minutes)
        intervening = event("codex:life", "tool_called", ancient_minutes,
                            tool="Read")
        close = self.resolution("journal-life", "codex:life", ancient_minutes)
        collision = self.approval("journal-life", "codex:life", ancient_minutes)
        collision["payload"]["message"] = "A different immutable question"
        orphan = self.resolution("orphan", "codex:life", ancient_minutes)
        chatter = [event(f"codex:other-{index}", "session_ended")
                   for index in range(serve.VIEWER_LINE_LIMIT + 1)]
        variants = {
            "pending": [request, intervening, orphan, journal],
            "resolved": [request, intervening, close, orphan, journal],
            "collided": [request, intervening, collision, orphan, journal],
        }
        rotated = {}
        for name, prefix in variants.items():
            tail = serve.carry_forward(
                list(map(json.dumps, [*prefix, *chatter])), now_ms)
            decoded = protocol_events(tail)
            rotated[name] = tail
            self.assertLessEqual(len(tail), serve.VIEWER_LINE_LIMIT)
            self.assertIn(request, decoded, name)
            self.assertIn(journal, decoded, name)
            self.assertNotIn(orphan, decoded, name)
            if name == "resolved":
                self.assertIn(close, decoded)
            if name == "collided":
                self.assertIn(collision, decoded)
            source = [*prefix, *chatter]
            positions = [source.index(item) for item in decoded]
            self.assertEqual(positions, sorted(set(positions)), name)

        script = r"""
const {createBrowserRuntime}=require('./viewer/browser-runtime.js');
const input=JSON.parse(require('node:fs').readFileSync(0,'utf8'));
const cursor='v1:0123456789abcdef0123456789abcdef:1:2:3:99';
const resident={valid:true,manifest_version:1,home:0,match:{agent_id:'codex:life'},
 meta:{name:'Life',char:'Monk',accent:'#fff'}};
async function view(lines,now){
 const runtime=createBrowserRuntime({now:()=>now,EventSource:null,setTimeout:()=>1,clearTimeout(){},
  fetch:async url=>url==='/villagers'?{ok:true,json:async()=>[resident]}:
   ({ok:true,headers:{get:name=>name==='X-Burrow-Cursor'?cursor:
    name==='X-Burrow-Reset'?'1':null},text:async()=>lines.join('\n')})});
 await runtime.poll();
 const villager=runtime.snapshot().villagers.find(item=>item.id==='codex:life');
 const approval=runtime.snapshot().approvalState.requests.get('journal-life');
 return {villager:villager?{state:villager.state,doing:villager.doing,
   request_id:villager.knock&&villager.knock.request_id}:null,
   approval:approval&&{pending:!approval.resolution&&!approval.collided,
    resolved:Boolean(approval.resolution),collided:approval.collided},
   orphan:runtime.snapshot().approvalState.requests.has('orphan')};
}
Promise.all(Object.entries(input.lines).flatMap(([name,lines])=>[
 view(lines,input.now).then(value=>[name,'active',value]),
 view(lines,input.now+120000).then(value=>[name,'expired',value])
])).then(rows=>process.stdout.write(JSON.stringify(rows)));
"""
        completed = subprocess.run(
            ["node", "-e", script], input=json.dumps({"lines": rotated,
                "now": now_ms}), text=True,
            cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            check=True, capture_output=True)
        views = {(name, age): value for name, age, value
                 in json.loads(completed.stdout)}
        for age in ["active", "expired"]:
            self.assertEqual(views[("pending", age)]["villager"]["state"],
                             "knocking")
            self.assertEqual(views[("pending", age)]["villager"]["request_id"],
                             "journal-life")
            self.assertTrue(views[("pending", age)]["approval"]["pending"])
            self.assertTrue(views[("resolved", age)]["approval"]["resolved"])
            self.assertTrue(views[("collided", age)]["approval"]["collided"])
            for name in variants:
                self.assertFalse(views[(name, age)]["orphan"])
        self.assertEqual(views[("resolved", "active")]["villager"]["doing"],
                         "writing the journal")
        self.assertEqual(views[("collided", "active")]["villager"]["doing"],
                         "writing the journal")
        self.assertIsNone(views[("resolved", "expired")]["villager"])
        self.assertIsNone(views[("collided", "expired")]["villager"])

    def test_journal_retention_covers_both_approval_sides_and_independent_knocks(self):
        """The actual rotated response is the browser's append authority."""
        now = datetime.datetime.now(datetime.timezone.utc)
        now_ms = int(now.timestamp() * 1000)
        ancient = 13 * 60
        active_journal = self.journal("codex:life", now.date().isoformat())
        active_journal["ts"] = now.isoformat(timespec="milliseconds").replace("+00:00", "Z")
        expired_journal = copy.deepcopy(active_journal)
        expired_journal["ts"] = (now - datetime.timedelta(minutes=ancient)).isoformat(
            timespec="milliseconds").replace("+00:00", "Z")
        request = self.approval("both-sides", "codex:life", ancient)
        ordinary = event("codex:life", "tool_called", ancient, tool="Read")
        close = self.resolution("both-sides", "codex:life", ancient)
        collision = copy.deepcopy(request)
        collision["payload"]["message"] = "Different immutable request"
        orphan = self.resolution("orphan-both-sides", "codex:life", ancient)
        chatter = [event(f"codex:pressure-{index}", "session_ended")
                   for index in range(serve.VIEWER_LINE_LIMIT + 3)]

        lifecycle_tails = {}
        for age, journal in [("active", active_journal), ("expired", expired_journal)]:
            for side, sequence in {
                    "after": [journal, request, ordinary],
                    "before": [request, ordinary, journal]}.items():
                for lifecycle, suffix in {
                        "pending": [orphan], "resolved": [close, orphan],
                        "collided": [collision, orphan]}.items():
                    source = [*sequence, *suffix, *chatter]
                    tail = serve.carry_forward(list(map(json.dumps, source)), now_ms)
                    retained = protocol_events(tail)
                    lifecycle_tails[f"{age}/{side}/{lifecycle}"] = tail
                    self.assertLessEqual(len(retained), serve.VIEWER_LINE_LIMIT)
                    self.assertEqual(len(retained), len({source.index(item) for item in retained}),
                                     (age, side, lifecycle))
                    positions = [source.index(item) for item in retained]
                    self.assertEqual(positions, sorted(positions), (age, side, lifecycle))
                    for required in [journal, request]:
                        self.assertIn(required, retained, (age, side, lifecycle))
                    if lifecycle != "collided":
                        self.assertIn(ordinary, retained, (age, side, lifecycle))
                    self.assertNotIn(orphan, retained, (age, side, lifecycle))
                    if lifecycle == "resolved":
                        self.assertIn(close, retained)
                    if lifecycle == "collided":
                        self.assertIn(collision, retained)

        lifecycle_script = r"""
const {createBrowserRuntime}=require('./viewer/browser-runtime.js');
const input=JSON.parse(require('node:fs').readFileSync(0,'utf8'));
const resident={file:'life.resident.json',valid:true,manifest_version:1,home:0,
 match:{agent_id:'codex:life'},meta:{name:'Life',char:'Monk',accent:'#fff'}};
const cursor='v1:0123456789abcdef0123456789abcdef:1:2:3:99';
async function reset(lines){const runtime=createBrowserRuntime({now:()=>input.now,EventSource:null,
 setTimeout:()=>1,clearTimeout(){},fetch:async url=>url==='/villagers'?
 {ok:true,json:async()=>[resident]}:{ok:true,headers:{get:name=>name==='X-Burrow-Cursor'?cursor:
 name==='X-Burrow-Reset'?'1':null},text:async()=>lines.join('\n')}});
 await runtime.poll();const snapshot=runtime.snapshot(),record=snapshot.approvalState.requests.get('both-sides');
 const villager=snapshot.villagers.find(item=>item.id==='codex:life');
 return {pending:Boolean(record&&!record.resolution&&!record.collided),
  resolved:Boolean(record&&record.resolution),collided:Boolean(record&&record.collided),
  state:villager&&villager.state,request_id:villager&&villager.knock&&villager.knock.request_id};}
Promise.all(Object.entries(input.lines).map(async ([name,lines])=>[name,await reset(lines)]))
 .then(rows=>process.stdout.write(JSON.stringify(rows)));
"""
        completed = subprocess.run(["node", "-e", lifecycle_script], input=json.dumps({
            "lines": lifecycle_tails, "now": now_ms}), text=True,
            cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            check=True, capture_output=True)
        for name, view in json.loads(completed.stdout):
            lifecycle = name.rsplit("/", 1)[-1]
            if lifecycle == "pending":
                self.assertEqual(view, {"pending": True, "resolved": False,
                    "collided": False, "state": "knocking", "request_id": "both-sides"}, name)
            else:
                self.assertFalse(view["pending"], name)
                self.assertEqual(view["resolved"], lifecycle == "resolved", name)
                self.assertEqual(view["collided"], lifecycle == "collided", name)
                self.assertNotEqual(view.get("state"), "knocking", name)

        independent_tails = {}
        for kind in ["plain", "malformed"]:
            independent = copy.deepcopy(request)
            independent["payload"] = {"message": f"Independent {kind} knock"}
            if kind == "malformed":
                independent["payload"].update({"request_id": "broken",
                    "action": "Publish", "detail": None, "options": ["approve"]})
            source = [request, active_journal, independent, *chatter]
            tail = serve.carry_forward(list(map(json.dumps, source)), now_ms)
            decoded = [json.loads(line) for line in tail]
            self.assertIn(request, decoded); self.assertIn(active_journal, decoded)
            self.assertIn(independent, decoded)
            self.assertLessEqual(len(tail), serve.VIEWER_LINE_LIMIT)
            independent_tails[kind] = tail

        script = r"""
const {createBrowserRuntime}=require('./viewer/browser-runtime.js');
const input=JSON.parse(require('node:fs').readFileSync(0,'utf8'));
const BOOT='0123456789abcdef0123456789abcdef';
const cursor=n=>`v1:${BOOT}:1:2:3:${n}`;
const resident={file:'life.resident.json',valid:true,manifest_version:1,home:0,
 match:{agent_id:'codex:life'},meta:{name:'Life',char:'Monk',accent:'#fff'}};
const response=(body,n,reset=false)=>({ok:true,headers:{get:name=>name==='X-Burrow-Cursor'?cursor(n):
 name==='X-Burrow-Reset'&&reset?'1':null},text:async()=>body});
function summary(runtime){
 const snapshot=runtime.snapshot(),villager=snapshot.villagers.find(item=>item.id==='codex:life');
 const request=snapshot.approvalState.requests.get('both-sides');
 const independent=villager&&villager.events.find(event=>event.payload&&
   /^Independent /.test(event.payload.message||''));
 const journal=snapshot.journalState.records.get(`codex:life\0${input.day}`);
 return {state:villager&&villager.state,message:villager&&villager.knock&&villager.knock.message,
  request_id:villager&&villager.knock&&villager.knock.request_id,
  malformed:snapshot.approvalState.malformed,
  requestOrdinal:request&&request.knockOrdinal,
  journalOrdinal:journal&&journal.ordinal,
  independentOrdinal:independent&&snapshot.approvalState.ordinalForEvent(independent)};
}
async function grouped(lines){let polls=0;const runtime=createBrowserRuntime({now:()=>input.now,
 EventSource:null,setTimeout:()=>1,clearTimeout(){},fetch:async url=>url==='/villagers'?
 {ok:true,json:async()=>[resident]}:response(lines.join('\n'),++polls*10,polls>1)});
 await runtime.poll();const bootstrap=summary(runtime);await runtime.poll();
 return {bootstrap,reset:summary(runtime)};}
async function streamed(lines){let stream;class EventSource{constructor(){this.listeners={};stream=this}
 addEventListener(n,f){this.listeners[n]=f}close(){}}
 const runtime=createBrowserRuntime({now:()=>input.now,EventSource,setTimeout:()=>1,clearTimeout(){},
 fetch:async url=>url==='/villagers'?{ok:true,json:async()=>[resident]}:response('',1)});
 await runtime.poll();runtime.connectStream();lines.forEach((line,index)=>stream.onmessage({
  lastEventId:cursor(index+2),data:line}));
 await stream.listeners.ready({lastEventId:cursor(lines.length+1),
  data:JSON.stringify({cursor:cursor(lines.length+1)})});return summary(runtime);}
Promise.all(Object.entries(input.lines).map(async ([kind,lines])=>
 [kind,await grouped(lines),await streamed(lines)])).then(value=>process.stdout.write(JSON.stringify(value)));
"""
        completed = subprocess.run(["node", "-e", script], input=json.dumps({
            "lines": independent_tails, "now": now_ms,
            "day": now.date().isoformat()}), text=True,
            cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            check=True, capture_output=True)
        for kind, grouped, streamed in json.loads(completed.stdout):
            expected = {"state": "knocking", "message": f"Independent {kind} knock",
                        "request_id": None, "malformed": 1 if kind == "malformed" else 0,
                        "requestOrdinal": "1", "journalOrdinal": "2",
                        "independentOrdinal": "3"}
            self.assertEqual(grouped["bootstrap"], expected, kind)
            self.assertEqual(grouped["reset"], expected, kind)
            self.assertEqual(streamed, expected, kind)

    def test_resolution_is_retained_only_with_its_request_and_never_as_liveness(self):
        knock = json.dumps(self.approval("paired", minutes_ago=5))
        close = json.dumps(self.resolution("paired", minutes_ago=4))
        orphan = json.dumps(self.resolution("orphan", agent="nobody", minutes_ago=3))
        ended = json.dumps(event("nobody", "session_ended", 2))
        tail = serve.carry_forward([knock, close, orphan, ended], int(datetime.datetime.now(
            datetime.timezone.utc).timestamp() * 1000))
        self.assertIn(knock, tail)
        self.assertIn(close, tail)
        self.assertNotIn(orphan, tail,
                         "an orphan close is ignored and cannot survive rotation to bind later")
        self.assertNotIn(ended, tail)
        script = """
const {createBrowserRuntime}=require('./viewer/browser-runtime.js');
const lines=JSON.parse(require('node:fs').readFileSync(0,'utf8'));
const cursor='v1:0123456789abcdef0123456789abcdef:1:2:3:99';
const runtime=createBrowserRuntime({now:()=>Date.now(),EventSource:null,
 setTimeout:()=>1,clearTimeout(){},fetch:async url=>url==='/villagers'?{ok:false}:
 ({ok:true,headers:{get:n=>n==='X-Burrow-Cursor'?cursor:null},text:async()=>lines.join('\\n')})});
runtime.poll().then(()=>process.stdout.write(JSON.stringify(runtime.snapshot().villagers.map(v=>v.id))));
"""
        projected = subprocess.run(["node", "-e", script], input=json.dumps(tail), text=True,
            cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            check=True, capture_output=True)
        self.assertEqual(json.loads(projected.stdout), ["approver"],
                         "orphan close does not create or refresh nobody")

    def test_approval_rotation_capacity_drops_whole_pairs_without_ghost_knocks(self):
        events = []
        for index in range(serve.KEEP_APPROVALS + 3):
            events.extend([self.approval(f"r-{index}", minutes_ago=10 - index / 10),
                           self.resolution(f"r-{index}", minutes_ago=9 - index / 10)])
        keep, isolated = serve._approval_keep_indexes(list(enumerate(events)))
        retained = [events[index] for index in keep]
        request_ids = {}
        for item in retained:
            request_ids.setdefault(item["payload"]["request_id"], set()).add(item["type"])
        self.assertLessEqual(len(request_ids), serve.KEEP_APPROVALS)
        self.assertTrue(all(types == {"needs_human", "needs_human_resolved"}
                            for types in request_ids.values()))
        self.assertEqual(isolated, set(range(len(events))))

    def test_rotation_quarantines_incompatible_reuse_and_keeps_first_matching_close(self):
        request_a = self.approval("reused", agent="agent-a", minutes_ago=5)
        early = self.resolution("reused", agent="agent-a", minutes_ago=4,
                                decision="deny")
        late = self.resolution("reused", agent="agent-a", minutes_ago=3,
                               decision="approve")
        request_b = self.approval("reused", agent="agent-b", minutes_ago=2)
        request_b["payload"]["action"] = "publish_note"
        ordered = [request_a, early, late, request_b]
        keep, isolated = serve._approval_keep_indexes(list(enumerate(ordered)))
        retained = [ordered[index] for index in sorted(keep)]
        self.assertEqual({item["agent_id"] for item in retained
                          if item["type"] == "needs_human"},
                         {"agent-a", "agent-b"})
        closes = [item for item in retained if item["type"] == "needs_human_resolved"]
        self.assertEqual([item["payload"]["decision"] for item in closes], ["deny"])
        self.assertEqual(isolated, set(range(len(ordered))))

        collided_first = [request_a, request_b, early]
        keep, _ = serve._approval_keep_indexes(list(enumerate(collided_first)))
        retained = [collided_first[index] for index in sorted(keep)]
        self.assertFalse(any(item["type"] == "needs_human_resolved"
                             for item in retained),
                         "a quarantined lifecycle cannot acquire a resolution")

    def test_shared_approval_lifecycle_fixture_matches_viewer_selection(self):
        fixture = os.path.join(os.path.dirname(__file__), "fixtures",
                               "approval-lifecycle.json")
        with open(fixture, encoding="utf-8") as stream:
            cases = json.load(stream)
        for case in cases:
            events = case["events"]
            keep, _ = serve._approval_keep_indexes(list(enumerate(events)))
            retained = [events[index] for index in sorted(keep)]
            requests = [item for item in retained if item["type"] == "needs_human"]
            closes = [item for item in retained
                      if item["type"] == "needs_human_resolved"]
            with self.subTest(case["name"]):
                self.assertEqual([item["ts"] for item in requests],
                                 [case["expected_request_ts"]])
                expected = ([] if case["expected_decision"] is None
                            else [case["expected_decision"]])
                self.assertEqual([item["payload"]["decision"] for item in closes], expected)

    def test_shared_immutable_request_fixture_uses_json_semantic_equality(self):
        fixture = os.path.join(os.path.dirname(__file__), "fixtures",
                               "approval-identity.json")
        with open(fixture, encoding="utf-8") as stream:
            cases = json.load(stream)
        for case in cases:
            left = self.approval(case["left"]["request_id"])
            left["payload"].update(case["left"])
            right = self.approval(case["right"]["request_id"])
            right["payload"].update(case["right"])
            with self.subTest(case["name"]):
                self.assertEqual(
                    serve._approval_lifecycle_identity(left) ==
                    serve._approval_lifecycle_identity(right),
                    case["compatible"],
                )

    def test_rotation_preserves_exact_whitespace_identity_and_append_order(self):
        request = self.approval(" request ", agent=" agent ")
        request["project"] = " life "
        request["ts"] = "2026-08-25T10:00:00.000Z"
        first = self.resolution(" request ", agent=" agent ", decision="approve")
        first["project"] = " life "
        second = self.resolution(" request ", agent=" agent ", decision="deny")
        second["project"] = " life "
        first["ts"] = second["ts"] = "2026-08-25T10:01:00.000Z"
        wrong = self.resolution("request", agent="agent", decision="deny")
        wrong["project"] = "life"
        events = [request, first, second, wrong]
        keep, _ = serve._approval_keep_indexes(list(enumerate(events)))
        retained = [events[index] for index in sorted(keep)]
        closes = [item for item in retained if item["type"] == "needs_human_resolved"]
        self.assertEqual([item["payload"]["decision"] for item in closes],
                         ["approve"],
                         "only the first exact close survives; conflicts and orphans do not")

    def test_approval_rotation_keeps_session_terminal_for_close_replay(self):
        knock = self.approval("parked", agent="parked-agent")
        activity = event("parked-agent", "tool_called", tool="Read")
        ended = event("parked-agent", "session_ended")
        close = self.resolution("parked", agent="parked-agent", decision="approve")
        lines = [json.dumps(item) for item in [knock, activity, ended, close]]
        tail = serve.carry_forward(lines, int(datetime.datetime.now(
            datetime.timezone.utc).timestamp() * 1000))
        retained = protocol_events(tail)
        self.assertEqual([item["type"] for item in retained],
                         ["needs_human", "session_ended", "needs_human_resolved"])
        self.assertEqual(retained[-1]["payload"]["decision"], "approve",
                         "rotation preserves the exact globally presentable decision")

    def test_rotation_keeps_first_close_by_append_index_not_timestamp(self):
        request = self.approval("bounded")
        request["ts"] = "2026-08-25T10:10:00.000Z"
        closes = []
        for minute in range(10):
            close = self.resolution("bounded", decision="deny")
            close["ts"] = f"2026-08-25T10:{minute:02d}:00.000Z"
            closes.append(close)
        valid = self.resolution("bounded", decision="approve")
        valid["ts"] = "2026-08-25T10:11:00.000Z"
        events = [request, valid, *closes]
        keep, _ = serve._approval_keep_indexes(list(enumerate(events)))
        retained = [events[index] for index in sorted(keep)]
        retained_closes = [item for item in retained
                           if item["type"] == "needs_human_resolved"]
        self.assertEqual(retained_closes, [valid])

    def test_approval_rotation_keeps_first_equal_timestamp_append(self):
        request = self.approval("cutoff-order")
        request["ts"] = "2026-08-25T10:10:00.000Z"
        first = self.resolution("cutoff-order", decision="approve")
        second = self.resolution("cutoff-order", decision="deny")
        first["ts"] = second["ts"] = "2026-08-25T10:08:00.000Z"
        closer = []
        for index in range(7):
            item = self.resolution("cutoff-order", decision="edit")
            item["ts"] = f"2026-08-25T10:09:0{index}.000Z"
            item["payload"]["extension"] = {"index": index}
            closer.append(item)
        events = [request, first, second, *closer]
        keep, _ = serve._approval_keep_indexes(list(enumerate(events)))
        retained = [events[index] for index in sorted(keep)]
        closes = [item for item in retained if item["type"] == "needs_human_resolved"]
        self.assertEqual([item["payload"]["decision"] for item in closes], ["approve"])

    def test_rotation_reconstructs_append_authority_and_independent_knocks(self):
        request = self.approval("projection")
        request["ts"] = "2026-08-25T10:00:00.000Z"
        future_activity = event("approver", "tool_called", tool="Read")
        future_activity["ts"] = "2026-08-25T23:00:00.000Z"
        close = self.resolution("projection")
        close["ts"] = "2026-08-25T09:00:00.000Z"
        plain = event("approver", "needs_human", message="Independent later knock")
        plain["ts"] = "2026-08-25T08:00:00.000Z"
        cases = [
            [request, future_activity, close],
            [request, future_activity, close, plain],
            [request, plain, close],
        ]
        script = """
const {createBrowserRuntime}=require('./viewer/browser-runtime.js');
const lines=JSON.parse(require('node:fs').readFileSync(0,'utf8'));
const cursor='v1:0123456789abcdef0123456789abcdef:1:2:3:99';
const runtime=createBrowserRuntime({now:()=>Date.parse('2026-08-25T10:02:00.000Z'),EventSource:null,
 setTimeout:()=>1,clearTimeout(){},fetch:async url=>url==='/villagers'?{ok:false}:
 ({ok:true,headers:{get:n=>n==='X-Burrow-Cursor'?cursor:null},text:async()=>lines.join('\\n')})});
runtime.poll().then(()=>process.stdout.write(JSON.stringify(runtime.snapshot().villagers.map(v=>({
 id:v.id,state:v.state,lastLine:v.lastLine,knock:v.knock&&v.knock.message,
 confirmations:v.approvals.map(item=>item.request_id)})))));
"""
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        now_ms = int(datetime.datetime(2026, 8, 25, 10, 2,
                                       tzinfo=datetime.timezone.utc).timestamp() * 1000)
        for events in cases:
            original = [json.dumps(item) for item in events]
            rotated = serve.carry_forward(original, now_ms)
            with self.subTest(types=[item["type"] for item in events]):
                projections = []
                for lines in (original, rotated):
                    completed = subprocess.run(
                        ["node", "-e", script], input=json.dumps(lines), text=True,
                        cwd=root, check=True, capture_output=True)
                    projections.append(json.loads(completed.stdout))
                self.assertEqual(projections[1], projections[0])

    def test_task_tie_projection_is_constant_space_under_ten_thousand_events(self):
        timestamp = "2026-08-25T10:01:00.000Z"
        post = {"v": 0, "ts": "2026-08-25T10:00:00.000Z", "source": "steward",
                "agent_id": "steward:api", "project": "life", "type": "task_posted",
                "payload": {"task_id": "stress", "title": "Stress",
                            "required_skills": ["research"], "posted_by": "api"}}
        transitions = []
        for index in range(10_000):
            claimant = "codex:holder-%05d" % index
            transitions.append({"v": 0, "ts": timestamp, "source": "steward",
                                "agent_id": claimant, "project": "life",
                                "type": "task_claimed", "payload": {
                                    "task_id": "stress", "title": "Stress",
                                    "claimant": claimant}})

        ordered = [post, *transitions, transitions[0], transitions[-1]]
        keep = serve._task_keep_indexes(list(enumerate(ordered)))
        retained = [ordered[index] for index in sorted(keep)]
        self.assertLessEqual(len(retained), 2,
                             "rotation retains only canonical post and transition")
        self.assertEqual(retained[-1]["payload"]["claimant"], "codex:holder-09999")

        reversed_input = [post, *reversed(transitions), transitions[-1], transitions[0]]
        reverse_keep = serve._task_keep_indexes(list(enumerate(reversed_input)))
        reverse_retained = [reversed_input[index] for index in sorted(reverse_keep)]
        self.assertEqual(
            [serve._task_event_identity(item) for item in retained],
            [serve._task_event_identity(item) for item in reverse_retained],
            "equal-time selection is independent of grouping, order, and exact replay")

    def test_matches_viewers_global_4000_line_window(self):
        sparse = json.dumps(event("sparse", "idle", 1))
        lines = [sparse] + [json.dumps(event("gone", "session_ended", 1, n=i))
                            for i in range(4000)]
        tail = serve.carry_forward(lines, int(datetime.datetime.now(
            datetime.timezone.utc).timestamp() * 1000))
        self.assertNotIn(sparse, tail)

    def test_latest_heartbeat_keeps_agent_live_without_consuming_history(self):
        actions = [json.dumps(event("a", "tool_called", 60 * 13,
                                    tool="Read", n=i))
                   for i in range(serve.KEEP_PER_AGENT + 10)]
        heartbeat = json.dumps(event("a", "heartbeat", 1, tool="Read"))
        tail = serve.carry_forward(actions + [heartbeat], int(datetime.datetime.now(
            datetime.timezone.utc).timestamp() * 1000))
        self.assertEqual(len(tail), serve.KEEP_PER_AGENT + 1)
        self.assertEqual(tail[-1], heartbeat)
        self.assertEqual(json.loads(tail[0])["payload"]["n"], 10)

    def test_heartbeat_after_session_end_revives_agent(self):
        ended = json.dumps(event("a", "session_ended", 2))
        heartbeat = json.dumps(event("a", "heartbeat", 1, tool="Read"))
        tail = serve.carry_forward([ended, heartbeat], int(datetime.datetime.now(
            datetime.timezone.utc).timestamp() * 1000))
        self.assertEqual(tail, [ended, heartbeat])

    def test_keeps_live_agents_and_drops_departed_ones(self):
        lines = [json.dumps(e) for e in [
            event("a", "task_started", 5, prompt="work"),
            event("gone", "task_started", 5, prompt="work"),
            event("stale", "idle", 60 * 20),          # 20 h ago: past the window
            event("gone", "session_ended", 4),
            event("a", "tool_called", 1, tool="Read"),
        ]]
        tail = serve.carry_forward(lines, int(datetime.datetime.now(
            datetime.timezone.utc).timestamp() * 1000))
        agents = {json.loads(line)["agent_id"] for line in tail}
        self.assertEqual(agents, {"a"})
        self.assertEqual(len(tail), 2)

    def test_preserves_original_order(self):
        lines = [json.dumps(event("a", "task_started", 3, prompt="one")),
                 json.dumps(event("b", "tool_called", 2, tool="Read")),
                 json.dumps(event("a", "idle", 1))]
        tail = serve.carry_forward(lines, int(datetime.datetime.now(
            datetime.timezone.utc).timestamp() * 1000))
        self.assertEqual(tail, lines)

    def test_caps_history_per_agent(self):
        lines = [json.dumps(event("a", "tool_called", 1, tool="Read", n=i))
                 for i in range(serve.KEEP_PER_AGENT + 40)]
        tail = serve.carry_forward(lines, int(datetime.datetime.now(
            datetime.timezone.utc).timestamp() * 1000))
        self.assertEqual(len(tail), serve.KEEP_PER_AGENT)
        self.assertEqual(tail, lines[-serve.KEEP_PER_AGENT:])

    def test_compaction_reload_keeps_child_lineage_outside_display_history(self):
        lineage = event("a-child", "task_started", 3,
                        prompt="delegated work", parent_agent_id="z-parent",
                        agent_type="reviewer")
        lines = [json.dumps(lineage), json.dumps(event("z-parent", "idle", 2))]
        lines.extend(json.dumps(event("a-child", "tool_called", 1,
                                             tool="Read", n=index))
                     for index in range(serve.KEEP_PER_AGENT + 1))

        tail = serve.carry_forward(lines, int(datetime.datetime.now(
            datetime.timezone.utc).timestamp() * 1000))
        script = """
const fs = require('node:fs');
const { reduce } = require('./viewer/projection.js');
const lines = JSON.parse(fs.readFileSync(0, 'utf8'));
const resident = {
  file: 'project.resident.json', valid: true, manifest_version: 1, home: 0,
  match: { project: 'burrow' },
  meta: { project: 'burrow', name: 'Maren', char: 'Monk', accent: '#a68a4f' },
  body: 'Resident', capabilities: {
    soul: {}, skills: [], memory: {}, routes: [], app_grants: []
  }
};
const village = reduce(lines, Date.now(), [resident]);
process.stdout.write(JSON.stringify(Object.fromEntries(
  village.map(v => [v.id, v.residency]))));
"""
        projected = subprocess.run(
            ["node", "-e", script], input=json.dumps(tail), text=True,
            cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            check=True, capture_output=True)

        self.assertIn(json.dumps(lineage), tail)
        self.assertEqual(json.loads(projected.stdout), {
            "a-child": "visitor", "z-parent": "resident",
        })

    def test_ignores_junk_lines(self):
        lines = ["not json", json.dumps({"type": "tool_called"}),
                 json.dumps(event("a", "idle", 1))]
        tail = serve.carry_forward(lines, int(datetime.datetime.now(
            datetime.timezone.utc).timestamp() * 1000))
        self.assertEqual(tail, [lines[-1]])


class RotationTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.events = os.path.join(self.tmp.name, "data", "events.jsonl")
        os.makedirs(os.path.dirname(self.events))
        self.previous = (serve.EVENTS, serve.MAX_LOG_BYTES, serve.ARCHIVE_DIR)
        serve.EVENTS = self.events
        serve.MAX_LOG_BYTES = 4096
        serve.ARCHIVE_DIR = ""
        serve._rotate_floor = 0

    def tearDown(self):
        serve.EVENTS, serve.MAX_LOG_BYTES, serve.ARCHIVE_DIR = self.previous
        serve._rotate_floor = 0
        self.tmp.cleanup()

    def write(self, events):
        with open(self.events, "a", encoding="utf-8") as f:
            for e in events:
                f.write(json.dumps(e, ensure_ascii=False) + "\n")

    def live_lines(self):
        with open(self.events, encoding="utf-8") as f:
            return f.read().splitlines()

    def archives(self):
        into = serve.archive_dir()
        if not os.path.isdir(into):
            return []
        return sorted(os.path.join(into, f) for f in os.listdir(into))

    def noisy_history(self):
        """A log well past the threshold: piles of chatter from a villager that
        went home and one that fell out of the 12 h window, then two live ones."""
        old = []
        for i in range(30):
            old.append(event("departed", "tool_called", 300, tool="Read",
                             detail="x" * 150, n=i))
            old.append(event("ancient", "tool_called", 60 * 30, tool="Grep",
                             detail="y" * 150, n=i))
        return old + [
            event("departed", "session_ended", 200),
            event("ancient", "idle", 60 * 29),
            event("live-1", "task_started", 5, prompt="the current task"),
            event("live-1", "tool_called", 4, tool="Read", detail="README.md"),
            event("live-2", "needs_human", 2, message="a question"),
        ]

    def test_rotates_and_preserves_the_village(self):
        self.write(self.noisy_history())
        before = village(self.live_lines())
        size = os.path.getsize(self.events)
        self.assertGreater(size, serve.MAX_LOG_BYTES)

        with serve.LOG_LOCK:
            serve.maybe_rotate()

        self.assertLess(os.path.getsize(self.events), size)
        self.assertEqual(len(self.archives()), 1)
        self.assertEqual(village(self.live_lines()), before)
        self.assertEqual(set(before), {"live-1", "live-2"})

    def test_archive_holds_everything_that_was_live(self):
        original = self.noisy_history()
        self.write(original)
        with serve.LOG_LOCK:
            serve.maybe_rotate()
        with open(self.archives()[0], encoding="utf-8") as f:
            archived = f.read().splitlines()
        self.assertEqual(archived,
                         [json.dumps(e, ensure_ascii=False) for e in original])

    def test_archive_directory_is_durable_before_live_log_is_modified(self):
        original = self.noisy_history()
        self.write(original)
        checkpoints = []

        def inspect_live(path):
            with open(self.events, "rb") as stream:
                checkpoints.append((path, stream.read()))

        with mock.patch.object(serve, "_fsync_parent", side_effect=inspect_live):
            with serve.LOG_LOCK:
                serve.maybe_rotate()

        self.assertEqual(len(checkpoints), 1)
        self.assertTrue(checkpoints[0][0].startswith(serve.archive_dir()))
        self.assertTrue(os.path.exists(checkpoints[0][0]))
        self.assertEqual(checkpoints[0][1],
                         "".join(json.dumps(item, ensure_ascii=False) + "\n"
                                 for item in original).encode())

    def test_already_open_append_descriptor_stays_on_live_log(self):
        self.write(self.noisy_history())
        fd = open(self.events, "a", encoding="utf-8")
        try:
            with serve.LOG_LOCK:
                serve.maybe_rotate()
            late = event("late", "task_started", 0, prompt="after rotation")
            fd.write(json.dumps(late) + "\n")
            fd.flush()
        finally:
            fd.close()
        self.assertIn("late", {item["agent_id"]
                               for item in protocol_events(self.live_lines())})

    def test_archive_names_carry_a_timestamp(self):
        self.write(self.noisy_history())
        with serve.LOG_LOCK:
            serve.maybe_rotate()
        name = os.path.basename(self.archives()[0])
        stamp = name[len("events-"):-len(".jsonl")]
        self.assertTrue(name.startswith("events-") and name.endswith(".jsonl"), name)
        datetime.datetime.strptime(stamp, "%Y%m%dT%H%M%SZ")

    def test_repeated_rotations_keep_every_segment(self):
        for _ in range(3):
            self.write(self.noisy_history())
            with serve.LOG_LOCK:
                serve.maybe_rotate()
        self.assertEqual(len(self.archives()), 3)

    def test_honours_a_separate_archive_directory(self):
        serve.ARCHIVE_DIR = os.path.join(self.tmp.name, "elsewhere")
        self.write(self.noisy_history())
        with serve.LOG_LOCK:
            serve.maybe_rotate()
        self.assertEqual(len(self.archives()), 1)
        self.assertTrue(self.archives()[0].startswith(serve.ARCHIVE_DIR))

    def test_disabled_by_zero_threshold(self):
        serve.MAX_LOG_BYTES = 0
        self.write(self.noisy_history())
        size = os.path.getsize(self.events)
        with serve.LOG_LOCK:
            serve.maybe_rotate()
        self.assertEqual(os.path.getsize(self.events), size)
        self.assertEqual(self.archives(), [])

    def test_no_thrash_when_every_event_is_still_live(self):
        """One busy agent whose whole (capped) history is current: there is
        nothing to reclaim, so we must not archive a copy on every append."""
        serve.MAX_LOG_BYTES = 512
        self.write([event("busy", "tool_called", 1, tool="Read", detail="z" * 100)
                    for _ in range(serve.KEEP_PER_AGENT)])
        for _ in range(5):
            with serve.LOG_LOCK:
                serve.maybe_rotate()
        self.assertLessEqual(len(self.archives()), 1)


class ServerRotationTest(unittest.TestCase):
    """The same thing through the HTTP surface the fleet actually uses."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.events = os.path.join(self.tmp.name, "data", "events.jsonl")
        os.makedirs(os.path.dirname(self.events))
        self.previous = (serve.EVENTS, serve.MAX_LOG_BYTES, serve.ARCHIVE_DIR)
        serve.EVENTS = self.events
        serve.MAX_LOG_BYTES = 8192
        serve.ARCHIVE_DIR = ""
        serve._rotate_floor = 0
        self.running_server = RunningServer(serve)
        self.server = self.running_server.server

    def tearDown(self):
        self.running_server.stop()
        serve.EVENTS, serve.MAX_LOG_BYTES, serve.ARCHIVE_DIR = self.previous
        serve._rotate_floor = 0
        self.tmp.cleanup()

    def write(self, events):
        with open(self.events, "a", encoding="utf-8") as stream:
            for item in events:
                stream.write(json.dumps(item, ensure_ascii=False) + "\n")

    def post(self, ev):
        conn = http.client.HTTPConnection(*self.server.server_address)
        conn.request("POST", "/events", json.dumps(ev),
                     {"Content-Type": "application/json"})
        response = conn.getresponse()
        response.read()
        conn.close()
        return response.status

    def get_events(self):
        _, _, body = self.get_events_response()
        return [line for line in body.splitlines() if line]

    def get_events_response(self, since=None):
        conn = http.client.HTTPConnection(*self.server.server_address)
        path = "/events" + ("?since=" + since if since else "")
        conn.request("GET", path)
        response = conn.getresponse()
        status = response.status
        headers = dict(response.getheaders())
        body = response.read().decode("utf-8")
        conn.close()
        return status, headers, body

    def project_jobs(self, lines):
        script = """
const fs = require('node:fs');
const jobs = require('./viewer/job-board.js');
const { validateEvent } = require('./viewer/projection.js');
const input = JSON.parse(fs.readFileSync(0, 'utf8'));
const state = jobs.createState();
jobs.fold(state, input, { validateEvent });
process.stdout.write(JSON.stringify(jobs.rows(state, Date.now())));
"""
        projected = subprocess.run(
            ["node", "-e", script], input=json.dumps(lines), text=True,
            cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            check=True, capture_output=True)
        return json.loads(projected.stdout)

    def task_event(self, task_id, etype, minutes_ago, claimant="codex:worker",
                   reason="session_failed"):
        payload = {"task_id": task_id, "title": "Task " + task_id}
        agent_id = "steward:api"
        if etype == "task_posted":
            payload.update(required_skills=["research"], posted_by="api")
        else:
            agent_id = claimant
            payload["claimant"] = claimant
            if etype == "task_done":
                payload["artifacts"] = ["notes/" + task_id + ".md"]
            if etype == "task_failed":
                payload["reason"] = reason
        return {"v": 0, "ts": ts(minutes_ago), "source": "steward",
                "agent_id": agent_id, "project": "life", "type": etype,
                "payload": payload}

    def seed_departed_chatter(self, n=60):
        """History that rotation can actually reclaim: an agent that went home."""
        with open(self.events, "a", encoding="utf-8") as f:
            for i in range(n):
                f.write(json.dumps(event("departed", "tool_called", 300,
                                         tool="Read", detail="o" * 200, n=i)) + "\n")
            f.write(json.dumps(event("departed", "session_ended", 299)) + "\n")

    def test_rotation_reset_preserves_cross_agent_task_lifecycles(self):
        """The real reset response must reconstruct every Steward task state.

        Posts belong to steward:api while claims and terminal evidence belong
        to claimants whose sessions may then end. Orphans remain explicit
        degraded evidence rather than acquiring invented post metadata.
        """
        serve.MAX_LOG_BYTES = 0
        lifecycle = [
            self.task_event("done", "task_posted", 9),
            self.task_event("done", "task_claimed", 8, "codex:done"),
            self.task_event("done", "task_done", 7, "codex:done"),
            event("codex:done", "session_ended", 6),
            self.task_event("failed", "task_posted", 9),
            self.task_event("failed", "task_claimed", 8, "codex:failed"),
            self.task_event("failed", "task_failed", 7, "codex:failed"),
            event("codex:failed", "session_ended", 6),
            self.task_event("reopened", "task_posted", 9),
            self.task_event("reopened", "task_claimed", 8, "codex:reopened"),
            self.task_event("reopened", "task_failed", 7, "codex:reopened",
                            "lease_expired"),
            event("codex:reopened", "session_ended", 6),
            self.task_event("orphan-claim", "task_claimed", 5, "codex:orphan"),
            self.task_event("orphan-done", "task_done", 4, "codex:orphan"),
            event("codex:orphan", "session_ended", 3),
            # Later facts arrive first; duplicates and older replay follow.
            self.task_event("unordered", "task_posted", 9),
            self.task_event("unordered", "task_done", 2, "codex:unordered"),
            self.task_event("unordered", "task_claimed", 8, "codex:unordered"),
            self.task_event("unordered", "task_posted", 10),
            self.task_event("unordered", "task_done", 2, "codex:unordered"),
            event("codex:unordered", "session_ended", 1),
        ]
        same_ts = ts(8)
        same_post = self.task_event("same-ms-reclaim", "task_posted", 9)
        same_expiry = self.task_event("same-ms-reclaim", "task_failed", 8,
                                      "codex:old-holder", "lease_expired")
        same_claim = self.task_event("same-ms-reclaim", "task_claimed", 8,
                                     "codex:new-holder")
        same_expiry["ts"] = same_ts
        same_claim["ts"] = same_ts
        lifecycle.extend([same_post, same_expiry, same_claim, same_expiry.copy()])
        # More terminal identities than the board retains prove that central
        # posts cannot leak back through per-agent history and resurrect work
        # deliberately omitted by task-ID capacity.
        for index in range(26):
            task_id = "capacity-%02d" % index
            claimant = "codex:capacity-%02d" % index
            lifecycle.extend([
                self.task_event(task_id, "task_posted", 14),
                self.task_event(task_id, "task_done", 13, claimant),
                event(claimant, "session_ended", 12),
            ])
        self.write(lifecycle)
        status, headers, initial_body = self.get_events_response()
        self.assertEqual(status, 200)
        cursor = headers["X-Burrow-Cursor"]
        before = self.project_jobs(initial_body.splitlines())

        self.seed_departed_chatter(80)
        serve.MAX_LOG_BYTES = 512
        status, headers, reset_body = self.get_events_response(cursor)
        self.assertEqual(status, 200)
        self.assertEqual(headers.get("X-Burrow-Reset"), "1")
        after = self.project_jobs(reset_body.splitlines())
        self.assertEqual(after, before)

        rows = {row["id"]: row for row in after}
        self.assertEqual(rows["done"]["state"], "done")
        self.assertEqual(rows["failed"]["state"], "failed")
        self.assertEqual(rows["reopened"]["state"], "open")
        self.assertEqual(rows["unordered"]["state"], "done")
        self.assertEqual(rows["same-ms-reclaim"]["state"], "claimed")
        self.assertEqual(rows["same-ms-reclaim"]["claimant"], "codex:new-holder")
        self.assertIsNone(rows["orphan-claim"]["required_skills"])
        self.assertIsNone(rows["orphan-done"]["required_skills"])

    def test_rotation_preserves_event_granular_capacity_without_restoring_evicted_post(self):
        """A claim may reintroduce an evicted ID, but not its missing post fields."""
        serve.MAX_LOG_BYTES = 0
        start = datetime.datetime(2026, 8, 25, 10, 0, tzinfo=datetime.timezone.utc)
        posts = []
        for index in range(serve.KEEP_TASKS + 1):
            item = self.task_event("task-%02d" % index, "task_posted", 1)
            item["ts"] = (start + datetime.timedelta(milliseconds=index)).isoformat(
                timespec="milliseconds").replace("+00:00", "Z")
            item["payload"]["required_skills"] = ["skill-%02d" % index]
            posts.append(item)
        claim = self.task_event("task-00", "task_claimed", 0, "codex:reintroduced")
        claim["ts"] = (start + datetime.timedelta(seconds=1)).isoformat(
            timespec="milliseconds").replace("+00:00", "Z")
        claim["payload"]["title"] = "Claimed after eviction"
        self.write([*posts, claim])

        status, headers, initial_body = self.get_events_response()
        self.assertEqual(status, 200)
        before = self.project_jobs(initial_body.splitlines())
        before_rows = {row["id"]: row for row in before}
        self.assertIsNone(before_rows["task-00"]["required_skills"])
        self.assertEqual(before_rows["task-00"]["title"], "Claimed after eviction")
        self.assertNotIn("task-01", before_rows)

        self.seed_departed_chatter(80)
        serve.MAX_LOG_BYTES = 512
        status, reset_headers, reset_body = self.get_events_response(
            headers["X-Burrow-Cursor"])
        self.assertEqual(status, 200)
        self.assertEqual(reset_headers.get("X-Burrow-Reset"), "1")
        after = self.project_jobs(reset_body.splitlines())
        self.assertEqual(after, before,
                         "rotation/reset preserves canonical rows and missing-post truth")

    def test_concurrent_posts_survive_rotation(self):
        self.seed_departed_chatter()
        agents = ["claude-code:%d" % i for i in range(4)]
        sent = []
        lock = threading.Lock()

        def hammer(agent):
            for i in range(60):
                ev = event(agent, "tool_called", 0, tool="Read", detail="q" * 200, n=i)
                self.assertEqual(self.post(ev), 204)
                with lock:
                    sent.append(ev)

        threads = [threading.Thread(target=hammer, args=(a,)) for a in agents]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        archives = sorted(os.listdir(serve.archive_dir())) if os.path.isdir(serve.archive_dir()) else []
        self.assertTrue(archives, "expected at least one rotation under load")

        # every accepted POST is somewhere on disk: live tail or an archive
        seen = set(self.get_events())
        for name in archives:
            with open(os.path.join(serve.archive_dir(), name), encoding="utf-8") as f:
                seen.update(f.read().splitlines())
        for ev in sent:
            self.assertIn(json.dumps(ev, ensure_ascii=False), seen)

        # and the live log alone still shows all four villagers, working
        drawn = village(self.get_events())
        self.assertEqual(set(drawn), set(agents))

    def test_local_mode_rotation_happens_on_read(self):
        """Nothing POSTs in local mode — emitters append to the file directly —
        so the read path has to keep it bounded too."""
        self.seed_departed_chatter()
        with open(self.events, "a", encoding="utf-8") as f:
            f.write(json.dumps(event("live", "task_started", 4, prompt="now")) + "\n")
            f.write(json.dumps(event("live", "idle", 1)) + "\n")
        size = os.path.getsize(self.events)
        self.assertGreater(size, serve.MAX_LOG_BYTES)
        lines = self.get_events()
        self.assertLess(os.path.getsize(self.events), serve.MAX_LOG_BYTES)
        self.assertTrue(os.listdir(serve.archive_dir()))
        self.assertEqual(set(village(lines)), {"live"})


if __name__ == "__main__":
    unittest.main()
