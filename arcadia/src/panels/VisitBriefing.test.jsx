import React from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import "@testing-library/jest-dom/vitest";
import { VisitBriefing } from "./VisitBriefing.jsx";
import { saveVisitBaseline } from "../world/visitBriefing.js";

const initial = { generation: 1, log_generation: 1, evaluated_at: "2026-09-05T10:00:00Z", villagers: [], tasks: [{ id: "t", state: "claimed" }], approvals: [] };
const changed = { ...initial, generation: 2, villagers: [{ id: "hob", name: "Hob", state: "working" }], tasks: [{ id: "t", title: "Finish garden", state: "done" }], approvals: [{ request_id: "a", message: "Please review", state: "pending" }] };

describe("visit briefing panel", () => {
  beforeEach(() => localStorage.clear());
  afterEach(() => cleanup());
  it("is quiet on first visit, then shows a collapsible returning-visit summary", () => {
    const first = render(<VisitBriefing snapshot={initial} />);
    expect(screen.queryByRole("region", { name: "Since your last visit" })).not.toBeInTheDocument();
    first.unmount();
    const callbacks = { onSelectAgent: vi.fn(), onOpenArchive: vi.fn(), onReviewApprovals: vi.fn() };
    const view = render(<VisitBriefing snapshot={changed} {...callbacks} />);
    fireEvent.click(screen.getByText("Since your last visit"));
    fireEvent.click(screen.getByRole("button", { name: "Hob" }));
    expect(callbacks.onSelectAgent).toHaveBeenCalledWith({ kind: "agent", id: "hob" });
    fireEvent.click(screen.getByRole("button", { name: "Finish garden" }));
    expect(callbacks.onOpenArchive).toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: "Please review" }));
    expect(callbacks.onReviewApprovals).toHaveBeenCalled();
    view.rerender(<VisitBriefing snapshot={{ ...changed, generation: 3 }} {...callbacks} />);
    expect(screen.getByText("3 updates")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Mark seen" }));
    expect(screen.queryByRole("region", { name: "Since your last visit" })).not.toBeInTheDocument();
  });
  it("goes quiet after a log reset", () => {
    saveVisitBaseline(initial);
    const view = render(<VisitBriefing snapshot={changed} />);
    expect(screen.getByRole("region", { name: "Since your last visit" })).toBeInTheDocument();
    view.rerender(<VisitBriefing snapshot={{ ...changed, log_generation: 2 }} />);
    expect(screen.queryByRole("region", { name: "Since your last visit" })).not.toBeInTheDocument();
  });
});
