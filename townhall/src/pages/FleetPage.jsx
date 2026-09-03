/* Chronicle's read-only fleet telemetry in Townhall's shared console language. */
import { useState } from "react";
import { Link, useNavigation } from "../navigation.jsx";
import { agentUuid, payloadSummary, related } from "../model.js";
import { routeTo } from "../routes.js";
import {
  Badge, Badges, Button, DetailHead, Empty, Facts, Label, Loading, PageHead, Panel,
  Row, Rows, Section, Select, Stack, Who,
} from "../console/ui.jsx";

const fallback = (value, alternative = "—") => value || alternative;
const terminal = new Set(["done", "cancelled", "failed"]);
const stamp = (value) => {
  const date = new Date(value);
  return Number.isFinite(date.valueOf()) ? date.toLocaleString() : "—";
};
const stateTone = (state) => {
  if (["failed", "error", "blocked"].includes(state)) return "fail";
  if (["pending", "waiting", "knocking"].includes(state)) return "wait";
  if (["active", "working", "thinking", "done"].includes(state)) return "live";
  return "";
};

function Summary({ model }) {
  const values = [
    ["observed", model.people.length],
    ["active", model.active],
    ["open work", model.tasks.filter((task) => !terminal.has(task.state)).length],
    ["human attention", model.approvals.filter((item) => item.state === "pending").length],
  ];
  return <div className="rise mb-7 grid border border-rule bg-deep sm:grid-cols-2 lg:grid-cols-4">
    {values.map(([label, value], index) => <div className="border-rule px-5 py-4 sm:border-l sm:first:border-l-0" key={label}>
      <Label>{label}</Label>
      <strong className={`mt-1 block font-serif text-[30px] font-normal ${index === 3 && value ? "text-wait" : "text-ink"}`}>{value}</strong>
    </div>)}
  </div>;
}

function agentAddress(person) {
  return routeTo.agent(person.hasResidentRecord ? agentUuid(person.id) : person.id);
}

function People({ people }) {
  if (!people.length) return <Empty title="No observed agents.">Chronicle's current snapshot contains no resident or transient session.</Empty>;
  return <Rows><Row head columns="1.5fr .8fr 1fr 1.1fr"><span>agent</span><span>state</span><span>kind</span><span>last signal</span></Row>
    {people.map((person) => {
      const content = <><Who accent={person.accent} name={fallback(person.name, person.id)} id={person.id} role={person.role || person.project} /><Badge tone={stateTone(person.state)}>{fallback(person.state)}</Badge><Stack sub={person.project}>{person.hasResidentRecord ? "resident" : "transient"}</Stack><span className="text-[11px] text-dim">{stamp(person.last_ts)}</span></>;
      return <Row key={person.id} columns="1.5fr .8fr 1fr 1.1fr" accent={person.accent}><Link to={agentAddress(person)} className="contents text-inherit no-underline">{content}</Link></Row>;
    })}
  </Rows>;
}

function Work({ tasks }) {
  if (!tasks.length) return <Empty title="The workstream is clear.">Chronicle retains no work in this snapshot.</Empty>;
  return <Rows>{tasks.map((task) => <Row key={task.id} columns="1.5fr .75fr 1fr"><Stack sub={(task.required_skills || []).join(", ") || "no required skills"}>{fallback(task.title, task.id)}</Stack><Badge tone={stateTone(task.state)}>{fallback(task.state)}</Badge><span className="text-[11px] text-dim">{fallback(task.claimant, "unclaimed")}</span></Row>)}</Rows>;
}

function Attention({ approvals }) {
  if (!approvals.length) return <Empty title="No human decisions queued.">Nothing in the retained snapshot is waiting at the threshold.</Empty>;
  return <Rows>{approvals.map((approval, index) => <Row key={approval.id || index} columns="1.5fr .75fr 1fr"><Stack sub={approval.action}>{fallback(approval.message, approval.action)}</Stack><Badge tone={stateTone(approval.state)}>{fallback(approval.state)}</Badge><span className="text-[11px] text-dim">{fallback(approval.agent_id)} · {fallback(approval.project)}</span></Row>)}</Rows>;
}

function Activity({ events }) {
  if (!events.length) return <Empty title="No retained activity.">Nothing matches this snapshot filter.</Empty>;
  return <Rows><Row head columns=".8fr 1fr 1fr 1.7fr"><span>when</span><span>agent</span><span>event</span><span>detail</span></Row>
    {events.map((event) => <Row key={event._stable || `${event.agent_id}:${event.ts}:${event.type}`} columns=".8fr 1fr 1fr 1.7fr"><time className="text-[11px] text-dim" dateTime={event.ts}>{stamp(event.ts)}</time><span className="truncate">{fallback(event.agent_name, event.agent_id)}</span><Badge tone={stateTone(event.type)}>{fallback(event.type)}</Badge><span className="truncate text-[11px] text-dim" title={payloadSummary(event)}>{fallback(payloadSummary(event), "no payload summary")}</span></Row>)}
  </Rows>;
}

