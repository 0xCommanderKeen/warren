# Resident manifests — v1

Resident manifests are checked-in `villagers/*.resident.json` declarations. A valid manifest promotes one matching event identity from Visitor to Resident during projection; the event log is never rewritten. Invalid manifests are omitted from residency and reported by `GET /residents` as diagnostics with `file`, JSON `path`, and `message`. `GET /villagers` remains the v0-compatible array of valid resident records plus any legacy display-only soul files.

The normative machine-readable shape is [resident-manifest.schema.json](resident-manifest.schema.json). Chronicle's dependency-free runtime validator applies the same shape plus cross-file uniqueness for `home` and `match`.

```json
{
  "manifest_version": 1,
  "match": { "agent_id": "resident:e4af805e-cfa0-49e1-9782-93f7ae051102" },
  "home": 1,
  "soul": {
    "name": "Hob",
    "char": "Monk",
    "accent": "#a68a4f",
    "role": "life bot",
    "description": "The household spirit."
  },
  "skills": [{ "id": "daily-summaries", "status_ref": "life-agent:configured" }],
  "memory": { "ref": "life-agent:/data/memory", "status_ref": "mounted" },
  "routes": [{ "id": "telegram", "status_ref": "life-agent:configured" }],
  "app_grants": [{ "id": "gmail", "status_ref": "life-agent:configured" }]
}
```

## Matching and homes

`match` contains an `agent_id`, a `project`, or both. Steward Residents use the permanent
wire join key `resident:<uid>`; `project` may additionally describe their scope. Event
`source` continues to name the producer and is not identity. Exact `agent_id` declarations
are reserved before project fallbacks, and each
declaration can match at most one active villager. Visitors have no Steward UID, retain
their emitter-provided `agent_id`, and never receive a plot; at-home activity and rest
happen at the shared visitor lodge. `home` is an explicit plot number from 0 through 7.
Duplicate homes are rejected instead of being dynamically reassigned, so active fleet
order, reloads, and absences cannot change where a Resident lives.

## Capability truth and privacy

All five capability dimensions are mandatory: `soul`, `skills`, `memory`, `routes`, and `app_grants`. Empty route or grant arrays truthfully mean none are declared. Skills, routes, and grants contain only an identifier and a `status_ref`; memory contains only a durable `ref` and `status_ref`. These references report where status is established—they do not claim a connection works.

Never put tokens, passwords, API keys, private keys, OAuth material, cookies, or other credentials in a manifest. Unknown fields are rejected, and credential/secret-shaped keys are rejected at any depth without echoing their values in diagnostics. Credentials remain in the owning app or runtime secret store.

Identifiers and status references use a deliberately narrow, whitespace-free character set. The runtime also rejects bearer values, assignment-shaped material, and opaque high-entropy strings anywhere in a manifest, including free-text soul fields, without echoing the rejected value in diagnostics.

The unauthenticated `GET /residents` and `GET /villagers` responses are an explicit public projection, not a serialized manifest. They expose only the manifest filename, validation/version marker, match, home, display metadata/body, and these allow-listed capability fields: all five soul display fields; `id` and `status_ref` for each skill, route, and app grant; and `ref` plus `status_ref` for memory. The server copies those fields into new records after validation, so an unrecognized manifest field can never become public merely because validation changes. Recognized credential formats, including AWS access-key IDs, are rejected without echoing their values.

## Routine declarations

The optional `routines` array mirrors Steward's public schedule fields: `id`, five-field
`schedule`, `schedule_tz`, and `enabled`. `steward_resident` names the URL path segment
used by Steward's run-now endpoint. Prompts, skill grants, internals, and credentials are
not copied into Chronicle. Schedules are rendered as “declared, not observed”; observed
outcomes come only from `routine_*` events. The Steward token is entered into the client
and retained in memory only, then sent directly to Steward—never to Chronicle's server.
The manifest validator accepts a safe five-field declaration envelope, including
croniter names and operators such as `MON`, `JAN`, `L`, `?`, ranges, lists, and steps.
It deliberately does not reimplement croniter: authenticated `GET /routines` is the
authority for whether Steward loaded the schedule and for its computed next fire. This
keeps local declarations display-only and prevents a second, drifting scheduler parser.
The validator rejects macros, extra/missing fields, controls, and non-cron punctuation,
and requires an installed `ZoneInfo` name such as `UTC` or `Europe/Ljubljana`. The schema
also includes the installed single-segment aliases Steward accepts (`GMT`, `CET`,
`Zulu`, `Factory`, and their peers); a slash is not required for a real zone name.

Chronicle also reads Steward's authenticated `GET /routines` directly from the browser to
show its computed `next_fire`; it never computes that promise from the local cron text.
Configure Steward with the client's exact origin, for example
`STEWARD_CORS_ORIGINS=http://village.local:8080`. When it is unset or does not match,
the browser blocks both the schedule read and run-now request as cross-origin failures.
Do not work around that by exposing Steward publicly: its documented deployment remains
tailnet-only. The connection dialog holds the URL and masked token only in the current
page's JavaScript memory and clears the password field as soon as it closes.
