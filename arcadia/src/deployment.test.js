import { readFileSync, readdirSync } from "node:fs";
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
    expect(nginx).toContain("location = /chronicle/state/stream");
    expect(nginx).toContain("proxy_pass http://host.docker.internal:8738/state/stream;");
    expect(nginx).toContain("proxy_buffering off");
    expect(nginx).toContain("proxy_cache off");
    expect(nginx).toContain("proxy_read_timeout 1h");
    expect(nginx).toContain("X-Accel-Buffering no");
    expect(nginx).toContain("rewrite ^ /chronicle/state/stream last");
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
  const api = [
    readFileSync("../steward/src/steward/api.py", "utf8"),
    ...readdirSync("../steward/src/steward/routes")
      .filter((name) => name.endsWith(".py"))
      .map((name) => readFileSync(`../steward/src/steward/routes/${name}`, "utf8")),
  ].join("\n");
  const declared = [...api.matchAll(/@app\.(?:get|post|put|patch|delete)\("(\/[^"]*)"/g)];
  declared.push(...api.matchAll(/@routes\.(?:get|post|put|patch|delete)\("(\/[^\"]*)"/g));
  expect(declared.length, "no API routes found — the reader has gone stale").toBeGreaterThan(0);
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
    // regex hands the bare path to Steward, so Chronicle's answers under the `/chronicle/`
    // prefix that already fronts its state routes.
    expect(stewardRoutes()).toContain("residents");
    expect(nginx).toContain("location = /chronicle/residents");
    expect(nginx).toContain("proxy_pass http://host.docker.internal:8738/residents;");
    // Exact matches take one spelling each; the other one must not reach the SPA.
    expect(nginx).toContain("location = /chronicle/residents/");
    expect(nginx).toContain("return 301 /chronicle/residents;");
  });

  it("carries no /burrow/ route at all", () => {
    // The prefix moved to /chronicle/ in warren#361 and its redirect was retired once
    // nothing asked for it. Asserted rather than left unsaid, because an unclaimed path
    // on this origin does not 404 — `location /` answers 200 with the SPA's index.html
    // (warren#242) — so a route that came back by accident would look like it worked.
    expect(nginx).not.toContain("/burrow/");
    // Relative Location headers. nginx's default absolute one is built from the `listen`
    // port, which is a guess about how the client reached this origin — wrong the moment
    // the config is run behind a published port that is not 8737.
    expect(nginx).toContain("absolute_redirect off;");
  });

  it("smoke-tests both gaps against a running origin", () => {
    const smoke = readFileSync("deploy/smoke.sh", "utf8");
    expect(smoke).toContain("$origin/chronicle/residents");
    // 401 is Steward answering; the SPA fallback would be a 200 carrying index.html.
    expect(smoke).toContain("lineage-preflight=401");
  });
});
