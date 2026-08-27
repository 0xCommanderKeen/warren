import { useEffect, useMemo, useState } from "react";
import { agentUrl, agentUuid, payloadSummary, related, routeAgent, viewModel } from "./model.js";
import { createStateTransport } from "./transport.js";

const fallback = (value, alternative = "—") => value || alternative;
const formatTime = (value) => {
  const parsed = Date.parse(value);
  return Number.isFinite(parsed) ? new Date(parsed).toLocaleString() : "—";
};

function usePathname() {
  const [pathname, setPathname] = useState(window.location.pathname);
  useEffect(() => {
    const update = () => setPathname(window.location.pathname);
    window.addEventListener("popstate", update);
    return () => window.removeEventListener("popstate", update);
  }, []);
  const navigate = (path) => {
    window.history.pushState({}, "", path);
    setPathname(path);
    window.scrollTo?.(0, 0);
  };
  return [pathname, navigate];
}

function useFleetState() {
  const [snapshot, setSnapshot] = useState(null);
  const [status, setStatus] = useState("disconnected");
  useEffect(() => {
    const baseUrl = new URLSearchParams(window.location.search).get("backend") || "";
    const transport = createStateTransport({
      fetch: window.fetch.bind(window),
      EventSource: window.EventSource,
      baseUrl,
      onState: setSnapshot,
      onStatus: setStatus,
      warn: (message) => console.warn("Burrow Observatory:", message),
    });
    transport.poll().then(() => transport.connect());
    return () => transport.close();
  }, []);
  return { snapshot, status };
}

function Connection({ status, evaluatedAt }) {
  const time = evaluatedAt
    ? new Date(evaluatedAt).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" })
    : "NO SIGNAL";
  return (
    <div className={`connection ${status}`} aria-live="polite">
      <span className="connection-dot" />
      <span>{status.toUpperCase()}</span>
      <span>{time}</span>
    </div>
  );
}

function EventRows({ events }) {
  if (!events.length) return <p className="empty">No retained activity in this snapshot.</p>;
  return events.map((event) => (
    <div className="event-row" data-agent={event.agent_id} key={event._stable || `${event.agent_id}:${event.ts}:${event.type}`}>
      <time className="event-time">{formatTime(event.ts)}</time>
      <b className="event-agent">{fallback(event.agent_name, event.agent_id)}</b>
      <span className="event-type">{fallback(event.type)}</span>
      <span className="event-source">{fallback(event.source)}</span>
      <span className="event-payload" title={payloadSummary(event)}>{fallback(payloadSummary(event), "No payload summary")}</span>
    </div>
  ));
}

function FleetPulse({ people, onOpen, navigate }) {
  return (
    <div className="fleet-pulse" aria-label="Fleet pulse visualization">
      <span className="pulse-core" />
      {people.map((person) => {
        const style = { "--x": `${person.x}%`, "--y": `${person.y}%`, "--signal": person.accent || "#d8ff3e" };
        const content = <><i className="signal-dot" /><span className="signal-label">{person.name}</span></>;
        return person.hasPage ? (
          <a className="signal-button" href={agentUrl(person.id)} aria-label={`Open ${person.name}`} style={style} key={person.id} onClick={(event) => { event.preventDefault(); navigate(agentUrl(person.id)); }}>{content}</a>
        ) : (
          <button className="signal-button" type="button" aria-label={`Inspect visitor ${person.name}`} style={style} key={person.id} onClick={() => onOpen(person)}>{content}</button>
        );
      })}
    </div>
  );
}

function LedgerRows({ items, empty, children }) {
  if (!items.length) return <p className="empty">{empty}</p>;
  return items.map(children);
}

