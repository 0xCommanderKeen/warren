import "@testing-library/jest-dom/vitest";
import { cleanup, fireEvent, render, screen, within, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import fixture from "../contract/fixtures/complete-v1.js";
import { VillageArchive } from "./VillageArchive.jsx";

afterEach(() => { cleanup(); vi.restoreAllMocks(); vi.unstubAllGlobals(); });
function snapshot() {
  const value = structuredClone(fixture.snapshot);
  value.tasks[0].state = "done";
  value.tasks[0].updated_at = "2026-08-27T12:00:00.000Z";
  value.artifacts.push({ ...value.artifacts[0], artifact: "older.md", ts: "2026-08-26T12:00:00.000Z" });
  return value;
}
function openArchive(extra = {}) { return render(<VillageArchive snapshot={snapshot()} onBack={vi.fn()} onSelectAgent={vi.fn()} {...extra} />); }

describe("village archive", () => {
  it("shows real completed tasks and keeps artifacts separate where no task link exists", () => {
    openArchive();
    const task = screen.getByRole("article", { name: "Freeze contract" });
    expect(task).toHaveTextContent("Keeper");
    expect(task).not.toHaveTextContent("report.md");
    expect(within(screen.getByRole("region", { name: "Project not recorded" })).getByRole("article", { name: "Freeze contract" })).toBeVisible();
    expect(within(screen.getByRole("region", { name: "burrow" })).getByRole("article", { name: "report.md" })).toBeVisible();
    expect(screen.queryByRole("article", { name: "Draft the letter" })).not.toBeInTheDocument();
    fireEvent.click(within(task).getByText("Record details"));
    expect(task).toHaveTextContent("No outputs linked in this record.");
  });

  it("filters by record type and project, searches names and paths, and sorts newest first", () => {
    openArchive();
    fireEvent.click(screen.getByRole("button", { name: "Artifacts", exact: true }));
    expect(screen.getAllByRole("article").map(record => within(record).getByRole("heading").textContent)).toEqual(["report.md", "older.md"]);
    fireEvent.change(screen.getByRole("combobox", { name: "Project" }), { target: { value: "project:burrow" } });
    fireEvent.change(screen.getByRole("searchbox", { name: "Search the archive" }), { target: { value: "Keeper" } });
    expect(screen.getAllByRole("article")).toHaveLength(2);
    fireEvent.change(screen.getByRole("searchbox"), { target: { value: "older.md" } });
    expect(screen.getAllByRole("article")).toHaveLength(1);
    expect(screen.getByRole("article")).toHaveAccessibleName("older.md");
    fireEvent.change(screen.getByRole("searchbox"), { target: { value: "missing" } });
    expect(screen.getByText("No matching records.")).toBeVisible();
    fireEvent.click(screen.getByRole("button", { name: "Clear filters" }));
    expect(screen.getAllByRole("article")).toHaveLength(4);
  });

  it("shows metadata before explicit preview and copies exact local paths with visible feedback", async () => {
    const writeText = vi.fn().mockResolvedValue(undefined);
    vi.stubGlobal("navigator", { clipboard: { writeText } });
    openArchive();
    const record = screen.getByRole("article", { name: "report.md" });
    fireEvent.click(within(record).getByText("Inspect artifact"));
    expect(record).toHaveTextContent("Preview availability depends on the archive server.");
    expect(within(record).queryByRole("link", { name: /Open artifact/ })).not.toBeInTheDocument();
    fireEvent.click(within(record).getByRole("button", { name: "Copy path" }));
    await waitFor(() => expect(within(record).getByRole("status")).toHaveTextContent("Path copied."));
    expect(writeText).toHaveBeenCalledWith("report.md");
  });

  it("shows clipboard failure instead of claiming the path was copied", async () => {
    vi.stubGlobal("navigator", { clipboard: { writeText: vi.fn().mockRejectedValue(new Error("denied")) } });
    openArchive();
    const record = screen.getByRole("article", { name: "report.md" });
    fireEvent.click(within(record).getByText("Inspect artifact"));
    fireEvent.click(within(record).getByRole("button", { name: "Copy path" }));
    await waitFor(() => expect(within(record).getByRole("status")).toHaveTextContent("Couldn't copy."));
  });

  it("only creates external artifact links for HTTP or HTTPS", () => {
    const value = snapshot();
    value.artifacts = ["https://example.org/output/report.html", "http://example.org/out.txt", "javascript:alert(1)", "file:///secrets/token", "data:text/html,hello", "/private/report.md"].map(artifact => ({ ...value.artifacts[0], artifact }));
    openArchive({ snapshot: value });
    for (const summary of screen.getAllByText("Inspect artifact")) fireEvent.click(summary);
    const links = screen.getAllByRole("link", { name: /Open artifact/ });
    expect(links).toHaveLength(2);
    for (const link of links) {
      expect(link.getAttribute("href")).toMatch(/^https?:\/\//);
      expect(link).toHaveAttribute("rel", "noopener noreferrer");
    }
  });

  it("opens agent details from actual identities and returns to the village", () => {
    const onSelectAgent = vi.fn(), onBack = vi.fn();
    openArchive({ onSelectAgent, onBack });
    fireEvent.click(within(screen.getByRole("article", { name: "Freeze contract" })).getByRole("button", { name: /Keeper/ }));
    expect(onSelectAgent).toHaveBeenCalledWith({ kind: "agent", id: "claude:keeper" });
    fireEvent.click(screen.getByRole("button", { name: /Back to village/ }));
    expect(onBack).toHaveBeenCalledOnce();
  });

  it("shows an honest empty archive without placeholder accomplishments", () => {
    openArchive({ snapshot: { ...snapshot(), tasks: [], artifacts: [], journals: [] } });
    expect(screen.getByText("The shelves are waiting.")).toBeVisible();
    expect(screen.queryByRole("article")).not.toBeInTheDocument();
    expect(screen.getByRole("status")).toHaveTextContent("0 records");
  });
});
