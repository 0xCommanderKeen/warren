import "@testing-library/jest-dom/vitest";
import {
  act,
  cleanup,
  fireEvent,
  render,
  screen,
  within,
} from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import fixture from "../contract/fixtures/complete-v1.js";
import { VillageExperience } from "./VillageExperience.jsx";

const originalScrollIntoView = Element.prototype.scrollIntoView;
let rendererProps;
let interiorProps;
vi.mock("../world/VillageWorld.jsx", () => ({
  VillageWorld: (props) => {
    rendererProps = props;
    return (
      <div data-testid="miniature-world">
        <button onClick={() => props.onError(new Error("WebGL unavailable"))}>
          Fail renderer
        </button>
        <button
          onClick={() =>
            props.onSelect({
              kind: "building",
              id: props.world.buildings.find((b) => b.kind === "workshop").id,
            })
          }
        >
          Select workshop
        </button>
      </div>
    );
  },
}));
vi.mock("../world/InteriorWorld.jsx", () => ({
  InteriorWorld: (props) => {
    interiorProps = props;
    return (
      <div data-testid="room-renderer">
        {props.agents.map((agent) => (
          <button
            key={agent.id}
            onClick={() => props.onSelect({ kind: "agent", id: agent.id })}
          >
            Inspect {agent.name} indoors
          </button>
        ))}
      </div>
    );
  },
}));
beforeEach(() => localStorage.clear());
afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
  vi.useRealTimers();
  if (originalScrollIntoView)
    Element.prototype.scrollIntoView = originalScrollIntoView;
  else delete Element.prototype.scrollIntoView;
});
const snapshot = () => structuredClone(fixture.snapshot);
const selectKeeper = () =>
  fireEvent.click(document.querySelector(".ve-person"));

describe("miniature village experience", () => {
  it("keeps the selected identity and current state through stream updates, then clears departed agents", () => {
    const first = snapshot();
    const view = render(<VillageExperience snapshot={first} />);
    selectKeeper();
    expect(
      screen.getByRole("region", { name: "Selected villager" }),
    ).toHaveTextContent("Needs approval");
    fireEvent.click(screen.getByRole("button", { name: "Follow agent ↗" }));
    expect(rendererProps.follow).toBe(true);
    const next = snapshot();
    next.villagers[0].last_line = "The report is ready.";
    view.rerender(<VillageExperience snapshot={next} />);
    expect(
      screen.getByRole("region", { name: "Selected villager" }),
    ).toHaveTextContent("The report is ready.");
    expect(rendererProps.follow).toBe(true);
    view.rerender(<VillageExperience snapshot={{ ...next, villagers: [] }} />);
    expect(
      screen.queryByRole("region", { name: "Selected villager" }),
    ).not.toBeInTheDocument();
    expect(rendererProps.selection).toBeNull();
    expect(rendererProps.follow).toBe(false);
  });
  it("finds project workshops and links occupants to their real records", () => {
    const data = snapshot();
    data.villagers[0].state = "working";
    data.villagers[0].pending_approval_ids = [];
    data.approvals = [];
    render(<VillageExperience snapshot={data} />);
    fireEvent.click(screen.getByRole("button", { name: /Projects 1/ }));
    fireEvent.change(
      screen.getByRole("searchbox", { name: "Find a villager" }),
      { target: { value: "burrow" } },
    );
    fireEvent.click(screen.getByRole("button", { name: /burrow 1 agent/ }));
    const building = screen.getByRole("region", { name: "Selected building" });
    expect(building).toHaveTextContent("shared place");
    fireEvent.click(within(building).getByRole("button", { name: /Keeper/ }));
    const detail = screen.getByRole("region", { name: "Selected villager" });
    expect(detail).toHaveTextContent("Freeze contract");
    expect(detail).toHaveTextContent("daily");
    expect(detail).toHaveTextContent("report.md");
    expect(detail).not.toHaveTextContent("Draft the letter");
  });
  it("keeps the accessible directory usable if graphics fail", () => {
    render(<VillageExperience snapshot={snapshot()} />);
    fireEvent.click(screen.getByRole("button", { name: "Fail renderer" }));
    expect(screen.getByRole("status")).toHaveTextContent(
      "Everyone is still available in the directory",
    );
    selectKeeper();
    expect(
      screen.getByRole("region", { name: "Selected villager" }),
    ).toHaveTextContent("Keeper");
  });
  it("routes attention to the agent without inventing approval mutations", () => {
    render(<VillageExperience snapshot={snapshot()} />);
    expect(screen.getByRole("link", { name: "Review →" })).toHaveAttribute(
      "href",
      "#approvals",
    );
    fireEvent.click(
      screen.getByRole("button", { name: "Locate agent needing attention" }),
    );
    expect(rendererProps.selection).toEqual({
      kind: "agent",
      id: "claude:keeper",
    });
  });
  it("repeats camera commands and honors reduced motion without stopping live data", () => {
    vi.stubGlobal("matchMedia", () => ({ matches: true }));
    render(<VillageExperience snapshot={snapshot()} />);
    expect(rendererProps.paused).toBe(true);
    fireEvent.click(screen.getByRole("button", { name: "Resume motion" }));
    expect(rendererProps.paused).toBe(false);
    fireEvent.click(screen.getByRole("button", { name: "Zoom in" }));
    const nonce = rendererProps.cameraCommand.nonce;
    fireEvent.click(screen.getByRole("button", { name: "Zoom in" }));
    expect(rendererProps.cameraCommand).toEqual({
      type: "zoom-in",
      nonce: nonce + 1,
    });
    fireEvent.change(
      screen.getByRole("combobox", { name: "Rendering quality" }),
      { target: { value: "low" } },
    );
    expect(rendererProps.quality).toBe("low");
  });
  it("displays only retained observations in the journal and lets entries select their agent", () => {
    render(<VillageExperience snapshot={snapshot()} />);
    fireEvent.click(screen.getByRole("button", { name: /Village journal/ }));
    const activity = screen.getByRole("region", { name: "Village activity" });
    fireEvent.click(
      within(activity).getByRole("button", { name: /Keeper needs human/ }),
    );
    expect(rendererProps.selection.id).toBe("claude:keeper");
    expect(activity).not.toHaveTextContent("Deploy?");
  });
});

