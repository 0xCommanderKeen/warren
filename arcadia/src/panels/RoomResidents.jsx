import { useMemo, useState } from "react";
import "./room-residents.css";

export function RoomResidents({ agents, selectedAgentId, onFocusAgent, kind }) {
  const [query, setQuery] = useState("");
  const people = useMemo(
    () =>
      [...agents].sort(
        (a, b) => a.name.localeCompare(b.name) || a.id.localeCompare(b.id),
      ),
    [agents],
  );
  const selectedIndex = people.findIndex(
    (agent) => agent.id === selectedAgentId,
  );
  const matches = people.filter((agent) =>
    `${agent.name} ${agent.project || ""}`
      .toLowerCase()
      .includes(query.trim().toLowerCase()),
  );
  const groups = useMemo(() => {
    const grouped = new Map();
    for (const agent of people) {
      const project = agent.project || null;
      if (!grouped.has(project)) grouped.set(project, []);
      grouped.get(project).push(agent);
    }
    return [...grouped.entries()];
  }, [people]);
  const cycle = (direction) => {
    const next =
      selectedIndex < 0
        ? direction === 1
          ? 0
          : people.length - 1
        : (selectedIndex + direction + people.length) % people.length;
    onFocusAgent(people[next].id);
  };
  if (!people.length) return null;
  return (
    <section className="room-residents" aria-label="Room resident navigation">
      <div className="room-residents-toolbar">
        <p>
          {kind === "lodge" ? "Guests in the lodge" : "Around the room"}
          <span>{people.length}</span>
        </p>
        {people.length > 1 && (
          <div className="room-resident-step">
            <button
              aria-label="Focus previous resident"
              title="Previous resident"
              onClick={() => cycle(-1)}
            >
              ←
            </button>
            <span>
              {selectedIndex >= 0
                ? `${selectedIndex + 1} / ${people.length}`
                : `${people.length} people`}
            </span>
            <button
              aria-label="Focus next resident"
              title="Next resident"
              onClick={() => cycle(1)}
            >
              →
            </button>
          </div>
        )}
      </div>
      {people.length > 12 && (
        <div className="room-resident-find">
          <label>
            <span>Find someone in this room</span>
            <input
              type="search"
              value={query}
              onChange={(event) => setQuery(event.target.value)}
              placeholder="Name or project…"
            />
          </label>
          <label>
            <span className="sr-only">Focus a room resident</span>
            <select
              value={
                matches.some((agent) => agent.id === selectedAgentId)
                  ? selectedAgentId
                  : ""
              }
              onChange={(event) => {
                if (event.target.value) onFocusAgent(event.target.value);
              }}
            >
              <option value="">
                {matches.length
                  ? `Choose a resident · ${matches.length}`
                  : "No matching residents"}
              </option>
              {matches.map((agent) => (
                <option key={agent.id} value={agent.id}>
                  {agent.name}
                  {agent.project ? ` · ${agent.project}` : ""}
                </option>
              ))}
            </select>
          </label>
        </div>
      )}
      {kind === "lodge" && (
        <div
          className="room-project-groups"
          aria-label="Guests by recorded project"
        >
          {groups.map(([project, members]) => (
            <details key={JSON.stringify(project)}>
              <summary>
                <span>{project || "No project recorded"}</span>
                <small>{members.length}</small>
              </summary>
              <div>
                {members.map((agent) => (
                  <button
                    key={agent.id}
                    aria-pressed={selectedAgentId === agent.id}
                    onClick={() => onFocusAgent(agent.id)}
                  >
                    {agent.name}
                  </button>
                ))}
              </div>
            </details>
          ))}
        </div>
      )}
    </section>
  );
}
