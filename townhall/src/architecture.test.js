import { existsSync, readFileSync, readdirSync } from "node:fs";
import { describe, expect, it } from "vitest";

const read = (name) => readFileSync(new URL(name, import.meta.url), "utf8");

/** Every page component in the tree. Adding a page means adding it here. */
const PAGES = [
  "FleetPage", "ResidentsPage", "ResidentDetail", "ResidentNew", "RoutinesPage",
  "ApprovalsPage", "BoardPage", "SkillsPage", "BudgetsPage", "DiagnosticsPage",
];

/** The ones the shell itself dispatches. ResidentsPage owns its own two sub-pages. */
const MOUNTED = [
  "FleetPage", "ResidentsPage", "RoutinesPage", "ApprovalsPage", "BoardPage", "SkillsPage",
  "BudgetsPage", "DiagnosticsPage",
];

describe("frontend foundation", () => {
  it("uses Tailwind directly without legacy style layers", () => {
    expect(read("./styles.css")).toContain('@import "tailwindcss"');
    expect(existsSync(new URL("./legacy.css", import.meta.url))).toBe(false);
    expect(existsSync(new URL("./interaction.css", import.meta.url))).toBe(false);
    expect(existsSync(new URL("./visitor-modal.css", import.meta.url))).toBe(false);
  });

  it("reads Chronicle's contract fixture in-tree rather than vendoring a copy", () => {
    const seam = read("./fixtures/complete-v1.js");

    expect(seam).toContain("../../../chronicle/tests/fixtures/state-contract/complete-v1.json");
    expect(existsSync(new URL("./fixtures/complete-v1.json", import.meta.url))).toBe(false);
  });

  it("reads Steward's OpenAPI document in-tree rather than vendoring a copy", () => {
    // warren#321, and the same rule as Chronicle's fixture above: Steward commits the
    // document `make openapi-write` exports, and this tree reads that file three
    // directories away. A copy here would be a contract that goes stale in silence.
    const seam = read("./steward/contract.test.js");

    expect(seam).toContain("../../../steward/docs/openapi.json");
    expect(existsSync(new URL("./steward/openapi.json", import.meta.url))).toBe(false);
  });
});