describe("quiet village announcements", () => {
  const arrival = (generation) => {
    const next = snapshot();
    next.generation = generation;
    next.villagers.push({
      ...next.villagers[0],
      id: "codex:visitor",
      name: "Ada",
      residency: "visitor",
      resident_file: null,
    });
    return next;
  };
  it("initializes silently and announces a new arrival only once, including after reconnect", () => {
    const view = render(<VillageExperience snapshot={snapshot()} />);
    expect(screen.queryByRole("status", { name: "Village update" })).toBeNull();
    const next = arrival(fixture.snapshot.generation + 1);
    view.rerender(<VillageExperience snapshot={next} />);
    expect(
      screen.getByRole("status", { name: "Village update" }),
    ).toHaveTextContent("Ada arrived in the village.");
    fireEvent.click(
      screen.getByRole("button", { name: "Dismiss village update" }),
    );
    view.rerender(<VillageExperience snapshot={structuredClone(next)} />);
    expect(screen.queryByRole("status", { name: "Village update" })).toBeNull();
    view.rerender(
      <VillageExperience
        snapshot={{ ...next, generation: next.generation + 1 }}
      />,
    );
    expect(screen.queryByRole("status", { name: "Village update" })).toBeNull();
  });
  it("resets its baseline quietly when generation drops or the retained log changes", () => {
    const view = render(<VillageExperience snapshot={snapshot()} />);
    const reset = arrival(1);
    view.rerender(<VillageExperience snapshot={reset} />);
    expect(screen.queryByRole("status", { name: "Village update" })).toBeNull();
    view.rerender(<VillageExperience snapshot={{ ...reset, generation: 2 }} />);
    expect(screen.queryByRole("status", { name: "Village update" })).toBeNull();
    const replacement = arrival(3);
    replacement.log_generation += 1;
    replacement.villagers.push({
      ...replacement.villagers[0],
      id: "claude:new",
      name: "Bea",
    });
    view.rerender(<VillageExperience snapshot={replacement} />);
    expect(screen.queryByRole("status", { name: "Village update" })).toBeNull();
  });
  it("announces observed task completion, expires it, and never celebrates historical done tasks", () => {
    vi.useFakeTimers();
    const initial = snapshot();
    initial.tasks[1].state = "done";
    const view = render(<VillageExperience snapshot={initial} />);
    expect(screen.queryByRole("status", { name: "Village update" })).toBeNull();
    const next = structuredClone(initial);
    next.generation += 1;
    next.tasks[0].state = "done";
    view.rerender(<VillageExperience snapshot={next} />);
    expect(
      screen.getByRole("status", { name: "Village update" }),
    ).toHaveTextContent("Task completed: Freeze contract.");
    act(() => vi.advanceTimersByTime(9_000));
    expect(screen.queryByRole("status", { name: "Village update" })).toBeNull();
    view.rerender(
      <VillageExperience
        snapshot={{ ...next, generation: next.generation + 1 }}
      />,
    );
    expect(screen.queryByRole("status", { name: "Village update" })).toBeNull();
  });
  it("shows the viewer's local clock and updates it once per minute", () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date(2026, 8, 5, 13, 2));
    render(<VillageExperience snapshot={snapshot()} />);
    const clock = screen.getByTitle("Your local time");
    expect(clock).toHaveTextContent("Daylight");
    expect(clock).toHaveAttribute(
      "datetime",
      new Date(2026, 8, 5, 13, 2).toISOString(),
    );
    act(() => vi.advanceTimersByTime(60_000));
    expect(clock).toHaveAttribute(
      "datetime",
      new Date(2026, 8, 5, 13, 3).toISOString(),
    );
  });
});

