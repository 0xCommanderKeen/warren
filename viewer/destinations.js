"use strict";

/* Where a villager belongs, as a value rather than a string.
 *
 *   { kind: "home" }           its own house — the default, and the fallback
 *   { kind: "door" }           your doorstep: it is knocking
 *   { kind: "building", id }   a shared building; `id` is a key in PLACES
 *   { kind: "plot", plot }     another villager's front door (delegation)
 *
 * Coordinates, slot bookkeeping, doorway glow and the panel label all switch on
 * `kind`, so adding a destination cannot leave one of them behind on a string
 * comparison it was never taught. Pure: no DOM, no clock — see
 * tests/destinations.test.js.
 */
(function (root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  else root.BurrowDestinations = api;
})(typeof globalThis !== "undefined" ? globalThis : this, function () {

  // The projected place name that means "handing work to somebody else" rather
  // than naming a building (docs/protocol.md, "Where the work happens").
  const DELEGATION = "delegation";
  const HOME = { kind: "home" }, DOOR = { kind: "door" };

  /* Which door a delegating villager walks to.
   *
   * The protocol carries no delegate identity — `tool_called` says an `Agent`
   * ran, never who it was — so the map claims only what the event supports:
   * that the work was handed to *somebody*, at a door that genuinely belongs to
   * somebody. The target is therefore drawn from the villagers actually in the
   * village, never from an empty plot, and it is **held**: a neighbour arriving
   * or leaving must not drag a delegating villager to a different house, which
   * would be movement no event asked for. It changes only when the villager it
   * walked to leaves the village, and it is null when nobody else is home —
   * then the delegating villager stays at its own house rather than crossing
   * the village to a door with nobody behind it.
   */
  function delegatePlot({ held, ownPlot, occupied, hash }) {
    const others = [...new Set(occupied || [])]
      .filter(p => Number.isInteger(p) && p >= 0 && p !== ownPlot)
      .sort((a, b) => a - b);
    if (!others.length) return null;
    if (others.includes(held)) return held;
    return others[Math.abs(hash || 0) % others.length];
  }

  /* `ctx` supplies what only the viewer knows: the villager's own plot, the
     plots occupied right now, the delegation target it is already standing at,
     and its identity hash. */
  function destinationFor(v, ctx) {
    if (v.state === "knocking") return DOOR;
    const away = v.state === "working" || v.state === "stale";
    if (!away || !v.place) return HOME;
    if (v.place !== DELEGATION) return { kind: "building", id: v.place };
    const plot = delegatePlot(ctx || {});
    return plot === null ? HOME : { kind: "plot", plot };
  }

  // Stable identity for the slot table: two villagers at the same destination
  // must share a bucket, and no two destinations may collide in one.
  function destinationKey(dest) {
    switch (dest.kind) {
      case "building": return "building:" + dest.id;
      case "plot":     return "plot:" + dest.plot;
      default:         return dest.kind;
    }
  }

  return { DELEGATION, destinationFor, destinationKey, delegatePlot };
});
