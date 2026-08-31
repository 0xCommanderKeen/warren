import { existsSync, readFileSync } from "node:fs";
import { describe, expect, it } from "vitest";

describe("frontend foundation", () => {
  it("uses Tailwind directly without legacy style layers", () => {
    const app = readFileSync(new URL("./App.jsx", import.meta.url), "utf8");
    const styles = readFileSync(new URL("./styles.css", import.meta.url), "utf8");

    expect(styles).toContain('@import "tailwindcss"');
    expect(app).toContain("text-amber-300");
    expect(app).toContain("lg:grid-cols-");
    expect(existsSync(new URL("./legacy.css", import.meta.url))).toBe(false);
    expect(existsSync(new URL("./interaction.css", import.meta.url))).toBe(false);
    expect(existsSync(new URL("./visitor-modal.css", import.meta.url))).toBe(false);
  });

  it("reads Chronicle's contract fixture in-tree rather than vendoring a copy", () => {
    const seam = readFileSync(new URL("./fixtures/complete-v1.js", import.meta.url), "utf8");

    expect(seam).toContain("../../../chronicle/tests/fixtures/state-contract/complete-v1.json");
    expect(existsSync(new URL("./fixtures/complete-v1.json", import.meta.url))).toBe(false);
  });
});
