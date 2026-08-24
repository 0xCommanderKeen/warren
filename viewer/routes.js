(function (root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  else root.BurrowRoutes = api;
})(typeof globalThis !== "undefined" ? globalThis : this, function () {
  "use strict";

  function routePoints(from, target) {
    if (Number.isFinite(target.approachX)) {
      return [
        { x: target.approachX, y: from.y },
        { x: target.approachX, y: target.y },
        { x: target.x, y: target.y },
      ];
    }
    return [{ x: target.x, y: from.y }, { x: target.x, y: target.y }];
  }

  return { routePoints };
});
