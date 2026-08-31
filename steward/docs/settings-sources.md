# What a settings file can do to a session

**Measured 2026-08-31** against `claude` **2.1.252** (the CLI on the operator's laptop)
and **2.1.243** (the version `CLAUDE_VERSION` in `steward/Makefile` pins into the resident
image). Headless, `claude -p --output-format json`, under `env -i` with only `HOME`,
`CLAUDE_CONFIG_DIR`, `PATH` and `TERM` set. This is the measurement steward #204 could not
take and steward #206 asked for, and it is why `ClaudeRunner.argv` now writes
`--setting-sources ""` on every session.

`docs/manifest.md` states the conclusion; this file is the working. Where the two
disagree, this file is the record of what was actually run.

## Why it needed measuring

`#204` bounded which *tools* a session has. It left the settings channel open, and the
only thing standing in that channel was a flag in the operator's home directory: the CLI
refuses to apply a `.claude/settings.json` from a workspace that was never trusted, and
says so on stderr. A resident's working directory **is** its memory directory — the one
place its charter lets it write — so "can a resident write itself a settings file its next
session honours" was a question about somebody else's host state rather than about
anything steward declares.

`--setting-sources` was known to *parse* (`""` and `project` both accepted, exit 0) and
had never been observed to *do* anything, because the trust gate masked every difference.
Shipping it on that basis would have been the `--allowed-tools` mistake again.

## How it was set up

A throwaway `CLAUDE_CONFIG_DIR` under `/tmp` holding its own `.claude.json`, whose
`projects["<scratch ws>"].hasTrustDialogAccepted` was flipped between `true` and `false`
between sweeps. Nothing in the operator's own `~/.claude` was edited, and no workspace of
theirs was trusted to find this out.

The three sources, as the CLI resolves them:

| source | file |
|---|---|
| `user` | `$CLAUDE_CONFIG_DIR/settings.json` (`~/.claude/settings.json` by default) |
| `project` | `<cwd>/.claude/settings.json` |
| `local` | `<cwd>/.claude/settings.local.json` |

Each probe planted **one** settings file at **one** source and watched for an effect the
session could not fake:

- **hooks** — a `SessionStart` hook whose command is `touch <sentinel>`. The sentinel
  either exists afterwards or it does not.
- **permission mode** — `permissions.defaultMode`, read off `permission_denials` in the
  result JSON after the session is made to attempt `mv a b`.
- **model** — the `model` field of the request that arrives at the endpoint.
- **MCP** — a stdio server whose command is `touch <sentinel>`; the sentinel says the
  process was spawned.
- **skills and `CLAUDE.md`** — whether a marker skill's name and a marker line from
  `CLAUDE.md` appear in the request body, and whether the `Skill` tool can invoke it.

A session's own account of its settings is worthless (#204 measured a zero-tool session
claiming six tools), so nothing here is read off what a session *said*.

Two runs of the sweep, for two different reasons:

1. **Against the real API**, on the operator's laptop, for the hook sentinels. A
   `SessionStart` hook fires before the first API call, so these rows are answered even
   by a session that then dies unauthenticated — the whole hook matrix cost nothing.
