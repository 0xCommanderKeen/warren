import { describe, expect, it } from "vitest";
import { createAdaptiveQuality } from "./adaptiveQuality.js";
describe("adaptive rendering", () => {
  it("ignores warmup and isolated stalls but lowers sustained slow rendering", () => {
    const quality = createAdaptiveQuality();
    for (const fps of [10,10,30,60,30,30]) expect(quality.observe(fps)).toBe("high");
    expect(quality.observe(30)).toBe("low");
    expect(quality.observe(60)).toBe("low");
    quality.reset();
    expect(quality.observe(60)).toBe("high");
  });
  it("ignores invalid samples", () => {
    const quality = createAdaptiveQuality();
    for (const fps of [NaN,Infinity,0,-1]) expect(quality.observe(fps)).toBe("high");
  });
});
