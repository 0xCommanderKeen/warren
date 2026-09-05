# Steward tests

API and CLI cases live in `test_api_<behavior>.py` and `test_cli_<behavior>.py`.
Start with the behavior being changed: auth, runs/scheduler, declarations,
approvals, provisioning, retirement, and the corresponding read surfaces.
`test_panel_api.py` covers the routine ledger and request-log contracts used by panels.
`test_chat.py` and `test_shared_chat.py` cover the single-resident and shared bot bridges.

Reusable setup belongs in `support/`, not in another collected test module.
Helpers used by only one behavior stay beside its cases. `conftest.py` owns the
suite-wide resident, skill, executable, and Git fixtures.

| Support | Setup and ownership |
| --- | --- |
| `support/api.py` | Function-scoped app factory. Initializes Git unless explicitly passed `git=False`. Does **not** activate lifespan; tests that need workers enter `with harness.client:` themselves. Teardown releases blocked runners, shuts down manual runs, and closes each store. |
| `support/panel.py` | Function-scoped panel factory. Always initializes Git. Does **not** activate lifespan. Teardown shuts down manual runs and closes each store. Its narrower factory remains separate from the API factory. |
| `support/cli.py` | CLI runner and shared command setup, including explicit temporary burrow/transport fixtures. |
| `support/chat.py` | Fake transports, scripted runner, chat manifest/message builders, and bridge fixtures. The store fixture owns its file-backed store context. |

Import fixtures explicitly into each module that uses them, including fixtures
needed by another imported fixture. The same-name aliases make pytest fixture
exports visible to linting; the local `PLC0414` annotation records why those
aliases are intentional. Fixture scope and cleanup remain with the original
fixture definitions.

For a mechanical move, capture full case IDs (including parameter IDs) before and
after with `uv run pytest --collect-only -o addopts='' -q`. Compare multisets after
removing only the file-path prefix before `::`, so missing or duplicated cases
cannot cancel out. Also compare function/class syntax trees to preserve test
bodies, decorators, assertions, and helper behavior. Run affected files with
`--no-cov` during the move, then `make check` for the complete suite and coverage.