describe("dossier navigation and recency", () => {
  it("shows the newest retained tasks, routines, and artifacts before truncating", () => {
    const data = snapshot();
    const dates = Array.from(
      { length: 9 },
      (_, i) => `2026-08-${String(i + 10).padStart(2, "0")}T12:00:00.000Z`,
    );
    data.tasks = dates.map((ts, i) => ({
      ...data.tasks[0],
      id: `task-${i}`,
      title: `Task ${i}`,
      updated_at: ts,
    }));
    data.routines = dates.map((ts, i) => ({
      ...data.routines[0],
      run_id: `run-${i}`,
      routine: `Routine ${i}`,
      updated_at: ts,
    }));
    data.artifacts = dates.map((ts, i) => ({
      ...data.artifacts[0],
      artifact: `artifact-${i}.md`,
      ts,
    }));
    render(<VillageExperience snapshot={data} />);
    selectKeeper();
    const dossier = screen.getByRole("region", { name: "Selected villager" });
    expect(dossier).toHaveTextContent("Task 8");
    expect(dossier).toHaveTextContent("Routine 8");
    expect(dossier).toHaveTextContent("artifact-8.md");
    expect(dossier).not.toHaveTextContent("Task 0");
    expect(dossier).not.toHaveTextContent("Routine 0");
    expect(dossier).not.toHaveTextContent("artifact-0.md");
    expect(
      within(dossier).getAllByRole("list")[0].firstElementChild,
    ).toHaveTextContent("Task 8");
  });
  it("scrolls to mobile details on selection without scrolling again on a live update", () => {
    vi.stubGlobal("matchMedia", (query) => ({
      matches: query === "(max-width: 800px)",
    }));
    const scroll = vi.fn();
    Element.prototype.scrollIntoView = scroll;
    const view = render(<VillageExperience snapshot={snapshot()} />);
    selectKeeper();
    expect(scroll).toHaveBeenCalledExactlyOnceWith({
      behavior: "smooth",
      block: "nearest",
    });
    view.rerender(
      <VillageExperience snapshot={structuredClone(fixture.snapshot)} />,
    );
    expect(scroll).toHaveBeenCalledTimes(1);
    fireEvent.click(screen.getByRole("button", { name: "Follow agent ↗" }));
    expect(rendererProps.follow).toBe(true);
    fireEvent.click(screen.getByRole("button", { name: "Reset camera" }));
    expect(rendererProps.follow).toBe(false);
    expect(scroll).toHaveBeenCalledTimes(1);
  });
  it("respects reduced motion when opening mobile details", () => {
    vi.stubGlobal("matchMedia", () => ({ matches: true }));
    const scroll = vi.fn();
    Element.prototype.scrollIntoView = scroll;
    render(<VillageExperience snapshot={snapshot()} />);
    selectKeeper();
    expect(scroll).toHaveBeenCalledExactlyOnceWith({
      behavior: "auto",
      block: "nearest",
    });
  });
});

