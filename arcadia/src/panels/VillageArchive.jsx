import { ArtifactPreview } from "./ArtifactPreview.jsx";
import { useMemo, useState } from "react";
import "./village-archive.css";

const typeNames = { task: "Completed task", artifact: "Artifact", journal: "Journal entry" };
const dateValue = value => Number.isFinite(Date.parse(value)) ? Date.parse(value) : -Infinity;
const recentFirst = (a, b) => dateValue(b.ts) - dateValue(a.ts) || a.key.localeCompare(b.key);
const fileName = path => path.split(/[\\/]/).filter(Boolean).at(-1) || path;
function externalUrl(value) {
  try {
    const url = new URL(value);
    return ["https:", "http:"].includes(url.protocol) ? url.href : null;
  } catch { return null; }
}
function RecordedTime({ value }) {
  return dateValue(value) === -Infinity ? <span>Time not recorded</span> : <time dateTime={value} title={value}>{new Date(value).toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" })}</time>;
}
function AgentName({ id, names, onSelectAgent }) {
  if (!id) return <span>Claimant not recorded</span>;
  const name = names.get(id);
  return name && onSelectAgent ? <button type="button" className="va-agent" onClick={() => onSelectAgent({ kind: "agent", id })}>{name} ↗</button> : <span>{name || id}</span>;
}
function PathActions({ path, canOpen }) {
  const [notice, setNotice] = useState(null);
  const link = canOpen ? externalUrl(path) : null;
  async function copy() {
    try {
      if (!navigator.clipboard?.writeText) throw new Error("unavailable");
      await navigator.clipboard.writeText(path);
      setNotice({ ok: true, text: "Path copied." });
    } catch {
      setNotice({ ok: false, text: "Couldn't copy. Select the path above and copy it manually." });
    }
  }
  return <><div className="va-path-actions"><button type="button" onClick={copy}>Copy path</button>{link && <a href={link} target="_blank" rel="noopener noreferrer">Open artifact ↗</a>}</div><p className={`va-copy-status ${notice?.ok === false ? "va-copy-error" : ""}`} role="status">{notice?.text || ""}</p></>;
}
function ArchiveRecord({ record, names, onSelectAgent }) {
  const titleId = `archive-record-${encodeURIComponent(record.key)}`;
  return <article className={`va-record va-record-${record.kind}`} aria-labelledby={titleId}>
    <div className="va-record-heading"><span className="va-record-symbol" aria-hidden="true">{record.kind === "task" ? "✓" : record.kind === "artifact" ? "◇" : "≡"}</span><div><p className="va-record-type">{typeNames[record.kind]}</p><h4 id={titleId}>{record.title}</h4></div></div>
    <div className="va-record-meta"><span>{record.kind === "task" ? "Claimed by " : "Recorded by "}<AgentName id={record.agentId} names={names} onSelectAgent={onSelectAgent} /></span><span><RecordedTime value={record.ts} /></span></div>
    <details className="va-record-details"><summary>{record.kind === "artifact" ? "Inspect artifact" : "Record details"}</summary>
      <dl><dt>{record.kind === "task" ? "Last updated" : "Observed"}</dt><dd><RecordedTime value={record.ts} /></dd>
        {record.kind === "task" ? <><dt>Task ID</dt><dd>{record.source.id}</dd><dt>Required skills</dt><dd>{record.source.required_skills.join(", ") || "None recorded"}</dd><dt>Posted by</dt><dd>{names.get(record.source.posted_by) || record.source.posted_by}</dd><dt>Outputs</dt><dd>No outputs linked in this record.</dd></> : <><dt>Full path</dt><dd className="va-path">{record.path}</dd><dt>Project</dt><dd>{record.project || "Not recorded"}</dd>{record.kind === "journal" && <><dt>Routine</dt><dd>{record.source.routine || "Not recorded"}</dd><dt>Source</dt><dd>{record.source.source || "Not recorded"}</dd></>}</>}
      </dl>
      {record.path && <><p className="va-metadata-note">This archive contains the recorded path and metadata. Preview availability depends on the archive server.</p><PathActions path={record.path} canOpen={record.kind === "artifact"} />{record.kind === "artifact" && <ArtifactPreview artifact={record.source} />}</>}
    </details>
  </article>;
}

export function VillageArchive({ snapshot, onSelectAgent, onBack }) {
  const [query, setQuery] = useState("");
  const [project, setProject] = useState("all");
  const [kind, setKind] = useState("all");
  const names = useMemo(() => new Map(snapshot.villagers.map(agent => [agent.id, agent.name])), [snapshot.villagers]);
  const records = useMemo(() => [
    // Version-1 tasks contain neither project nor output references. Agent identity
    // alone does not establish a task/artifact association or a historical project.
    ...snapshot.tasks.filter(task => task.state === "done").map(task => ({ key: `task:${task.id}`, kind: "task", title: task.title, agentId: task.claimant, project: "", ts: task.updated_at, source: task })),
    ...snapshot.artifacts.map((artifact, index) => ({ key: `artifact:${artifact.agent_id}:${artifact.ts}:${artifact.artifact}:${index}`, kind: "artifact", title: fileName(artifact.artifact), path: artifact.artifact, agentId: artifact.agent_id, project: artifact.project, ts: artifact.ts, source: artifact })),
    ...snapshot.journals.map((journal, index) => ({ key: `journal:${journal.agent_id}:${journal.observed_at}:${journal.path}:${index}`, kind: "journal", title: `${journal.day} · ${journal.routine || "Journal"}`, path: journal.path, agentId: journal.agent_id, project: journal.project, ts: journal.observed_at, source: journal })),
  ].sort(recentFirst), [snapshot.tasks, snapshot.artifacts, snapshot.journals]);
  const projects = [...new Set(records.map(record => record.project).filter(Boolean))].sort((a, b) => a.localeCompare(b));
  const effectiveProject = project === "all" || (project === "unrecorded" && records.some(record => !record.project)) || projects.some(name => `project:${name}` === project) ? project : "all";
  const search = query.trim().toLowerCase();
  const filtered = records.filter(record => (kind === "all" || kind === record.kind) &&
    (effectiveProject === "all" || (effectiveProject === "unrecorded" ? !record.project : `project:${record.project}` === effectiveProject)) &&
    [record.title, record.path, record.project, record.agentId, names.get(record.agentId), record.source.id].filter(Boolean).join(" ").toLowerCase().includes(search));
  const groups = new Map();
  for (const record of filtered) {
    if (!groups.has(record.project)) groups.set(record.project, []);
    groups.get(record.project).push(record);
  }
  return <section className="va-archive" aria-label="Village archive">
    <header className="va-header"><div><p className="va-kicker">The village archive</p><h3>Work worth keeping.</h3><p>Completed tasks, artifacts, and journal observations from Warren.</p></div><button type="button" className="va-back" aria-label="Back to village" onClick={onBack}>← Back to village</button></header>
    <div className="va-tools"><label className="va-search"><span>Search the archive</span><input type="search" placeholder="A filename, task, project, or agent…" value={query} onChange={event => setQuery(event.target.value)} /></label><label className="va-project-filter"><span>Project</span><select value={effectiveProject} onChange={event => setProject(event.target.value)}><option value="all">All projects</option>{projects.map(name => <option key={name} value={`project:${name}`}>{name}</option>)}{records.some(record => !record.project) && <option value="unrecorded">Project not recorded</option>}</select></label></div>
    <div className="va-filter-row"><div className="va-kinds" aria-label="Archive record types">{[["all", "Everything"], ["task", "Completed tasks"], ["artifact", "Artifacts"], ["journal", "Journals"]].map(([value, label]) => <button type="button" key={value} aria-pressed={kind === value} onClick={() => setKind(value)}>{label}</button>)}</div><p className="va-count" role="status">{filtered.length} {filtered.length === 1 ? "record" : "records"}</p></div>
    {!records.length ? <div className="va-empty"><span aria-hidden="true">◇</span><h4>The shelves are waiting.</h4><p>Completed tasks, recorded artifacts, and journal entries will appear here.</p></div> : !filtered.length ? <div className="va-empty"><h4>No matching records.</h4><p>Try another search or project.</p><button type="button" onClick={() => { setQuery(""); setProject("all"); setKind("all"); }}>Clear filters</button></div> : <div className="va-groups">{[...groups].map(([name, entries]) => <section className="va-project-group" aria-label={name || "Project not recorded"} key={name}><div className="va-group-heading"><h3>{name || "Project not recorded"}</h3><span>{entries.length} {entries.length === 1 ? "record" : "records"} · newest first</span></div><div className="va-records">{entries.map(record => <ArchiveRecord record={record} names={names} onSelectAgent={onSelectAgent} key={record.key} />)}</div></section>)}</div>}
  </section>;
}
