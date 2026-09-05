# Response contract migration

The first complete typed consumer path is `GET /routines` → run-now `POST` (202)
→ `GET /requests/{request_id}`, plus the request-list audit view (`GET /requests`).
Their success envelopes and nested routine/heartbeat/run fields are closed models.
Nullable routine fields are required keys, including `last_run`, `last_request`,
`journal`, `anchor`, `next_fire`, and the scheduler's `alive`/`last_tick`.
A null value records absence of evidence; a missing key is a contract error.

`RequestResponse.detail` is deliberately an open, action-specific object. Other
writers log different data here; typing its internals is separate migration work.
`outcome` and last-run `trigger` remain extensible strings. The run receipt fixes
`status: accepted` and `trigger: manual`; it does not claim a completed effect.

The selected routes also publish the simple `{detail: {error, message}}` refusal
shape: 401 on all four, 404 on request detail, and 403/404/409 on run-now. Refusals
have no receipt id. FastAPI's existing 422 validation schema is retained. Error
bodies on other routes are not claimed as typed by this migration.

## Remaining successful response endpoints

This finite list is checked against every exported success response by
`tests/test_openapi_contract.py`. Each future slice removes its entries, adds named
models, and validates real HTTP answers and the consumer fixtures against the
exported schema. An arbitrary dictionary is debt, not a validated contract.
The status is included because different successes can have different shapes.

- `GET /approvals 200`
- `GET /approvals/{request_id} 200`
- `GET /jobs 200`
- `GET /org 200`
- `GET /residents 200`
- `GET /residents/{resident_id} 200`
- `GET /residents/{resident_id}/budget 200`
- `GET /residents/{resident_id}/conversations 200`
- `GET /residents/{resident_id}/conversations/{conversation} 200`
- `GET /residents/{resident_id}/declaration 200`
- `GET /residents/{resident_id}/inbox 200`
- `GET /residents/{resident_id}/journal 200`
- `GET /secrets 200`
- `GET /skills 200`
- `GET /skills/{name} 200`
- `GET /tasks/{task_id}/lineage 200`
- `POST /approvals/{request_id} 200`
- `POST /delegate 202`
- `POST /jobs 202`
- `POST /reload 200`
- `POST /residents 201`
- `POST /residents/{resident_id}/provision 200`
- `POST /residents/{resident_id}/rehearse 200`
- `POST /residents/{resident_id}/retire 200`
- `POST /skills 201`
- `PUT /residents/{resident_id}/declaration 200`
- `PUT /secrets/{name} 200`
- `PUT /skills/{name} 200`
