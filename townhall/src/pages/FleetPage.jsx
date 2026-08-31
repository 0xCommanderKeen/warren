/* The fleet — read-only telemetry over Chronicle's `/state`.
 *
 * This is the observatory the repo already had, moved wholesale into the console shell as
 * one subpage of it (warren#225). Its own look survives here and nowhere else: the atlas
 * palette, the polar projection, the display face. The two may converge later; nothing in
 * this file was restyled to make that decision early.
 *
 * The one thing that changed is chrome. The page used to own the header, the connection
 * pill and the snapshot footer; the rail owns those now, for every page.
 */

import { useEffect, useState } from "react";
import { Link, useNavigation } from "../navigation.jsx";
import { agentUuid, payloadSummary, related } from "../model.js";
import { routeTo } from "../routes.js";

const fallback = (value, alternative = "—") => value || alternative;
const formatTime = (value, compact = false) => {
  const date = new Date(value);
  if (!Number.isFinite(date.valueOf())) return "—";
  return compact ? date.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" }) : date.toLocaleString();
};

function SectionLabel({ index, children, tone = "amber" }) {
  return <div className={`flex items-center gap-3 font-mono text-[9px] tracking-[0.22em] uppercase ${tone === "cyan" ? "text-teal-300" : "text-amber-300"}`}><span>{index}</span><span className="h-px w-8 bg-current opacity-40" /><span>{children}</span></div>;
}

const Empty = ({ children }) => <p className="border-t border-dashed border-white/10 py-8 font-mono text-[11px] leading-6 text-slate-400">{children}</p>;

function Constellation({ people, inspect }) {
  return <div className="relative mx-auto aspect-square w-full max-w-[590px]" aria-label="Fleet constellation">
    <div className="absolute inset-[5%] rounded-full border border-white/10" /><div className="absolute inset-[19%] rounded-full border border-dashed border-white/10" /><div className="absolute inset-[36%] rounded-full border border-white/10" />
    <div className="absolute left-1/2 top-1/2 h-px w-[108%] -translate-x-1/2 bg-white/10" /><div className="absolute left-1/2 top-1/2 h-[108%] w-px -translate-y-1/2 bg-white/10" /><div className="orbit-sweep absolute inset-[5%] rounded-full" />
    <div className="absolute left-1/2 top-1/2 z-10 grid size-20 -translate-x-1/2 -translate-y-1/2 place-items-center rounded-full border border-amber-300/40 bg-amber-300/10 shadow-[0_0_70px_rgba(243,179,61,.16)]"><span className="font-display text-2xl italic text-amber-200">W</span></div>
    {people.map((person, index) => {
      const color = person.accent || (index % 2 ? "#79d7d0" : "#f3b33d");
      const classes = "group absolute z-20 -translate-x-1/2 -translate-y-1/2 p-3 text-left transition-transform duration-300 hover:scale-125 focus-visible:outline focus-visible:outline-1 focus-visible:outline-offset-4 focus-visible:outline-amber-300";
      const style = { left: `${person.x}%`, top: `${person.y}%` };
      const content = <><span className="absolute inset-0 rounded-full opacity-25 blur-md" style={{ background: color }} /><span className="relative block size-2.5 rounded-full border-2 border-[#080b0f]" style={{ background: color, boxShadow: `0 0 0 1px ${color}` }} /><span className="absolute left-5 top-1/2 -translate-y-1/2 whitespace-nowrap font-mono text-[9px] tracking-wider text-slate-300 group-hover:text-white">{person.name}</span></>;
      return person.hasPage ? <Link key={person.id} to={routeTo.agent(agentUuid(person.id))} className={classes} style={style}>{content}</Link> : <button key={person.id} type="button" className={classes} style={style} onClick={() => inspect(person)}>{content}</button>;
    })}
    <span className="absolute bottom-[7%] left-[8%] font-mono text-[8px] tracking-[0.2em] text-slate-400">FLEET / POLAR PROJECTION</span>
  </div>;
}

function Metric({ value, label, alert = false }) {
  return <div className="group border-l border-white/10 px-5 py-4 first:border-l-0 lg:px-7"><strong className={`font-display text-4xl font-normal tracking-tight ${alert ? "text-amber-300" : "text-slate-100"}`}>{value}</strong><span className="mt-2 block font-mono text-[8px] tracking-[0.18em] text-slate-400 group-hover:text-slate-200">{label}</span></div>;
}

