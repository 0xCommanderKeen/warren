// Lighting follows the viewer's local clock; it never changes Chronicle state.
export function daylightAt(date = new Date()) {
  const hour = date.getHours() + date.getMinutes() / 60 + date.getSeconds() / 3600;
  const elevation = Math.sin(((hour - 6) / 24) * Math.PI * 2);
  const daylight = Math.max(0, Math.min(1, (elevation + 0.12) / 0.55));
  return {
    hour,
    daylight,
    night: daylight < 0.22,
    phase: hour < 5 || hour >= 21 ? "Night" : hour < 8 ? "Morning" : hour < 17 ? "Daylight" : hour < 21 ? "Evening" : "Night",
    sun: [Math.cos(((hour - 6) / 24) * Math.PI * 2) * 28, 10 + Math.max(0, elevation) * 32, 18],
  };
}
