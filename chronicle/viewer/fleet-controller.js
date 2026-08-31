"use strict";

/* DOM controller shared by the page and dependency-free behavioral tests. */
(function (root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  else root.BurrowFleetController = api;
})(typeof globalThis === "object" ? globalThis : this, function () {
  function createFleetController(options) {
    const panel = options.panel;
    const body = options.body;
    const document = options.document;
    const moveFocus = options.moveFocus;

    function focusKey(element) {
      if (!element || !body.contains(element)) return null;
      return element.dataset && element.dataset.fleetFocus || null;
    }

    function captureFocus() {
      const active = document.activeElement;
      const key = focusKey(active);
      if (!key) return null;
      const selection = typeof active.selectionStart === "number" ? {
        start: active.selectionStart, end: active.selectionEnd,
        direction: active.selectionDirection || "none",
      } : null;
      return { key, selection };
    }

    function restoreFocus(snapshot) {
      if (!snapshot) return false;
      const replacement = [...body.querySelectorAll("[data-fleet-focus]")]
        .find(element => element.dataset.fleetFocus === snapshot.key);
      if (!replacement || replacement.disabled) return false;
      replacement.focus({ preventScroll: true });
      if (snapshot.selection && typeof replacement.setSelectionRange === "function") {
        const length = String(replacement.value || "").length;
        replacement.setSelectionRange(Math.min(snapshot.selection.start, length),
          Math.min(snapshot.selection.end, length), snapshot.selection.direction);
      }
      return true;
    }

    function preserveFocus(render) {
      const snapshot = captureFocus();
      const result = render();
      restoreFocus(snapshot);
      return result;
    }

    function selectTab(id, focus = false) {
      options.renderTab(id);
      if (!focus) return;
      const selected = body.querySelector(`[data-fleet-tab="${id}"]`);
      if (selected) selected.focus();
    }

    function click(event) {
      const tab = event.target.closest("[data-fleet-tab]");
      if (tab) { selectTab(tab.dataset.fleetTab, true); return; }
      const agent = event.target.closest("[data-agent]");
      if (agent) options.openAgent(agent.dataset.agent);
    }

    function keydown(event) {
      if (event.key === "Escape") {
        options.close();
        options.launcher.focus();
        return;
      }
      const tabs = [...body.querySelectorAll('[role="tab"]')];
      const current = tabs.indexOf(document.activeElement);
      if (current < 0) return;
      const next = moveFocus(current, event.key, tabs.length);
      if (next === current) return;
      event.preventDefault();
      selectTab(tabs[next].dataset.fleetTab, true);
    }

    function fitViewport(width) {
      const narrow = width <= 680;
      panel.dataset.narrow = narrow ? "true" : "false";
      panel.style.width = narrow ? "100vw" : "";
      panel.style.maxWidth = "100vw";
      panel.style.left = narrow ? "0" : "";
      panel.style.right = narrow ? "auto" : "";
      body.style.minWidth = "0";
      body.style.maxWidth = "100%";
      return { narrow, overflow: Math.max(0, panel.scrollWidth - panel.clientWidth) };
    }

    body.addEventListener("click", click);
    panel.addEventListener("keydown", keydown);
    return { selectTab, fitViewport, captureFocus, restoreFocus, preserveFocus };
  }

  return { createFleetController };
});