function PersonRow({ person, inspect }) {
  const content = <div className="group grid grid-cols-[44px_1fr_auto] items-center gap-4 border-t border-white/10 py-4 text-left transition-colors hover:bg-white/[.025]"><span className="grid size-9 place-items-center rounded-full border border-white/15 font-display text-lg" style={{ color: person.accent || "#f3b33d" }}>{(person.name || "?")[0]}</span><span className="min-w-0"><b className="block truncate text-xs font-medium text-slate-100">{person.name}</b><small className="mt-1 block truncate font-mono text-[9px] tracking-wide text-slate-400">{fallback(person.role, person.project || "visitor")} / {person.hasPage ? "resident" : "transient"}</small></span><span className="font-mono text-[8px] tracking-[0.12em] text-teal-300">{person.state}</span></div>;
  return person.hasPage ? <Link to={routeTo.agent(agentUuid(person.id))} className="block" key={person.id}>{content}</Link> : <button className="block w-full" type="button" onClick={() => inspect(person)} key={person.id}>{content}</button>;
}

function WorkRow({ title, meta, state, alert = false }) {
  return <div className="grid grid-cols-[1fr_auto] gap-4 border-t border-white/10 py-4"><div><b className="block text-[11px] font-medium text-slate-200">{fallback(title)}</b><span className="mt-1.5 block font-mono text-[9px] leading-4 text-slate-400">{fallback(meta)}</span></div><span className={`font-mono text-[8px] tracking-[0.12em] ${alert ? "text-amber-300" : "text-teal-300"}`}>{fallback(state)}</span></div>;
}

function Activity({ events }) {
  if (!events.length) return <Empty>No retained activity in this snapshot.</Empty>;
  return <div className="divide-y divide-white/10 border-t border-white/10">{events.map((event) => <div className="grid gap-2 py-4 md:grid-cols-[90px_160px_140px_1fr] md:gap-5" key={event._stable || `${event.agent_id}:${event.ts}:${event.type}`}><time className="font-mono text-[9px] text-slate-400">{formatTime(event.ts, true)}</time><b className="truncate text-[11px] font-medium text-slate-300">{fallback(event.agent_name, event.agent_id)}</b><span className="font-mono text-[9px] tracking-wide text-teal-300">{fallback(event.type)}</span><span className="truncate font-mono text-[9px] text-slate-400" title={payloadSummary(event)}>{fallback(payloadSummary(event), "No payload summary")}</span></div>)}</div>;
}

function FleetOverview({ model, inspect }) {
  const [filter, setFilter] = useState("all");
  const openTasks = model.tasks.filter((task) => !["done", "cancelled", "failed"].includes(task.state)).length;
  const pending = model.approvals.filter((approval) => approval.state === "pending").length;
  const events = model.events.filter((event) => filter === "all" || event.agent_id === filter);
  return <>
    <section className="grid min-h-[560px] items-center gap-10 border-b border-white/10 py-12 lg:grid-cols-[.8fr_1.2fr] lg:py-16"><div className="relative z-10"><SectionLabel index="00">Authoritative fleet telemetry</SectionLabel><h1 className="mt-10 max-w-[720px] font-display text-[clamp(3.4rem,7vw,7rem)] font-normal leading-[.72] tracking-[-.065em] text-slate-100">Watch the<br /><span className="italic text-amber-300">quiet machinery.</span></h1><p className="mt-10 max-w-md text-[13px] leading-7 text-slate-300">A read-only atlas of agents, work, and the places where human attention bends the system. Every write in this console lives on another page.</p></div><Constellation people={model.people} inspect={inspect} /></section>
    <section className="grid grid-cols-2 border-b border-white/10 lg:grid-cols-4" aria-label="Fleet summary"><Metric value={model.people.length} label="OBSERVED AGENTS" /><Metric value={model.active} label="ACTIVE SIGNALS" /><Metric value={openTasks} label="OPEN WORK" /><Metric value={pending} label="HUMAN ATTENTION" alert /></section>
    <section className="grid border-b border-white/10 lg:grid-cols-[.9fr_1.1fr_1fr]"><div className="py-12 lg:border-r lg:border-white/10 lg:pr-8"><SectionLabel index="01" tone="cyan">Signals</SectionLabel><h2 className="mb-7 mt-4 font-display text-3xl text-slate-100">In the field</h2>{model.people.length ? model.people.map((person) => <PersonRow person={person} inspect={inspect} key={person.id} />) : <Empty>No observed agents.</Empty>}</div><div className="py-12 lg:border-r lg:border-white/10 lg:px-8"><SectionLabel index="02" tone="cyan">Workstream</SectionLabel><h2 className="mb-7 mt-4 font-display text-3xl text-slate-100">In motion</h2>{model.tasks.length ? model.tasks.map((task) => <WorkRow key={task.id} title={task.title} state={task.state} meta={`${fallback(task.claimant, "unclaimed")} / ${fallback((task.required_skills || []).join(", "), "unconstrained")}`} />) : <Empty>The workstream is clear.</Empty>}</div><div className="py-12 lg:pl-8"><SectionLabel index="03">Attention</SectionLabel><h2 className="mb-7 mt-4 font-display text-3xl text-slate-100">At the threshold</h2>{model.approvals.length ? model.approvals.map((approval, index) => <WorkRow key={approval.id || index} title={fallback(approval.message, approval.action)} state={approval.state} meta={`${approval.agent_id} / ${approval.project}`} alert />) : <Empty>No human decisions queued.</Empty>}</div></section>
    <section className="py-16"><div className="mb-9 flex flex-col justify-between gap-6 md:flex-row md:items-end"><div><SectionLabel index="04" tone="cyan">Retained snapshot history</SectionLabel><h2 className="mt-4 font-display text-5xl text-slate-100">Signal ledger</h2></div><label className="font-mono text-[9px] tracking-[0.16em] text-slate-400">FILTER / <select className="ml-2 border border-white/10 bg-transparent px-3 py-2 text-slate-200 outline-none focus:border-teal-300" value={filter} onChange={(event) => setFilter(event.target.value)}><option className="bg-slate-950" value="all">ALL AGENTS</option>{model.people.map((person) => <option className="bg-slate-950" value={person.id} key={person.id}>{person.name}</option>)}</select></label></div><Activity events={events} /></section>
  </>;
}

