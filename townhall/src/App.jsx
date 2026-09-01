/* townhall — the warren control panel.
 *
 * The shell is the steward operator console's, ported rather than redesigned (warren#225):
 * a fixed rail on the left hosting every section, a single column of page to its right,
 * and a footer on the rail that says what is answering and what is not. The fleet views
 * this repo already had are one entry in that rail; the write pages are the rest.
 *
 * Two different servers answer this page and the split is deliberate. Chronicle's public
 * `/state` feeds the fleet, unauthenticated, exactly as before. Steward answers the write
 * pages, gated on an operator token a human types. Neither knows about the other.
 */

import { useEffect, useMemo, useState } from "react";
import { NAV, navFor, routeTo } from "./routes.js";
import { Link, NavigationProvider, useNavigation } from "./navigation.jsx";
import { StewardProvider, useSteward } from "./steward/context.jsx";
import { Button, Label, PageHead } from "./console/ui.jsx";
import { viewModel } from "./model.js";
import { knockCount, plural } from "./diagnostics.js";
import { createStateTransport } from "./transport.js";
import { LedgerProvider } from "./console/ledger.jsx";
import DiagnosticsPage from "./pages/DiagnosticsPage.jsx";
import FleetPage from "./pages/FleetPage.jsx";
import SkillsPage from "./pages/SkillsPage.jsx";
import ResidentsPage from "./pages/ResidentsPage.jsx";
import RoutinesPage from "./pages/RoutinesPage.jsx";
import ApprovalsPage from "./pages/ApprovalsPage.jsx";
import BoardPage from "./pages/BoardPage.jsx";
import BudgetsPage from "./pages/BudgetsPage.jsx";

/**
 * Which Chronicle answers `/state`. This origin, unless a developer running vite says so.
 *
 * Same shape and same reason as `stewardBase()` in steward/context.jsx: `import.meta.env.DEV`
 * is `false` in a built bundle, so Vite drops this branch and the deployed townhall has no
 * `?backend=` to honour (warren#241).
 */
function chronicleBase() {
  if (!import.meta.env.DEV) return "";
  try {
    return new URLSearchParams(window.location.search).get("backend") || "";
  } catch {
    return "";
  }
}

/** Chronicle's snapshot feed. Unchanged: poll once, then stream, and never write. */
function useFleetState() {
  const [snapshot, setSnapshot] = useState(null);
  const [status, setStatus] = useState("disconnected");
  useEffect(() => {
    const transport = createStateTransport({
      fetch: window.fetch.bind(window),
      EventSource: window.EventSource,
      baseUrl: chronicleBase(),
      onState: setSnapshot,
      onStatus: setStatus,
      warn: (message) => console.warn("Townhall:", message),
    });
    transport.poll().then(() => transport.connect());
    return () => transport.close();
  }, []);
  return { snapshot, status };
}

/* -- the rail ------------------------------------------------------------------------ */

const LINK_TONE = {
  ok: { dot: "bg-live", text: "text-ink" },
  bad: { dot: "bg-fail", text: "text-fail" },
  busy: { dot: "bg-wait breathe", text: "text-dim" },
  idle: { dot: "bg-faint", text: "text-dim" },
};

function LinkState({ tone, children, title }) {
  const look = LINK_TONE[tone] || LINK_TONE.idle;
  return (
    <div className={`my-2 flex items-center gap-2 ${look.text}`} title={title} aria-live="polite">
      <span className={`size-[7px] flex-none ${look.dot}`} />
      <span className="min-w-0 truncate">{children}</span>
    </div>
  );
}

const CHRONICLE_TONE = { live: "ok", polling: "busy", reconnecting: "busy", disconnected: "bad" };
const STEWARD_WORDS = {
  held: ["ok", "credential held"],
  open: ["ok", "steward runs open"],
  unknown: ["idle", "no credential yet"],
};

/* What this tab is actually carrying. A minted operator credential is the intended answer;
 * anything else held is almost certainly the master token, and saying so out loud is the
 * whole point of warren#225 — the master token in a browser is the thing being retired, so
 * a control panel that could not tell you it was there would be hiding it. */
