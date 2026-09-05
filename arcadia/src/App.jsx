import { TelemetryWarning } from "./panels/TelemetryWarning.jsx";
import { useEffect, useRef, useState } from "react";
import { pendingApprovals } from "./contract/approvals.js";
import {
  ContractValidationError,
  parseSnapshot,
  UnsupportedSchemaVersionError,
} from "./contract/parseSnapshot.js";
import { VillageExperience } from "./panels/VillageExperience.jsx";
import { ApprovalProvider, ApprovalKnocks } from "./panels/AgentAttention.jsx";
import { ReadOnlyPanels } from "./panels/ReadOnlyPanels.jsx";
import { createStateTransport } from "./transport/createStateTransport.js";

const mono = "font-mono text-xs uppercase tracking-[0.12em]";

function ContractMismatch({ error }) {
  return (
    <main className="grid min-h-screen place-items-center bg-[#2a1817] p-4 text-[#f4dfcc]">
      <section
        className="max-w-2xl border-l-[5px] border-[#d96b54] px-6 py-5"
        role="alert"
      >
        <p className={mono}>Contract mismatch</p>
        <h1 className="my-2 text-[clamp(2rem,5vw,4rem)] font-normal tracking-[-0.06em]">
          Arcadia cannot enter this village.
        </h1>
        <p>
          {error instanceof Error ? error.message : "Invalid village snapshot"}
        </p>
      </section>
    </main>
  );
}

export function backendFromLocation(search = window.location.search) {
  if (import.meta.env.DEV)
    return new URLSearchParams(search).get("backend") || "/chronicle";
  return "/chronicle";
}

export function LiveApp({
  baseUrl = backendFromLocation(),
  stewardClient = null,
  transportFactory = createStateTransport,
}) {
  const [envelope, setEnvelope] = useState(null);
  const [contractError, setContractError] = useState(null);
  const [transportError, setTransportError] = useState(null);
  const [connectionStatus, setConnectionStatus] = useState("connecting");

  useEffect(() => {
    const transport = transportFactory({
      fetch: globalThis.fetch.bind(globalThis),
      EventSource: window.EventSource,
      baseUrl,
      onStatus: (status) => {
        setConnectionStatus(status);
        if (status === "live") setTransportError(null);
      },
      onEnvelope: (nextEnvelope) => {
        setEnvelope(nextEnvelope);
        setContractError(null);
        setTransportError(null);
      },
      onError: (error) => {
        if (
          error instanceof UnsupportedSchemaVersionError ||
          error instanceof ContractValidationError
        ) {
          setContractError(error);
        } else setTransportError(error);
      },
    });
    transport.start().catch(() => {});
    return () => transport.close();
  }, [baseUrl, transportFactory]);

  if (contractError) return <ContractMismatch error={contractError} />;
  if (transportError && !envelope) {
    return (
      <main className="grid min-h-screen place-items-center bg-[#eee5d1] p-4 text-[#15241c]">
        <p
          className="border border-[#d96b54] bg-[#faf6eb] px-5 py-4 font-mono text-sm"
          role="alert"
        >
          Chronicle is unavailable. Arcadia will keep trying to reconnect.
        </p>
      </main>
    );
  }
  return (
    <App
      envelope={envelope}
      stewardClient={stewardClient}
      connectionStatus={transportError ? "reconnecting" : connectionStatus}
    />
  );
}

function StewardSnapshotBridge({ client, snapshot }) {
  useEffect(() => {
    client?.confirm(snapshot);
  }, [client, snapshot]);
  return null;
}

export function App({
  envelope,
  stewardClient = null,
  connectionStatus = "live",
}) {
  if (envelope == null) {
    return (
      <main className="grid min-h-screen place-items-center bg-[#eee5d1] p-4 text-[#15241c]">
        <p
          className="border border-[#785a25] bg-[#faf6eb] px-5 py-4 font-mono text-sm"
          role="status"
        >
          Village snapshot has not loaded yet.
        </p>
      </main>
    );
  }

  let snapshot;
  try {
    snapshot = parseSnapshot(envelope);
  } catch (error) {
    return <ContractMismatch error={error} />;
  }

  return <VillageShell snapshot={snapshot} stewardClient={stewardClient} connectionStatus={connectionStatus} />;
}

function viewFromHash(hash) {
  if (hash === "#records") return "records";
  if (hash === "#approvals" || hash.startsWith("#approval-")) return "approvals";
  return "village";
}