describe("persistent village geography", () => {
  it("restores a resident's allocated home after remount with a changed population", () => {
    const first = snapshot();
    const view = render(<VillageExperience snapshot={first} />);
    const home = rendererProps.world.buildings.find(
      (building) => building.id === "home:claude:keeper",
    ).position;
    expect(localStorage.getItem("arcadia:village-layout:v1")).toBeTruthy();
    view.unmount();
    const changed = snapshot();
    changed.villagers.unshift({
      ...changed.villagers[0],
      id: "claude:earlier",
      name: "Earlier",
      home: 0,
    });
    render(<VillageExperience snapshot={changed} />);
    expect(
      rendererProps.world.buildings.find(
        (building) => building.id === "home:claude:keeper",
      ).position,
    ).toEqual(home);
    expect(
      rendererProps.world.buildings.find(
        (building) => building.id === "home:claude:earlier",
      ).position,
    ).not.toEqual(home);
    expect(localStorage.getItem("arcadia:village-layout:v1")).not.toContain(
      "Needs approval",
    );
  });
  it("keeps rendering if storage is blocked or the write quota is exhausted", () => {
    vi.spyOn(Storage.prototype, "getItem").mockImplementation(() => {
      throw new Error("Storage blocked");
    });
    vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => {
      throw new Error("Quota exceeded");
    });
    render(<VillageExperience snapshot={snapshot()} />);
    selectKeeper();
    expect(
      screen.getByRole("region", { name: "Selected villager" }),
    ).toHaveTextContent("Keeper");
  });
  it("ignores malformed saved layout JSON", () => {
    localStorage.setItem("arcadia:village-layout:v1", "{broken");
    render(<VillageExperience snapshot={snapshot()} />);
    expect(rendererProps.world.agents[0].name).toBe("Keeper");
  });
});