const Fact = ({ label, value }) => <div className="min-w-0 border-l border-t border-white/10 p-4 sm:p-5"><span className="block font-mono text-[8px] tracking-[.16em] text-slate-400">{label}</span><b className="mt-2 block break-words font-mono text-[10px] font-normal leading-5 text-slate-300">{fallback(value)}</b></div>;
const RecordGroup = ({ index, title, children }) => <section className="border-t border-white/10 py-10"><SectionLabel index={index} tone="cyan">Record</SectionLabel><h2 className="mb-6 mt-4 font-display text-3xl text-slate-100">{title}</h2>{children}</section>;
const RecordRows = ({ items, empty, render }) => items.length ? items.map(render) : <Empty>{empty}</Empty>;

function AgentRecord({ person, model, modal = false }) {
  const tasks = related(model.tasks, person.id, (item, id) => item.claimant === id || item.posted_by === id);
  const approvals = related(model.approvals, person.id), artifacts = related(model.artifacts, person.id), journals = related(model.journals, person.id), routines = related(model.routines, person.id);
  const row = (title, meta, key, alert = false) => <WorkRow key={key} title={title} meta={meta} state={alert ? "attention" : "retained"} alert={alert} />;
  return <article>
    {!modal && <Link to={routeTo.fleet()} className="mb-12 inline-flex items-center gap-3 font-mono text-[9px] tracking-[.16em] text-slate-400 transition-colors hover:text-amber-300">← RETURN TO FLEET</Link>}
    <header className="grid items-end gap-10 border-b border-white/10 pb-12 lg:grid-cols-[1fr_auto]"><div><SectionLabel index={modal ? "VISITOR" : "RESIDENT"}>{modal ? "Transient session" : "Permanent record"}</SectionLabel><h1 id={modal ? "visitor-title" : undefined} className="mt-8 font-display text-[clamp(3.2rem,8vw,7rem)] leading-[.75] tracking-[-.06em] text-slate-100">{fallback(person.name, person.id)}</h1><p className="mt-8 font-mono text-[10px] tracking-[.14em] text-slate-400">{fallback(person.role, person.residency)} / {fallback(person.project)}</p></div><div className="grid size-32 place-items-center rounded-full border border-white/15 bg-white/[.02] font-display text-7xl italic shadow-[0_0_90px_rgba(121,215,208,.1)]" style={{ color: person.accent || "#79d7d0" }}>{(person.name || "?")[0]}</div></header>
    <div className="flex flex-wrap gap-x-8 gap-y-3 border-b border-white/10 py-5 font-mono text-[9px] tracking-[.12em] text-slate-400"><span className="text-teal-300">● {fallback(person.state).toUpperCase()}</span><span>LAST / {fallback(person.last_ts)}</span><span>MOOD / {fallback(person.mood?.name)}</span></div>
    {person.body && <p className="my-14 max-w-4xl font-display text-[clamp(1.6rem,3.4vw,3rem)] leading-[1.08] text-slate-200">{person.body}</p>}
    <section className="grid border-b border-r border-white/10 sm:grid-cols-2 lg:grid-cols-5">{[["FULL ID", person.id], ["RESIDENT FILE", person.resident_file], ["HOME", person.home], ["BASE / PLACE", `${fallback(person.base)} / ${fallback(person.place)}`], ["WORKING DIRECTORY", person.cwd], ["CHARACTER", person.char], ["TOOLS", person.capabilities?.tools?.join(", ")], ["MANIFEST", person.manifest?.manifest_version], ["LINEAGE", Object.keys(person.lineage || {}).length ? JSON.stringify(person.lineage) : ""], ["APPROVAL IDS", (person.pending_approval_ids || []).join(", ")]].map(([label, value]) => <Fact label={label} value={value} key={label} />)}</section>
    <div className="grid lg:grid-cols-3 lg:gap-x-8">{[<RecordGroup index="01" title="Work" key="w"><RecordRows items={tasks} empty="No retained tasks." render={(item) => row(fallback(item.title, item.id), `${fallback(item.state)} / ${fallback(item.updated_at)}`, item.id)} /><RecordRows items={routines} empty="No retained routines." render={(item, index) => row(item.routine, `${item.state} / ${fallback(item.outcome, item.error)}`, `${item.routine}:${index}`)} /></RecordGroup>, <RecordGroup index="02" title="Output" key="o"><RecordRows items={artifacts} empty="No retained artifacts." render={(item, index) => row(item.artifact, item.ts, `${item.artifact}:${index}`)} /><RecordRows items={journals} empty="No retained journals." render={(item, index) => row(item.path, `${item.day} / ${item.routine}`, `${item.path}:${index}`)} /></RecordGroup>, <RecordGroup index="03" title="Attention" key="a"><RecordRows items={approvals} empty="No retained approvals." render={(item, index) => row(fallback(item.message, item.action), `${item.state} / ${item.opened_at}`, `${item.id || item.action}:${index}`, true)} /></RecordGroup>]}</div>
    <section className="py-14"><SectionLabel index="04" tone="cyan">Retained history</SectionLabel><h2 className="mb-8 mt-4 font-display text-4xl text-slate-100">Signal record</h2><Activity events={(person.history || []).slice().reverse().map((event, index) => ({ ...event, agent_name: person.name, _stable: `${person.id}:${index}` }))} /></section>
  </article>;
}

