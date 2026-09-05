import { useMemo } from "react";
import { buildAgentHandoffs } from "../world/handoffs.js";
import "./agent-handoffs.css";

const labels = {
  open: "Pending · task open",
  claimed: "Picked up",
  done: "Completion recorded",
  failed: "Failure recorded",
  unconfirmed: "Delivery recorded · current state unconfirmed",
};
function AgentLink({ id, snapshot, onSelectAgent }) {
  const agent = snapshot.villagers.find((item) => item.id === id);
  return agent && onSelectAgent ? (
    <button onClick={() => onSelectAgent(id)}>{agent.name}</button>
  ) : (
    <span title={id}>{agent?.name || id}</span>
  );
}
export function AgentHandoffs({ snapshot, onSelectAgent, agentId = null }) {
  const model = useMemo(
    () => buildAgentHandoffs(snapshot, agentId),
    [snapshot, agentId],
  );
  const link = (id) => (
    <AgentLink id={id} snapshot={snapshot} onSelectAgent={onSelectAgent} />
  );
  if (
    !model.handoffs.length &&
    !model.blocked.length &&
    !model.lineage.length &&
    !model.unlinked.length
  )
    return (
      <section
        className="agent-handoffs handoffs-quiet"
        aria-label="Recorded agent handoffs"
      >
        <p>
          No explicit agent-to-agent handoffs are retained in this snapshot.
        </p>
        <small>Accepted handoffs and recorded outcomes will appear here.</small>
      </section>
    );
  return (
    <section className="agent-handoffs" aria-label="Recorded agent handoffs">
      <header>
        <h3>Work passed between agents</h3>
        <p>
          Retained records only. Missing events do not imply a reply or a
          completed task. Reply delivery and message text are not recorded in
          this view.
        </p>
      </header>
      {model.handoffs.length ? (
        <ol className="handoff-list">
          {model.handoffs.map((item) => (
            <li key={item.id}>
              <div className="handoff-people">
                {link(item.from)}
                <span aria-label="delegated to">→</span>
                {link(item.to)}
                <small data-state={item.state}>{labels[item.state]}</small>
              </div>
              <h4>{item.title}</h4>
              <p className="handoff-meta">
                Task <code>{item.id}</code>
                {item.parentTaskId && (
                  <>
                    {" "}
                    · Parent task <code>{item.parentTaskId}</code>
                  </>
                )}{" "}
                · Route {item.route}
              </p>
              <details>
                <summary>
                  Recorded steps · {1 + item.transitions.length}
                </summary>
                <ol>
                  <li>
                    <time dateTime={item.origin.ts}>{item.origin.ts}</time>{" "}
                    Handoff accepted
                  </li>
                  {item.transitions.map((event) => (
                    <li key={event.key}>
                      <time dateTime={event.ts}>{event.ts}</time>{" "}
                      {event.type === "task_session_finished"
                        ? "Session reported after losing its claim; task state unchanged"
                        : event.type === "task_failed" &&
                            event.payload.reason?.trim() === "lease_expired"
                          ? "Lease expired; task reopened"
                          : event.type.replaceAll("_", " ")}
                      {event.payload.run_id && (
                        <small> · Run {event.payload.run_id}</small>
                      )}
                    </li>
                  ))}
                </ol>
              </details>
              {item.result && (
                <div className="handoff-result">
                  {item.result.reason && <p>{item.result.reason}</p>}
                  {item.result.artifacts.length > 0 && (
                    <ul>
                      {item.result.artifacts
                        .slice(0, 3)
                        .map((artifact, index) => (
                          <li key={`${index}:${artifact}`}>{artifact}</li>
                        ))}
                    </ul>
                  )}
                  {item.result.artifacts.length > 3 && (
                    <details>
                      <summary>
                        {item.result.artifacts.length - 3} more recorded{" "}
                        {item.result.artifacts.length === 4
                          ? "output"
                          : "outputs"}
                      </summary>
                      <ul>
                        {item.result.artifacts
                          .slice(3)
                          .map((artifact, index) => (
                            <li key={`${index}:${artifact}`}>{artifact}</li>
                          ))}
                      </ul>
                    </details>
                  )}
                </div>
              )}
            </li>
          ))}
        </ol>
      ) : (
        <p className="handoff-empty">
          No explicit agent-to-agent handoffs are retained in this snapshot.
        </p>
      )}
      {model.blocked.length > 0 && (
        <section
          className="handoff-blocked"
          aria-label="Blocked handoff attempts"
        >
          <h4>Attempts that were not delivered</h4>
          {model.blocked.map((item) => (
            <article key={item.id}>
              <div className="handoff-people">
                {link(item.from)}
                <small>Not delivered</small>
              </div>
              <strong>{item.title}</strong>
              {item.recipient && (
                <p>
                  Requested resident: {item.recipient} · identity not linked
                </p>
              )}
              <p>{item.reason}</p>
              <a
                href={
                  item.state === "pending"
                    ? `#approval-${encodeURIComponent(item.id)}`
                    : "#records"
                }
              >
                Approval request {item.id} · {item.state}
              </a>
            </article>
          ))}
        </section>
      )}
      {model.lineage.length > 0 && (
        <details className="handoff-secondary">
          <summary>Explicit session lineage · {model.lineage.length}</summary>
          <p>
            Parent and child session identities; no task assignment or reply is
            inferred.
          </p>
          {model.lineage.map((item) => (
            <div className="handoff-people" key={item.id}>
              {link(item.parent)}
              <span>→</span>
              {link(item.child)}
            </div>
          ))}
        </details>
      )}
      {model.unlinked.length > 0 && (
        <details className="handoff-secondary">
          <summary>Unlinked task records · {model.unlinked.length}</summary>
          <ul>
            {model.unlinked.slice(0, 20).map((item) => (
              <li key={item.id}>
                <strong>{item.event.type.replaceAll("_", " ")}</strong> ·{" "}
                {item.event.payload.task_id || "No task ID"}
                <p>{item.reason}</p>
              </li>
            ))}
          </ul>
          {model.unlinked.length > 20 && (
            <p>
              Showing 20 of {model.unlinked.length} retained unmatched records.
            </p>
          )}
        </details>
      )}
    </section>
  );
}
