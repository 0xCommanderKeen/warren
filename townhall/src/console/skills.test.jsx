/* The skills picker on the declaration editor (warren#331).
 *
 * The behaviours worth protecting are the two that cannot be seen by reading the page: that
 * opening the editor and saving does not rewrite `skills:` into a shape nobody asked for,
 * and that a grant the library does not have is still on the screen with steward's refusal
 * on it — because that is the only grant that is actually wrong, and hiding it would draw a
 * resident as holding nothing wrong while the save kept being refused.
 */

import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { NavigationProvider } from "../navigation.jsx";
import { StewardProvider } from "../steward/context.jsx";
import ResidentsPage from "../pages/ResidentsPage.jsx";

function memoryStorage() {
  const map = new Map();
  return {
    getItem: (key) => (map.has(key) ? map.get(key) : null),
    setItem: (key, value) => map.set(key, String(value)),
    removeItem: (key) => map.delete(key),
  };
}

const json = (status, body) => ({
  status,
  ok: status >= 200 && status < 300,
  text: async () => JSON.stringify(body),
});

const LIBRARY = {
  library: "skills",
  skills: [
    { name: "journal", description: "Write the daily journal.", default: false, holders: [] },
    { name: "read-inbox", description: "Read the resident's letters.", default: false, holders: [] },
    { name: "house-style", description: "The shared voice.", default: true, holders: [] },
  ],
  errors: [],
};

/** A declaration whose manifest carries both grant spellings and one block no form knows. */
const DECLARATION = {
  id: "hob",
  manifest: {
    version: 0,
    id: "hob",
    soul: { name: "Hob", char: "Keeper", role: "life bot", accent: "#a68a4f" },
    skills: ["journal", { id: "write-journal", note: "end of run" }],
    deploy: { host: "dxp2800" },
    routines: [{ id: "daily", schedule: "0 9 * * *", prompt: "Write it." }],
  },
  text: "version: 0\n",
  soul: "---\n---\n",
  soul_file: "soul.md",
  revision: "sha256:decl",
  paths: ["residents/hob/manifest.yaml"],
};

/** Steward, scripted: the library, the declaration, and whatever the write should answer. */
function steward({ library = LIBRARY, declaration = DECLARATION, write } = {}) {
  return vi.fn((url, init = {}) => {
    const path = String(url).split("?")[0];
    if (init.method === "PUT") {
      return Promise.resolve(
        write ||
          json(200, {
            status: "accepted",
            commit: { committed: true, sha: "9f1c0a77bbdd", note: "committed" },
            warnings: [],
            message: "written and validated",
          }),
      );
    }
    if (path.endsWith("/skills")) return Promise.resolve(json(200, library));
    return Promise.resolve(json(200, declaration));
  });
}

function mount(fetch) {
  const storage = memoryStorage();
  storage.setItem("townhall.steward.operator", "operator-token");
  return render(
    <NavigationProvider base="/">
      <StewardProvider storage={storage} fetch={fetch}>
        <ResidentsPage page="residentDeclaration" params={{ id: "hob" }} />
      </StewardProvider>
    </NavigationProvider>,
  );
}

/** The manifest the form actually sent. */
async function sentManifest(fetch) {
  await waitFor(() => expect(fetch.mock.calls.some(([, init]) => init?.method === "PUT")).toBe(true));
  const [, init] = fetch.mock.calls.find(([, call]) => call?.method === "PUT");
  return JSON.parse(init.body).manifest;
}

const save = () => fireEvent.click(screen.getByRole("button", { name: /write declaration/i }));

/**
 * Wait until the picker has drawn its library rows.
 *
 * The panel draws nothing until `GET /skills` has answered, so every interaction below has
 * to wait for that rather than for the declaration: a grant clicked while the library was
 * still in flight would be clicked on a row that is about to be replaced.
 */
const drawn = () => screen.findByRole("checkbox", { name: "read-inbox" });

/** One picker row, by the skill's name, so a note box can be found inside its own row. */
const rowFor = (name) => screen.getByLabelText(new RegExp(`^note for ${name}$`));

