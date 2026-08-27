function Empty({ children }) {
  return <p className="panel-empty">{children}</p>;
}

function Panel({ title, note, children, className = "" }) {
  return (
    <section className={`panel ${className}`} aria-label={title}>
      <header className="panel__header">
        <h2>{title}</h2>
        <p>{note}</p>
      </header>
      {children}
    </section>
  );
}

function villagerName(snapshot, agentId) {
  return snapshot.villagers.find((villager) => villager.id === agentId)?.name || agentId;
}

export function ReadOnlyPanels({ snapshot }) {
  return (
    <div className="panels" aria-label="Village records">
      <Panel title="Notice board" note="Recent artifacts across the village" className="panel--notice">
        {snapshot.artifacts.length ? (
          <ul className="record-list">
            {snapshot.artifacts.map((artifact) => (
              <li key={`${artifact.agent_id}:${artifact.ts}:${artifact.artifact}`}>
                <strong>{artifact.artifact}</strong>
                <span>{villagerName(snapshot, artifact.agent_id)} · {artifact.project}</span>
                <time dateTime={artifact.ts}>{artifact.ts}</time>
              </li>
            ))}
          </ul>
        ) : <Empty>Nothing has been produced yet.</Empty>}
      </Panel>

      <Panel title="Job board" note="Steward's current queue" className="panel--jobs">
        {snapshot.tasks.length ? (
          <ul className="record-list">
            {snapshot.tasks.map((task) => (
              <li key={task.id}>
                <strong>{task.title}</strong>
                <span>{task.state} · {task.required_skills.join(", ") || "no required skills"}</span>
                <span>{task.claimant ? villagerName(snapshot, task.claimant) : "Unclaimed"}</span>
              </li>
            ))}
          </ul>
        ) : <Empty>There are no jobs in the queue.</Empty>}
      </Panel>

      <Panel title="Routine ledger" note="Observed routine runs" className="panel--routines">
        {snapshot.routines.length ? (
          <ul className="record-list">
            {snapshot.routines.map((run) => (
              <li key={run.run_id}>
                <strong>{run.routine}</strong>
                <span>{run.state}{run.outcome ? ` · ${run.outcome}` : ""}{run.duration_s !== null ? ` · ${run.duration_s}s` : ""}</span>
                <span>{run.artifacts.join(", ") || "no artifacts"} · {villagerName(snapshot, run.agent_id)}</span>
              </li>
            ))}
          </ul>
        ) : <Empty>No routine runs have been observed.</Empty>}
      </Panel>

      <Panel title="Charter journal" note="Resident declarations" className="panel--charters">
        {snapshot.residents.length ? (
          <ul className="charter-list">
            {snapshot.residents.map((resident) => (
              <li key={resident.file}>
                <h3>{resident.meta.name}</h3>
                <p>{resident.meta.role || "Role not declared"}</p>
                <p>{resident.body || "No charter body declared."}</p>
                <dl className="charter-fields">
                  <div><dt>Manifest</dt><dd>v{resident.manifest_version} · {resident.file}</dd></div>
                  <div><dt>Match</dt><dd>{resident.match.agent_id || resident.match.project || "Not declared"}</dd></div>
                  <div><dt>Home</dt><dd>{resident.home}</dd></div>
                  <div>
                    <dt>Capabilities</dt>
                    <dd>{Object.entries(resident.capabilities).map(([name, value]) => (
                      <span key={name}>{name}: {Array.isArray(value) ? value.join(", ") : JSON.stringify(value)}</span>
                    ))}</dd>
                  </div>
                  <div>
                    <dt>Routines</dt>
                    <dd>{resident.routines.length ? resident.routines.map((routine) => routine.id).join(", ") : "None declared"}</dd>
                  </div>
                </dl>
              </li>
            ))}
          </ul>
        ) : <Empty>No resident charters are available.</Empty>}
      </Panel>

      <Panel title="Journal observations" note="Per-agent journal records" className="panel--journals">
        {snapshot.journals.length ? (
          <ul className="record-list">
            {snapshot.journals.map((journal) => (
              <li key={`${journal.agent_id}:${journal.day}`}>
                <strong>{villagerName(snapshot, journal.agent_id)}</strong>
                <span>{journal.day} · {journal.routine}</span>
                <span>{journal.path}</span>
              </li>
            ))}
          </ul>
        ) : <Empty>No journal observations have been recorded.</Empty>}
      </Panel>
    </div>
  );
}
