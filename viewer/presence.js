"use strict";

// Keep the house projection tied to where the sprite has actually travelled,
// rather than to the event state (which describes its destination immediately).
(function (root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  else root.BurrowPresence = api;
})(typeof globalThis !== "undefined" ? globalThis : this, function () {
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

  return { startJourney, finishJourney, houseLight };
});
