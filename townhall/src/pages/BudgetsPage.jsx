/* Budgets — the caps, and the spend they are being measured against.
 *
 * The knob and the number come from two different places on purpose. A cap is a *declared*
 * fact and lives in the manifest's `budgets` block, so editing one is a declaration write
 * like any other: validated whole-tree, committed by steward. Spend is a *measured* fact
 * and comes from `GET /residents/{id}/budget`, which is the same sum over the same ledger
 * rows that `steward budget show` prints — no second implementation, and nothing here is
 * projected or extrapolated.
 *
 * Putting them side by side is the whole point of the page: a cap you cannot see today's
 * spend against is a number somebody guessed, and #29's open question — what a real week
 * actually costs — is answered by looking at the two together.
 */

import { useEffect, useState } from "react";
import { Link } from "../navigation.jsx";
import { routeTo } from "../routes.js";
import { useSteward, useStewardQuery } from "../steward/context.jsx";
import { useStewardWrite } from "../steward/useStewardWrite.js";
import { describeCommit, diagnosticsFor } from "../steward/client.js";
import { BUDGET_FIELDS, changed, getIn, numberValue, scalarValue, setIn } from "../manifest.js";
import { Gate } from "../console/Gate.jsx";
import {
  Actions, Badge, Button, Empty, Facts, Field, Gauge, Input, Loading, Note, PageHead, Panel,
  Problem, Receipt, Row, Rows, Section, Stack, Who, buttonClass,
} from "../console/ui.jsx";

const money = (value) => `$${Number(value || 0).toFixed(4)}`;
const stamp = (iso) => {
  const when = new Date(iso);
  return Number.isNaN(when.getTime())
    ? iso
    : when.toLocaleString([], { year: "numeric", month: "short", day: "2-digit", hour: "2-digit", minute: "2-digit" });
};

/* -- the fleet's caps at a glance ---------------------------------------------------- */

const COLUMNS = "1.4fr 1.3fr .8fr .9fr";

function BudgetList() {
  const { client } = useSteward();
  const { data, error, loading } = useStewardQuery((signal) => client.listResidents({ signal }), []);

  if (loading) return <Loading>reading the fleet's ledgers…</Loading>;
  if (error) return <Problem error={error} />;

  const residents = data?.residents || [];
  if (!residents.length) {
    return <Empty title="Nothing to budget.">Steward could validate no resident, so there is no cap to set and no spend to count.</Empty>;
  }

  return (
    <Rows className="rise">
      <Row head columns={COLUMNS}>
        <span>resident</span>
        <span>spent against the worst cap</span>
        <span>today</span>
        <span />
      </Row>
      {residents.map((resident) => {
        const budget = resident.budget || {};
        return (
          <Row key={resident.id} columns={COLUMNS} accent={resident.soul.accent}>
            <Link to={routeTo.budgets(resident.id)} className="text-inherit no-underline">
              <Who
                accent={resident.soul.accent}
                name={resident.soul.name}
                id={resident.id}
                role={resident.soul.role}
                retired={resident.retired}
              />
            </Link>
            <Gauge budget={budget} />
            <Stack sub={`${budget.runs || 0} runs`}>{money(budget.spent_usd)}</Stack>
            <span className="justify-self-end">
              <Link to={routeTo.budgets(resident.id)} className={buttonClass("ghost", true)}>
                set caps
              </Link>
            </span>
          </Row>
        );
      })}
    </Rows>
  );
}

/* -- one resident's caps ------------------------------------------------------------- */

