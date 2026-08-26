"use strict";

/* One process serves every Python→viewer parity check in test_rotation.py. */
const readline = require("node:readline");
const realFs = require("node:fs");
const path = require("node:path");
const { createRequire } = require("node:module");
const rootRequire = createRequire(path.join(process.cwd(), "inline-parity.js"));

const input = readline.createInterface({ input: process.stdin, crlfDelay: Infinity });

async function evaluate(request) {
  for (const loaded of Object.keys(require.cache)) {
    if (loaded.includes(`${path.sep}viewer${path.sep}`)) delete require.cache[loaded];
  }
  let output = "";
  let wrote;
  const written = new Promise(resolve => { wrote = resolve; });
  const fakeFs = { ...realFs,
    readFileSync(path, options) {
      if (path === 0) {
        return typeof options === "string" || options && options.encoding
          ? request.input : Buffer.from(request.input);
      }
      return realFs.readFileSync(path, options);
    },
  };
  const localRequire = name => name === "node:fs" || name === "fs" ? fakeFs : rootRequire(name);
  const fakeStdout = { write(value) { output += String(value); wrote(); return true; } };
  const fakeProcess = new Proxy(process, {
    get(target, property) {
      return property === "stdout" ? fakeStdout : Reflect.get(target, property);
    },
  });
  try {
    const returned = Function("require", "process", request.script)(localRequire, fakeProcess);
    if (returned && typeof returned.then === "function") await returned;
    if (!output) await Promise.race([
      written,
      new Promise((_, reject) => setTimeout(() => reject(new Error("script produced no output")), 30000)),
    ]);
    return { returncode: 0, stdout: output, stderr: "" };
  } catch (error) {
    return { returncode: 1, stdout: output, stderr: String(error && error.stack || error) };
  }
}

(async () => {
  for await (const line of input) {
    const response = await evaluate(JSON.parse(line));
    process.stdout.write(JSON.stringify(response) + "\n");
  }
})();
