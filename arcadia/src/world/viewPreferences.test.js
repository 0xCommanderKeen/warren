import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { readCameraPreferences, readDisplayPreferences, saveCameraPreferences, saveDisplayPreferences } from "./viewPreferences.js";

const bounds = { minX: -20, maxX: 20, minZ: -20, maxZ: 20 };
const pose = { position: [20, 30, 30], target: [0, 0, 0], zoom: 2 };
describe("view preferences", () => {
  beforeEach(() => localStorage.clear());
  afterEach(() => vi.unstubAllGlobals());
  it("retains display choices while leaving reduced-motion default to the caller", () => {
    expect(readDisplayPreferences()).toEqual({ quality: "high" });
    saveDisplayPreferences({ quality: "low", paused: true });
    expect(readDisplayPreferences()).toEqual({ quality: "low", paused: true });
    saveDisplayPreferences({ quality: "high", paused: false });
    expect(readDisplayPreferences()).toEqual({ quality: "high", paused: false });
  });
  it("restores a camera and clamps target to the current world without changing orientation", () => {
    saveCameraPreferences(pose);
    expect(readCameraPreferences(bounds)).toEqual(pose);
    saveCameraPreferences({ position: [120, 30, 130], target: [100, 0, 100], zoom: 3 });
    expect(readCameraPreferences(bounds)).toEqual({ position: [40, 30, 50], target: [20, 0, 20], zoom: 3 });
  });
  it.each([
    { ...pose, zoom: 6 }, { ...pose, zoom: 0 }, { ...pose, position: [0, 0, 0] },
    { ...pose, position: [0, -5, 0] }, { ...pose, position: [0, 30, 0] },
    { ...pose, position: [1e9, 30, 30] }, { ...pose, target: [0, "0", 0] },
    { ...pose, position: [1000, 1500, 1500] },
  ])("ignores malformed or unsafe camera state (%#)", value => {
    localStorage.setItem("arcadia:camera:v1", JSON.stringify({ version: 1, ...value }));
    expect(readCameraPreferences(bounds)).toBeNull();
  });
  it("ignores malformed JSON and invalid display values", () => {
    localStorage.setItem("arcadia:camera:v1", "broken");
    localStorage.setItem("arcadia:display:v1", JSON.stringify({ version: 1, quality: "low", paused: "yes" }));
    expect(readCameraPreferences(bounds)).toBeNull();
    expect(readDisplayPreferences()).toEqual({ quality: "high" });
  });
  it("does not fail when storage access is blocked", () => {
    vi.stubGlobal("localStorage", { getItem() { throw Error("blocked"); }, setItem() { throw Error("blocked"); } });
    expect(readDisplayPreferences()).toEqual({ quality: "high" });
    expect(readCameraPreferences(bounds)).toBeNull();
    expect(() => saveDisplayPreferences({ quality: "low", paused: true })).not.toThrow();
    expect(() => saveCameraPreferences(pose)).not.toThrow();
  });
});
