/* Which origin townhall believes holds steward.
 *
 * One question, and the answer that matters is the boring one: this origin. warren#241 —
 * `?steward=` used to be honoured by the deployed bundle, so a crafted link could point an
 * unlocked tab's bearer token at somebody else's server.
 */

import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { StewardProvider, useSteward } from "./context.jsx";

function memoryStorage() {
  const map = new Map();
  return {
    getItem: (key) => (map.has(key) ? map.get(key) : null),
    setItem: (key, value) => map.set(key, String(value)),
    removeItem: (key) => map.delete(key),
  };
}

/** Renders the base the provider settled on, brackets so an empty one is still visible. */
function Base() {
  const { baseUrl } = useSteward();
  return <span data-testid="base">{`[${baseUrl}]`}</span>;
}

const baseUnder = (search) => {
  window.history.replaceState({}, "", search);
  render(
    <StewardProvider storage={memoryStorage()} fetch={vi.fn()}>
      <Base />
    </StewardProvider>,
  );
  return screen.getByTestId("base").textContent.slice(1, -1);
};

beforeEach(() => window.history.replaceState({}, "", "/"));
afterEach(() => {
  cleanup();
  vi.unstubAllEnvs();
});

describe("where steward lives", () => {
  it("is this origin when nobody says otherwise", () => {
    expect(baseUnder("/observatory/skills")).toBe("");
  });

  it("is still this origin under ?steward= in anything that ships", () => {
    // The exfiltration link, defused: in a built bundle `import.meta.env.DEV` is false and
    // this branch is not in the file at all, so the parameter is inert text in a URL.
    vi.stubEnv("DEV", false);
    expect(baseUnder("/observatory/?steward=https://evil.tld")).toBe("");
  });

  it("is whatever a developer running vite points it at", () => {
    vi.stubEnv("DEV", true);
    expect(baseUnder("/?steward=http://127.0.0.1:8801")).toBe("http://127.0.0.1:8801");
  });
});
