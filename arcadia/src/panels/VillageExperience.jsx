import { AgentProfile } from "./AgentProfile.jsx";
import { RoomResidents } from "./RoomResidents.jsx";
import { VillageLayoutEditor } from "./VillageLayoutEditor.jsx";
import { AgentHandoffs } from "./AgentHandoffs.jsx";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { WorkshopBoard } from "./WorkshopBoard.jsx";
import { VisitBriefing } from "./VisitBriefing.jsx";
import { VillageNavigator } from "./VillageNavigator.jsx";
import { VillageArchive } from "./VillageArchive.jsx";
import { AgentAttention } from "./AgentAttention.jsx";
import { readDisplayPreferences, saveDisplayPreferences } from "../world/viewPreferences.js";
import { InteriorWorld } from "../world/InteriorWorld.jsx";
import { VillageWorld } from "../world/VillageWorld.jsx";
import { createVillageLayout } from "../world/layout.js";
import { buildingOccupancy } from "../world/occupancy.js";
import { daylightAt } from "../world/daylight.js";
import { pendingApprovals } from "../contract/approvals.js";
import "./village-experience.css";

const purposes = {
  home: "A permanent home for a resident of Warren.",
  workshop: "A shared place for working agents across every project.",
  lodge: "A place for visiting agent sessions to gather.",
  square: "The heart of the village. Requests for your attention arrive here.",
  archive: "The village's recorded artifacts, journals, and routines.",
  noticeboard: "Available jobs and recorded work across Warren.",
};
const stateLabel = (state) =>
  state === "knocking" ? "Needs you" : state?.replaceAll("_", " ") || "Unknown";
const timeLabel = (ts) =>
  ts && !Number.isNaN(Date.parse(ts))
    ? new Date(ts).toLocaleTimeString([], {
        hour: "2-digit",
        minute: "2-digit",
      })
    : "Unknown time";

function Portrait({ agent, large = false }) {
  return (
    <span
      className={`ve-portrait ${large ? "ve-portrait-large" : ""}`}
      style={{
        "--coat": agent.appearance?.body || agent.accent || "#678477",
        "--skin": agent.appearance?.skin || "#d5ac86",
        "--hat": agent.appearance?.hat || "#74523c",
      }}
      aria-hidden="true"
    >
      <i className="ve-portrait-body" />
      <i className="ve-portrait-face" />
      <i className="ve-portrait-hat" />
    </span>
  );
}

function PersonalSpace({ agent, room, snapshot, selected, onSelect }) {
  const task = snapshot.tasks
    .filter((task) => task.state === "claimed" && task.claimant === agent.id)
    .sort((a, b) => b.updated_at.localeCompare(a.updated_at))[0];
  const artifact = snapshot.artifacts
    .filter((item) => item.agent_id === agent.id)
    .sort((a, b) => b.ts.localeCompare(a.ts))[0];
  return (
    <button
      className="ve-space-card"
      aria-pressed={selected}
      onClick={onSelect}
    >
      <span className="ve-space-person">
        <Portrait agent={agent} />
        <span>
          <strong>{agent.name}</strong>
          <small>
            {room.kind === "workshop" ? "Desk" : "Bed"} ·{" "}
            {agent.project || "No project"}
          </small>
        </span>
        <span className="ve-person-state" data-state={agent.state}>
          <i />
          {stateLabel(agent.state)}
        </span>
      </span>
      <span className="ve-space-task">
        {task ? task.title : "No claimed task recorded."}
      </span>
      <span className="ve-space-observation">
        {agent.last_line || "No recent activity recorded."}
      </span>
      {artifact && (
        <span className="ve-space-output" title={artifact.artifact}>
          Latest output ·{" "}
          {artifact.artifact.split("/").filter(Boolean).at(-1) ||
            artifact.artifact}
        </span>
      )}
    </button>
  );
}

function BuildingPreview({ building, world, onSelect }) {
  const enterable = ["home", "lodge", "workshop"].includes(building.kind);
  const { agents: occupants, summary } = buildingOccupancy(world, building);
  return (
    <details className="ve-building-preview">
      <summary>
        <span className="ve-building-icon" aria-hidden="true">
          ⌂
        </span>
        <span>
          <strong>{building.name}</strong>
          <small>
            {summary} · {building.kind}
          </small>
        </span>
      </summary>
      <div className="ve-building-preview-body">
        {occupants.length ? (
          <ul>
            {occupants.map((agent) => (
              <li key={agent.id}>
                <span>{agent.name}</span>
                <small>{stateLabel(agent.state)}</small>
              </li>
            ))}
          </ul>
        ) : (
          <p>
            {enterable
              ? "Nobody is inside right now."
              : "No agents here right now."}
          </p>
        )}
        <button onClick={() => onSelect({ kind: "building", id: building.id })}>
          {enterable ? "Enter" : "Inspect"} {building.name} →
        </button>
      </div>
    </details>
  );
}

