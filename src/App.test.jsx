import "@testing-library/jest-dom/vitest";
import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { App } from "./App.jsx";

vi.mock("./game/PhaserGame.jsx", () => ({
  PhaserGame: () => <div data-testid="village-canvas" />,
}));

describe("Arcadia", () => {
  it("visibly rejects an unsupported snapshot before rendering villagers", () => {
    render(
      <App
        envelope={{
          kind: "snapshot",
          snapshot: {
            schema_version: 2,
            villagers: [{ id: "future:villager", name: "Future Villager" }],
          },
        }}
      />,
    );

    expect(screen.getByRole("alert")).toHaveTextContent(
      "Unsupported village schema version: 2",
    );
    expect(screen.queryByText("Future Villager")).not.toBeInTheDocument();
    expect(screen.queryByTestId("village-canvas")).not.toBeInTheDocument();
  });
});
