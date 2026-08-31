/* The pending ledger: asked → accepted → confirmed, and never a step skipped.
 *
 * steward's *action* endpoints answer `202 accepted` with a request id, and they mean it —
 * the effect has not happened yet, and steward will not claim it has. So a control panel
 * that turned a 202 green would be lying, and this is the machinery that stops it: every
 * mutating action raises a ticket, and the ticket reaches "confirmed" only by *reading
 * steward's own records back*. Nothing in this file writes that word on the strength of a
 * status code.
 *
 * Ported from the steward console (warren#225) with one bug corrected rather than carried.
 *
 * **#153 — a dismissed ticket kept polling for three minutes.** The console detached the
 * DOM node and left the closure running: `GET /residents` and the whole board every two
 * seconds, per dismissed ticket, and then a full view re-render when the verdict landed,
 * minutes later, on whatever page the operator had since navigated to. Here a ticket owns
 * its poll in its own effect, so dismissing it *unmounts* it and React's cleanup aborts the
 * in-flight fetch and clears the timer on the spot. And "catch the view up" is a callback
 * the raiser supplies, so a settled ticket refreshes the one query it concerns instead of
 * re-fetching a page nobody is looking at.
 */

import { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState } from "react";
import { Badge, Button } from "./ui.jsx";

const POLL_MS = 2000;
const FIRST_POLL_MS = 600;
//: Three minutes of asking, and then it says so instead of asking forever.
const POLL_LIMIT = 90;

const LedgerContext = createContext(null);

export function useLedger() {
  const value = useContext(LedgerContext);
  if (!value) throw new Error("useLedger outside a LedgerProvider");
  return value;
}

let sequence = 0;

export function LedgerProvider({ children }) {
  const [tickets, setTickets] = useState([]);

  const dismiss = useCallback((key) => {
    setTickets((current) => current.filter((ticket) => ticket.key !== key));
  }, []);

  /**
   * Record that steward accepted something, and start reading back what became of it.
   *
   * `confirm(signal)` returns a verdict — `{state, why}` — or `null` for "steward has
   * recorded nothing yet". Returning `null` forever is a real answer and gets said out
   * loud after three minutes; it is not a reason to invent a success.
   */
  const raise = useCallback((ticket) => {
    sequence += 1;
    const key = `ticket-${sequence}`;
    setTickets((current) => [...current, { ...ticket, key }]);
    return key;
  }, []);

  const value = useMemo(() => ({ tickets, raise, dismiss }), [tickets, raise, dismiss]);

  return (
    <LedgerContext.Provider value={value}>
      {children}
      <LedgerRail tickets={tickets} dismiss={dismiss} />
    </LedgerContext.Provider>
  );
}

/** The corner of the screen where everything in flight is listed, and nowhere else. */
function LedgerRail({ tickets, dismiss }) {
  if (!tickets.length) return null;
  return (
    <div
      aria-label="Pending actions"
      className="fixed bottom-4 right-4 z-40 flex max-h-[70vh] w-[min(400px,calc(100vw-2rem))] flex-col gap-2 overflow-y-auto"
    >
      {tickets.map((ticket) => (
        <Ticket key={ticket.key} ticket={ticket} onDismiss={() => dismiss(ticket.key)} />
      ))}
    </div>
  );
}

const STATE_TONE = { asked: "wait", accepted: "wait", confirmed: "live", failed: "fail", refused: "fail" };
const STATE_EDGE = {
  accepted: "border-wait/45 bg-wait/[.05]",
  confirmed: "border-live/45 bg-live/[.05]",
  failed: "border-fail/45 bg-fail/[.06]",
  refused: "border-fail/45 bg-fail/[.06]",
};

/**
 * One action, and what steward has said about it so far.
 *
 * The poll lives in this component's own effect on purpose. Its lifetime is the ticket's
 * lifetime, so there is exactly one place that can stop it and it cannot be forgotten.
 */
