const DISPLAY_KEY = "arcadia:display:v1";
const CAMERA_KEY = "arcadia:camera:v1";

function read(key) {
  try {
    const value = localStorage.getItem(key);
    return value && value.length < 16384 ? JSON.parse(value) : null;
  } catch { return null; }
}
function write(key, value) {
  try { localStorage.setItem(key, JSON.stringify(value)); } catch { /* Optional preferences. */ }
}

export function readDisplayPreferences() {
  const value = read(DISPLAY_KEY);
  if (value?.version !== 1 || !["high", "low"].includes(value.quality) ||
    (value.paused !== undefined && typeof value.paused !== "boolean")) return { quality: "high" };
  return { quality: value.quality, ...(value.paused === undefined ? {} : { paused: value.paused }) };
}

export function saveDisplayPreferences({ quality, paused }) {
  if (!["high", "low"].includes(quality) || (paused !== undefined && typeof paused !== "boolean")) return;
  write(DISPLAY_KEY, { version: 1, quality, ...(paused === undefined ? {} : { paused }) });
}

function validCamera(value) {
  const vector = point => Array.isArray(point) && point.length === 3 && point.every(n => Number.isFinite(n) && Math.abs(n) <= 100000);
  if (value?.version !== 1 || !vector(value.position) || !vector(value.target) || !Number.isFinite(value.zoom) || value.zoom < .25 || value.zoom > 5) return false;
  const delta = value.position.map((n, i) => n - value.target[i]);
  const distance = Math.hypot(...delta);
  const polar = Math.acos(delta[1] / distance);
  // The exterior camera's far plane is 600; a distant saved pose would hide the village.
  return distance >= 1 && distance <= 500 && polar >= .3 - 1e-6 && polar <= Math.PI / 2.5 + 1e-6;
}

export function readCameraPreferences(bounds) {
  const value = read(CAMERA_KEY);
  if (!validCamera(value)) return null;
  const clamp = (n, min, max) => Math.min(max, Math.max(min, n));
  const target = [clamp(value.target[0], bounds.minX, bounds.maxX), clamp(value.target[1], 0, 20), clamp(value.target[2], bounds.minZ, bounds.maxZ)];
  return { position: value.position.map((n, i) => n + target[i] - value.target[i]), target, zoom: value.zoom };
}

export function saveCameraPreferences(value) {
  const state = { version: 1, position: value.position, target: value.target, zoom: value.zoom };
  if (validCamera(state)) write(CAMERA_KEY, state);
}
