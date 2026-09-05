import "@testing-library/jest-dom/vitest";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { VillageNavigator } from "./VillageNavigator.jsx";
import { createVillageLayout } from "../world/layout.js";
import fixture from "../contract/fixtures/complete-v1.js";
afterEach(cleanup);
const world = () => createVillageLayout().update(fixture.snapshot);
describe("village navigator", () => {
  it("offers direct building shortcuts and only shows the overview map when zoomed", () => {
    const onSelect = vi.fn(),
      onOverview = vi.fn();
    const view = render(
      <VillageNavigator
        world={world()}
        camera={{ zoom: 1 }}
        onSelect={onSelect}
        onOverview={onOverview}
      />,
    );
    expect(
      screen.queryByRole("group", { name: "Village overview map" }),
    ).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "Go to Workshop" }));
    expect(onSelect).toHaveBeenCalledWith({ kind: "building", id: "workshop" });
    fireEvent.click(screen.getByRole("button", { name: "Show village overview" }));
    expect(onOverview).toHaveBeenCalledOnce();
    view.rerender(
      <VillageNavigator
        world={world()}
        camera={{ zoom: 2, target: [0, 0, 0] }}
        onSelect={onSelect}
        onOverview={onOverview}
      />,
    );
    expect(
      screen.getByRole("group", { name: "Village overview map" }),
    ).toBeVisible();
  });
  it("uses actual building geometry and supports keyboard map navigation", () => {
    const data = world(),
      onSelect = vi.fn();
    const selected = data.buildings.find(
      (building) => building.kind === "home",
    );
    render(
      <VillageNavigator
        world={data}
        selection={{ kind: "building", id: selected.id }}
        visible
        onSelect={onSelect}
        onOverview={() => {}}
      />,
    );
    const button = screen.getByRole("button", {
      name: `Locate ${selected.name} on map`,
    });
    expect(button).toHaveAttribute("aria-pressed", "true");
    expect(button.querySelector("rect")).toHaveAttribute(
      "x",
      String(selected.position[0] - selected.width / 2),
    );
    fireEvent.keyDown(button, { key: "Enter" });
    expect(onSelect).toHaveBeenCalledWith({
      kind: "building",
      id: selected.id,
    });
    fireEvent.keyDown(button, { key: " " });
    expect(onSelect).toHaveBeenCalledTimes(2);
  });
  it("handles modified shortcuts without taking keystrokes from editors", () => {
    const onSelect = vi.fn(),
      onOverview = vi.fn();
    render(
      <>
        <VillageNavigator
          world={world()}
          onSelect={onSelect}
          onOverview={onOverview}
        />
        <input aria-label="Search" />
        <div
          contentEditable
          suppressContentEditableWarning
          data-testid="editor"
        >
          Write here
        </div>
      </>,
    );
    fireEvent.keyDown(window, { key: "1" });
    expect(onSelect).not.toHaveBeenCalled();
    fireEvent.keyDown(window, { key: "1", code: "Digit1", altKey: true });
    expect(onSelect).toHaveBeenCalledExactlyOnceWith({
      kind: "building",
      id: "workshop",
    });
    fireEvent.keyDown(screen.getByRole("textbox", { name: "Search" }), {
      key: "2",
      code: "Digit2",
      altKey: true,
    });
    fireEvent.keyDown(screen.getByTestId("editor"), {
      key: "3",
      code: "Digit3",
      altKey: true,
    });
    fireEvent.keyDown(window, {
      key: "3",
      code: "Digit3",
      altKey: true,
      ctrlKey: true,
    });
    expect(onSelect).toHaveBeenCalledTimes(1);
    fireEvent.keyDown(window, { key: "0", code: "Digit0", altKey: true });
    expect(onOverview).toHaveBeenCalledOnce();
  });
  it("honors the parent visibility override inside a room", () => {
    render(
      <VillageNavigator
        world={world()}
        roomId="workshop"
        camera={{ zoom: 3 }}
        visible={false}
        onSelect={() => {}}
        onOverview={() => {}}
      />,
    );
    expect(
      screen.queryByRole("group", { name: "Village overview map" }),
    ).toBeNull();
    expect(
      screen.getByRole("button", { name: "Go to Workshop" }),
    ).toBeVisible();
  });
});
