import { useEffect, useMemo, useRef, useState } from "react";
import { createVisitBriefing, readVisitBaseline, saveVisitBaseline } from "../world/visitBriefing.js";
import "./visit-briefing.css";

export function VisitBriefing({ snapshot, onSelectAgent, onOpenArchive, onReviewApprovals, onOpenTask }) {
  const tracker = useRef(null);
  if (!tracker.current) tracker.current = createVisitBriefing(readVisitBaseline());
  const [revision, setRevision] = useState(0);
  const summary = useMemo(() => tracker.current.update(snapshot), [snapshot, revision]);
  useEffect(() => { saveVisitBaseline(snapshot); }, [snapshot]);
  const count = summary.arrivals.length + summary.completedTasks.length + summary.failures.length + summary.approvals.length;
  if (!count) return null;
  const markSeen = () => { tracker.current.markSeen(snapshot); saveVisitBaseline(snapshot); setRevision(value => value + 1); };
  const select = id => onSelectAgent?.({ kind: "agent", id });
  return <section className="visit-briefing" aria-label="Since your last visit">
    <details>
      <summary><span>Since your last visit</span><span className="visit-briefing-count">{count} {count === 1 ? "update" : "updates"}</span></summary>
      <div className="visit-briefing-body">
        <p>Retained changes since <time dateTime={summary.since}>{new Date(summary.since).toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" })}</time>, and requests still waiting.</p>
        {summary.completedTasks.length > 0 && <div><h3>Completed tasks</h3><ul>{summary.completedTasks.map(task => <li key={task.id}><button onClick={() => onOpenArchive?.()}>{task.title || task.id}</button></li>)}</ul></div>}
        {summary.arrivals.length > 0 && <div><h3>New arrivals</h3><ul>{summary.arrivals.map(agent => <li key={agent.id}><button onClick={() => select(agent.id)}>{agent.name || agent.id}</button></li>)}</ul></div>}
        {summary.failures.length > 0 && <div><h3>Failures</h3><ul>{summary.failures.map(({ kind, record }) => <li key={`${kind}:${record.id}`}><button onClick={() => kind === "agent" ? select(record.id) : onOpenTask?.(record.id)}>{record.name || record.title || record.id}</button></li>)}</ul></div>}
        {summary.approvals.length > 0 && <div><h3>Unanswered requests</h3><ul>{summary.approvals.map(approval => <li key={approval.request_id}><button onClick={() => onReviewApprovals?.()}>{approval.message || approval.request_id}</button></li>)}</ul></div>}
        <button className="visit-briefing-seen" onClick={markSeen}>Mark seen</button>
      </div>
    </details>
  </section>;
}
