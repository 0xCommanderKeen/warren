"use strict";

/* One renderer-owned authority for character sprite names. Projection and
 * declaration UIs consume this exact frozen array in both browser and Node
 * contexts, so a selectable declaration can never drift from what Burrow can
 * render. */
(function (root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  else root.BurrowSprites = api;
})(typeof globalThis === "object" ? globalThis : this, function () {
  const CHARS = Object.freeze(["Villager", "Villager2", "Villager3", "Villager4", "Villager5",
    "Woman", "Boy", "OldMan", "Princess", "Hunter", "Noble", "Monk"]);
  return Object.freeze({ CHARS });
});
