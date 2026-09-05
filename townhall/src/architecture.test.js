import { readFileSync, readdirSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";

const read = (name) => readFileSync(new URL(name, import.meta.url), "utf8");

// Narrow source lint, not evidence of runtime behavior. Discover all production modules,
// including nested/new pages, without requiring a second hand-maintained page inventory.
const sourceFiles = (directory = dirname(fileURLToPath(import.meta.url))) =>
  readdirSync(directory, { withFileTypes: true }).flatMap((entry) => {
    const url = join(directory, entry.name);
    if (entry.isDirectory()) return sourceFiles(url);
    return /\.[jt]sx?$/.test(entry.name) && !/\.test\.[jt]sx?$/.test(entry.name) ? [url] : [];
  });

describe("source lint: authority boundaries", () => {
  const files = sourceFiles();

  it("discovers production pages and nested console modules", () => {
    expect(files.some((file) => file.endsWith("/pages/ResidentsPage.jsx"))).toBe(true);
    expect(files.some((file) => file.endsWith("/console/ledger.jsx"))).toBe(true);
  });

  it("keeps page routing and the pending ledger in their shared owners", () => {
    const pages = files.filter((file) => file.includes("/pages/"));
    expect(pages.length).toBeGreaterThan(0);
    for (const file of pages) {
      const source = readFileSync(file, "utf8");
      expect(source, file).not.toMatch(/\bLedgerProvider\b/);
      expect(source, file).not.toMatch(/window\.location\.pathname/);
    }
  });

  it("forbids literal bearer credentials in production source", () => {
    for (const file of files) {
      const source = readFileSync(file, "utf8");
      expect(source, file).not.toMatch(/STEWARD_TOKEN\s*[:=]\s*["'][^"']+["']/);
      expect(source, file).not.toMatch(/Authorization["']?\s*:\s*["']Bearer [^$"']/);
    }
  });
});

describe("the dev proxy is the deployed origin's route table", () => {
  // warren#242: `vite.config.js` and `arcadia/deploy/nginx.conf` carry the same list of
  // Steward paths, one for `pnpm dev` and one for the NAS. A path in one and not the other
  // is a page that works in dev and serves the village's index.html deployed, or the
  // reverse — which is how `/tasks/{id}/lineage` and `POST /delegate` were absent from both
  // and nothing said so. Both are checked against Steward itself rather than each other.
  // `.github/workflows/townhall.yml` lists `api.py` among this suite's paths.

  /** Steward's top-level route segments, from the `@app` decorators that declare them. */
  const stewardApiRoutes = () => {
    const directory = join(dirname(fileURLToPath(import.meta.url)), "../../steward/src/steward/routes");
    const api = [
      read("../../steward/src/steward/api.py"),
      ...readdirSync(directory)
        .filter((name) => name.endsWith(".py"))
        .map((name) => readFileSync(join(directory, name), "utf8")),
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

  it("keeps ember legible as a button ground", () => {
    expect(contrast(token("on-ember"), token("ember"))).toBeGreaterThanOrEqual(4.5);
  });
});