describe("the console shell", () => {
  it("hosts every page from one sidebar rather than a page owning the chrome", () => {
    const app = read("./App.jsx");

    expect(app).toContain("NAV.map");
    expect(app).toContain("rail:fixed");
    for (const page of MOUNTED) {
      expect(app).toContain(page);
    }
  });

  it("keeps the pending ledger above every page rather than inside one", () => {
    // An action asked for on Routines is still unconfirmed while you read the Board, so the
    // ledger outlives the page that raised it. A ledger owned by a page would vanish on
    // navigation and take the only record of an in-flight write with it.
    const app = read("./App.jsx");
    expect(app).toContain("LedgerProvider");
    for (const page of PAGES) {
      expect(read(`./pages/${page}.jsx`)).not.toContain("LedgerProvider");
    }
  });

  it("never writes 'confirmed' anywhere but behind a read of steward's own store", () => {
    // The console's hardest rule, and the one a port is most likely to lose. Every
    // occurrence of the word lives in ledger.jsx, inside a function that has already
    // awaited an answer from steward — never in a page, where it would be a click's
    // intention rather than steward's word.
    for (const page of PAGES) {
      expect(read(`./pages/${page}.jsx`)).not.toContain('"confirmed"');
    }
    const ledger = read("./console/ledger.jsx");
    for (const [before] of [...ledger.matchAll(/state: "confirmed"/g)].map((match) => [
      ledger.slice(0, match.index),
    ])) {
      expect(before).toMatch(/await client\.\w+\(/);
    }
  });

  it("keeps the atlas look inside the fleet page and nowhere else", () => {
    const fleet = read("./pages/FleetPage.jsx");
    const app = read("./App.jsx");

    // warren#225: the fleet views become one subpage; their styling survives there only.
    expect(fleet).toContain("text-amber-300");
    expect(fleet).toContain("lg:grid-cols-");
    expect(app).not.toContain("text-amber-300");
  });

  it("routes through the base prefix instead of reading pathname raw", () => {
    // The bug arcadia/docs/deployment.md named: under /observatory/ a deep link matched
    // nothing, because the router treated the mount prefix as part of the route.
    const navigation = read("./navigation.jsx");
    expect(navigation).toContain("matchPath");
    expect(read("./App.jsx")).toContain("import.meta.env.BASE_URL");

    for (const page of PAGES) {
      expect(read(`./pages/${page}.jsx`)).not.toContain("window.location.pathname");
    }
  });

  it("never bakes a steward credential into the bundle", () => {
    const sources = [
      "./App.jsx", "./navigation.jsx", "./console/Gate.jsx", "./console/ledger.jsx",
      "./steward/client.js", "./steward/credential.js", "./steward/context.jsx",
      ...PAGES.map((page) => `./pages/${page}.jsx`),
    ].map(read).join("\n");

    // No literal token anywhere, and no Authorization header built from a literal.
    expect(sources).not.toMatch(/STEWARD_TOKEN\s*[:=]\s*["'][^"']+["']/);
    expect(sources).not.toMatch(/Authorization["']?\s*:\s*["']Bearer [^$"']/);

    // Exactly one module mints the header, and it mints it from stored input.
    expect(read("./steward/credential.js")).toMatch(/Bearer \$\{/);
    expect(read("./steward/client.js")).not.toContain("Bearer ");
    expect(read("./steward/credential.js")).toContain("sessionStorage");
  });
});

/* -- origin overrides (#241) ----------------------------------------------------------- */

/** The body of a top-level `function name(…) {…}`, up to the closing brace in column 0. */
const functionBody = (source, name) => {
  const start = source.indexOf(`function ${name}(`);
  expect(start, `no function ${name} to read`).toBeGreaterThan(-1);
  return source.slice(start, source.indexOf("\n}", start));
};

describe("the dev proxy is the deployed origin's route table", () => {
  // warren#242: `vite.config.js` and `arcadia/deploy/nginx.conf` carry the same list of
  // Steward paths, one for `pnpm dev` and one for the NAS. A path in one and not the other
  // is a page that works in dev and serves the village's index.html deployed, or the
  // reverse — which is how `/tasks/{id}/lineage` and `POST /delegate` were absent from both
  // and nothing said so. Both are checked against Steward itself rather than each other.
  // `.github/workflows/townhall.yml` lists `api.py` among this suite's paths.

  /** Steward's top-level route segments, from the `@app` decorators that declare them. */
  const stewardApiRoutes = () => {
    const directory = "../steward/src/steward/routes";
    const api = [
      read("../../steward/src/steward/api.py"),
      ...readdirSync(directory)
        .filter((name) => name.endsWith(".py"))
        .map((name) => readFileSync(`${directory}/${name}`, "utf8")),
    ].join("\n");
    const declared = [...api.matchAll(/@app\.(?:get|post|put|patch|delete)\("(\/[^"]*)"/g)];
    declared.push(...api.matchAll(/@routes\.(?:get|post|put|patch|delete)\("(\/[^\"]*)"/g));
    expect(declared.length, "no API routes found — the reader has gone stale").toBeGreaterThan(0);
    return [...new Set(declared.map((match) => match[1].split("/")[1]))].sort();
  };

  it("proxies every top-level Steward route to Steward", () => {
    const config = read("../vite.config.js");
    const list = config.slice(
      config.indexOf("...Object.fromEntries("),
      config.indexOf("].map("),
    );
    expect(list, "no proxy path list to read").toContain("/residents");

    const proxied = [...list.matchAll(/"\/([a-z]+)"/g)].map((match) => match[1]).sort();
    expect(proxied).toEqual(stewardApiRoutes());
  });

  it("sends Chronicle's snapshot somewhere else entirely", () => {
    // The one path in this proxy that is not Steward's, and the reason the list above is
    // read from its own block rather than from every quoted path in the file.
    const config = read("../vite.config.js");
    expect(config).toContain('"/state"');
    expect(stewardApiRoutes()).not.toContain("state");
  });
});

describe("the origin overrides are a development convenience, enforced as one", () => {
  // warren#241: `?steward=` and `?backend=` were honest-system dev conveniences that the
  // deployed bundle also honoured, so `/observatory/?steward=https://evil.tld` opened in an
  // unlocked tab sent the operator bearer token — the control plane's master key — to
  // whoever wrote the link. The gate is `import.meta.env.DEV`, which Vite resolves to false
  // at build time: the branch that reads the parameter is eliminated from what ships.

  it.each([
    ["./steward/context.jsx", "stewardBase", "steward"],
    ["./App.jsx", "chronicleBase", "backend"],
  ])("reads %s's override only behind import.meta.env.DEV", (file, name, param) => {
    const source = read(file);
    const body = functionBody(source, name);

    expect(body).toContain("import.meta.env.DEV");
    expect(body.indexOf("import.meta.env.DEV")).toBeLessThan(body.indexOf(`"${param}"`));
    expect(body).toContain(`get("${param}")`);

    // And nowhere else in that file: one read, inside the one guarded function.
    expect([...source.matchAll(new RegExp(`get\\("${param}"\\)`, "g"))]).toHaveLength(1);
  });

  it("names the query parameters in no other module", () => {
    const others = [
      "./navigation.jsx", "./transport.js", "./main.jsx", "./routes.js", "./model.js",
      "./console/Gate.jsx", "./console/ledger.jsx",
      "./steward/client.js", "./steward/credential.js",
      ...PAGES.map((page) => `./pages/${page}.jsx`),
    ].map(read).join("\n");

    expect(others).not.toContain('get("steward")');
    expect(others).not.toContain('get("backend")');
  });

  it("refuses the credential to any base that is not this origin", () => {
    // Defence in depth: even if a base reached the client from somewhere this file cannot
    // see, the bearer header does not follow it off-origin.
    const client = read("./steward/client.js");

    expect(client).toContain("isSameOrigin");
    expect(client).toContain("cross_origin_base");
    // The refusal is what a shipped bundle does unconditionally; only a dev build relaxes it.
    expect(functionBody(client, "createStewardClient")).toContain("import.meta.env.DEV");
  });
});

/* -- contrast (#152) ------------------------------------------------------------------ */

const channel = (value) => {
  const srgb = value / 255;
  return srgb <= 0.04045 ? srgb / 12.92 : ((srgb + 0.055) / 1.055) ** 2.4;
};

const luminance = (hex) => {
  const [r, g, b] = [1, 3, 5].map((at) => parseInt(hex.slice(at, at + 2), 16));
  return 0.2126 * channel(r) + 0.7152 * channel(g) + 0.0722 * channel(b);
};

const contrast = (fore, back) => {
  const [light, dark] = [luminance(fore), luminance(back)].sort((a, b) => b - a);
  return (light + 0.05) / (dark + 0.05);
};

/** Pull a `--color-x: #rrggbb;` declaration out of the theme block. */
const token = (name) => {
  const match = read("./styles.css").match(new RegExp(`--color-${name}:\\s*(#[0-9a-fA-F]{6})`));
  if (!match) throw new Error(`no --color-${name} in styles.css`);
  return match[1];
};

describe("the console palette meets WCAG AA", () => {
  // The console's own audit measured its quietest body text at 2.86:1. This is the port,
  // so it is also the fix: the hierarchy is kept and every step is lifted until it passes.
  const surfaces = ["void", "deep", "deeper", "raise"];
  const texts = ["ink", "dim", "faint", "read", "ember", "wait", "live", "fail"];

  it.each(texts)("renders %s at 4.5:1 or better on every surface", (text) => {
    for (const surface of surfaces) {
      expect(contrast(token(text), token(surface))).toBeGreaterThanOrEqual(4.5);
    }
  });

  it("keeps the console's three-step hierarchy rather than flattening it", () => {
    const onVoid = (name) => contrast(token(name), token("void"));
    expect(onVoid("ink")).toBeGreaterThan(onVoid("dim"));
    expect(onVoid("dim")).toBeGreaterThan(onVoid("faint"));
  });

  it("has retired the exact value the audit failed", () => {
    // #5d5a51 is the console's old --faint, measured at 2.86:1. It may still be named in a
    // comment explaining the fix; it may not be a token any more.
    expect(read("./styles.css")).not.toMatch(/--color-[\w-]+:\s*#5d5a51/i);
    expect(token("faint").toLowerCase()).not.toBe("#5d5a51");
  });

  it("keeps ember legible as a button ground", () => {
    expect(contrast(token("on-ember"), token("ember"))).toBeGreaterThanOrEqual(4.5);
  });
});
