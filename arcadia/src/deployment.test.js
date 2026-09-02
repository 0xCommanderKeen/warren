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

  it("serves the bundle with real MIME types, and the smoke test proves it", () => {
    // Without the include, nginx knows html/gif/jpeg and nothing else: every module
    // script goes out as text/plain, the browser refuses it, and both SPAs render a blank
    // page while every curl-based check stays green (2026-09-02, found by Miha).
    expect(nginx).toContain("include /etc/nginx/mime.types;");
    expect(nginx).toContain("default_type application/octet-stream;");
    const smoke = readFileSync("deploy/smoke.sh", "utf8");
    expect(smoke).toContain("module-type=javascript");
  });

  it("keeps Chronicle ingest and Steward's API behind the deployed origin", () => {
    expect(nginx).toContain("location = /events");
    expect(nginx).toContain("host.docker.internal:8738");
    expect(nginx).toContain("host.docker.internal:8802");
    expect(readFileSync("deploy/smoke.sh", "utf8")).toContain("steward-preflight=401");
  });
});

/* -- one origin, two services, no path claimed twice (#242) ---------------------------- */

/**
 * Steward's own top-level route segments, read off the `@app` decorators that declare them.
 *
 * Derived rather than listed, because the regression this guards against is Steward growing
 * a route and the origin not learning it — which is exactly how `/tasks/{id}/lineage` and
 * `POST /delegate` came to answer with the village's index.html (#242). A hand-kept copy
 * here could not see the new route, so it could not fail. `.github/workflows/arcadia.yml`
 * lists `api.py` among this suite's paths so the change that breaks this runs it.
 *
 * FastAPI is read as text on purpose — the alternative is booting Steward from a JS suite.
 * Every route in that file is an `@app` decorator today; a router mounted elsewhere would
 * be invisible here, so add it to the file or to this reader.
 */
const stewardApiRoutes = () => {
  const api = readFileSync("../steward/src/steward/api.py", "utf8");
  const declared = [...api.matchAll(/@app\.(?:get|post|put|patch|delete)\("(\/[^"]*)"/g)];
  expect(declared.length, "no @app routes found — the reader has gone stale").toBeGreaterThan(0);
  return [...new Set(declared.map((match) => match[1].split("/")[1]))].sort();
};

describe("the origin's route table matches the services behind it", () => {
  /** The alternation inside the one regex `location` that fronts Steward. */
  const stewardRoutes = () => {
    // One block fronts Steward, so one alternation is the whole answer. Split a path into a
    // `location` of its own and this reader stops being true — fail here rather than pass a
    // config it can no longer see all of.
    expect([...nginx.matchAll(/host\.docker\.internal:8802/g)]).toHaveLength(1);
    const match = nginx.match(/location ~ \^\/\(([^)]*)\)\(\/\|\$\)/);
    expect(match, "no Steward regex location to read").not.toBeNull();
    return [...new Set(match[1].split("|"))].sort();
  };

  it("proxies every top-level Steward route", () => {
    // An unproxied route does not 404: it falls through to the SPA and answers 200 with
    // index.html, so a Townhall page built on it looks alive and confirms nothing.
    expect(stewardRoutes()).toEqual(stewardApiRoutes());
    // The two #242 was filed for. Named as well as derived, so the fix cannot quietly
    // regress into a set that merely agrees with a Steward that lost them too.
    expect(stewardRoutes()).toEqual(expect.arrayContaining(["tasks", "delegate"]));
  });

  it("leaves /residents unambiguously Steward's, and gives Chronicle's report its own path", () => {
    // Both services answer `GET /residents` — Steward's is the fleet's resident listing,
    // Chronicle's is the manifest-validation report its runbook checks after a deploy. The
    // regex hands the bare path to Steward, so Chronicle's answers under the `/burrow/`
    // prefix that already fronts its state routes.
    expect(stewardRoutes()).toContain("residents");
    expect(nginx).toContain("location = /burrow/residents");
    expect(nginx).toContain("proxy_pass http://host.docker.internal:8738/residents;");
    // Exact matches take one spelling each; the other one must not reach the SPA.
    expect(nginx).toContain("location = /burrow/residents/");
    expect(nginx).toContain("return 301 /burrow/residents;");
  });

  it("smoke-tests both gaps against a running origin", () => {
    const smoke = readFileSync("deploy/smoke.sh", "utf8");
    expect(smoke).toContain("$origin/burrow/residents");
    // 401 is Steward answering; the SPA fallback would be a 200 carrying index.html.
    expect(smoke).toContain("lineage-preflight=401");
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
