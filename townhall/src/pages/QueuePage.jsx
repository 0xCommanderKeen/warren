import { useEffect, useState } from "react";
import { Gate } from "../console/Gate.jsx";
import {
  Button,
  Empty,
  Loading,
  PageHead,
  Panel,
  Problem,
} from "../console/ui.jsx";
import { useSteward, useStewardQuery } from "../steward/context.jsx";

const issueUrl = (repo, number) =>
  `https://github.com/${repo}/issues/${number}`;
const External = ({ href, children }) => (
  <a
    className="text-ember underline underline-offset-4"
    href={href}
    target="_blank"
    rel="noreferrer"
  >
    {children}
  </a>
);

function RecommendationNote({ queue }) {
  const { report, repository } = queue;
  const states = new Map(
    queue.ranked_items.map((item) => [item.number, item.state]),
  );
  return (
    <Panel title="The resident’s recommendation">
      {report.note ? (
        <>
          <p className="text-[12px] text-dim">
            {report.run.resident} · recorded {report.run.recorded_at} · run{" "}
            <code>{report.run.run_id}</code>
          </p>
          <p className="text-[12px] text-dim">
            Code inspected at{" "}
            <External
              href={`https://github.com/${repository}/commit/${report.note.commit}`}
            >
              {report.note.commit.slice(0, 12)}
            </External>
            . Issue states below come from the tracker observation.
          </p>
          <ol className="space-y-5 pl-5">
            {report.note.recommendations.map((item, index) => (
              <li key={`${item.number}:${index}`}>
                <External href={issueUrl(repository, item.number)}>
                  #{item.number}
                </External>
                <span className="ml-3 text-[11px] uppercase text-faint">
                  {states.get(item.number) || "unknown"}
                </span>
                <p className="text-[13px] leading-relaxed text-ink">
                  {item.reason}
                </p>
                <details className="text-[12px] text-dim">
                  <summary className="cursor-pointer">Evidence</summary>
                  {item.evidence.map((evidence, i) => (
                    <div className="mt-2 border-l border-rule pl-3" key={i}>
                      <code className="break-words">{evidence.source}</code>
                      <pre className="whitespace-pre-wrap font-mono">
                        {evidence.quote}
                      </pre>
                    </div>
                  ))}
                </details>
              </li>
            ))}
          </ol>
          {!report.note.recommendations.length && (
            <p>No work recommended in this review.</p>
          )}
        </>
      ) : (
        <p className="text-[13px] text-dim">
          {report.message}{" "}
          {report.run
            ? `Run ${report.run.run_id}: ${report.run.outcome}.`
            : `Waiting for ${queue.reporter}’s first recorded review.`}
        </p>
      )}
    </Panel>
  );
}

