import { createContext, useContext, useEffect, useId, useState } from "react";
import { pendingApprovals } from "../contract/approvals.js";
import "./agent-attention.css";

const mono = "font-mono text-xs uppercase tracking-[0.12em]";
const ApprovalContext = createContext(null);

// Both views share one presentation lock. StewardClient remains the write authority.
export function ApprovalProvider({ snapshot, stewardClient, children }) {
  const [error, setError] = useState(null);
  const [submittedRequestId, setSubmittedRequestId] = useState(null);
  const [credentialsReady, setCredentialsReady] = useState(
    () => typeof stewardClient?.setCredentials !== "function",
  );
  useEffect(() => {
    if (
      submittedRequestId &&
      !pendingApprovals(snapshot.approvals).some(
        (approval) => approval.request_id === submittedRequestId,
      )
    ) {
      setSubmittedRequestId(null);
      setError(null);
    }
  }, [snapshot, submittedRequestId]);
  async function decide(approval, body) {
    if (submittedRequestId !== null || !stewardClient) return;
    setError(null);
    setSubmittedRequestId(approval.request_id);
    try {
      await stewardClient.decideApproval(approval.request_id, body);
    } catch (writeError) {
      setError(
        writeError instanceof Error
          ? writeError.message
          : "Steward could not record the answer",
      );
      if (writeError?.ambiguous !== true) {
        setSubmittedRequestId(null);
        if (
          writeError?.status === 401 ||
          writeError?.code === "credentials_required"
        )
          setCredentialsReady(false);
      }
    }
  }
  function unlock(token) {
    if (!token.trim()) return;
    stewardClient?.setCredentials({ token });
    setCredentialsReady(true);
  }
  function choose(approval, option) {
    if (option === "approve" || option === "deny")
      return decide(approval, { decision: option });
    if (option !== "edit") return;
    const entered = window.prompt(
      "Edit approval detail as JSON",
      JSON.stringify(approval.detail),
    );
    if (entered === null) return;
    try {
      const edit = JSON.parse(entered);
      if (edit === null || typeof edit !== "object" || Array.isArray(edit))
        throw new Error("Steward edits must be JSON objects");
      return decide(approval, { decision: "edit", edit });
    } catch (editError) {
      setError(
        editError instanceof Error ? editError.message : "Invalid edit JSON",
      );
    }
  }
  return (
    <ApprovalContext.Provider
      value={{
        error,
        submittedRequestId,
        credentialsReady,
        unlock,
        choose,
        stewardClient,
      }}
    >
      {children}
    </ApprovalContext.Provider>
  );
}

function optionLabel(option) {
  return typeof option === "string" ? option : JSON.stringify(option);
}

export function ApprovalKnocks({
  snapshot,
  stewardClient: suppliedClient,
  agentId,
}) {
  const controller = useContext(ApprovalContext);
  if (!controller)
    return (
      <ApprovalProvider snapshot={snapshot} stewardClient={suppliedClient}>
        <ApprovalKnocks snapshot={snapshot} agentId={agentId} />
      </ApprovalProvider>
    );
  return (
    <ApprovalView
      snapshot={snapshot}
      agentId={agentId}
      controller={controller}
    />
  );
}

