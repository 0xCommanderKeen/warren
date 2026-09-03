# Migrating existing Resident identity

New declarations use `agent_id: resident:<uid>`. Existing `steward:*`, `claude-code:*`,
and `codex:*` keys must be changed deliberately: the old and new keys are different
Chronicle villagers, even when their slug or display name matches.

Before changing a manifest, choose one consequence and record it in the operator change:

1. **Archive and reset.** Stop the Resident and Chronicle, copy the live `events.jsonl`,
   its `archive/` directory, and adjacent delivery-index/generation files to a dated backup.
   Start Chronicle with a fresh empty data directory, change the manifest and matching soul
   frontmatter to `resident:<the existing uid>`, deploy both services, then restart the
   Resident. The visible village begins with the first new-key event; old history remains
   only in the dated archive.
2. **Explicit history rewrite.** With Chronicle stopped, copy every retained live and
   archived JSONL file, replace only the specifically approved old `agent_id` with
   `resident:<uid>`, rebuild derived indexes by removing the copied delivery-index files,
   and start Chronicle against the migrated copy. Preserve `source` unchanged. Verify the
   old key belongs to exactly that one Resident before rewriting it.

Never infer a mapping from `id` or `soul.name`: slugs can be reused and display names can be
shared. Never merge all events with a matching name. Keep the original archive until the
new projection and the Resident's first event have been verified.