function Queue() {
  const { client } = useSteward();
  const [since, setSince] = useState(() =>
    new Date(Date.now() - 86400000).toISOString().slice(0, 10),
  );
  const [label, setLabel] = useState("");
  const { data, error, loading, refresh } = useStewardQuery(
    (signal) =>
      client.readQueue({
        signal,
        query: { since: since ? `${since}T00:00:00Z` : undefined },
      }),
    [since],
  );
  useEffect(() => {
    const timer = window.setInterval(refresh, 60000);
    return () => window.clearInterval(timer);
  }, [refresh]);
  if (loading && !data) return <Loading>Reading the issue tracker…</Loading>;
  if (error)
    return (
      <>
        <Problem error={error} />
        <Button onClick={refresh}>Try again</Button>
      </>
    );
  if (!data) return null;
  const labels = [
    ...new Set(data.issues.flatMap((issue) => issue.labels)),
  ].sort();
  const issues = data.issues.filter(
    (issue) => !label || issue.labels.includes(label),
  );
  const stale = data.issues.filter((issue) => issue.stale_blocked);
  return (
    <>
      <div className="mb-6 flex flex-wrap items-end gap-4 text-[12px]">
        <label className="flex flex-col gap-2 text-dim">
          Closed since (UTC)
          <input
            className="border border-rule bg-deep p-2 text-ink"
            type="date"
            value={since}
            onChange={(event) => setSince(event.target.value)}
          />
        </label>
        <label className="flex flex-col gap-2 text-dim">
          Issue label
          <select
            className="border border-rule bg-deep p-2 text-ink"
            value={label}
            onChange={(event) => setLabel(event.target.value)}
          >
            <option value="">All labels</option>
            {labels.map((value) => (
              <option key={value}>{value}</option>
            ))}
          </select>
        </label>
        <Button onClick={refresh} disabled={loading}>
          {loading ? "Reading…" : "Refresh"}
        </Button>
      </div>
      <p className="mb-5 text-[12px] text-dim">
        <External href={`https://github.com/${data.repository}`}>
          {data.repository}
        </External>{" "}
        · observed {data.observed_at}. Checks for updates every minute. Reads
        share a cached observation; refreshing does not bypass the tracker rate
        limit.
      </p>
      {stale.length > 0 && (
        <Panel title={`${stale.length} blocked labels need review`}>
          <p className="text-[13px] text-dim">
            Every explicit blocker is closed for these issues. The tracker
            labels have not been changed.
          </p>
          <div className="flex flex-wrap gap-3">
            {stale.map((issue) => (
              <External key={issue.number} href={issue.url}>
                #{issue.number}
              </External>
            ))}
          </div>
        </Panel>
      )}
      <RecommendationNote queue={data} />
      <Panel title={`Open issues · ${issues.length}`}>
        {!issues.length ? (
          <Empty title="No matching open issues." />
        ) : (
          <ul className="m-0 list-none divide-y divide-rule p-0">
            {issues.map((issue) => (
              <li key={issue.number} className="py-4 first:pt-0">
                <External href={issue.url}>
                  #{issue.number} · {issue.title}
                </External>
                <p className="my-2 text-[11px] text-faint">
                  {issue.labels.join(" · ") || "No labels"}
                </p>
                {issue.blockers.length > 0 && (
                  <div className="flex flex-wrap gap-3 text-[12px] text-dim">
                    Blocked by{" "}
                    {issue.blockers.map((blocker) => (
                      <span key={blocker.number}>
                        <External
                          href={issueUrl(data.repository, blocker.number)}
                        >
                          #{blocker.number}
                        </External>{" "}
                        {blocker.state}
                      </span>
                    ))}
                  </div>
                )}
                {issue.unknown_blockers.length > 0 && (
                  <p className="text-[12px] text-wait">
                    Unresolved dependency text:{" "}
                    {issue.unknown_blockers.join("; ")}
                  </p>
                )}
                {issue.chains.length > 0 && (
                  <details className="mt-2 text-[12px] text-dim">
                    <summary className="cursor-pointer">
                      Dependency chains
                    </summary>
                    {issue.chains.map((path, i) => (
                      <p key={i}>
                        {path.map((number) => `#${number}`).join(" → ")}
                      </p>
                    ))}
                    {issue.chains_truncated && (
                      <p>
                        Chain display limit reached; additional paths exist.
                      </p>
                    )}
                  </details>
                )}
              </li>
            ))}
          </ul>
        )}
      </Panel>
      <Panel title={`Open pull requests · ${data.pull_requests.length}`}>
        {data.pull_requests.length ? (
          <ul className="list-none space-y-3 p-0">
            {data.pull_requests.map((pr) => (
              <li key={pr.number} className="text-[13px]">
                <External href={pr.url}>
                  #{pr.number} · {pr.title}
                </External>{" "}
                <span
                  className={
                    pr.mergeability === "CONFLICTING" ? "text-fail" : "text-dim"
                  }
                >
                  {pr.mergeability.toLowerCase()}
                </span>
              </li>
            ))}
          </ul>
        ) : (
          <p className="text-[13px] text-dim">No open pull requests.</p>
        )}
      </Panel>
      <Panel title={`Recently closed · ${data.recently_closed.length}`}>
        {data.recently_closed.length ? (
          <ul className="list-none space-y-3 p-0">
            {data.recently_closed.map((issue) => (
              <li key={issue.number} className="text-[13px]">
                <External href={issue.url}>
                  #{issue.number} · {issue.title}
                </External>{" "}
                <span className="text-dim">{issue.closed_at}</span>
              </li>
            ))}
          </ul>
        ) : (
          <p className="text-[13px] text-dim">
            No issues closed in this window.
          </p>
        )}
      </Panel>
    </>
  );
}

export default function QueuePage() {
  const { locked } = useSteward();
  return (
    <>
      <PageHead title="Queue">
        What can move next. Tracker facts are computed; the resident’s
        recommendation carries its own evidence and run receipt.
      </PageHead>
      {locked ? <Gate what="The work queue" /> : <Queue />}
    </>
  );
}