function Records({ title, items, children }) {
  return (
    <section className="ve-detail-records">
      <h4>
        {title}
        <span>{items.length}</span>
      </h4>
      {items.length ? (
        <ul>{items.slice(0, 8).map(children)}</ul>
      ) : (
        <p className="ve-muted">Nothing recorded.</p>
      )}
      {items.length > 8 && <a href="#records">See all records →</a>}
    </section>
  );
}

function AgentDetails({
  agent,
  snapshot,
  follow,
  setFollow,
  detailRef,
  onVisitHome,
  stewardClient,
}) {
  const charter = snapshot.residents.find(
    (r) => r.file === agent.resident_file,
  );
  const tasks = snapshot.tasks
    .filter((t) => t.claimant === agent.id || t.assignee === agent.id)
    .sort((a, b) => b.updated_at.localeCompare(a.updated_at));
  const routines = snapshot.routines
    .filter((r) => r.agent_id === agent.id)
    .sort((a, b) => b.updated_at.localeCompare(a.updated_at));
  const artifacts = snapshot.artifacts
    .filter((a) => a.agent_id === agent.id)
    .sort((a, b) => b.ts.localeCompare(a.ts));
  return (
    <section
      ref={detailRef}
      className="ve-dossier"
      aria-label="Selected villager"
    >
      <div className="agent-profile-identity">
      <div className="ve-detail-heading">
        <Portrait agent={agent} large />

      </div>
      <p className="ve-kicker">
        {agent.residency === "resident" ? "Village resident" : "Visiting agent"}
      </p>
      <h3 id="agent-profile-name">{agent.name}</h3>
      {charter?.meta.role && <p className="ve-role">{charter.meta.role}</p>}
      <div className="ve-detail-status">
        <span className="ve-state" data-state={agent.state}>
          {stateLabel(agent.state)}
        </span>
        <button
          className="ve-text-button"
          aria-pressed={follow}
          onClick={() => setFollow(!follow)}
        >
          {follow ? "Following · stop" : "Follow agent ↗"}
        </button>
      </div>

      <dl className="ve-facts">
        <dt>Project</dt>
        <dd>{agent.project || "None"}</dd>
        <dt>Last seen</dt>
        <dd>
          {agent.last_ts ? (
            <time
              dateTime={agent.last_ts}
              title={new Date(agent.last_ts).toLocaleString()}
            >
              {new Date(agent.last_ts).toLocaleString()}
            </time>
          ) : (
            "Unknown"
          )}
        </dd>
      </dl>
      {charter?.body && (
        <details className="ve-charter">
          <summary>About this resident</summary>
          <p>{charter.body}</p>
          {Object.keys(charter.capabilities || {}).length > 0 && (
            <dl className="ve-facts">
              {Object.entries(charter.capabilities).map(([key, value]) => (
                <div key={key}>
                  <dt>{key}</dt>
                  <dd>
                    {Array.isArray(value)
                      ? value.join(", ")
                      : JSON.stringify(value)}
                  </dd>
                </div>
              ))}
            </dl>
          )}
        </details>
      )}
      {agent.residency === "resident" && (
        <button className="ve-text-button" onClick={onVisitHome}>
          Visit home →
        </button>
      )}
      </div>
      <div className="agent-profile-work">
      <h4>Current activity</h4>
      <p className="ve-current">{agent.last_line || "No recent activity recorded."}</p>
      <AgentAttention snapshot={snapshot} stewardClient={stewardClient} agentId={agent.id} />
      <Records title="Tasks" items={tasks}>
        {(t) => (
          <li key={t.id}>
            <strong>{t.title}</strong>
            <small>{t.state}</small>
          </li>
        )}
      </Records>
      <Records title="Routine runs" items={routines}>
        {(r) => (
          <li key={r.run_id}>
            <strong>{r.routine}</strong>
            <small>
              {r.state}
              {r.outcome ? ` · ${r.outcome}` : ""}
            </small>
          </li>
        )}
      </Records>
      <Records title="Artifacts" items={artifacts}>
        {(a, i) => (
          <li key={`${a.ts}:${i}`}>
            <strong title={a.artifact}>{a.artifact}</strong>
            <small>{timeLabel(a.ts)}</small>
          </li>
        )}
      </Records>
      <Records
        title="Recent observations"
        items={[...(agent.history || [])].sort((a, b) =>
          b.ts.localeCompare(a.ts),
        )}
      >
        {(e, i) => (
          <li key={`${e.ts}:${i}`}>
            <strong>{e.type.replaceAll("_", " ")}</strong>
            <small>{timeLabel(e.ts)}</small>
          </li>
        )}
      </Records>
      </div>
    </section>
  );
}

