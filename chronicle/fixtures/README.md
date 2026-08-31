# fixtures

Recorded sequences of protocol events (see [../docs/protocol.md](../docs/protocol.md))
for looking at the viewer without a live fleet. `play.py` re-stamps each event with
the current time and appends it to a log, one every couple of seconds, so a
projection rule can be watched end to end.

```sh
python3 fixtures/play.py &                                  # writes /tmp/chronicle-play.jsonl
CHRONICLE_EVENTS=/tmp/chronicle-play.jsonl python3 serve.py     # open http://127.0.0.1:8737
```

The village still never lies: every sprite you see is driven by an event in the
fixture. The fixture is the honest part — it says what happened; the viewer just
projects it.

## `demo.py`

`play.py` replays a recorded log. `demo.py` scripts one in code, so a scenario can
loop, hold a state, and reach places that are awkward to record — a villager going
stale takes 30 real minutes, so the scenario backdates it instead.

```sh
python3 fixtures/demo.py --list
python3 fixtures/demo.py                     # "village", looping
python3 fixtures/demo.py library --once      # one library round trip
python3 fixtures/demo.py states --speed 2    # twice as fast
CHRONICLE_EVENTS=/tmp/chronicle-demo.jsonl python3 serve.py
```

| scenario  | what it shows |
|-----------|---------------|
| `library` | one villager walks to the library and back |
| `slots`   | three share the library; one leaves without nudging the others |
| `states`  | working, researching, resting, knocking and stale, side by side |
| `village` | a full fleet: overlapping tasks, a knock that gets answered, someone going home |

The same line holds here, and it is the reason this writes a log instead of
driving the viewer directly: a debug tool that can place a sprite can show a
village that never happened, and then the one rule is only true in production.
What is faked is the *fleet*. Everything downstream of the log — projection,
placement, doorway light — is the real thing, on the same path a live fleet takes.

For the day/night tint the viewer takes query parameters instead, no events
needed: `?time=21:00`, `?phase=night`, or `?cycle=60` for a full day a minute.

## `library-walk.jsonl`

Two agents, one shared location. What to watch for:

1. **scholar-1** takes a task and reads a file — works at its own house.
2. It calls `WebSearch` → **researching**, so it walks to the library and stands
   at the door.
3. **scholar-2** starts up at its own house, then calls `WebFetch` → it walks to
   the library too and takes a *different* slot beside scholar-1.
4. scholar-1 calls `WebFetch` — still research, still the library: it hops
   because a real event arrived, but it does not move.
5. scholar-1 calls `Edit` → **crafting**, not research: it walks home. scholar-2
   keeps the slot it was already standing in.
6. Both finish (`idle`) and rest at their own doors; the library goes dark.

## `meaningful-locations.jsonl`

One worker visits every meaningful work location in sequence: research goes to the
library, inbox work goes to the post office, editing and shell work go to the
workshop, and an uncovered `Read` returns home. A second crafter overlaps at the
workshop to demonstrate stable shared-building slots.

The delegation step is the one to watch: the traveler walks to the crafter's
door, because the crafter is the only other villager in the village. Nothing in
the log says the work went to *that* villager — the protocol has no delegate
identity — so all the map claims is a door with somebody behind it (see
docs/protocol.md, "What delegation may claim"). Replay it with the crafter's two
lines removed and the traveler stays home instead.

The final idle events send both villagers home.

## Lineage and residency fixtures

`tests/fixtures/codex-hooks.jsonl` and `tests/fixtures/claude-subagents.jsonl` are
redacted hook-shape fixtures. Their end-to-end adapter tests prove parent/child
lineage, independent villager identities, and matching-child stop behavior for both
runners. Resident promotion, exact-identity priority, stable homes and the shared
Visitor lodge used to be folded from fixed events by `tests/residents.test.js`;
that test went with the viewer (warren#219) and
`tests/test_village_state.py::test_projects_resident_and_visitor_identity_and_lifecycle`
now proves the same projection in Python, still without modifying event history.
Checked-in manifests are validated end to end by `tests/test_residents.py`.
