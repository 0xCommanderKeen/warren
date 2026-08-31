# One Spool beneath the durable logs

Design analysis for issue #221. This is the design conversation the issue demanded
before any code moved: what each of the three durable logs actually guarantees
today, which of those guarantees are the same guarantee wearing three names, and
which are genuinely different and must stay different.

Read this before changing `hooks/durable.py`, `hooks/emit.py`, or
`notification_persistence.py`.

## The three sites

Line numbers are from the tree this analysis was written against, not the issue's
2026-08-26 numbers.

| Site | Where | Shape |
| --- | --- | --- |
| **Knock journal** | `notification_persistence.py` `compact_locked` / `recover` / `journal` / `record_attempt` / `commit_terminal` | JSON records, active + `.replay.*` generations, dedupe by `knock_key(event)` |
| **Terminal ledgers** | `notification_persistence.py` `remember_batch` / `load_ledger` / `contains` | One opaque string per line, single generation, dedupe by the string itself |
| **Primary outbox** | `hooks/emit.py` `_update_outbox` / `_journal_outbox` / `_read_durable_outbox_snapshot` | JSON records, active + `.journal.*` generations, dedupe by `(target, delivery_id)` |
| **Deferred local spool** | `hooks/emit.py` `_defer_local` / `_replay_deferred` / `_compact_deferred_locked` | JSON records, active + `.replay.*` generations, dedupe by `_burrow_deferred_id` (a stored member name, kept at its pre-rename spelling — see below) |

Four structures, not three: `notification_persistence.py` carries both a
generational journal and a flat ledger, and they have opposite capacity policies.

All four sit on `hooks/durable.py`'s file primitives — `stage_lines`,
`stage_json`, `publish_staged`, `retire_files`, `fsync_parent`, `publish_lines`
— and each re-derives the same walk from those primitives up to a bounded
ordered log.

## The guarantees, as the code enforces them today

Nine distinct guarantees are in force. Naming them is the whole point of this
document: three of the four sites implement most of them, and the places where a
site *doesn't* are the places a naive merge would silently change behaviour.

### G1 — Publication ordering

Write the replacement to a staging path, `flush()`, `fsync()` the file
descriptor, `os.replace()` onto the target, then `fsync` the parent directory.
Nothing observes a partially written generation, and the rename is durable before
the call returns.

Owned by `durable.stage_lines` + `durable.publish_staged`. All four sites use it
unchanged.

*Proven by* `tests/test_durable.py::test_publish_orders_file_fsync_replace_directory_fsync_and_retirement`,
which asserts the literal operation sequence `["replace", "dir-fsync", "retire",
"dir-fsync"]` through mocks — not the end state.

### G2 — Publish, then retire

The replacement is durable *before* any source generation is unlinked. Every
accepted record therefore always has at least one durable home; a crash in the
window between replace and unlink leaves the record in two places, which is
harmless because every reader dedupes (G9).

- Knock journal: `publish_compaction` → `durable.publish_lines(path, lines,
  retire=durable.replay_paths(path))`.
- Outbox: `_update_outbox` publishes `OUTBOX`, then `durable.retire_files(journals)`.
- `_journal_outbox`: publishes the new `.journal.*`, then retires the old ones.
- Deferred: `publish_staged` onto the active path, then
  `retire_files(_deferred_generations(path)[1:])`.

The ledger has no sources to retire, so G2 is vacuous there.

*Proven by* the same `test_durable.py` sequence assertion, plus each site's own
crash tests.

### G3 — Evidence before erasure

A torn suffix found in a source generation is copied to a quarantine file, that
file is fsynced, the quarantine set is pruned to its bound, the directory is
fsynced — and only then is the source retired. The corrupt bytes therefore
survive the deletion of the file they came from.

Order is publish → quarantine → retire, and it is deliberate: quarantining before
publication would keep the evidence but risk having no durable authority at all.

- `_update_outbox`: `for journal, torn in journals: if torn: _quarantine_outbox_tail(torn)`
  sits between `publish_staged` and `retire_files`.
- `_compact_deferred_locked`: same position, via `_quarantine_deferred_tail`.

**Two sites deviate, on purpose or by omission:**

- `_journal_outbox` reads its inputs as `valid, _ = _read_outbox_journal(journal)`
  and **discards the torn bytes without quarantining them**, then retires the
  journal. Evidence is lost on the lock-contention path but not on the main path.
  This is preserved as-is; changing it is a behaviour change, not a refactor.
