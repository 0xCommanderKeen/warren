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
