import { afterEach, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import "@testing-library/jest-dom/vitest";
import { AgentProfile } from "./AgentProfile.jsx";
afterEach(cleanup);
it("opens an accessible profile and restores scrolling and focus on dismissal", () => {
  const opener = document.createElement("button");
  document.body.append(opener);
  opener.focus();
  const close = vi.fn();
  const { unmount } = render(
    <AgentProfile onClose={close}>
      <h2 id="agent-profile-name">Pip</h2>
      <button onClick={close}>Close</button>
    </AgentProfile>,
  );
  expect(screen.getByRole("dialog", { name: "Pip" })).toBeVisible();
  expect(document.body.style.overflow).toBe("hidden");
  fireEvent(
    screen.getByRole("dialog"),
    new Event("cancel", { bubbles: true, cancelable: true }),
  );
  expect(close).toHaveBeenCalledOnce();
  unmount();
  expect(document.body.style.overflow).toBe("");
  expect(document.activeElement).toBe(opener);
  opener.remove();
});

it("dismisses only deliberate backdrop clicks, not a text drag ending outside", () => {
  const close = vi.fn();
  render(
    <AgentProfile onClose={close}>
      <h2 id="agent-profile-name">Pip</h2>
      <p>Selectable work details</p>
    </AgentProfile>,
  );
  const dialog = screen.getByRole("dialog");
  vi.spyOn(dialog, "getBoundingClientRect").mockReturnValue({
    left: 100,
    right: 600,
    top: 100,
    bottom: 600,
  });
  fireEvent.pointerDown(screen.getByText("Selectable work details"), {
    clientX: 150,
    clientY: 150,
  });
  fireEvent.click(dialog, { clientX: 50, clientY: 50 });
  expect(close).not.toHaveBeenCalled();
  fireEvent.pointerDown(dialog, { clientX: 50, clientY: 50 });
  fireEvent.click(dialog, { clientX: 50, clientY: 50 });
  expect(close).toHaveBeenCalledOnce();
});

it("uses the latest close callback and restores preexisting body overflow", () => {
  document.body.style.overflow = "clip";
  const first = vi.fn(),
    second = vi.fn();
  const view = render(
    <AgentProfile onClose={first}>
      <h2 id="agent-profile-name">Pip</h2>
    </AgentProfile>,
  );
  view.rerender(
    <AgentProfile onClose={second}>
      <h2 id="agent-profile-name">Hob</h2>
    </AgentProfile>,
  );
  fireEvent(
    screen.getByRole("dialog"),
    new Event("cancel", { bubbles: true, cancelable: true }),
  );
  expect(first).not.toHaveBeenCalled();
  expect(second).toHaveBeenCalledOnce();
  view.unmount();
  expect(document.body.style.overflow).toBe("clip");
  document.body.style.overflow = "";
});

it("dismisses before following a link to the village records", () => {
  const close = vi.fn();
  render(
    <AgentProfile onClose={close}>
      <h2 id="agent-profile-name">Pip</h2>
      <a href="#records">See all records</a>
    </AgentProfile>,
  );
  fireEvent.click(screen.getByRole("link", { name: "See all records" }));
  expect(close).toHaveBeenCalledOnce();
});
