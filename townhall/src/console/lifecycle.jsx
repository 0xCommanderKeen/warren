/* Putting a resident's container up, and ending it (warren#331).
 *
 * The record page could show a `retired` badge and the sentence "takes no routines, no board
 * work, no letters" with nothing behind it: steward had no retire route at all, and
 * retirement is not a manifest edit. Writing `retired: true` through the declaration editor
 * marks the resident and leaves its container running on the host with a live village token
 * beside it — the half that matters most left undone. `POST /residents/{id}/retire` runs the
 * whole act in the one safe order; this panel is its button.
 *
 * **The plan comes before the act, always.** Both buttons here rehearse first —
 * `{dry_run: true}` reaches no host, marks nothing and commits nothing — and the real call
 * exists only once steward's own plan is on the screen. That is not a confirmation dialog
 * dressed up: the rehearsal is the *server's* answer, so what an operator confirms is the
 * exact argv and the exact files, not this file's description of them.
 *
 * What the plan names deliberately: `claude/` on the host, which retirement leaves alone.
 * Steward never wrote its contents and a re-provision does not restore them, so removing it
 * would silently require a re-login — an operator who wants it gone is told it is there
 * rather than discovering it a month later.
 *
 * The receipt is the panel's, not the button's, and that is the whole reason this file has
 * two components rather than one. A successful retirement changes what the record says, so
 * the refresh that follows it takes the Retire button off the screen — and a receipt owned
 * by that button would go with it, taking the commit sha and steward's own sentence about
 * what happened to the host along with it.
 */

import { useEffect, useRef, useState } from "react";
import { Link } from "../navigation.jsx";
import { routeTo } from "../routes.js";
import { useSteward } from "../steward/context.jsx";
import { Actions, Button, Facts, Note, Panel, Problem, Receipt, buttonClass } from "./ui.jsx";

/** The two acts, as data: one flow, because the shape of both is plan-then-confirm. */
const ACTS = {
  provision: {
    verb: "Provision",
    running: "provisioning…",
    done: "provisioned",
    plan: "what provisioning would do",
    tone: "primary",
    call: (client, id, body) => client.provisionResident(id, body),
  },
  retire: {
    verb: "Retire",
    running: "retiring…",
    done: "retired",
    plan: "what retiring would do",
    tone: "danger",
    call: (client, id, body) => client.retireResident(id, body),
  },
};

/** The commands steward said it would run, or ran. Verbatim, never paraphrased. */
function Commands({ commands }) {
  if (!commands?.length) return null;
  return (
    <>
      <div className="mb-1.5 mt-4 text-[9.5px] uppercase tracking-[.2em] text-faint">commands</div>
      <ul className="my-0 list-none space-y-1 p-0">
        {commands.map((line) => (
          <li key={line} className="text-[11.5px] text-dim [overflow-wrap:anywhere]">
            {line}
          </li>
        ))}
      </ul>
    </>
  );
}

/** A retirement, as the four facts it actually is — and the one thing it does not touch. */
function RetireFacts({ report, rehearsal }) {
  return (
    <Facts
      pairs={[
        [
          "manifest",
          report.marked
            ? `${rehearsal ? "would be marked" : "marked"} retired: true — ${report.manifest_path}`
            : "already says retired: true",
        ],
        [
          "commit",
          rehearsal
            ? "the mark is committed before the container is stopped, never after"
            : report.commit || "nothing was committed",
        ],
        [
          "container",
          rehearsal
            ? "brought down, then its .env and compose file removed"
            : report.stopped
              ? "down"
              : report.note,
        ],
        [
          "token",
          rehearsal
            ? "the .env holding CHRONICLE_TOKEN is removed after the stop, never before"
            : report.scrubbed
              ? "the .env holding CHRONICLE_TOKEN was removed"
              : "no .env was found to remove",
        ],
        [
          "left in place",
          "residents/<id>/ and its history, the memory directory, and claude/ — which holds " +
            "any `docker exec … claude` login steward did not write and does not restore",
        ],
      ]}
    />
  );
}