function AgentRecord({ person, model, modal = false, navigate }) {
  const approvals = related(model.approvals, person.id);
  const tasks = related(model.tasks, person.id, (item, id) => item.claimant === id || item.posted_by === id);
  const artifacts = related(model.artifacts, person.id);
  const journals = related(model.journals, person.id);
  const routines = related(model.routines, person.id);
  const tools = person.capabilities?.tools;
  const manifest = person.manifest || {};
  return (
    <>
      {!modal && <a className="agent-back" href="/" onClick={(event) => { event.preventDefault(); navigate("/"); }}>← FLEET OVERVIEW</a>}
      <header className="agent-page-hero">
        <div><p className="detail-kicker">{modal ? "VISITOR SESSION RECORD" : "PERMANENT RESIDENT RECORD"} / READ ONLY</p><h1 id={modal ? "drawer-name" : undefined}>{fallback(person.name, person.id)}</h1><p className="agent-role">{fallback(person.role, person.residency)} · {fallback(person.project)}</p></div>
        <div className="agent-monogram" style={{ "--accent": person.accent || "#d8ff3e" }}>{(person.name || "?")[0]}</div>
      </header>
      <div className="agent-status-line"><span className="detail-state">{fallback(person.state)}</span><span>LAST SIGNAL {fallback(person.last_ts)}</span><span>MOOD {fallback(person.mood?.name)}</span></div>
      {person.body && <p className="agent-statement">{person.body}</p>}
      <section className="agent-facts">
        {[["FULL AGENT ID", person.id], ["RESIDENT FILE", person.resident_file], ["HOME", person.home], ["BASE / PLACE", `${fallback(person.base)} / ${fallback(person.place)}`], ["WORKING DIRECTORY", person.cwd], ["CHARACTER", person.char], ["TOOLS", Array.isArray(tools) ? tools.join(", ") : ""], ["MANIFEST VERSION", manifest.manifest_version], ["LINEAGE", Object.keys(person.lineage || {}).length ? JSON.stringify(person.lineage) : ""], ["PENDING APPROVAL IDS", (person.pending_approval_ids || []).join(", ")]].map(([label, value]) => <div key={label}><span>{label}</span><b>{fallback(value)}</b></div>)}
      </section>
      <div className="agent-ledger">
        <section><p className="eyebrow">01 / WORK</p><h2>Tasks & routines</h2><LedgerRows items={tasks} empty="No retained tasks.">{(item) => <div className="ledger-row" key={item.id}><b>{fallback(item.title, item.id)}</b><span>{fallback(item.state)} · {fallback(item.updated_at)}</span></div>}</LedgerRows><LedgerRows items={routines} empty="No retained routine runs.">{(item, index) => <div className="ledger-row" key={`${item.routine}:${index}`}><b>{fallback(item.routine)}</b><span>{fallback(item.state)} · {fallback(item.updated_at)}</span><small>{fallback(item.outcome, item.error)}</small></div>}</LedgerRows></section>
        <section><p className="eyebrow">02 / OUTPUT</p><h2>Artifacts & journals</h2><LedgerRows items={artifacts} empty="No retained artifacts.">{(item, index) => <div className="ledger-row" key={`${item.artifact}:${index}`}><b>{fallback(item.artifact)}</b><span>{fallback(item.ts)}</span></div>}</LedgerRows><LedgerRows items={journals} empty="No retained journals.">{(item, index) => <div className="ledger-row" key={`${item.path}:${index}`}><b>{fallback(item.path)}</b><span>{fallback(item.day)} · {fallback(item.routine)}</span></div>}</LedgerRows></section>
        <section><p className="eyebrow">03 / ATTENTION</p><h2>Approvals</h2><LedgerRows items={approvals} empty="No retained approval requests.">{(item, index) => <div className="ledger-row approval-ledger" key={`${item.id || item.action}:${index}`}><b>{fallback(item.message, item.action)}</b><span>{fallback(item.state)} · {fallback(item.opened_at)}</span><small>{fallback(item.detail && JSON.stringify(item.detail))}</small></div>}</LedgerRows></section>
      </div>
      <section className="agent-history"><p className="eyebrow">04 / RETAINED HISTORY</p><h2>Signal record</h2><EventRows events={(person.history || []).slice().reverse().map((event, index) => ({ ...event, agent_name: person.name, _stable: `${person.id}:${index}` }))} /></section>
    </>
  );
}

