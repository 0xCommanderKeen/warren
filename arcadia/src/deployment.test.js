import { readFileSync } from "node:fs";
import { describe, expect, it } from "vitest";

const nginx = readFileSync("deploy/nginx.conf", "utf8");
const compose = readFileSync("deploy/compose.yaml", "utf8");

describe("production deployment contract", () => {
  it("serves Arcadia with a browser-route fallback", () => {
    expect(nginx).toMatch(/location \/ \{/);
    expect(nginx).toContain("try_files $uri $uri/ /index.html");
    expect(compose).toContain('"8737:8737"');
  });

  it("proxies the prefixed state stream without buffering or losing queries", () => {
    expect(nginx).toContain("location = /burrow/state/stream");
    expect(nginx).toContain("proxy_pass http://host.docker.internal:8738/state/stream;");
    expect(nginx).toContain("proxy_buffering off");
    expect(nginx).toContain("proxy_cache off");
    expect(nginx).toContain("proxy_read_timeout 1h");
    expect(nginx).toContain("X-Accel-Buffering no");
    expect(nginx).toContain("rewrite ^ /burrow/state/stream last");
  });

  it("keeps Burrow ingest and Steward writes behind the deployed origin", () => {
    expect(nginx).toContain("location = /events");
    expect(nginx).toContain("location ~ ^/(jobs|approvals|residents|skills|reload|routines|requests)(/|$)");
    expect(nginx).toContain("host.docker.internal:8738");
    expect(nginx).toContain("host.docker.internal:8802");
    expect(readFileSync("deploy/smoke.sh", "utf8")).toContain("steward-preflight=401");
  });
});

/* -- the origin the shipped bundle talks to (#256) ------------------------------------- */

const client = readFileSync("src/steward/StewardClient.js", "utf8");
const main = readFileSync("src/main.jsx", "utf8");

/** The body of a top-level `function name(…) {…}`, up to the closing brace in column 0. */
const functionBody = (source, name) => {
  const start = source.indexOf(`function ${name}(`);
  expect(start, `no function ${name} to read`).toBeGreaterThan(-1);
  return source.slice(start, source.indexOf("\n}", start));
};

describe("the shipped bundle honours no origin override", () => {
  // warren#256, sibling of #241. The nginx contract above is the whole reason this holds:
  // Steward's write routes are proxied behind the deployed origin, so a shipped Arcadia has
  // no honest use for a Steward URL of its own — and every dishonest use is a link handing
  // the operator's bearer token to somebody else's server.

  it("resolves Steward's base in one dev-guarded place", () => {
    const body = functionBody(client, "stewardBaseFromLocation");

    expect(body).toContain("import.meta.env.DEV");
    expect(body.indexOf("import.meta.env.DEV")).toBeLessThan(body.indexOf('"steward"'));
    expect(body).toContain('get("steward")');
    // The build-time variable is gated by the same branch, not read beside it.
    expect(body).toContain("VITE_STEWARD_URL");
    expect(body.indexOf("import.meta.env.DEV")).toBeLessThan(body.indexOf("VITE_STEWARD_URL"));
  });

  it("names the override nowhere else — main.jsx reads no URL of its own", () => {
    expect(main).not.toContain("URLSearchParams");
    expect(main).not.toContain("location.search");
    expect(main).not.toContain("VITE_STEWARD_URL");
    expect(main).toContain("stewardBaseFromLocation");

    // One read site each — prose about them is welcome, a second read of them is not.
    expect([...client.matchAll(/get\("steward"\)/g)]).toHaveLength(1);
    expect([...client.matchAll(/import\.meta\.env\.VITE_STEWARD_URL/g)]).toHaveLength(1);
  });

  it("refuses the credential to any base that is not this origin", () => {
    // Defence in depth: even if a base reached the client from somewhere these files cannot
    // see, the bearer header does not follow it off-origin.
    expect(client).toContain("isSameOrigin");
    expect(client).toContain("cross_origin_base");
    expect(functionBody(client, "createStewardClient")).toContain("import.meta.env.DEV");
  });
});
