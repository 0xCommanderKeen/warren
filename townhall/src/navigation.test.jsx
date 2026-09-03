import { act, cleanup, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import App from "./App.jsx";

beforeEach(() => window.history.replaceState({}, "", "/observatory/"));
afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

describe("browser navigation at a mounted build", () => {
  it("renders not found and activates no section after leaving the mount", () => {
    vi.spyOn(window, "fetch").mockResolvedValue({ status: 503, json: async () => ({}) });
    render(<App base="/observatory/" />);

    expect(screen.getByRole("link", { name: /fleet/i }).getAttribute("aria-current")).toBe("page");

    act(() => {
      window.history.pushState({}, "", "/chronicle/state");
      window.dispatchEvent(new PopStateEvent("popstate"));
    });

    expect(screen.getByText("No such page")).toBeTruthy();
    expect(screen.queryByRole("link", { current: "page" })).toBeNull();
  });
});
