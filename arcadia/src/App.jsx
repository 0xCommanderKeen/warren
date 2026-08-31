import { useEffect, useState } from "react";
import { pendingApprovals } from "./contract/approvals.js";
import { parseSnapshot, UnsupportedSchemaVersionError } from "./contract/parseSnapshot.js";
import { PhaserGame } from "./game/PhaserGame.jsx";
import { ReadOnlyPanels } from "./panels/ReadOnlyPanels.jsx";
import { createStateTransport } from "./transport/createStateTransport.js";

const mono = "font-mono text-xs uppercase tracking-[0.12em]";

function ContractMismatch({ error }) {
  return (
    <main className="grid min-h-screen place-items-center bg-[#2a1817] p-4 text-[#f4dfcc]">
      <section className="max-w-2xl border-l-[5px] border-[#d96b54] px-6 py-5" role="alert">
        <p className={mono}>Contract mismatch</p>
        <h1 className="my-2 text-[clamp(2rem,5vw,4rem)] font-normal tracking-[-0.06em]">Arcadia cannot enter this village.</h1>
        <p>{error instanceof Error ? error.message : "Invalid village snapshot"}</p>
      </section>
    </main>
  );
}

export function backendFromLocation(search = window.location.search) {
  return new URLSearchParams(search).get("backend") || "/burrow";
}

export function LiveApp({
  baseUrl = backendFromLocation(),
  stewardClient = null,
  transportFactory = createStateTransport,
}) {
  const [envelope, setEnvelope] = useState(null);
  const [contractError, setContractError] = useState(null);
  const [transportError, setTransportError] = useState(null);

  useEffect(() => {
    const transport = transportFactory({
      fetch: globalThis.fetch.bind(globalThis),
      EventSource: window.EventSource,
      baseUrl,
      onEnvelope: (nextEnvelope) => {
        setEnvelope(nextEnvelope);
        setContractError(null);
        setTransportError(null);
      },
      onError: (error) => {
        if (error instanceof UnsupportedSchemaVersionError) setContractError(error);
        else setTransportError(error);
      },
    });
    transport.start().catch(() => {});
    return () => transport.close();
  }, [baseUrl, transportFactory]);

  if (contractError) return <ContractMismatch error={contractError} />;
  if (transportError && !envelope) {
    return (
      <main className="grid min-h-screen place-items-center bg-[#eee5d1] p-4 text-[#15241c]">
        <p className="border border-[#d96b54] bg-[#faf6eb] px-5 py-4 font-mono text-sm" role="alert">
          Burrow is unavailable. Arcadia will keep trying to reconnect.
        </p>
      </main>
    );
  }
  return <App envelope={envelope} stewardClient={stewardClient} />;
}

function StewardSnapshotBridge({ client, snapshot }) {
  useEffect(() => {
    client?.confirm(snapshot);
  }, [client, snapshot]);
  return null;
}

