import React from "react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import "@testing-library/jest-dom/vitest";
import { VillageLayoutEditor } from "./VillageLayoutEditor.jsx";

afterEach(cleanup);
const world = { buildings: [{ id: "home:hob", name: "Hob’s home", kind: "home", position: [0, 0] }, { id: "workshop", name: "Workshop", kind: "workshop", position: [10, 0] }], editLimit: 40, canUndoLayoutMove: true };
describe("village layout editor", () => {
  it("moves from keyboard controls, reports occupied errors and selects map buildings", () => {
    const onMoveBuilding = vi.fn().mockReturnValueOnce({ ok: false, error: "That plot is occupied." }).mockReturnValue({ ok: true, changed: true });
    render(<VillageLayoutEditor world={world} onMoveBuilding={onMoveBuilding} />);
    fireEvent.click(screen.getByRole("button", { name: "Move east" }));
    expect(onMoveBuilding).toHaveBeenCalledWith("home:hob", [10, 0]);
    expect(screen.getByRole("status")).toHaveTextContent("occupied");
    fireEvent.click(screen.getByRole("button", { name: "Workshop at 10, 0" }));
    fireEvent.click(screen.getByRole("button", { name: "Move north" }));
    expect(onMoveBuilding).toHaveBeenLastCalledWith("workshop", [10, -10]);
    expect(screen.getByRole("status")).toHaveTextContent("Workshop moved");
  });
  it("offers undo/reset and an accessible empty plot action", () => {
    const onMoveBuilding = vi.fn().mockReturnValue({ ok: true, changed: true });
    const onUndoMove = vi.fn().mockReturnValue({ ok: true });
    const onReset = vi.fn().mockReturnValue({ ok: true });
    render(<VillageLayoutEditor world={world} onMoveBuilding={onMoveBuilding} onUndoMove={onUndoMove} onReset={onReset} />);
    fireEvent.click(screen.getByRole("button", { name: "Move Hob’s home to -10, -10" }));
    expect(onMoveBuilding).toHaveBeenCalledWith("home:hob", [-10, -10]);
    fireEvent.click(screen.getByRole("button", { name: "Undo last move" }));
    expect(onUndoMove).toHaveBeenCalledOnce();
    fireEvent.click(screen.getByRole("button", { name: "Reset layout" }));
    expect(onReset).toHaveBeenCalledOnce();
  });
});
