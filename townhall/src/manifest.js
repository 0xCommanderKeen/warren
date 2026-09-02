/* Editing a manifest without losing the parts nobody put on a form.
 *
 * `PUT /residents/{id}/declaration` is a full replacement, not a patch — steward says so
 * plainly, because merging a partial edit would mean steward deciding whether a missing
 * key meant "cleared" or "untouched". So the form does not build a manifest; it edits the
 * one `GET` returned, in place and immutably, and sends the whole object back. A key no
 * field on the page knows about — `app_grants`, `deploy`, `routes`, a resident's routines —
 * round-trips byte-for-byte in the data, untouched and unguessed at.
 *
 * The one thing that does not survive this route is YAML comments, since steward
 * re-serialises the mapping. That is why the editor also offers the `text` spelling, which
 * is written byte for byte. Neither is more validated than the other.
 */

/** Read a dotted path. Missing anywhere along the way is `undefined`, never a throw. */
export function getIn(object, path) {
  return String(path)
    .split(".")
    .reduce((node, key) => (node === null || node === undefined ? undefined : node[key]), object);
}

/**
 * Write a dotted path, immutably. `undefined` deletes the key rather than storing a hole.
 *
 * Deleting matters: steward reads an absent budget as *unlimited* and an absent optional
 * field as unset, so a form that wrote `null` where a person cleared a box would be
 * declaring something rather than declaring nothing.
 */
export function setIn(object, path, value) {
  const [key, ...rest] = String(path).split(".");
  const base = object && typeof object === "object" && !Array.isArray(object) ? object : {};
  if (!rest.length) {
    if (value === undefined) {
      if (!(key in base)) return base;
      const { [key]: _dropped, ...kept } = base;
      return kept;
    }
    if (base[key] === value) return base;
    return { ...base, [key]: value };
  }
  const child = setIn(base[key], rest.join("."), value);
  if (child === base[key]) return base;
  // Pruning an emptied branch would delete a block the person did not ask to delete.
  return { ...base, [key]: child };
}

/** A textarea of one-per-line values becomes a list; blank lines are not list items. */
export function linesToList(text) {
  return String(text ?? "")
    .split("\n")
    .map((line) => line.trim())
    .filter(Boolean);
}

/** And back, for the textarea. A non-list is empty rather than "[object Object]". */
export function listToLines(value) {
  return Array.isArray(value) ? value.map((item) => String(item)).join("\n") : "";
}

/** What a text input should show for a scalar the manifest may not carry at all. */
export function scalarValue(value) {
  return value === null || value === undefined ? "" : String(value);
}

/**
 * A number a person typed, as steward's validator will see it.
 *
 * Returns `undefined` for a cleared box (delete the key: unlimited), `null` for something
 * that is not a number at all — which the caller keeps as-is so steward is the one that
 * refuses it, rather than this file silently dropping what somebody typed.
 */
export function numberValue(text, { integer = false } = {}) {
  const trimmed = String(text ?? "").trim();
  if (!trimmed) return undefined;
  const parsed = Number(trimmed);
  if (!Number.isFinite(parsed)) return null;
  return integer ? Math.trunc(parsed) : parsed;
}

/** The budget dimensions steward's `Budgets` model declares, in the order it declares them. */
export const BUDGET_FIELDS = [
  {
    path: "budgets.daily_cost_usd",
    label: "daily cost (USD)",
    integer: false,
    gauge: "daily_cost_usd",
    hint: "Money this resident may spend per local day. Cleared means unlimited — which steward reports out loud rather than assuming.",
  },
  {
    path: "budgets.daily_tokens",
    label: "daily tokens",
    integer: true,
    gauge: "daily_tokens",
    hint: "Input plus output tokens per local day, counted in the resident's own time zone.",
  },
  {
    path: "budgets.max_run_seconds",
    label: "max run seconds",
    integer: true,
    gauge: null,
    hint: "Not a daily cap: a ceiling on one run, enforced as min(routine timeout, this).",
  },
];

/** Has this draft actually diverged from what was loaded? */
export function changed(left, right) {
  return JSON.stringify(left) !== JSON.stringify(right);
}

/* -- skill grants -------------------------------------------------------------------- */

/**
 * The grants a manifest carries, as rows a picker can tick — with the original entry kept.
 *
 * A manifest may spell a grant two ways: `journal` and `{id: journal, note: "…"}` are the
 * same grant, and steward's own `SkillGrant` accepts both. A form that normalised one into
 * the other would rewrite `skills:` for every resident whose author preferred bare names,
 * the moment somebody opened the editor and saved something else. So the entry that was
 * read is carried on the row and handed straight back by {@link grantEntries} unless this
 * row's id or note actually changed.
 */
export function grantRows(manifest) {
  const entries = Array.isArray(manifest?.skills) ? manifest.skills : [];
  return entries.map((entry) => ({
    id: typeof entry === "string" ? entry : String(entry?.id ?? ""),
    note: typeof entry === "object" && entry !== null && typeof entry.note === "string" ? entry.note : "",
    entry,
  }));
}

/** One row, back in the spelling it came in — or the smallest one that carries its note. */
function grantEntry({ id, note, entry }) {
  const trimmed = String(note ?? "").trim();
  const bare = typeof entry === "string";
  const held = bare ? entry : entry?.id;
  const heldNote = !bare && typeof entry?.note === "string" ? entry.note : "";
  // Untouched: hand back the very object that was read, so an editor that only changed the
  // charter sends a `skills` block that is byte-for-byte what was on disk.
  if (entry !== undefined && entry !== null && id === held && trimmed === heldNote.trim()) {
    return entry;
  }
  if (bare || entry === undefined || entry === null) return trimmed ? { id, note: trimmed } : id;
  // Anything else the manifest put on this grant — `source`, a key added later — survives,
  // in its own position: only `note` is this form's to write.
  const { note: _replaced, ...rest } = entry;
  return trimmed ? { ...rest, id, note: trimmed } : { ...rest, id };
}

/**
 * The `skills` value for a manifest, from the picker's rows.
 *
 * `undefined` for no grants at all, never `[]`: steward reads an absent `skills` as "this
 * resident holds the library's defaults and nothing more", and writing an empty list where
 * the key was missing would put a line in the file that says the same thing louder.
 */
export function grantEntries(rows) {
  const entries = (rows || []).map(grantEntry);
  return entries.length ? entries : undefined;
}
