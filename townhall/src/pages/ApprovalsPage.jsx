/* Gated actions waiting on a person, and the record of the ones already answered.
 *
 * Steward never invents one of these: a request is created by the session that reached the
 * gate, and this page only ever answers it. Decisions are recorded once — the first wins,
 * and a replay changes nothing.
 *
 * Arcadia stays the *ambient* approvals surface, the knock you notice while watching the
 * village. This is the governance one: pending and decided side by side, the whole detail,
 * the audit row. Both talk to the same steward endpoints, so neither can be more true.
 *
 * **#154 is the reason this page fetches the way it does.** `GET /approvals` applies its
 * expiry filter *only* when the requested status is `pending`, deliberately — steward's own
 * comment says a panel listing an expiring request as answerable would let a human click
 * approve on something the deny-by-default sweep is about to close. The console asked for
 * `status=all` and re-partitioned on `item.status === "pending"`, which reconstructed the
 * pending list *without* the filter that exists to prevent exactly that, and rendered live
 * Approve/Deny buttons on requests steward answers `409 approval_expired`. So: two fetches,
 * `pending` and `resolved`, and the server's definition of pending is the only one. And
 * because a tab left open crosses the deadline by itself, the controls re-check the clock
 * every second and retire themselves — the decision goes away, and the reason is written
 * where the buttons were.
 */

import { useRef, useState } from "react";
import { useSteward, useStewardQuery } from "../steward/context.jsx";
import { Gate } from "../console/Gate.jsx";
import { confirmDecision, useLedger } from "../console/ledger.jsx";
import {
  Actions, Badge, Badges, Button, Clock, Empty, Label, Loading, Note, PageHead, Problem, Row,
  Rows, Section, Stack, Textarea, useNow,
} from "../console/ui.jsx";
import { expired } from "../console/time.js";

//: The marker steward puts where a rendering withheld a secret (steward #144). Spelled here
//: so the edit box can say what leaving it alone means.
const REDACTION = "[redacted:secret]";

const DECIDED_COLUMNS = "1.4fr .8fr .8fr 1fr 1fr";

/* -- one pending request -------------------------------------------------------------- */

function ApprovalCard({ item, onSettled }) {
  const { client } = useSteward();
  const { raise } = useLedger();
  const now = useNow();
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState(() => JSON.stringify(item.detail || {}, null, 2));
  const [complaint, setComplaint] = useState(null);
  const [refusal, setRefusal] = useState(null);
  const [decisionLocked, setDecisionLocked] = useState(false);
  // State alone is one render late: two click handlers can run before React paints the
  // disabled controls. The ref is the synchronous first-decision gate; the state renders it.
  const decisionLock = useRef(false);

  const offered = new Set(item.options || []);
  // #154, the live half. steward filtered this list when it was fetched; a page left open
  // outlives that answer, so the same predicate runs here every second.
  const gone = expired(item.expires_at, now);
  const withheld = draft.includes(REDACTION);

  async function send(decision, edit) {
    if (decisionLock.current) return;
    decisionLock.current = true;
    setDecisionLocked(true);
    setRefusal(null);
    try {
      const answer = await client.decideApproval(
        item.request_id,
        edit === undefined ? { decision } : { decision, edit },
      );
      raise({
        what: `${decision} ${item.action}`,
        requestId: answer.request_id,
        why: answer.message,
        confirm: confirmDecision(client, answer.request_id),
        onSettled,
      });
    } catch (caught) {
      setRefusal(caught);
      // A local refusal means no request left the tab; a 4xx is steward definitively
      // refusing this request. Network/response/server failures are ambiguous — the
      // conditional write may already have won — so unlocking those could submit a
      // contradictory answer while confirmation catches up.
      if (caught?.status === null || (caught?.status >= 400 && caught?.status < 500)) {
        decisionLock.current = false;
        setDecisionLocked(false);
      }
    }
  }

  function sendEdit() {
    setComplaint(null);
    let parsed;
    try {
      parsed = JSON.parse(draft);
    } catch (error) {
      setComplaint(`That is not JSON: ${error.message}. Nothing was sent.`);
      return;
    }
    if (parsed === null || typeof parsed !== "object" || Array.isArray(parsed)) {
      setComplaint(
        "The edit must be a JSON object — steward records it as the modified detail. Nothing was sent.",
      );
      return;
    }
    send("edit", parsed);
  }

  return (
    <section className="rise mb-4 border border-rule bg-deep px-[22px] py-5">
      <div className="flex flex-wrap items-baseline justify-between gap-4">
        <div className="min-w-0">
          <Label>{item.action}</Label>
          <p className="mb-0 mt-1.5 font-serif text-[19px] leading-[1.4] text-read">{item.message}</p>
        </div>
        <Badges>
          <Badge tone="on">{item.resident || item.agent_id}</Badge>
          {item.expires_at ? (
            <Badge tone={gone ? "fail" : "wait"}>
              expires <Clock at={item.expires_at} mode="until" />
            </Badge>
          ) : (
            <Badge>no expiry</Badge>
          )}
        </Badges>
      </div>

      <p className="mb-0 mt-3 text-[11px] text-faint">
        raised <Clock at={item.created_at} /> · {item.request_id}
      </p>

      {Object.keys(item.detail || {}).length ? (
        <details className="mt-3.5" open>
          <summary className="cursor-pointer text-[9.5px] uppercase tracking-[.2em] text-faint">
            detail
          </summary>
          <pre className="mt-2 overflow-x-auto border border-rule-2 bg-void p-3 text-[11.5px] text-dim">
            {JSON.stringify(item.detail, null, 2)}
          </pre>
        </details>
      ) : (
        <p className="mb-0 mt-3.5 text-[11.5px] text-faint">
          The request carries no structured detail.
        </p>
      )}

      {editing ? (
        <div className="mt-3">
          <Textarea
            rows={8}
            value={draft}
            disabled={decisionLocked}
            onChange={(event) => setDraft(event.target.value)}
          />
          {withheld ? (
            <p className="mb-0 mt-1.5 text-[11px] leading-[1.6] text-wait">
              One value here was withheld as a secret and shows as <code>{REDACTION}</code>. Leave
              it exactly as it is and steward restores the real value; you do not need to know it
              to edit the rest.
            </p>
          ) : null}
          {complaint ? <p className="mb-0 mt-1.5 text-[11px] text-fail">{complaint}</p> : null}
        </div>
      ) : null}

      {refusal ? <Problem error={refusal} /> : null}

      <Actions className="mt-4">
        {gone ? (
          <p className="m-0 max-w-[70ch] text-[11.5px] leading-[1.65] text-fail">
            This request passed its deadline while the page was open. Deny-by-default has the
            last word — steward answers <code>409 approval_expired</code> to a decision now,
            and its sweep records the denial against "expiry" rather than against you. The
            controls are gone because pressing them could only ever fail.
          </p>
        ) : (
          <>
            {offered.has("approve") ? (
              <Button disabled={decisionLocked} tone="primary" onClick={() => send("approve")}>
                Approve
              </Button>
            ) : null}
            {offered.has("deny") ? (
              <Button disabled={decisionLocked} tone="danger" onClick={() => send("deny")}>
                Deny
              </Button>
            ) : null}
            {offered.has("edit") ? (
              <Button
                disabled={decisionLocked}
                onClick={() => (editing ? sendEdit() : setEditing(true))}
              >
                {editing ? "Send edit" : "Edit…"}
              </Button>
            ) : null}
            {/* Only what the request offered. steward answers 409
                approval_decision_not_offered to anything else, so drawing a fourth button
                would be drawing a refusal. */}
            <Note>
              {(item.options || []).length ? `options: ${item.options.join(", ")}` : null}
            </Note>
          </>
        )}
      </Actions>
    </section>
  );
}