- The knock journal never quarantines. `prune_terminal_generations` *retains*
  unparseable lines verbatim inside the generation (`except ValueError:
  retained.append(line)`), and `recover` marks the generation `complete=False`
  instead. Its torn bytes are quarantined by never being deleted (G7).

### G4 — Victim before omission

A record evicted for capacity is durably recorded somewhere else *before* the
authority that omits it is published. If that recording fails, the publication is
aborted. A crash may over-report a drop; it can never create a silent one.

- Deferred: `if dropped and not _diagnose("drop", …): raise OSError("local
  deferred drop diagnostic was not durable")` — before `stage_lines`.
- Knock journal: `compact_locked` calls `remember_batch(DROPPED, victims,
  preserve_existing=retained)` and raises `OSError("knock victims exceed durable
  terminal capacity")` unless every victim landed — before `publish_compaction`.

**Two sites deviate:**

- The outbox reports *after* the fact: `dropped, queued = _update_outbox(…)` then
  `if dropped: _diagnose("drop", …)` in `deliver`. A crash between publication and
  the diagnostic loses the report. Accepted: the outbox's contents are already
  best-effort transport copies whose loss is bounded by the local log fallback.
- `remember_batch` *inverts* G4: rather than dropping a requested key and
  reporting it, it raises `OSError("terminal batch exceeds durable ledger
  capacity")` **and leaves the file untouched**. A terminal outcome that cannot be
  durably recorded is a refusal, not a loss.

### G5 — Staging is never authority

An orphan staging file is discarded on recovery and never promoted, *even when it
is empty or valid JSONL*, because syntactic validity says nothing about whether
the writer finished the generation it intended. Only `os.replace` commits.

`_recover_outbox` states this explicitly and unlinks `OUTBOX.pending` + fsyncs the
directory. The other sites get it implicitly: their staging names are stable per
target, so the next `stage_lines` truncates whatever was left.

### G6 — Generation handoff

Under the stable sidecar lock: if more than one generation already exists,
compact first; then, only if the active file is non-empty, `os.replace(active,
<path>.replay.<uuid4>)` and fsync the directory. The active path is now absent so
concurrent appenders start a fresh generation, and the handed-off generation is
immutable.

The pre-handoff compaction is what bounds physical copies — without it every
recovery would add a generation.

- `NotificationPersistence.recover`: `if (generations and has_active) or
  len(generations) > 1: self.compact_locked(path)`, then the rename.
- `_replay_deferred`: `if len(_deferred_generations(path)) > 1:
  _compact_deferred_locked(path)`, then the rename.

The outbox has **no handoff**. Its auxiliary `.journal.*` generations are created
by a *writer* that could not get the main lock, not by a reader taking authority.
This is the structural difference the issue warned about: the outbox's generations
flow contention → main, while the journal's and the deferred spool's flow
active → replay → drained.

### G7 — Retire only after the ack is durable

A generation is unlinked only once its records are provably durable somewhere
else, and the directory is fsynced after.

- Deferred: records are written into the live `events.jsonl`, `live.flush()`,
  `os.fsync(live.fileno())`, *then* `os.unlink(generation)` + directory fsync,
  per generation. A crash between the fsync and the unlink is idempotent because
  the replayed records carry `_burrow_deferred_id` and the next pass finds them
  in the live log.
- Knock journal: `retire_replay_if_terminal` retires only when the generation
  parsed **completely** *and* every event in it is terminal in the delivered/drop
  ledgers. An incomplete (torn) generation is therefore never retired — it stays
  until a compaction folds it back in. That is this site's answer to G3.
- Outbox: the ack is a re-publication that omits the delivered keys
  (`_update_outbox(delivered_keys, [])`), not a retirement.

### G8 — Bounded front trim

Evict oldest-first until both the record count and the encoded byte total are
within their caps. The measure is always encoded UTF-8 bytes of the serialized
line, never in-memory size.

`_bounded_records` (emit), the `while lines and (len(lines) > records or total >
byte_limit)` loops in `compact_locked` and `remember_batch`. Three copies of the
same loop.

### G9 — Dedupe by key, last value wins in the first slot

When the same key appears more than once across generations, the **last** value
read wins, but it occupies the **first** occurrence's queue position. This is what
makes G2's crash-window duplicates harmless *and* order-preserving: the newer copy
of a record (with, say, a bumped `attempt_generation`) replaces the older one
without jumping the queue.