function ApprovalKnocks({ snapshot, stewardClient }) {
  const [error, setError] = useState(null);
  const [submittedRequestId, setSubmittedRequestId] = useState(null);
  const [credentialsReady, setCredentialsReady] = useState(
    () => typeof stewardClient?.setCredentials !== "function",
  );
  const [token, setToken] = useState("");
  const villagers = new Map(snapshot.villagers.map((villager) => [villager.id, villager]));
  const approvals = pendingApprovals(snapshot.approvals)
    .toSorted((left, right) =>
      left.opened_at.localeCompare(right.opened_at) ||
      left.request_id.localeCompare(right.request_id));

  useEffect(() => {
    if (submittedRequestId && !approvals.some((approval) =>
      approval.request_id === submittedRequestId)) {
      setSubmittedRequestId(null);
      setError(null);
    }
  }, [approvals, submittedRequestId]);

  if (approvals.length === 0) return null;

  async function decide(approval, decision) {
    setError(null);
    setSubmittedRequestId(approval.request_id);
    try {
      await stewardClient.decideApproval(approval.request_id, { decision });
    } catch (writeError) {
      setError(writeError instanceof Error ? writeError.message : "Steward could not record the answer");
      if (writeError?.ambiguous !== true) setSubmittedRequestId(null);
    }
  }

  function unlock(event) {
    event.preventDefault();
    if (!token.trim()) return;
    stewardClient.setCredentials({ token });
    setToken("");
    setCredentialsReady(true);
  }

  return (
    <section
      aria-busy={submittedRequestId !== null}
      aria-label="Approval knocks"
      className="absolute top-3 left-3 z-3 grid max-h-[calc(100%-1.5rem)] w-[min(28rem,calc(100%-1.5rem))] gap-2 overflow-auto"
    >
      {!credentialsReady ? (
        <form className="border-2 border-[#2a1817] bg-[#fff8e7] p-3 shadow-[5px_5px_0_#785a25]" onSubmit={unlock}>
          <label className={`${mono} block text-[#785a25]`} htmlFor="steward-token">Steward token</label>
          <div className="mt-2 flex gap-2">
            <input
              autoComplete="off"
              className="min-w-0 flex-1 border border-[#2a1817] bg-white px-2 py-1 font-mono text-sm"
              id="steward-token"
              onChange={(event) => setToken(event.target.value)}
              type="password"
              value={token}
            />
            <button className="border border-[#2a1817] bg-[#eee5d1] px-3 py-1 font-mono text-xs uppercase" type="submit">
              Unlock answers
            </button>
          </div>
          <p className="mt-2 font-mono text-xs text-[#566158]">Kept in this tab only.</p>
        </form>
      ) : null}
      {approvals.map((approval) => {
        const villager = villagers.get(approval.agent_id);
        return (
          <article className="border-2 border-[#2a1817] bg-[#fff8e7] p-3 shadow-[5px_5px_0_#d96b54]" key={approval.request_id}>
            <p className={`${mono} text-[#9a3f32]`}>Knock · {villager?.name || approval.agent_id}</p>
            <h2 className="my-1 text-xl font-normal">{approval.message}</h2>
            {approval.detail && Object.keys(approval.detail).length > 0 ? (
              <p className="mb-2 font-mono text-xs text-[#566158]">{JSON.stringify(approval.detail)}</p>
            ) : null}
            <div className="flex flex-wrap gap-2">
              {approval.options.map((option) => (
                <button
                  aria-label={`${option[0].toUpperCase()}${option.slice(1)} ${approval.message}`}
                  className="border border-[#2a1817] bg-[#eee5d1] px-3 py-1.5 font-mono text-xs uppercase tracking-[0.1em] shadow-[2px_2px_0_#2a1817] enabled:cursor-pointer enabled:hover:translate-x-px enabled:hover:translate-y-px enabled:hover:shadow-none disabled:opacity-50"
                  disabled={!stewardClient || !credentialsReady || submittedRequestId !== null}
                  key={option}
                  onClick={() => decide(approval, option)}
                  type="button"
                >
                  {option}
                </button>
              ))}
            </div>
          </article>
        );
      })}
      {submittedRequestId && !error ? (
        <p className="border border-[#785a25] bg-[#fff8e7] p-2 font-mono text-xs" role="status">
          Answer sent. Waiting for Steward's confirming state…
        </p>
      ) : null}
      {error ? <p className="border border-[#d96b54] bg-[#2a1817] p-2 text-sm text-[#fff8e7]" role="alert">{error}</p> : null}
    </section>
  );
}

export function App({ envelope, stewardClient = null }) {
  if (envelope == null) {
    return (
      <main className="grid min-h-screen place-items-center bg-[#eee5d1] p-4 text-[#15241c]">
        <p className="border border-[#785a25] bg-[#faf6eb] px-5 py-4 font-mono text-sm" role="status">Village snapshot has not loaded yet.</p>
      </main>
    );
  }

  let snapshot;
  try {
    snapshot = parseSnapshot(envelope);
  } catch (error) {
    return <ContractMismatch error={error} />;
  }

  return (
    <main className="min-h-screen bg-[#eee5d1] p-[clamp(1rem,3vw,2.5rem)] font-serif text-[#15241c]">
      <StewardSnapshotBridge client={stewardClient} snapshot={snapshot} />
      <header className="mx-auto mb-4 flex max-w-7xl items-end justify-between max-sm:flex-col max-sm:items-start max-sm:gap-2">
        <div>
          <p className={mono}>Burrow · generation {snapshot.generation}</p>
          <h1 className="text-[clamp(2.5rem,7vw,5rem)] font-normal tracking-[-0.06em]">Arcadia</h1>
        </div>
        <p className={mono}>{snapshot.villagers.length} villager online</p>
      </header>

      <section className="relative mx-auto aspect-[5/3] max-w-7xl overflow-hidden border border-[#1d3328] bg-[#9db57a] shadow-[10px_10px_0_#1d3328] max-sm:aspect-[4/5]" aria-label="Village">
        <PhaserGame snapshot={snapshot} />
        <ApprovalKnocks snapshot={snapshot} stewardClient={stewardClient} />
        <div className="pointer-events-none absolute right-3 bottom-3 z-2 flex max-w-[calc(100%-1.5rem)] flex-wrap justify-end gap-1.5" aria-label="Villagers">
          {snapshot.villagers.map((villager) => (
            <article className="grid grid-cols-[auto_1fr_auto] items-center gap-2 border border-[#1d3328] bg-[rgb(250_246_235/88%)] px-2.5 py-2 shadow-[2px_2px_0_rgb(29_51_40/70%)] max-sm:grid-cols-[auto_1fr]" key={villager.id}>
              <span className="h-7 w-2 bg-[var(--villager-accent,#6a7b67)]" style={{ "--villager-accent": villager.accent }} aria-hidden="true" />
              <div>
                <h2 className="text-sm font-normal">{villager.name}</h2>
                <p className="font-mono text-xs text-[#566158] max-sm:hidden">{villager.project || "Wandering"}</p>
              </div>
              <span className={`${mono} text-[#785a25] max-sm:hidden`}>{villager.state}</span>
            </article>
          ))}
        </div>
      </section>
      <ReadOnlyPanels snapshot={snapshot} />
    </main>
  );
}
