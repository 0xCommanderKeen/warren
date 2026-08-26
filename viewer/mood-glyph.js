"use strict";

(function (root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  else root.BurrowMoodGlyph = api;
})(typeof globalThis === "object" ? globalThis : this, function () {
  const escape = value => String(value).replace(/[&<>"']/g, character =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[character]);
  function duration(value) {
    if (value === null) return "unobserved";
    let remaining = value;
    const hours = Math.floor(remaining / 3600000); remaining %= 3600000;
    const minutes = Math.floor(remaining / 60000); remaining %= 60000;
    const seconds = Math.floor(remaining / 1000); remaining %= 1000;
    const parts = [];
    if (hours) parts.push(`${hours}h`);
    if (minutes || hours) parts.push(`${minutes}m`);
    if (seconds || minutes || hours) parts.push(`${seconds}s`);
    parts.push(`${remaining}ms`);
    return parts.join(" ");
  }
  function render(name, mood, options = {}) {
    if (!mood) return "";
    const authorityUncertain = mood.authority && mood.authority.complete === false;
    const signal = mood.signals;
    const failure = authorityUncertain ? "authority history overflow; exact signal unavailable" : signal.failure.observed ?
      `streak ${signal.failure.streak}; ${signal.failure.failuresLabel} failures in rolling 24 log-hours` : "unobserved";
    const workload = authorityUncertain ? "authority history overflow; exact signal unavailable" : signal.workload.observed ?
      `${signal.workload.level}; density ${signal.workload.density}/24 across eight UTC quarter-hours` : "unobserved";
    const interaction = authorityUncertain ? "authority history overflow; exact signal unavailable" : signal.interaction.observed ?
      `${signal.interaction.level}; ${signal.interaction.kind}; log age ${duration(signal.interaction.logAgeMs)}` : "unobserved";
    const need = authorityUncertain ? "authority history overflow; exact signal unavailable" : signal.unresolvedNeed.observed ?
      `${signal.unresolvedNeed.state}; ${signal.unresolvedNeed.kind}${signal.unresolvedNeed.request_id ?
        ` ${signal.unresolvedNeed.request_id}` : ""}; log age ${duration(signal.unresolvedNeed.logAgeMs)}` :
      "none observed in retained authority";
    return `<details class="mood${options.stale ? " mood-stale" : ""}" data-mood>` +
      `<summary aria-label="${escape(name)}: mood ${escape(mood.status)}; open observed-signal breakdown">` +
      `<span class="mood-glyph" aria-hidden="true">${escape(mood.glyph)}</span>` +
      `<span class="mood-status">${escape(mood.status)}</span></summary>` +
      `<div class="mood-breakdown"><dl><dt>Log age as of</dt><dd class="mood-asof">${escape(mood.anchor)}</dd>` +
      `<dt>Failure streak</dt><dd>${escape(failure)}</dd>` +
      `<dt>Workload density</dt><dd>${escape(workload)}</dd>` +
      `<dt>Last human interaction</dt><dd>${escape(interaction)}</dd>` +
      `<dt>Oldest unresolved human need</dt><dd>${escape(need)}</dd></dl>` +
      `<div>${authorityUncertain ? "Authority history overflow; retained authority is incomplete and no partial score is presented as exact." :
        `score ${mood.score}; ${mood.enoughEvidence ? "enough observed evidence" : "not enough observed evidence"}`}</div>` +
      `${options.stale ? "<div>Presence is stale; staleness is not scored.</div>" : ""}</div></details>`;
  }
  function bind(container) {
    if (!container || container.dataset.moodBound === "true") return;
    container.dataset.moodBound = "true";
    const pointerFocus = new WeakSet();
    container.addEventListener("pointerdown", event => {
      const details = event.target.closest && event.target.closest("details[data-mood]");
      if (details) pointerFocus.add(details);
    });
    container.addEventListener("focusin", event => {
      const details = event.target.closest && event.target.closest("details[data-mood]");
      if (details && !pointerFocus.has(details)) details.open = true;
    });
    container.addEventListener("click", event => {
      const details = event.target.closest && event.target.closest("details[data-mood]");
      if (details) pointerFocus.delete(details);
    });
    container.addEventListener("pointerover", event => {
      const details = event.target.closest && event.target.closest("details[data-mood]");
      if (details && event.pointerType === "mouse") details.open = true;
    });
    container.addEventListener("pointerout", event => {
      const details = event.target.closest && event.target.closest("details[data-mood]");
      if (details && !details.contains(event.relatedTarget) && !details.contains(container.ownerDocument.activeElement)) details.open = false;
    });
    container.addEventListener("keydown", event => {
      if (["Enter", " "].includes(event.key) && event.target.matches &&
          event.target.matches("details[data-mood] > summary")) {
        event.preventDefault();
        event.target.parentElement.open = !event.target.parentElement.open;
        return;
      }
      if (event.key !== "Escape") return;
      const details = event.target.closest && event.target.closest("details[data-mood][open]");
      if (!details) return;
      event.preventDefault(); event.stopImmediatePropagation(); details.open = false;
      details.querySelector("summary").focus();
    });
  }
  return { render, bind };
});