function VisitorDialog({ person, model, close }) {
  useEffect(() => {
    const keydown = (event) => event.key === "Escape" && close();
    window.addEventListener("keydown", keydown);
    return () => window.removeEventListener("keydown", keydown);
  }, [close]);
  return <div className="fixed inset-0 z-50 grid place-items-center p-2 sm:p-6"><button type="button" className="absolute inset-0 bg-black/80 backdrop-blur-sm" aria-label="Close visitor record" onClick={close} /><aside className="relative max-h-[94vh] w-full max-w-[1400px] overflow-y-auto border border-white/15 bg-[#0b0f14] px-5 py-6 shadow-2xl sm:px-10 lg:px-16" role="dialog" aria-modal="true" aria-labelledby="visitor-title"><button type="button" className="sticky top-0 z-10 ml-auto block border border-white/10 bg-[#0b0f14] px-3 py-2 font-mono text-[9px] tracking-widest text-slate-300 hover:text-amber-300" onClick={close}>CLOSE ×</button><AgentRecord person={person} model={model} modal /></aside></div>;
}

/** The fleet page and the uid-addressed agent record it links into. */
export default function FleetPage({ model, page, params }) {
  const [visitor, setVisitor] = useState(null);
  const { navigate } = useNavigation();

  if (!model) {
    return <section className="grid min-h-[60vh] place-items-center text-center"><div><SectionLabel index="00">Waiting for Chronicle</SectionLabel><h1 className="mt-8 font-display text-6xl tracking-tight text-slate-100">Listening.</h1><p className="mt-5 font-mono text-[10px] tracking-widest text-slate-400">COMPLETE SNAPSHOT REQUIRED</p></div></section>;
  }

  if (page === "agent") {
    const matches = model.people.filter((item) => item.hasPage && agentUuid(item.id) === params.uuid);
    const person = matches.length === 1 ? matches[0] : null;
    if (!person) {
      return <section className="grid min-h-[60vh] place-items-center"><div><SectionLabel index="404">No permanent record</SectionLabel><h1 className="mt-8 font-display text-6xl text-slate-100">Signal absent.</h1><button type="button" onClick={() => navigate(routeTo.fleet())} className="mt-8 cursor-pointer border-0 bg-transparent p-0 font-mono text-[9px] tracking-widest text-amber-300">← RETURN TO FLEET</button></div></section>;
    }
    return <AgentRecord person={person} model={model} />;
  }

  return <>
    <FleetOverview model={model} inspect={setVisitor} />
    {visitor ? <VisitorDialog person={visitor} model={model} close={() => setVisitor(null)} /> : null}
  </>;
}