function FleetOverview({ model }) {
  const [filter, setFilter] = useState("all");
  const events = model.events.filter((event) => filter === "all" || event.agent_id === filter);
  return <>
    <PageHead title="Fleet">Chronicle's read-only view of who is present, what is moving, and where a human decision is waiting. Writes live on the other Townhall pages.</PageHead>
    <Summary model={model} />
    <Section count={model.people.length}>Observed agents</Section><People people={model.people} />
    <div className="grid gap-7 lg:grid-cols-2"><div><Section count={model.tasks.length}>Work in motion</Section><Work tasks={model.tasks} /></div><div><Section count={model.approvals.length}>Human attention</Section><Attention approvals={model.approvals} /></div></div>
    <div className="mt-9 flex flex-wrap items-end justify-between gap-4"><Section count={events.length}>Retained activity</Section><label className="mb-[14px] min-w-[190px]"><Label className="mb-1.5 block">show signals from</Label><Select value={filter} onChange={(event) => setFilter(event.target.value)}><option value="all">all agents</option>{model.people.map((person) => <option value={person.id} key={person.id}>{person.name}</option>)}</Select></label></div>
    <Activity events={events} />
  </>;
}

function RecordRows({ items, empty, render }) {
  return items.length ? <Rows>{items.map(render)}</Rows> : <Empty title={empty}>Chronicle retains no matching record in this snapshot.</Empty>;
}

function AgentRecord({ person, model }) {
  const tasks = related(model.tasks, person.id, (item, id) => item.claimant === id || item.posted_by === id);
  const approvals = related(model.approvals, person.id);
  const artifacts = related(model.artifacts, person.id);
  const journals = related(model.journals, person.id);
  const routines = related(model.routines, person.id);
  return <article>
    <DetailHead accent={person.accent} title={fallback(person.name, person.id)} back={<Link to={routeTo.fleet()} className="text-[10px] uppercase tracking-[.16em] text-ember no-underline">← Fleet</Link>} aside={<Badges><Badge tone={stateTone(person.state)}>{fallback(person.state)}</Badge><Badge>{person.hasResidentRecord ? "resident" : "transient"}</Badge></Badges>}>{fallback(person.role, person.project || person.residency)}</DetailHead>
    {person.body ? <Panel><p className="m-0 max-w-[80ch] font-serif text-[17px] leading-[1.7] text-read">{person.body}</p></Panel> : null}
    <Facts className="mb-7" pairs={[["full id", person.id], ["last signal", stamp(person.last_ts)], ["mood", person.mood?.name], ["resident file", person.resident_file], ["home", person.home], ["base / place", `${fallback(person.base)} / ${fallback(person.place)}`], ["working directory", person.cwd], ["tools", person.capabilities?.tools?.join(", ")], ["manifest", person.manifest?.manifest_version]]} />
    <div className="grid gap-7 lg:grid-cols-3">
      <div><Section count={tasks.length + routines.length}>Work</Section><RecordRows items={[...tasks, ...routines]} empty="No retained work." render={(item, index) => <Row key={item.id || `${item.routine}:${index}`} columns="1fr"><Stack sub={item.updated_at || item.outcome || item.error}>{fallback(item.title, item.routine)}</Stack></Row>} /></div>
      <div><Section count={artifacts.length + journals.length}>Output</Section><RecordRows items={[...artifacts, ...journals]} empty="No retained output." render={(item, index) => <Row key={`${item.artifact || item.path}:${index}`} columns="1fr"><Stack sub={item.ts || `${item.day || ""} ${item.routine || ""}`}>{fallback(item.artifact, item.path)}</Stack></Row>} /></div>
      <div><Section count={approvals.length}>Attention</Section><RecordRows items={approvals} empty="No retained approvals." render={(item, index) => <Row key={item.id || index} columns="1fr"><Stack sub={item.opened_at}><span><Badge tone={stateTone(item.state)}>{item.state}</Badge> {fallback(item.message, item.action)}</span></Stack></Row>} /></div>
    </div>
    <Section count={(person.history || []).length}>Signal record</Section><Activity events={(person.history || []).slice().reverse().map((event, index) => ({ ...event, agent_name: person.name, _stable: `${person.id}:${index}` }))} />
  </article>;
}

export default function FleetPage({ model, page, params }) {
  const { navigate } = useNavigation();
  if (!model) return <><PageHead title="Fleet">Chronicle's read-only view of the fleet.</PageHead><Loading>waiting for Chronicle's complete snapshot…</Loading></>;
  if (page === "agent") {
    const matches = model.people.filter((item) =>
      item.id === params.uuid || (item.hasResidentRecord && agentUuid(item.id) === params.uuid)
    );
    const person = matches.length === 1 ? matches[0] : null;
    if (!person) return <><PageHead title="No agent record">This address does not match exactly one agent in Chronicle's current snapshot.</PageHead><Button onClick={() => navigate(routeTo.fleet())}>← return to fleet</Button></>;
    return <AgentRecord person={person} model={model} />;
  }
  return <FleetOverview model={model} />;
}