afterEach(cleanup);

describe("granting and revoking a skill from the editor", () => {
  it("ticks the grants the manifest already carries and marks defaults inherited", async () => {
    mount(steward());

    const inbox = await drawn();
    expect(inbox.checked).toBe(false);
    expect(screen.getByRole("checkbox", { name: "journal" }).checked).toBe(true);
    // A default is ticked and cannot be unticked: "every resident holds this" is not
    // something a manifest can say otherwise about.
    const inherited = screen.getByRole("checkbox", { name: "house-style" });
    expect(inherited.checked).toBe(true);
    expect(inherited.disabled).toBe(true);
  });

  it("sends the declaration back unchanged apart from skills", async () => {
    const fetch = steward();
    mount(fetch);

    fireEvent.click(await drawn());
    save();

    const sent = await sentManifest(fetch);
    expect(sent.skills).toEqual(["journal", { id: "write-journal", note: "end of run" }, "read-inbox"]);
    // Everything else — including the two blocks no field on this page knows about —
    // round-trips exactly as it was read.
    expect({ ...sent, skills: undefined }).toEqual({ ...DECLARATION.manifest, skills: undefined });
  });

  it("does not touch skills at all when nothing in the picker was touched", async () => {
    const fetch = steward();
    mount(fetch);

    await drawn();
    save();

    expect((await sentManifest(fetch)).skills).toEqual(DECLARATION.manifest.skills);
  });

  it("revokes a grant by unticking it", async () => {
    const fetch = steward();
    mount(fetch);

    await drawn();
    fireEvent.click(screen.getByRole("checkbox", { name: "journal" }));
    save();

    expect((await sentManifest(fetch)).skills).toEqual([{ id: "write-journal", note: "end of run" }]);
  });

  it("edits a grant's note in place, and only for grants it actually holds", async () => {
    const fetch = steward();
    mount(fetch);

    await drawn();
    // Only granted skills carry a note box: there is nothing to write a note on for a skill
    // this resident does not hold.
    expect(screen.queryByLabelText(/^note for read-inbox$/)).toBeNull();
    fireEvent.change(rowFor("journal"), { target: { value: "because Hob writes one" } });
    save();

    expect((await sentManifest(fetch)).skills).toEqual([
      { id: "journal", note: "because Hob writes one" },
      { id: "write-journal", note: "end of run" },
    ]);
  });

  it("shows a grant the library does not have, and lands steward's refusal on it", async () => {
    const fetch = steward({
      write: json(422, {
        detail: {
          error: "manifest_invalid",
          message: "the manifest does not validate",
          diagnostics: [
            {
              file: "residents/hob/manifest.yaml",
              field: "skills[1].id",
              problem: "skill 'write-journal' is not in the skills library",
              example: "id: journal",
              severity: "error",
            },
          ],
        },
      }),
    });
    mount(fetch);

    // It is in the manifest and not in the library, so it is drawn after the library rows,
    // named for what it is rather than quietly left out.
    await drawn();
    const stranger = screen.getByRole("checkbox", { name: "write-journal" });
    expect(stranger.checked).toBe(true);
    expect(screen.getByText("not in the library")).toBeTruthy();

    save();

    // On the field, not only at the top of the page: `skills[1].id` is keyed on this
    // grant's position in the manifest, and a refusal that only appeared in the banner
    // would leave the operator counting rows to find out which one it meant.
    await screen.findAllByText(/is not in the skills library/);
    const row = stranger.closest("label").parentElement;
    expect(within(row).getByText(/is not in the skills library/)).toBeTruthy();
  });

  it("says nothing is wrong when steward is pointed at no library at all", async () => {
    // `library: null` is not an empty library. Steward checks no grant and injects no skill
    // in that state — `grant_diagnostics` complains about nothing and the save succeeds — so
    // a panel that badged every grant "not in the library" would be inventing a refusal and
    // inviting an operator to revoke grants that are perfectly fine.
    const fetch = steward({ library: { library: null, skills: [], errors: [] } });
    mount(fetch);

    expect(await screen.findByRole("checkbox", { name: "journal" })).toBeTruthy();
    expect(screen.queryByText("not in the library")).toBeNull();
    expect(screen.getByText(/pointed at no skills library/i)).toBeTruthy();
    save();

    expect((await sentManifest(fetch)).skills).toEqual(DECLARATION.manifest.skills);
  });

  it("draws a repeated grant on its own line, so unticking removes the line clicked", async () => {
    // Nothing in steward refuses `skills: [journal, journal]`. Drawn as one row, unticking
    // it removed the *other* line and left the box ticked — a click that appeared to do
    // nothing, and steward's diagnostic about the hidden line rendered nowhere.
    const twice = {
      ...DECLARATION,
      manifest: { ...DECLARATION.manifest, skills: ["journal", "journal"] },
    };
    const fetch = steward({ declaration: twice });
    mount(fetch);

    await drawn();
    expect(screen.getByText("granted twice")).toBeTruthy();
    const rows = screen.getAllByRole("checkbox", { name: "journal" });
    expect(rows).toHaveLength(2);
    fireEvent.click(rows[1]);
    save();

    expect((await sentManifest(fetch)).skills).toEqual(["journal"]);
  });

  it("keeps the picker usable when the library itself cannot be read", async () => {
    // The grants are in the manifest, not in the library, so a library that 500s must not
    // take the only view of them off the screen.
    const fetch = vi.fn((url, init = {}) => {
      const path = String(url).split("?")[0];
      if (init.method === "PUT") return Promise.resolve(json(200, { status: "accepted", warnings: [] }));
      if (path.endsWith("/skills")) {
        return Promise.resolve(json(503, { detail: { error: "unavailable", message: "no library" } }));
      }
      return Promise.resolve(json(200, DECLARATION));
    });
    mount(fetch);

    expect(await screen.findByText(/the skills library could not be read/i)).toBeTruthy();
    // Not "not in the library": steward never said that, and a panel that could not read
    // the library does not get to decide a grant is wrong.
    expect(screen.queryByText("not in the library")).toBeNull();
    const held = screen.getByRole("checkbox", { name: "journal" });
    expect(held.checked).toBe(true);
    fireEvent.click(held);
    save();

    expect((await sentManifest(fetch)).skills).toEqual([{ id: "write-journal", note: "end of run" }]);
  });
});