const CREDENTIAL_NOTE = {
  operator: ["named operator — revocable, and your name is on every write it makes"],
  other: ["not an operator credential: likely the master token, which names nobody and cannot be revoked without a restart"],
};

/**
 * What the snapshot is complaining about, in the rail rather than only on its own page.
 *
 * A knock at a resident's chat bot is the one thing in the village an *outsider* causes, and
 * the reason warren#276 records it at all is for a person to notice. A page nobody has a
 * reason to open would not be noticing — so the count lives where the connection state does,
 * and says "knocks" out loud when there are any.
 */
function Complaints({ snapshot }) {
  const diagnostics = snapshot?.diagnostics || [];
  if (!diagnostics.length) return null;
  const knocks = knockCount(diagnostics);
  return (
    <Link
      to={routeTo.diagnostics()}
      className="mt-1 block text-[10px] text-dim no-underline hover:text-ink"
    >
      {plural(diagnostics.length, "diagnostic")}
      {knocks ? ` · ${plural(knocks, "knock")}` : ""}
    </Link>
  );
}

function Rail({ snapshot, chronicle }) {
  const { page } = useNavigation();
  const { credential, status } = useSteward();
  const current = navFor(page);
  const [stewardTone, stewardWords] = STEWARD_WORDS[status] || STEWARD_WORDS.unknown;
  const [credentialNote] = CREDENTIAL_NOTE[credential.kind?.()] || [null];

  return (
    <aside className="rail-gradient z-30 flex flex-col border-b border-rule px-5 py-4 rail:fixed rail:inset-y-0 rail:left-0 rail:w-[232px] rail:border-b-0 rail:border-r rail:px-0 rail:pb-[22px] rail:pt-[30px]">
      <header className="flex items-baseline gap-4 pr-4 rail:block rail:px-[26px] rail:pb-[26px]">
        <Link to={routeTo.fleet()} className="font-serif text-[27px] leading-none tracking-[.015em] text-ink no-underline">
          townhall
          <span className="ml-[7px] inline-block size-[7px] translate-y-1 bg-ember" />
        </Link>
        <div className="text-[10px] uppercase tracking-[.21em] text-faint rail:mt-[9px]">warren control panel</div>
      </header>

      <nav
        aria-label="Sections"
        className="flex flex-wrap rail:flex-col rail:flex-nowrap rail:border-t rail:border-rule-2"
      >
        {NAV.map((entry) => {
          const here = entry.nav === current;
          return (
            <Link
              key={entry.nav}
              to={entry.route}
              aria-current={here ? "page" : undefined}
              className={`flex items-baseline gap-3 border-t-2 px-3 py-2 text-[11.5px] uppercase tracking-[.13em] no-underline transition-colors rail:border-t-0 rail:border-b rail:border-b-rule-2 rail:border-l-2 rail:py-[11px] rail:pl-[25px] rail:pr-[26px] ${
                here
                  ? "border-t-ember bg-ember/[.06] text-ink rail:border-l-ember"
                  : "border-t-transparent text-dim hover:bg-ink/[.028] hover:text-ink rail:border-l-transparent"
              }`}
            >
              <i className={`not-italic text-[9.5px] tracking-[.1em] ${here ? "text-ember" : "text-faint"}`}>
                {entry.index}
              </i>
              {entry.label}
            </Link>
          );
        })}
      </nav>

      <footer className="mt-4 hidden px-[26px] rail:mt-auto rail:block rail:pt-[22px]">
        <Label>chronicle</Label>
        <LinkState tone={CHRONICLE_TONE[chronicle] || "idle"} title={snapshot?.cursor || undefined}>
          {chronicle}
        </LinkState>
        <div className="mb-4">
          <div className="text-[10px] text-faint">
            {snapshot ? `schema v${snapshot.schema_version} · gen ${snapshot.generation}` : "no snapshot yet"}
          </div>
          <Complaints snapshot={snapshot} />
        </div>

        <Label>steward</Label>
        <LinkState tone={stewardTone}>{stewardWords}</LinkState>
        {credentialNote ? (
          <div className="mb-3 text-[10px] leading-[1.55] text-faint">{credentialNote}</div>
        ) : null}
        <Button tiny disabled={status === "unknown"} onClick={() => credential.forget()}>
          forget credential
        </Button>

        <p className="mt-4 border-t border-rule-2 pt-[14px] text-[10.5px] leading-[1.6] text-faint">
          Chronicle is the village. Steward is the write path, and every answer on these pages
          is steward's own — not what the click intended.
        </p>
      </footer>

      {/* The same two facts, where the rail has collapsed into a bar. */}
      <div className="mt-3 flex items-center gap-5 text-[10px] rail:hidden">
        <LinkState tone={CHRONICLE_TONE[chronicle] || "idle"}>chronicle {chronicle}</LinkState>
        <LinkState tone={stewardTone}>{stewardWords}</LinkState>
      </div>
    </aside>
  );
}

