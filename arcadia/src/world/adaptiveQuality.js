/** Ignore startup samples; sustained slow rendering opts into the light profile. */
export function createAdaptiveQuality() {
  let samples = 0, slow = 0, level = "high";
  return {
    reset() { samples = 0; slow = 0; level = "high"; },
    observe(fps) {
      if (!Number.isFinite(fps) || fps <= 0) return level;
      samples += 1;
      if (samples <= 2 || level === "low") return level;
      slow = fps < 40 ? slow + 1 : 0;
      if (slow >= 3) level = "low";
      return level;
    },
  };
}