describe("the way back from retirement, in the editor", () => {
  const retired = {
    ...DECLARATION,
    manifest: { ...DECLARATION.manifest, retired: true },
  };

  it("offers to untick retired, and nothing to tick it", async () => {
    // Ticking it here is precisely the half-done retirement this whole issue is about: the
    // mark without the stop leaves a container running with a live village token.
    mount(steward({ declaration: retired }));

    await drawn();
    const box = screen.getByRole("checkbox", { name: "retired" });
    expect(box.checked).toBe(true);
    expect(within(screen.getByText(/FIRST half/).closest("label")).getByRole("checkbox")).toBe(box);

    cleanup();
    mount(steward());
    await drawn();
    expect(screen.queryByRole("checkbox", { name: "retired" })).toBeNull();
  });

  it("stays on the screen once unticked, so an accidental click is one click back", async () => {
    // It is drawn because the file on disk says retired, not because the draft does. Gated
    // on the draft, unticking took the box away with it and the only way back was to
    // discard the whole unsaved edit.
    mount(steward({ declaration: retired }));

    await drawn();
    const box = screen.getByRole("checkbox", { name: "retired" });
    fireEvent.click(box);
    expect(screen.getByRole("checkbox", { name: "retired" }).checked).toBe(false);
    fireEvent.click(screen.getByRole("checkbox", { name: "retired" }));
    expect(screen.getByRole("checkbox", { name: "retired" }).checked).toBe(true);
  });

  it("deletes the key rather than writing retired: false", async () => {
    const fetch = steward({ declaration: retired });
    mount(fetch);

    await drawn();
    fireEvent.click(screen.getByRole("checkbox", { name: "retired" }));
    save();

    expect(await sentManifest(fetch)).not.toHaveProperty("retired");
  });
});