/** A provision, in the same shape. */
function ProvisionFacts({ report, rehearsal }) {
  const target = report.provision?.target || {};
  return (
    <Facts
      pairs={[
        ["host", target.host ? `${target.user}@${target.host}:${target.path}` : "not resolved"],
        ["container", target.container],
        ["image", target.image],
        [
          "bundle",
          rehearsal
            ? "compared file by file first; only what differs is sent"
            : report.provision?.sent
              ? "uploaded"
              : "the host already matched, byte for byte",
        ],
        [
          // The one row of a rehearsal that is not a rehearsal. `_register` runs the real
          // `Scheduler.check()` against this host either way — a missing `claude`, a memory
          // directory that is not there — so what is shown here is a result, not a
          // prediction, and saying "would" over it would be the opposite of true. It is
          // also host-local: a resident whose sessions run somewhere else can fail this
          // check here and pass it where it actually runs, which is why the row names the
          // host it was run on rather than reading as a verdict on the deploy.
          "schedule",
          report.register?.ok === false
            ? `checked now, on this host — did not pass: ${(report.register.problems || []).join("; ")}`
            : rehearsal
              ? "checked now, on this host — passes"
              : "checked",
        ],
      ]}
    />
  );
}

/** One report, rendered: steward's plan before the act, or steward's answer after it. */
function Report({ kind, report, rehearsal }) {
  return (
    <div className="mt-3 border border-rule-2 bg-deeper px-[15px] py-3">
      <div className="mb-2 text-[9.5px] uppercase tracking-[.2em] text-faint">
        {rehearsal ? ACTS[kind].plan : "what steward did"}
      </div>
      {kind === "retire" ? (
        <RetireFacts report={report} rehearsal={rehearsal} />
      ) : (
        <ProvisionFacts report={report} rehearsal={rehearsal} />
      )}
      <Commands commands={report.commands || report.provision?.commands} />
      {rehearsal ? (
        <p className="mb-0 mt-3 text-[11.5px] leading-[1.6] text-wait">{report.message}</p>
      ) : null}
    </div>
  );
}

/**
 * One act: rehearse, read the plan, then do it.
 *
 * `onDone` is handed steward's whole answer, because the panel above owns what is said
 * about a finished act — see the note at the top of this file.
 */
function Act({ kind, residentUid, residentId, name, onDone, children }) {
  const { client } = useSteward();
  const act = ACTS[kind];
  const [plan, setPlan] = useState(null);
  const [error, setError] = useState(null);
  const [busy, setBusy] = useState(null);

  const live = useRef(false);
  useEffect(() => {
    live.current = true;
    return () => { live.current = false; };
  }, []);
  const canConfirm = plan?.uid === residentUid && plan?.id === residentId && plan?.kind === kind;

  async function run(dryRun) {
    if (!live.current || busy !== null || (!dryRun && !canConfirm)) return;
    setBusy(dryRun ? "rehearsing" : "running");
    setError(null);
    try {
      const answer = await act.call(client, residentId, {
        dry_run: dryRun,
        ...(kind === "retire" && !dryRun ? { revision: plan?.report.revision } : {}),
      });
      if (!live.current) return;
      if (dryRun) {
        if (answer.resident !== residentId) {
          throw new Error("Steward's plan names a different resident. Rehearse again.");
        }
        setPlan({ uid: residentUid, id: residentId, kind, report: answer });
      } else {
        setPlan(null);
        onDone(answer);
      }
    } catch (caught) {
      if (!live.current) return;
      setError(caught);
      // A refused *real* run leaves the plan on the screen: it is still the plan, and the
      // refusal is usually something to fix and ask again rather than start over.
      if (dryRun) setPlan(null);
    } finally {
      if (live.current) setBusy(null);
    }
  }

  return (
    <>
      {children}
      {error ? <Problem error={error} /> : null}
      {plan ? <Report kind={kind} report={plan.report} rehearsal /> : null}
      <Actions>
        {plan ? (
          <>
            <Button tone={act.tone} onClick={() => run(false)} disabled={busy !== null || !canConfirm}>
              {busy === "running" ? act.running : `${act.verb} ${name} for real`}
            </Button>
            <Button onClick={() => setPlan(null)} disabled={busy !== null}>
              Cancel
            </Button>
          </>
        ) : (
          <Button onClick={() => run(true)} disabled={busy !== null}>
            {busy === "rehearsing" ? "asking steward for the plan…" : `${act.verb}…`}
          </Button>
        )}
        <Note>
          {plan
            ? "This is steward's own plan, not a description of one. The button above runs exactly it."
            : "Shows steward's plan first. Nothing is sent, marked, or committed until you confirm it."}
        </Note>
      </Actions>
    </>
  );
}

