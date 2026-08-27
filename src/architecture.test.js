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
});
