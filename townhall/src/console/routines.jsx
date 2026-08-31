/* Standing work, and whether anything is keeping the promises it makes.
 *
 * Shared by the fleet-wide Routines page and by one resident's own page, because they are
 * the same rows asked about with a different filter and a ledger that disagreed with itself
 * between two views would be worse than one view.
 *
 * Two things here are load-bearing.
 *
 * **The heartbeat.** A routine's `next_fire` is a promise the *scheduler* keeps, and the
 * scheduler is a different process. steward reports its own liveness on `GET /routines`,
 * including `alive: null` — nothing has ever ticked — as a third answer distinct from a
 * daemon that died. Nothing here infers it.
 *
 * **The last run, beside the last request (warren#104).** `last_request` is the API request
 * log; a scheduled fire is not an HTTP request, so it never appears there, and a panel
 * reading only that told an operator a healthy resident "only runs when I trigger it
 * manually". `last_run` is steward's run ledger, which every finished session writes to —
 * so it carries the trigger and the outcome, and it is the column that answers "did this
 * fire".
 */

import { useState } from "react";
import { Badge, Clock, Row, Rows, Stack } from "./ui.jsx";
import { confirmRun } from "./ledger.jsx";
import { useLedger } from "./ledger.jsx";
import { span } from "./time.js";

export const ROUTINE_COLUMNS = "1fr 1.1fr .95fr 1.1fr 1fr auto";
export const RESIDENT_ROUTINE_COLUMNS = "1fr 1.15fr .95fr 1.1fr auto";

/* -- the scheduler's heartbeat -------------------------------------------------------- */

export function SchedulerBadge({ scheduler }) {
  if (!scheduler) return <Badge>scheduler unknown</Badge>;
  if (scheduler.alive === null) return <Badge tone="fail">scheduler has never ticked</Badge>;
  if (!scheduler.alive) return <Badge tone="fail">scheduler not up</Badge>;
  return <Badge tone="live">scheduler up</Badge>;
}

/** The badge's fine print: what the heartbeat means for the next fires listed below it. */
export function SchedulerNote({ scheduler }) {
  if (!scheduler) {
    return (
      <>
        {" "}
        — this steward reports no heartbeat, so nothing here says whether the next fires below
        will happen.
      </>
    );
  }
  if (scheduler.alive === null) {
    return (
      <>
        {" "}
        — nothing has ever ticked steward's state file, so every next fire below is a promise
        with nobody to keep it.
      </>
    );
  }
  if (!scheduler.alive) {
    return (
      <>
        {" "}
        — last tick <Clock at={scheduler.last_tick} />, older than{" "}
        {span(0, (scheduler.stale_after_s || 0) * 1000)}: nothing below fires until{" "}
        <code>steward scheduler run</code> is up again.
      </>
    );
  }
  return (
    <>
      {" "}
      — last tick <Clock at={scheduler.last_tick} />, so what is listed below really does fire.
    </>
  );
}

/* -- run now -------------------------------------------------------------------------- */

/* What steward answers a run-now for a retired resident: 409 resident_retired. The button
 * says so here rather than sending the request and rendering the refusal, because a control
 * that can only ever fail should look like one before it is pressed. This is the console's
 * honesty rule — grey out from steward's own answers — and the disabled state carries the
 * reason, since a control whose refusal is invisible is worse than no control. */
export const RETIRED_REFUSAL =
  "This resident is retired — its manifest declares retired: true — so steward answers " +
  "409 resident_retired to a run-now, fires no routine and claims no board work. Set " +
  "retired: false and commit that decision to bring it back.";

export const DISABLED_REFUSAL =
  "This routine is disabled in the manifest. Enable it there rather than firing something " +
  "the declaration says is off.";

export function RunButton({ row, client, onSettled }) {
  const { raise } = useLedger();
  const [asking, setAsking] = useState(false);
  const refusal = row.retired ? RETIRED_REFUSAL : row.enabled ? null : DISABLED_REFUSAL;

  async function run() {
    setAsking(true);
    try {
      const answer = await client.runRoutine(row.resident, row.routine);
      raise({
        what: `run ${row.key}`,
        requestId: answer.request_id,
        why:
          "accepted, not yet confirmed — steward has queued one run and will record what it " +
          `came to. ${answer.message}`,
        confirm: confirmRun(client, answer.request_id),
        onSettled,
      });
    } catch (error) {
      raise({
        what: `run ${row.key}`,
        refused: true,
        why: `${error.code} — ${error.message}`,
      });
    } finally {
      setAsking(false);
    }
  }

  return (
    <button
      type="button"
      onClick={run}
      disabled={asking || Boolean(refusal)}
      title={refusal || undefined}
      className="inline-flex cursor-pointer items-center rounded-none border border-rule bg-transparent px-2.5 py-[5px] font-mono text-[10px] uppercase leading-none tracking-[.12em] text-dim transition-colors hover:border-rule-lit hover:text-ink disabled:cursor-not-allowed disabled:border-rule-2 disabled:text-faint"
    >
      {asking ? "asking…" : "run now"}
    </button>
  );
}

/* -- the two columns that say what happened ------------------------------------------- */

const RUN_TONE = { ok: "live", ran: "live", queued: "wait" };

/**
 * The last run steward's ledger recorded (warren#104), with how it was started.
 *
 * `null` — nothing has ever finished — is said as "never run", which is a different fact
 * from a run that failed and must not be drawn as one.
 */
export function LastRun({ run }) {
  if (!run) return <span className="text-faint">never run</span>;
  return (
    <Stack
      sub={
        <>
          {run.trigger || "trigger not recorded"} · <Clock at={run.recorded_at} />
        </>
      }
    >
      <Badge tone={RUN_TONE[run.outcome] || "fail"}>{run.outcome || "no outcome"}</Badge>
    </Stack>
  );
}

/** The last run *somebody asked for over HTTP*, which is a different question. */
export function LastRequest({ request }) {
  if (!request) return <span className="text-faint">none through this API</span>;
  const outcome = request.outcome || "";
  return (
    <Stack sub={<Clock at={request.received_at} />}>
      <Badge tone={RUN_TONE[outcome] || "fail"}>{outcome}</Badge>
    </Stack>
  );
}

/* -- the rows ------------------------------------------------------------------------- */

/** One resident's routines: the same ledger, without the resident column. */
export function ResidentRoutines({ rows, client, onSettled }) {
  return (
    <Rows>
      <Row head columns={RESIDENT_ROUTINE_COLUMNS}>
        <span>routine</span>
        <span>schedule</span>
        <span>next</span>
        <span>last run</span>
        <span />
      </Row>
      {rows.map((row) => (
        <Row key={row.key} columns={RESIDENT_ROUTINE_COLUMNS}>
          <Stack
            sub={
              <>
                {row.enabled ? `timeout ${row.timeout_s}s` : "disabled in the manifest"}
                {row.journal ? " · closes the day" : ""}
              </>
            }
          >
            {row.routine}
          </Stack>
          <Stack sub={row.schedule_tz}>
            <span className="[overflow-wrap:anywhere]">{row.schedule}</span>
          </Stack>
          <Stack sub={row.anchor ? <>anchored <Clock at={row.anchor} /></> : "never fired"}>
            {row.retired ? (
              <span className="text-faint">never</span>
            ) : row.enabled ? (
              <Clock at={row.next_fire} mode="until" />
            ) : (
              <span className="text-faint">—</span>
            )}
          </Stack>
          <LastRun run={row.last_run} />
          <RunButton row={row} client={client} onSettled={onSettled} />
        </Row>
      ))}
    </Rows>
  );
}