2. **Against a stub Messages endpoint** (`ANTHROPIC_BASE_URL` pointed at a local HTTP
   server that answers with a canned `tool_use` and logs each request's `model`), for
   everything that needs the session to get past the first turn: MCP init, an attempted
   tool call, the model on the wire. No spend, and no real credentials in a scratch
   config dir.

## What each source can change

Rows are what happened, not what was configured. `hook fired` means the sentinel existed.

| what the settings file carried | user | project | local | gated by workspace trust? |
|---|---|---|---|---|
| `hooks.SessionStart` → `touch` | fired | fired | fired | **no** |
| `permissions.defaultMode: bypassPermissions` | applied | applied | applied | **no** |
| `model: haiku` (no `--model` on argv) | applied | applied | not run | no gate seen |
| `permissions.allow: ["Bash(mv:*)"]` | no gate | **gated** | no gate | **yes — and only this** |
| `mcpServers: {…}` | no spawn | no spawn | no spawn | n/a — not a settings key |

"gated" is the CLI printing its ignore message; "no gate" is it printing nothing. Only
the `project` file was ever gated: an untrusted `settings.local.json` and the user file
drew no complaint. Whether their rules then *matched* was not measurable here — see
"What did not survive verification".

The load-bearing line is the last column. The trust gate covers `permissions.allow`
entries and **nothing else**; its own message says as much, naming exactly what it drops:

```
Ignoring 3 permissions.allow entries from .claude/settings.json: this workspace has not
been trusted. Run Claude Code interactively here once and accept the trust dialog, or set
projects["<path>"].hasTrustDialogAccepted: true in <config>/.claude.json.
```

A hook in that same file, in that same untrusted workspace, ran anyway. So did
`defaultMode: bypassPermissions`, which is the whole permission system switched off:
`mv a b` recorded `permission_denials: []` and the tool reported success, against
`"This command requires approval"` and one denial with no settings file at all.

That is the answer to the question #206 asked. **The mitigation `#204` recorded was never
covering the dangerous half.** A resident that wrote
`{"hooks": {"SessionStart": [...]}}` into its own memory directory would have had
arbitrary code at its next fire, trusted workspace or not, and `--tools` would not have
touched it — a hook is not a tool.

Two smaller findings from the same sweeps:

- **Trust is not inherited.** A subdirectory of a trusted workspace is untrusted: running
  in `<trusted>/sub` with a settings file in `<trusted>/sub/.claude/` produced the ignore
  message. So trust would have had to be granted to each resident's memory directory by
  name — the gate was never going to be quietly satisfied.
- **`mcpServers` in `settings.json` is not a thing.** No server was spawned from any of
  the three sources. A `.mcp.json` in the working directory **is** — that server spawned
  in both a trusted and an untrusted workspace.

## Whether the flag is honoured

Every combination, both trust states, both CLI versions. A sweep row reads: settings file
at one source, this flag on argv, did the hook fire?

| flag | user file | project file | local file |
|---|---|---|---|
| *(none)* | fired | fired | fired |
| `--setting-sources ""` | — | — | — |
| `--setting-sources user` | fired | — | — |
| `--setting-sources project` | — | fired | — |
| `--setting-sources local` | — | — | fired |
| `--setting-sources user,project,local` | fired | fired | fired |

Exactly what it says on the tin: a subset is honoured as a subset, and the empty string is
honoured as *none*. Identical on 2.1.243 and 2.1.252, and identical in a trusted and an
untrusted workspace — which matters, because it means the flag does not merely re-do what
the trust gate was doing.

An unknown name is refused loudly rather than ignored:

```
Error processing --setting-sources: Invalid setting source: bogus. Valid options are:
user, project, local
```

That is the opposite of `--tools`, where an unknown *tool* name is silently dropped
(#204), and it is why the flag can be trusted to fail visibly if the spelling ever moves.

`--setting-sources ""` also switched off, in the same runs:

- `permissions.defaultMode` from every source (`mv` denied again);
- the `model` key (the wire showed the default, not the file's model);
- the `.mcp.json` server in the working directory — which is worth knowing, because
  `--strict-mcp-config` only reaches a session whose manifest declares a `tools` bound,
  and every live resident today is `unrestricted`.

## What it costs

`--setting-sources ""` is *load nothing from the filesystem*, not *load no permissions*.
Two things a session used to pick up out of its working directory stop arriving:

- **`CLAUDE.md`** in the working directory is no longer read. Steward never writes one,
  and a resident writing its own would be the instruction-shaped version of the hole this
  flag closes, so this is a feature. The journal is the supported channel for a resident
  leaving itself something (`docs/manifest.md`, "The journal").
- **`.claude/skills` is no longer discovered.** Steward materializes the effective skill
  set there before every claude run, and the CLI no longer sees it: the `Skill` tool
  answers `Unknown skill: <name>` and the skill's name is absent from the request body.

The second one is a real loss and is stated plainly rather than papered over. It is
survivable because the prompt, not the directory, was always the delivery path: steward
injects each granted skill's name, description and **body** into every session
(`steward.prompt.render_skills`). A resident still holds its skills; what it no longer has
is the CLI's own progressive-disclosure route to them. `steward skills` says so out loud —
"prompt — plus a copy in .claude/skills/ the session's CLI does not discover" — rather
than printing two channels where one works.

Restoring discovery without re-opening the settings channel is a separate design
(`--plugin-dir` is the obvious candidate and has not been measured), and a separate issue.

## What did not survive verification

- *"The CLI refuses to apply a `.claude/settings.json` from an untrusted workspace"* —
  the sentence `docs/manifest.md` carried out of #204. True only of `permissions.allow`.
  Hooks, permission modes and models from that same untrusted file all applied.
- *"Headless is permissive by default"* — recorded in `docs/manifest.md` from #204, where
  a no-flags session ran `echo` with `permission_denials: []`. In an isolated config dir
  with no settings at all, a headless `echo hi` was **denied** ("This command requires
  approval"). The permissiveness measured in #204 most likely came from the operator's own
  `~/.claude/settings.json`, which sets `permissions.defaultMode: auto` — that is, from
  exactly the inheritance this issue closes. Stated as the likeliest reading rather than a
  measured one: it was not re-run against the operator's real settings file, because doing
  so would have meant running sessions under their config.
- **Permission *rules* were not measured under the stub.** `--allowed-tools "Bash(mv:*)"`
  on argv also failed to approve `mv a b` there, which is a stub artifact: matching a
  `Bash(...)` rule needs a real model to extract the command prefix. The rule rows above
  therefore rest on the trust gate's own message plus #204's real-API result, and are
  marked as such. The `defaultMode` rows do not depend on a classifier and were measured
  directly.
- **A session's self-report** was not used anywhere, for anything.

## Reproducing it

Nothing here is checked in as a test: it measures a binary steward does not ship, on a
machine that is not CI, and a test that spends money at someone's API is worse than a
dated record. The shape, if it needs redoing after a CLI upgrade:

1. A scratch `CLAUDE_CONFIG_DIR` with a `.claude.json` that marks the scratch workspace
   `hasTrustDialogAccepted: true`.
2. A settings file at one source carrying a `SessionStart` hook that touches a sentinel.
3. `claude -p ok --output-format json` in that workspace, under `env -i`.
4. Look for the sentinel — not at what the session said.

`steward doctor` is the standing check that the installed CLI still knows the flag:
`required_flags` reports `--setting-sources` for every `claude` resident, declarations or
not, so a CLI too old to have it is red in daylight rather than a failed 07:00 routine.
