import { describe, expect, it } from "vitest";
import {
  BUDGET_FIELDS, changed, getIn, grantEntries, grantRows, linesToList, listToLines,
  numberValue, scalarValue, setIn,
} from "./manifest.js";

const manifest = () => ({
  version: 0,
  id: "hob",
  summary: "The household spirit.",
  soul: { name: "Hob", accent: "#a68a4f", role: "life bot", file: "soul.md" },
  charter: { mission: "Keep the household running.", duties: ["Post a summary."], rules: ["Never send email."] },
  budgets: { daily_cost_usd: 10 },
  deploy: { host: "dxp2800", user: "Miha", container: "hob" },
  routines: [{ id: "inbox-read", schedule: "15 * * * *" }],
});

describe("reading a manifest", () => {
  it("reads a dotted path and answers undefined for a missing one", () => {
    expect(getIn(manifest(), "soul.name")).toBe("Hob");
    expect(getIn(manifest(), "budgets.daily_tokens")).toBeUndefined();
    expect(getIn(manifest(), "nothing.at.all")).toBeUndefined();
    expect(getIn(undefined, "soul.name")).toBeUndefined();
  });
});

describe("editing a manifest", () => {
  it("writes a nested value without touching anything else", () => {
    const before = manifest();
    const after = setIn(before, "soul.name", "Quill");

    expect(after.soul.name).toBe("Quill");
    expect(before.soul.name).toBe("Hob");
    // Every block no field on the page knows about survives, identical.
    expect(after.deploy).toEqual(before.deploy);
    expect(after.routines).toEqual(before.routines);
    expect(after.charter).toBe(before.charter);
  });

  it("creates the branch when a block does not exist yet", () => {
    const after = setIn({ id: "pip" }, "budgets.daily_cost_usd", 2.5);
    expect(after).toEqual({ id: "pip", budgets: { daily_cost_usd: 2.5 } });
  });

  it("deletes rather than writing null, because absent means unlimited", () => {
    const after = setIn(manifest(), "budgets.daily_cost_usd", undefined);
    expect("daily_cost_usd" in after.budgets).toBe(false);
    // The block itself stays: emptying it is not the same as deleting a block nobody asked
    // to delete.
    expect(after.budgets).toEqual({});
  });

  it("is a no-op when nothing actually changed", () => {
    const before = manifest();
    expect(setIn(before, "soul.name", "Hob")).toBe(before);
    expect(setIn(before, "budgets.daily_tokens", undefined)).toBe(before);
  });

  it("round-trips a whole manifest through an edit and back", () => {
    const before = manifest();
    const edited = setIn(before, "summary", "Something else");
    expect(setIn(edited, "summary", before.summary)).toEqual(before);
  });
});

describe("form values", () => {
  it("moves a one-per-line textarea to a list and back", () => {
    expect(linesToList(" a \n\n b \n")).toEqual(["a", "b"]);
    expect(listToLines(["a", "b"])).toBe("a\nb");
    expect(listToLines(undefined)).toBe("");
    expect(linesToList("")).toEqual([]);
  });

  it("shows an absent scalar as an empty box, not as 'null'", () => {
    expect(scalarValue(null)).toBe("");
    expect(scalarValue(undefined)).toBe("");
    expect(scalarValue(0)).toBe("0");
    expect(scalarValue("x")).toBe("x");
  });

  it("reads a cleared cap as unlimited and a typed one as a number", () => {
    expect(numberValue("")).toBeUndefined();
    expect(numberValue("   ")).toBeUndefined();
    expect(numberValue("10.5")).toBe(10.5);
    expect(numberValue("20400", { integer: true })).toBe(20400);
    expect(numberValue("20400.7", { integer: true })).toBe(20400);
  });

  it("hands nonsense to steward rather than swallowing it", () => {
    // A refusal a person can see beats a number this file quietly invented.
    expect(numberValue("abc")).toBeNull();
  });

  it("covers exactly the three dimensions steward's Budgets model declares", () => {
    expect(BUDGET_FIELDS.map((field) => field.path)).toEqual([
      "budgets.daily_cost_usd", "budgets.daily_tokens", "budgets.max_run_seconds",
    ]);
  });
});

describe("noticing an edit", () => {
  it("knows a draft that diverged from the one that was loaded", () => {
    const before = manifest();
    expect(changed(before, manifest())).toBe(false);
    expect(changed(before, setIn(before, "soul.role", "note bot"))).toBe(true);
  });
});

/* -- skill grants -------------------------------------------------------------------- */

/** Both spellings steward accepts, side by side, plus a key no form here knows about. */
const granted = {
  ...manifest(),
  skills: ["journal", { id: "write-journal", note: "end of run" }, { id: "vendored", source: "local" }],
};

describe("editing a resident's grants", () => {
  it("round-trips an untouched block byte for byte, bare names and all", () => {
    // The one that matters. A picker that normalised `journal` into `{id: journal}` would
    // rewrite `skills:` for every resident whose author preferred bare names, the moment
    // somebody opened the page to fix a typo in the charter and pressed save.
    const rows = grantRows(granted);

    expect(grantEntries(rows)).toEqual(granted.skills);
    expect(JSON.stringify(grantEntries(rows))).toBe(JSON.stringify(granted.skills));
  });

  it("reads a note off either spelling and leaves the entry alone", () => {
    expect(grantRows(granted).map((row) => [row.id, row.note])).toEqual([
      ["journal", ""],
      ["write-journal", "end of run"],
      ["vendored", ""],
    ]);
  });

  it("keeps a bare name bare until it is given a note", () => {
    const rows = grantRows(granted);

    expect(grantEntries([{ ...rows[0], note: "  " }])).toEqual(["journal"]);
    expect(grantEntries([{ ...rows[0], note: "because Hob writes one" }])).toEqual([
      { id: "journal", note: "because Hob writes one" },
    ]);
  });

  it("keeps everything else on a grant when only the note changes", () => {
    // `source: local` is steward's, not this form's, and a picker that dropped it would
    // silently repoint the grant at the library.
    const rows = grantRows(granted);

    expect(grantEntries([{ ...rows[2], note: "vendored on purpose" }])).toEqual([
      { id: "vendored", source: "local", note: "vendored on purpose" },
    ]);
    expect(grantEntries([{ ...rows[1], note: "" }])).toEqual([{ id: "write-journal" }]);
  });

  it("adds a newly ticked skill as a bare name, at the end", () => {
    const rows = grantRows(granted);

    expect(grantEntries([...rows, { id: "read-inbox", note: "", entry: undefined }])).toEqual([
      ...granted.skills, "read-inbox",
    ]);
  });

  it("deletes the key rather than writing an empty list when the last grant goes", () => {
    // An absent `skills` and `skills: []` say the same thing, and only one of them is a
    // line somebody has to read and wonder about.
    expect(grantEntries([])).toBeUndefined();
    expect(setIn(granted, "skills", grantEntries([]))).not.toHaveProperty("skills");
  });

  it("says a manifest with no grants at all has no rows", () => {
    expect(grantRows(manifest())).toEqual([]);
    expect(grantRows(undefined)).toEqual([]);
  });
});
