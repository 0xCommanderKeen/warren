const recordList = "grid list-none gap-3 p-0";
const record = "grid gap-0.5 border-l-[3px] border-[#d2a15c] pl-2.5";
const metadata = "font-mono text-xs text-[#566158]";

function Empty({ children }) {
  return <p className={`${metadata} leading-6`}>{children}</p>;
}

function Panel({ title, note, children, wide = false }) {
  return (
    <section className={`${wide ? "col-span-3" : "col-span-2"} min-w-0 border border-[#1d3328] bg-[#faf6eb] p-4 shadow-[4px_4px_0_#1d3328] max-md:col-span-1`} aria-label={title}>
      <header className="mb-4 border-b border-[#b7aa8c] pb-2.5">
        <h2 className="text-xl font-normal">{title}</h2>
        <p className={metadata}>{note}</p>
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
    <div className="mx-auto mt-8 grid max-w-7xl grid-cols-6 gap-4 max-md:grid-cols-2 max-sm:grid-cols-1" aria-label="Village records">
      <Panel title="Notice board" note="Recent artifacts across the village">
        {snapshot.artifacts.length ? <ul className={recordList}>{snapshot.artifacts.map((artifact) => (
          <li className={record} key={`${artifact.agent_id}:${artifact.ts}:${artifact.artifact}`}>
            <strong className="[overflow-wrap:anywhere] font-normal">{artifact.artifact}</strong>
            <span className={metadata}>{villagerName(snapshot, artifact.agent_id)} · {artifact.project}</span>
            <time className={`${metadata} [overflow-wrap:anywhere]`} dateTime={artifact.ts}>{artifact.ts}</time>
          </li>
        ))}</ul> : <Empty>Nothing has been produced yet.</Empty>}
      </Panel>

      <Panel title="Job board" note="Steward's current queue">
        {snapshot.tasks.length ? <ul className={recordList}>{snapshot.tasks.map((task) => (
          <li className={record} key={task.id}>
            <strong className="font-normal">{task.title}</strong>
            <span className={metadata}>{task.state} · {task.required_skills.join(", ") || "no required skills"}</span>
            <span className={metadata}>{task.claimant ? villagerName(snapshot, task.claimant) : "Unclaimed"}</span>
          </li>
        ))}</ul> : <Empty>There are no jobs in the queue.</Empty>}
      </Panel>

      <Panel title="Routine ledger" note="Observed routine runs">
        {snapshot.routines.length ? <ul className={recordList}>{snapshot.routines.map((run) => (
          <li className={record} key={run.run_id}>
            <strong className="font-normal">{run.routine}</strong>
            <span className={metadata}>{run.state}{run.outcome ? ` · ${run.outcome}` : ""}{run.duration_s !== null ? ` · ${run.duration_s}s` : ""}</span>
            <span className={metadata}>{run.artifacts.join(", ") || "no artifacts"} · {villagerName(snapshot, run.agent_id)}</span>
          </li>
        ))}</ul> : <Empty>No routine runs have been observed.</Empty>}
      </Panel>

      <Panel title="Charter journal" note="Resident declarations" wide>
        {snapshot.residents.length ? <ul className={recordList}>{snapshot.residents.map((resident) => (
          <li className="border-l-[3px] border-[#6aa84f] pl-2.5" key={resident.file}>
            <h3 className="text-base font-normal">{resident.meta.name}</h3>
            <p className={`${metadata} mt-1`}>{resident.meta.role || "Role not declared"}</p>
            <p className="mt-1">{resident.body || "No charter body declared."}</p>
            <dl className="mt-3 grid gap-1.5 font-mono text-xs">
              <CharterField name="Manifest">v{resident.manifest_version} · {resident.file}</CharterField>
              <CharterField name="Match">{resident.match.agent_id || resident.match.project || "Not declared"}</CharterField>
              <CharterField name="Home">{resident.home}</CharterField>
              <CharterField name="Capabilities">{Object.entries(resident.capabilities).map(([name, value]) => (
                <span className="block" key={name}>{name}: {Array.isArray(value) ? value.join(", ") : JSON.stringify(value)}</span>
              ))}</CharterField>
              <CharterField name="Routines">{resident.routines.length ? resident.routines.map((routine) => routine.id).join(", ") : "None declared"}</CharterField>
            </dl>
          </li>
        ))}</ul> : <Empty>No resident charters are available.</Empty>}
      </Panel>

      <Panel title="Journal observations" note="Per-agent journal records" wide>
        {snapshot.journals.length ? <ul className={recordList}>{snapshot.journals.map((journal) => (
          <li className={record} key={`${journal.agent_id}:${journal.day}`}>
            <strong className="font-normal">{villagerName(snapshot, journal.agent_id)}</strong>
            <span className={metadata}>{journal.day} · {journal.routine}</span>
            <span className={`${metadata} [overflow-wrap:anywhere]`}>{journal.path}</span>
          </li>
        ))}</ul> : <Empty>No journal observations have been recorded.</Empty>}
      </Panel>
    </div>
  );
}

function CharterField({ name, children }) {
  return (
    <div className="grid grid-cols-[minmax(6rem,auto)_1fr] gap-2">
      <dt className="text-[#785a25]">{name}</dt>
      <dd className="m-0 [overflow-wrap:anywhere]">{children}</dd>
    </div>
  );
}
