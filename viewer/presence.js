"use strict";

// Keep the house projection tied to where the sprite has actually travelled,
// rather than to the event state (which describes its destination immediately).
(function (root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  else root.BurrowPresence = api;
})(typeof globalThis !== "undefined" ? globalThis : this, function () {
  /* Whether a villager belongs at its own house right now. A villager working
   * at a shared place (the library) does not, so its house stays dark — and
   * that has to hold whether it walked there or the viewer loaded with it
   * already standing there.
   *
   * `place` is the destination the viewer resolved, not the raw projected name:
   * a name the map has no building for is not a place, and the villager the
   * viewer therefore leaves at home must have a lit house like anyone else. */
  function atHome(state, place) {
    return !place && (state === "working" || state === "resting");
  }

  function startJourney(viz, arrivesHome) {
    // Leaving is physical as soon as the walk starts. On a return trip, retain
    // the away state until the final tween completes.
    if (!arrivesHome) viz.home = false;
  }

  function finishJourney(viz, arrivesHome) {
    viz.home = arrivesHome;
  }

  function houseLight(viz) {
    // Stale means location is unknown, even if the last animation ended at home.
    const home = Boolean(viz.home) && viz.state !== "stale";
    return { home, working: home && viz.state === "working" };
  }

  return { atHome, startJourney, finishJourney, houseLight };
});