/** How a finished act's commit is described, in the shape `Receipt` draws. */
function commitOf(kind, report) {
  if (kind === "provision") {
    return {
      state: "converged",
      note: "provisioning writes nothing into the checkout, so there is no commit to make",
    };
  }
  return report.commit
    ? { state: "committed", sha: report.commit, short: report.commit.slice(0, 10), note: null }
    : { state: "converged", note: "the manifest already said retired, so there was nothing to commit" };
}

/**
 * The record page's lifecycle panel: up, and out.
 *
 * A retired resident is offered Provision and not Retire — there is nothing left to end. But
 * Provision alone is not the way back, and saying so is the point of the paragraph above it:
 * steward refuses to build a container for a manifest that says `retired: true`, because
 * coming back is a person's decision written into the file and committed. So the way back is
 * two steps, and the first is a link to the editor.
 */
export function LifecyclePanel({ resident, refresh }) {
  // UID owns receipts; the path id also invalidates a plan if the resident is renamed.
  return <ResidentLifecycle key={`${resident.uid}:${resident.id}`} resident={resident} refresh={refresh} />;
}

function ResidentLifecycle({ resident, refresh }) {
  const [done, setDone] = useState(null);

  const finish = (kind) => (answer) => {
    setDone({ kind, answer });
    refresh?.();
  };

  return (
    <Panel title="Lifecycle" tone={resident.retired ? "ember" : undefined}>
      {done ? (
        <Receipt
          title={ACTS[done.kind].done}
          status={resident.soul.name}
          commit={commitOf(done.kind, done.answer)}
          onDismiss={() => setDone(null)}
        >
          {done.answer.message}
          <Report kind={done.kind} report={done.answer} rehearsal={false} />
        </Receipt>
      ) : null}

      {resident.retired ? (
        <>
          <p className="mb-3 mt-0 text-[12px] leading-[1.7] text-dim">
            <strong className="text-ink">{resident.soul.name} is retired.</strong> The manifest
            and the soul are still in git — retirement is a lifecycle state, not a deletion —
            and steward refuses to build a container for a declaration that says{" "}
            <code>retired: true</code>. Coming back is two steps, in this order: untick{" "}
            <em>retired</em> in the declaration and save it, so the decision is in git; then
            provision, which puts the container up from the manifest as it stands.
          </p>
          <p className="mb-4 mt-0">
            <Link
              to={routeTo.residentDeclaration(resident.uid)}
              className={buttonClass("ghost", true)}
            >
              Edit the declaration
            </Link>
          </p>
        </>
      ) : null}

      <Act
        key="provision"
        residentUid={resident.uid}
        kind="provision"
        residentId={resident.id}
        name={resident.soul.name}
        onDone={finish("provision")}
      >
        <p className="mb-1 mt-0 text-[12px] leading-[1.7] text-dim">
          <strong className="text-ink">Provision</strong> builds the container from{" "}
          <code>{resident.path}</code> exactly as it stands — routes, app grants and{" "}
          <code>runner.placement</code> included, none of which any form can express. The
          bundle on the host is compared file by file, so a second run sends nothing and still
          reconciles a container that is down.
        </p>
      </Act>

      {resident.retired ? null : (
        <>
          <hr className="my-6 border-0 border-t border-rule" />
          <Act
            key="retire"
            residentUid={resident.uid}
            kind="retire"
            residentId={resident.id}
            name={resident.soul.name}
            onDone={finish("retire")}
          >
            <p className="mb-1 mt-0 text-[12px] leading-[1.7] text-dim">
              <strong className="text-ink">Retire</strong> ends it: <code>retired: true</code>{" "}
              committed, then the container brought down, then its <code>.env</code> — which
              holds the village ingest token — and its compose file removed from the host.
              Marked before stopped, always: the mark is what takes the resident out of the
              watchdog, which would otherwise notice the container go away and put it back.
            </p>
          </Act>
        </>
      )}
    </Panel>
  );
}
