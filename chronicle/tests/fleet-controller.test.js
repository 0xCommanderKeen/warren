"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const { moveFocus } = require("../viewer/fleet-operations.js");
const { createFleetController } = require("../viewer/fleet-controller.js");

class Element {
  constructor(document, attributes = {}) {
    this.ownerDocument = document;
    this.dataset = attributes.dataset || {};
    this.role = attributes.role;
    this.style = {};
    this.listeners = {};
    this.children = [];
    this.parent = null;
    this.clientWidth = attributes.clientWidth || 0;
    this.contentWidth = attributes.contentWidth || this.clientWidth;
    this.value = attributes.value || "";
    this.selectionStart = attributes.selectionStart;
    this.selectionEnd = attributes.selectionEnd;
    this.selectionDirection = attributes.selectionDirection;
  }
  get scrollWidth() {
    const bodyCanShrink = this.children.some(child => child.style.minWidth === "0" &&
      child.style.maxWidth === "100%");
    return this.style.maxWidth === "100vw" && bodyCanShrink ? this.clientWidth : this.contentWidth;
  }
  addEventListener(type, listener) { this.listeners[type] = listener; }
  append(...children) { for (const child of children) { child.parent = this; this.children.push(child); } }
  replaceChildren(...children) { this.children = []; this.append(...children); }
  contains(element) { return element === this || this.children.some(child => child.contains(element)); }
  querySelectorAll(selector) {
    if (selector === '[role="tab"]') return this.children.filter(child => child.role === "tab");
    if (selector === "[data-fleet-focus]") return this.children.filter(child => child.dataset.fleetFocus);
    return [];
  }
  querySelector(selector) {
    const id = selector.match(/data-fleet-tab="([^"]+)"/)?.[1];
    return this.children.find(child => child.dataset.fleetTab === id) || null;
  }
  closest(selector) {
    if (selector === "[data-fleet-tab]" && this.dataset.fleetTab) return this;
    if (selector === "[data-agent]" && this.dataset.agent) return this;
    return this.parent ? this.parent.closest(selector) : null;
  }
  focus() { this.ownerDocument.activeElement = this; }
  setSelectionRange(start, end, direction) {
    this.selectionStart = start; this.selectionEnd = end; this.selectionDirection = direction;
  }
  dispatch(type, fields = {}) {
    const event = { target: this, preventDefault() { this.defaultPrevented = true; }, ...fields };
    let node = this;
    while (node) { if (node.listeners[type]) node.listeners[type](event); node = node.parent; }
    return event;
  }
}

function fixture() {
  const document = { activeElement: null };
  const panel = new Element(document, { clientWidth: 320, contentWidth: 470 });
  const body = new Element(document);
  const launcher = new Element(document);
  panel.append(body);
  const opened = [];
  let selected = "activity";
  function render(id) {
    selected = id;
    body.replaceChildren(...["activity", "attention", "residents"].map(tab =>
      new Element(document, { role: "tab", dataset: { fleetTab: tab, fleetFocus: `tab:${tab}` } })));
  }
  render(selected);
  const controller = createFleetController({ panel, body, document, launcher, moveFocus,
    renderTab: render, openAgent: id => opened.push(id), close: () => { selected = null; } });
  return { document, panel, body, launcher, controller, opened, selected: () => selected };
}

test("Arrow, Home, and End select and focus the newly rendered tab element", () => {
  const dom = fixture();
  const first = dom.body.children[0];
  first.focus();
  first.dispatch("keydown", { key: "ArrowRight" });
  assert.equal(dom.selected(), "attention");
  assert.notEqual(dom.document.activeElement, first, "the replaced tab is not left focused");
  assert.equal(dom.document.activeElement.dataset.fleetTab, "attention");
  dom.document.activeElement.dispatch("keydown", { key: "End" });
  assert.equal(dom.document.activeElement.dataset.fleetTab, "residents");
  dom.document.activeElement.dispatch("keydown", { key: "Home" });
  assert.equal(dom.document.activeElement.dataset.fleetTab, "activity");
});

test("all capability-row clicks open resident detail and Escape restores launcher focus", () => {
  const dom = fixture();
  const rows = ["soul", "skills", "memory", "routes", "app-grants"].map(() =>
    new Element(dom.document, { dataset: { agent: "resident-7" } }));
  dom.body.replaceChildren(...rows);
  for (const row of rows) row.dispatch("click");
  assert.deepEqual(dom.opened, Array(5).fill("resident-7"));
  rows[0].focus();
  rows[0].dispatch("keydown", { key: "Escape" });
  assert.equal(dom.selected(), null);
  assert.equal(dom.document.activeElement, dom.launcher);
});

test("background render preserves a logical tab while replacing its DOM node", () => {
  const dom = fixture();
  const old = dom.body.children[0];
  old.focus();
  dom.controller.preserveFocus(() => dom.controller.selectTab("activity"));
  assert.notEqual(dom.document.activeElement, old);
  assert.equal(dom.document.activeElement.dataset.fleetFocus, "tab:activity");
});

test("background render preserves capability focus and search selection/caret", () => {
  const dom = fixture();
  const capability = new Element(dom.document,
    { dataset: { agent: "resident-7", fleetFocus: "cap:resident.json:memory" } });
  dom.body.replaceChildren(capability);
  capability.focus();
  dom.controller.preserveFocus(() => dom.body.replaceChildren(new Element(dom.document,
    { dataset: { agent: "resident-7", fleetFocus: "cap:resident.json:memory" } })));
  assert.equal(dom.document.activeElement.dataset.fleetFocus, "cap:resident.json:memory");

  const search = new Element(dom.document, { value: "needle", selectionStart: 1,
    selectionEnd: 4, selectionDirection: "forward", dataset: { fleetFocus: "filter:query" } });
  dom.body.replaceChildren(search);
  search.focus();
  dom.controller.preserveFocus(() => dom.body.replaceChildren(new Element(dom.document,
    { value: "needle", dataset: { fleetFocus: "filter:query" } })));
  assert.equal(dom.document.activeElement.dataset.fleetFocus, "filter:query");
  assert.deepEqual([dom.document.activeElement.selectionStart, dom.document.activeElement.selectionEnd,
    dom.document.activeElement.selectionDirection], [1, 4, "forward"]);
});
