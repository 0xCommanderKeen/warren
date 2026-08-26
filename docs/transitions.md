# Durable transitions and the facts that go with them (v0)

Every durable state change steward makes has a matching fact in burrow's log, and the two
have to agree. A row that changed with no fact is work the village cannot see; a fact with
no row is the village rendering work that never happened. Neither is recoverable by
reading the other, because **persistence and event delivery are not one transaction and
never will be**: the store is SQLite on the NAS and the emitter is an HTTP POST to another
process with a local JSONL fallback. What can be made true is narrower and worth saying
out loud:

> The fact is handed to the emitter **only** when the durable change actually happened in
> *this* call, and it is handed over exactly once.

That sentence is the transition modules' whole job. Delivery after that is the emitter's business
(`docs/api.md`, `steward/events.py`): a POST that fails still lands in the fallback log,
emitting never raises, and no transition is rolled back because a village was unreachable.

This document is the behavioral contract. Part 1 is the matrix — every transition, its
guard, its outcomes, and its fact. Part 2 is the two interface designs that were compared.
Part 3 records which one was approved and who owns what.

The matrix was written from the code as it stood before steward #123, when each pairing
was the caller's to get right; the **owner** row of each entry now names the module that
holds it. Nothing else in the matrix changed, because nothing else was allowed to: the
refactor preserves database state, event names, payloads, identities, ordering,
idempotency, fallback delivery, and every public result for equivalent inputs.

---

## 1. The transition matrix

Vocabulary used in the outcome columns:

| outcome | meaning | writes | emits |
|---|---|---|---|
| **applied** | the durable change happened in this call | yes | its fact, where the act has one |
| **refused** | a precondition said no before anything was written | no | nothing |
| **replayed** | already recorded; the recorded outcome is returned | no | nothing |
| **expired** | past its deadline; deny-by-default has the last word | no | nothing |
| **superseded** | a conditional write lost to another writer | no | nothing |
| **answered** | steward recorded the answer itself, deliberately knocking on nobody | yes | nothing |

Note the direction of the promise. Every fact belongs to an *applied* transition; not
every applied transition has a fact. Two acts deliberately say nothing: a budget resume
answered through the API (the decision that triggered it already emitted
`needs_human_resolved`, and saying it twice would put two answers in the log for one
question), and the repeat-deny guard. The second gets its own outcome name, `answered`,
rather than being folded into `applied`, because it is the only case where the row itself
is written *already resolved* — steward answering a resident on the human's behalf — and
that is worth a caller being able to see.

### 1.1 Task transitions

#### T1 — post

| | |
|---|---|
| **owner** | `TaskTransitions.post`, called by `POST /jobs` |
| **precondition** | none beyond request validation |
| **guard** | unconditional `INSERT INTO jobs` |
| **before → after** | (no row) → `status=open`, `claimant=NULL`, `assignee=NULL`, `posted_by='api'` |
| **outcomes** | applied only |
| **fact** | `task_posted`, `agent_id=steward:api`, `project=steward`, payload `{task_id, title, required_skills, posted_by}` |
| **lineage** | none — a posted notice has no parent |
| **redaction** | none |
| **delivery failure** | fallback log; the row stands |
| **callers** | `POST /jobs` only (no CLI) |
| **tests** | `tests/test_api.py` |

#### T2 — claim (open board)