/* -- the page ------------------------------------------------------------------------ */

const TITLES = {
  fleet: "Fleet", agent: "Record", residents: "Residents", resident: "Resident",
  residentNew: "New resident", residentDeclaration: "Declaration", routines: "Routines",
  approvals: "Approvals", board: "Job board", skills: "Skills", skill: "Skill",
  skillNew: "New skill", budgets: "Budgets", diagnostics: "Diagnostics",
};

function NotFound() {
  return (
    <>
      <PageHead title="No such page">
        This address does not name anything townhall serves. Nothing was looked up and nothing
        was guessed at — the rail on the left is the whole of what exists.
      </PageHead>
      <Link to={routeTo.fleet()} className="text-[11px] uppercase tracking-[.16em] text-ember no-underline">
        ← back to the fleet
      </Link>
    </>
  );
}

function Page({ model, page, params }) {
  switch (page) {
    case "fleet":
    case "agent":
      return <FleetPage model={model} page={page} params={params} />;
    case "skills":
    case "skill":
    case "skillNew":
      return <SkillsPage page={page} params={params} />;
    case "residents":
    case "resident":
    case "residentNew":
    case "residentDeclaration":
      return <ResidentsPage page={page} params={params} />;
    case "routines":
      return <RoutinesPage />;
    case "approvals":
      return <ApprovalsPage />;
    case "board":
      return <BoardPage />;
    case "budgets":
      return <BudgetsPage params={params} />;
    case "diagnostics":
      return <DiagnosticsPage model={model} />;
    default:
      return <NotFound />;
  }
}

function Shell() {
  const { page, params } = useNavigation();
  const { snapshot, status } = useFleetState();
  const model = useMemo(() => snapshot && viewModel(snapshot), [snapshot]);

  useEffect(() => {
    document.title = `${TITLES[page] || "Not found"} — townhall`;
  }, [page]);

  return (
    <div className="min-h-screen bg-void text-ink selection:bg-ember selection:text-on-ember">
      <div className="grain pointer-events-none fixed inset-0 z-0" aria-hidden="true" />
      <a
        href="#main"
        className="fixed left-[-999px] top-2 z-50 bg-ember px-3.5 py-2 text-on-ember focus:left-2"
      >
        Skip to content
      </a>
      <Rail snapshot={snapshot} chronicle={status} />
      <main
        id="main"
        tabIndex={-1}
        className="relative z-10 max-w-[1240px] px-5 pb-[120px] pt-8 outline-none rail:ml-[232px] rail:px-[44px] rail:pt-10"
      >
        <Page model={model} page={page} params={params} />
      </main>
    </div>
  );
}

export default function App() {
  return (
    <NavigationProvider base={import.meta.env.BASE_URL}>
      <StewardProvider>
        {/* The pending ledger outlives any one page on purpose: an action asked for on the
            Routines page is still unconfirmed while you read the Board, and a receipt that
            disappeared on navigation would be a receipt that told nobody anything. */}
        <LedgerProvider>
          <Shell />
        </LedgerProvider>
      </StewardProvider>
    </NavigationProvider>
  );
}
