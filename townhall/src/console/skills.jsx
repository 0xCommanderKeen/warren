/* Granting and revoking a skill on a resident that already exists (warren#331).
 *
 * The nursery form has had a library-backed picker since it was ported: `ResidentNew` reads
 * `GET /skills` and ticks names into `POST /residents`. The *editor* never had one. So a
 * skill could be chosen at birth and never changed again — granting one to a live resident
 * meant switching the declaration editor to **yaml** and writing the `skills:` block by
 * hand, which is exactly the "edit the YAML" the write surface was built to end.
 *
 * Two rules this file exists to keep:
 *
 * **A grant goes back the way it came.** A manifest may spell a grant as a bare name or as
 * `{id, note}`, and steward accepts both. Normalising one into the other would rewrite
 * `skills:` for every resident whose author preferred bare names, the moment somebody
 * opened this page and saved a charter edit. `grantRows`/`grantEntries` carry the original
 * entry through untouched; this file only ever adds, removes and renotes rows.
 *
 * **A grant the library does not have is shown, not hidden.** It is the one grant that is
 * *wrong* — steward answers `skills[i].id` diagnostics about it — so a picker that listed
 * only library skills would draw a resident as holding nothing wrong while it refused to
 * save. Those rows come last, named, with the refusal on them and a way to untick.
 */

import { useSteward, useStewardQuery } from "../steward/context.jsx";
import { diagnosticsFor } from "../steward/client.js";
import { grantEntries, grantRows } from "../manifest.js";
import { Badge, Check, Empty, Input, Loading, Note, Panel, Problem } from "./ui.jsx";

/**
 * The rows to draw, in the order they are drawn: the library, then grants it does not have.
 *
 * `grant` is the index into the manifest's own `skills` array, which is what steward's
 * diagnostics are keyed on (`skills[2].id`) — so a refusal lands on the box it is about
 * rather than at the top of the panel. `null` means "not granted", not "index nought".
 *
 * `library` is `null` when the library could not be read at all, which is *not* the same
 * as an empty one: the grants are still real, and marking every one of them "not in the
 * library" because steward's `/skills` answered 503 would be this panel inventing a
 * refusal steward never made.
 */
export function pickerRows(library, rows) {
  // First index, not last: a manifest may grant the same skill twice — nothing in steward
  // refuses it — and the library row has to stand for one particular line of the file, or
  // unticking it would silently delete a different one and leave the box ticked.
  const first = new Map();
  rows.forEach((row, index) => {
    if (!first.has(row.id)) first.set(row.id, index);
  });
  const has = (name) => (library || []).some((skill) => skill.name === name);
  const known = (library || []).map((skill) => ({
    name: skill.name,
    description: skill.description,
    inherited: Boolean(skill.default),
    unknown: false,
    repeated: false,
    grant: first.has(skill.name) ? first.get(skill.name) : null,
  }));
  const claimed = new Set(known.map((row) => row.grant).filter((index) => index !== null));
  // Every grant a library row did not already stand for: one the library does not have, and
  // one the file lists a second time. Both get a row of their own, because a grant with
  // nowhere to be drawn is a grant nobody can untick and a diagnostic nobody can read.
  const rest = rows
    .map((row, index) => ({ row, index }))
    .filter(({ index }) => !claimed.has(index))
    .map(({ row, index }) => ({
      name: row.id,
      description: null,
      inherited: false,
      unknown: library !== null && !has(row.id),
      repeated: true,
      grant: index,
    }));
  return [...known, ...rest];
}

/** Every `skills…` diagnostic no row above will render, so none is lost off the bottom. */
export function orphanedDiagnostics(diagnostics, rows) {
  const claimed = new Set(rows.map((_row, index) => `skills[${index}].id`));
  return (diagnostics || []).filter(
    (item) => typeof item.field === "string" && item.field.startsWith("skills") && !claimed.has(item.field),
  );
}

/** What one row says under its name — the honest sentence for the state it is in. */
export function rowDescription(row, { held, blind }) {
  if (row.unknown) {
    return (
      "This manifest grants a skill the library does not have, so the resident does not " +
      "actually hold it. Untick to drop the grant, or add the skill to the library."
    );
  }
  if (row.repeated) {
    return (
      "This manifest grants the same skill more than once. The effective set is the same " +
      "either way; unticking removes this line and leaves the one above it."
    );
  }
  if (blind === "unreadable") {
    return (
      "Granted by this manifest. The library could not be read, so whether this grant " +
      "resolves to anything is not something this page can say."
    );
  }
  if (blind === "unconfigured") {
    return (
      "Granted by this manifest. Steward is pointed at no skills library at all, so no " +
      "grant is checked and no skill is injected — this line is valid and does nothing."
    );
  }
  if (row.inherited && held) {
    return (
      "Granted explicitly, and also a library default: the effective set is the same either " +
      "way, so this line adds nothing."
    );
  }
  if (row.inherited) return "A library default — every resident holds it without a grant.";
  return row.description;
}

/**
 * The picker.
 *
 * Reads the library itself rather than being handed one, because the panel is the only
 * thing that needs it and a page that fetched it would have to hold a fifth loading state
 * for a box nobody may open. `edit` is the declaration editor's own path writer, so what
 * this panel changes travels through exactly the code every other field travels through and
 * lands in the same `PUT …/declaration`.
 */
