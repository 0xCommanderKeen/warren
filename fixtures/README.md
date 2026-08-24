# fixtures

Recorded sequences of protocol events (see [../docs/protocol.md](../docs/protocol.md))
for looking at the viewer without a live fleet. `play.py` re-stamps each event with
the current time and appends it to a log, one every couple of seconds, so a
projection rule can be watched end to end.

```sh
python3 fixtures/play.py &                                  # writes /tmp/burrow-play.jsonl
BURROW_EVENTS=/tmp/burrow-play.jsonl python3 serve.py     # open http://127.0.0.1:8737
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
BURROW_EVENTS=/tmp/burrow-demo.jsonl python3 serve.py
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
