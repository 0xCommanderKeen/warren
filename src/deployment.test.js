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
    expect(nginx).toContain("location ~ ^/(jobs|approvals|residents)(/|$)");
    expect(nginx).toContain("host.docker.internal:8738");
    expect(nginx).toContain("host.docker.internal:8802");
    expect(readFileSync("deploy/smoke.sh", "utf8")).toContain("steward-preflight=401");
  });
});