describe("entering village rooms", () => {
  const atWork = () => {
    const data = snapshot();
    data.approvals = [];
    data.villagers[0].pending_approval_ids = [];
    data.villagers[0].state = "working";
    data.villagers.push({
      ...data.villagers[0],
      id: "codex:guest",
      name: "Guest",
      residency: "visitor",
      resident_file: null,
      state: "resting",
      project: "other",
    });
    return data;
  };
  it("enters the one shared workshop and lists only its actual indoor occupants", () => {
    render(<VillageExperience snapshot={atWork()} />);
    fireEvent.click(
      within(
        screen.getByRole("navigation", { name: "Village places" }),
      ).getByRole("button", { name: "Workshop" }),
    );
    const room = screen.getByRole("region", { name: "Building interior" });
    expect(room).toHaveTextContent("Workshop");
    expect(interiorProps.agents.map((agent) => agent.name)).toEqual(["Keeper"]);
    expect(room).not.toHaveTextContent("Guest");
    fireEvent.click(
      within(room).getByRole("button", { name: "Inspect Keeper indoors" }),
    );
    expect(
      screen.getByRole("region", { name: "Selected villager" }),
    ).toHaveTextContent("Keeper");
    expect(
      screen.getByRole("region", { name: "Building interior" }),
    ).toBeVisible();
    fireEvent.click(screen.getByRole("button", { name: "Back to village" }));
    expect(
      screen.queryByRole("region", { name: "Building interior" }),
    ).toBeNull();
    expect(screen.getByTestId("miniature-world")).toBeVisible();
  });
  it("opens an indoor person's room and can visit their currently empty home", () => {
    render(<VillageExperience snapshot={atWork()} />);
    selectKeeper();
    expect(interiorProps.building.id).toBe("workshop");
    fireEvent.click(screen.getByRole("button", { name: "Visit home →" }));
    expect(interiorProps.building.id).toBe("home:claude:keeper");
    expect(
      screen.getByRole("region", { name: "Building interior" }),
    ).toHaveTextContent("No agents are inside right now.");
  });
  it("opens the lodge from the accessible places and returns outdoors when a home disappears", () => {
    const data = atWork();
    const view = render(<VillageExperience snapshot={data} />);
    fireEvent.click(
      within(
        screen.getByRole("navigation", { name: "Village places" }),
      ).getByRole("button", { name: "Visitor lodge" }),
    );
    expect(interiorProps.agents.map((agent) => agent.name)).toEqual(["Guest"]);
    selectKeeper();
    fireEvent.click(screen.getByRole("button", { name: "Visit home →" }));
    view.rerender(
      <VillageExperience
        snapshot={{ ...data, villagers: data.villagers.slice(1) }}
      />,
    );
    expect(
      screen.queryByRole("region", { name: "Building interior" }),
    ).toBeNull();
  });
  it("updates the occupants of an open room directly from the next snapshot", () => {
    const first = atWork();
    const view = render(<VillageExperience snapshot={first} />);
    fireEvent.click(
      within(
        screen.getByRole("navigation", { name: "Village places" }),
      ).getByRole("button", { name: "Workshop" }),
    );
    expect(interiorProps.agents.map((agent) => agent.name)).toEqual(["Keeper"]);
    const next = atWork();
    next.generation += 1;
    next.villagers[0].state = "resting";
    next.villagers[1].state = "working";
    view.rerender(<VillageExperience snapshot={next} />);
    expect(interiorProps.building.id).toBe("workshop");
    expect(interiorProps.agents.map((agent) => agent.name)).toEqual(["Guest"]);
  });
  it("keeps two projects in one workshop instead of creating project buildings", () => {
    const data = atWork();
    data.villagers[1].state = "working";
    render(<VillageExperience snapshot={data} />);
    fireEvent.click(screen.getByRole("button", { name: /Projects 2/ }));
    fireEvent.click(screen.getByRole("button", { name: /other 1 agent/ }));
    expect(interiorProps.building.id).toBe("workshop");
    expect(interiorProps.agents).toHaveLength(2);
    expect(
      rendererProps.world.buildings.filter(
        (building) => building.kind === "workshop",
      ),
    ).toHaveLength(1);
  });
});

it("controls the room camera independently of the outdoor camera", () => {
  render(<VillageExperience snapshot={snapshot()} />);
  fireEvent.click(
    within(
      screen.getByRole("navigation", { name: "Village places" }),
    ).getByRole("button", { name: "Workshop" }),
  );
  fireEvent.click(screen.getByRole("button", { name: "Zoom into room" }));
  expect(interiorProps.cameraCommand).toEqual({
    roomId: "workshop",
    type: "zoom-in",
    nonce: 1,
  });
  fireEvent.click(screen.getByRole("button", { name: "Zoom into room" }));
  expect(interiorProps.cameraCommand.nonce).toBe(2);
  fireEvent.click(screen.getByRole("button", { name: "Reset room view" }));
  expect(interiorProps.cameraCommand.type).toBe("reset");
  expect(rendererProps.cameraCommand).toBeNull();
  fireEvent.click(
    within(
      screen.getByRole("navigation", { name: "Village places" }),
    ).getByRole("button", { name: "Visitor lodge" }),
  );
  expect(interiorProps.cameraCommand).toBeNull();
});