function SpendPanel({ budget }) {
  const spent = budget.spent || {};
  return (
    <Panel title="Spent — measured, not projected">
      <p className="mt-0 mb-3.5 text-[12px] leading-[1.7] text-dim">
        Counted in {budget.window.tz}, for the {budget.window.day} window that runs to{" "}
        {stamp(budget.window.end)}. Every number is a sum over rows steward wrote when a run
        finished, inside a window computed from the calendar at the moment of this request —
        a daily cap that reset because the daemon bounced would not be a cap.
      </p>

      {budget.paused ? (
        <div className="mb-3.5 border border-l-[3px] border-fail/45 bg-fail/[.06] px-[18px] py-[15px]">
          <div className="text-[10px] uppercase tracking-[.16em] text-fail">paused</div>
          <div className="mt-[7px]">
            {budget.pause?.reason}. Scheduled fires and board claims are skipped while this
            stands. Unpausing goes through the ordinary approvals machinery — answer approval{" "}
            <code className="text-ink">{budget.pause?.request_id}</code>, or{" "}
            <code className="text-ink">steward budget unpause {budget.resident}</code>.
          </div>
        </div>
      ) : null}

      {(budget.budgets || []).length ? (
        <Rows>
          <Row head columns="1fr 1fr 1fr 1fr">
            <span>budget</span>
            <span>spent</span>
            <span>limit</span>
            <span>remaining</span>
          </Row>
          {budget.budgets.map((item) => (
            <Row key={item.budget} columns="1fr 1fr 1fr 1fr">
              <span>{item.budget}</span>
              <span className={item.exhausted ? "text-fail" : "text-dim"}>{item.spent}</span>
              <span className="text-dim">{item.limit === null ? "no cap declared" : item.limit}</span>
              <span className="text-dim">{item.remaining === null ? "—" : item.remaining}</span>
            </Row>
          ))}
        </Rows>
      ) : (
        <Empty title="No caps declared.">
          This resident has no budget block, so nothing stops it but its own schedule. That is
          unlimited, not unknown — and the form beside this is how it stops being either.
        </Empty>
      )}

      <Facts
        className="mt-4"
        pairs={[
          [
            "runs",
            `${spent.runs || 0}${spent.unreported_runs ? ` (${spent.unreported_runs} reported no usage)` : ""}`,
          ],
          ["tokens", String(spent.tokens || 0)],
          ["cost", money(spent.cost_usd)],
          ["seconds", String(Math.round(spent.duration_s || 0))],
          ["max run", budget.max_run_seconds ? `${budget.max_run_seconds}s` : null],
          ["allowance", budget.allowance ? `until ${stamp(budget.allowance.until)}` : null],
          ["summary", budget.summary],
        ]}
      />
      {spent.unreported_runs ? (
        <p className="mb-0 mt-3 text-[10.5px] leading-[1.6] text-faint">
          A <code>codex</code> or <code>command</code> session has no usage to report. Steward
          writes those as zero and says how many they were rather than inventing a number, so a
          cap on cost does not bind a runner that cannot be metered.
        </p>
      ) : null}
    </Panel>
  );
}

function CapsForm({ id, declaration, onWritten }) {
  const { client } = useSteward();
  const [draft, setDraft] = useState(declaration.manifest);
  const {
    saving, refusal, receipt, save: write, reset: resetWrite, clearReceipt,
  } = useStewardWrite(
    (manifest) =>
      // A cap is a manifest field, so this is an ordinary declaration write — same
      // whole-tree validation, same commit. There is no budget-shaped write endpoint and
      // there should not be one.
      client.writeDeclaration(id, {
        manifest,
        soul: declaration.soul,
        revision: declaration.revision,
      }),
    { identity: id },
  );

  // Sync the draft to whatever is now on disk, but keep the receipt: re-reading after a
  // save must not sweep away the commit the person is still reading. It clears when a
  // different resident is opened, or by its own ×.
  useEffect(() => {
    setDraft(declaration.manifest);
  }, [declaration]);

  useEffect(() => {
    resetWrite();
  }, [id, resetWrite]);

  const diagnostics = refusal?.diagnostics || [];
  const dirty = changed(draft, declaration.manifest);

  async function save(event) {
    event.preventDefault();
    const answer = await write(draft);
    if (!answer) return;
    onWritten?.();
  }

  return (
    <form onSubmit={save}>
      <Panel title="Caps — declared in the manifest">
        <p className="mt-0 mb-4 text-[12px] leading-[1.7] text-dim">
          A budget is a ceiling, not a forecast. Every field here is optional and an absent one
          means <strong className="font-normal text-ink">unlimited</strong> — said out loud
          rather than assumed. Clearing a box deletes the key; it does not write a zero.
        </p>

        {BUDGET_FIELDS.map((field) => (
          <Field
            key={field.path}
            label={field.label}
            hint={field.hint}
            problems={diagnosticsFor(diagnostics, field.path)}
          >
            <Input
              inputMode="decimal"
              placeholder="unlimited"
              value={scalarValue(getIn(draft, field.path))}
              onChange={(event) => {
                const parsed = numberValue(event.target.value, { integer: field.integer });
                // Something that is not a number at all is kept as typed, so steward is the
                // one that refuses it rather than this form silently dropping it.
                setDraft((previous) =>
                  setIn(previous, field.path, parsed === null ? event.target.value : parsed),
                );
              }}
              invalid={diagnosticsFor(diagnostics, field.path).length > 0}
            />
          </Field>
        ))}

        <p className="mb-0 mt-1 text-[10.5px] leading-[1.6] text-faint">
          This writes the whole declaration, in the <code>manifest</code> spelling — so the
          comments in <code>manifest.yaml</code> do not survive it. To keep them, set the cap in
          the YAML editor on{" "}
          <Link to={routeTo.resident(id)} className="text-ember no-underline">
            the resident's page
          </Link>
          .
        </p>
      </Panel>

      {refusal ? <Problem error={refusal} /> : null}
      {receipt ? (
        <Receipt
          title="caps written"
          status={receipt.status}
          commit={describeCommit(receipt.commit)}
          onDismiss={clearReceipt}
        >
          {receipt.message} A new cap binds the next run, not the one in flight.
        </Receipt>
      ) : null}

      <Actions>
        <Button tone="primary" type="submit" disabled={saving || !dirty}>
          {saving ? "asking steward…" : dirty ? "Write caps" : "Nothing changed"}
        </Button>
        <Link to={routeTo.budgets()} className={buttonClass("ghost")}>
          All budgets
        </Link>
        <Note>Written through the declaration, validated whole-tree, committed by steward.</Note>
      </Actions>
    </form>
  );
}

