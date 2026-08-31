"use strict";

/* The browser and Python rotation code load the same retention-policy.json.
 * Browser startup is deliberately synchronous: projection modules consume the
 * policy while their scripts are evaluated, before the asynchronous runtime
 * starts. */
(function (root, factory) {
  const policy = factory();
  if (typeof module === "object" && module.exports) module.exports = policy;
  else root.BurrowRetentionPolicy = policy;
})(typeof globalThis === "object" ? globalThis : this, function () {
  if (typeof module === "object" && module.exports) {
    return Object.freeze(require("../retention-policy.json"));
  }
  const request = new XMLHttpRequest();
  request.open("GET", "/retention-policy.json", false);
  request.send();
  if (request.status < 200 || request.status >= 300) {
    throw new Error("could not load retention policy");
  }
  return Object.freeze(JSON.parse(request.responseText));
});
