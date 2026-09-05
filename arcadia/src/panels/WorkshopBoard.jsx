import { TelemetryWarning } from "./TelemetryWarning.jsx";
import { useEffect, useId, useMemo, useRef, useState } from "react";
import "./workshop-board.css";

const columns = [
  { id: "open", title: "Queued", note: "Open tasks", symbol: "○" },
  { id: "claimed", title: "Active", note: "Claimed tasks", symbol: "◒" },
  { id: "failed", title: "Needs attention", note: "Failed tasks", symbol: "!" },
  { id: "done", title: "Completed", note: "Done tasks", symbol: "✓" },
];
const stateNames = {
  open: "Open",
  claimed: "Claimed",
  failed: "Failed",
  done: "Done",
};
const timestamp = (value) =>
  Number.isFinite(Date.parse(value)) ? Date.parse(value) : -Infinity;
function Updated({ value }) {
  return timestamp(value) === -Infinity ? (
    <span>Time not recorded</span>
  ) : (
    <time dateTime={value} title={value}>
      {new Date(value).toLocaleString(undefined, {
        dateStyle: "medium",
        timeStyle: "short",
      })}
    </time>
  );
}
function AgentRecord({ id, people, absentLabel, role, onSelectAgent }) {
  if (!id) return <span>{absentLabel}</span>;
  const agent = people.get(id);
  return (
    <span className="wb-person">
      <span>
        {agent?.name || id}
        {!agent && <small>Not in the current village</small>}
      </span>
      {agent && onSelectAgent && (
        <button
          type="button"
          className="wb-locate"
          aria-label={`Locate ${role} ${agent.name}`}
          onClick={() => onSelectAgent({ kind: "agent", id })}
        >
          Locate ↗
        </button>
      )}
    </span>
  );
}