function ResidentBudget({ id }) {
  const { client } = useSteward();
  const budget = useStewardQuery((signal) => client.readBudget(id, { signal }), [id]);
  const declaration = useStewardQuery((signal) => client.readDeclaration(id, { signal }), [id]);

  if (budget.loading || declaration.loading) return <Loading>reading the ledger…</Loading>;
  if (budget.error) return <Problem error={budget.error} />;
  if (declaration.error) return <Problem error={declaration.error} />;
  if (!budget.data || !declaration.data) return null;

  return (
    <div className="grid gap-4 [grid-template-columns:repeat(auto-fit,minmax(340px,1fr))] items-start">
      <div>
        <CapsForm
          id={id}
          declaration={declaration.data}
          onWritten={() => {
            declaration.refresh();
            budget.refresh();
          }}
        />
      </div>
      <SpendPanel budget={budget.data} />
    </div>
  );
}

/* -- the page ------------------------------------------------------------------------ */

export default function BudgetsPage({ params }) {
  const { locked } = useSteward();
  const id = params.id;

  return (
    <>
      <PageHead
        title={id ? `${id} — budget` : "Budgets"}
        aside={id ? <Badge tone="ember">daily caps</Badge> : null}
      >
        {id ? (
          <>
            The caps this resident is held to, and what it has actually spent against them
            today. The knob is a manifest field and the numbers are steward's own ledger — the
            same sum <code>steward budget show</code> prints, over the same rows, in the same
            window.
          </>
        ) : (
          <>
            What each resident may spend in a local day, and how close it is. An exhausted
            budget pauses the resident: scheduled fires and board claims are skipped, a run-now
            is refused, and exactly one <code>needs_human</code> is raised — however many fires
            are refused afterwards.
          </>
        )}
      </PageHead>

      {locked ? <Gate what="Budgets" /> : id ? <ResidentBudget id={id} /> : (
        <>
          <BudgetList />
          <Section>Where these numbers come from</Section>
          <p className="max-w-[78ch] text-[12px] leading-[1.7] text-dim">
            Spend is read from <code>GET /residents/&#123;id&#125;/budget</code>, which steward has
            served since before the write API and which returns exactly what{" "}
            <code>steward budget show --format json</code> prints for that resident. Townhall adds
            no arithmetic of its own. The one thing the API does not expose is the CLI's{" "}
            <code>--by-origin</code> rollup; nothing on this page needs it, so no endpoint was
            added for it.
          </p>
        </>
      )}
    </>
  );
}