export function SkillsPanel({ manifest, edit, diagnostics = [] }) {
  const { client } = useSteward();
  const library = useStewardQuery((signal) => client.listSkills({ signal }), []);

  const rows = grantRows(manifest);
  const write = (next) => edit("skills", grantEntries(next));
  const grant = (name) => write([...rows, { id: name, note: "", entry: undefined }]);
  const revoke = (index) => write(rows.filter((_row, at) => at !== index));
  const renote = (index, note) =>
    write(rows.map((row, at) => (at === index ? { ...row, note } : row)));

  // Nothing is drawn until the library has answered one way or the other. Drawing the
  // grants first and re-sorting them under the library a moment later would flash every
  // one of them as "not in the library", which is the most alarming thing this panel can
  // say and would be false every time.
  const settled = Boolean(library.data) || Boolean(library.error);
  // Two ways there is no library to check a grant against, and neither is an empty one.
  // `library: null` is steward saying it is pointed at no `skills/` at all, in which case
  // *no grant is checked and no skill is injected* — `grant_diagnostics` complains about
  // nothing, and the save succeeds. Marking every grant "not in the library" there would be
  // this page inventing a refusal steward never made, over a fleet where the whole feature
  // is switched off.
  const blind = library.error ? "unreadable" : library.data?.library === null ? "unconfigured" : null;
  const drawn = settled ? pickerRows(blind ? null : library.data.skills, rows) : [];
  const orphans = orphanedDiagnostics(diagnostics, rows);

  return (
    <Panel title="Skills">
      <p className="mb-3 mt-0 text-[12px] leading-[1.7] text-dim">
        A grant is what this resident holds on top of the library's defaults. Ticking one here
        writes it into <code>skills:</code> and steward commits the change like any other edit
        — the container picks it up on its next session, with no redeploy.
      </p>
      {library.error ? (
        <Problem title="the skills library could not be read" error={library.error} />
      ) : null}
      {blind === "unconfigured" ? (
        <p className="mb-3 mt-0 text-[12px] leading-[1.7] text-wait">
          Steward is pointed at no skills library, so there is nothing to tick and nothing to
          check a grant against: no skill is injected into any session, and a grant here is
          valid and inert. That is how a tree from before the library existed keeps working.
        </p>
      ) : null}
      {settled ? null : <Loading>reading the library…</Loading>}

      {drawn.length ? (
        drawn.map((row) => {
          const problems = row.grant === null ? [] : diagnosticsFor(diagnostics, `skills[${row.grant}].id`);
          const held = row.grant !== null;
          return (
            // Keyed on the *line of the file* for a grant with no library row of its own,
            // because two of those can carry the same name.
            <div key={row.repeated ? `grant:${row.grant}` : `library:${row.name}`}>
              <Check
                name={row.name}
                // The label wraps the description too, so the box needs a name of its own
                // to be addressable — by a test, and by a screen reader reading a list of
                // twenty skills whose accessible names would otherwise be paragraphs.
                aria-label={row.name}
                note={
                  // At most one: `inherited` means the library has it and every resident
                  // holds it, `unknown` means the library does not have it, and `repeated`
                  // means the file lists it twice. A row that is both unknown and repeated
                  // is named for the worse of the two.
                  row.inherited ? (
                    <Badge tone="ember">default</Badge>
                  ) : row.unknown ? (
                    <Badge tone="fail">not in the library</Badge>
                  ) : row.repeated ? (
                    <Badge tone="wait">granted twice</Badge>
                  ) : null
                }
                description={rowDescription(row, { held, blind })}
                // Inherited *and* not granted is the one box with nothing behind it to
                // change: unticking a default is not a thing a manifest can say.
                disabled={row.inherited && !held}
                checked={row.inherited || held}
                onChange={(event) => (event.target.checked ? grant(row.name) : revoke(row.grant))}
              />
              {problems.length ? (
                <p className="mb-2 ml-[30px] mt-[-4px] text-[11px] text-fail">
                  {problems.map((item) => item.problem).join(" ")}
                </p>
              ) : null}
              {held ? (
                <div className="mb-3 ml-[30px]">
                  <Input
                    value={rows[row.grant].note}
                    placeholder="why this resident holds it — optional, and stored on the grant"
                    onChange={(event) => renote(row.grant, event.target.value)}
                    aria-label={`note for ${row.name}`}
                  />
                </div>
              ) : null}
            </div>
          );
        })
      ) : !settled ? null : (
        <Empty title="The library is empty.">
          Steward found no skills to grant, and this manifest grants none. A resident is
          perfectly valid holding nothing; it simply gets no skill injected into its sessions.
        </Empty>
      )}

      {orphans.length ? (
        <ul className="my-0 list-none space-y-1 p-0">
          {orphans.map((item, index) => (
            <li key={index} className="text-[11px] text-fail">
              {item.field}: {item.problem}
            </li>
          ))}
        </ul>
      ) : null}
      <Note>
        Written through the same full-replacement PUT every other field on this page uses.
        Nothing here reaches a host.
      </Note>
    </Panel>
  );
}
