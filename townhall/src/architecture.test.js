import { existsSync, readFileSync } from "node:fs";
import { describe, expect, it } from "vitest";

const read = (name) => readFileSync(new URL(name, import.meta.url), "utf8");

/** Every page component in the tree. Adding a page means adding it here. */
const PAGES = [
  "FleetPage", "ResidentsPage", "ResidentDetail", "ResidentNew", "RoutinesPage",
  "ApprovalsPage", "BoardPage", "SkillsPage", "BudgetsPage",
];

/** The ones the shell itself dispatches. ResidentsPage owns its own two sub-pages. */
const MOUNTED = [
  "FleetPage", "ResidentsPage", "RoutinesPage", "ApprovalsPage", "BoardPage", "SkillsPage",
  "BudgetsPage",
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
    expect(navigation).toContain("stripBase");
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