function FleetOverview({ model, navigate, onOpen }) {
  const [filter, setFilter] = useState("all");
  const openTasks = model.tasks.filter((task) => !["done", "cancelled", "failed"].includes(task.state)).length;
  const pending = model.approvals.filter((item) => item.state === "pending").length;
  const events = model.events.filter((event) => filter === "all" || event.agent_id === filter);
  return <>
    <section className="hero"><div><p className="eyebrow">AUTHORITATIVE FLEET STATE / READ ONLY</p><h1>Listen to the<br /><em>whole system.</em></h1><p className="lede">A live instrument for seeing who is present, what is moving, and where human attention is accumulating.</p></div><FleetPulse people={model.people} navigate={navigate} onOpen={onOpen} /></section>
    <section className="metrics" aria-label="Fleet summary">{[[model.people.length, "RESIDENTS"], [model.active, "ACTIVE SIGNALS"], [openTasks, "OPEN TASKS"], [pending, "NEED ATTENTION"]].map(([value, label]) => <div className={label === "NEED ATTENTION" ? "attention" : ""} key={label}><strong>{value}</strong><span>{label}</span></div>)}</section>
    <section className="board">
      <article className="panel"><div className="panel-heading"><span>01</span><h2>Signals</h2><small>Select an agent to inspect</small></div><div>{model.people.length ? model.people.map((person, index) => { const content = <span className="resident" style={{ animationDelay: `${index * 35}ms` }}><span className="resident-initial" style={{ "--accent": person.accent || "#d8ff3e" }}>{(person.name || "?")[0]}</span><span><b className="name">{person.name}</b><small className="meta">{fallback(person.role, person.project || "visitor")} · {person.hasPage ? "permanent record" : "visitor"}</small></span><span className="state">{person.state}</span></span>; return person.hasPage ? <a className="resident-button" href={agentUrl(person.id)} key={person.id} onClick={(event) => { event.preventDefault(); navigate(agentUrl(person.id)); }}>{content}</a> : <button className="resident-button" type="button" key={person.id} onClick={() => onOpen(person)}>{content}</button>; }) : <p className="empty">No residents in this snapshot.</p>}</div></article>
      <article className="panel"><div className="panel-heading"><span>02</span><h2>Workstream</h2><small>Authoritative task queue</small></div>{model.tasks.length ? model.tasks.map((task, index) => <div className="task" style={{ animationDelay: `${index * 35}ms` }} key={task.id}><span><b className="item-title">{task.title}</b><small className="item-meta">{fallback(task.claimant, "unclaimed")} · {fallback((task.required_skills || []).join(", "), "no skill constraint")}</small></span><span className="state">{task.state}</span></div>) : <p className="empty">The workstream is clear.</p>}</article>
      <article className="panel"><div className="panel-heading"><span>03</span><h2>Attention</h2><small>Human decisions</small></div>{model.approvals.length ? model.approvals.map((approval, index) => <div className="approval" style={{ animationDelay: `${index * 35}ms` }} key={approval.id || index}><span><b className="item-title">{fallback(approval.message, approval.action)}</b><small className="item-meta">{approval.agent_id} · {approval.project}</small></span><span className="state">{approval.state}</span></div>) : <p className="empty">No human decisions queued.</p>}</article>
    </section>
    <section className="activity-section"><div className="activity-heading"><div><p className="eyebrow">04 / RETAINED SNAPSHOT HISTORY</p><h2>Event signal</h2></div><label className="feed-filter">SHOW <select value={filter} onChange={(event) => setFilter(event.target.value)}><option value="all">ALL AGENTS</option>{model.people.map((person) => <option value={person.id} key={person.id}>{person.name}</option>)}</select></label></div><div className="event-feed"><EventRows events={events} /></div></section>
  </>;
}

export default function App() {
  const { snapshot, status } = useFleetState();
  const [pathname, navigate] = usePathname();
  const [visitor, setVisitor] = useState(null);
  const model = useMemo(() => snapshot && viewModel(snapshot), [snapshot]);
  const uuid = routeAgent(pathname);
  const resident = uuid && model?.people.filter((person) => person.hasPage && agentUuid(person.id) === uuid);
  const person = resident?.length === 1 ? resident[0] : null;
  useEffect(() => { document.title = person ? `${person.name} — Burrow Observatory` : uuid ? "Agent not found — Burrow Observatory" : "Burrow Observatory"; }, [person, uuid]);
  return <div className="app-shell">
    <div className="grain" aria-hidden="true" />
    <header className="masthead"><a className="wordmark" href="/" onClick={(event) => { event.preventDefault(); navigate("/"); }}><span className="mark">B/</span>OBSERVATORY</a><Connection status={status} evaluatedAt={snapshot?.evaluated_at} /></header>
    <main>{!model ? <section className="hero"><div><p className="eyebrow">AUTHORITATIVE FLEET STATE / READ ONLY</p><h1>Listening for<br /><em>the system.</em></h1><p className="lede">Waiting for a complete Burrow snapshot.</p></div></section> : uuid ? <article className="agent-page">{person ? <AgentRecord person={person} model={model} navigate={navigate} /> : <><a className="agent-back" href="/" onClick={(event) => { event.preventDefault(); navigate("/"); }}>← FLEET OVERVIEW</a><div className="route-empty"><p className="eyebrow">NO PERMANENT RECORD</p><h1>Agent not found.</h1><p>Only created residents with a unique UUID have permanent Observatory pages.</p></div></>}</article> : <FleetOverview model={model} navigate={navigate} onOpen={setVisitor} />}</main>
    {visitor && model && <><aside className="agent-drawer open" role="dialog" aria-modal="true" aria-labelledby="drawer-name"><button className="drawer-close" type="button" aria-label="Close visitor record" onClick={() => setVisitor(null)}>CLOSE ×</button><AgentRecord person={visitor} model={model} modal navigate={navigate} /></aside><button className="drawer-scrim open" type="button" aria-label="Close visitor record" onClick={() => setVisitor(null)} /></>}
    <footer><span>SCHEMA <b>{snapshot ? `v${snapshot.schema_version}` : "—"}</b></span><span>GENERATION <b>{snapshot?.generation ?? "—"}</b></span><span>CURSOR <b>{snapshot?.cursor || "—"}</b></span></footer>
  </div>;
}