export function VillageExperience({ snapshot, stewardClient, active = true }) {
  const layout = useRef(null);
  if (!layout.current) {
    let savedLayout = null;
    try {
      savedLayout = JSON.parse(
        localStorage.getItem("arcadia:village-layout:v1"),
      );
    } catch {
      // Storage can be disabled, full, or left with an invalid old value.
    }
    layout.current = createVillageLayout(savedLayout);
  }
  const [layoutRevision, setLayoutRevision] = useState(0);
  const [editingLayout, setEditingLayout] = useState(false);
  const world = useMemo(() => layout.current.update(snapshot), [snapshot, layoutRevision]);
  const changeLayout = (action) => {
    const result = action();
    if (result.ok && result.changed) setLayoutRevision(value => value + 1);
    return result;
  };
  useEffect(() => {
    try {
      const saved = layout.current.serialize();
      if (saved)
        localStorage.setItem(
          "arcadia:village-layout:v1",
          JSON.stringify(saved),
        );
    } catch {
      // Persistence is optional: the live village still works without storage.
    }
  }, [world]);
  const [selection, setSelection] = useState(null);
  const [profileOpen, setProfileOpen] = useState(false);
  useEffect(() => { if (!active) setProfileOpen(false); }, [active]);
  const [roomId, setRoomId] = useState(null);
  const [roomError, setRoomError] = useState(null);
  const [roomCameraCommand, setRoomCameraCommand] = useState(null);
  const room = world.buildings.find((building) => building.id === roomId);
  const roomAgents = room
    ? world.agents.filter(
        (agent) => agent.buildingId === room.id && agent.indoor,
      )
    : [];
  useEffect(() => {
    if (roomId && !room) setRoomId(null);
  }, [roomId, room]);
  useEffect(() => setRoomError(null), [roomId]);
  const onRoomError = useCallback(
    (error) => setRoomError(error?.message || "Room rendering is unavailable."),
    [],
  );
  const detailRef = useRef(null);
  const roomRef = useRef(null);
  const previousScrollRoom = useRef(null);
  useEffect(() => {
    const enteredRoom = roomId && roomId !== previousScrollRoom.current;
    previousScrollRoom.current = roomId;
    if (!active || selection?.kind === "agent" || !selection || !globalThis.matchMedia?.("(max-width: 800px)").matches)
      return;
    const target = enteredRoom ? roomRef.current : detailRef.current;
    target?.scrollIntoView?.({
      behavior: globalThis.matchMedia?.("(prefers-reduced-motion: reduce)")
        .matches
        ? "auto"
        : "smooth",
      block: "nearest",
    });
  }, [selection?.kind, selection?.id, roomId, active]);
  const [query, setQuery] = useState("");
  const [tab, setTab] = useState("people");
  const [filter, setFilter] = useState("all");
  const [preferences] = useState(readDisplayPreferences);
  const [paused, setPaused] = useState(() => preferences.paused ??
    (globalThis.matchMedia?.("(prefers-reduced-motion: reduce)").matches ?? false));
  const [quality, setQuality] = useState(preferences.quality);
  const [adaptiveDetail, setAdaptiveDetail] = useState("high");
  useEffect(() => saveDisplayPreferences({ quality, paused }), [quality, paused]);
  const [follow, setFollow] = useState(false);
  const [followDestination, setFollowDestination] = useState(null);
  const [cameraView, setCameraView] = useState({ zoom: 1 });
  const [mapHost, setMapHost] = useState(null);
  const [taskRequest, setTaskRequest] = useState(null);
  const [taskFocusAgentId, setTaskFocusAgentId] = useState(null);
  const [cameraCommand, setCameraCommand] = useState(null);
  const [ready, setReady] = useState(false);
  const [error, setError] = useState(null);
  const [feedOpen, setFeedOpen] = useState(false);
  const [localTime, setLocalTime] = useState(() => new Date());
  const [announcement, setAnnouncement] = useState(null);
  const observed = useRef(null);
  useEffect(() => {
    const timer = setInterval(() => setLocalTime(new Date()), 60_000);
    return () => clearInterval(timer);
  }, []);
  useEffect(() => {
    const previous = observed.current;
    const baseline = {
      generation: snapshot.generation,
      logGeneration: snapshot.log_generation,
      agentIds: new Set(world.agents.map((agent) => agent.id)),
      tasks: new Map(snapshot.tasks.map((task) => [task.id, task.state])),
    };
    // A fresh page or a replaced Chronicle log establishes a quiet baseline.
    if (
      !previous ||
      snapshot.generation < previous.generation ||
      snapshot.log_generation !== previous.logGeneration
    ) {
      observed.current = baseline;
      setAnnouncement(null);
      return;
    }
    if (snapshot.generation === previous.generation) return;
    const arrivals = world.agents.filter(
      (agent) => !previous.agentIds.has(agent.id),
    );
    const completed = snapshot.tasks.filter(
      (task) =>
        task.state === "done" &&
        previous.tasks.has(task.id) &&
        previous.tasks.get(task.id) !== "done",
    );
    baseline.agentIds = new Set([...previous.agentIds, ...baseline.agentIds]);
    observed.current = baseline;
    const messages = [];
    if (arrivals.length)
      messages.push(
        arrivals.length === 1
          ? `${arrivals[0].name} arrived in the village.`
          : `${arrivals.length} agents arrived in the village.`,
      );
    if (completed.length)
      messages.push(
        completed.length === 1
          ? `Task completed: ${completed[0].title}.`
          : `${completed.length} tasks completed.`,
      );
    if (messages.length)
      setAnnouncement({
        generation: snapshot.generation,
        text: messages.join(" "),
      });
  }, [snapshot, world]);
  useEffect(() => {
    if (!announcement) return;
    const timer = setTimeout(() => setAnnouncement(null), 9_000);
    return () => clearTimeout(timer);
  }, [announcement]);
  const onReady = useCallback(() => {
    setReady(true);
    setError(null);
  }, []);
  const onError = useCallback((e) => {
    setError(e?.message || "3D rendering is unavailable.");
  }, []);
  const command = (type) => {
    if (type === "reset") setFollow(false);
    setCameraCommand((previous) => ({
      type,
      nonce: (previous?.nonce || 0) + 1,
    }));
  };
  const select = useCallback(
    (next) => {
      setSelection(next);
      setProfileOpen(next?.kind === "agent");
      setFollow(false);
      setRoomCameraCommand(null);
      setTaskRequest(null);
      setTaskFocusAgentId(null);
      if (!next) return;
      const target =
        next.kind === "building"
          ? world.buildings.find((building) => building.id === next.id)
          : world.agents.find((agent) => agent.id === next.id);
      setRoomId(
        next.kind === "building"
          ? ["home", "lodge", "workshop", "archive"].includes(target?.kind)
            ? target.id
            : null
          : target?.indoor
            ? target.buildingId
            : null,
      );
    },
    [world],
  );
  const highlightTaskAgent = useCallback(next => { setProfileOpen(false); setSelection(next); setFollow(false); setTaskFocusAgentId(next?.id || null); }, []);
  const selectedAgent =
    selection?.kind === "agent"
      ? world.agents.find((a) => a.id === selection.id)
      : null;
  const selectedBuilding =
    selection?.kind === "building"
      ? world.buildings.find((b) => b.id === selection.id)
      : null;
  useEffect(() => {
    if (selection && !selectedAgent && !selectedBuilding) {
      setSelection(null);
      setFollow(false);
    }
  }, [selection, selectedAgent, selectedBuilding]);
  useEffect(() => {
    if (!active || !follow || !selectedAgent) { setFollowDestination(null); return; }
    const nextRoom = selectedAgent.indoor ? selectedAgent.buildingId : null;
    if (roomId === nextRoom) { setFollowDestination(null); return; }
    const enter = () => { setRoomId(nextRoom); setRoomCameraCommand(null); setFollowDestination(null); };
    if (paused || globalThis.matchMedia?.("(prefers-reduced-motion: reduce)").matches) { enter(); return; }
    setFollowDestination(world.buildings.find(building => building.id === selectedAgent.buildingId)?.name || "The village");
    const timer = setTimeout(enter, 850);
    return () => clearTimeout(timer);
  }, [active, follow, selectedAgent?.id, selectedAgent?.buildingId, selectedAgent?.indoor, roomId, paused]);
  const overview = () => {
    setRoomId(null);
    setRoomCameraCommand(null);
    setSelection(null);
    setFollow(false);
    command("reset");
  };
  const workshop = world.buildings.find(
    (building) => building.kind === "workshop",
  );
  const projects = [
    ...new Set(world.agents.map((agent) => agent.project).filter(Boolean)),
  ]
    .sort()
    .map((project) => ({
      id: project,
      name: project,
      project,
      agentIds: world.agents
        .filter((agent) => agent.project === project)
        .map((agent) => agent.id),
    }));
  const search = query.toLowerCase().trim();
  const people = world.agents
    .filter(
      (a) =>
        `${a.name} ${a.project || ""} ${a.state}`
          .toLowerCase()
          .includes(search) &&
        (filter === "all" ||
          (filter === "residents"
            ? a.residency === "resident"
            : a.state === "working")),
    )
    .sort(
      (a, b) =>
        Number(b.residency === "resident") -
          Number(a.residency === "resident") ||
        a.name.localeCompare(b.name) ||
        a.id.localeCompare(b.id),
    );
  const visibleProjects = projects.filter((p) =>
    `${p.name} ${p.project || ""}`.toLowerCase().includes(search),
  );
  const approvals = pendingApprovals(snapshot.approvals);
  const events = useMemo(
    () =>
      snapshot.villagers
        .flatMap((a) =>
          (a.history || []).map((event, index) => ({
            ...event,
            agentName: a.name,
            agentId: a.id,
            key: `${a.id}:${index}:${event.ts}`,
          })),
        )
        .sort((a, b) => b.ts.localeCompare(a.ts))
        .slice(0, 12),
    [snapshot],
  );
  const working = world.agents.filter((a) => a.state === "working").length;
  return (
    <div id="village" className="ve-experience">
      <VisitBriefing snapshot={snapshot} onSelectAgent={select}
        onOpenArchive={() => select({ kind: "building", id: "archive" })}
        onOpenTask={id => { select({ kind: "building", id: "workshop" }); setTaskRequest(previous => ({ id, nonce: (previous?.nonce || 0) + 1 })); }}
        onReviewApprovals={() => { window.location.hash = "#approvals"; }} />
      <header className="ve-introduction">
        <h2>The clearing</h2>
        <p>
          {world.agents.length} {world.agents.length === 1 ? "agent" : "agents"}
          <span> / </span>
          {projects.length} {projects.length === 1 ? "project" : "projects"}
          <span> / </span>
          {working} working
        </p>
      </header>
      <nav className="ve-location-bar" aria-label="Your location">
        <button onClick={overview}>Village overview</button>
        <span aria-hidden="true">/</span>
        <span>{room?.name || (selectedAgent ? world.buildings.find(b => b.id === selectedAgent.buildingId)?.name : "The clearing")}</span>
        {follow && selectedAgent && <span className="ve-following" role="status">Following {selectedAgent.name} <button onClick={() => setFollow(false)}>Stop following</button></span>}
      </nav>
      {followDestination && <p className="ve-follow-destination" role="status">Next view: {followDestination}</p>}
      <VillageNavigator active={active} world={world} selection={selection} onSelect={select} onOverview={overview}
        mapHost={mapHost} camera={cameraView} roomId={roomId} visible={!room && cameraView.zoom > 1.4} />
      <div className="ve-main">
        <section
          className="ve-world-panel"
          data-interior={Boolean(room)}
          aria-label="Village"
        >
          <div className="ve-world-heading">
            <span className="ve-map-label">
              <i />
              WARREN · THE CLEARING
            </span>
            <span className="ve-world-note">A home for every agent</span>
          </div>
          {room?.kind === "archive" && <VillageArchive snapshot={snapshot} onSelectAgent={select} onBack={() => { setRoomId(null); setSelection(null); setFollow(false); }} />}
          {room && room.kind !== "archive" && (
            <section
              ref={roomRef}
              className="ve-room"
              aria-label="Building interior"
            >
              <header className="ve-room-header">
                <button
                  aria-label="Back to village"
                  onClick={() => {
                    setRoomId(null);
                    setRoomCameraCommand(null);
                    setSelection(null);
                    setFollow(false);
                  }}
                >
                  ← Back to village
                </button>
                <div>
                  <p className="ve-kicker">Inside the {room.kind}</p>
                  <h3>{room.name}</h3>
                </div>
                <span>{roomAgents.length} inside</span>
              </header>
              <div className="ve-room-canvas">
                <InteriorWorld
                  key={room.id}
                  building={room}
                  agents={roomAgents}
                  focusAgentId={follow ? selectedAgent?.id : taskFocusAgentId}
                  paused={paused}
                  quality={quality === "auto" ? adaptiveDetail : quality}
                  onSelect={select}
                  onError={onRoomError}
                  cameraCommand={
                    roomCameraCommand?.roomId === room.id
                      ? roomCameraCommand
                      : null
                  }
                />
                <div
                  className="ve-camera ve-room-camera"
                  aria-label="Room camera controls"
                >
                  {[
                    ["zoom-in", "+", "Zoom into room"],
                    ["zoom-out", "−", "Zoom out of room"],
                    ["reset", "⌂", "Reset room view"],
                  ].map(([type, icon, label]) => (
                    <button
                      key={type}
                      aria-label={label}
                      title={label}
                      onClick={() =>
                        setRoomCameraCommand((previous) => ({
                          roomId: room.id,
                          type,
                          nonce: (previous?.nonce || 0) + 1,
                        }))
                      }
                    >
                      {icon}
                    </button>
                  ))}
                </div>
                {roomError && (
                  <div
                    className="ve-scene-message ve-scene-error"
                    role="status"
                  >
                    <strong>The room view couldn't open.</strong>
                    <p>The people inside are listed below.</p>
                    <details>
                      <summary>Rendering details</summary>
                      {roomError}
                    </details>
                  </div>
                )}
              </div>
              {room.kind === "workshop" && <WorkshopBoard snapshot={snapshot} onSelectAgent={select} taskRequest={taskRequest} onHighlightAgent={highlightTaskAgent} />}
              {room.kind === "workshop" && <AgentHandoffs snapshot={snapshot} onSelectAgent={id => select({ kind: "agent", id })} />}
              <RoomResidents agents={roomAgents} selectedAgentId={selectedAgent?.id} kind={room.kind} onFocusAgent={id => { setProfileOpen(false); setSelection({ kind: "agent", id }); setFollow(false); setTaskFocusAgentId(id); }} />
              <div className="ve-room-roster" aria-label="People inside">
                {roomAgents.length ? (
                  roomAgents.map((agent) => (
                    <PersonalSpace
                      key={agent.id}
                      agent={agent}
                      room={room}
                      snapshot={snapshot}
                      selected={selectedAgent?.id === agent.id}
                      onSelect={() => select({ kind: "agent", id: agent.id })}
                    />
                  ))
                ) : (
                  <p>No agents are inside right now.</p>
                )}
              </div>
            </section>
          )}
          <div className="ve-canvas" ref={setMapHost} hidden={Boolean(room)}>
            <VillageWorld
              world={world}
              selection={selection}
              onSelect={select}
              paused={paused}
              quality={quality}
              follow={follow}
              cameraCommand={cameraCommand}
              onReady={onReady}
              onCameraChange={setCameraView}
              onQualityChange={setAdaptiveDetail}
              onError={onError}
            />
            {!ready && !error && (
              <div className="ve-scene-message">Opening the village…</div>
            )}
            {error && (
              <div className="ve-scene-message ve-scene-error" role="status">
                <strong>The village view couldn't open.</strong>
                <p>Everyone is still available in the directory.</p>
                <details>
                  <summary>Rendering details</summary>
                  {error}
                </details>
              </div>
            )}
          </div>
          {announcement && (
            <div
              className="ve-announcement"
              role="status"
              aria-label="Village update"
            >
              <span>{announcement.text}</span>
              <button
                onClick={() => setAnnouncement(null)}
                aria-label="Dismiss village update"
              >
                ×
              </button>
            </div>
          )}
          <div className="ve-map-overlay">
            <div className="ve-camera" aria-label="Village camera controls">
              {[
                ["zoom-in", "+", "Zoom in"],
                ["zoom-out", "−", "Zoom out"],
                ["rotate-left", "↶", "Rotate left"],
                ["rotate-right", "↷", "Rotate right"],
                ["reset", "⌂", "Reset camera"],
              ].map(([type, icon, label]) => (
                <button
                  key={type}
                  type="button"
                  aria-label={label}
                  title={label}
                  onClick={() => command(type)}
                >
                  {icon}
                </button>
              ))}
            </div>
          </div>
          {approvals.length > 0 && (
            <div className="ve-attention">
              <span className="ve-attention-mark">!</span>
              <div>
                <strong>
                  {approvals.length}{" "}
                  {approvals.length === 1 ? "request needs" : "requests need"}{" "}
                  your attention
                </strong>
                <span>Waiting in the village square</span>
              </div>
              <button
                onClick={() => {
                  const agent = world.agents.find(
                    (a) => a.id === approvals[0].agent_id,
                  );
                  select(
                    agent
                      ? { kind: "agent", id: agent.id }
                      : {
                          kind: "building",
                          id: world.buildings.find((b) => b.kind === "square")
                            ?.id,
                        },
                  );
                }}
                aria-label="Locate agent needing attention"
              >
                Locate ↗
              </button>
              <a href="#approvals">Review →</a>
            </div>
          )}
          <footer className="ve-world-footer" style={room?.kind === "archive" ? { display: "none" } : undefined}>
            <time
              className="ve-local-time"
              dateTime={localTime.toISOString()}
              title="Your local time"
            >
              {daylightAt(localTime).phase} ·{" "}
              {timeLabel(localTime.toISOString())} local
            </time>
            <span className="ve-navigation-hint">
              Drag to explore · scroll to zoom
            </span>
            <div>
              <button aria-pressed={paused} onClick={() => setPaused(!paused)}>
                {paused ? "Resume motion" : "Pause motion"}
              </button>
              <button aria-pressed={editingLayout} onClick={() => { setEditingLayout(value => !value); setRoomId(null); setFollow(false); }}>Edit layout</button>
              <label className="ve-quality">
                <span className="sr-only">Rendering quality</span>
                <select
                  aria-label="Rendering quality"
                  title="Scenic includes scenery and shadows. Simple hides them. Automatic adjusts to rendering speed."
                  value={quality}
                  onChange={(e) => setQuality(e.target.value)}
                >
                  <option value="high">Scenic</option>
                  <option value="low">Simple</option>
                  <option value="auto">Automatic</option>
                </select>
              </label>
            </div>
          </footer>
          {quality === "auto" && <p className="ve-navigation-hint">Automatic · {adaptiveDetail === "low" ? "simple" : "scenic"} view</p>}
          {editingLayout && <VillageLayoutEditor world={world} onMoveBuilding={(id, position) => changeLayout(() => layout.current.moveBuilding(id, position))} onUndoMove={() => changeLayout(() => layout.current.undoMove())} onReset={() => changeLayout(() => layout.current.resetLayout())} />}
        </section>
        <aside className="ve-sidebar" aria-label="Villagers">
          <header className="ve-directory-heading">
            <p className="ve-kicker">Around the village</p>
            <div className="ve-tabs" aria-label="Village directory">
              <button
                aria-pressed={tab === "people"}
                onClick={() => setTab("people")}
              >
                People <span>{world.agents.length}</span>
              </button>
              <button
                aria-pressed={tab === "projects"}
                onClick={() => setTab("projects")}
              >
                Projects <span>{projects.length}</span>
              </button>
              <button
                aria-pressed={tab === "buildings"}
                onClick={() => setTab("buildings")}
              >
                Buildings <span>{world.buildings.length}</span>
              </button>
            </div>
          </header>
          <label className="ve-search">
            <span aria-hidden="true">⌕</span>
            <span className="sr-only">Find a villager</span>
            <input
              type="search"
              value={query}
              onChange={(e) => setQuery(e.target.value)}
              placeholder="Find an agent, project, or building…"
            />
          </label>
          {tab === "people" && (
            <div className="ve-filters" aria-label="Filter villagers">
              {[
                ["all", "Everyone"],
                ["residents", "Residents"],
                ["working", "Working"],
              ].map(([value, label]) => (
                <button
                  key={value}
                  aria-pressed={filter === value}
                  onClick={() => setFilter(value)}
                >
                  {label}
                </button>
              ))}
            </div>
          )}
          <div
            className={`ve-directory ${selection ? "ve-directory-compact" : ""}`}
          >
            {tab === "people"
              ? people.map((a) => (
                  <button
                    className="person ve-person"
                    key={a.id}
                    aria-pressed={selectedAgent?.id === a.id}
                    onClick={() =>
                      select(
                        selectedAgent?.id === a.id
                          ? null
                          : { kind: "agent", id: a.id },
                      )
                    }
                  >
                    <Portrait agent={a} />
                    <span className="ve-person-copy">
                      <strong>{a.name}</strong>
                      <small>
                        {a.project ||
                          (a.residency === "resident"
                            ? "At home"
                            : "Visitor lodge")}
                      </small>
                    </span>
                    <span className="ve-person-state" data-state={a.state}>
                      <i />
                      {stateLabel(a.state)}
                    </span>
                  </button>
                ))
              : tab === "projects"
                ? visibleProjects.map((b) => (
                    <button
                      className="ve-project"
                      aria-label={`${b.project || b.name} ${b.agentIds.length} ${b.agentIds.length === 1 ? "agent" : "agents"} · Workshop`}
                      key={b.id}
                      aria-pressed={selectedBuilding?.id === workshop?.id}
                      onClick={() =>
                        workshop &&
                        select({ kind: "building", id: workshop.id })
                      }
                    >
                      <span className="ve-building-icon" aria-hidden="true">
                        ⌂
                      </span>
                      <span>
                        <strong>{b.project || b.name}</strong>
                        <small>
                          {b.agentIds.length}{" "}
                          {b.agentIds.length === 1 ? "agent" : "agents"} ·
                          Workshop
                        </small>
                      </span>
                      <span aria-hidden="true">↗</span>
                    </button>
                  ))
                : world.buildings
                    .filter((building) =>
                      `${building.name} ${building.kind}`
                        .toLowerCase()
                        .includes(search),
                    )
                    .map((building) => (
                      <BuildingPreview
                        key={building.id}
                        building={building}
                        world={world}
                        onSelect={select}
                      />
                    ))}
            {tab === "buildings" &&
              !world.buildings.some((building) =>
                `${building.name} ${building.kind}`
                  .toLowerCase()
                  .includes(search),
              ) && <p className="ve-empty">No buildings match this search.</p>}
            {tab === "people" && !people.length && (
              <p className="ve-empty">
                {world.agents.length
                  ? "No villagers match this view."
                  : "The village is quiet. Residents will appear here when Chronicle observes them."}
              </p>
            )}
            {tab === "projects" && !visibleProjects.length && (
              <p className="ve-empty">
                {projects.length
                  ? "No projects match this search."
                  : "Projects appear here when agents work on them."}
              </p>
            )}
          </div>
          <nav className="ve-places" aria-label="Village places">
            {world.buildings
              .filter((b) =>
                [
                  "square",
                  "lodge",
                  "workshop",
                  "archive",
                  "noticeboard",
                ].includes(b.kind),
              )
              .map((b) => (
                <button
                  key={b.id}
                  aria-pressed={selectedBuilding?.id === b.id}
                  onClick={() => select({ kind: "building", id: b.id })}
                >
                  {b.name}
                </button>
              ))}
          </nav>
          {selectedAgent ? (
            <>
            <button className="ve-open-profile" onClick={() => setProfileOpen(true)}>View {selectedAgent.name}’s profile ↗</button>
            {active && profileOpen && <AgentProfile onClose={() => setProfileOpen(false)}>
            <AgentDetails
              detailRef={detailRef}
              agent={selectedAgent}
              snapshot={snapshot}
              stewardClient={stewardClient}
              follow={follow}
              setFollow={value => { setFollow(value); setProfileOpen(false); }}
              onVisitHome={() =>
                select({ kind: "building", id: `home:${selectedAgent.id}` })
              }
            />
            </AgentProfile>}
            </>
          ) : selectedBuilding ? (
            <section
              ref={detailRef}
              className="ve-dossier"
              aria-label="Selected building"
            >
              <div className="ve-detail-heading">
                <span className="ve-building-icon" aria-hidden="true">
                  ⌂
                </span>
                <button
                  className="ve-icon-button"
                  onClick={() => select(null)}
                  aria-label="Close building details"
                >
                  ×
                </button>
              </div>
              <p className="ve-kicker">{selectedBuilding.kind}</p>
              <h3>{selectedBuilding.name}</h3>
              <p className="ve-current">{purposes[selectedBuilding.kind]}</p>
              {world.agents
                .filter((a) =>
                  ["home", "lodge", "workshop"].includes(selectedBuilding.kind)
                    ? a.buildingId === selectedBuilding.id && a.indoor
                    : selectedBuilding.agentIds.includes(a.id),
                )
                .map((a) => (
                  <button
                    className="ve-occupant"
                    key={a.id}
                    onClick={() => select({ kind: "agent", id: a.id })}
                  >
                    <Portrait agent={a} />
                    <span>{a.name}</span>
                    <span>↗</span>
                  </button>
                ))}
              {selectedBuilding.kind === "square" ? (
                <a className="ve-dossier-link" href="#approvals">
                  {approvals.length} pending requests →
                </a>
              ) : (
                <a className="ve-dossier-link" href="#records">
                  Open village records →
                </a>
              )}
            </section>
          ) : (
            <div className="ve-explore-note">
              <span aria-hidden="true">⌂</span>
              <p>
                <strong>Come a little closer.</strong>Select a person or a
                building to discover what's happening.
              </p>
            </div>
          )}
        </aside>
      </div>
      <section className="ve-activity" aria-label="Village activity">
        <button
          className="ve-activity-toggle"
          aria-expanded={feedOpen}
          onClick={() => setFeedOpen(!feedOpen)}
        >
          <span>
            <i className="ve-activity-dot" />
            Village journal <small>Recent observations</small>
          </span>
          <span>{feedOpen ? "Close −" : "Open +"}</span>
        </button>
        {feedOpen && (
          <div className="ve-feed">
            {events.length ? (
              events.map((event) => (
                <button
                  key={event.key}
                  aria-label={`${timeLabel(event.ts)} ${event.agentName} ${event.type.replaceAll("_", " ")}`}
                  onClick={() => select({ kind: "agent", id: event.agentId })}
                >
                  <time dateTime={event.ts} title={event.ts}>
                    {timeLabel(event.ts)}
                  </time>
                  <strong>{event.agentName}</strong>
                  <span>{event.type.replaceAll("_", " ")}</span>
                  <span aria-hidden="true">↗</span>
                </button>
              ))
            ) : (
              <p className="ve-empty">No recent observations recorded.</p>
            )}
            <p className="ve-feed-note">
              Recorded activity from Chronicle. Movement through the village
              illustrates agent state.
            </p>
          </div>
        )}
      </section>
    </div>
  );
}