| | |
|---|---|
| **owner** | `TaskTransitions.claim`, called by `Dispatcher.claim` |
| **precondition** | resident is active and declares `board.claim`; task is `open` with `assignee IS NULL`; `required_skills ⊆ claimable_skills` |
| **guard** | `UPDATE jobs SET status='claimed' … WHERE task_id=? AND status='open'`; `rowcount==0` → next candidate |
| **before → after** | `open` → `claimed`, `claimant=<agent id>`, `claimed_at=now`, `lease_expires_at=now+board.lease_s` |
| **outcomes** | applied (a row was won) · refused (nothing open, nothing matching, or every race lost — one fact from the caller's side: this resident has no work) |
| **fact** | `task_claimed`, `agent_id=<claimant agent id>`, `project=<resident project>`, payload `{task_id, title, claimant}` + `parent_task_id` **only when set** |
| **lineage** | `parent_task_id` copied from the row, omitted when absent |
| **clock** | read per claim, not per dispatch (#73) — the Nth lease of a slow drain is still a full lease |
| **callers** | `Dispatcher._drain` |
| **tests** | `tests/test_board.py` |

#### T3 — take delivery (delegated inbox)

Identical to T2 except: `Store.claim_next_delegated`, narrowed by `assignee=?`, no skill
match, lease `Dispatcher.delegation_lease_s`. The fact is the *same* `task_claimed` — a
delegated item is a task addressed to one resident, and the village learns of its pickup
the way it learns of any other.

#### T4/T5 — finish (done / failed)

| | |
|---|---|
| **owner** | `TaskTransitions.finish`, called by `Dispatcher._record` |
| **precondition** | the session ran (or was refused admission, which still closes the claim) |
| **guard** | `UPDATE jobs … WHERE task_id=? AND status='claimed' AND claimant=? AND (lease IS NULL OR claimed_at=lease)` — the lease token is `claimed_at` from *this* claim (#72) |
| **before → after** | `claimed` → `done`/`failed`, `outcome`, `reason`, `artifacts`, `finished_at`, `lease_expires_at=NULL` |
| **outcomes** | applied · superseded (`rowcount==0`: the lease died mid-session and the task is somebody else's now — **nothing is emitted**, and the caller reports `status=failed, reason="lease lost while the session was running"` carrying the *pre-close* row) |
| **fact (done)** | `task_done`, `agent_id=<claimant>`, `project=<resident project>`, payload `{task_id, title, claimant, artifacts}` + `run_id` **when set** + `parent_task_id` **when set** |
| **fact (failed)** | `task_failed`, same identity, payload `{task_id, title, claimant, reason}` + `run_id` + `parent_task_id`, `reason` truncated to 500 chars |
| **status/reason derivation** | `done` iff `RunResult.ok`; otherwise `failed` with `reason = f"{outcome}: {summary()}"` — one rule, written in `TaskTransitions.finish` and nowhere else, so a caller cannot decide two of {status, reason, event type} and disagree with the third |
| **run_id** | this attempt's registry row, never the task id: a task claimed, dropped and re-claimed is two sessions (#39) |
| **callers** | `Dispatcher.work` (both the admitted path and the admission-refused path) |

#### T6 — lease expiry

| | |
|---|---|
| **owner** | `TaskTransitions.expire_leases`, called by `Dispatcher.expire_leases` |
| **precondition** | `status='claimed'` and `lease_expires_at <= now` |
| **guard** | per row, `UPDATE … WHERE task_id=? AND status='claimed'`; `rowcount!=1` → skipped silently |
| **before → after** | `claimed` → `open`, `claimant=NULL`, `claimed_at=NULL`, `lease_expires_at=NULL` |
| **outcomes** | applied per swept row · superseded per row that changed under the sweep (no fact) |
| **returns** | `list[Transition[JobRecord]]`, one per lease swept — a sweep is a batch of transitions and says so; `Dispatcher.expire_leases` reads the rows off. Building a transition and dropping it would make `outcome.applied` a fire-and-forget emit conduit |
| **fact** | `task_failed` with `reason='lease_expired'`, `agent_id = job.claimant or steward:api`, `project` resolved from the fleet or `steward`, `parent_task_id` when set, and **no `run_id`** — this is the board mourning a claim, not a session reporting back, and naming a session would answer a registry row this sweep knows nothing about (#39) |
| **row shape emitted from** | the row *as it was when the lease died* (claimant still named), not the reopened row |
| **callers** | `Dispatcher.dispatch`, including `sweep_only` (scheduler tick, watchdog pass) |

#### T7 — delegate (deliver a letter)

| | |
|---|---|
| **owner** | `DelegationTransitions.deliver`, called by `Delegator.delegate` past every check |
| **preconditions, in order** | handoff parsed · sender not retired · receiver exists, not retired, not self · sender's manifest permits (`delegation.send`, `to` list) · receiver's route exists, is kind `delegation`, is `active` · parent resolved from what the sender actually holds (#67) · depth ≤ `max_depth` · no resident revisited |
| **guard** | unconditional `INSERT` — every check is a refusal *before* the write |
| **before → after** | (no row) → `status=open`, `assignee`, `delegated_by`, `route`, `parent_task_id`, `origin`, `depth` |
| **outcomes** | applied · refused (any check raises `DelegationError`; **nothing written, nothing emitted**, and a harvested refusal knocks instead — a separate approval transition) |
| **fact** | `task_delegated`, `agent_id = sender.agent_id` (or `steward:api` for a human), `project = sender.project` (or `steward`), payload `{task_id, title, from, to, route, parent_task_id, depth}` — note `parent_task_id` is **always present here**, `null` included, unlike the task facts |
| **callers** | `Delegator.harvest` (session output), `POST /delegate`, `steward delegate` |

### 1.2 Approval transitions

#### A1 — raise

| | |
|---|---|
| **owner** | `ApprovalTransitions.raise_request` (a session's ask) and `ApprovalTransitions.knock` (steward's own news about a resident) — two named acts over one private body, because the difference between them is not a knob |
| **precondition** | none — an escalation steward could not even parse is still raised, under `unreadable_escalation`, carrying the raw block and the complaint |
| **guard** | unconditional `INSERT`; the repeat-deny lookup decides *how* the row is filed |
| **before → after** | (no row) → `status=pending` … **or** `status=resolved, decision=deny, decided_by='repeat'` when the same resident was denied the same action inside the window |
| **outcomes** | `raise_request`: applied (pending row + knock) · answered (repeat auto-deny: row written, **no fact**, nobody's phone buzzes). `knock`: applied only — with the guard off there is no auto-deny branch to take |
| **fact** | `needs_human`, `agent_id = manifest.agent_id or steward:<id>`, `project = manifest.project or <id>`, payload `{message, request_id, action, detail, options, expires_at}` |
| **redaction** | `message` and every `detail` value are scrubbed of inline secrets **before** they are length-capped, so a secret cut in half can never surface a live prefix (#65); capping is `500` chars for `message` (`ERROR_MAX_CHARS`, via `truncate_error`) and `2 000` chars per `detail` string with `8 000` for the serialized map (`DETAIL_FIELD_MAX_CHARS` / `DETAIL_MAX_CHARS`) — a steward-authored knock composed against the wrong number is a question cut off mid-sentence at the panel |
| **repeat guard** | a property of *which act was called*, not a flag: `raise_request` is a session's own ask and the guard is always on; `knock` is steward's own news about a resident (budget pause, watchdog give-up, delegation refusals) and the guard is always off. Never applied to `unreadable_escalation` either way |
| **callers of `raise_request`** | `ApprovalTransitions.harvest` (session output), `steward approval raise` |
| **callers of `knock`** | `BudgetTransitions.pause`, `Delegator._knock`, `Watchdog._give_up` |

#### A2 — decide

| | |
|---|---|
| **owner** | `ApprovalTransitions.decide`, called by `POST /approvals/{id}` and by the budget resume |
| **guard** | `UPDATE … WHERE request_id=? AND status='pending' AND (expires_at IS NULL OR expires_at > now)` |
| **outcomes** | applied (`recorded=True`) · refused (`record is None` — no such request) · **expired** (`recorded=False` and the row is still `pending`: past the deadline, `409 approval_expired`, deny-by-default keeps the last word) · **replayed** (`recorded=False` and the row is resolved: the first decision won; `200`, read back, nothing emitted) |
| **before → after** | `pending` → `resolved`, `decision`, `decided_by`, `decided_at`, `edit` |
| **fact** | `needs_human_resolved`, `agent_id = record.agent_id`, `project = record.project`, payload `{request_id, decision, decided_by, action}` — emitted under the *resident's* identity, because the villager walking away from your door is the one who knocked |
| **ordering** | store → emit → request-log `accept` → budget resume (the resume is a workflow concern, not part of this transition) |

#### A3 — expire (deny by default)

| | |
|---|---|
| **owner** | `ApprovalTransitions.expire`, called by `Dispatcher.dispatch` |
| **guard** | per row, `UPDATE … WHERE request_id=? AND status='pending'`; `rowcount!=1` → skipped (a human answered in the same instant, and their answer wins) |
| **outcomes** | applied per row · superseded per row a human won (no fact) |
| **returns** | `list[Transition[ApprovalRecord]]`, one per request denied, for the same reason `expire_leases` does |
| **fact** | `needs_human_resolved`, `decision='deny'`, `decided_by = record.decided_by or 'expiry'` |
| **callers** | one: `Dispatcher.dispatch`, which the scheduler tick, `steward board dispatch`, and every watchdog pass all reach |

### 1.3 Budget transitions

#### B1 — pause

| | |
|---|---|
| **owner** | `BudgetTransitions.pause`, called by `BudgetGuard._pause` |
| **precondition** | a gauge is over its cap, and no standing allowance covers this moment |
| **guard** | conditional insert; `created=False` means somebody tripped the same budget in the same instant and already knocked |
| **before → after** | (no pause) → a pause row naming budget, spent, cap, reason, `request_id`, `window_end` |
| **outcomes** | applied (pause row + one knock) · superseded (`created=False`: the existing row is read back, **nothing emitted**) |
| **`window_end`** | required at this seam, not defaulted. It is what B2 scopes the "carry on" allowance to, and a pause written without one is lifted into an allowance of nothing — so the next fire re-trips the still-exhausted cap and knocks again, every fire. The store column still defaults to empty for rows written before it existed; the *act* does not, because no caller genuinely does not know which window it just ran out of |
| **fact** | none of its own; the pause's visible fact is the `needs_human` that A1's `knock` emits for `budget_unpause`, with `expires_at=None` — deny-by-default protects nothing here, because the safe state *is* the current one |
| **callers** | `BudgetGuard.allow` (before a run) and `BudgetGuard._pause_if_over` (after one, #68) |

#### B2 — resume

| | |
|---|---|
| **owner** | `BudgetTransitions.resume`, called by `BudgetGuard.resume` |
| **guard** | `unpause_resident` returns `None` when nothing was paused |
| **before → after** | pause row cleared; when the pause named a `window_end`, an allowance is granted until it, so the next fire does not re-trip the same cap and knock into a loop |
| **outcomes** | applied · refused (nothing was paused: nothing written, nothing emitted) |
| **fact** | `needs_human_resolved` with `decision='approve'` — **only** on the CLI path (`decide=True`), and only when that decision was this call's to record. The API path passes `decide=False` because `POST /approvals/{id}` already recorded the decision and emitted the fact; recording it twice would put two answers in the log for one question |
| **already answered** | a person may have denied the unpause from a panel and somebody may then lift the same pause from a terminal. The pause still lifts — a terminal is a person too — but the inner A2 comes back **replayed**, the approval log keeps the deny, and nothing new is emitted. That is logged at `WARNING`, and the inner transition is kept on `Transition.via` so a caller can render it rather than seeing only "applied, silently" |
| **callers** | `steward budget unpause` (`decide=True`), `POST /approvals/{id}` via `_resume_if_budget` (`decide=False`) |

### 1.4 What is deliberately *not* in the matrix

Routine bracketing (`routine_started` / `routine_finished` / `routine_failed`) and
`resident_restarted` are transitions of a *run*, not of a durable domain row: the run
registry row and the event are already paired inside the scheduler and the watchdog, and
#123 names them out of scope. They are listed here so the omission is a decision rather
than an oversight. The watchdog's give-up *knock* is in scope, because it is an A1.

---

## 2. Two designs

Both designs keep the store and the emitter as separate implementations, both keep the
event factories, and neither introduces a bus, an outbox, a store callback, a new schema,
or a retry policy. What they disagree about is **who owns a transition**.

### Design A — one deep module per domain

A `steward.transitions` package with four modules, each a small class bound to a store and
an emitter:

```
steward/transitions/__init__.py    # the vocabulary, re-exported
steward/transitions/outcome.py     # Transition[T] and the six outcomes
steward/transitions/task.py        # TaskTransitions: post claim take_delivery finish expire_leases
steward/transitions/approval.py    # ApprovalTransitions: raise_request knock decide expire harvest
steward/transitions/delegation.py  # DelegationTransitions: deliver
steward/transitions/budget.py      # BudgetTransitions: pause resume
```

```python
outcome = tasks.finish(job, claimant=..., project=..., result=result, run_id=run_id, now=moment)
if outcome.superseded:
    ...                     # the lease died; nothing was written, emitted, or read back
report = BoardReport(task=job, ...)          # the pre-close row is the honest one to report
```

Each method takes the domain act's own vocabulary, performs the conditional write,
interprets `rowcount` itself, selects identity and lineage, builds the matching fact, and
hands it to the emitter — but only on the branch where the write won. It returns a
`Transition[RecordT]`: the durable record, which of the six outcomes happened, and the
fact that was handed over (so a test can assert row and event together without reaching
into the emitter).

**Depth.** Four narrow interfaces over a lot of interpretation. `TaskTransitions.finish`
takes a job, a claimant and a `RunResult`, and hides the status derivation, the reason
string, the lease token, the `rowcount` interpretation, the done-vs-failed choice, the
`run_id` and lineage keys, and the truncation.

**Locality.** Task language stays with tasks and budget language with budgets. Nothing has
to name a "command" or a "kind" to say what it is doing.

**Leverage.** The raise transition already has five callers; each one drops the
knowledge that a repeat auto-deny must not emit. The board drops two `rowcount`
interpretations. The API drops the four-way decide branch's coupling to the event.

**Cost.** Four modules and a shared result type; a reader chasing "what happens when a
resident is paused" opens two files (`budgets.py` for the gauge policy,
`transitions/budget.py` for the write and the knock).

### Design B — one lifecycle coordinator

A single `steward/transitions.py` holding one `FleetTransitions` class with the same
domain-named methods (`post_task`, `claim_task`, `finish_task`, `expire_leases`,
`raise_approval`, `decide_approval`, `expire_approvals`, `deliver_delegation`,
`pause_budget`, `resume_budget`), constructed once from a store and an emitter and threaded
into the board, the API, the CLI, and the guards.

**Depth.** One interface, wider: ten-plus methods on one object, and every caller that
wants one of them can reach all of them.

**Locality.** Worse in one specific way that matters here. The four domains have genuinely
different vocabularies — a lease token, a repeat window, a route and a depth, a spend
window — and one class holding all four either grows a shared parameter surface that means
different things per method, or drifts toward the generic command shape #123 explicitly
rejects. The gravity is real: the second time somebody adds a method, "these all take a
record and emit a fact" starts looking like an abstraction.

**Leverage.** Identical to A for callers — the same knowledge moves out of the same places.

**Cost.** One import instead of four, and one object to construct. Genuinely simpler
wiring, especially in `api.py`, which needs three of the four.

### Rejected without being written

**A generic command/transition bus** — `apply(TaskClaimed(...))` or
`transition(kind, payload)`. It collapses four unrelated vocabularies into one, it makes
every call site name its act in a type rather than a verb, and it puts a dispatch table
between a caller and the thing it is doing. #123 rules it out and it deserves to be.

**A store that emits.** Passing the emitter into `Store` and emitting from inside the
`with self._lock, self._conn` block would look atomic and would not be: the POST happens
under a database lock, a slow village becomes a slow board, and the failure mode of an
unreachable burrow changes from "logged locally" to "held the write open for two seconds".
It also puts protocol knowledge in the one module that has none.

### The judgement

| | A: per-domain modules | B: one coordinator |
|---|---|---|
| depth | four narrow, deeply informed interfaces | one wide interface |
| locality | domain language stays in its domain | four vocabularies in one class |
| leverage over callers | equal | equal |
| idempotent result modeling | equal (shared `Transition[T]`) | equal |
| store/event details internal | equal | equal |
| resists collapsing into a command bus | yes — nothing to collapse into | weakly; the gravity is toward it |
| wiring cost | four constructions | one |

A wins on the axis this refactor exists to protect. B's only real advantage is wiring, and
wiring is paid once at construction rather than at every call site.

---

## 3. What was approved

**Design A**, with these ownership boundaries.

**Inside the transition modules:**

- the conditional write and the interpretation of its result;
- which fact, under which identity and project, carrying which lineage;
- that the fact is handed to the emitter on the winning branch and on no other;
- redaction and bounding, applied before any detail reaches protocol delivery (kept where
  it already is, inside the event factories, which the transitions now solely call);
- the six-outcome result each act reports.

**Outside them, unchanged:**

- HTTP and CLI translation — status codes, exit codes, request logging (`#122`);
- resident session execution (`#118`) and job selection before a transition;
- the approval block grammar and the delegate block grammar, and every delegation
  guardrail (permission, route, depth, cycle) that runs *before* the write;
- `Store`'s SQL and record mapping; `EventEmitter`'s transport, breaker, and fallback;
- scheduler routine bracketing and the watchdog's restart events.

**Implemented** in `src/steward/transitions/`, with `outcome.py` holding the six-outcome
result and the single function in the package that reaches an emitter — every other
constructor is not given one, so a refusal, a replay, an expiry, a lost race and a
self-answered request cannot emit rather than merely not doing so. The seam's own contract
is tested in `tests/test_transitions.py`; the approval, board, delegation and budget suites
now drive their durable behaviour through it.

**One observable surface did move, and it is not a payload.** Log lines that moved into
the package log under the module that now holds them, so an operator grepping journald by
logger name has to follow them:

| line | before | after |
|---|---|---|
| lease expired, back on the board | `steward.board` | `steward.transitions.task` |
| budget pause warning, pause-lifted info | `steward.budgets` | `steward.transitions.budget` |
| approval raised unreadable, repeat auto-denied, expired by default | `steward.approvals` | `steward.transitions.approval` |

Nothing in the repo keys on these names — no handler, no test, no alert — and the message
text is unchanged, so this is a grep habit to update rather than a breakage. The one line
that did **not** move is the superseded-close warning: it names the *resident*, the seam
knows only the claimant's burrow agent id, so it stays in `Dispatcher._record` on
`steward.board` exactly as before.

**The honest limit, restated where the code will read it:** a transition is not atomic
across the store and the village. It orders them, and it refuses to emit a fact for a
write that did not happen. It cannot promise the reverse — a process killed between the
`COMMIT` and the POST leaves a row whose fact reached only the local fallback log, which is
exactly the case the watchdog reads that log to find.
