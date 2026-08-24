# The resident manifest (v0)

A resident is two files in this repo:

```
residents/<id>/
  manifest.yaml   # the versioned declaration — structure, validated
  soul.md         # the free-form soul body — markdown with frontmatter
```

Editing a resident is edit → commit → deploy. Nothing about a resident lives anywhere
else: steward injects the charter into every headless session, and burrow reads the same
file for display. One place, two readers.

**Manifests hold references and grants, never credentials.** A manifest with a
credential-shaped key (`token`, `secret`, `password`, `api_key`, …) or an inline secret
value fails validation and is never stored. Credentials live outside both repos, full stop.

## Top level

| field | required | meaning |
|---|---|---|
| `version` | yes (defaults to `0`) | Manifest schema version. Only `0` exists. |
| `id` | yes | Slug; must equal the directory name under `residents/`. |
| `agent_id` | one of these two | Exact burrow identity, `<source>:<name>` (e.g. `claude-code:life-agent`). |
| `project` | one of these two | Project label for a project-scoped soul (e.g. `burrow`). |
| `summary` | no | One line burrow can display. |
| `soul` | yes | Identity dimension — see below. |
| `charter` | yes | Purpose and obligations — see below. |
| `skills` | yes | Capability dimension (may be an empty list). |
| `memory` | yes | Memory dimension. |
| `routes` | yes | Route dimension (may be an empty list). |
| `app_grants` | yes | App access dimension (may be an empty list). |
| `runner` | no | Which brain the resident runs on. Defaults to `{kind: claude}`. |
| `routines` | no | Standing scheduled work. Defaults to `[]`. |

`agent_id` matches before `project`, mirroring burrow's resident matching: an exact
agent-id manifest is reserved first, a project-scoped soul catches the rest. A manifest
with neither cannot be matched to a villager and fails validation.