describe("personal work cards and building previews", () => {
  function workingSnapshot() {
    const data = snapshot();
    data.villagers[0].state = "working";
    data.villagers[0].pending_approval_ids = [];
    data.approvals = [];
    return data;
  }
  it("shows a claimed task and recorded activity on an agent's desk card, never history payload work", () => {
    const data = workingSnapshot();
    data.villagers[0].last_line = "Reading the latest report.";
    data.villagers[0].history[0].payload.title =
      "Invented historical assignment";
    render(<VillageExperience snapshot={data} />);
    fireEvent.click(
      within(
        screen.getByRole("navigation", { name: "Village places" }),
      ).getByRole("button", { name: "Workshop" }),
    );
    const room = screen.getByRole("region", { name: "Building interior" });
    const card = within(room).getByRole("button", {
      name: /Keeper.*Desk.*Freeze contract/,
    });
    expect(card).toHaveTextContent("working");
    expect(card).toHaveTextContent("Reading the latest report.");
    expect(card).toHaveTextContent("Latest output · report.md");
    expect(card).not.toHaveTextContent("Invented historical assignment");
    expect(card).not.toHaveTextContent("Draft the letter");
    fireEvent.click(card);
    expect(
      screen.getByRole("region", { name: "Selected villager" }),
    ).toBeVisible();
  });
  it("previews actual occupancy and names without entering, then updates counts from the snapshot", () => {
    const data = workingSnapshot();
    const view = render(<VillageExperience snapshot={data} />);
    fireEvent.click(screen.getByRole("button", { name: /Buildings/ }));
    const home = [...document.querySelectorAll(".ve-building-preview")].find(
      (item) => item.textContent.includes("Keeper’s home"),
    );
    const workshop = [
      ...document.querySelectorAll(".ve-building-preview"),
    ].find((item) => item.querySelector("strong").textContent === "Workshop");
    expect(home).toHaveTextContent("0 inside");
    expect(workshop).toHaveTextContent("1 working");
    expect(workshop).toHaveTextContent("Keeper");
    expect(
      screen.queryByRole("region", { name: "Building interior" }),
    ).toBeNull();
    const next = workingSnapshot();
    next.generation += 1;
    next.villagers[0].state = "resting";
    view.rerender(<VillageExperience snapshot={next} />);
    expect(home).toHaveTextContent("1 inside");
    expect(workshop).toHaveTextContent("0 working");
    expect(workshop).not.toHaveTextContent("Keeper");
    home.open = true;
    fireEvent.click(
      within(home).getByRole("button", { name: /Enter Keeper’s home/ }),
    );
    expect(
      screen.getByRole("region", { name: "Building interior" }),
    ).toHaveTextContent("Bed");
    expect(interiorProps.agents[0].name).toBe("Keeper");
  });
});

it("follows a selected agent between workshop, home and the outdoor square", () => {
  const first = snapshot();
  first.approvals = [];
  first.villagers[0].state = "working";
  first.villagers[0].residency = "resident";
  const view = render(<VillageExperience snapshot={first} />);
  selectKeeper();
  fireEvent.click(screen.getByRole("button", { name: "Follow agent ↗" }));
  expect(interiorProps.building.id).toBe("workshop");
  const resting = structuredClone(first);
  resting.villagers[0].state = "resting";
  view.rerender(<VillageExperience snapshot={resting} />);
  expect(interiorProps.building.id).toBe(`home:${resting.villagers[0].id}`);
  expect(screen.getByRole("navigation", { name: "Your location" })).toHaveTextContent("Following Keeper");
  const waiting = structuredClone(resting);
  waiting.approvals = snapshot().approvals;
  view.rerender(<VillageExperience snapshot={waiting} />);
  expect(screen.queryByRole("region", { name: "Building interior" })).not.toBeInTheDocument();
  expect(rendererProps.follow).toBe(true);
  fireEvent.click(screen.getByRole("button", { name: "Village overview" }));
  expect(rendererProps.follow).toBe(false);
});

it("remembers rendering quality and the chosen motion preference", () => {
  const view = render(<VillageExperience snapshot={snapshot()} />);
  fireEvent.change(screen.getByRole("combobox", { name: "Rendering quality" }), { target: { value: "low" } });
  fireEvent.click(screen.getByRole("button", { name: "Pause motion", exact: true }));
  view.unmount();
  render(<VillageExperience snapshot={snapshot()} />);
  expect(screen.getByRole("combobox", { name: "Rendering quality" })).toHaveValue("low");
  expect(rendererProps.paused).toBe(true);
});
