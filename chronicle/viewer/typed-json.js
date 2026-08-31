"use strict";

/* A flat typed JSON graph used wherever JavaScript and Python must agree on
 * exact wire identity. Flat nodes make valid deeply nested approval detail a
 * data-size concern, never a language recursion-limit concern. */
(function (root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  else root.BurrowTypedJSON = api;
})(typeof globalThis === "object" ? globalThis : this, function () {
  function exactDenseArray(value) {
    if (!Array.isArray(value) || Object.getPrototypeOf(value) !== Array.prototype ||
        Object.getOwnPropertySymbols(value).length) return false;
    const names = Object.getOwnPropertyNames(value);
    if (names.length !== value.length + 1 || names.at(-1) !== "length") return false;
    for (let index = 0; index < value.length; index++) {
      if (names[index] !== String(index)) return false;
      const descriptor = Object.getOwnPropertyDescriptor(value, names[index]);
      if (!descriptor || !Object.hasOwn(descriptor, "value") || !descriptor.enumerable ||
          !descriptor.configurable || !descriptor.writable) return false;
    }
    const length = Object.getOwnPropertyDescriptor(value, "length");
    return Boolean(length && length.value === value.length && length.writable && !length.enumerable &&
      !length.configurable);
  }
  /* Direct-object callers do not get to smuggle behavior or hidden state into
   * the JSON domain. JSON.parse produces exactly this shape: an ordinary/null
   * prototype object whose own string names are all enumerable data fields. */
  function exactPlainObject(value) {
    if (value === null || typeof value !== "object" || Array.isArray(value) ||
        Object.getOwnPropertySymbols(value).length) return false;
    const prototype = Object.getPrototypeOf(value);
    if (prototype !== Object.prototype && prototype !== null) return false;
    const names = Object.getOwnPropertyNames(value);
    const keys = Object.keys(value);
    if (names.length !== keys.length || names.some((name, index) => name !== keys[index])) return false;
    return names.every(name => {
      const descriptor = Object.getOwnPropertyDescriptor(value, name);
      return Boolean(descriptor && descriptor.enumerable && Object.hasOwn(descriptor, "value"));
    });
  }
  function stringToken(value) {
    let encoded = '"';
    for (let index = 0; index < value.length; index++) {
      const code = value.charCodeAt(index);
      if (code === 0x22) encoded += '\\"';
      else if (code === 0x5c) encoded += "\\\\";
      else if (code === 0x08) encoded += "\\b";
      else if (code === 0x09) encoded += "\\t";
      else if (code === 0x0a) encoded += "\\n";
      else if (code === 0x0c) encoded += "\\f";
      else if (code === 0x0d) encoded += "\\r";
      else if (code >= 0x20 && code <= 0x7e) encoded += value[index];
      else encoded += "\\u" + code.toString(16).padStart(4, "0");
    }
    return encoded + '"';
  }

  function numberBits(value) {
    if (!Number.isFinite(value)) return "nonfinite";
    if (value === 0) return "0000000000000000";
    const bytes = new Uint8Array(8);
    new DataView(bytes.buffer).setFloat64(0, value, false);
    return [...bytes].map(byte => byte.toString(16).padStart(2, "0")).join("");
  }

  function typedGraph(value) {
    const nodes = [], seen = new Set(), active = new Set(), root = { index: null };
    const stack = [{ value, target: root, key: "index", exit: false }];
    while (stack.length) {
      const frame = stack.pop(), item = frame.value;
      const array = Array.isArray(item);
      const object = item !== null && typeof item === "object" && !array;
      if (!array && !object) {
        let node;
        if (item === null) node = ["n"];
        else if (typeof item === "boolean") node = ["b", item ? 1 : 0];
        else if (typeof item === "number") node = ["f", numberBits(item)];
        else if (typeof item === "string") node = ["s", item];
        else node = ["x"];
        nodes.push(node); frame.target[frame.key] = nodes.length - 1; continue;
      }
      if (frame.exit) {
        active.delete(item);
        let node;
        if (array) node = ["a", frame.children];
        else node = ["o", frame.keys.map((key, index) => [key, frame.children[index]])];
        nodes.push(node); frame.target[frame.key] = nodes.length - 1; continue;
      }
      // JSON wire values are trees. A repeated container identity is therefore
      // a hostile direct-object graph even when it is not an active cycle. If
      // aliases were expanded independently, a tiny DAG could amplify before
      // the encoded capsule byte ceiling is reached.
      if (seen.has(item)) throw new TypeError(active.has(item) ?
        "cyclic JSON value" : "aliased JSON value");
      seen.add(item);
      if ((!array && !exactPlainObject(item)) || (array && !exactDenseArray(item))) {
        nodes.push(["x"]); frame.target[frame.key] = nodes.length - 1; continue;
      }
      const keys = array ? null : Object.keys(item).sort((left, right) => {
        const a = stringToken(left), b = stringToken(right);
        return a < b ? -1 : a > b ? 1 : 0;
      });
      let children;
      if (array) {
        children = new Array(item.length);
        // This is a trust boundary. Do not consult an input-owned/inherited
        // iterator even after proving the ordinary dense-array shape.
        for (let index = 0; index < item.length; index++) children[index] = item[index];
      } else children = keys.map(key => item[key]);
      const childIndexes = new Array(children.length);
      active.add(item);
      stack.push({ ...frame, exit: true, keys, children: childIndexes });
      for (let index = children.length - 1; index >= 0; index--) {
        stack.push({ value: children[index], target: childIndexes, key: index, exit: false });
      }
    }
    return [nodes, root.index];
  }

  function graphString(graph) {
    const nodes = graph[0], root = graph[1];
    const encoded = new Array(nodes.length);
    for (let position = 0; position < nodes.length; position++) {
      const node = nodes[position];
      const tag = node[0];
      if (tag === "n" || tag === "x") encoded[position] = `["${tag}"]`;
      else if (tag === "b") encoded[position] = `["b",${node[1]}]`;
      else if (tag === "f" || tag === "s") encoded[position] =
        `["${tag}",${stringToken(node[1])}]`;
      else if (tag === "a") encoded[position] = `["a",[${node[1].join(",")}]]`;
      else {
        const entries = new Array(node[1].length);
        for (let index = 0; index < node[1].length; index++) {
          entries[index] = `[${stringToken(node[1][index][0])},${node[1][index][1]}]`;
        }
        encoded[position] = `["o",[${entries.join(",")}]]`;
      }
    }
    return `[[${encoded.join(",")}],${root}]`;
  }

  function identity(value) { return graphString(typedGraph(value)); }

  function decodeGraph(graph) {
    if (!exactDenseArray(graph) || graph.length !== 2 || !exactDenseArray(graph[0]) ||
        !Number.isSafeInteger(graph[1])) throw new TypeError("invalid typed JSON graph");
    const values = [], references = new Array(graph[0].length).fill(0);
    for (let position = 0; position < graph[0].length; position++) {
      const node = graph[0][position];
      if (!exactDenseArray(node) || typeof node[0] !== "string") throw new TypeError("invalid typed JSON node");
      const tag = node[0]; let value;
      if (tag === "n" && node.length === 1) value = null;
      else if (tag === "b" && node.length === 2 && (node[1] === 0 || node[1] === 1)) value = Boolean(node[1]);
      else if (tag === "s" && node.length === 2 && typeof node[1] === "string") value = node[1];
      else if (tag === "f" && node.length === 2 && typeof node[1] === "string") {
        if (node[1] === "nonfinite") value = Infinity;
        else {
          if (!/^[0-9a-f]{16}$/.test(node[1])) throw new TypeError("invalid binary64 token");
          const bytes = Uint8Array.from(node[1].match(/../g), byte => Number.parseInt(byte, 16));
          value = new DataView(bytes.buffer).getFloat64(0, false);
          if (!Number.isFinite(value) || (value === 0 && node[1] !== "0000000000000000")) {
            throw new TypeError("noncanonical binary64 token");
          }
        }
      } else if (tag === "a" && node.length === 2 && exactDenseArray(node[1])) {
        value = new Array(node[1].length);
        for (let index = 0; index < node[1].length; index++) {
          const reference = node[1][index];
          if (!Number.isSafeInteger(reference) || reference < 0 || reference >= position) {
            throw new TypeError("invalid typed JSON array reference");
          }
          references[reference]++;
          value[index] = values[reference];
        }
      } else if (tag === "o" && node.length === 2 && exactDenseArray(node[1])) {
        value = {};
        let prior = null;
        for (let index = 0; index < node[1].length; index++) {
          const entry = node[1][index];
          if (!exactDenseArray(entry) || entry.length !== 2 || typeof entry[0] !== "string" ||
              !Number.isSafeInteger(entry[1]) || entry[1] < 0 || entry[1] >= position ||
              (prior !== null && TYPED_COMPARE(prior, entry[0]) >= 0)) {
            throw new TypeError("invalid typed JSON object entry");
          }
          references[entry[1]]++;
          Object.defineProperty(value, entry[0], { value: values[entry[1]], enumerable: true,
            configurable: true, writable: true });
          prior = entry[0];
        }
      } else throw new TypeError("invalid typed JSON node");
      values.push(value);
    }
    if (graph[1] < 0 || graph[1] >= values.length || graph[1] !== values.length - 1) {
      throw new TypeError("invalid typed JSON root");
    }
    if (references.some((count, index) =>
      index === graph[1] ? count !== 0 : count !== 1)) {
      throw new TypeError("noncanonical typed JSON tree");
    }
    const value = values[graph[1]];
    if (graphString(typedGraph(value)) !== graphString(graph)) {
      throw new TypeError("noncanonical typed JSON graph");
    }
    return value;
  }

  const TYPED_COMPARE = (left, right) => {
    const a = stringToken(left), b = stringToken(right);
    return a < b ? -1 : a > b ? 1 : 0;
  };

  return { exactDenseArray, exactPlainObject, stringToken, numberBits, typedGraph,
    graphString, identity, decodeGraph };
});