function VillageShell({ snapshot, stewardClient, connectionStatus }) {
  const [hash, setHash] = useState(() => window.location.hash);
  const heading = useRef(null);
  const villageHeading = useRef(null);
  const view = viewFromHash(hash);
  const [visitedVillage, setVisitedVillage] = useState(view === "village");
  useEffect(() => { if (view === "village") setVisitedVillage(true); }, [view]);
  const previousView = useRef(view);
  const approvals = pendingApprovals(snapshot.approvals);
  useEffect(() => {
    const changed = () => setHash(window.location.hash);
    window.addEventListener("hashchange", changed);
    window.addEventListener("popstate", changed);
    return () => {
      window.removeEventListener("hashchange", changed);
      window.removeEventListener("popstate", changed);
    };
  }, []);
  useEffect(() => {
    const previous = previousView.current;
    previousView.current = view;
    if (view === "village") {
      if (previous !== "village") {
        villageHeading.current?.focus({ preventScroll: true });
        villageHeading.current?.scrollIntoView?.({ block: "start", behavior: "auto" });
      }
      return;
    }
    heading.current?.focus({ preventScroll: true });
    const target = document.getElementById(hash.slice(1)) || heading.current;
    target?.scrollIntoView?.({ block: "start", behavior: "auto" });
  }, [hash, view]);
  function navigate(hash) {
    if (window.location.hash !== hash) window.history.pushState(null, "", hash);
    setHash(hash);
  }
  function followInternalLink(event) {
    if (event.defaultPrevented || event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
    const link = event.target.closest?.("a[href]");
    const target = link?.getAttribute("href");
    if (!target || !["#village", "#records", "#approvals"].includes(target) && !target.startsWith("#approval-")) return;
    event.preventDefault();
    navigate(target);
  }
  return (
    <ApprovalProvider snapshot={snapshot} stewardClient={stewardClient}>
      <main className="arcadia-shell" onClick={followInternalLink}>
        <StewardSnapshotBridge client={stewardClient} snapshot={snapshot} />
        <header className="masthead">
          <a className="wordmark" href="/" aria-label="Arcadia home">
            <span aria-hidden="true">♧</span> WARREN / ARCADIA
          </a>
          <nav aria-label="Main navigation">
            <a href="#village" aria-current={view === "village" ? "page" : undefined}>Village</a>
            <a href="#records" aria-current={view === "records" ? "page" : undefined}>Village records</a>
            <a href="#approvals" aria-current={view === "approvals" ? "page" : undefined}>Requests{approvals.length ? ` (${approvals.length})` : ""}</a>
            <a href="/observatory/">Townhall ↗</a>
          </nav>
        </header>
        <section className="village-intro">
          <div>
            <h1 ref={villageHeading} tabIndex={-1}>Arcadia</h1>
            <p>
              Follow the work. Meet the people. Find your place in the village.
            </p>
          </div>
          <div className="village-summary">
            <span
              className={`connection connection-${connectionStatus}`}
              role="status"
              aria-label="Village connection"
            >
              <i />
              {connectionStatus === "live"
                ? "Live village"
                : "Reconnecting · showing last snapshot"}
            </span>
            <p>
              {snapshot.villagers.filter((v) => v.state === "working").length}{" "}
              working <span> / </span>
              {pendingApprovals(snapshot.approvals).length} awaiting an answer
            </p>
          </div>
        </section>
        <TelemetryWarning snapshot={snapshot} />
        {(visitedVillage || view === "village") && <div className="village-scene-view" hidden={view !== "village"}><VillageExperience snapshot={snapshot} stewardClient={stewardClient} active={view === "village"} /></div>}
        {view !== "village" && <section className={`village-secondary-view ${view === "records" ? "records-page" : "requests-page"}`} aria-label={view === "records" ? "Village records" : "Village requests"} id={view === "records" ? "records" : "requests-view"}>
          <header className="secondary-view-heading">
            <div><p className="eyebrow">{view === "records" ? "The village almanac" : "Waiting for your answer"}</p><h2 ref={heading} tabIndex={-1}>{view === "records" ? "Village records" : "Requests"}</h2><p>{view === "records" ? "Recorded work, routines, and resident declarations." : "Review pending requests and send an answer to Steward."}</p></div>
            <a className="back-to-village" href="#village">← Back to village</a>
          </header>
          {view === "records" ? <ReadOnlyPanels snapshot={snapshot} /> : approvals.length ? <ApprovalKnocks snapshot={snapshot} stewardClient={stewardClient} /> : <p className="requests-empty" id="approvals">No requests are waiting for an answer.</p>}
        </section>}
        <footer className="village-footer">
          <span>Arcadia · a window into Warren</span>
          <span>An original miniature world · Powered by Warren</span>
        </footer>
      </main>
    </ApprovalProvider>
  );
}
