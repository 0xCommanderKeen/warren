# Projection test triage

Issue #96 removed the browser's duplicate event reducer. This records where the
assertions from each projection-coupled Node suite went.

| Removed suite | Disposition |
| --- | --- |
| `tests/protocol.test.js` | Protocol validation is covered by `tests/test_protocol.py`; invalid-event exclusion, failed-tool state, and snapshot diagnostics are covered by `tests/test_village_state.py`. Capsule parsing and Mood derivation assertions exercised the deleted browser reducers only. Python rotation remains covered by `VillageProjectionTests.test_rotation_is_observationally_invisible`. |
| `tests/test_heartbeat.js` | Moved to `VillageProjectionTests.test_heartbeat_refreshes_liveness_without_replacing_visible_activity`; clock-only stale/absent transitions remain in `test_clock_transitions_do_not_require_an_event`. |
| `tests/residents.test.js` | Exact/project matching, child exclusion, and stable homes are covered by `tests/test_village_state.py`; manifest validation and precedence are covered by `tests/test_residents.py` and `test_serve.py`. Legacy browser-soul styling assertions die with the browser reducer. |
| `tests/approval-knocks.test.js` | Authoritative snapshot projection is covered by `test_projects_task_approval_journal_routine_mood_and_diagnostics`; shape, immutable identity, collision, and delivery behavior are covered by `tests/test_protocol.py`, `test_serve.py`, and `tests/test_events.py`. Browser acknowledgement and panel-fold assertions belong to the retired client implementation. |
| `tests/job-board.test.js` | Authoritative task state is covered by `test_projects_task_approval_journal_routine_mood_and_diagnostics`; protocol validation and retention ordering are owned by `protocol.py` and the `retention*.py` modules. Browser posting, acknowledgement, and presentation assertions belong to the retired client implementation. |
| `tests/routine-ledger.test.js` | Authoritative routine state is covered by `test_projects_task_approval_journal_routine_mood_and_diagnostics`; routine validation is covered by `tests/test_protocol.py`, and manifest declarations by `tests/test_residents.py`. Browser Run Now, acknowledgement, and ledger presentation assertions belong to the retired client implementation. |
| `tests/journal-observations.test.js` | Validation, first-append authority, collision, capacity, ordering, and HTTP rejection are covered by `tests/test_journal_observations.py`; snapshot projection is covered by `tests/test_village_state.py`. Browser cache reconciliation and animation assertions belong to the retired client implementation. |
| `tests/destinations.test.js` | Python-owned tool-to-place mapping moved to `VillageProjectionTests.test_tool_places_are_owned_by_the_python_projection`. Plot selection, slot holding, and map destinations belong to the in-tree viewer and die with it. |
| `tests/test_places.js` | Python place production moved to `VillageProjectionTests.test_tool_places_are_owned_by_the_python_projection`. HTML map geometry and fallback destinations are viewer-only. |
| `tests/nursery.test.js` | This suite covered only the viewer's resident-creation client and its acknowledgement state machine; it has no server projection semantics. Steward event validation remains covered by `tests/test_protocol.py` and resident manifests by `tests/test_residents.py`. |

The adapter integration tests in `tests/test_codex_adapter.py` and
`tests/test_claude_subagents.py` now call `village_state.project_village()`
directly, so emitted hooks are tested against the authoritative projection
without a Node subprocess.