The five capability dimensions (`soul`, `skills`, `memory`, `routes`, `app_grants`) must
be present explicitly. An empty list is a valid declaration ("this resident has no
routes"); a missing key is not, because silence must never be read as a grant.

## `soul` — identity

Field names match burrow's `villagers/*.md` frontmatter, so no translation is needed.

```yaml
soul:
  name: Hob            # display name
  char: Monk           # burrow sprite key
  accent: "#a68a4f"    # hex, #rrggbb
  role: life bot       # one line
  file: soul.md        # optional; the soul body, relative to the manifest
```

## `charter` — what the resident is for

The charter is what steward injects into every headless session for this resident.

```yaml
charter:
  mission: One paragraph of purpose.
  duties:
    - Standing responsibilities, one per line.
  rules:
    - Hard constraints, e.g. "Never send email without explicit approval."
  escalation: When and how to raise needs_human instead of acting.
```

`escalation` is either a paragraph or a structured block:

```yaml
  escalation:
    when:
      - A message needs a reply that was not explicitly asked for.
    how: needs_human          # the protocol event used to escalate
    note: Optional guidance for the human.
```

## `skills` — capabilities

Each entry names a skill in the skills library (`skills/<id>/SKILL.md`, steward #12).
A bare string is shorthand for `{id: <string>}`.

```yaml
skills:
  - daily-summary
  - id: read-inbox
    source: library          # library (default) | local
    note: Why this resident holds it.
```

Today the id is validated as a slug; once the library lands, unknown names fail
validation with the closest match named.

## `memory` — durable knowledge location

A location, never its contents.

```yaml
memory:
  kind: directory            # directory | file | repo
  path: /data/residents/life-agent/memory
  journal: journal.md        # optional; read back at the start of the next session
```

## `routes` — declared inbound channels

```yaml
routes:
  - id: inbox
    kind: email              # email | chat | http | webhook | cron | cli | job-board
    address: mailbox:household   # a reference, never a credential
    status: active           # active | pending | disabled
    note: Optional.
```

## `app_grants` — declared app access

Identifier and status only. There is deliberately no field for a credential or a
credential reference.

```yaml
app_grants:
  - id: gmail
    name: Gmail
    status: granted          # granted | pending | revoked
    scopes: [gmail.readonly] # scope names, not values
    status_ref: https://myaccount.google.com/permissions   # where the grant is administered
```

## `runner` — which brain (steward #11)

Declared now, executed once the runner abstraction lands.

```yaml
runner:
  kind: claude               # claude | codex | command | mock
  model: claude-opus-5
  command: [my-agent, --prompt, "{prompt}", --cwd, "{workdir}"]   # kind: command only
  permission_mode: null
```

`command` is an argv list, not a shell string, and accepts only the `{prompt}` and
`{workdir}` placeholders — manifest content can never become shell.

## `routines` — standing work (steward #2)

Declared now, fired by the scheduler once it lands. Keep `enabled: false` until then:
the village must never show work that is not happening.

```yaml
routines:
  - id: daily-summary
    schedule: "0 7 * * *"    # five-field cron
    prompt: Write today's household summary.
    requires: [daily-summary, read-inbox]   # must be granted under skills
    timeout_s: 900           # the run is killed after this and emitted as routine_failed
    enabled: false
```

A routine that requires a skill the manifest does not grant fails validation, not
execution.

## The soul body

`soul.md` is markdown with YAML frontmatter, in the same shape as burrow's
`villagers/*.md`:

```markdown
---
agent_id: claude-code:life-agent
name: Hob
char: Monk
accent: "#a68a4f"
role: life bot
---
A short paragraph of who this villager is.

## Voice

How the resident sounds. Injected into every session, so it is capped at 1200
characters — a voice you cannot afford to send is not a voice.
```

The manifest is the source of truth. Any identity key present in the frontmatter
(`name`, `char`, `accent`, `role`, `agent_id`, `project`) must agree with the manifest,
or validation fails rather than letting two files disagree about who someone is.

## Validation

```
steward validate                 # the residents/ tree
steward validate residents/life-agent
steward validate residents/life-agent/manifest.yaml --format json
steward schema                   # JSON Schema for the manifest, for burrow and editors
```

Exit code is non-zero on any error, so CI can gate on it.

The same path is importable, and returns structured diagnostics rather than printed text:

```python
from steward import load_manifest, validate_tree

result = validate_tree("residents")
for diagnostic in result.diagnostics:
    print(diagnostic.file, diagnostic.field_path, diagnostic.problem, diagnostic.example)

resident = load_manifest("residents/life-agent/manifest.yaml")  # raises ManifestError
```

Every diagnostic carries four facts: the **file**, the **field path**, the **problem**,
and an **example** of a valid value.

```
residents/life-agent/manifest.yaml: error: charter.mission
    problem: required field is missing
    example: mission: Keep the household running day to day.
```

### What is rejected as a credential

- Any key whose name looks like a credential — `token`, `secret`, `password`,
  `passphrase`, `api_key`, `access_key`, `private_key`, `client_secret`, `credential(s)`,
  `bearer`, `authorization`, `cookie`, `*_token` — anywhere in the tree.
- Any value matching a known secret shape: `sk-…`, `ghp_…`/`github_pat_…`, `xox[bapr]-…`,
  `AKIA…`, `AIza…`, a JWT, a PEM private key, or a URL with an inline password.
- An opaque blob in a field that is supposed to hold a reference (`memory.path`,
  `routes[].address`, `app_grants[].status_ref`).
- Inline secrets in the soul body.

The credential scan runs **before** schema binding, so a secret is never loaded into a
model or echoed back in a diagnostic.

## Reading manifests from burrow

Burrow reads the same files for display (burrow #35). The contract:

- Identity fields in `soul` are exactly burrow's villager frontmatter fields.
- Match a villager to a resident by `agent_id` first, then by `project`.
- The five capability dimensions are the panel burrow renders; `app_grants[].status`
  is the only truth about whether access exists.
- `steward schema` emits the JSON Schema, so burrow can validate without depending on
  this package.
