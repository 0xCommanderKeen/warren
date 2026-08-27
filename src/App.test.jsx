import "@testing-library/jest-dom/vitest";
import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { App } from "./App.jsx";
import fixture from "./contract/fixtures/complete-v1.json";

vi.mock("./game/PhaserGame.jsx", () => ({
  PhaserGame: () => <div data-testid="village-canvas" />,
}));

afterEach(cleanup);

describe("Arcadia", () => {
  it("renders every read-only panel directly from the contract fixture", () => {
    render(<App envelope={fixture} />);

    expect(screen.getByRole("region", { name: "Notice board" })).toHaveTextContent(
      /report\.mdKeeper · burrow/,
    );
    expect(screen.getByRole("region", { name: "Job board" })).toHaveTextContent(
      /Freeze contractclaimed · pythonKeeper/,
    );
    expect(screen.getByRole("region", { name: "Routine ledger" })).toHaveTextContent(
      /dailyfinished · ok · 300sreport\.md · Keeper/,
    );
    expect(screen.getByRole("region", { name: "Charter journal" })).toHaveTextContent(
      /KeeperkeeperMaintains Burrow\./,
    );
    expect(screen.getByRole("region", { name: "Journal observations" })).toHaveTextContent(
      /Keeper2026-08-27 · dailyjournals\/2026-08-27\.md/,
    );
  });

  it("does not turn villager history into panel state", () => {
    const envelope = structuredClone(fixture);
    envelope.snapshot.artifacts = [];
    envelope.snapshot.tasks = [];
    envelope.snapshot.routines = [];
    envelope.snapshot.journals = [];
    envelope.snapshot.villagers[0].history = [
      {
        type: "task_done",
        payload: { title: "History must remain invisible", artifact: "ghost.md" },
      },
    ];

    render(<App envelope={envelope} />);

    expect(screen.queryByText(/History must remain invisible|ghost\.md/)).not.toBeInTheDocument();
    expect(screen.getByRole("region", { name: "Notice board" })).toHaveTextContent(
      "Nothing has been produced yet.",
    );
    expect(screen.getByRole("region", { name: "Job board" })).toHaveTextContent(
      "There are no jobs in the queue.",
    );
    expect(screen.getByRole("region", { name: "Routine ledger" })).toHaveTextContent(
      "No routine runs have been observed.",
    );
    expect(screen.getByRole("region", { name: "Journal observations" })).toHaveTextContent(
      "No journal observations have been recorded.",
    );
  });

  it("distinguishes a snapshot that has not loaded from an unavailable contract", () => {
    const { rerender } = render(<App envelope={null} />);

    expect(screen.getByRole("status")).toHaveTextContent("Village snapshot has not loaded yet.");

    rerender(<App envelope={{ kind: "snapshot", snapshot: { ...fixture.snapshot, tasks: null } }} />);

    expect(screen.getByRole("alert")).toHaveTextContent(
      "Expected snapshot.tasks to be an array",
    );
  });

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
