// @vitest-environment node
import { createServer as createHttpServer } from "node:http";
import { createServer as createViteServer } from "vite";
import { expect, it } from "vitest";

it("forwards approval POSTs and credentials to the configured Steward", async () => {
  const received = [];
  const upstream = createHttpServer(async (request, response) => {
    let body = "";
    for await (const chunk of request) body += chunk;
    received.push({ url: request.url, method: request.method, token: request.headers.authorization, body });
    response.writeHead(202, { "Content-Type": "application/json" });
    response.end(JSON.stringify({ status: "recorded", approval_request_id: "test-request" }));
  });
  await new Promise(resolve => upstream.listen(0, "127.0.0.1", resolve));
  const previous = process.env.STEWARD_URL;
  process.env.STEWARD_URL = `http://127.0.0.1:${upstream.address().port}`;
  let vite;
  try {
    vite = await createViteServer({ server: { host: "127.0.0.1", port: 0, hmr: false } });
    await vite.listen();
    const response = await fetch(`http://127.0.0.1:${vite.httpServer.address().port}/approvals/test-request`, {
      method: "POST",
      headers: { Authorization: "Bearer test-only", "Content-Type": "application/json" },
      body: JSON.stringify({ decision: "approve" }),
    });
    expect(response.status).toBe(202);
    expect(await response.json()).toEqual({ status: "recorded", approval_request_id: "test-request" });
    expect(received).toEqual([{ url: "/approvals/test-request", method: "POST", token: "Bearer test-only", body: '{"decision":"approve"}' }]);
  } finally {
    if (previous === undefined) delete process.env.STEWARD_URL;
    else process.env.STEWARD_URL = previous;
    await vite?.close();
    await new Promise(resolve => upstream.close(resolve));
  }
});