- Outbox: `_dedupe_outbox_records`, key `OutboxRecordKey(target, delivery_id)`.
- Deferred: the identical loop inlined in `_compact_deferred_locked`, key
  `_burrow_deferred_id`. That key keeps its pre-rename spelling on purpose: it is
  a member of records already on disk, and the crash-idempotence above depends on
  a record written before an upgrade still being recognised after one. Renaming
  it would not degrade — it would make a spool generation written by the previous
  build replay as if it had never been seen.
- Knock journal: `latest[knock_key(event)] = entry` on an `OrderedDict` — assigning
  to an existing key updates the value without moving it. Same rule.

**The ledger deviates:** `remember_batch` explicitly *promotes* a re-remembered key
to the end (`remembered.pop(key)` then `remembered[key] = None`), unless it is
already last. Last value wins in the **last** slot — LRU, not FIFO — because the
ledger's eviction order is its retention policy. It also skips the write entirely
when nothing changed, which no other site does.

## What unifies, and what does not

**Unifies (goes into `Spool`):** G1, G2, G3, G5, G6, G7's mechanical half (retire
after a caller-supplied ack), G8, G9. These are nine-tenths of the duplicated
code and all of the subtle ordering.

**Does not unify (stays in an adapter, documented):**

1. **The outbox's three-lock protocol.** Thread lock → blocking transaction lock →
   *non-blocking* main lock, with an entirely separate journal-writing path taken
   when the main lock is held. Auxiliary writers never take the main lock, which is
   what makes the order acyclic. This is transport policy, not spool mechanics.
2. **The outbox's two-file publish transaction.** `OUTBOX` and
   `OUTBOX.schedule.json` are replaced in one `publish_staged` call, outbox first.
   A crash between them leaves a stale schedule, which is safe only because
   `_read_schedule` treats any unreadable or oversized schedule as `{}`. `Spool`
   exposes this as an `extra=` parameter on `publish` rather than pretending the
   schedule is a spool.
3. **`attempt_generation` fairness.** Per-target round-robin scheduling that
   rewrites records in place before publication. Pure payload semantics.
4. **The ledger's LRU promotion, no-op skip, and capacity refusal (G4 inverted).**
   The adapter builds the promoted order itself and turns `Spool.bound`'s victims
   into a raised `OSError` instead of a drop.
5. **`prune_terminal_generations`.** Rewrites each generation *in place*,
   individually, rather than folding them together — deliberately, so that
   `recover`'s per-generation `complete` accounting survives. It also stages under
   a stable per-target name (`path + ".prune-" + sha256(abspath(generation))`)
   chosen to sit outside the `.replay.*` glob. `Spool.publish_onto` supports it;
   the policy stays in `notification_persistence.py`.
6. **Damage tolerance.** Two policies survive as `Spool.read(damage=…)`, taken
   explicitly rather than guessed:
   - `STOP_AT_DAMAGE` — the first bad line ends the generation and everything from
     it onward is a torn tail (outbox journals, deferred).
   - `SKIP_DAMAGE` — bad lines are skipped and the generation is marked
     incomplete, which is what blocks its retirement (knock journal `recover`,
     and the live outbox, whose own writer already proved it whole).

   A third policy — preserving bad lines verbatim as opaque records — was
   designed and then dropped: only `prune_terminal_generations` needs it, it is
   a raw-text operation rather than a record operation, and its crash behaviour
   is pinned by a test that patches the publish seam. It stays hand-rolled.

   The *shape* check is a codec concern and also differs: a decoded non-object is
   damage to the outbox and an absent line to the deferred spool. `json_record`
   raises `ValueError` for the former, `emit.py`'s own `_deferred_record` returns
   `None` for the latter (it also synthesizes the replay id), and `json_entry`
   additionally treats a blank line as absent so the knock journal's blank-line
   tolerance survives.

   The two terminal ledgers read with `SKIP_DAMAGE` and then *refuse to write*
   when the generation was not whole — see "Two things the migration got wrong
   first" below.
7. **`_journal_outbox`'s silent torn discard** (G3 deviation above), preserved
   verbatim.
8. **Where a quarantine file is named.** The outbox names every sample after the
   spool (`primary-outbox.jsonl.torn.*`) even when the bytes came from a journal;
   the deferred spool names it after the generation the bytes came from
   (`events.jsonl.deferred.replay.<id>.torn.*`). Both roots are globbed by
   existing tests, so this is a `torn_at_source` flag with its own test rather
   than something to quietly unify. The retention budget is shared across both
   roots either way.

