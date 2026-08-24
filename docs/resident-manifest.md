# Resident manifests — v1

Resident manifests are checked-in `villagers/*.resident.json` declarations. A valid manifest promotes one matching event identity from Visitor to Resident during projection; the event log is never rewritten. Invalid manifests are omitted from residency and reported by `GET /residents` as diagnostics with `file`, JSON `path`, and `message`. `GET /villagers` remains the v0-compatible array of valid resident records plus any legacy display-only soul files.

The normative machine-readable shape is [resident-manifest.schema.json](resident-manifest.schema.json). Burrow's dependency-free runtime validator applies the same shape plus cross-file uniqueness for `home` and `match`.

```json
{
  "manifest_version": 1,
  "match": { "agent_id": "claude-code:life-agent" },
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

`match` contains exactly one `agent_id` or `project`. Exact `agent_id` declarations are reserved before project fallbacks, and each declaration can match at most one active villager. `home` is an explicit plot number from 0 through 7. Duplicate homes are rejected instead of being dynamically reassigned, so active fleet order, reloads, and absences cannot change where a Resident lives. Visitors never receive a plot; at-home activity and rest happen at the shared visitor lodge.

## Capability truth and privacy

All five capability dimensions are mandatory: `soul`, `skills`, `memory`, `routes`, and `app_grants`. Empty route or grant arrays truthfully mean none are declared. Skills, routes, and grants contain only an identifier and a `status_ref`; memory contains only a durable `ref` and `status_ref`. These references report where status is established—they do not claim a connection works.

Never put tokens, passwords, API keys, private keys, OAuth material, cookies, or other credentials in a manifest. Unknown fields are rejected, and credential/secret-shaped keys are rejected at any depth without echoing their values in diagnostics. Credentials remain in the owning app or runtime secret store.

Identifiers and status references use a deliberately narrow, whitespace-free character set. The runtime also rejects bearer values, assignment-shaped material, and opaque high-entropy strings anywhere in a manifest, including free-text soul fields, without echoing the rejected value in diagnostics.

The unauthenticated `GET /residents` and `GET /villagers` responses are an explicit public projection, not a serialized manifest. They expose only the manifest filename, validation/version marker, match, home, display metadata/body, and these allow-listed capability fields: all five soul display fields; `id` and `status_ref` for each skill, route, and app grant; and `ref` plus `status_ref` for memory. The server copies those fields into new records after validation, so an unrecognized manifest field can never become public merely because validation changes. Recognized credential formats, including AWS access-key IDs, are rejected without echoing their values.