export function Ticket({ ticket, onDismiss }) {
  const refused = Boolean(ticket.refused);
  const [verdict, setVerdict] = useState(
    refused ? { state: "refused", why: ticket.why } : { state: "accepted", why: ticket.why },
  );
  // Held in a ref so a raiser that re-renders does not restart a poll that is already
  // running — the effect below depends on the ticket's identity, not on its callbacks.
  const settled = useRef(ticket.onSettled);
  settled.current = ticket.onSettled;

  useEffect(() => {
    if (refused || !ticket.confirm) return undefined;
    const controller = new AbortController();
    let live = true;
    let timer = null;
    let tries = 0;

    const poll = async () => {
      tries += 1;
      let answer = null;
      try {
        answer = await ticket.confirm(controller.signal);
      } catch (error) {
        if (!live || controller.signal.aborted) return;
        setVerdict({
          state: "failed",
          why: `could not read back what happened: ${error?.message || error}`,
        });
        return;
      }
      if (!live) return;
      if (answer) {
        setVerdict(answer);
        settled.current?.(answer);
        return;
      }
      if (tries >= POLL_LIMIT) {
        // Three minutes of silence, said plainly. The console used to explain it with a
        // guess; the honest version names the silence and leaves the diagnosis to the
        // Routines page, which has steward's heartbeat on it.
        setVerdict({
          state: "accepted",
          why:
            "accepted, and steward has recorded no outcome in three minutes. It is queued, " +
            "still running, or nothing is up to run it — the Routines page carries steward's " +
            "own heartbeat, which is the fact that tells those apart.",
        });
        return;
      }
      timer = setTimeout(poll, POLL_MS);
    };

    timer = setTimeout(poll, FIRST_POLL_MS);
    return () => {
      live = false;
      if (timer !== null) clearTimeout(timer);
      controller.abort();
    };
  }, [ticket, refused]);

  return (
    <div className={`border border-l-[3px] px-[15px] py-3 ${STATE_EDGE[verdict.state] || STATE_EDGE.accepted}`}>
      <div className="flex items-baseline justify-between gap-3">
        <span className="min-w-0 truncate text-[12px] text-ink">{ticket.what}</span>
        <span className="flex flex-none items-center gap-2">
          <Badge tone={STATE_TONE[verdict.state] || "wait"}>{verdict.state}</Badge>
          <Button tiny onClick={onDismiss} aria-label="dismiss" title="dismiss" className="border-0 px-1">
            ×
          </Button>
        </span>
      </div>
      <p className="mb-0 mt-1.5 text-[11px] leading-[1.6] text-dim">{verdict.why}</p>
      {ticket.requestId ? (
        <p className="mb-0 mt-1.5 text-[10px] text-faint [overflow-wrap:anywhere]">
          request {ticket.requestId}
        </p>
      ) : null}
    </div>
  );
}

/* -- the confirmations ---------------------------------------------------------------
 *
 * Each one is a *read*. That is the whole rule: the only thing allowed to move a ticket to
 * "confirmed" is steward's own store, answering a question it was asked afterwards.
 * ------------------------------------------------------------------------------------ */

/** A run-now, confirmed by the request log steward wrote it into. */
export const confirmRun = (client, requestId) => async (signal) => {
  const record = await client.readRequest(requestId, { signal });
  if (record.outcome === "queued") return null;
  const detail = record.detail || {};
  if (record.outcome === "ran") {
    return {
      state: "confirmed",
      why: `steward's log: ran${detail.run_id ? ` (run ${detail.run_id})` : ""}.`,
    };
  }
  return {
    state: "failed",
    why: `steward's log: ${record.outcome}${detail.error ? ` — ${detail.error}` : ""}`,
  };
};

/** A decision, confirmed by polling the exact request-log id the POST returned. */
export const confirmDecision = (client, requestId) => async (signal) => {
  const record = await client.readRequest(requestId, { signal });
  if (record.outcome === "recorded_announcement_pending") return null;
  return {
    state: record.outcome === "recorded" ? "confirmed" : "failed",
    why: `steward's log: ${record.outcome}.`,
  };
};

/** A posted job, confirmed by finding it on the board steward keeps. */
export const confirmJob = (client, taskId) => async (signal) => {
  const board = await client.listJobs({ signal });
  const job = (board.jobs || []).find((item) => item.task_id === taskId);
  if (!job) return null;
  return {
    state: "confirmed",
    why:
      `on the board, status ${job.status}. No resident has been prompted — one claims it ` +
      "on its own next wake-up, and task_claimed is the only proof of that.",
  };
};

/**
 * A declared resident, confirmed by watching it come back through the validator.
 *
 * `answer` is the 201 body, and everything said about the host afterwards is quoted off
 * the nursery's own report rather than assumed from the status code.
 */
export const confirmDeclared = (client, answer) => async (signal) => {
  const listing = await client.listResidents({ signal });
  if (!(listing.residents || []).some((item) => item.id === answer.id)) return null;

  const provision = answer.provision;
  if (!provision) {
    return {
      state: "confirmed",
      why:
        "the manifest validates and steward can read it. Nothing is deployed and no routine " +
        "is scheduled: deploy it from the CLI, or declare again with deploy ticked.",
    };
  }
  const host = provision.target ? provision.target.container : answer.id;
  const count = (provision.commands || []).length;
  const did = provision.sent
    ? `the bundle went to ${host} and steward ran ${count} command${count === 1 ? "" : "s"} there`
    : `nothing was uploaded to ${host}: the host already had this bundle, which is what a ` +
      "converged re-run looks like";
  const register = answer.register;
  if (register && register.ok === false) {
    return {
      state: "failed",
      why: `${did}, but the schedule check did not pass: ${(register.problems || []).join("; ")}`,
    };
  }
  const fires = (register && register.next_fires) || [];
  return {
    state: "confirmed",
    why:
      `the manifest validates and steward can read it; ${did}. ` +
      (fires.length
        ? `Next fires: ${fires.map((fire) => `${fire.routine} at ${fire.at}`).join(", ")}.`
        : "No enabled routine, so this resident fires nothing on a schedule.") +
      " It appears in the village when it emits its own first event, and never before.",
  };
};