function ApprovalView({ snapshot, agentId, controller }) {
  const { error, submittedRequestId, credentialsReady, choose, stewardClient } =
    controller;
  const [token, setToken] = useState("");
  const tokenId = useId();
  useEffect(() => {
    if (credentialsReady) setToken("");
  }, [credentialsReady]);
  const villagers = new Map(
    snapshot.villagers.map((villager) => [villager.id, villager]),
  );
  const approvals = pendingApprovals(snapshot.approvals)
    .filter((approval) => !agentId || approval.agent_id === agentId)
    .toSorted(
      (left, right) =>
        left.opened_at.localeCompare(right.opened_at) ||
        left.request_id.localeCompare(right.request_id),
    );
  function unlock(event) {
    event.preventDefault();
    controller.unlock(token);
    setToken("");
  }
  if (!approvals.length) return null;
  return (
    <section
      aria-busy={submittedRequestId !== null}
      aria-label={agentId ? "Agent approval requests" : "Approval knocks"}
      className={
        agentId ? "approval-knocks agent-approval-knocks" : "approval-knocks"
      }
      id={agentId ? undefined : "approvals"}
    >
      {!credentialsReady ? (
        <form
          className="border-2 border-[#2a1817] bg-[#fff8e7] p-3 shadow-[5px_5px_0_#785a25]"
          onSubmit={unlock}
        >
          <label className={`${mono} block text-[#785a25]`} htmlFor={tokenId}>
            Steward token
          </label>
          <div className="mt-2 flex gap-2">
            <input
              autoComplete="off"
              className="min-w-0 flex-1 border border-[#2a1817] bg-white px-2 py-1 font-mono text-sm"
              id={tokenId}
              onChange={(event) => setToken(event.target.value)}
              type="password"
              value={token}
            />
            <button
              className="border border-[#2a1817] bg-[#eee5d1] px-3 py-1 font-mono text-xs uppercase"
              type="submit"
            >
              Unlock answers
            </button>
          </div>
          <p className="mt-2 font-mono text-xs text-[#566158]">
            Kept in this tab only.
          </p>
        </form>
      ) : null}
      {approvals.map((approval) => {
        const villager = villagers.get(approval.agent_id);
        return (
          <article
            className="border-2 border-[#2a1817] bg-[#fff8e7] p-3 shadow-[5px_5px_0_#d96b54]"
            key={approval.request_id}
            id={
              agentId
                ? undefined
                : `approval-${encodeURIComponent(approval.request_id)}`
            }
          >
            <p className={`${mono} text-[#9a3f32]`}>
              Knock · {villager?.name || approval.agent_id}
            </p>
            <h2 className="my-1 text-xl font-normal">{approval.message}</h2>
            {approval.detail && Object.keys(approval.detail).length > 0 ? (
              <details className="approval-detail">
                <summary>Request details</summary>
                <pre>{JSON.stringify(approval.detail, null, 2)}</pre>
              </details>
            ) : null}
            <div className="flex flex-wrap gap-2">
              {approval.options.map((option, optionIndex) => {
                const label = optionLabel(option);
                const actionable =
                  option === "approve" ||
                  option === "deny" ||
                  option === "edit";
                return (
                  <button
                    aria-label={`${label[0]?.toUpperCase() || ""}${label.slice(1)} ${approval.message}`}
                    className="border border-[#2a1817] bg-[#eee5d1] px-3 py-1.5 font-mono text-xs uppercase tracking-[0.1em] shadow-[2px_2px_0_#2a1817] enabled:cursor-pointer enabled:hover:translate-x-px enabled:hover:translate-y-px enabled:hover:shadow-none disabled:opacity-50"
                    disabled={
                      !actionable ||
                      !stewardClient ||
                      !credentialsReady ||
                      submittedRequestId !== null
                    }
                    key={`${optionIndex}:${label}`}
                    onClick={() => choose(approval, option)}
                    type="button"
                  >
                    {label}
                  </button>
                );
              })}
            </div>
          </article>
        );
      })}
      {submittedRequestId && !error ? (
        <p
          className="border border-[#785a25] bg-[#fff8e7] p-2 font-mono text-xs"
          role="status"
        >
          Answer sent. Waiting for Steward's confirming state…
        </p>
      ) : null}
      {error ? (
        <p
          className="border border-[#d96b54] bg-[#2a1817] p-2 text-sm text-[#fff8e7]"
          role="alert"
        >
          {error}
        </p>
      ) : null}
    </section>
  );
}

export function AgentAttention({ snapshot, stewardClient, agentId }) {
  const failedTasks = snapshot.tasks.filter(
    (task) =>
      task.state === "failed" &&
      (task.claimant === agentId ||
        (!task.claimant && task.assignee === agentId)),
  );
  const failedRuns = snapshot.routines.filter(
    (run) => run.agent_id === agentId && run.state === "failed",
  );
  const agent = snapshot.villagers.find((villager) => villager.id === agentId);
  const failedEvents = (agent?.history || [])
    .filter((event) => /(^|_)(failed|failure|error)($|_)/.test(event.type))
    .toSorted((a, b) => b.ts.localeCompare(a.ts))
    .slice(0, 3);
  const hasFailures =
    failedTasks.length ||
    failedRuns.length ||
    failedEvents.length ||
    agent?.state === "failed";
  return (
    <div className="agent-attention">
      <ApprovalKnocks
        snapshot={snapshot}
        stewardClient={stewardClient}
        agentId={agentId}
      />
      {hasFailures ? (
        <section
          className="agent-failure-context"
          aria-label="Agent failure context"
        >
          <h4>Needs a closer look</h4>
          {agent?.state === "failed" && (
            <p>
              {agent.last_line ||
                "This agent is in a failed state. No further detail was recorded."}
            </p>
          )}
          {failedTasks.map((task) => (
            <p key={task.id}>
              <strong>Failed task</strong>
              {task.title}
            </p>
          ))}
          {failedRuns.map((run) => (
            <p key={run.run_id}>
              <strong>Failed routine · {run.routine}</strong>
              {run.error || run.outcome || "No error detail recorded."}
            </p>
          ))}
          {failedEvents.map((event, index) => (
            <p key={`${event.ts}:${index}`}>
              <strong>{event.type.replaceAll("_", " ")}</strong>
              <time dateTime={event.ts}>{event.ts}</time>
              {typeof event.payload?.message === "string"
                ? event.payload.message
                : typeof event.payload?.error === "string"
                  ? event.payload.error
                  : null}
            </p>
          ))}
          <a href="#records">Review village records →</a>
        </section>
      ) : null}
    </div>
  );
}