/* -- the page ------------------------------------------------------------------------- */

function Board() {
  const { client } = useSteward();
  const { data, error, loading, refresh } = useStewardQuery(
    (signal) =>
      Promise.all([
        client.listApprovals("pending", { signal }),
        client.listApprovals("resolved", { signal }),
      ]).then(([waiting, history]) => ({
        // steward's answer, unfiltered and un-repartitioned. Its `pending` already excludes
        // anything past its deadline (#154), and rebuilding that list here is the one thing
        // this page must not do.
        pending: waiting.approvals || [],
        decided: [...(history.approvals || [])].reverse(),
      })),
    [],
  );

  if (loading && !data) return <Loading>reading what is waiting…</Loading>;
  if (error) return <Problem error={error} />;

  return (
    <>
      <Section count={data.pending.length}>Pending</Section>
      {data.pending.length ? (
        data.pending.map((item) => (
          <ApprovalCard key={item.request_id} item={item} onSettled={refresh} />
        ))
      ) : (
        <Empty title="Nothing is waiting on you.">
          No session has reached a gated action, or every request has already been answered. A
          request nobody answers before its expiry resolves itself as a denial, recorded against
          "expiry" rather than against you.
        </Empty>
      )}

      <Section count={data.decided.length}>Decided</Section>
      {data.decided.length ? (
        <Rows>
          <Row head columns={DECIDED_COLUMNS}>
            <span>action</span>
            <span>resident</span>
            <span>decision</span>
            <span>decided by</span>
            <span>when</span>
          </Row>
          {data.decided.map((item) => (
            <Row key={item.request_id} columns={DECIDED_COLUMNS}>
              <Stack sub={item.message}>{item.action}</Stack>
              <span className="text-dim">{item.resident || item.agent_id}</span>
              <Badge tone={item.decision === "approve" ? "live" : "fail"}>
                {item.decision || "—"}
              </Badge>
              <span className="text-dim">{item.decided_by || "—"}</span>
              <Clock at={item.decided_at} />
            </Row>
          ))}
        </Rows>
      ) : (
        <Empty title="Nothing decided yet.">
          This is the audit view — request and decision in one row. It fills up as approvals are
          answered here, from the CLI, or by expiry.
        </Empty>
      )}
    </>
  );
}

export default function ApprovalsPage() {
  const { locked } = useSteward();
  return (
    <>
      <PageHead title="Approvals">
        Gated actions waiting on a person. Steward never invents one of these: a request is
        created by the session that reached the gate, and this page only ever answers it.
        Decisions are recorded once — the first one wins, and a replay changes nothing.
      </PageHead>
      {locked ? <Gate what="The approvals queue" /> : <Board />}
    </>
  );
}