## The interface, as shipped

```python
class Spool:
    """One bounded, ordered, crash-safe log: an active authority plus immutable
    auxiliary generations, behind a stable sidecar lock."""

    def __init__(self, path, limits=None, decode=json_record, encode=encode_json,
                 key=None, order=None, generation_prefix=REPLAY_PREFIX,
                 torn_files=8, torn_bytes=256 * 1024, torn_at_source=False)
```

`limits` is a callable returning `(records, bytes)`, so live settings are re-read
per operation instead of frozen at construction. Construction performs no I/O —
which is what lets `emit.py` build a spool per call and pick up patched module
globals, and lets the notification store build one per ledger kind.

**Names** — `pending_path`, `lock_path(suffix="")`, `generation_path(name)`,
`generation_paths()`, `generations()` (active first). A quarantine file never
counts as a generation even when its name matches the glob.

**Locking** — `lock(suffix="", exclusive=True, blocking=True, create=False)`, a
context manager yielding the lock file, or `None` when a non-blocking
acquisition found it held. Contention is a result, never an exception.

**Reading** — `read(path=None, damage=STOP_AT_DAMAGE)` → `Generation(path,
records, torn, complete)`, or `None` when the file cannot be read at all —
deliberately distinguishable from an empty generation, because one is retirable
and the other is not. `snapshot(damage, active_damage)` returns the same for
every generation; `collect(...)` returns the deduped, ordered records.

**Policy** — `dedupe(records)` (G9), `arrange(records)` (dedupe then `order`),
`bound(records, max_records=None, max_bytes=None)` → `(kept, victims)` (G8).

**Writing** — `publish(records, target=None, staging=None, retire=(),
quarantine=(), extra=(), encode=None)` performs G1 → G3 → G2 in that order;
`handoff(generation=None)` (G6) → the new generation path or `None`;
`retire(paths)`; `quarantine_tail(torn, source=None)`; `discard_staging()` (G5).

`target`/`staging` cover the in-place generation rewrite; `extra` takes
already-staged `(pending, target)` pairs for a multi-file publish; `encode`
overrides the codec for a caller holding pre-encoded lines. Staging order among
pending files is immaterial — staging is never authority.

`Spool` owns no locks it is not asked for, no process state, and no delivery
policy. It owns exactly the crash ordering — the level the issue said
`durable.py` sat one step below.

## Two things the migration got wrong first

Both were found by a differential review of old versus new on the same
fixtures, not by the suite — neither was covered by a test, which is why both
now have one.

1. **A dropped `isinstance(event, dict)` guard.** A journal line is an object,
   but its `event` member may be `null` or a scalar. The old code checked this
   in two places; the first migration checked it in neither, so such a line
   raised `AttributeError` out of `read_journal_keys`, `compact_locked`,
   `journal`, `record_attempt` and `commit_terminal`. `AttributeError` is not
   `OSError`, so it escaped the degradation every one of those callers relies
   on. `journal_event(entry)` now holds the guard in one place.

2. **A damaged ledger was silently truncated.** Reading the old ledger in text
   mode meant an undecodable byte raised and the file was *never rewritten*.
   Reading it through `Spool` under `STOP_AT_DAMAGE` returned only the prefix,
   and `remember_batch` then published exactly that — permanently forgetting
   every key behind the damage. That is the same loss the capacity refusal
   exists to prevent, arriving by a different road. Ledger reads now use
   `SKIP_DAMAGE` (so a key behind the damage still suppresses a knock), and
   `remember_batch` refuses to rewrite a ledger it could not read whole.

The general lesson for anyone extending `Spool`: a shared reader makes damage
*policy* explicit, which is the win, but it also means every call site inherits
whatever the default happens to be. Damage tolerance is a durability decision
at each site. Choose it deliberately.

## What this did not change

`docs/protocol.md`'s transport section is unchanged, and that is the right
outcome rather than a missed win. It documents the *observable* durability
contract — capacity ceilings, physical crash-copy bounds, what survives a
restart — and none of that moved. The prose there looks duplicated because two
transports really do make the same promises, not because one implementation was
described twice; deleting either copy would remove a guarantee a reader needs,
to save lines in a document nobody was struggling with. The mechanism behind
both now has one home, and this file is it.