/** Read-only task state from Chronicle; claiming work does not imply agent activity. */
export function WorkshopBoard({
  snapshot,
  onSelectAgent,
  onHighlightAgent,
  taskRequest,
}) {
  const [expanded, setExpanded] = useState(true);
  const [query, setQuery] = useState("");
  const [selectedId, setSelectedId] = useState(null);
  const prefix = useId();
  const bodyId = `${prefix}-board`;
  const detailId = `${prefix}-task`;
  const people = useMemo(
    () => new Map(snapshot.villagers.map((agent) => [agent.id, agent])),
    [snapshot.villagers],
  );
  const search = query.trim().toLowerCase();
  const tasks = useMemo(
    () =>
      [...snapshot.tasks].sort(
        (a, b) =>
          timestamp(b.updated_at) - timestamp(a.updated_at) ||
          a.id.localeCompare(b.id),
      ),
    [snapshot.tasks],
  );
  const filtered = tasks.filter((task) =>
    [
      task.id,
      task.title,
      task.state,
      task.claimant,
      task.assignee,
      people.get(task.claimant)?.name,
      people.get(task.assignee)?.name,
      ...task.required_skills,
    ]
      .filter(Boolean)
      .join(" ")
      .toLowerCase()
      .includes(search),
  );
  const selected = tasks.find((task) => task.id === selectedId);
  const detailRef = useRef(null);
  const highlight = useRef(onHighlightAgent);
  highlight.current = onHighlightAgent;
  useEffect(() => {
    if (selectedId && !selected) {
      setSelectedId(null);
      highlight.current?.(null);
    }
  }, [selectedId, selected]);
  useEffect(() => {
    if (expanded && selectedId)
      detailRef.current?.scrollIntoView?.({
        block: "nearest",
        behavior: globalThis.matchMedia?.("(prefers-reduced-motion: reduce)")
          .matches
          ? "auto"
          : "smooth",
      });
  }, [selectedId, expanded]);
  const handledRequest = useRef(null);
  function agentFor(task) {
    const id = [task?.claimant, task?.assignee].find(
      (candidate) => candidate && people.has(candidate),
    );
    return id ? { kind: "agent", id } : null;
  }
  function selectTask(task) {
    setSelectedId(task?.id || null);
    highlight.current?.(agentFor(task));
  }
  useEffect(() => {
    if (!taskRequest?.id) {
      handledRequest.current = null;
      return;
    }
    const key = JSON.stringify([taskRequest.id, taskRequest.nonce]);
    if (handledRequest.current === key) return;
    const requested = tasks.find((task) => task.id === taskRequest.id);
    if (!requested) return;
    handledRequest.current = key;
    setExpanded(true);
    setQuery("");
    setSelectedId(requested.id);
    const id = [requested.claimant, requested.assignee].find(
      (candidate) => candidate && people.has(candidate),
    );
    highlight.current?.(id ? { kind: "agent", id } : null);
  }, [taskRequest?.id, taskRequest?.nonce, tasks, people]);
  const unknown = filtered.filter(
    (task) => !columns.some((column) => column.id === task.state),
  );
  const shownColumns = unknown.length
    ? [
        ...columns,
        {
          id: "other",
          title: "Other recorded states",
          note: "As recorded",
          symbol: "·",
        },
      ]
    : columns;
  return (
    <section className="wb-board" aria-label="Workshop task board">
      <TelemetryWarning snapshot={snapshot} />
      <header className="wb-header">
        <div>
          <p className="wb-kicker">Pinned in the workshop · across Warren</p>
          <h3>On the workbench</h3>
          <p>
            {tasks.length} recorded {tasks.length === 1 ? "task" : "tasks"}
          </p>
        </div>
        <button
          type="button"
          className="wb-toggle"
          aria-label={expanded ? "Collapse task board" : "Expand task board"}
          aria-expanded={expanded}
          aria-controls={bodyId}
          onClick={() => setExpanded(!expanded)}
        >
          {expanded ? "Fold away −" : "Open board +"}
        </button>
      </header>
      {expanded && (
        <div id={bodyId} className="wb-body">
          <label className="wb-search">
            <span>Search workshop tasks</span>
            <input
              type="search"
              placeholder="Find a task, person, or skill…"
              value={query}
              onChange={(event) => setQuery(event.target.value)}
            />
          </label>
          {filtered.length > 0 && (
            <p className="wb-scroll-hint">Swipe across to view task states.</p>
          )}
          {!tasks.length ? (
            <div className="wb-empty">
              <span aria-hidden="true">○</span>
              <h4>A clear workbench.</h4>
              <p>Tasks will appear here when they are recorded in Warren.</p>
            </div>
          ) : !filtered.length ? (
            <div className="wb-empty">
              <h4>No matching tasks.</h4>
              <button type="button" onClick={() => setQuery("")}>
                Clear search
              </button>
            </div>
          ) : (
            <div className="wb-columns">
              {shownColumns.map((column) => {
                const entries =
                  column.id === "other"
                    ? unknown
                    : filtered.filter((task) => task.state === column.id);
                return (
                  <section
                    className={`wb-column wb-column-${column.id}`}
                    aria-label={column.title}
                    key={column.id}
                  >
                    <header>
                      <h4>
                        <span aria-hidden="true">{column.symbol}</span>
                        {column.title}
                        <small>{entries.length}</small>
                      </h4>
                      <p>{column.note}</p>
                    </header>
                    {entries.length ? (
                      <ul>
                        {entries.map((task) => (
                          <li key={task.id}>
                            <button
                              type="button"
                              className="wb-task"
                              aria-expanded={selectedId === task.id}
                              aria-controls={
                                selectedId === task.id ? detailId : undefined
                              }
                              onClick={() =>
                                selectTask(selectedId === task.id ? null : task)
                              }
                            >
                              <strong>{task.title}</strong>
                              <span className="wb-task-person">
                                {task.claimant
                                  ? people.get(task.claimant)?.name ||
                                    task.claimant
                                  : "Unclaimed"}
                              </span>
                              <span className="wb-task-bottom">
                                <span className="wb-state">
                                  {stateNames[task.state] || task.state}
                                </span>
                                <span aria-hidden="true">↗</span>
                              </span>
                            </button>
                          </li>
                        ))}
                      </ul>
                    ) : (
                      <p className="wb-column-empty">Nothing here.</p>
                    )}
                  </section>
                );
              })}
            </div>
          )}
          {selected && (
            <section
              ref={detailRef}
              id={detailId}
              className="wb-detail"
              aria-label="Selected task"
            >
              <header>
                <div>
                  <p className="wb-kicker">
                    Task record · {stateNames[selected.state] || selected.state}
                  </p>
                  <h4>{selected.title}</h4>
                </div>
                <button
                  type="button"
                  aria-label="Close task details"
                  onClick={() => selectTask(null)}
                >
                  ×
                </button>
              </header>
              <dl>
                <dt>Task ID</dt>
                <dd>{selected.id}</dd>
                <dt>State</dt>
                <dd>{selected.state}</dd>
                <dt>Claimant</dt>
                <dd>
                  <AgentRecord
                    id={selected.claimant}
                    people={people}
                    absentLabel="Unclaimed"
                    role="claimant"
                    onSelectAgent={onSelectAgent}
                  />
                </dd>
                <dt>Assigned to</dt>
                <dd>
                  <AgentRecord
                    id={selected.assignee}
                    people={people}
                    absentLabel="Not assigned"
                    role="assigned agent"
                    onSelectAgent={onSelectAgent}
                  />
                </dd>
                <dt>Required skills</dt>
                <dd>
                  {selected.required_skills.join(", ") || "None recorded"}
                </dd>
                <dt>Posted by</dt>
                <dd>
                  {people.get(selected.posted_by)?.name || selected.posted_by}
                </dd>
                <dt>Last updated</dt>
                <dd>
                  <Updated value={selected.updated_at} />
                </dd>
              </dl>
            </section>
          )}
        </div>
      )}
    </section>
  );
}
